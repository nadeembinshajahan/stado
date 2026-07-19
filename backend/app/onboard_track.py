"""Onboard (Jetson) target-tracker control channel.

Outrider runs an onboard CSRT/MIL tracker INSIDE its VIO pipeline (oakd_tracker),
which owns the OAK-D. It listens on a UDP control port and burns the lock reticle
straight into the RGB stream the GCS already shows as Outrider's feed — so a box
drawn on Outrider's feed locks ONBOARD (lowest latency), no video round-trip.

Protocol (ASCII, UDP → Jetson):
    SEED <x> <y> <w> <h>          normalized 0..1 (top-left x,y + size) → lock
    CLEAR                         stop tracking / drop the reticle
    FOLLOW <0|1> [profile]        disable/enable the onboard follow controller.
                                  Optional trailing token selects the per-class
                                  speed envelope the controller should fly with
                                  (person|car|custom); omitted ⇒ keep current.
    PROFILE <person|car|custom>   set the follow speed/behaviour envelope WITHOUT
                                  changing the enable state (so the operator can
                                  pre-select "this is a car" before FOLLOW 1).
The Jetson replies BOX <tracking|lost> <x> <y> <w> <h> per frame (not consumed
here yet — the reticle is visible in the feed; BOX ingestion for geolocation is a
later add).

PROFILE / the trailing FOLLOW token is BACKWARD-COMPATIBLE: an older onboard
handler that only understands `FOLLOW <0|1>` simply ignores the extra token, and
a bare `FOLLOW 1` / `FOLLOW 0` still works. The per-class speed envelopes live on
the Jetson (oakd_tracker follow controller) — see reviews/outrider-follow-readiness.md
for the controller spec; the GCS only NAMES the profile to fly.

Why GCS-side classification: the GCS already knows the target class — the operator
says "follow the car"/"the person", or VLM grounding returns a label — so it
maps that to a profile name and tells the controller which envelope to use. This
avoids running a second onboard detector (YOLO auto-classify) just to pick a speed
band (heavier; see the readiness doc for that alternative).
"""
from __future__ import annotations

import logging
import socket

from .config import settings

log = logging.getLogger("gcs.onboard_track")

# Profiles the onboard controller knows. Kept here so the GCS validates the name
# before it goes on the wire (a typo'd profile must not silently fly the wrong
# envelope). "custom" lets the controller fall back to its compiled defaults.
PROFILES = ("person", "car", "custom")


def normalize_profile(name: str | None) -> str | None:
    """Map a free-form class/description token to a known profile name, else None.

    Accepts the canonical names plus the common synonyms the GCS produces from a
    VLM label or the operator's words ("truck"/"vehicle" → car, "man"/"woman"/
    "pedestrian" → person). Returns None for anything unrecognized so callers can
    leave the controller on its current/default envelope rather than guess.
    """
    if not name:
        return None
    t = str(name).strip().lower()
    if t in PROFILES:
        return t
    car_words = ("car", "truck", "pickup", "vehicle", "van", "suv", "sedan",
                 "lorry", "jeep", "bus", "motorcycle", "bike", "scooter")
    person_words = ("person", "man", "woman", "people", "pedestrian", "runner",
                    "human", "guy", "boy", "girl", "child", "kid", "individual")
    if any(w in t for w in car_words):
        return "car"
    if any(w in t for w in person_words):
        return "person"
    return None


def _send(msg: str) -> dict:
    host = settings.outrider_jetson_host
    if not host:
        return {"ok": False, "reason": "Outrider Jetson host not configured (set OUTRIDER_JETSON_HOST)"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.sendto(msg.encode("ascii"), (host, settings.outrider_onboard_track_port))
        s.close()
        log.info("onboard-track → %s:%d  %s", host, settings.outrider_onboard_track_port, msg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — surface the reason to the operator
        return {"ok": False, "reason": str(e)}


def seed(x: float, y: float, w: float, h: float) -> dict:
    """Seed the onboard tracker with a normalized 0..1 box."""
    if w <= 0 or h <= 0:
        return {"ok": False, "reason": "degenerate box"}
    return _send(f"SEED {x:.4f} {y:.4f} {w:.4f} {h:.4f}")


def clear() -> dict:
    return _send("CLEAR")


def set_profile(name: str | None) -> dict:
    """Select the onboard follow speed/behaviour envelope by target class.

    `name` may be a canonical profile ("person"/"car"/"custom") or any class word
    the GCS already has (e.g. a VLM label "truck") — it is normalized first.
    Sends `PROFILE <name>` over :8771. Does NOT enable/disable follow. Returns
    {"ok": False, ...} for an unrecognized profile so a typo never flies the wrong
    envelope. Falls back to the configured default when `name` is empty/None.
    """
    # Empty/None ⇒ fall back to the configured default envelope. A NON-empty but
    # unrecognized name is an ERROR (don't guess — never fly the wrong envelope).
    if not name:
        profile = normalize_profile(settings.outrider_follow_profile)
    else:
        profile = normalize_profile(name)
    if profile is None:
        return {"ok": False, "reason": f"unknown follow profile: {name!r}",
                "profiles": list(PROFILES)}
    res = _send(f"PROFILE {profile}")
    return {**res, "profile": profile}


def follow(enable: bool, profile: str | None = None) -> dict:
    """Enable/disable the onboard follow controller (FOLLOW 1 / FOLLOW 0).

    When enabling, an optional `profile` (target class) selects the per-class
    speed envelope: it is sent as the trailing token of `FOLLOW 1 <profile>` so a
    single datagram both arms follow AND sets the envelope. The token is
    normalized (so a raw class word works) and validated; an unrecognized profile
    is dropped to a bare `FOLLOW 1` rather than failing the follow. A
    backward-compatible onboard handler ignores the extra token. `FOLLOW 0` is
    always sent bare (no envelope needed to stop).
    """
    if not enable:
        return {**_send("FOLLOW 0"), "follow": False, "profile": None}
    prof = normalize_profile(profile)
    if prof is None and profile:
        log.warning("onboard-track: unknown follow profile %r — sending bare FOLLOW 1", profile)
    msg = f"FOLLOW 1 {prof}" if prof else "FOLLOW 1"
    return {**_send(msg), "follow": True, "profile": prof}
