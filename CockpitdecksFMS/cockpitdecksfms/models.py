from dataclasses import dataclass
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
    # File modification time (epoch seconds) for "newest first" sort
    file_mtime: float = 0.0


@dataclass
class FlightPlanEntry:
    entry_type: int
    ident: str
    altitude: int
    lat: float
    lon: float


@dataclass
class ProcedureInfo:
    name: str            # raw procedure identifier, e.g. "CHATY5", "I06L"
    proc_type: str       # "SID" | "STAR" | "APP"
    transition: str      # runway for SID (e.g. "06B"), entry fix for STAR, blank for APP common
    waypoints: List[str] # ordered fix idents (vector legs excluded)
    display_name: str    # human-readable, e.g. "CHATY5 06B", "ILS 06L"
    display_runway: str  # runway portion for the second column, e.g. "06B", "24R"
