"""Demo-only frontend patches for the SITL demo (applied in-repo, committed).

Ports of the inline `sed` patches from the stado-demo Dockerfile, Stage A:

  1. Hide <VideoPanel /> + <SecondFeedPanel /> — the demo runs SITL-only with
     no real camera; the placeholders look broken to a reviewer.
  2. Point both hardcoded map fallbacks (Dubai) at the SITL spawn (Bengaluru)
     so a cold-start reviewer isn't staring at empty desert.
  3. Disable the geolocation/IP-fallback map override — on a cloud deploy the
     first MAVLink fix can arrive after the 1.5 s fallback timer, which would
     snap the map to the REVIEWER's street instead of the drones.

Idempotent: each step checks its marker / target before rewriting.
Run from the repo root:  python3 scripts/patch_frontend_demo.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FE = ROOT / "frontend" / "src"

SPAWN_LAT, SPAWN_LON = "13.078065", "77.644651"  # PX4 SITL home (Bengaluru)

changed: list[str] = []


def patch(path: pathlib.Path, transforms: list[tuple[str, str]], marker: str) -> None:
    src = path.read_text()
    if marker in src:
        print(f"  {path.name}: already patched — skip")
        return
    orig = src
    for pattern, repl in transforms:
        new = re.sub(pattern, repl, src)
        if new == src:
            sys.exit(f"patch_frontend_demo: pattern not found in {path}: {pattern!r}")
        src = new
    if src != orig:
        path.write_text(src)
        changed.append(path.name)
        print(f"  {path.name}: patched")


# 1. Hide the video panels (absolutely positioned — removal disturbs no layout).
patch(
    FE / "App.tsx",
    [
        (re.escape("<VideoPanel />"), "<></>{/* DEMO: no camera in SITL */}"),
        (re.escape("<SecondFeedPanel />"), "<></>"),
        (r"import VideoPanel from [^\n]*\n", ""),
        (r"import SecondFeedPanel from [^\n]*\n", ""),
    ],
    marker="DEMO: no camera in SITL",
)

# 2a. MapView fallback center → SITL spawn.
patch(
    FE / "components" / "MapView.tsx",
    [(
        r"FALLBACK_CENTER = \{ lat: [0-9.]+, lng: [0-9.]+ \}",
        f"FALLBACK_CENTER = {{ lat: {SPAWN_LAT}, lng: {SPAWN_LON} }} /* DEMO: SITL spawn */",
    )],
    marker="DEMO: SITL spawn",
)

# 2b. Map3DView fallback → SITL spawn.
patch(
    FE / "components" / "Map3DView.tsx",
    [
        (re.escape("homePos?.lat ?? 25.2048"), f"homePos?.lat ?? {SPAWN_LAT} /* DEMO */"),
        (re.escape("homePos?.lng ?? 55.2708"), f"homePos?.lng ?? {SPAWN_LON}"),
    ],
    marker="homePos?.lat ?? " + SPAWN_LAT,
)

# 3. Kill the geolocation/IP override — stay on FALLBACK_CENTER until a fix.
patch(
    FE / "components" / "MapView.tsx",
    [(
        re.escape("if (done.current) return; // the drone-fix effect already centered us"),
        "if (true) return; // DEMO: geolocation override disabled — hold FALLBACK_CENTER until drone fix",
    )],
    marker="DEMO: geolocation override disabled",
)

print("patch_frontend_demo: done" + (f" — changed {changed}" if changed else " (no-op)"))
