"""
PI_FMSCompanion — XPPython3 plugin entry point.

X-Plane FMS Companion: standalone copilot assistant for the stock X-Plane FMS / G1000.
Loads flight plans, browses SID/STAR/APP procedures from CIFP, maintains a scrollable
LEGS list with waypoint selection/activation/direct-to, and provides a Dear ImGui window
with LOAD / NAV / ROUTE / DEP / ARR / APP / CHECK tabs.

This plugin exposes no datarefs or commands to external tools. All interaction is through
the plugin window (Plugins → FMS Companion → Show / Hide FMS Companion Window) or the
single toggle command: fmscompanion/toggle_window.

Package layout (fmscompanion/):
  models.py       — FlightPlanInfo, FlightPlanEntry, ProcedureInfo dataclasses
  fms_io.py       — FMS file parsing, loading into the X-Plane FMS SDK
  fms_state.py    — Live FMS state reads (active leg, entry count, idents)
  legs.py         — Scrollable LEGS list with selection/activation/direct-to
  plan_browser.py — Plan list display, sorting, row selection
  procedures.py   — CIFP parsing, SID/STAR/APP browser, FMS procedure insertion
  validator.py    — Route anomaly checks → list of ValidationIssue
  ui.py           — Dear ImGui window and tab layout
"""

import json
import os
import sys
import time
from datetime import datetime  # noqa: F401 — used in _cmd_dump_state
from typing import Dict, List, Optional

# Make the fmscompanion package importable when deployed as a flat copy alongside
# the fmscompanion/ package folder in PythonPlugins/.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from XPPython3 import xp

from fmscompanion.fms_io import FmsIOMixin
from fmscompanion.fms_state import FmsStateMixin
from fmscompanion.legs import LegsMixin
from fmscompanion.models import FlightPlanInfo, ValidationIssue, SEVERITY_INFO, SEVERITY_WARN
import re as _re

from fmscompanion.metar import fetch_metar, parse_wind, rank_runways
from fmscompanion.nav_monitor import NavMonitorMixin
from fmscompanion.plan_browser import PlanBrowserMixin
from fmscompanion.validator import validate
from fmscompanion.procedures import ProceduresMixin
from fmscompanion.ui import UIMixin


class PythonInterface(FmsIOMixin, FmsStateMixin, PlanBrowserMixin, LegsMixin, ProceduresMixin, NavMonitorMixin, UIMixin):
    NAME    = "FMS Companion"
    SIG     = "xppython3.fmscompanion"
    DESC    = (
        "X-Plane FMS Companion: flight plan loader, procedure browser (SID/STAR/APP), "
        "scrollable LEGS list with waypoint activation/direct-to, and live NAV data display."
    )
    RELEASE = __import__("fmscompanion").__version__

    # Command prefix used by _create_command when no explicit prefix is given
    CMD_PREFIX = "fmscompanion"

    # ── Layout constants ──
    PLAN_LIST_VISIBLE_ROWS = 10
    PLAN_LIST_MAX_PLANS    = 10
    LEGS_VISIBLE_ROWS      = 10
    PROC_VISIBLE_ROWS      = 10

    # ── Procedure kinds ──
    KINDS           = ("dep", "arr", "app")
    _KIND_CIFP_TYPE = {"dep": "SID", "arr": "STAR", "app": "APP"}
    _APP_TYPE_LABELS = {
        "I": "ILS", "R": "RNAV", "V": "VOR", "N": "NDB", "L": "LOC",
        "D": "DME", "S": "RNAV", "B": "LOC BC", "T": "TACAN",
        "U": "SDF", "H": "HUD", "P": "GPS", "Q": "RNAV", "X": "LDA",
    }

    # ── FMS type → XP nav type mapping ──
    FMS_TYPE_TO_NAV = {
        1:  xp.Nav_Airport,
        2:  xp.Nav_NDB,
        3:  xp.Nav_VOR,
        11: xp.Nav_Fix,
        28: xp.Nav_LatLon,
    }

    def __init__(self):
        self.enabled = False
        self.trace   = True
        self.info    = f"{self.NAME} (rel. {self.RELEASE})"

        # ── Plan browser state ──
        self.plans:    List[FlightPlanInfo] = []
        self.index     = -1
        self.loaded    = 0
        self.loaded_filename    = ""
        self.loaded_index       = 0
        self.loaded_sid         = ""
        self.loaded_star        = ""
        self.loaded_distance_nm = 0.0
        self.last_status = "INIT"
        self.last_error  = ""

        # ── Map range mode ──
        self.map_mode       = 0   # 0 = G1000, 1 = GCU478
        self.map_mode_names = ["G1000", "GCU478"]
        self.map_range_cmds = {
            0: ("sim/GPS/g1000n1_range_down", "sim/GPS/g1000n1_range_up"),
            1: ("sim/GPS/gcu478/range_down",  "sim/GPS/gcu478/range_up"),
        }
        self.map_cmd_refs: Dict[int, tuple] = {}

        # ── Avionics auto-detection (probed in XPluginStart) ──
        # fpl_command_ref is used by the "Open FPL" button; avionics_name is
        # surfaced in the UI so the button label reflects the actual aircraft.
        self.fpl_command_ref = None
        self.avionics_name   = "FMS"

        # ── LEGS state ──
        self.legs_selected    = -1
        self.legs_window_start = 0

        # ── Route entry (LOAD tab, typed routes) ──
        self.route_entry_text:   str = ""
        self.route_entry_parsed: list = []   # list[ParsedToken], populated by PARSE
        self.route_entry_status: str = ""

        # ── Simbrief integration ──
        self.simbrief_id:        str = ""
        self._simbrief_fetching: bool = False
        self._simbrief_result:   object = None   # (route_str, error_str) | None
        self._simbrief_error:    str = ""

        # ── Plan browser scroll/sort state ──
        self.browser_list_window_start = 0
        self.plan_sort_key  = 1      # 0 = filename, 1 = timestamp
        self.plan_sort_desc = True   # most recent first

        # ── Registered commands (toggle_window only) ──
        self._cmd_handlers: Dict[str, Dict[str, object]] = {}

        # ── Procedure state ──
        self.proc_dep_icao  = ""
        self.proc_dest_icao = ""
        self._cifp_cache: Dict[str, list] = {}
        self._proc_procs:             Dict[str, list] = {k: [] for k in self.KINDS}
        self._proc_index:             Dict[str, int]  = {k: -1 for k in self.KINDS}
        self._proc_window:            Dict[str, int]  = {k: 0  for k in self.KINDS}
        self._proc_cache_valid:       Dict[str, bool] = {k: False for k in self.KINDS}
        self._proc_rows_cache:        Dict[str, dict] = {k: {}   for k in self.KINDS}
        self._proc_status:            Dict[str, str]  = {k: "INIT" for k in self.KINDS}
        self._proc_splice_point:      Dict[str, int]  = {k: -1 for k in self.KINDS}
        self._proc_loaded:            Dict[str, str]  = {k: ""  for k in self.KINDS}
        self._proc_names:             Dict[str, list] = {k: []  for k in self.KINDS}
        self._proc_name_idx:          Dict[str, int]  = {k: -1 for k in self.KINDS}
        self._proc_name_window:       Dict[str, int]  = {k: 0  for k in self.KINDS}
        self._proc_trans_cache_valid: Dict[str, bool] = {k: False for k in self.KINDS}
        self._proc_trans_rows_cache:  Dict[str, dict] = {k: {}   for k in self.KINDS}

        # ── UI-readable state dicts ──
        self.string_values: Dict[str, str] = {
            "plan_name":        "No flight plans",
            "plan_departure":   "----",
            "plan_destination": "----",
            "plan_cycle":       "",
            "plan_filename":    "",
            "plan_path":        "",
            "plan_dep_runway":  "",
            "plan_dest_runway": "",
            "plan_sid":         "",
            "plan_star":        "",
            "plan_waypoints":   "",
            "loaded_filename":  "",
            "loaded_sid":       "",
            "loaded_star":      "",
            "map_mode":         "",
            "status":           "INIT",
            "last_error":       "",
            "last_dump":        "",
        }
        self.int_values: Dict[str, int] = {
            "index":             0,
            "count":             0,
            "loaded":            0,
            "loaded_index":      0,
            "plan_waypoint_count": 0,
            "plan_max_altitude": 0,
        }
        self.float_values: Dict[str, float] = {
            "plan_distance_nm":   0.0,
            "loaded_distance_nm": 0.0,
        }

        # ── Validation ──
        self.validation_issues: List[ValidationIssue] = []

        # ── Nav monitor ──
        self._nav_monitor_init()

        # ── Wind / METAR — departure ──
        self.dep_wind_metar: str = ""
        self.dep_wind_dir:   Optional[float] = None
        self.dep_wind_spd:   Optional[float] = None
        self.dep_runway_ranking:   list = []
        self.dep_recommended_sids: list = []   # [(display_name, display_runway), …]

        # ── Wind / METAR — arrival ──
        self.wind_metar: str = ""
        self.wind_dir:   Optional[float] = None
        self.wind_spd:   Optional[float] = None
        self.wind_runway_ranking:   list = []
        self.arr_recommended_apps:  list = []  # [(display_name, display_runway), …]
        self.arr_recommended_stars: list = []  # [display_name, …]

        # ── Internal caches ──
        self._perf_enabled     = True
        self._list_rows_cache: Dict[int, Dict[str, object]] = {}
        self._list_cache_valid = False
        self._entry_parse_cache: Dict[tuple, list] = {}

        self._ui_init()

    # ── Logging ──

    def _log(self, *parts):
        if self.trace:
            print(self.info, *parts)

    def _perf_log(self, *parts):
        if not self._perf_enabled:
            return
        line = f"{self.info} [perf] " + " ".join(str(p) for p in parts) + "\n"
        try:
            xp.debugString(line)
        except Exception:
            print(self.info, "[perf]", *parts)

    # ── Validation ──

    def _run_validation(self):
        """Run route validation against the live FMS route and current selection."""
        plan = self._selected_plan()
        if plan is None or not self.loaded:
            self.validation_issues = []
            return
        try:
            entries = self._live_fms_entries()
            issues = validate(entries, plan)

            # ── Live-state checks (need FMS/mixin access, can't live in validator.py) ──

            # No approach selected via APP tab
            if not self._proc_loaded.get("app", ""):
                dest = (plan.dest or "").strip()
                issues.append(ValidationIssue(
                    severity=SEVERITY_WARN,
                    code="NO_APP",
                    message=f"No approach selected for {dest or 'destination'}.",
                    suggestion="Browse approach procedures on the APP tab.",
                ))

            # Active waypoint check
            try:
                count = xp.countFMSEntries()
                if count > 1:
                    active = xp.getDestinationFMSEntry()
                    active_info = self._safe_fms_entry_info(active)
                    active_type = active_info.type if active_info else -1
                    # type 1 = airport; type 512 = fix/navaid; type 2048 = lat/lon
                    if active_type == xp.Nav_Airport and active == 0:
                        issues.append(ValidationIssue(
                            severity=SEVERITY_INFO,
                            code="ACTIVE_AT_DEP",
                            message="Active waypoint is still the departure airport.",
                            suggestion="Advance to the first en-route or SID fix.",
                        ))
                    elif active_type == xp.Nav_Airport and active >= count - 1:
                        issues.append(ValidationIssue(
                            severity=SEVERITY_INFO,
                            code="ACTIVE_AT_DEST",
                            message="Active waypoint is the destination airport.",
                            suggestion="Check whether the correct leg is active.",
                        ))
            except Exception:
                pass

            self.validation_issues = issues
            self._log(
                "Validation:",
                len(issues), "issues",
                {s: sum(1 for v in issues if v.severity == s)
                 for s in ("ERROR", "WARN", "INFO")},
            )
        except Exception as exc:
            self._log("Validation error:", exc)
            self.validation_issues = []

    def _live_fms_entries(self) -> List["FlightPlanEntry"]:
        """Return a best-effort FlightPlanEntry snapshot of the current live FMS route."""
        from fmscompanion.models import FlightPlanEntry

        entries: List[FlightPlanEntry] = []
        try:
            count = xp.countFMSEntries()
        except Exception:
            return entries

        nav_to_fms_type = {
            xp.Nav_Airport: 1,
            xp.Nav_NDB: 2,
            xp.Nav_VOR: 3,
            xp.Nav_Fix: 11,
            xp.Nav_LatLon: 28,
        }

        for i in range(count):
            info = self._safe_fms_entry_info(i)
            if not info:
                continue
            lat = getattr(info, "latitude", None)
            if lat is None:
                lat = getattr(info, "lat", 0.0)
            lon = getattr(info, "longitude", None)
            if lon is None:
                lon = getattr(info, "lon", 0.0)
            ident = (getattr(info, "navAidID", "") or "").strip()
            if not ident and info.type == xp.Nav_LatLon:
                ident = self._legs_format_ident(info)
            entries.append(
                FlightPlanEntry(
                    entry_type=nav_to_fms_type.get(getattr(info, "type", None), 11),
                    ident=ident or "----",
                    altitude=int(getattr(info, "altitude", 0) or 0),
                    lat=float(lat or 0.0),
                    lon=float(lon or 0.0),
                )
            )
        return entries

    def _mark_route_unloaded(self):
        """Reset derived state when there is no longer a loaded route in the FMS."""
        self.loaded = 0
        self.loaded_filename = ""
        self.loaded_index = 0
        self.loaded_sid = ""
        self.loaded_star = ""
        self.loaded_distance_nm = 0.0
        self.validation_issues = []
        self.dep_recommended_sids = []
        self.arr_recommended_apps = []
        self.arr_recommended_stars = []
        self.dep_runway_ranking = []
        self.wind_runway_ranking = []
        self._proc_loaded = {k: "" for k in self.KINDS}
        self._proc_splice_point = {k: -1 for k in self.KINDS}

    def _sync_route_state(self):
        """Refresh loaded state, validation, wind recommendations, and published UI state."""
        count = self._read_fms_entry_count()
        if count <= 0:
            self._mark_route_unloaded()
            self._set_status("EMPTY")
        else:
            self.loaded = 1
            self._run_validation()
            self._wind_refresh_dep()
            self._wind_refresh_arr()
        self._publish_state()

    # ── Wind helpers ──────────────────────────────────────────────────────────

    def _route_idents(self) -> set:
        """Return the set of waypoint idents currently in the FMS plan."""
        try:
            count = xp.countFMSEntries()
            route = set()
            for i in range(count):
                info = self._safe_fms_entry_info(i)
                ident = (getattr(info, "navAidID", "") or "").strip() if info else ""
                if ident:
                    route.add(ident)
            return route
        except Exception:
            return set()

    @staticmethod
    def _rwy_num(rwy_id: str) -> str:
        """Return just the leading runway digits from a runway id.

        Handles standard suffixes plus CIFP variants like "11-Y" or "06B".
        """
        match = _re.match(r'^\s*(\d{1,2})', rwy_id.strip().upper())
        return match.group(1).zfill(2) if match else ""

    def _cmd_wind_refresh(self):
        """Fetch METAR for both DEP and DEST, rank runways, recommend procedures."""
        self._wind_refresh_dep()
        self._wind_refresh_arr()
        self._save_state()

    def _wind_refresh_dep(self):
        icao = (self.proc_dep_icao or "").strip().upper()
        metar = fetch_metar(icao) if icao else ""
        self.dep_wind_metar = metar
        dir_, spd = parse_wind(metar)
        self.dep_wind_dir = dir_
        self.dep_wind_spd = spd
        self._log(f"wind_refresh DEP {icao}: metar={bool(metar)} dir={dir_} spd={spd}")

        # Mirror _wind_refresh_arr: if the procedure cache is empty for this
        # airport, refresh before ranking — otherwise SIDs are invisible and
        # we silently produce no advisory.
        if icao and not self._proc_procs.get("dep"):
            self._proc_refresh()

        # DEP procedures share a physical runway (e.g. "06B"/"06C" → both runway 06)
        # Deduplicate to unique numeric runway IDs before ranking.
        seen: set = set()
        rwy_ids = []
        for proc in self._proc_procs.get("dep", []):
            num = self._rwy_num(proc.display_runway or "")
            if num and num not in seen:
                seen.add(num)
                rwy_ids.append(num)

        if rwy_ids and dir_ is not None and spd is not None:
            self.dep_runway_ranking = rank_runways(rwy_ids, dir_, spd)
        else:
            self.dep_runway_ranking = []

        # Pick the best DEP runway: wind ranking → plan hint → first runway
        # with SIDs. Fallback mirrors _best_arrival_runway so we still give
        # advice when METAR is unavailable or wind is VRB/calm.
        best_num = ""
        if self.dep_runway_ranking:
            best_num = self._rwy_num(self.dep_runway_ranking[0][0])
        else:
            plan = self._selected_plan()
            plan_rwy = (getattr(plan, "dep_runway", "") or "").strip().upper()
            if plan_rwy:
                best_num = self._rwy_num(plan_rwy)
            elif rwy_ids:
                best_num = rwy_ids[0]

        # SIDs for the best departure runway, filtered by route connectivity.
        # A SID is a good connector if its last waypoint appears in the FMS route.
        if best_num:
            route = self._route_idents()
            runway_sids = [
                proc for proc in self._proc_procs.get("dep", [])
                if self._rwy_num(proc.display_runway or "") == best_num
            ]
            connected = [
                (p.display_name, p.display_runway) for p in runway_sids
                if p.waypoints and p.waypoints[-1] in route
            ]
            # Fall back to all runway-matched SIDs if none connect to the route
            self.dep_recommended_sids = connected if connected else [
                (p.display_name, p.display_runway) for p in runway_sids
            ]
        else:
            self.dep_recommended_sids = []

    def _best_arrival_runway(self) -> str:
        """Choose the best arrival runway from wind, plan hints, or procedure connectivity."""
        if self.wind_runway_ranking:
            return self.wind_runway_ranking[0][0]

        plan = self._selected_plan()
        plan_rwy = (getattr(plan, "dest_runway", "") or "").strip().upper()
        if plan_rwy:
            return plan_rwy

        route = self._route_idents()
        candidates: Dict[str, dict] = {}

        for proc in self._proc_procs.get("app", []):
            rwy = (proc.display_runway or "").strip().upper()
            if not rwy:
                continue
            bucket = candidates.setdefault(rwy, {"app_connected": 0, "star_connected": 0, "apps": 0, "stars": 0})
            bucket["apps"] += 1
            if (proc.transition and proc.transition in route) or (proc.waypoints and proc.waypoints[0] in route):
                bucket["app_connected"] += 1

        for proc in self._proc_procs.get("arr", []):
            name = (proc.display_name or "").upper()
            for rwy, bucket in candidates.items():
                if f"RW{self._rwy_num(rwy)}" not in name:
                    continue
                bucket["stars"] += 1
                if (proc.transition and proc.transition in route) or (proc.waypoints and proc.waypoints[0] in route):
                    bucket["star_connected"] += 1

        if candidates:
            ranked = sorted(
                candidates.items(),
                key=lambda item: (
                    item[1]["app_connected"],
                    item[1]["star_connected"],
                    item[1]["apps"],
                    item[1]["stars"],
                    item[0],
                ),
                reverse=True,
            )
            return ranked[0][0]

        app_runways = sorted({
            (proc.display_runway or "").strip().upper()
            for proc in self._proc_procs.get("app", [])
            if (proc.display_runway or "").strip()
        })
        return app_runways[0] if app_runways else ""

    def _wind_refresh_arr(self):
        icao = (self.proc_dest_icao or "").strip().upper()
        metar = fetch_metar(icao) if icao else ""
        self.wind_metar = metar
        dir_, spd = parse_wind(metar)
        self.wind_dir = dir_
        self.wind_spd = spd
        self._log(f"wind_refresh ARR {icao}: metar={bool(metar)} dir={dir_} spd={spd}")

        # If the procedure cache is stale or was never populated for this airport,
        # refresh before attempting runway/app recommendations.
        if icao and not self._proc_procs.get("app") and not self._proc_procs.get("arr"):
            self._proc_refresh()

        # APP procedures have full runway IDs ("28L", "28R") — keep them distinct.
        seen: set = set()
        rwy_ids = []
        for proc in self._proc_procs.get("app", []):
            rwy = (proc.display_runway or "").strip()
            if rwy and rwy not in seen:
                seen.add(rwy)
                rwy_ids.append(rwy)

        if rwy_ids and dir_ is not None and spd is not None:
            self.wind_runway_ranking = rank_runways(rwy_ids, dir_, spd)
        else:
            self.wind_runway_ranking = []

        # APPs for the best arrival runway (exact match, then numeric fallback).
        # When wind does not yield a runway (VRB/calm/missing), fall back to the
        # plan's destination runway or procedure connectivity.
        best_rwy = self._best_arrival_runway()
        best_num = self._rwy_num(best_rwy) if best_rwy else ""
        if best_rwy:
            self.arr_recommended_apps = [
                (proc.display_name, proc.display_runway)
                for proc in self._proc_procs.get("app", [])
                if (proc.display_runway or "").strip().upper() == best_rwy
                or (
                    best_num
                    and not best_rwy[-1:].isalpha()
                    and self._rwy_num(proc.display_runway or "") == best_num
                )
            ]
        else:
            self.arr_recommended_apps = []

        # STARs — only recommend those serving the best arrival runway,
        # further filtered by route connectivity (entry fix must be in FMS route).
        seen_names: set = set()
        stars_rwy: list = []
        best_rwy_num = self._rwy_num(best_rwy)
        for proc in self._proc_procs.get("arr", []):
            if proc.name in seen_names:
                continue
            seen_names.add(proc.name)
            if best_rwy_num and f"RW{best_rwy_num}" in proc.display_name.upper():
                stars_rwy.append(proc)
        route = self._route_idents()
        # A STAR connects if its transition fix or first waypoint is in the route
        connected = [
            p.display_name for p in stars_rwy
            if (p.transition and p.transition in route)
            or (p.waypoints and p.waypoints[0] in route)
        ]
        self.arr_recommended_stars = connected if connected else [p.display_name for p in stars_rwy]

    def _cmd_load(self):
        """Load selected plan into FMS, run validation, and refresh wind ranking."""
        super()._cmd_load()
        self._sync_route_state()
        self._save_state()

    def _cmd_proc_activate(self, kind: str) -> None:
        super()._cmd_proc_activate(kind)
        self._sync_route_state()
        self._save_state()

    def _cmd_load_recommended(self):
        """Load plan then apply the top recommended SID, STAR, and APP in one click."""
        self._cmd_load()
        applied = []
        sid = self.dep_recommended_sids
        if sid:
            dn, _ = sid[0]
            if self._cmd_apply_recommended("dep", dn):
                applied.append(f"SID:{dn}")
        star = self.arr_recommended_stars
        if star:
            if self._cmd_apply_recommended("arr", star[0]):
                applied.append(f"STAR:{star[0]}")
        app = self.arr_recommended_apps
        if app:
            dn, _ = app[0]
            if self._cmd_apply_recommended("app", dn):
                applied.append(f"APP:{dn}")
        self._log("load_recommended applied:", ", ".join(applied) if applied else "none")

    # ── State persistence ─────────────────────────────────────────────────────

    def _state_path(self) -> str:
        return os.path.join(xp.getSystemPath(), "Output", "FMSCompanion", "state.json")

    def _save_state(self):
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            plan = self._selected_plan()
            data = {
                "version": 1,
                "plan_filename": plan.filename if plan else "",
                "simbrief_id":   self.simbrief_id,
                "proc_dep_icao": self.proc_dep_icao,
                "proc_dest_icao": self.proc_dest_icao,
                "proc_loaded":   dict(self._proc_loaded),
                "proc_name_idx": dict(self._proc_name_idx),
                "proc_index":    dict(self._proc_index),
                "wind": {
                    "dep_metar":            self.dep_wind_metar,
                    "dep_wind_dir":         self.dep_wind_dir,
                    "dep_wind_spd":         self.dep_wind_spd,
                    "dep_runway_ranking":   self.dep_runway_ranking,
                    "dep_recommended_sids": self.dep_recommended_sids,
                    "arr_metar":            self.wind_metar,
                    "arr_wind_dir":         self.wind_dir,
                    "arr_wind_spd":         self.wind_spd,
                    "arr_runway_ranking":   self.wind_runway_ranking,
                    "arr_recommended_apps": self.arr_recommended_apps,
                    "arr_recommended_stars":self.arr_recommended_stars,
                },
            }
            def _safe(obj):
                if isinstance(obj, float) and (obj != obj or obj in (float('inf'), float('-inf'))):
                    return None
                return str(obj)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=_safe)
        except Exception as exc:
            self._log("State save error:", exc)

    def _restore_state(self):
        path = self._state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != 1:
                return

            # Restore plan selection
            filename = data.get("plan_filename", "")
            if filename:
                for i, plan in enumerate(self.plans):
                    if plan.filename == filename:
                        self.index = i
                        break

            self.simbrief_id = data.get("simbrief_id", "")

            # Restore airports and reload CIFP
            dep  = data.get("proc_dep_icao",  "")
            dest = data.get("proc_dest_icao", "")
            if dep:
                self.proc_dep_icao = dep
            if dest:
                self.proc_dest_icao = dest
            if dep or dest:
                self._proc_refresh()

            # Restore procedure selection state
            for kind in self.KINDS:
                loaded = data.get("proc_loaded", {}).get(kind, "")
                name_idx = data.get("proc_name_idx", {}).get(kind, -1)
                idx = data.get("proc_index", {}).get(kind, -1)
                if loaded:
                    self._proc_loaded[kind] = loaded
                if isinstance(name_idx, int):
                    names = self._proc_names.get(kind, [])
                    if 0 <= name_idx < len(names):
                        self._proc_name_idx[kind] = name_idx
                if isinstance(idx, int):
                    self._proc_index[kind] = idx

            # Restore wind data
            w = data.get("wind", {})
            self.dep_wind_metar        = w.get("dep_metar", "")
            self.dep_wind_dir          = w.get("dep_wind_dir")
            self.dep_wind_spd          = w.get("dep_wind_spd")
            self.dep_runway_ranking    = w.get("dep_runway_ranking", [])
            self.dep_recommended_sids  = w.get("dep_recommended_sids", [])
            self.wind_metar            = w.get("arr_metar", "")
            self.wind_dir              = w.get("arr_wind_dir")
            self.wind_spd              = w.get("arr_wind_spd")
            self.wind_runway_ranking   = w.get("arr_runway_ranking", [])
            self.arr_recommended_apps  = w.get("arr_recommended_apps", [])
            self.arr_recommended_stars = w.get("arr_recommended_stars", [])

            # Re-run validation against restored FMS state
            self._run_validation()
            self._log("State restored from", path)
        except Exception as exc:
            self._log("State restore error:", exc)

    # ── State dump ────────────────────────────────────────────────────────────

    def _cmd_dump_state(self) -> str:
        """Write a JSON snapshot of all plugin state to Output/FMSCompanion/.

        Returns the file path on success, empty string on failure.
        """
        try:
            out_dir = os.path.join(xp.getSystemPath(), "Output", "FMSCompanion")
            os.makedirs(out_dir, exist_ok=True)
            ts   = datetime.now()
            path = os.path.join(out_dir, f"dump_{ts.strftime('%Y%m%d_%H%M%S')}.json")

            data = {
                "timestamp":      ts.isoformat(timespec="seconds"),
                "plugin_version": self.RELEASE,
                "plan":           self._dump_plan(),
                "fms_entries":    self._dump_fms_entries(),
                "nav":            self._dump_nav(),
                "fuel": {
                    "on_board_kg": round(self.fuel_on_board_kg, 2),
                    "flow_kg_s":   round(self.fuel_flow_kg_s,   4),
                    "flow_kg_hr":  round(self.fuel_flow_kg_s * 3600, 1),
                },
                "wind": {
                    "dep_icao":     self.proc_dep_icao  or "",
                    "dep_metar":    self.dep_wind_metar  or "",
                    "dep_wind_dir": self.dep_wind_dir,
                    "dep_wind_spd": self.dep_wind_spd,
                    "dep_ranking":  self.dep_runway_ranking,
                    "dep_sids":     self.dep_recommended_sids,
                    "arr_icao":     self.proc_dest_icao  or "",
                    "arr_metar":    self.wind_metar       or "",
                    "arr_wind_dir": self.wind_dir,
                    "arr_wind_spd": self.wind_spd,
                    "arr_ranking":  self.wind_runway_ranking,
                    "arr_apps":     self.arr_recommended_apps,
                    "arr_stars":    self.arr_recommended_stars,
                },
                "validation_issues": [
                    {"severity": v.severity, "code": v.code,
                     "message": v.message, "affected_index": v.affected_index,
                     "suggestion": v.suggestion}
                    for v in self.validation_issues
                ],
                "procedures_loaded": {k: self._proc_loaded.get(k, "") for k in self.KINDS},
                "nav_advisories":    list(self.nav_advisories),
            }

            def _json_safe(obj):
                """Coerce non-JSON-serialisable values: inf/nan → None."""
                if isinstance(obj, float) and (obj != obj or obj == float('inf') or obj == float('-inf')):
                    return None
                return str(obj)

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=_json_safe)

            self._log("Dump written:", path)
            self.string_values["last_dump"] = os.path.basename(path)
            return path

        except Exception as exc:
            self._log("Dump error:", exc)
            return ""

    def _dump_plan(self) -> dict:
        plan = self._selected_plan()
        if not plan:
            return {}
        return {
            "filename":    plan.filename,
            "dep":         plan.dep,
            "dest":        plan.dest,
            "sid":         plan.sid,
            "star":        plan.star,
            "waypoints":   plan.waypoint_count,
            "distance_nm": round(plan.total_distance_nm, 1),
            "max_alt_ft":  plan.max_altitude,
        }

    def _dump_fms_entries(self) -> list:
        entries = []
        try:
            count  = xp.countFMSEntries()
            active = xp.getDestinationFMSEntry()
            for i in range(count):
                info = self._safe_fms_entry_info(i)
                if not info:
                    continue
                lat = getattr(info, "latitude",  None) or getattr(info, "lat",  None) or 0.0
                lon = getattr(info, "longitude", None) or getattr(info, "lon",  None) or 0.0
                entries.append({
                    "index":    i,
                    "ident":    info.navAidID or "",
                    "type":     info.type,
                    "altitude": info.altitude,
                    "lat":      round(lat, 6),
                    "lon":      round(lon, 6),
                    "active":   i == active,
                })
        except Exception as exc:
            entries.append({"error": str(exc)})
        return entries

    def _dump_nav(self) -> dict:
        def _getf(key):
            ref = self._nav_drefs.get(key) if self._nav_drefs else None
            if not ref:
                return None
            try:
                return round(xp.getDataf(ref), 3)
            except Exception:
                return None

        return {
            "active_ident": self._read_fms_active_ident(),
            "active_index": self._read_fms_active_index(),
            "entry_count":  self._read_fms_entry_count(),
            "xtk_dots":     _getf("xtk"),
            "gs_kt":        _getf("gs"),
            "dtk_deg":      _getf("dtk"),
            "trk_deg":      _getf("trk"),
            "dis_nm":       _getf("dis"),
            "ete_min":      _getf("ete"),
            "brg_deg":      _getf("brg"),
            "eta_h":        _getf("eta_h"),
            "eta_m":        _getf("eta_m"),
        }

    # ── Command registration (used by UIMixin._ui_register_command only) ──

    def _create_command(self, suffix: str, desc: str, callback, prefix: str = None):
        """Register a single X-Plane command. Stored for cleanup on XPluginStop."""
        name = f"{prefix or self.CMD_PREFIX}/{suffix}"
        cmd_ref = xp.createCommand(name, desc)
        if not cmd_ref:
            self._log("ERROR: command creation failed for", name)
            return

        def handler(commandRef, phase, refcon):
            if phase == xp.CommandBegin:
                callback()
            return 1

        xp.registerCommandHandler(cmd_ref, handler, 1, None)
        self._cmd_handlers[name] = {"ref": cmd_ref, "handler": handler}
        self._log("Registered command", name)

    # ── Avionics detection ──

    # Probe order matters — first match wins. G1000 is most common in stock
    # aircraft; GNS 530/430 covers older Cessna/Piper stock planes. Add more
    # as we learn what specific aircraft expose.
    _FPL_CANDIDATES = [
        ("G1000",   "sim/GPS/g1000n1_fpl"),
        ("G1000",   "sim/GPS/g1000n2_fpl"),
        ("G1000",   "sim/GPS/g1000n3_fpl"),
        ("GNS 530", "sim/GPS/g530n1_fpl"),
        ("GNS 530", "sim/GPS/g530n2_fpl"),
        ("GNS 430", "sim/GPS/g430n1_fpl"),
        ("GNS 430", "sim/GPS/g430n2_fpl"),
    ]

    def _detect_avionics(self):
        for name, cmd in self._FPL_CANDIDATES:
            ref = xp.findCommand(cmd)
            if ref:
                self.fpl_command_ref = ref
                self.avionics_name   = name
                self._log(f"Detected avionics: {name} via {cmd}")
                return
        self._log("No known FPL command found — Open FPL button will no-op.")

    # ── XPlugin lifecycle ──

    def XPluginStart(self):
        self._log("XPluginStart", f"RELEASE={self.RELEASE}")

        # Resolve map range command refs
        for mode, (down_cmd, up_cmd) in self.map_range_cmds.items():
            self.map_cmd_refs[mode] = (xp.findCommand(down_cmd), xp.findCommand(up_cmd))
            self._log("map range cmds", mode, "->", self.map_cmd_refs[mode])

        # Probe for avionics (picks the FPL command we can drive on this aircraft)
        self._detect_avionics()

        # Register the toggle-window command and build the Plugins menu entry
        self._ui_register_command()
        self._ui_build_menu()

        # Populate the plan list immediately
        self._refresh_plan_list()

        return self.NAME, self.SIG, self.DESC

    def XPluginStop(self):
        self._ui_destroy_menu()
        self._ui_destroy_window()

        for name, meta in self._cmd_handlers.items():
            try:
                xp.unregisterCommandHandler(meta["ref"], meta["handler"], 1, None)
                self._log("Unregistered command", name)
            except Exception as exc:
                self._log("Failed to unregister command", name, exc)
        self._cmd_handlers = {}

        self._log("XPluginStop")
        return None

    def XPluginEnable(self):
        self.enabled = True
        self._log("XPluginEnable")
        self._proc_airports_from_fms()
        self._restore_state()
        self._nav_monitor_start()
        self._ui_create_window()
        return 1

    def XPluginDisable(self):
        self.enabled = False
        self._save_state()
        self._nav_monitor_stop()
        self._ui_destroy_window()
        self._log("XPluginDisable")
        return None

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        if inMessage == xp.MSG_PLANE_LOADED and inParam == 0:
            self._log("User aircraft loaded — refreshing plan list")
            self._refresh_plan_list()
            self._publish_state()
        return None

    # ── Core state helpers ──

    def _set_status(self, text: str, error: str = ""):
        self.last_status = text
        self.last_error  = error
        self.string_values["status"]     = text
        self.string_values["last_error"] = error

    def _selected_plan(self) -> FlightPlanInfo | None:
        if not self.plans:
            return None
        if self.index < 0 or self.index >= len(self.plans):
            return None
        return self.plans[self.index]

    def _publish_state(self):
        """Sync self.plans/index/loaded to the string/int/float dicts that the UI reads."""
        t0   = time.perf_counter()
        plan = self._selected_plan()

        self.int_values["count"]  = len(self.plans)
        self.int_values["index"]  = (self.index + 1) if (self.plans and 0 <= self.index < len(self.plans)) else 0
        self.int_values["loaded"] = int(self.loaded)

        if plan is None:
            self.string_values["plan_name"]        = "No flight plans" if not self.plans else "Select plan"
            self.string_values["plan_departure"]   = "----"
            self.string_values["plan_destination"] = "----"
            self.string_values["plan_cycle"]       = ""
            self.string_values["plan_filename"]    = ""
            self.string_values["plan_path"]        = ""
            self.string_values["plan_dep_runway"]  = ""
            self.string_values["plan_dest_runway"] = ""
            self.string_values["plan_sid"]         = ""
            self.string_values["plan_star"]        = ""
            self.string_values["plan_waypoints"]   = ""
            self.int_values["plan_waypoint_count"] = 0
            self.int_values["plan_max_altitude"]   = 0
            self.float_values["plan_distance_nm"]  = 0.0
        else:
            self.string_values["plan_name"]        = plan.display_name
            self.string_values["plan_departure"]   = plan.dep
            self.string_values["plan_destination"] = plan.dest
            self.string_values["plan_cycle"]       = plan.cycle
            self.string_values["plan_filename"]    = os.path.splitext(plan.filename)[0]
            self.string_values["plan_path"]        = plan.full_path
            self.string_values["plan_dep_runway"]  = plan.dep_runway
            self.string_values["plan_dest_runway"] = plan.dest_runway
            self.string_values["plan_sid"]         = plan.sid
            self.string_values["plan_star"]        = plan.star
            self.string_values["plan_waypoints"]   = plan.waypoint_list
            self.int_values["plan_waypoint_count"] = plan.waypoint_count
            self.int_values["plan_max_altitude"]   = plan.max_altitude
            self.float_values["plan_distance_nm"]  = plan.total_distance_nm

        self.string_values["loaded_filename"]      = self.loaded_filename
        self.int_values["loaded_index"]            = self.loaded_index
        self.string_values["loaded_sid"]           = self.loaded_sid
        self.string_values["loaded_star"]          = self.loaded_star
        self.float_values["loaded_distance_nm"]    = self.loaded_distance_nm
        self.string_values["map_mode"]             = self.map_mode_names[self.map_mode]
        self.string_values["status"]               = self.last_status
        self.string_values["last_error"]           = self.last_error

        self._log(
            "State",
            f"index={self.int_values['index']}",
            f"count={self.int_values['count']}",
            f"plan={self.string_values['plan_filename']}",
            f"status={self.string_values['status']}",
        )
        self._perf_log(f"publish_state_ms={(time.perf_counter() - t0) * 1000.0:.2f}")
