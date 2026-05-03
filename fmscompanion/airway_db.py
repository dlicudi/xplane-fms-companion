"""Airway graph built from X-Plane's earth_awy.dat.

Loaded lazily on first use, then cached for the session.
"""

from __future__ import annotations

import math
import os
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from XPPython3 import xp


# {airway_name: {fix_ident: {neighbor_ident, ...}}}
_graph: Dict[str, Dict[str, Set[str]]] = {}
_fixes: Dict[str, List[Tuple[float, float]]] = {}
_loaded = False


def find_path(fix1: str, fix2: str, airway: str) -> Optional[List[str]]:
    """Return ordered fix names from fix1 to fix2 along airway (inclusive), or None."""
    _ensure_loaded()
    adj = _graph.get(airway)
    if not adj or fix1 not in adj:
        return None
    # BFS — airways are short linear chains, so this is fast.
    queue: deque[List[str]] = deque([[fix1]])
    visited: Set[str] = {fix1}
    while queue:
        path = queue.popleft()
        if path[-1] == fix2:
            return path
        for nb in adj.get(path[-1], ()):
            if nb not in visited:
                visited.add(nb)
                queue.append(path + [nb])
    return None


def resolve_fix(ident: str,
                hint_lat: Optional[float] = None,
                hint_lon: Optional[float] = None) -> Optional[Tuple[float, float, float]]:
    """Return (lat, lon, dist_nm) for an exact fix ident from earth_fix.dat."""
    _ensure_loaded()
    if not _fixes:
        fix_path = _find_fix_dat()
        if fix_path:
            _load_fixes(fix_path)
        gns_path = _find_gns_waypoints()
        if gns_path:
            _load_gns_waypoints(gns_path)
    candidates = _fixes.get(ident.strip().upper())
    if not candidates:
        return None
    if hint_lat is None or hint_lon is None:
        lat, lon = candidates[0]
        return lat, lon, 0.0
    best = min(
        candidates,
        key=lambda pos: _haversine_nm(hint_lat, hint_lon, pos[0], pos[1]),
    )
    return best[0], best[1], _haversine_nm(hint_lat, hint_lon, best[0], best[1])


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    path = _find_dat()
    if path:
        _load(path)
    fix_path = _find_fix_dat()
    if fix_path:
        _load_fixes(fix_path)
    gns_path = _find_gns_waypoints()
    if gns_path:
        _load_gns_waypoints(gns_path)
    _loaded = True


def _find_dat() -> Optional[str]:
    root = xp.getSystemPath()
    for sub in ("Custom Data", os.path.join("Resources", "default data")):
        p = os.path.join(root, sub, "earth_awy.dat")
        if os.path.isfile(p):
            return p
    return None


def _find_fix_dat() -> Optional[str]:
    root = xp.getSystemPath()
    for sub in ("Custom Data", os.path.join("Resources", "default data")):
        p = os.path.join(root, sub, "earth_fix.dat")
        if os.path.isfile(p):
            return p
    return None


def _find_gns_waypoints() -> Optional[str]:
    root = xp.getSystemPath()
    p = os.path.join(root, "Custom Data", "GNS430", "navdata", "Waypoints.txt")
    return p if os.path.isfile(p) else None


def _load(path: str):
    # Build adjacency sets treating all segments as bidirectional — direction
    # restrictions are procedural and not enforced by the G1000 FMS anyway.
    graph: Dict[str, Dict[str, Set[str]]] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) != 11:
                continue
            try:
                int(parts[2])   # fix1 type — must be numeric
            except ValueError:
                continue
            fix1, fix2, awy = parts[0], parts[3], parts[10]
            adj = graph.setdefault(awy, {})
            adj.setdefault(fix1, set()).add(fix2)
            adj.setdefault(fix2, set()).add(fix1)
    _graph.update(graph)


def _load_fixes(path: str):
    fixes: Dict[str, List[Tuple[float, float]]] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                lat = float(parts[0])
                lon = float(parts[1])
            except ValueError:
                continue
            ident = parts[2].strip().upper()
            if not ident or ident in ("99", "I"):
                continue
            fixes.setdefault(ident, []).append((lat, lon))
    _fixes.update(fixes)


def _load_gns_waypoints(path: str):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            ident = parts[0].upper()
            try:
                lat = float(parts[1])
                lon = float(parts[2])
            except ValueError:
                continue
            if not ident:
                continue
            _fixes.setdefault(ident, []).append((lat, lon))


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
