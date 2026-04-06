"""Route validation — pure Python, no X-Plane SDK dependency.

Entry point:
    issues = validate(entries, plan)

All checks operate on the parsed .fms data only (FlightPlanEntry list + FlightPlanInfo).
Nothing here reads live sim state; that belongs in nav_monitor.py (Phase 2).
"""

import math
from typing import List

from fmscompanion.models import (
    FlightPlanEntry,
    FlightPlanInfo,
    ValidationIssue,
    SEVERITY_INFO,
    SEVERITY_WARN,
    SEVERITY_ERROR,
)

# FMS entry type codes
_TYPE_AIRPORT = 1
_TYPE_LATLON  = 28

# Thresholds
_JUMP_MULTIPLIER   = 3.0   # flag a leg if > this × median leg distance
_JUMP_MIN_NM       = 300.0 # …and longer than this absolute minimum (avoids flagging short routes)
_DUPE_WINDOW       = 3     # check for duplicate ident within this many consecutive entries


def validate(entries: List[FlightPlanEntry], plan: FlightPlanInfo) -> List[ValidationIssue]:
    """Return a list of ValidationIssue for the given route. Empty list = no problems found."""
    issues: List[ValidationIssue] = []

    _check_route_size(entries, issues)
    if not entries:
        return issues  # nothing more to check on an empty route

    _check_departure(entries, plan, issues)
    _check_arrival(entries, plan, issues)
    _check_missing_sid(plan, issues)
    _check_missing_star(plan, issues)
    _check_invalid_coords(entries, issues)
    _check_discontinuities(entries, issues)
    _check_duplicate_fixes(entries, issues)
    _check_route_jumps(entries, issues)

    return issues


# ── Individual checks ────────────────────────────────────────────────────────

def _check_route_size(entries: List[FlightPlanEntry], issues: List[ValidationIssue]) -> None:
    if len(entries) == 0:
        issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="NO_ROUTE",
            message="FMS route is empty.",
            suggestion="Load a flight plan from the LOAD tab.",
        ))
    elif len(entries) == 1:
        issues.append(ValidationIssue(
            severity=SEVERITY_WARN,
            code="SINGLE_ENTRY",
            message="Route has only one entry — likely incomplete.",
            suggestion="Check that the full flight plan was loaded.",
        ))


def _check_departure(
    entries: List[FlightPlanEntry],
    plan: FlightPlanInfo,
    issues: List[ValidationIssue],
) -> None:
    first = entries[0]
    if first.entry_type != _TYPE_AIRPORT:
        issues.append(ValidationIssue(
            severity=SEVERITY_WARN,
            code="NO_DEPARTURE",
            message=f"First waypoint '{first.ident}' is not an airport fix.",
            affected_index=0,
            suggestion="The route should start with the departure airport.",
        ))


def _check_arrival(
    entries: List[FlightPlanEntry],
    plan: FlightPlanInfo,
    issues: List[ValidationIssue],
) -> None:
    last = entries[-1]
    if last.entry_type != _TYPE_AIRPORT:
        issues.append(ValidationIssue(
            severity=SEVERITY_WARN,
            code="NO_ARRIVAL",
            message=f"Last waypoint '{last.ident}' is not an airport fix.",
            affected_index=len(entries) - 1,
            suggestion="The route should end with the destination airport.",
        ))


def _check_missing_sid(plan: FlightPlanInfo, issues: List[ValidationIssue]) -> None:
    dep = (plan.dep or "").strip()
    if dep and dep != "----" and not (plan.sid or "").strip():
        issues.append(ValidationIssue(
            severity=SEVERITY_WARN,
            code="NO_SID",
            message=f"No SID selected for departure {dep}.",
            suggestion="Browse departure procedures on the DEP tab.",
        ))


def _check_missing_star(plan: FlightPlanInfo, issues: List[ValidationIssue]) -> None:
    dest = (plan.dest or "").strip()
    if dest and dest != "----" and not (plan.star or "").strip():
        issues.append(ValidationIssue(
            severity=SEVERITY_WARN,
            code="NO_STAR",
            message=f"No STAR selected for arrival {dest}.",
            suggestion="Browse arrival procedures on the ARR tab.",
        ))


def _check_invalid_coords(
    entries: List[FlightPlanEntry],
    issues: List[ValidationIssue],
) -> None:
    for i, e in enumerate(entries):
        if e.entry_type == _TYPE_LATLON:
            continue  # lat/lon entries at 0,0 are theoretically valid (unlikely but skip)
        if e.lat == 0.0 and e.lon == 0.0:
            issues.append(ValidationIssue(
                severity=SEVERITY_WARN,
                code="INVALID_COORDS",
                message=f"Waypoint '{e.ident}' (#{i + 1}) has zero coordinates — may not have resolved.",
                affected_index=i,
                suggestion="Clear and re-add this waypoint, or reload the plan.",
            ))


def _check_discontinuities(
    entries: List[FlightPlanEntry],
    issues: List[ValidationIssue],
) -> None:
    for i, e in enumerate(entries):
        ident = (e.ident or "").strip()
        if not ident or ident in ("----", "DISCO") or ident.upper().startswith("DISCO"):
            issues.append(ValidationIssue(
                severity=SEVERITY_WARN,
                code="DISCONTINUITY",
                message=f"Discontinuity at entry #{i + 1} (ident: '{ident or 'empty'}').",
                affected_index=i,
                suggestion="Clear this entry or insert the missing waypoint.",
            ))


def _check_duplicate_fixes(
    entries: List[FlightPlanEntry],
    issues: List[ValidationIssue],
) -> None:
    seen_at: dict = {}  # ident → last index where it appeared
    for i, e in enumerate(entries):
        ident = (e.ident or "").strip().upper()
        if not ident or ident in ("----",):
            continue
        prev = seen_at.get(ident, -1)
        if prev >= 0 and (i - prev) <= _DUPE_WINDOW:
            issues.append(ValidationIssue(
                severity=SEVERITY_INFO,
                code="DUPLICATE_FIX",
                message=f"'{ident}' appears at both #{prev + 1} and #{i + 1}.",
                affected_index=i,
                suggestion=f"Clear the duplicate at #{i + 1} using the ROUTE tab.",
            ))
        seen_at[ident] = i


def _has_valid_coords(entry: FlightPlanEntry) -> bool:
    """Return False if the entry has unresolved zero coords (not a deliberate lat/lon fix)."""
    return entry.entry_type == _TYPE_LATLON or not (entry.lat == 0.0 and entry.lon == 0.0)


def _check_route_jumps(
    entries: List[FlightPlanEntry],
    issues: List[ValidationIssue],
) -> None:
    if len(entries) < 2:
        return

    # Skip legs where either endpoint has unresolved (0, 0) coords — those are
    # already flagged by _check_invalid_coords and would produce phantom 2000+ nm
    # distances that corrupt the median and generate misleading ROUTE_JUMP warnings.
    # Track (from_idx, to_idx, distance) so we can report correct entry numbers.
    valid_legs = [
        (i - 1, i, _haversine_nm(entries[i - 1].lat, entries[i - 1].lon,
                                  entries[i].lat,     entries[i].lon))
        for i in range(1, len(entries))
        if _has_valid_coords(entries[i - 1]) and _has_valid_coords(entries[i])
    ]

    if not valid_legs:
        return

    distances = [d for _, _, d in valid_legs]
    median    = _median(distances)
    threshold = max(_JUMP_MULTIPLIER * median, _JUMP_MIN_NM)

    for from_idx, to_idx, dist in valid_legs:
        if dist > threshold:
            issues.append(ValidationIssue(
                severity=SEVERITY_WARN,
                code="ROUTE_JUMP",
                message=(
                    f"Large leg #{from_idx + 1}→#{to_idx + 1} "
                    f"({entries[from_idx].ident} → {entries[to_idx].ident}): "
                    f"{dist:.0f} nm (median {median:.0f} nm)."
                ),
                affected_index=to_idx,
                suggestion="Check for a missing waypoint or an out-of-sequence fix.",
            ))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R_NM = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]
