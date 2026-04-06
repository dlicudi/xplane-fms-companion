"""METAR fetch, wind parsing, and runway ranking.

Uses xp.getMETARForAirport (XP12 SDK) to get the reported weather at a specific
airport — not the sim's live weather at the aircraft position.

Public API:
    fetch_metar(icao)           → raw METAR string (empty string on failure)
    parse_wind(metar)           → (direction_deg | None, speed_kt | None)
    runway_heading(rwy_id)      → magnetic heading derived from runway identifier
    rank_runways(rwys, dir, spd)→ list of (rwy_id, headwind_kt, crosswind_kt)
                                  sorted by headwind descending (best first)
"""

import math
import re
from typing import List, Optional, Tuple

from XPPython3 import xp


def fetch_metar(icao: str) -> str:
    """Return the current METAR string for *icao* from X-Plane's weather system.

    Returns an empty string if the airport is not found, the SDK call is
    unavailable (pre-XP12.1), or any other error occurs.
    """
    if not icao:
        return ""
    try:
        result = xp.getMETARForAirport(icao.upper())
        return result if isinstance(result, str) else ""
    except Exception:
        return ""


def parse_wind(metar: str) -> Tuple[Optional[float], Optional[float]]:
    """Extract surface wind from a METAR string.

    Returns ``(direction_deg, speed_kt)``.
    - Direction is ``None`` for variable-direction winds (VRB).
    - Both are ``None`` if no wind group was found.
    - Both are ``0.0`` for calm (00000KT).

    Handles gusts (reported speed is the mean, not the gust).
    Handles both KT and MPS (MPS converted to knots).
    """
    if not metar:
        return None, None

    # Calm: 00000KT
    if re.search(r'\b00000KT\b', metar):
        return 0.0, 0.0

    # Variable direction: VRBssKT or VRBssGggKT
    m = re.search(r'\bVRB(\d{2,3})(?:G\d{2,3})?KT\b', metar)
    if m:
        return None, float(m.group(1))

    # Standard KT: dddssKT or dddssGggKT
    m = re.search(r'\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b', metar)
    if m:
        return float(m.group(1)), float(m.group(2))

    # MPS (convert to knots: 1 m/s ≈ 1.944 kt)
    m = re.search(r'\b(\d{3})(\d{2,3})(?:G\d{2,3})?MPS\b', metar)
    if m:
        return float(m.group(1)), round(float(m.group(2)) * 1.944, 1)

    return None, None


def runway_heading(rwy_id: str) -> float:
    """Derive the approximate magnetic heading from a runway identifier.

    Examples: "28L" → 280°, "36" → 360°, "01" → 10°, "9R" → 90°.
    """
    # Strip suffix (L / R / C / W / T) and leading zeros
    num_str = re.sub(r'[LRCWT]$', '', rwy_id.strip().upper())
    try:
        num = int(num_str)
    except ValueError:
        return 0.0
    return 360.0 if num == 0 else float(num * 10)


def rank_runways(
    rwy_ids: List[str],
    wind_dir: float,
    wind_spd: float,
) -> List[Tuple[str, float, float]]:
    """Rank runways by headwind component (most favourable first).

    Args:
        rwy_ids:  List of runway identifiers (e.g. ["06L", "06R", "24L", "24R"]).
        wind_dir: Wind direction in degrees magnetic (the direction *from* which
                  the wind blows, per METAR convention).
        wind_spd: Wind speed in knots.

    Returns:
        List of ``(rwy_id, headwind_kt, crosswind_kt)`` sorted by headwind
        descending. Negative headwind = tailwind.
    """
    results = []
    for rwy in rwy_ids:
        hdg   = runway_heading(rwy)
        angle = math.radians(hdg - wind_dir)
        headwind  = round(wind_spd * math.cos(angle), 1)
        crosswind = round(abs(wind_spd * math.sin(angle)), 1)
        results.append((rwy, headwind, crosswind))
    results.sort(key=lambda x: x[1], reverse=True)
    return results
