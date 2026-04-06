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

import os
import sys
import time
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

        # ── Wind / METAR ──
        self.wind_metar: str = ""
        self.wind_dir:   Optional[float] = None
        self.wind_spd:   Optional[float] = None
        self.wind_runway_ranking: list = []

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
                    if active == 0:
                        issues.append(ValidationIssue(
                            severity=SEVERITY_INFO,
                            code="ACTIVE_AT_DEP",
                            message="Active waypoint is still the departure airport.",
                            suggestion="Advance to the first en-route or SID fix.",
                        ))
                    elif active >= count - 1:
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

    def _cmd_wind_refresh(self):
        """Fetch METAR for dest airport, parse wind, rank runways from APP procedures."""
        icao = (self.proc_dest_icao or "").strip().upper()
        if not icao:
            self._log("wind_refresh: no dest ICAO")
            self.wind_metar = ""
            self.wind_dir   = None
            self.wind_spd   = None
            self.wind_runway_ranking = []
            return

        metar = fetch_metar(icao)
        self.wind_metar = metar
        wind_dir, wind_spd = parse_wind(metar)
        self.wind_dir = wind_dir
        self.wind_spd = wind_spd
        self._log(f"wind_refresh: {icao} metar={bool(metar)} dir={wind_dir} spd={wind_spd}")

        # Runway IDs from already-parsed APP procedures
        seen: set = set()
        rwy_ids = []
        for proc in self._proc_procs.get("app", []):
            rwy = (proc.display_runway or "").strip()
            if rwy and rwy not in seen:
                seen.add(rwy)
                rwy_ids.append(rwy)

        if rwy_ids and wind_dir is not None and wind_spd is not None:
            self.wind_runway_ranking = rank_runways(rwy_ids, wind_dir, wind_spd)
        else:
            self.wind_runway_ranking = []

    def _cmd_load(self):
        """Load selected plan into FMS, run validation, and refresh wind ranking."""
        super()._cmd_load()
        self._run_validation()
        self._cmd_wind_refresh()

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
