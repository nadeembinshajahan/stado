"""Open-vocabulary target acquisition: turn a text description into a box.

Two backends (pluggable via GROUNDING_BACKEND):
  - "qwen":      Qwen vision (settings.qwen_vision_model) spatial grounding (cloud)
  - "moondream": the user's local moondream Flask service (:5000 /process)

Both return a list of normalized [x0, y0, x1, y1] boxes for `description`.
The pipeline matches the best box to the nearest live track.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .. import qwen

log = logging.getLogger("gcs.grounding")

MOONDREAM_URL = "http://127.0.0.1:5000/process"


async def ground_moondream(jpeg: bytes, description: str) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            MOONDREAM_URL,
            files={"frame": ("frame.jpg", jpeg, "image/jpeg")},
            data={"object": description, "boxes": "1"},
        )
        r.raise_for_status()
        return r.json().get("boxes", [])


async def ground_qwen(jpeg: bytes, description: str) -> list[list[float]]:
    """Qwen vision grounding → normalized [x0,y0,x1,y1] boxes (0-1)."""
    prompt = (
        f"Detect {description} in this image. Return ONLY JSON: a list like "
        '[{"box_2d":[ymin,xmin,ymax,xmax],"label":"..."}] with coordinates '
        "normalized 0-1000 (top-left origin). Return [] if none are present."
    )
    text = (await qwen.vision_chat(jpeg, prompt)) or ""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[list[float]] = []
    for item in arr:
        # The model returns [ymin, xmin, ymax, xmax] in 0-1000; key varies.
        coords = item.get("box_2d") or item.get("box") if isinstance(item, dict) else item
        if not coords or len(coords) != 4:
            continue
        ymin, xmin, ymax, xmax = coords
        out.append([xmin / 1000.0, ymin / 1000.0, xmax / 1000.0, ymax / 1000.0])
    return out


_PLATE_CHARS_RE = re.compile(r"[A-Z0-9]")


async def read_plate(jpeg: bytes) -> str | None:
    """Ask the vision model to read a vehicle license plate from a (cropped) JPEG.

    Returns the raw plate characters the model reports, or None when the plate
    is not clearly legible / no plate is present. Never raises — returns None on
    any error so the caller's loop just retries next interval.
    """
    prompt = (
        "Read the vehicle license plate in this image. "
        "Return ONLY the plate characters with no spaces or punctuation "
        "(e.g. KA01AB1234), or the single word NONE if no plate is clearly "
        "legible."
    )
    text = ((await qwen.vision_chat(jpeg, prompt)) or "").strip().upper()
    if not text or "NONE" in text:
        return None
    # Keep only plate-legal characters from whatever the model returned.
    cleaned = "".join(_PLATE_CHARS_RE.findall(text))
    # A plausible Indian plate is roughly 6-11 alphanumerics.
    if not (6 <= len(cleaned) <= 11):
        return None
    return cleaned


async def describe_scene(jpeg: bytes, question: str) -> str | None:
    """Describe what's in the drone camera view (for STADO's 'tell me what you see').
    Returns a terse 1-2 sentence description, or None on error."""
    prompt = (
        "You are the eyes of a surveillance drone. In ONE or TWO terse sentences, describe what is "
        "visible in this camera view — notable people, vehicles, structures, terrain — like a radio "
        "operator giving a sitrep. Be factual and brief; no preamble."
    )
    if question:
        prompt += f" The operator specifically asks: {question}"
    return await qwen.vision_chat(jpeg, prompt)


async def resolve_target(
    jpeg: bytes, description: str, backend: str = "qwen"
) -> list[float] | None:
    """Return the best single normalized box for `description`, or None."""
    try:
        if backend == "moondream":
            boxes = await ground_moondream(jpeg, description)
        else:
            boxes = await ground_qwen(jpeg, description)
    except Exception as exc:  # noqa: BLE001
        log.warning("grounding (%s) failed: %s", backend, exc)
        return None
    if not boxes:
        return None
    # Largest box = most prominent instance.
    best = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return best
