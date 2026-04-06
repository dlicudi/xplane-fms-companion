"""
PI_CockpitdecksFMS — XPPython3 plugin entry point.

Cockpitdecks FMS plugin: browse and load Output/FMS plans, scrollable LEGS list with
waypoint selection/activation/direct-to, live FMS state datarefs, procedure (SID/STAR/APP)
browser, and map range control.

Package layout (cockpitdecksfms/):
  models.py       — FlightPlanInfo, FlightPlanEntry, ProcedureInfo dataclasses
  drefs.py        — DrefsMixin: dataref and command registration helpers
  fms_state.py    — FmsStateMixin: live FMS state reads, waypoint/map commands
  fms_io.py       — FmsIOMixin: FMS file parsing, loading, plan navigation commands
  plan_browser.py — PlanBrowserMixin: plan list display, sorting, row selection
  legs.py         — LegsMixin: scrollable LEGS list
  procedures.py   — ProceduresMixin: CIFP parsing, SID/STAR/APP browser
"""

import os
import sys
import time

# Make the cockpitdecksfms package importable when this file is deployed as a flat
# copy into PythonPlugins/ alongside the cockpitdecksfms/ package folder.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from typing import Dict, List

from XPPython3 import xp

from cockpitdecksfms.drefs import DrefsMixin
from cockpitdecksfms.fms_io import FmsIOMixin
from cockpitdecksfms.fms_state import FmsStateMixin
from cockpitdecksfms.legs import LegsMixin
from cockpitdecksfms.models import FlightPlanEntry, FlightPlanInfo, ProcedureInfo
from cockpitdecksfms.plan_browser import PlanBrowserMixin
from cockpitdecksfms.procedures import ProceduresMixin


class PythonInterface(DrefsMixin, FmsStateMixin, FmsIOMixin, PlanBrowserMixin, LegsMixin, ProceduresMixin):
    NAME = "Cockpitdecks FMS"
    SIG = "xppython3.cockpitdecksfms"
    DESC = (
        "Cockpitdecks FMS plugin: browse and load Output/FMS plans (cockpitdecks/fms/load), "
        "scrollable LEGS list with waypoint selection/activation/direct-to (cockpitdecks/fms/legs), "
        "live FMS state datarefs, and map range control."
    )
    RELEASE = "2.0.22"

    DREF_PREFIX = "cockpitdecks/fms/load"
    CMD_PREFIX = "cockpitdecks/fms/load"

    LEGS_DREF_PREFIX = "cockpitdecks/fms/legs"
    LEGS_CMD_PREFIX = "cockpitdecks/fms/legs"
    LEGS_VISIBLE_ROWS = 3

    DEP_DREF_PREFIX = "cockpitdecks/fms/dep"
    DEP_CMD_PREFIX = "cockpitdecks/fms/dep"
    ARR_DREF_PREFIX = "cockpitdecks/fms/arr"
    ARR_CMD_PREFIX = "cockpitdecks/fms/arr"
    APP_DREF_PREFIX = "cockpitdecks/fms/app"
    APP_CMD_PREFIX = "cockpitdecks/fms/app"
    PROC_VISIBLE_ROWS = 3
    KINDS = ("dep", "arr", "app")
    _KIND_CIFP_TYPE = {"dep": "SID", "arr": "STAR", "app": "APP"}

    _APP_TYPE_LABELS = {
        "I": "ILS", "R": "RNAV", "V": "VOR", "N": "NDB", "L": "LOC",
        "D": "DME", "S": "RNAV", "B": "LOC BC", "T": "TACAN",
        "U": "SDF", "H": "HUD", "P": "GPS", "Q": "RNAV", "X": "LDA",
    }

    PLAN_LIST_VISIBLE_ROWS = 3
    PLAN_LIST_MAX_PLANS = 9

    ACTION_NONE = 0
    ACTION_PREVIOUS = 1
    ACTION_NEXT = 2
    ACTION_REFRESH = 3
    ACTION_LOAD = 4
    ACTION_OPEN_FPL = 5

    FMS_TYPE_TO_NAV = {
        1: xp.Nav_Airport,
        2: xp.Nav_NDB,
        3: xp.Nav_VOR,
        11: xp.Nav_Fix,
        28: xp.Nav_LatLon,
    }

    def __init__(self):
        self.enabled = False
        self.trace = True
        self.info = f"{self.NAME} (rel. {self.RELEASE})"

        self.plans: List[FlightPlanInfo] = []
        self.index = -1
        self.loaded = 0
        self.loaded_filename = ""
        self.loaded_index = 0
        self.loaded_sid = ""
        self.loaded_star = ""
        self.loaded_distance_nm = 0.0
        self.last_status = "INIT"
        self.last_error = ""

        self.map_mode = 0  # 0 = G1000, 1 = GCU478
        self.map_mode_names = ["G1000", "GCU478"]
        self.map_range_cmds = {
            0: ("sim/GPS/g1000n1_range_down", "sim/GPS/g1000n1_range_up"),
            1: ("sim/GPS/gcu478/range_down", "sim/GPS/gcu478/range_up"),
        }
        self.map_cmd_refs = {}

        self.legs_selected = -1
        self.legs_window_start = 0

        self.browser_list_window_start = 0
        self.plan_sort_key = 0
        self.plan_sort_desc = False

        self.accessors = []
        self.commands: Dict[str, Dict[str, object]] = {}

        self.proc_dep_icao = ""
        self.proc_dest_icao = ""
        self._cifp_cache: Dict[str, List[ProcedureInfo]] = {}
        self._proc_procs: Dict[str, List[ProcedureInfo]] = {k: [] for k in self.KINDS}
        self._proc_index: Dict[str, int] = {k: -1 for k in self.KINDS}
        self._proc_window: Dict[str, int] = {k: 0 for k in self.KINDS}
        self._proc_cache_valid: Dict[str, bool] = {k: False for k in self.KINDS}
        self._proc_rows_cache: Dict[str, Dict[int, Dict[str, object]]] = {k: {} for k in self.KINDS}
        self._proc_status: Dict[str, str] = {k: "INIT" for k in self.KINDS}
        self._proc_splice_point: Dict[str, int] = {k: -1 for k in self.KINDS}
        self._proc_loaded: Dict[str, str] = {k: "" for k in self.KINDS}

        self.string_values: Dict[str, str] = {
            "plan_name": "No flight plans",
            "plan_departure": "----",
            "plan_destination": "----",
            "plan_cycle": "",
            "plan_filename": "",
            "plan_path": "",
            "plan_dep_runway": "",
            "plan_dest_runway": "",
            "plan_sid": "",
            "plan_star": "",
            "plan_waypoints": "",
            "loaded_filename": "",
            "loaded_sid": "",
            "loaded_star": "",
            "map_mode": "",
            "status": "INIT",
            "last_error": "",
        }
        self.int_values: Dict[str, int] = {
            "index": 0,
            "count": 0,
            "loaded": 0,
            "loaded_index": 0,
            "action": 0,
            "last_action": 0,
            "action_ack": 0,
            "plan_waypoint_count": 0,
            "plan_max_altitude": 0,
        }
        self.float_values: Dict[str, float] = {
            "plan_distance_nm": 0.0,
            "loaded_distance_nm": 0.0,
        }

        self._perf_enabled = True
        self._list_rows_cache: Dict[int, Dict[str, object]] = {}
        self._list_cache_valid = False
        self._entry_parse_cache: Dict[tuple, list] = {}

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

    # ── XPlugin lifecycle ──

    def XPluginStart(self):
        self._log("XPluginStart", f"LEGS_VISIBLE_ROWS={self.LEGS_VISIBLE_ROWS}")

        self._register_string_dref("plan_name")
        self._register_string_dref("plan_departure")
        self._register_string_dref("plan_destination")
        self._register_string_dref("plan_cycle")
        self._register_string_dref("plan_filename")
        self._register_string_dref("plan_path")
        self._register_string_dref("plan_dep_runway")
        self._register_string_dref("plan_dest_runway")
        self._register_string_dref("plan_sid")
        self._register_string_dref("plan_star")
        self._register_string_dref("plan_waypoints")
        self._register_string_dref("status")
        self._register_string_dref("last_error")
        self._register_string_dref("loaded_filename")
        self._register_string_dref("loaded_sid")
        self._register_string_dref("loaded_star")
        self._register_string_dref("map_mode")

        self._register_int_dref("index")
        self._register_int_dref("count")
        self._register_int_dref("loaded")
        self._register_int_dref("loaded_index")
        self._register_writable_action_dref("action")
        self._register_int_dref("last_action")
        self._register_int_dref("action_ack")
        self._register_int_dref("plan_waypoint_count")
        self._register_int_dref("plan_max_altitude")

        self._register_float_dref("plan_distance_nm")
        self._register_float_dref("loaded_distance_nm")

        self._register_live_fms_drefs()

        self._create_command("previous", "Select previous FMS plan", self._cmd_previous)
        self._create_command("next", "Select next FMS plan", self._cmd_next)
        self._create_command("refresh", "Refresh FMS plan list", self._cmd_refresh)
        self._create_command("load", "Load selected FMS plan", self._cmd_load)
        self._create_command("open_fpl", "Open G1000 FPL page", self._cmd_open_fpl)
        self._create_command("wp_next", "Display next FMS waypoint", self._cmd_wp_next)
        self._create_command("wp_previous", "Display previous FMS waypoint", self._cmd_wp_previous)
        self._create_command("wp_activate", "Activate displayed FMS waypoint", self._cmd_wp_activate)
        self._create_command("wp_direct", "Direct-to displayed FMS waypoint", self._cmd_wp_direct)
        self._create_command("clear_fms_entry", "Clear displayed FMS entry", self._cmd_clear_fms_entry)
        self._create_command("map_range_down", "Map range zoom in", self._cmd_map_range_down)
        self._create_command("map_range_up", "Map range zoom out", self._cmd_map_range_up)
        self._create_command("map_toggle", "Toggle map range target", self._cmd_map_toggle)

        self._register_plan_list_window_drefs()
        self._create_plan_list_window_commands()

        for mode, (down_cmd, up_cmd) in self.map_range_cmds.items():
            self.map_cmd_refs[mode] = (xp.findCommand(down_cmd), xp.findCommand(up_cmd))
            self._log("findCommand map range", mode, down_cmd, "->", self.map_cmd_refs[mode][0],
                      up_cmd, "->", self.map_cmd_refs[mode][1])

        self._register_legs_drefs()
        self._create_legs_commands()

        self._proc_register_section("dep", self.DEP_DREF_PREFIX, self.DEP_CMD_PREFIX)
        self._proc_register_section("arr", self.ARR_DREF_PREFIX, self.ARR_CMD_PREFIX)
        self._proc_register_section("app", self.APP_DREF_PREFIX, self.APP_CMD_PREFIX)

        self._refresh_plan_list()
        return self.NAME, self.SIG, self.DESC

    def XPluginStop(self):
        for key, meta in self.commands.items():
            try:
                xp.unregisterCommandHandler(meta["ref"], meta["fun"], 1, None)
                self._log("Unregistered command handler", key)
            except Exception as exc:
                self._log("Failed to unregister command handler", key, exc)
        self.commands = {}

        for accessor in self.accessors:
            try:
                xp.unregisterDataAccessor(accessor)
            except Exception as exc:
                self._log("Failed to unregister data accessor", accessor, exc)
        self.accessors = []

        self._log("XPluginStop")
        return None

    def XPluginEnable(self):
        self.enabled = True
        self._log("XPluginEnable")
        self._proc_airports_from_fms()
        return 1

    def XPluginDisable(self):
        self.enabled = False
        self._log("XPluginDisable")
        return None

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        if inMessage == xp.MSG_PLANE_LOADED and inParam == 0:
            self._log("User aircraft loaded; refreshing plan list")
            self._refresh_plan_list()
            self._publish_state()
        return None

    # ── Core state helpers ──

    def _set_status(self, text: str, error: str = ""):
        self.last_status = text
        self.last_error = error
        self.string_values["status"] = text
        self.string_values["last_error"] = error

    def _selected_plan(self) -> FlightPlanInfo | None:
        if not self.plans:
            return None
        if self.index < 0 or self.index >= len(self.plans):
            return None
        return self.plans[self.index]

    def _publish_state(self):
        t0 = time.perf_counter()
        plan = self._selected_plan()

        self.int_values["count"] = len(self.plans)
        if self.plans and 0 <= self.index < len(self.plans):
            self.int_values["index"] = self.index + 1
        else:
            self.int_values["index"] = 0
        self.int_values["loaded"] = int(self.loaded)

        if plan is None:
            if not self.plans:
                self.string_values["plan_name"] = "No flight plans"
            else:
                self.string_values["plan_name"] = "Select plan"
            self.string_values["plan_departure"] = "----"
            self.string_values["plan_destination"] = "----"
            self.string_values["plan_cycle"] = ""
            self.string_values["plan_filename"] = ""
            self.string_values["plan_path"] = ""
            self.string_values["plan_dep_runway"] = ""
            self.string_values["plan_dest_runway"] = ""
            self.string_values["plan_sid"] = ""
            self.string_values["plan_star"] = ""
            self.string_values["plan_waypoints"] = ""
            self.int_values["plan_waypoint_count"] = 0
            self.int_values["plan_max_altitude"] = 0
            self.float_values["plan_distance_nm"] = 0.0
        else:
            self.string_values["plan_name"] = plan.display_name
            self.string_values["plan_departure"] = plan.dep
            self.string_values["plan_destination"] = plan.dest
            self.string_values["plan_cycle"] = plan.cycle
            self.string_values["plan_filename"] = os.path.splitext(plan.filename)[0]
            self.string_values["plan_path"] = plan.full_path
            self.string_values["plan_dep_runway"] = plan.dep_runway
            self.string_values["plan_dest_runway"] = plan.dest_runway
            self.string_values["plan_sid"] = plan.sid
            self.string_values["plan_star"] = plan.star
            self.string_values["plan_waypoints"] = plan.waypoint_list
            self.int_values["plan_waypoint_count"] = plan.waypoint_count
            self.int_values["plan_max_altitude"] = plan.max_altitude
            self.float_values["plan_distance_nm"] = plan.total_distance_nm

        self.string_values["loaded_filename"] = self.loaded_filename
        self.int_values["loaded_index"] = self.loaded_index
        self.string_values["loaded_sid"] = self.loaded_sid
        self.string_values["loaded_star"] = self.loaded_star
        self.float_values["loaded_distance_nm"] = self.loaded_distance_nm
        self.string_values["map_mode"] = self.map_mode_names[self.map_mode]

        self.string_values["status"] = self.last_status
        self.string_values["last_error"] = self.last_error
        self._log(
            "State",
            f"index={self.int_values['index']}",
            f"count={self.int_values['count']}",
            f"plan={self.string_values['plan_filename']}",
            f"status={self.string_values['status']}",
        )
        self._perf_log(f"publish_state_ms={(time.perf_counter() - t0) * 1000.0:.2f}")

    def _record_action(self, action: int):
        self.int_values["last_action"] = int(action)
        self.int_values["action_ack"] += 1

    def _perform_action(self, action: int):
        self._record_action(action)
        if action == self.ACTION_PREVIOUS:
            self._cmd_previous()
        elif action == self.ACTION_NEXT:
            self._cmd_next()
        elif action == self.ACTION_REFRESH:
            self._cmd_refresh()
        elif action == self.ACTION_LOAD:
            self._cmd_load()
        elif action == self.ACTION_OPEN_FPL:
            self._cmd_open_fpl()
        else:
            self._log("Ignoring action", action)
