"""Mission-report summary generation.

Builds a concise, operator-grade summary of a recorded flight from its stats,
mode timeline, agent-action timeline and start location, using the Qwen
reasoning model (settings.qwen_reasoning_model, via the Model Studio
OpenAI-compatible endpoint). Degrades gracefully: on ANY error (no key,
network, bad response) it returns a deterministic summary built from the same
stats, so the report / PDF never breaks.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import qwen

log = logging.getLogger("gcs.report")


def _fmt_duration(s: float | None) -> str:
    if not s or s <= 0:
        return "unknown duration"
    m = int(s // 60)
    sec = int(round(s % 60))
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _fmt_dist(m: float | None) -> str:
    if m is None:
        return "unknown"
    if m >= 1000:
        return f"{m / 1000:.2f} km"
    return f"{round(m)} m"


def _coord(c: dict[str, Any] | None) -> str | None:
    if not c:
        return None
    lat, lon = c.get("lat"), c.get("lon")
    if lat is None or lon is None:
        return None
    return f"{lat:.6f}, {lon:.6f}"


def _local_time(ts: float | None) -> str:
    if ts is None:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def build_facts(flight: dict[str, Any]) -> dict[str, Any]:
    """Distil a flight record into the structured facts the prompt is built on."""
    actions = flight.get("actions", []) or []
    modes = flight.get("mode_timeline", []) or []
    return {
        "vehicle": flight.get("vehicle_name") or flight.get("vehicle_id") or "drone",
        "duration_s": flight.get("duration_s"),
        "distance_m": flight.get("distance_m"),
        "max_alt_m": flight.get("max_alt_m"),
        "max_speed_ms": flight.get("max_speed_ms"),
        "battery_start_pct": flight.get("battery_start_pct"),
        "battery_min_pct": flight.get("battery_min_pct"),
        "battery_used_pct": flight.get("battery_used_pct"),
        "takeoff": _coord(flight.get("takeoff")),
        "landing": _coord(flight.get("landing")),
        "modes": [m.get("mode") for m in modes if m.get("mode")],
        "n_modes": len(modes),
        "actions": [
            f"{_local_time(act.get('ts'))} {act.get('label')}"
            f"{'' if act.get('ok', True) else ' (FAILED)'}"
            for act in actions
        ],
        "n_actions": len(actions),
        "n_events": len(flight.get("events", []) or []),
    }


def fallback_summary(flight: dict[str, Any]) -> str:
    """Deterministic summary from stats — used when the model is unavailable."""
    f = build_facts(flight)
    bits: list[str] = []
    bits.append(
        f"{f['vehicle']} flew for {_fmt_duration(f['duration_s'])}, "
        f"covering {_fmt_dist(f['distance_m'])} with a peak altitude of "
        f"{(f['max_alt_m'] or 0):.0f} m and a top ground speed of "
        f"{(f['max_speed_ms'] or 0):.1f} m/s."
    )
    if f["modes"]:
        uniq: list[str] = []
        for m in f["modes"]:
            if m not in uniq:
                uniq.append(m)
        bits.append("Flight modes used: " + ", ".join(uniq) + ".")
    if f["n_actions"]:
        bits.append(
            f"The agent executed {f['n_actions']} commanded "
            f"action{'s' if f['n_actions'] != 1 else ''} during the flight."
        )
    if f["battery_used_pct"] is not None:
        bits.append(f"Battery consumed: {f['battery_used_pct']}%.")
    return " ".join(bits)


def _prompt(flight: dict[str, Any]) -> str:
    f = build_facts(flight)
    lines = [
        "You are a flight-operations analyst writing the headline summary of a "
        "drone mission report. Write 2 to 4 concise, professional sentences "
        "summarizing the mission for an operator. Be factual, specific and "
        "neutral. Do not use bullet points, markdown, or a preamble — output "
        "only the summary prose.",
        "",
        "MISSION FACTS:",
        f"- Vehicle: {f['vehicle']}",
        f"- Duration: {_fmt_duration(f['duration_s'])}",
        f"- Total distance: {_fmt_dist(f['distance_m'])}",
        f"- Max altitude (rel): {(f['max_alt_m'] or 0):.1f} m",
        f"- Max ground speed: {(f['max_speed_ms'] or 0):.1f} m/s",
    ]
    if f["battery_used_pct"] is not None:
        lines.append(
            f"- Battery: {f['battery_start_pct']}% -> {f['battery_min_pct']}% "
            f"(used {f['battery_used_pct']}%)"
        )
    if f["takeoff"]:
        lines.append(f"- Takeoff location: {f['takeoff']}")
    if f["landing"]:
        lines.append(f"- Landing location: {f['landing']}")
    if f["modes"]:
        lines.append("- Mode timeline: " + " -> ".join(f["modes"]))
    if f["actions"]:
        lines.append("- Agent (STADO) action timeline:")
        lines.extend(f"    {a}" for a in f["actions"])
    else:
        lines.append("- Agent action timeline: none recorded.")
    lines.append(f"- Notable events recorded: {f['n_events']}")
    return "\n".join(lines)


async def generate_summary(flight: dict[str, Any]) -> str:
    """Generate the mission summary via the Qwen reasoning model, falling back
    deterministically.

    Never raises — returns prose either way."""
    text = await qwen.text_chat(_prompt(flight))
    if not text:
        log.warning("report summary model unavailable — using deterministic fallback")
        return fallback_summary(flight)
    return text
