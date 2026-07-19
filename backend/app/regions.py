"""Operator-defined named SEARCH/SURVEY areas (oriented rectangles), synced from
the GCS frontend so the voice agent knows them by name — e.g. "survey Sector 1".
Mirrors pois.py (markers); this is the region counterpart."""
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
# [{id, name, center:[lat,lon], width_m, height_m, heading_deg}]
_regions: list[dict] = []


def set_regions(items: list[dict]) -> None:
    """Replace the region set (the frontend POSTs its full list on every change)."""
    global _regions
    cleaned = []
    for r in items or []:
        try:
            c = r["center"]
            cleaned.append({
                "id": r.get("id"),
                "name": str(r.get("name", "") or "").strip(),
                "center": [float(c[0]), float(c[1])],
                "width_m": float(r["width_m"]),
                "height_m": float(r["height_m"]),
                "heading_deg": float(r.get("heading_deg", 0) or 0),
            })
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    with _lock:
        _regions = cleaned


def get_regions() -> list[dict]:
    with _lock:
        return list(_regions)


def find(name: str) -> dict | None:
    """Resolve a spoken/typed region name to a single region, or None.

    Matching tiers (each scoped to its own candidate set; the FIRST tier that
    yields exactly one match wins). On more than one match in a tier the result
    is AMBIGUOUS and we return None rather than guessing — so "Sector 1" never
    silently resolves to "Sector 12".

      1. exact (case-insensitive),
      2. word-boundary (the query appears as a whole word in the name),
      3. prefix (the name starts with the query, or vice versa).
    """
    if not name:
        return None
    s = name.strip().lower()
    if not s:
        return None
    with _lock:
        names = [(r, (r["name"] or "").lower()) for r in _regions]
    return _resolve(s, names)


def context_line() -> str:
    """One line listing the search areas, for the voice agent's system context."""
    rs = get_regions()
    if not rs:
        return ""
    items = "; ".join(
        f"'{r['name']}' ({r['width_m']:.0f}x{r['height_m']:.0f} m, centre "
        f"{r['center'][0]:.5f},{r['center'][1]:.5f})"
        for r in rs if r["name"]
    )
    if not items:
        return ""
    return (
        "OPERATOR-DEFINED SEARCH AREAS currently on the map (refer to them by name): " + items +
        ". When the operator says 'survey <name>' / 'search <name>', call survey_region with that "
        "name — do NOT treat a search area as a marker (it is a region, not a point)."
    )
