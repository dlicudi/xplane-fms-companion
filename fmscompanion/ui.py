"""UIMixin — Dear ImGui plugin window for FMS Companion.

Requires xp_imgui (XPPython3 imgui wrapper); gracefully disabled if not available.

Tabs:
    LOAD     — .fms file browser
    NAV      — live data grid (WPT, XTK, DTK, GS, ETE, BRG, TRK, ETA)
    ROUTE    — active FMS legs list
    DEP      — SID procedure name browser
    DEP TR   — SID transition browser for selected procedure
    ARR      — STAR procedure name browser
    ARR TR   — STAR transition browser
    APP      — approach procedure name browser
    APP TR   — approach transition browser

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

_TAB_LOAD    = 0
_TAB_NAV     = 1
_TAB_ROUTE   = 2
_TAB_DEP     = 3
_TAB_DEP_TR  = 4
_TAB_ARR     = 5
_TAB_ARR_TR  = 6
_TAB_APP     = 7
_TAB_APP_TR  = 8
_TAB_CHECK   = 9
_TAB_FUEL    = 10
_TAB_WIND    = 11

_TAB_LABELS = ["LOAD", "NAV", "ROUTE", "DEP", "DEP TR", "ARR", "ARR TR", "APP", "APP TR", "CHECK", "FUEL", "WIND"]

_PROC_KIND_FOR_TAB  = {_TAB_DEP: "dep", _TAB_ARR: "arr", _TAB_APP: "app"}
_TRANS_KIND_FOR_TAB = {_TAB_DEP_TR: "dep", _TAB_ARR_TR: "arr", _TAB_APP_TR: "app"}
_TRANS_TAB_FOR_KIND = {"dep": _TAB_DEP_TR, "arr": _TAB_ARR_TR, "app": _TAB_APP_TR}

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
    "dis":   "sim/cockpit2/radios/indicators/gps_dme_distance_nm",
    "ete":   "sim/cockpit2/radios/indicators/gps_dme_time_min",
    "gs":    "sim/cockpit2/gauges/indicators/ground_speed_kt",
    "dtk":   "sim/cockpit2/radios/indicators/gps_bearing_deg_mag",
    "eta_h": "sim/cockpit2/radios/indicators/fms1_act_eta_hour",
    "eta_m": "sim/cockpit2/radios/indicators/fms1_act_eta_minute",
    "brg":   "sim/cockpit2/radios/indicators/gps_bearing_deg_mag",
    "xtk":   "sim/cockpit/radios/gps_course_deviation",
    "trk":   "sim/cockpit2/gauges/indicators/ground_track_mag_pilot",
}


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
            self._ui_menu_id = xp.createMenu(
                "FMS Companion", plugins_menu, 0, self._ui_menu_handler, None)
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
        elif self._ui_tab == _TAB_CHECK:
            self._ui_draw_check()
        elif self._ui_tab == _TAB_FUEL:
            self._ui_draw_fuel()
        elif self._ui_tab == _TAB_WIND:
            self._ui_draw_wind()
        elif self._ui_tab in _PROC_KIND_FOR_TAB:
            self._ui_draw_proc_names(_PROC_KIND_FOR_TAB[self._ui_tab])
        elif self._ui_tab in _TRANS_KIND_FOR_TAB:
            self._ui_draw_proc_trans(_TRANS_KIND_FOR_TAB[self._ui_tab])

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

        # Advisory banner — shown only when there are active advisories
        advisories = getattr(self, "nav_advisories", [])
        if advisories:
            imgui.separator()
            for msg in advisories:
                imgui.text_colored(f"\u26a0  {msg}", *_COL_RED)

    # ── LOAD tab ───────────────────────────────────────────────────────────────

    def _ui_draw_load(self):
        self.PLAN_LIST_VISIBLE_ROWS = self._ui_visible_rows(reserved_px=230)
        sv = self.string_values
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
                imgui.text("")
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

    # ── ROUTE tab ─────────────────────────────────────────────────────────────

    def _ui_draw_route(self):
        self.LEGS_VISIBLE_ROWS = self._ui_visible_rows(reserved_px=200)
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
                imgui.text("")
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
        if imgui.button("UP##route"):
            self._cmd_legs_scroll_up()
        imgui.same_line()
        if imgui.button("DN##route"):
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
        if imgui.button("Clear All"):
            self._cmd_legs_clear_all()

    # ── DEP / ARR / APP — procedure name list ─────────────────────────────────

    def _ui_draw_proc_names(self, kind: str):
        self.PROC_VISIBLE_ROWS = self._ui_visible_rows(reserved_px=160)
        label_map = {"dep": "SID", "arr": "STAR", "app": "APP"}
        label    = label_map[kind]
        airport  = self._proc_airport_for(kind)
        names    = self._proc_names.get(kind, [])
        loaded   = self._proc_loaded.get(kind, "")
        sel_name = self._proc_selected_proc_name(kind)
        page_str = self._proc_name_list_page_str(kind)

        imgui.text_colored(label, *_COL_YELLOW)
        imgui.same_line()
        imgui.text_colored(f"  {airport or '----'}", *(_COL_YELLOW if airport else _COL_GREY))
        if loaded:
            imgui.same_line()
            imgui.text_colored(f"  \u2713 {loaded}", *_COL_GREEN)
        imgui.same_line()
        if imgui.button(f"Refresh##{kind}"):
            self._cmd_proc_refresh(kind)

        imgui.separator()

        if not names:
            imgui.text_colored(f"No {label} procedures found", *_COL_GREY)
            imgui.text_colored(f"  DEP: {self.proc_dep_icao or '?'}   DEST: {self.proc_dest_icao or '?'}", *_COL_DIM)
            return

        imgui.columns(4, f"{kind}_hdr", border=False)
        imgui.text_colored("#", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored(label, *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("", *_COL_YELLOW)
        imgui.next_column()
        if page_str:
            imgui.text_colored(page_str, *_COL_DIM)
        imgui.columns(1)
        imgui.separator()

        window_start = self._proc_name_window.get(kind, 0)
        for row in range(1, self.PROC_VISIBLE_ROWS + 1):
            pi = window_start + row - 1
            if pi >= len(names):
                imgui.text("")
                continue
            name   = names[pi]
            is_sel = (name == sel_name and sel_name != "")
            trans_count = sum(1 for p in self._proc_procs.get(kind, []) if p.name == name)
            col    = _COL_YELLOW if is_sel else _COL_WHITE

            imgui.columns(4, f"{kind}_r{row}", border=False)
            imgui.text_colored(str(pi + 1), *_COL_DIM)
            imgui.next_column()
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{'>' if is_sel else ' '} {name}##{kind}_{row}"):
                self._cmd_proc_select_row(kind, row)
                self._ui_tab = _TRANS_TAB_FOR_KIND[kind]
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(f"{trans_count} tr" if trans_count > 1 else "", *_COL_DIM)
            imgui.next_column()
            imgui.columns(1)

        imgui.separator()
        if imgui.button(f"UP##{kind}_np"):
            self._cmd_proc_scroll_up(kind)
        imgui.same_line()
        if imgui.button(f"DN##{kind}_nn"):
            self._cmd_proc_scroll_down(kind)

    # ── DEP TR / ARR TR / APP TR — transition list ────────────────────────────

    def _ui_draw_proc_trans(self, kind: str):
        self.PROC_VISIBLE_ROWS = self._ui_visible_rows(reserved_px=200)
        label_map = {"dep": "SID", "arr": "STAR", "app": "APP"}
        label     = label_map[kind]
        sel_name  = self._proc_selected_proc_name(kind)
        loaded    = self._proc_loaded.get(kind, "")
        transitions = self._proc_transitions(kind)
        idx       = self._proc_index.get(kind, -1)
        page_str  = self._proc_trans_list_page_str(kind)

        imgui.text_colored(label, *_COL_YELLOW)
        if sel_name:
            imgui.same_line()
            imgui.text_colored(f" \u203a {sel_name}", *_COL_YELLOW)
        if loaded:
            imgui.same_line()
            imgui.text_colored(f"  \u2713 {loaded}", *_COL_GREEN)
        imgui.same_line()
        if imgui.button(f"Back##{kind}_back"):
            self._cmd_proc_back(kind)
            self._ui_tab = _PROC_KIND_FOR_TAB.get(
                next((t for t, k in _TRANS_KIND_FOR_TAB.items() if k == kind), _TAB_DEP), _TAB_DEP)

        imgui.separator()

        if not sel_name:
            imgui.text_colored(f"No {label} selected — go to {label} page first", *_COL_GREY)
            return

        if not transitions:
            imgui.text_colored(f"No transitions for {sel_name}", *_COL_GREY)
            return

        imgui.columns(4, f"{kind}tr_hdr", border=False)
        imgui.text_colored("#", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored(label, *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("RWY", *_COL_YELLOW)
        imgui.next_column()
        if page_str:
            imgui.text_colored(page_str, *_COL_DIM)
        imgui.columns(1)
        imgui.separator()

        window_start = self._proc_window.get(kind, 0)
        for row in range(1, self.PROC_VISIBLE_ROWS + 1):
            pi = window_start + row - 1
            if pi >= len(transitions):
                imgui.text("")
                continue
            proc   = transitions[pi]
            is_sel = pi == idx
            col    = _COL_YELLOW if is_sel else _COL_WHITE

            imgui.columns(4, f"{kind}tr_r{row}", border=False)
            imgui.text_colored(str(pi + 1), *_COL_DIM)
            imgui.next_column()
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{'>' if is_sel else ' '} {proc.display_name}##{kind}tr_{row}"):
                self._cmd_proc_select_trans_row(kind, row)
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(proc.display_runway or "", *_COL_ORANGE)
            imgui.next_column()
            imgui.columns(1)

        imgui.separator()
        if imgui.button(f"UP##{kind}tr_up"):
            self._cmd_proc_trans_scroll_up(kind)
        imgui.same_line()
        if imgui.button(f"DN##{kind}tr_dn"):
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
            if imgui.button(f"Insert {label} into FMS##{kind}_ins"):
                self._cmd_proc_activate(kind)
            imgui.same_line()
            if imgui.button(f"Clear##{kind}_clr"):
                self._cmd_proc_clear_selected(kind)

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
            proc_label="Recommended SIDs",
            procs=getattr(self, "dep_recommended_sids", []),
            stars=None,
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
            proc_label="Recommended approaches",
            procs=getattr(self, "arr_recommended_apps", []),
            stars=getattr(self, "arr_recommended_stars", []),
            fetch_cmd=self._wind_refresh_arr,
            id_prefix="arr",
        )

    def _ui_wind_section(self, label, icao, metar, wind_dir, wind_spd,
                         ranking, proc_label, procs, stars, fetch_cmd, id_prefix):
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

        # Procedure recommendations
        if procs:
            imgui.text_colored(proc_label + ":", *_COL_YELLOW)
            for name, rwy in procs:
                imgui.text_colored(f"  {name}", *_COL_GREEN)

        # STARs (arrival only — not runway-specific in CIFP)
        if stars is not None:
            imgui.text_colored("Available STARs:", *_COL_YELLOW)
            if stars:
                for name in stars:
                    imgui.text_colored(f"  {name}", *_COL_WHITE)
            else:
                imgui.text_colored("  None - open ARR tab and Refresh first.", *_COL_GREY)
