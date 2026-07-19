"""Qwen chat-completions client (Alibaba Cloud Model Studio).

Thin async wrapper over the OpenAI-compatible endpoint
(`settings.qwen_openai_base_url`) used by every NON-realtime model call in the
GCS: vision grounding (target boxes, plate reads, scene description), satellite
survey-perimeter detection, and mission-report summarization. The realtime
voice session speaks the native realtime WebSocket protocol instead — see
`voice_qwen.py`.

Degrades gracefully: every helper returns None when the key/SDK is missing or
the call fails, so callers keep their own deterministic fallbacks and nothing
user-facing hard-crashes on a network blip.
"""
from __future__ import annotations

import base64
import logging

from .config import settings

log = logging.getLogger("gcs.qwen")


def client():
    """An AsyncOpenAI client for Model Studio's compatible-mode endpoint, or
    None when DASHSCOPE_API_KEY / the openai SDK is unavailable."""
    if not settings.dashscope_api_key:
        return None
    try:
        from openai import AsyncOpenAI
    except Exception as exc:  # noqa: BLE001
        log.warning("openai SDK not installed: %s", exc)
        return None
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.qwen_openai_base_url,
    )


def _first_text(resp) -> str | None:
    try:
        text = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError):
        return None
    return text or None


async def vision_chat(
    image: bytes, prompt: str, *, mime: str = "image/jpeg", model: str | None = None
) -> str | None:
    """One image + one prompt → the model's text reply (None on any failure)."""
    cl = client()
    if cl is None:
        return None
    b64 = base64.b64encode(image).decode()
    try:
        resp = await cl.chat.completions.create(
            model=model or settings.qwen_vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("qwen vision call failed: %s", exc)
        return None
    return _first_text(resp)


async def text_chat(prompt: str, *, model: str | None = None) -> str | None:
    """One text prompt → the model's text reply (None on any failure)."""
    cl = client()
    if cl is None:
        return None
    try:
        resp = await cl.chat.completions.create(
            model=model or settings.qwen_reasoning_model,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("qwen text call failed: %s", exc)
        return None
    return _first_text(resp)
