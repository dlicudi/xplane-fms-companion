"""UIMixin — Dear ImGui plugin window for FMS Companion.

Requires xp_imgui (XPPython3 imgui wrapper); gracefully disabled if not available.

Tabs:
    LOAD     — .fms file browser
    NAV      — live data grid (WPT, XTK, DTK, GS, ETE, BRG, TRK, ETA)
    ROUTE    — active FMS legs list
    ADVISE   — recommended terminal setup and guidance

Registers a single X-Plane command:
    fmscompanion/toggle_window  — show/hide the window
"""

try:
    import xp_imgui
    import imgui
    _HAS_IMGUI = True
except ImportError:
    _HAS_IMGUI = False

import os

from XPPython3 import xp
from fmscompanion.models import SEVERITY_ERROR, SEVERITY_WARN

_TAB_LOAD  = 0
_TAB_NAV   = 1
_TAB_ROUTE = 2
_TAB_ADVISE = 3
_TAB_CHECK = 4
_TAB_FUEL  = 5
_TAB_WIND  = 6
_TAB_PREFS = 7

_TAB_LABELS = ["LOAD", "NAV", "ROUTE", "ADVISE", "CHECK", "FUEL", "WIND", "PREFS"]

_TAB_DEP   = 100
_TAB_ARR   = 101
_TAB_APP   = 102

_PROC_KIND_FOR_TAB = {_TAB_DEP: "dep", _TAB_ARR: "arr", _TAB_APP: "app"}

_COL_GREEN   = (0.2,  0.9,  0.2,  1.0)
_COL_YELLOW  = (1.0,  0.8,  0.0,  1.0)
_COL_ORANGE  = (1.0,  0.55, 0.0,  1.0)
_COL_CYAN    = (0.0,  0.75, 1.0,  1.0)
_COL_RED     = (1.0,  0.3,  0.3,  1.0)
_COL_GREY    = (0.6,  0.6,  0.6,  1.0)
_COL_WHITE   = (0.85, 0.85, 0.85, 1.0)
_COL_BLUE    = (0.25, 0.50, 0.75, 1.0)
_COL_BLUE_HOV = (0.35, 0.60, 0.85, 1.0)
_COL_DIM     = (0.45, 0.45, 0.45, 1.0)

# Sim datarefs used by the NAV grid
_NAV_DREF_NAMES = {
    "dis":    "sim/cockpit2/radios/indicators/gps_dme_distance_nm",
    "ete":    "sim/cockpit2/radios/indicators/gps_dme_time_min",
    "gs":     "sim/cockpit2/gauges/indicators/ground_speed_kt",
    "dtk":    "sim/cockpit2/radios/indicators/gps_bearing_deg_mag",
    "eta_h":  "sim/cockpit2/radios/indicators/fms1_act_eta_hour",
    "eta_m":  "sim/cockpit2/radios/indicators/fms1_act_eta_minute",
    "brg":    "sim/cockpit2/radios/indicators/gps_bearing_deg_mag",
    "xtk":    "sim/cockpit/radios/gps_course_deviation",
    "trk":    "sim/cockpit2/gauges/indicators/ground_track_mag_pilot",
    "ac_lat": "sim/flightmodel/position/latitude",
    "ac_lon": "sim/flightmodel/position/longitude",
    "ac_alt": "sim/flightmodel/position/elevation",   # metres MSL
}

# TOD computation constants
_TOD_DESCENT_FT_PER_NM   = 318.0   # ≈ 3° glide (318 ft / nm)
_TOD_PATTERN_ABOVE_FIELD = 1500.0  # target altitude above destination elev
_METRES_TO_FEET          = 3.28084


class UIMixin:
    """Mixin providing the Dear ImGui plugin window for FMS Companion."""

    def _ui_init(self):
        self._ui_window = None
        self._ui_tab = _TAB_NAV
        self._ui_menu_id = None
        self._nav_drefs = {}   # populated lazily on first NAV draw

    # ── Command + menu ─────────────────────────────────────────────────────────

    def _ui_register_command(self):
        self._create_command(
            "toggle_window",
            "Toggle FMS Companion window",
            self._ui_toggle_window,
            prefix="fmscompanion",
        )

    def _ui_build_menu(self):
        try:
            plugins_menu = xp.findPluginsMenu()
            # Submenu must attach to an item we own in the Plugins menu —
            # passing parentItem=0 without appending first hits another
            # plugin's slot and makes createMenu fail.
            parent_item = xp.appendMenuItem(plugins_menu, "FMS Companion", 0)
            self._ui_menu_id = xp.createMenu(
                "FMS Companion", plugins_menu, parent_item,
                self._ui_menu_handler, None)
            xp.appendMenuItem(self._ui_menu_id, "Show / Hide FMS Companion Window", "toggle")
        except Exception as exc:
            self._log("UI: failed to create menu:", exc)

    def _ui_menu_handler(self, menuRef, itemRef):
        self._ui_toggle_window()

    def _ui_destroy_menu(self):
        if self._ui_menu_id is not None:
            try:
                xp.destroyMenu(self._ui_menu_id)
            except Exception:
                pass
            self._ui_menu_id = None

    # ── Window lifecycle ───────────────────────────────────────────────────────

    def _ui_create_window(self):
        if not _HAS_IMGUI:
            self._log("UI: xp_imgui not available — window disabled")
            return
        if self._ui_window:
            return
        try:
            self._ui_window = xp_imgui.Window(
                left=60, top=720, right=700, bottom=180,
                draw=self._ui_draw,
                refCon=None,
                visible=1,
            )
            from fmscompanion import __version__
            self._ui_window.setTitle(f"FMS Companion  v{__version__}")
        except Exception as exc:
            self._log("UI: failed to create window:", exc)

    def _ui_destroy_window(self):
        if self._ui_window:
            try:
                xp.setWindowIsVisible(self._ui_window.windowID, False)
            except Exception:
                pass
            self._ui_window = None

    def _ui_toggle_window(self):
        if not self._ui_window:
            self._ui_create_window()
            return
        wid = self._ui_window.windowID
        xp.setWindowIsVisible(wid, not xp.getWindowIsVisible(wid))

    # ── Top-level draw callback ────────────────────────────────────────────────

    def _ui_draw(self, window_id, refcon):
        # Status bar
        status = self.string_values.get("status", "?")
        err    = self.string_values.get("last_error", "")
        if status == "ERROR":
            s_col = _COL_RED
        elif status in ("INIT", "EMPTY"):
            s_col = _COL_GREY
        elif status.startswith("LOADED") or status in ("OK",) or status.startswith("SORT"):
            s_col = _COL_GREEN
        else:
            s_col = _COL_WHITE
        imgui.text_colored(status, *s_col)
        if err:
            imgui.same_line()
            imgui.text_colored(f"  {err}", *_COL_RED)
        imgui.same_line()
        if imgui.button("Dump##statusdump"):
            path = self._cmd_dump_state()
            if path:
                self.string_values["last_dump"] = os.path.basename(path)
        last_dump = self.string_values.get("last_dump", "")
        if last_dump:
            imgui.same_line()
            imgui.text_colored(last_dump, *_COL_DIM)

        imgui.separator()

        # Tab bar
        issues = getattr(self, "validation_issues", [])
        for i, label in enumerate(_TAB_LABELS):
            if i > 0:
                imgui.same_line()
            active = self._ui_tab == i

            # CHECK tab: show count and tint by worst severity
            if i == _TAB_CHECK and issues:
                severities = {v.severity for v in issues}
                if SEVERITY_ERROR in severities:
                    btn_col = (0.7, 0.1, 0.1, 1.0)
                    hov_col = (0.8, 0.2, 0.2, 1.0)
                elif SEVERITY_WARN in severities:
                    btn_col = (0.55, 0.35, 0.0, 1.0)
                    hov_col = (0.65, 0.45, 0.0, 1.0)
                else:
                    btn_col, hov_col = _COL_BLUE, _COL_BLUE_HOV
                tab_label = f"CHECK {len(issues)}##tab{i}"
            else:
                btn_col, hov_col = _COL_BLUE, _COL_BLUE_HOV
                tab_label = f"{label}##tab{i}"

            if active:
                imgui.push_style_color(imgui.COLOR_BUTTON, *btn_col)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *hov_col)
            else:
                if i == _TAB_CHECK and issues:
                    imgui.push_style_color(imgui.COLOR_BUTTON, *btn_col)
                    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *hov_col)
            if imgui.button(tab_label):
                self._ui_tab = i
            if active or (i == _TAB_CHECK and issues):
                imgui.pop_style_color(2)

        imgui.separator()

        if self._ui_tab == _TAB_LOAD:
            self._ui_draw_load()
        elif self._ui_tab == _TAB_NAV:
            self._ui_draw_nav()
        elif self._ui_tab == _TAB_ROUTE:
            self._ui_draw_route()
        elif self._ui_tab == _TAB_ADVISE:
            self._ui_draw_advise()
        elif self._ui_tab == _TAB_CHECK:
            self._ui_draw_check()
        elif self._ui_tab == _TAB_FUEL:
            self._ui_draw_fuel()
        elif self._ui_tab == _TAB_WIND:
            self._ui_draw_wind()
        elif self._ui_tab == _TAB_PREFS:
            self._ui_draw_prefs()
        elif self._ui_tab in _PROC_KIND_FOR_TAB:
            kind = _PROC_KIND_FOR_TAB[self._ui_tab]
            self._ui_draw_proc_names(kind)

    # ── Dynamic row count ─────────────────────────────────────────────────────

    # Approximate pixel height of one button row in the default xp_imgui style.
    _ROW_HEIGHT_PX = 22
    # Fixed overhead that is always present: title bar + status line + tab bar.
    _CHROME_PX     = 90

    def _ui_visible_rows(self, reserved_px: int = 200, minimum: int = 3) -> int:
        """Return how many list rows fit in the current window height.

        Tries Dear ImGui introspection first; falls back to XP window geometry
        (reliable since we always have the windowID).
        """
        # 1. Dear ImGui content region (works if xp_imgui exposes these)
        try:
            _, avail_h = imgui.get_content_region_avail()
            line_h     = imgui.get_text_line_height_with_spacing()
            if line_h > 0 and avail_h > reserved_px:
                return max(minimum, int((avail_h - reserved_px) / line_h))
        except Exception:
            pass
        # 2. XP window geometry fallback
        try:
            wid = self._ui_window.windowID
            _, top, _, bottom = xp.getWindowGeometry(wid)
            win_h = top - bottom
            usable = win_h - self._CHROME_PX - reserved_px
            if usable > 0:
                return max(minimum, int(usable / self._ROW_HEIGHT_PX))
        except Exception:
            pass
        return minimum

    # ── NAV tab ───────────────────────────────────────────────────────────────

    def _ui_ensure_nav_drefs(self):
        if self._nav_drefs:
            return
        for key, path in _NAV_DREF_NAMES.items():
            try:
                ref = xp.findDataRef(path)
                self._nav_drefs[key] = ref if ref else None
            except Exception:
                self._nav_drefs[key] = None

    def _nav_getf(self, key, default=0.0):
        ref = self._nav_drefs.get(key)
        if not ref:
            return default
        try:
            return xp.getDataf(ref)
        except Exception:
            return default

    def _ui_draw_nav(self):
        self._ui_ensure_nav_drefs()

        def fmtf(key, fmt, suffix=""):
            v = self._nav_getf(key)
            return f"{fmt.format(v)}{suffix}"

        dst_nm = self.float_values.get("loaded_distance_nm", 0.0)

        # Row 1: DIS | ETE | GS | DTK
        imgui.columns(4, "nav_r1", border=True)

        imgui.text_colored("DIS", *_COL_YELLOW)
        imgui.text(fmtf("dis", "{:.1f}"))
        imgui.text_colored(f"DST {dst_nm:.0f}", *_COL_DIM)
        imgui.next_column()

        imgui.text_colored("ETE", *_COL_YELLOW)
        imgui.text_colored(fmtf("ete", "{:.0f}", " min"), *_COL_ORANGE)
        imgui.next_column()

        imgui.text_colored("GS", *_COL_YELLOW)
        imgui.text(fmtf("gs", "{:.0f}", " kts"))
        imgui.next_column()

        imgui.text_colored("DTK", *_COL_YELLOW)
        imgui.text_colored(fmtf("dtk", "{:.0f}", "\xb0"), *_COL_ORANGE)

        imgui.columns(1)
        imgui.separator()

        # Row 2: WPT | ETA | BRG | XTK
        imgui.columns(4, "nav_r2", border=True)

        imgui.text_colored("WPT", *_COL_YELLOW)
        imgui.text_colored(self._read_fms_active_ident(), *_COL_GREEN)
        imgui.next_column()

        eta_h = int(self._nav_getf("eta_h"))
        eta_m = int(self._nav_getf("eta_m"))
        imgui.text_colored("ETA", *_COL_YELLOW)
        imgui.text_colored(f"{eta_h:02d}:{eta_m:02d}Z", *_COL_ORANGE)
        imgui.next_column()

        imgui.text_colored("BRG", *_COL_YELLOW)
        imgui.text(fmtf("brg", "{:.0f}", "\xb0"))
        imgui.next_column()

        imgui.text_colored("XTK", *_COL_YELLOW)
        imgui.text(fmtf("xtk", "{:.1f}", " nm"))

        imgui.columns(1)
        imgui.separator()

        # Row 3: TRK | DEST | DTO | MAP
        imgui.columns(4, "nav_r3", border=True)

        imgui.text_colored("TRK", *_COL_YELLOW)
        imgui.text(fmtf("trk", "{:.0f}", "\xb0"))
        imgui.next_column()

        imgui.text_colored("DEST", *_COL_YELLOW)
        imgui.text_colored(self._read_fms_last_ident(), *_COL_CYAN)
        imgui.next_column()

        imgui.text_colored("DTO", *_COL_YELLOW)
        active_ident = self._read_fms_active_ident()
        if imgui.button(f"{active_ident}##dto"):
            self._cmd_legs_direct_to()
        imgui.next_column()

        imgui.text_colored("MAP", *_COL_YELLOW)
        map_label = self.map_mode_names[self.map_mode] if self.map_mode_names else "G1000"
        if imgui.button(f"{map_label}##maptog"):
            self._cmd_map_toggle()

        imgui.columns(1)

        # TOD advisory — 3° descent to pattern altitude at destination.
        tod = self._ui_compute_tod()
        if tod is not None:
            imgui.separator()
            self._ui_draw_tod(tod)

        # Advisory banner — shown only when there are active advisories
        advisories = getattr(self, "nav_advisories", [])
        if advisories:
            imgui.separator()
            for msg in advisories:
                imgui.text_colored(f"\u26a0  {msg}", *_COL_RED)

    # ── TOD (top-of-descent) advisory ────────────────────────────────────────

    def _ui_compute_tod(self):
        """Compute a straight-line top-of-descent advisory relative to the
        last FMS entry. Returns a dict or None when data is unavailable or
        the result isn't meaningful (no route, below pattern alt already,
        on the ground)."""
        import math

        count = self._read_fms_entry_count()
        if count <= 0:
            return None

        self._ui_ensure_nav_drefs()
        lat_ref = self._nav_drefs.get("ac_lat")
        lon_ref = self._nav_drefs.get("ac_lon")
        alt_ref = self._nav_drefs.get("ac_alt")
        if not (lat_ref and lon_ref and alt_ref):
            return None

        try:
            ac_lat    = xp.getDataf(lat_ref)
            ac_lon    = xp.getDataf(lon_ref)
            ac_alt_ft = xp.getDataf(alt_ref) * _METRES_TO_FEET
        except Exception:
            return None

        dest = self._safe_fms_entry_info(count - 1)
        if not dest:
            return None
        dest_lat = getattr(dest, "latitude", None)
        if dest_lat is None:
            dest_lat = getattr(dest, "lat", None)
        dest_lon = getattr(dest, "longitude", None)
        if dest_lon is None:
            dest_lon = getattr(dest, "lon", None)
        if dest_lat is None or dest_lon is None:
            return None
        dest_lat = float(dest_lat)
        dest_lon = float(dest_lon)

        # Destination field elevation: FMS stores altitude in feet; for airport
        # entries this is usually 0, so we can't count on it. Treat 0 as unknown
        # and assume sea level — acceptable for an advisory.
        dest_elev_ft = float(getattr(dest, "altitude", 0) or 0)

        R_NM = 3440.065
        dlat = math.radians(dest_lat - ac_lat)
        dlon = math.radians(dest_lon - ac_lon)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(ac_lat)) * math.cos(math.radians(dest_lat))
             * math.sin(dlon / 2) ** 2)
        dist_to_dest = R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        target_alt_ft = dest_elev_ft + _TOD_PATTERN_ABOVE_FIELD
        alt_to_lose   = ac_alt_ft - target_alt_ft
        if alt_to_lose <= 0:
            return None   # already at or below pattern altitude

        tod_dist_from_dest = alt_to_lose / _TOD_DESCENT_FT_PER_NM
        dist_to_tod        = dist_to_dest - tod_dist_from_dest

        gs = self._nav_getf("gs")
        time_to_tod_min = (dist_to_tod / gs) * 60.0 if gs >= 40.0 else None

        return {
            "dist_to_tod":    dist_to_tod,
            "dist_to_dest":   dist_to_dest,
            "alt_to_lose":    alt_to_lose,
            "time_to_tod":    time_to_tod_min,
            "target_alt":     target_alt_ft,
            "past_tod":       dist_to_tod < 0,
        }

    def _ui_draw_tod(self, tod: dict):
        alt_to_lose = tod["alt_to_lose"]
        dist_to_tod = tod["dist_to_tod"]
        time_to_tod = tod["time_to_tod"]

        if tod["past_tod"]:
            msg = f"DESCEND NOW — {abs(dist_to_tod):.0f} nm past TOD  (lose {alt_to_lose:.0f} ft)"
            imgui.text_colored(msg, *_COL_RED)
        elif dist_to_tod < 1.0:
            msg = f"TOD imminent — start descent  (lose {alt_to_lose:.0f} ft)"
            imgui.text_colored(msg, *_COL_YELLOW)
        else:
            if time_to_tod is not None:
                msg = f"TOD in {dist_to_tod:.0f} nm ({time_to_tod:.0f} min)  —  lose {alt_to_lose:.0f} ft"
            else:
                msg = f"TOD in {dist_to_tod:.0f} nm  —  lose {alt_to_lose:.0f} ft"
            imgui.text_colored(msg, *_COL_CYAN)
        imgui.text_colored(
            f"  3\xb0 descent to {tod['target_alt']:.0f} ft MSL (field + 1500)",
            *_COL_DIM,
        )

    # ── LOAD tab ───────────────────────────────────────────────────────────────

    # ── Clipboard ────────────────────────────────────────────────────────────

    def _read_clipboard(self) -> str:
        """Return the OS clipboard as a string, or '' on any failure.

        Workaround for xp_imgui: Ctrl/Cmd+V paste doesn't reliably reach the
        input_text widget, so we shell out to the platform clipboard tool.
        """
        import subprocess
        import sys
        try:
            if sys.platform == "darwin":
                out = subprocess.check_output(["pbpaste"], timeout=1)
            elif sys.platform == "win32":
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                    timeout=1,
                )
            else:
                out = subprocess.check_output(
                    ["xclip", "-selection", "clipboard", "-o"], timeout=1,
                )
            return out.decode("utf-8", errors="replace")
        except Exception as exc:
            self._log("clipboard read failed:", exc)
            return ""

    # ── Route entry (typed custom route) ─────────────────────────────────────

    _ROUTE_ENTRY_BUF = 256

    _TOKEN_COLOURS = {
        "ok":        _COL_GREEN,
        "skipped":   _COL_DIM,
        "too_far":   _COL_ORANGE,
        "not_found": _COL_RED,
    }

    def _ui_draw_route_entry(self):
        # Poll for a completed background fetch and apply it on the main thread.
        result = self._simbrief_result
        if result is not None:
            self._simbrief_result = None
            route_str, error = result
            if route_str:
                self.route_entry_text = route_str
                self._cmd_route_entry_parse()
            else:
                self._simbrief_error = error or "Unknown error"

        if self.simbrief_id:
            if self._simbrief_fetching:
                imgui.text_colored("Simbrief: fetching...", *_COL_DIM)
            else:
                if imgui.button("FETCH FROM SIMBRIEF"):
                    self._simbrief_error = ""
                    self._cmd_simbrief_fetch()
            err = getattr(self, "_simbrief_error", "")
            if err:
                imgui.text_colored(f"  {err}", *_COL_RED)
            imgui.separator()

        imgui.text_colored("Route:", *_COL_YELLOW)
        imgui.same_line()
        try:
            changed, new_text = imgui.input_text(
                "##route_entry", self.route_entry_text, self._ROUTE_ENTRY_BUF,
            )
        except Exception:
            # Older xp_imgui builds used a 2-arg signature.
            changed, new_text = imgui.input_text("##route_entry", self.route_entry_text)
        if changed:
            self.route_entry_text = new_text

        imgui.same_line()
        if imgui.button("PASTE##route_entry"):
            pasted = self._read_clipboard()
            if pasted:
                self.route_entry_text = pasted.strip()
        imgui.same_line()
        if imgui.button("PARSE##route_entry"):
            self._cmd_route_entry_parse()
        imgui.same_line()
        if imgui.button("CLEAR##route_entry"):
            self.route_entry_text = ""
            self.route_entry_parsed = []
            self.route_entry_status = ""

        tokens = self.route_entry_parsed
        if tokens:
            for t in tokens:
                col = self._TOKEN_COLOURS.get(t.status, _COL_WHITE)
                marker = {"ok": "+", "skipped": ".",
                          "too_far": "!", "not_found": "x"}.get(t.status, "?")
                line = f"  {marker} {t.raw:<10} {t.category}"
                if t.message:
                    line += f"  - {t.message}"
                imgui.text_colored(line, *col)

            imgui.text_colored(f"  {self.route_entry_status}", *_COL_DIM)

            ok_entries = any(t.entry is not None for t in tokens)
            if ok_entries and imgui.button("LOAD ROUTE INTO FMS##route_entry"):
                self._cmd_route_entry_load()

    def _ui_draw_load(self):
        sv = self.string_values

        # ── Route entry (typed custom route) ──
        self._ui_draw_route_entry()
        imgui.separator()

        loaded_fn = sv.get("loaded_filename", "")

        if loaded_fn:
            imgui.text_colored(f"Loaded: {loaded_fn}", *_COL_GREEN)
            parts = []
            if sv.get("loaded_sid"):
                parts.append(f"SID {sv['loaded_sid']}")
            if sv.get("loaded_star"):
                parts.append(f"STAR {sv['loaded_star']}")
            if parts:
                imgui.same_line()
                imgui.text_colored(f"  {' | '.join(parts)}", *_COL_YELLOW)
        else:
            imgui.text_colored("No plan loaded", *_COL_GREY)

        imgui.separator()

        sort_key = "NAME" if self.plan_sort_key == 0 else "DATE"
        sort_dir = "DESC" if self.plan_sort_desc else "ASC"
        if imgui.button(f"Key: {sort_key}"):
            self._cmd_list_toggle_sort_key()
        imgui.same_line()
        if imgui.button(f"Dir: {sort_dir}"):
            self._cmd_list_toggle_sort_direction()
        imgui.same_line()
        if imgui.button("Refresh##load"):
            self._cmd_refresh()

        imgui.separator()

        n = len(self.plans)
        if n == 0:
            imgui.text_colored("No flight plans found", *_COL_GREY)
            return

        imgui.columns(4, "load_hdr", border=False)
        imgui.text_colored("FPL", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("WPTS", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("MAX ALT", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("DIS", *_COL_YELLOW)
        imgui.columns(1)
        imgui.separator()

        self._ensure_list_cache()
        for row in range(1, self.PLAN_LIST_VISIBLE_ROWS + 1):
            r = self._list_rows_cache.get(row, {})
            fn = r.get("filename", "")
            if not fn:
                imgui.text_colored("-", *_COL_DIM)
                continue
            is_sel  = r.get("is_selected", 0)
            wpts    = r.get("wpt_count", 0)
            max_alt = r.get("max_alt_ft", 0)
            dist    = int(round(r.get("distance_nm", 0.0)))
            col     = _COL_ORANGE if not is_sel else _COL_WHITE

            imgui.columns(4, f"load_r{row}", border=False)
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{'>' if is_sel else ' '} {fn}##{row}"):
                self._cmd_list_select_row(row)
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(str(wpts), *col)
            imgui.next_column()
            imgui.text_colored(f"{max_alt} ft" if max_alt else "--", *col)
            imgui.next_column()
            imgui.text_colored(f"{dist} nm" if dist else "--", *col)
            imgui.columns(1)

        imgui.separator()
        page_ind = self._plan_list_read_page_indicator() or "\u2014"
        imgui.text(f"Page {page_ind}  ({n} plans)")
        imgui.same_line()
        if imgui.button("< Prev##load"):
            self._cmd_list_scroll_up()
        imgui.same_line()
        if imgui.button("Next >##load"):
            self._cmd_list_scroll_down()

        if self.index >= 0:
            plan = self._selected_plan()
            if plan:
                imgui.separator()
                dep  = plan.dep  or "----"
                dest = plan.dest or "----"
                imgui.text_colored(f"{plan.display_name}", *_COL_YELLOW)
                imgui.text(f"  {dep} \u2192 {dest}   {plan.total_distance_nm:.0f} nm   {plan.waypoint_count} wpts")
                if plan.sid:
                    imgui.text(f"  SID: {plan.sid}")
                    if plan.star:
                        imgui.same_line()
                        imgui.text(f"   STAR: {plan.star}")
                elif plan.star:
                    imgui.text(f"  STAR: {plan.star}")
                imgui.separator()
                if imgui.button("LOAD INTO FMS"):
                    self._cmd_load()
                imgui.same_line()
                if imgui.button("+ RECOMMENDED"):
                    self._cmd_load_recommended()

    # ── ROUTE tab ─────────────────────────────────────────────────────────────

    def _ui_draw_route(self):
        count = self._read_fms_entry_count()
        if count <= 0:
            imgui.text_colored("FMS route is empty", *_COL_GREY)
            imgui.separator()
            if imgui.button("Detect airports"):
                self._proc_airports_from_fms()
            return

        page_ind = self._legs_read_page_indicator()
        imgui.text(f"Active: ")
        imgui.same_line()
        imgui.text_colored(self._read_fms_active_ident(), *_COL_GREEN)
        imgui.same_line()
        imgui.text(f"  {count} entries  page {page_ind}")
        imgui.same_line()
        imgui.text_colored(f"  SEL {self._legs_read_selected_over_count()}", *_COL_DIM)

        imgui.separator()

        imgui.columns(5, "route_hdr", border=False)
        imgui.text_colored("#",   *_COL_YELLOW); imgui.next_column()
        imgui.text_colored("WPT", *_COL_YELLOW); imgui.next_column()
        imgui.text_colored("DTK", *_COL_YELLOW); imgui.next_column()
        imgui.text_colored("DIS", *_COL_YELLOW); imgui.next_column()
        imgui.text_colored("ALT", *_COL_YELLOW)
        imgui.columns(1)
        imgui.separator()

        for row in range(1, self.LEGS_VISIBLE_ROWS + 1):
            idx = self._legs_fms_index_for_row(row)
            if idx < 0:
                imgui.text_colored("-", *_COL_DIM)
                continue
            ident     = self._legs_read_row_ident(row) or "---"
            alt       = self._legs_read_row_alt(row)
            dis       = self._legs_leg_distance_nm(row)
            dtk       = self._legs_leg_dtk(row)
            is_active = self._legs_read_row_is_active(row)
            is_sel    = self._legs_read_row_is_selected(row)

            if is_active:
                col = _COL_GREEN
            elif is_sel:
                col = _COL_YELLOW
            else:
                col = _COL_WHITE

            dis_str = f"{dis:.0f}" if dis >= 0 else "--"
            dtk_str = f"{dtk:.0f}\xb0" if dtk >= 0 else "--"

            imgui.columns(5, f"route_r{row}", border=False)
            imgui.text_colored(str(idx + 1), *_COL_DIM)
            imgui.next_column()
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{ident}##{row}"):
                self._cmd_legs_select_row(row)
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(dtk_str, *_COL_DIM)
            imgui.next_column()
            imgui.text_colored(dis_str, *_COL_ORANGE)
            imgui.next_column()
            imgui.text_colored(alt if alt else "--", *_COL_DIM)
            imgui.columns(1)

        imgui.separator()
        if imgui.button("< Prev##route"):
            self._cmd_legs_scroll_up()
        imgui.same_line()
        if imgui.button("Next >##route"):
            self._cmd_legs_scroll_down()

        if self.legs_selected >= 0:
            imgui.separator()
            sel_info  = self._safe_fms_entry_info(self.legs_selected)
            sel_ident = self._legs_format_ident(sel_info)
            imgui.text_colored(f"Selected: {sel_ident}  (#{self.legs_selected + 1})", *_COL_YELLOW)
            if imgui.button("Activate"):
                self._cmd_legs_activate()
            imgui.same_line()
            if imgui.button("Direct-To"):
                self._cmd_legs_direct_to()
            imgui.same_line()
            if imgui.button("Clear WPT"):
                self._cmd_legs_clear_selected()
            imgui.same_line()
            if imgui.button("Clear From Here"):
                self._cmd_legs_clear_from_here()

        imgui.separator()
        if imgui.button("Direct-To Dest"):
            self._cmd_legs_direct_to_destination()
        imgui.same_line()
        if imgui.button("Sync from FMS"):
            self._cmd_sync_from_fms()
        imgui.same_line()
        if imgui.button("Clear All"):
            self._cmd_legs_clear_all()

    # ── ADVISE tab ────────────────────────────────────────────────────────────

    def _ui_recommended_proc(self, kind: str):
        route = self._route_idents() if hasattr(self, "_route_idents") else set()
        airport = self._proc_airport_for(kind) if hasattr(self, "_proc_airport_for") else ""

        if airport and not self._proc_procs.get(kind) and hasattr(self, "_proc_refresh"):
            self._proc_refresh()

        if kind == "dep":
            recs = getattr(self, "dep_recommended_sids", [])
            if recs:
                display_name = recs[0][0]
                return next((p for p in self._proc_procs.get(kind, []) if p.display_name == display_name), None)
            loaded = self._proc_loaded.get(kind, "")
            if loaded:
                return next((p for p in self._proc_procs.get(kind, []) if p.display_name == loaded), None)
            return None
        if kind == "arr":
            recs = getattr(self, "arr_recommended_stars", [])
            if recs:
                display_name = recs[0]
                return next((p for p in self._proc_procs.get(kind, []) if p.display_name == display_name), None)
            best_rwy = self._best_arrival_runway() if hasattr(self, "_best_arrival_runway") else ""
            best_num = self._rwy_num(best_rwy) if best_rwy and hasattr(self, "_rwy_num") else ""
            candidates = []
            seen = set()
            for proc in self._proc_procs.get(kind, []):
                if proc.name in seen:
                    continue
                seen.add(proc.name)
                if best_num and f"RW{best_num}" not in proc.display_name.upper():
                    continue
                connected = int(
                    (proc.transition and proc.transition in route)
                    or (proc.waypoints and proc.waypoints[0] in route)
                )
                candidates.append((connected, proc))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1]
            loaded = self._proc_loaded.get(kind, "")
            if loaded:
                return next((p for p in self._proc_procs.get(kind, []) if p.display_name == loaded), None)
            return None
        recs = getattr(self, "arr_recommended_apps", [])
        if recs:
            display_name = recs[0][0]
            return next((p for p in self._proc_procs.get(kind, []) if p.display_name == display_name), None)
        best_rwy = self._best_arrival_runway() if hasattr(self, "_best_arrival_runway") else ""
        best_num = self._rwy_num(best_rwy) if best_rwy and hasattr(self, "_rwy_num") else ""
        candidates = []
        for proc in self._proc_procs.get(kind, []):
            proc_rwy = (proc.display_runway or "").strip().upper()
            if best_rwy and proc_rwy != best_rwy and not (best_num and self._rwy_num(proc.display_runway or "") == best_num):
                continue
            connected = int(
                (proc.transition and proc.transition in route)
                or (proc.waypoints and proc.waypoints[0] in route)
            )
            candidates.append((connected, proc))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        loaded = self._proc_loaded.get(kind, "")
        if loaded:
            return next((p for p in self._proc_procs.get(kind, []) if p.display_name == loaded), None)
        return None

    def _ui_recommendation_reason(self, kind: str, proc) -> str:
        if not proc:
            airport = self._proc_airport_for(kind) or "airport"
            label = {"dep": "SIDs", "arr": "STARs", "app": "approaches"}.get(kind, "procedures")
            # Distinguish "no procs published for this airport" from
            # "procs exist but cache is empty".
            if not self._proc_procs.get(kind):
                return f"{airport} has no {label} cached. Refresh procedures."
            return f"No {label} published for {airport}."

        route = self._route_idents() if hasattr(self, "_route_idents") else set()
        plan = self._selected_plan() if hasattr(self, "_selected_plan") else None

        if kind == "dep":
            if getattr(self, "dep_runway_ranking", []):
                best = self.dep_runway_ranking[0][0]
                reason = f"Runway {best} is the best departure runway from current wind."
            else:
                dep_rwy = getattr(plan, "dep_runway", "") if plan else ""
                reason = f"Using filed departure runway {dep_rwy}." if dep_rwy else "No wind ranking; showing best route-connected SID."
            if proc.waypoints and proc.waypoints[-1] in route:
                reason += f" Route joins at {proc.waypoints[-1]}."
            return reason

        if kind == "arr":
            if getattr(self, "wind_runway_ranking", []):
                best = self.wind_runway_ranking[0][0]
                reason = f"Runway {best} is the best arrival runway from current wind."
            else:
                dest_rwy = getattr(plan, "dest_runway", "") if plan else ""
                reason = f"Using filed destination runway {dest_rwy}." if dest_rwy else "No wind ranking; using best route-connected STAR."
            join_fix = proc.transition if proc.transition in route else (proc.waypoints[0] if proc.waypoints and proc.waypoints[0] in route else "")
            if join_fix:
                reason += f" Route joins at {join_fix}."
            return reason

        if getattr(self, "wind_runway_ranking", []):
            best = self.wind_runway_ranking[0][0]
            reason = f"Approach runway {best} is favored by wind."
        else:
            dest_rwy = getattr(plan, "dest_runway", "") if plan else ""
            reason = f"Using filed destination runway {dest_rwy}." if dest_rwy else "No wind ranking; using best matching approach."
        if proc.transition and proc.transition in route:
            reason += f" Entry transition {proc.transition} matches the route."
        elif proc.transition:
            reason += f" Entry transition {proc.transition} is available."
        return reason

    def _ui_draw_advice_section(self, kind: str, title: str, airport: str):
        proc = self._ui_recommended_proc(kind)
        loaded = self._proc_loaded.get(kind, "")
        label = "SID" if kind == "dep" else ("STAR" if kind == "arr" else "APP")

        imgui.text_colored(title, *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {airport or '----'}", *(_COL_CYAN if airport else _COL_GREY))
        if proc and loaded == proc.display_name:
            imgui.same_line()
            imgui.text_colored("  \u2713 loaded", *_COL_GREEN)
        elif loaded:
            imgui.same_line()
            imgui.text_colored(f"  loaded: {loaded}", *_COL_DIM)

        if not proc:
            imgui.text_colored(self._ui_recommendation_reason(kind, proc), *_COL_GREY)
            return

        imgui.columns(2, f"adv_{kind}_head", border=False)
        imgui.text_colored(label, *_COL_DIM)
        imgui.next_column()
        imgui.text_colored(proc.display_name, *_COL_GREEN)
        imgui.columns(1)

        imgui.columns(2, f"adv_{kind}_meta", border=False)
        imgui.text_colored("Transition", *_COL_DIM)
        imgui.next_column()
        imgui.text_colored(proc.transition or "--", *_COL_WHITE)
        imgui.columns(1)

        imgui.columns(2, f"adv_{kind}_rwy", border=False)
        imgui.text_colored("Runway", *_COL_DIM)
        imgui.next_column()
        imgui.text_colored(proc.display_runway or "--", *_COL_ORANGE)
        imgui.columns(1)

        fixes_preview = " ".join(proc.waypoints[:6]) if proc.waypoints else "--"
        if len(proc.waypoints) > 6:
            fixes_preview += f" +{len(proc.waypoints) - 6}"
        imgui.columns(2, f"adv_{kind}_fixes", border=False)
        imgui.text_colored("Fixes", *_COL_DIM)
        imgui.next_column()
        imgui.text_colored(fixes_preview, *_COL_WHITE)
        imgui.columns(1)

        imgui.text_colored(self._ui_recommendation_reason(kind, proc), *_COL_DIM)

    def _ui_draw_advise(self):
        plan = self._selected_plan() if hasattr(self, "_selected_plan") else None
        dep_icao = getattr(self, "proc_dep_icao", "") or getattr(plan, "dep", "")
        dest_icao = getattr(self, "proc_dest_icao", "") or getattr(plan, "dest", "")

        avionics = getattr(self, "avionics_name", "FMS")
        imgui.text_colored("Advisory Only", *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(
            f"  Recommended terminal setup for native {avionics} PROC loading.",
            *_COL_DIM,
        )
        if imgui.button("Refresh Advice##advise_refresh"):
            if hasattr(self, "_proc_airports_from_fms"):
                self._proc_airports_from_fms()
            if hasattr(self, "_proc_refresh"):
                self._proc_refresh()
            self._cmd_wind_refresh()
        imgui.same_line()
        if imgui.button(f"Open {avionics} FPL##advise_fpl"):
            self._cmd_open_fpl()

        imgui.text_colored(
            f"Preferred workflow: review advice here, then load procedures in native {avionics} PROC. Direct FMS injection remains an internal fallback and may display differently.",
            *_COL_DIM,
        )
        imgui.separator()

        self._ui_draw_advice_section("dep", "Departure", dep_icao)
        imgui.separator()
        self._ui_draw_advice_section("arr", "Arrival", dest_icao)
        imgui.separator()
        self._ui_draw_advice_section("app", "Approach", dest_icao)

        imgui.separator()
        issues = getattr(self, "validation_issues", [])
        if issues:
            imgui.text_colored("Current route still has validation findings on CHECK.", *_COL_ORANGE)
        else:
            imgui.text_colored("Current route passes CHECK.", *_COL_GREEN)

    # ── DEP / ARR / APP — procedure name list ─────────────────────────────────

    def _ui_draw_proc_names(self, kind: str):
        label_map = {"dep": "SID", "arr": "STAR", "app": "APP"}
        label    = label_map[kind]
        airport  = self._proc_airport_for(kind)
        names    = self._proc_names.get(kind, [])
        loaded   = self._proc_loaded.get(kind, "")
        sel_name = self._proc_selected_proc_name(kind)
        transitions = self._proc_transitions(kind)
        idx = self._proc_index.get(kind, -1)

        # Build set of recommended proc names for green highlighting
        if kind == "dep":
            rec_display = {dn for dn, _ in getattr(self, "dep_recommended_sids", [])}
        elif kind == "arr":
            rec_display = set(getattr(self, "arr_recommended_stars", []))
        elif kind == "app":
            rec_display = {dn for dn, _ in getattr(self, "arr_recommended_apps", [])}
        else:
            rec_display = set()
        rec_names = {
            p.name for p in self._proc_procs.get(kind, [])
            if p.display_name in rec_display
        }

        imgui.text_colored(label, *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {airport or '----'}", *(_COL_YELLOW if airport else _COL_GREY))
        if loaded:
            imgui.same_line()
            imgui.text_colored(f"  \u2713 {loaded}", *_COL_GREEN)
        if rec_names:
            imgui.same_line()
            imgui.text_colored("  * = wind favoured", *_COL_DIM)
        imgui.same_line()
        if imgui.button(f"Refresh##{kind}"):
            self._cmd_proc_refresh(kind)
        imgui.same_line()
        if imgui.button(f"Advice##{kind}_advice"):
            self._ui_tab = _TAB_ADVISE

        imgui.separator()
        avionics = getattr(self, "avionics_name", "FMS")
        imgui.text_colored(
            f"Advisory/native {avionics} loading is preferred for display fidelity. Direct injection below is a fallback.",
            *_COL_DIM,
        )
        imgui.separator()

        if not names:
            imgui.text_colored(f"No {label} procedures found", *_COL_GREY)
            imgui.text_colored(f"  DEP: {self.proc_dep_icao or '?'}   DEST: {self.proc_dest_icao or '?'}", *_COL_DIM)
            return

        n = len(names)
        page_size = self.PROC_VISIBLE_ROWS
        name_window_start = self._proc_name_window.get(kind, 0)
        name_page = name_window_start // page_size + 1
        name_total_pages = max(1, (n + page_size - 1) // page_size)
        nt = len(transitions)
        trans_window_start = self._proc_window.get(kind, 0)
        trans_page = trans_window_start // page_size + 1 if nt else 1
        trans_total_pages = max(1, (nt + page_size - 1) // page_size) if nt else 1

        imgui.columns(2, f"{kind}_split", border=False)

        imgui.text_colored(f"{label} Names", *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {name_page}/{name_total_pages}", *_COL_DIM)
        imgui.separator()
        imgui.columns(3, f"{kind}_hdr", border=False)
        imgui.text_colored("#", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored(label, *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("TR", *_COL_YELLOW)
        imgui.columns(1)
        imgui.separator()

        for row in range(page_size):
            pi = name_window_start + row
            if pi >= n:
                imgui.text_colored("-", *_COL_DIM)
                continue
            name = names[pi]
            is_sel = (name == sel_name and sel_name != "")
            is_rec = name in rec_names
            trans_count = sum(1 for p in self._proc_procs.get(kind, []) if p.name == name)
            col = _COL_YELLOW if is_sel else (_COL_GREEN if is_rec else _COL_WHITE)
            prefix = "*" if (is_rec and not is_sel) else (">" if is_sel else " ")

            imgui.columns(3, f"{kind}_r{row}", border=False)
            imgui.text_colored(str(pi + 1), *_COL_DIM)
            imgui.next_column()
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{prefix} {name}##{kind}_{row}"):
                self._cmd_proc_select_row(kind, pi)
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(str(trans_count), *_COL_DIM)
            imgui.columns(1)

        imgui.separator()
        if imgui.button(f"< Prev##{kind}_prev"):
            self._cmd_proc_scroll_up(kind)
        imgui.same_line()
        if imgui.button(f"Next >##{kind}_next"):
            self._cmd_proc_scroll_down(kind)

        imgui.next_column()

        imgui.text_colored("Transitions", *_COL_YELLOW)
        if sel_name:
            imgui.same_line()
            imgui.text_colored(f"  {sel_name}", *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {trans_page}/{trans_total_pages}", *_COL_DIM)
        imgui.separator()

        if not sel_name:
            imgui.text_colored(f"Select a {label} on the left.", *_COL_GREY)
        elif not transitions:
            imgui.text_colored(f"No transitions for {sel_name}", *_COL_GREY)
        else:
            imgui.columns(3, f"{kind}tr_hdr", border=False)
            imgui.text_colored("#", *_COL_YELLOW)
            imgui.next_column()
            imgui.text_colored("Transition", *_COL_YELLOW)
            imgui.next_column()
            imgui.text_colored("RWY", *_COL_YELLOW)
            imgui.columns(1)
            imgui.separator()

            for row in range(page_size):
                pi = trans_window_start + row
                if pi >= nt:
                    imgui.text_colored("-", *_COL_DIM)
                    continue
                proc   = transitions[pi]
                is_sel = pi == idx
                col    = _COL_YELLOW if is_sel else _COL_WHITE

                imgui.columns(3, f"{kind}tr_r{row}", border=False)
                imgui.text_colored(str(pi + 1), *_COL_DIM)
                imgui.next_column()
                imgui.push_style_color(imgui.COLOR_TEXT, *col)
                if imgui.button(f"{'>' if is_sel else ' '} {proc.display_name}##{kind}tr_{row}"):
                    self._cmd_proc_select_trans_row(kind, pi)
                imgui.pop_style_color()
                imgui.next_column()
                imgui.text_colored(proc.display_runway or "", *_COL_ORANGE)
                imgui.columns(1)

            imgui.separator()
            if imgui.button(f"< Prev##{kind}tr_prev"):
                self._cmd_proc_trans_scroll_up(kind)
            imgui.same_line()
            if imgui.button(f"Next >##{kind}tr_next"):
                self._cmd_proc_trans_scroll_down(kind)

            if idx >= 0:
                proc = transitions[idx]
                imgui.separator()
                imgui.text_colored(f"Selected: {proc.display_name}", *_COL_YELLOW)
                fixes_preview = " ".join(proc.waypoints[:8])
                if len(proc.waypoints) > 8:
                    fixes_preview += f" +{len(proc.waypoints) - 8}"
                imgui.text_colored(f"  {len(proc.waypoints)} fixes: {fixes_preview}", *_COL_DIM)
                imgui.separator()

                _is_ils = kind == "app" and bool(proc.name) and proc.name[0] in "ILXB"
                if _is_ils:
                    self._ui_draw_ils_advisory(proc, kind, label)
                else:
                    if imgui.button(f"Insert {label} into FMS##{kind}_ins"):
                        self._cmd_proc_activate(kind)
                    imgui.same_line()
                    if imgui.button(f"Show In ADVISE##{kind}_adv"):
                        self._ui_tab = _TAB_ADVISE
                    imgui.same_line()
                    if imgui.button(f"Clear##{kind}_clr"):
                        self._cmd_proc_clear_selected(kind)
                    imgui.same_line()
                    if imgui.button(f"Back##{kind}_back"):
                        self._cmd_proc_back(kind)

        imgui.columns(1)

    def _ui_draw_ils_advisory(self, proc, kind: str, label: str) -> None:
        """Render the ILS/LOC approach setup advisory with per-step action buttons."""
        apt_icao   = getattr(self, "proc_dest_icao", "")
        freq_khz, course_deg = self._lookup_ils_info(proc, apt_icao)
        nav1_freq  = self._ils_read_nav1_freq()
        nav1_crs   = self._ils_read_nav1_course()

        imgui.text_colored("── APPROACH SETUP ──", *_COL_CYAN)

        # Step 1 — NAV1 frequency
        imgui.text_colored("1.", *_COL_DIM)
        imgui.same_line()
        if freq_khz is not None:
            freq_mhz  = freq_khz / 100.0
            tuned     = nav1_freq is not None and nav1_freq == freq_khz
            col       = _COL_GREEN if tuned else _COL_WHITE
            suffix    = "  ✓" if tuned else ""
            imgui.text_colored(f"NAV1  {freq_mhz:.2f} MHz{suffix}", *col)
            if not tuned:
                imgui.same_line()
                if imgui.button("Tune NAV1##app_ils_freq"):
                    self._cmd_tune_nav1(freq_khz)
        else:
            imgui.text_colored("NAV1  frequency unavailable", *_COL_DIM)

        # Step 2 — inbound course
        imgui.text_colored("2.", *_COL_DIM)
        imgui.same_line()
        if course_deg is not None:
            set_ok = (
                nav1_crs is not None
                and abs(((nav1_crs - course_deg) + 180) % 360 - 180) < 2.0
            )
            col    = _COL_GREEN if set_ok else _COL_WHITE
            suffix = "  ✓" if set_ok else ""
            imgui.text_colored(f"CRS   {course_deg:.0f}°{suffix}", *col)
            if not set_ok:
                imgui.same_line()
                if imgui.button("Set CRS##app_ils_crs"):
                    self._cmd_set_nav1_course(course_deg)
        else:
            imgui.text_colored("CRS   course unavailable", *_COL_DIM)

        # Step 3 — insert into FMS
        imgui.text_colored("3.", *_COL_DIM)
        imgui.same_line()
        loaded = getattr(self, "_proc_loaded", {}).get(kind, "")
        inserted = bool(loaded and loaded == proc.display_name)
        col    = _COL_GREEN if inserted else _COL_ORANGE
        suffix = "  ✓" if inserted else ""
        if imgui.button(f"Insert {label} into FMS##{kind}_ins"):
            self._cmd_proc_activate(kind)
        imgui.same_line()
        imgui.text_colored(suffix, *col)

        imgui.separator()
        if imgui.button(f"Show In ADVISE##{kind}_adv"):
            self._ui_tab = _TAB_ADVISE
        imgui.same_line()
        if imgui.button(f"Clear##{kind}_clr"):
            self._cmd_proc_clear_selected(kind)
        imgui.same_line()
        if imgui.button(f"Back##{kind}_back"):
            self._cmd_proc_back(kind)

    # ── CHECK tab ─────────────────────────────────────────────────────────────

    def _ui_draw_check(self):
        issues = getattr(self, "validation_issues", [])

        loaded_fn = self.string_values.get("loaded_filename", "")
        if loaded_fn:
            imgui.text_colored(f"Plan: {loaded_fn}", *_COL_DIM)
        else:
            imgui.text_colored("No plan loaded — load a plan first.", *_COL_GREY)

        imgui.same_line()
        if imgui.button("Re-check##chk"):
            self._run_validation()

        imgui.separator()

        if not issues:
            if loaded_fn:
                imgui.text_colored("\u2713  No issues found.", *_COL_GREEN)
            return

        # Summary line
        n_err  = sum(1 for v in issues if v.severity == SEVERITY_ERROR)
        n_warn = sum(1 for v in issues if v.severity == SEVERITY_WARN)
        n_info = sum(1 for v in issues if v.severity == "INFO")
        parts = []
        if n_err:
            parts.append(f"{n_err} error{'s' if n_err > 1 else ''}")
        if n_warn:
            parts.append(f"{n_warn} warning{'s' if n_warn > 1 else ''}")
        if n_info:
            parts.append(f"{n_info} info")
        summary_col = _COL_RED if n_err else (_COL_YELLOW if n_warn else _COL_DIM)
        imgui.text_colored(", ".join(parts), *summary_col)
        imgui.separator()

        _SEV_COL = {
            SEVERITY_ERROR: _COL_RED,
            SEVERITY_WARN:  _COL_YELLOW,
            "INFO":         _COL_DIM,
        }

        for i, issue in enumerate(issues):
            col = _SEV_COL.get(issue.severity, _COL_WHITE)
            # Badge + message on one line
            imgui.text_colored(f"[{issue.severity}]", *col)
            imgui.same_line()
            imgui.text_colored(f"  {issue.message}", *_COL_WHITE)
            # Suggestion indented below
            if issue.suggestion:
                imgui.text_colored(f"    \u2192 {issue.suggestion}", *_COL_DIM)
            # Jump-to button for entry-specific issues
            if issue.affected_index >= 0:
                if imgui.button(f"Go to #{issue.affected_index + 1}##chk{i}"):
                    count = self._read_fms_entry_count()
                    if 0 <= issue.affected_index < count:
                        self.legs_selected    = issue.affected_index
                        self.legs_window_start = (
                            issue.affected_index // self.LEGS_VISIBLE_ROWS
                        ) * self.LEGS_VISIBLE_ROWS
                        self._ui_tab = _TAB_ROUTE
            if i < len(issues) - 1:
                imgui.separator()

    # ── FUEL tab ──────────────────────────────────────────────────────────────

    def _ui_draw_fuel(self):
        _KG_TO_LB = 2.20462

        fuel_kg  = getattr(self, "fuel_on_board_kg", 0.0)
        flow_kgs = getattr(self, "fuel_flow_kg_s",   0.0)
        fuel_lb  = fuel_kg * _KG_TO_LB

        # ── Fuel on board ──────────────────────────────────────────────────
        imgui.columns(2, "fuel_fob", border=True)
        imgui.text_colored("FUEL ON BOARD", *_COL_YELLOW)
        imgui.next_column()
        if fuel_kg > 0:
            imgui.text_colored(f"{fuel_kg:.0f} kg  /  {fuel_lb:.0f} lb", *_COL_GREEN)
        else:
            imgui.text_colored("-- kg", *_COL_GREY)
        imgui.columns(1)
        imgui.separator()

        # ── Burn rate ──────────────────────────────────────────────────────
        flow_kgh = flow_kgs * 3600.0
        flow_lbh = flow_kgh * _KG_TO_LB

        imgui.columns(2, "fuel_flow", border=True)
        imgui.text_colored("BURN RATE", *_COL_YELLOW)
        imgui.next_column()
        if flow_kgh > 0:
            imgui.text_colored(f"{flow_kgh:.0f} kg/hr  /  {flow_lbh:.0f} lb/hr", *_COL_ORANGE)
        else:
            imgui.text_colored("-- kg/hr  (engines off or no data)", *_COL_GREY)
        imgui.columns(1)
        imgui.separator()

        # ── Endurance ──────────────────────────────────────────────────────
        imgui.columns(2, "fuel_endur", border=True)
        imgui.text_colored("ENDURANCE", *_COL_YELLOW)
        imgui.next_column()
        if fuel_kg > 0 and flow_kgs > 0:
            endur_s   = fuel_kg / flow_kgs
            endur_h   = int(endur_s // 3600)
            endur_m   = int((endur_s % 3600) // 60)
            endur_col = _COL_RED if endur_h < 1 else (_COL_YELLOW if endur_h < 2 else _COL_GREEN)
            imgui.text_colored(f"{endur_h:d}:{endur_m:02d}  (hr:min until exhaustion)", *endur_col)
        elif fuel_kg > 0:
            imgui.text_colored("-- (engines off or no flow data)", *_COL_GREY)
        else:
            imgui.text_colored("-- (no fuel data)", *_COL_GREY)
        imgui.columns(1)
        imgui.separator()

        # ── GS-based ETE to destination ────────────────────────────────────
        self._ui_ensure_nav_drefs()
        gs_kt   = self._nav_getf("gs")
        dst_nm  = self.float_values.get("loaded_distance_nm", 0.0)

        imgui.columns(2, "fuel_ete", border=True)
        imgui.text_colored("PLAN DIST", *_COL_YELLOW)
        imgui.next_column()
        if dst_nm > 0:
            imgui.text_colored(f"{dst_nm:.0f} nm  (loaded plan total)", *_COL_DIM)
        else:
            imgui.text_colored("-- nm  (no plan loaded)", *_COL_GREY)
        imgui.columns(1)

        imgui.columns(2, "fuel_gs_ete", border=True)
        imgui.text_colored("GS / ETE DEST", *_COL_YELLOW)
        imgui.next_column()
        if gs_kt >= 40.0 and dst_nm > 0:
            ete_h  = dst_nm / gs_kt
            ete_hr = int(ete_h)
            ete_m  = int((ete_h - ete_hr) * 60)
            imgui.text_colored(
                f"{gs_kt:.0f} kt   ETE {ete_hr}:{ete_m:02d}  (plan dist / GS, advisory)",
                *_COL_DIM,
            )
        elif gs_kt < 40.0:
            imgui.text_colored(f"{gs_kt:.0f} kt  (on ground)", *_COL_GREY)
        else:
            imgui.text_colored("--", *_COL_GREY)
        imgui.columns(1)
        imgui.separator()

        # ── Fuel at destination (endurance - ETE) ─────────────────────────
        imgui.columns(2, "fuel_dest", border=True)
        imgui.text_colored("FUEL AT DEST", *_COL_YELLOW)
        imgui.next_column()
        if gs_kt >= 40.0 and dst_nm > 0 and flow_kgs > 0:
            ete_h       = dst_nm / gs_kt
            burn_kg     = flow_kgs * ete_h * 3600.0
            remain_kg   = fuel_kg - burn_kg
            remain_lb   = remain_kg * _KG_TO_LB
            dest_col    = _COL_RED if remain_kg < 0 else (_COL_YELLOW if remain_kg < 50 else _COL_GREEN)
            if remain_kg < 0:
                imgui.text_colored("INSUFFICIENT FUEL FOR ROUTE (advisory)", *_COL_RED)
            else:
                imgui.text_colored(
                    f"{remain_kg:.0f} kg  /  {remain_lb:.0f} lb  (advisory only)",
                    *dest_col,
                )
        else:
            imgui.text_colored("-- (need GS, plan distance, and fuel flow)", *_COL_GREY)
        imgui.columns(1)

        imgui.separator()
        imgui.text_colored(
            "Advisory only - based on plan total distance and current GS/burn rate.",
            *_COL_DIM,
        )

    # ── WIND tab ──────────────────────────────────────────────────────────────

    def _ui_draw_wind(self):
        dep_icao  = getattr(self, "proc_dep_icao",  "") or ""
        dest_icao = getattr(self, "proc_dest_icao", "") or ""

        if imgui.button("Fetch Both##windall"):
            self._cmd_wind_refresh()

        imgui.separator()
        self._ui_wind_section(
            label="DEP",
            icao=dep_icao,
            metar=getattr(self, "dep_wind_metar", ""),
            wind_dir=getattr(self, "dep_wind_dir",  None),
            wind_spd=getattr(self, "dep_wind_spd",  None),
            ranking=getattr(self, "dep_runway_ranking", []),
            fetch_cmd=self._wind_refresh_dep,
            id_prefix="dep",
        )

        imgui.separator()
        self._ui_wind_section(
            label="ARR",
            icao=dest_icao,
            metar=getattr(self, "wind_metar", ""),
            wind_dir=getattr(self, "wind_dir",  None),
            wind_spd=getattr(self, "wind_spd",  None),
            ranking=getattr(self, "wind_runway_ranking", []),
            fetch_cmd=self._wind_refresh_arr,
            id_prefix="arr",
        )

    def _ui_wind_section(self, label, icao, metar, wind_dir, wind_spd,
                         ranking, fetch_cmd, id_prefix):
        # Section header
        imgui.text_colored(label, *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {icao or '----'}", *(_COL_CYAN if icao else _COL_GREY))
        imgui.same_line()
        if imgui.button(f"Fetch##{id_prefix}fetch"):
            fetch_cmd()

        # METAR
        if metar:
            imgui.text_colored(metar, *_COL_DIM)
        elif icao:
            imgui.text_colored(f"No METAR for {icao} - press Fetch.", *_COL_GREY)
        else:
            imgui.text_colored("Load a plan to populate airport.", *_COL_GREY)
            return

        # Wind summary
        if wind_spd is None:
            if metar:
                imgui.text_colored("Wind not found in METAR.", *_COL_GREY)
            return
        if wind_spd == 0.0:
            imgui.text_colored("CALM", *_COL_GREEN)
        elif wind_dir is None:
            imgui.text_colored(f"VRB / {wind_spd:.0f} kt", *_COL_YELLOW)
        else:
            imgui.text_colored(f"{wind_dir:.0f}\xb0 / {wind_spd:.0f} kt", *_COL_WHITE)

        # Runway ranking
        if not ranking:
            if wind_dir is None:
                imgui.text_colored("  Variable wind - cannot rank runways.", *_COL_GREY)
            else:
                tip = "DEP" if id_prefix == "dep" else "APP"
                imgui.text_colored(
                    f"  No runways - open {tip} tab and Refresh first.", *_COL_GREY)
        else:
            imgui.columns(4, f"{id_prefix}_hdr", border=False)
            imgui.text_colored("RWY",  *_COL_YELLOW); imgui.next_column()
            imgui.text_colored("HDGW", *_COL_YELLOW); imgui.next_column()
            imgui.text_colored("XW",   *_COL_YELLOW); imgui.next_column()
            imgui.text_colored("",     *_COL_YELLOW)
            imgui.columns(1)

            best_hw = ranking[0][1]
            for rwy, headwind, crosswind in ranking:
                if headwind == best_hw and headwind > 0:
                    hw_col, marker = _COL_GREEN, ">"
                elif headwind < 0:
                    hw_col, marker = _COL_RED, "TW"
                else:
                    hw_col, marker = _COL_WHITE, ""
                imgui.columns(4, f"{id_prefix}_{rwy}", border=False)
                imgui.text_colored(rwy,                   *_COL_CYAN);   imgui.next_column()
                imgui.text_colored(f"{headwind:+.1f} kt", *hw_col);      imgui.next_column()
                imgui.text_colored(f"{crosswind:.1f} kt", *_COL_ORANGE); imgui.next_column()
                imgui.text_colored(marker,                *hw_col)
                imgui.columns(1)

    # ── PREFS tab ─────────────────────────────────────────────────────────────

    _SIMBRIEF_ID_BUF = 64

    def _ui_draw_prefs(self):
        imgui.text_colored("Simbrief pilot ID", *_COL_YELLOW)
        try:
            changed, new_id = imgui.input_text(
                "##simbrief_id", self.simbrief_id, self._SIMBRIEF_ID_BUF,
            )
        except Exception:
            changed, new_id = imgui.input_text("##simbrief_id", self.simbrief_id)
        if changed:
            self.simbrief_id = new_id.strip()
            self._simbrief_error = ""
        imgui.text_colored("Numeric ID or username from simbrief.com", *_COL_DIM)

        # Recommendations are shown in the DEP / ARR / APP tabs with Apply buttons.
