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
    RELEASE = "0.1.0"

    # Command prefix used by _create_command when no explicit prefix is given
    CMD_PREFIX = "fmscompanion"

    # ── Layout constants ──
    PLAN_LIST_VISIBLE_ROWS = 3
    PLAN_LIST_MAX_PLANS    = 9
    LEGS_VISIBLE_ROWS      = 3
    PROC_VISIBLE_ROWS      = 3

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

        # ── LEGS state ──
        self.legs_selected    = -1
        self.legs_window_start = 0

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
        """Run route validation against the currently loaded plan and store results."""
        plan = self._selected_plan()
        if plan is None or not self.loaded:
            self.validation_issues = []
            return
        try:
            entries = self._get_cached_entries(plan)
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

    # ── Wind helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _rwy_num(rwy_id: str) -> str:
        """Strip all trailing letters from a runway id, returning just the digits.
        Works for L/R/C suffixes and CIFP variant letters (B, D, G, …).
        E.g. "06B" → "06", "28L" → "28", "27" → "27".
        """
        return _re.sub(r'[A-Za-z]+$', '', rwy_id.strip())

    def _cmd_wind_refresh(self):
        """Fetch METAR for both DEP and DEST, rank runways, recommend procedures."""
        self._wind_refresh_dep()
        self._wind_refresh_arr()

    def _wind_refresh_dep(self):
        icao = (self.proc_dep_icao or "").strip().upper()
        metar = fetch_metar(icao) if icao else ""
        self.dep_wind_metar = metar
        dir_, spd = parse_wind(metar)
        self.dep_wind_dir = dir_
        self.dep_wind_spd = spd
        self._log(f"wind_refresh DEP {icao}: metar={bool(metar)} dir={dir_} spd={spd}")

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

        # SIDs for the best departure runway
        if self.dep_runway_ranking:
            best_num = self._rwy_num(self.dep_runway_ranking[0][0])
            self.dep_recommended_sids = [
                (proc.display_name, proc.display_runway)
                for proc in self._proc_procs.get("dep", [])
                if self._rwy_num(proc.display_runway or "") == best_num
            ]
        else:
            self.dep_recommended_sids = []

    def _wind_refresh_arr(self):
        icao = (self.proc_dest_icao or "").strip().upper()
        metar = fetch_metar(icao) if icao else ""
        self.wind_metar = metar
        dir_, spd = parse_wind(metar)
        self.wind_dir = dir_
        self.wind_spd = spd
        self._log(f"wind_refresh ARR {icao}: metar={bool(metar)} dir={dir_} spd={spd}")

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

        # APPs for the best arrival runway (exact match, then numeric fallback)
        if self.wind_runway_ranking:
            best_rwy = self.wind_runway_ranking[0][0]       # e.g. "28L"
            best_num = self._rwy_num(best_rwy)              # e.g. "28"
            self.arr_recommended_apps = [
                (proc.display_name, proc.display_runway)
                for proc in self._proc_procs.get("app", [])
                if proc.display_runway == best_rwy
                or (not best_rwy[-1:].isalpha() and
                    self._rwy_num(proc.display_runway or "") == best_num)
            ]
        else:
            self.arr_recommended_apps = []

        # STARs — prefer those serving the best arrival runway; fall back to all.
        # CIFP display_name encodes the served runway as "RW##" e.g. "BOSN1P RW29".
        seen_names: set = set()
        stars_best: list = []
        stars_all:  list = []
        best_rwy_num = self._rwy_num(self.wind_runway_ranking[0][0]) if self.wind_runway_ranking else ""
        for proc in self._proc_procs.get("arr", []):
            if proc.name in seen_names:
                continue
            seen_names.add(proc.name)
            stars_all.append(proc.display_name)
            if best_rwy_num and f"RW{best_rwy_num}" in proc.display_name.upper():
                stars_best.append(proc.display_name)
        self.arr_recommended_stars = stars_best if stars_best else stars_all

    def _cmd_load(self):
        """Load selected plan into FMS, run validation, and refresh wind ranking."""
        super()._cmd_load()
        self._run_validation()
        self._cmd_wind_refresh()

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

    # ── XPlugin lifecycle ──

    def XPluginStart(self):
        self._log("XPluginStart", f"RELEASE={self.RELEASE}")

        # Resolve map range command refs
        for mode, (down_cmd, up_cmd) in self.map_range_cmds.items():
            self.map_cmd_refs[mode] = (xp.findCommand(down_cmd), xp.findCommand(up_cmd))
            self._log("map range cmds", mode, "->", self.map_cmd_refs[mode])

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
        self._nav_monitor_start()
        self._ui_create_window()
        return 1

    def XPluginDisable(self):
        self.enabled = False
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
