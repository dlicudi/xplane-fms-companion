from dataclasses import dataclass, field
from typing import List


@dataclass
class FlightPlanInfo:
    filename: str
    full_path: str
    display_name: str
    dep: str
    dest: str
    cycle: str
    dep_runway: str = ""
    dest_runway: str = ""
    sid: str = ""
    star: str = ""
    waypoint_count: int = 0
    total_distance_nm: float = 0.0
    waypoint_list: str = ""
    max_altitude: int = 0
    file_timestamp: float = 0.0
    file_mtime: float = 0.0


@dataclass
class FlightPlanEntry:
    entry_type: int
    ident: str
    altitude: int
    lat: float
    lon: float


# ── Validation ──────────────────────────────────────────────────────────────

# Severity levels — used as plain strings so they compare easily in UI code
SEVERITY_INFO  = "INFO"
SEVERITY_WARN  = "WARN"
SEVERITY_ERROR = "ERROR"


@dataclass
class ValidationIssue:
    severity: str        # SEVERITY_INFO | SEVERITY_WARN | SEVERITY_ERROR
    code: str            # e.g. "NO_SID", "DUPLICATE_FIX"
    message: str         # human-readable one-liner
    affected_index: int = -1   # 0-based FMS entry index; -1 if not entry-specific
    suggestion: str = ""       # advisory text shown below the message


# ── Procedures ───────────────────────────────────────────────────────────────

@dataclass
class ProcedureInfo:
    name: str            # raw procedure identifier, e.g. "CHATY5", "I06L"
    proc_type: str       # "SID" | "STAR" | "APP"
    transition: str      # runway for SID (e.g. "06B"), entry fix for STAR, blank for APP common
    waypoints: List[str] # ordered fix idents (vector legs excluded)
    display_name: str    # human-readable, e.g. "CHATY5 06B", "ILS 06L"
    display_runway: str  # runway portion for the second column, e.g. "06B", "24R"
