"""Operator-placed named points of interest (markers), synced from the GCS
frontend so the voice agent has them as context — e.g. "orbit Sector 1"."""
from __future__ import annotations

import re
import threading


def _resolve(query: str, candidates: list) -> dict | None:
    """Pick a single record from `candidates` (a list of (record, lower_name))
    for a lowercased `query` using exact → word-boundary → prefix tiers. Returns
    None when a tier has zero or MORE-THAN-ONE matches (ambiguous), so a loose
    query like "Sector 1" never silently resolves to "Sector 12"."""
    # 1) exact.
    exact = [rec for rec, n in candidates if n == query]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    # 2) word-boundary: the query appears as a whole word inside the name.
    pat = re.compile(r"\b" + re.escape(query) + r"\b")
    wb = [rec for rec, n in candidates if n and pat.search(n)]
    if len(wb) == 1:
        return wb[0]
    if len(wb) > 1:
        return None
    # 3) prefix: name starts with query, or query starts with name.
    pref = [rec for rec, n in candidates if n and (n.startswith(query) or query.startswith(n))]
    if len(pref) == 1:
        return pref[0]
    return None


_lock = threading.Lock()
_pois: list[dict] = []  # [{id, name, lat, lng}]


def set_pois(items: list[dict]) -> None:
    """Replace the marker set (the frontend POSTs its full list on every change)."""
    global _pois
    cleaned = []
    for i in items or []:
        try:
            cleaned.append({
                "id": i.get("id"),
                "name": str(i.get("name", "") or "").strip(),
                "lat": float(i["lat"]),
                "lng": float(i["lng"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    with _lock:
        _pois = cleaned


def get_pois() -> list[dict]:
    with _lock:
        return list(_pois)


def find(name: str) -> dict | None:
    """Resolve a spoken/typed marker name to a single marker, or None.

    Matching tiers (exact → word-boundary → prefix); on more than one match in a
    tier the result is AMBIGUOUS and None is returned rather than guessing — so
    "Sector 1" never silently resolves to "Sector 12". See `_resolve`."""
    if not name:
        return None
    s = name.strip().lower()
    if not s:
        return None
    with _lock:
        names = [(p, (p["name"] or "").lower()) for p in _pois]
    return _resolve(s, names)


def context_line() -> str:
    """One line listing the marked points, for the voice agent's system context."""
    ps = get_pois()
    if not ps:
        return ""
    pts = "; ".join(f"'{p['name']}' ({p['lat']:.5f},{p['lng']:.5f})" for p in ps if p["name"])
    if not pts:
        return ""
    return (
        "OPERATOR-MARKED POINTS currently on the map (refer to them by name): " + pts +
        ". When the operator names one (e.g. 'orbit Sector 1', 'fly to the LZ'), use "
        "orbit_point / goto_point with that name."
    )
