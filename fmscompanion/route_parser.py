"""Route string parser and resolver.

Parses a user-typed route string into classified tokens and resolves each
token against the X-Plane nav database using chained lat/lon hints — each
lookup is biased by the previous waypoint's position, which kills the
duplicate-ident ambiguity (e.g. two "GRICE" fixes, one in Scotland, one in
Louisiana).

No airway expansion. Procedures are flagged for the user to apply via the
DEP/ARR/APP tabs; we do not try to execute them here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from XPPython3 import xp

from fmscompanion import airway_db
from fmscompanion.models import FlightPlanEntry

# ── Heuristics for tokens we won't resolve as navaids ───────────────────────

# Airway: 1-2 letters + 1-4 digits, optional trailing letter.  P600, UN601, V23, B28Y.
_RE_AIRWAY    = re.compile(r"^[A-Z]{1,2}\d{1,4}[A-Z]?$")
# Procedure ident: letters ending in digit + optional letter.  GRIC3C, LAM1A, BOYNE1Y.
_RE_PROCEDURE = re.compile(r"^[A-Z]{2,}\d[A-Z]?$")
# ICAO airport code.
_RE_AIRPORT   = re.compile(r"^[A-Z]{4}$")
# ICAO flight-plan speed/level token.  N0172F040, M082F350, K0430S1130.
_RE_SPEED_LVL = re.compile(r"^[NMK]\d{3,4}[FAS]\d{3,4}$")
# Airport with filed-runway suffix.  EGPN/27, EGPD/34L.
_RE_AIRPORT_RWY = re.compile(r"^([A-Z]{4})/(\d{1,2}[LRCTlrct]?)$")

# A resolved navaid must be within this distance of the chained position hint,
# otherwise the SDK picked a same-name fix in the wrong hemisphere.
_NAV_MATCH_TOLERANCE_NM = 600.0

_NAVAID_TYPES = (xp.Nav_Fix, xp.Nav_VOR, xp.Nav_NDB)

# FlightPlanEntry.entry_type codes — mirrors PythonInterface.FMS_TYPE_TO_NAV.
_NAV_TO_FMS_TYPE = {
    xp.Nav_Airport: 1,
    xp.Nav_NDB:     2,
    xp.Nav_VOR:     3,
    xp.Nav_Fix:     11,
}


@dataclass
class ParsedToken:
    raw: str
    category: str              # airport | navaid | airway | procedure | unknown
    status:   str              # ok | skipped | not_found | too_far
    message:  str = ""
    entry: Optional[FlightPlanEntry] = None   # set when status == "ok"


def parse_route_string(text: str,
                       hint_lat: Optional[float] = None,
                       hint_lon: Optional[float] = None) -> List[ParsedToken]:
    """Tokenize the user's route string and resolve each token.

    hint_lat / hint_lon seed the chained-position lookup so the first middle
    token has something to anchor to even when the route doesn't start with
    an airport. Pass current aircraft position when available.
    """
    tokens = [t for t in text.strip().upper().split() if t]
    out: List[ParsedToken] = []
    if not tokens:
        return out

    last_idx = len(tokens) - 1
    prev_lat, prev_lon = hint_lat, hint_lon

    # Pre-calculate direct distance for dynamic tolerance calculation
    total_dist = 0.0
    if len(tokens) >= 2:
        def _get_ident(raw: str) -> str:
            m = _RE_AIRPORT_RWY.match(raw)
            return m.group(1) if m else raw

        dep_pos = _resolve_airport(_get_ident(tokens[0]))
        arr_pos = _resolve_airport(_get_ident(tokens[-1]))
        if dep_pos and arr_pos:
            total_dist = _haversine_nm(dep_pos[0], dep_pos[1], arr_pos[0], arr_pos[1])

    tolerance = max(_NAV_MATCH_TOLERANCE_NM, total_dist * 0.5)

    for i, raw in enumerate(tokens):
        is_endpoint = (i == 0 or i == last_idx)

        # The first and last tokens are always the departure and arrival airport.
        # They are never classified as navaids, airways, or procedures.
        if is_endpoint:
            m = _RE_AIRPORT_RWY.match(raw)
            if m:
                icao, rwy = m.group(1), m.group(2)
                resolved = _resolve_airport(icao)
                if resolved:
                    lat, lon = resolved
                    entry = FlightPlanEntry(entry_type=1, ident=icao, altitude=0, lat=lat, lon=lon)
                    out.append(ParsedToken(raw=raw, category="airport", status="ok",
                                          message=f"filed RWY {rwy}", entry=entry))
                    prev_lat, prev_lon = lat, lon
                else:
                    out.append(ParsedToken(raw=raw, category="airport", status="not_found",
                                          message="airport not in nav database"))
            else:
                resolved = _resolve_airport(raw)
                if resolved:
                    lat, lon = resolved
                    entry = FlightPlanEntry(entry_type=1, ident=raw, altitude=0, lat=lat, lon=lon)
                    out.append(ParsedToken(raw=raw, category="airport", status="ok", entry=entry))
                    prev_lat, prev_lon = lat, lon
                else:
                    out.append(ParsedToken(raw=raw, category="airport", status="not_found",
                                          message="airport not in nav database"))
            continue

        # ICAO flight-plan noise we ignore silently.  "DCT" = direct (no-op)
        # and tokens like "N0172F040" encode filed speed/level, not a fix.
        if raw == "DCT" or _RE_SPEED_LVL.match(raw):
            out.append(ParsedToken(
                raw=raw, category="filler", status="skipped",
                message="ICAO route keyword - ignored",
            ))
            continue

        # Airway / procedure shape recognition BEFORE navaid lookup.
        # Tokens like "P600" match the airway pattern but also happen to
        # resolve as distant NDBs in the world database, producing a bogus
        # "too_far" warning. Classify by shape up-front so we don't try.
        if _RE_AIRWAY.match(raw):
            out.append(ParsedToken(
                raw=raw, category="airway", status="skipped",
                message="airway - no expansion",
            ))
            continue
        if _RE_PROCEDURE.match(raw):
            out.append(ParsedToken(
                raw=raw, category="procedure", status="skipped",
                message="procedure - apply via DEP/ARR/APP",
            ))
            continue

        # Navaid search (Fix / VOR / NDB), biased by the chained position.
        resolved = _resolve_navaid(raw, prev_lat, prev_lon)
        if resolved is not None:
            nav_type, lat, lon, dist_nm = resolved
            have_hint = prev_lat is not None and prev_lon is not None
            if have_hint and dist_nm > tolerance:
                out.append(ParsedToken(
                    raw=raw, category="navaid", status="too_far",
                    message=f"nearest match {dist_nm:.0f} nm from prior fix",
                ))
                continue
            fms_type = _NAV_TO_FMS_TYPE.get(nav_type, 11)
            entry = FlightPlanEntry(entry_type=fms_type, ident=raw, altitude=0, lat=lat, lon=lon)
            message = "exact fix" if nav_type == xp.Nav_Fix else ""
            out.append(ParsedToken(raw=raw, category="navaid", status="ok",
                                   message=message, entry=entry))
            prev_lat, prev_lon = lat, lon
            continue

        # Nothing matched any known shape or resolved in the nav DB.
        out.append(ParsedToken(
            raw=raw, category="unknown", status="not_found",
            message="not in nav database",
        ))

    return _dedup_consecutive(_expand_airways(out))


# ── Internals ───────────────────────────────────────────────────────────────


def _dedup_consecutive(tokens: List[ParsedToken]) -> List[ParsedToken]:
    """Drop consecutive tokens with the same ident (e.g. SID name = entry fix)."""
    out: List[ParsedToken] = []
    for t in tokens:
        if out and t.entry and out[-1].entry and t.entry.ident == out[-1].entry.ident:
            continue
        out.append(t)
    return out


def _expand_airways(tokens: List[ParsedToken]) -> List[ParsedToken]:
    """Replace each airway token with its intermediate waypoints where possible."""
    result: List[ParsedToken] = []
    for i, tok in enumerate(tokens):
        if tok.category != "airway":
            result.append(tok)
            continue

        prev = next((t for t in reversed(result) if t.entry is not None), None)
        nxt  = next((t for t in tokens[i + 1:] if t.entry is not None), None)

        if prev is None or nxt is None:
            result.append(tok)
            continue

        path = airway_db.find_path(prev.entry.ident, nxt.entry.ident, tok.raw)
        if path is None:
            result.append(ParsedToken(
                raw=tok.raw, category="airway", status="skipped",
                message=f"airway - no path {prev.entry.ident}-{nxt.entry.ident}",
            ))
            continue

        # path[0] = prev fix (already in result), path[-1] = nxt fix (added later)
        prev_lat, prev_lon = prev.entry.lat, prev.entry.lon
        for ident in path[1:-1]:
            resolved = _resolve_navaid(ident, prev_lat, prev_lon)
            if resolved is None:
                result.append(ParsedToken(
                    raw=ident, category="navaid", status="not_found",
                    message=f"via {tok.raw}",
                ))
            else:
                nav_type, lat, lon, _ = resolved
                fms_type = _NAV_TO_FMS_TYPE.get(nav_type, 11)
                entry = FlightPlanEntry(entry_type=fms_type, ident=ident, altitude=0, lat=lat, lon=lon)
                result.append(ParsedToken(
                    raw=ident, category="navaid", status="ok",
                    message=f"via {tok.raw}", entry=entry,
                ))
                prev_lat, prev_lon = lat, lon

    return result


def _resolve_airport(ident: str) -> Optional[Tuple[float, float]]:
    try:
        nav_ref = xp.findNavAid(None, ident, None, None, None, xp.Nav_Airport)
    except Exception:
        return None
    if nav_ref == xp.NAV_NOT_FOUND:
        return None
    return _nav_position(nav_ref)


def _resolve_navaid(ident: str,
                    hint_lat: Optional[float],
                    hint_lon: Optional[float]) -> Optional[Tuple[int, float, float, float]]:
    """Return (nav_type, lat, lon, dist_from_hint_nm) of the closest matching
    navaid across Fix/VOR/NDB types, or None if no type matches."""
    best: Optional[Tuple[int, float, float, float]] = None
    exact_fix = airway_db.resolve_fix(ident, hint_lat, hint_lon)
    if exact_fix is not None:
        lat, lon, dist = exact_fix
        best = (xp.Nav_Fix, lat, lon, dist)
    for nav_type in _NAVAID_TYPES:
        try:
            nav_ref = xp.findNavAid(None, ident, hint_lat, hint_lon, None, nav_type)
        except Exception:
            continue
        if nav_ref == xp.NAV_NOT_FOUND:
            continue
        pos = _nav_position(nav_ref)
        if pos is None:
            continue
        lat, lon = pos
        if hint_lat is not None and hint_lon is not None:
            dist = _haversine_nm(hint_lat, hint_lon, lat, lon)
        else:
            dist = 0.0
        if best is None or dist < best[3]:
            best = (nav_type, lat, lon, dist)
    return best


def _nav_position(nav_ref) -> Optional[Tuple[float, float]]:
    try:
        info = xp.getNavAidInfo(nav_ref)
    except Exception:
        return None
    lat = getattr(info, "latitude", None)
    if lat is None:
        lat = getattr(info, "lat", None)
    lon = getattr(info, "longitude", None)
    if lon is None:
        lon = getattr(info, "lon", None)
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R_NM = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
