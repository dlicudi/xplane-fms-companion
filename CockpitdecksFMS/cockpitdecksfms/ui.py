"""UIMixin — Dear ImGui status/debug window for CockpitdecksFMS.

Requires xp_imgui (XPPython3 imgui wrapper) to be installed; gracefully
disabled if not available.

Tabs mirror the Loupedeck Live page layout:
    LOAD     — .fms file browser
    NAV      — 4×3 live data grid (matches fms_nav.yaml exactly)
    ROUTE    — active FMS legs list
    DEP      — SID procedure name browser
    DEP TR   — SID transition browser for selected procedure
    ARR      — STAR procedure name browser
    ARR TR   — STAR transition browser
    APP      — approach procedure name browser
    APP TR   — approach transition browser

Registers a single X-Plane command:
    cockpitdecks/fms/toggle_window  — show/hide the window
"""

try:
    import xp_imgui
    import imgui

    _HAS_IMGUI = True
except ImportError:
    _HAS_IMGUI = False

from XPPython3 import xp

_TAB_LOAD    = 0
_TAB_NAV     = 1
_TAB_ROUTE   = 2
_TAB_DEP     = 3
_TAB_DEP_TR  = 4
_TAB_ARR     = 5
_TAB_ARR_TR  = 6
_TAB_APP     = 7
_TAB_APP_TR  = 8

_TAB_LABELS = ["LOAD", "NAV", "ROUTE", "DEP", "DEP TR", "ARR", "ARR TR", "APP", "APP TR"]

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
    """Mixin providing a Dear ImGui status/debug window for CockpitdecksFMS."""

    def _ui_init(self):
        self._ui_window = None
        self._ui_tab = _TAB_NAV
        self._ui_menu_id = None
        self._nav_drefs = {}   # populated lazily on first NAV draw

    # ── Command + menu ─────────────────────────────────────────────────────────

    def _ui_register_command(self):
        self._create_command(
            "toggle_window",
            "Toggle Cockpitdecks FMS window",
            self._ui_toggle_window,
            prefix="cockpitdecks/fms",
        )

    def _ui_build_menu(self):
        try:
            plugins_menu = xp.findPluginsMenu()
            self._ui_menu_id = xp.createMenu(
                "Cockpitdecks FMS", plugins_menu, 0, self._ui_menu_handler, None)
            xp.appendMenuItem(self._ui_menu_id, "Show / Hide FMS Window", "toggle")
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
            self._ui_window.setTitle("Cockpitdecks FMS")
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

        imgui.separator()

        # Tab bar
        for i, label in enumerate(_TAB_LABELS):
            if i > 0:
                imgui.same_line()
            active = self._ui_tab == i
            if active:
                imgui.push_style_color(imgui.COLOR_BUTTON, *_COL_BLUE)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *_COL_BLUE_HOV)
            if imgui.button(f"{label}##tab{i}"):
                self._ui_tab = i
            if active:
                imgui.pop_style_color(2)

        imgui.separator()

        if self._ui_tab == _TAB_LOAD:
            self._ui_draw_load()
        elif self._ui_tab == _TAB_NAV:
            self._ui_draw_nav()
        elif self._ui_tab == _TAB_ROUTE:
            self._ui_draw_route()
        elif self._ui_tab in _PROC_KIND_FOR_TAB:
            self._ui_draw_proc_names(_PROC_KIND_FOR_TAB[self._ui_tab])
        elif self._ui_tab in _TRANS_KIND_FOR_TAB:
            self._ui_draw_proc_trans(_TRANS_KIND_FOR_TAB[self._ui_tab])

    # ── NAV tab — 4×3 grid matching fms_nav.yaml ──────────────────────────────

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

        # helpers
        def fmtf(key, fmt, suffix=""):
            v = self._nav_getf(key)
            return f"{fmt.format(v)}{suffix}"

        dst_nm = self.float_values.get("loaded_distance_nm", 0.0)

        # ── Row 1: DIS | ETE | GS | DTK ──────────────────────────────────────
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

        # ── Row 2: WPT | ETA | BRG | XTK ─────────────────────────────────────
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

        # ── Row 3: TRK | DEST | DTO | SRC ────────────────────────────────────
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

        imgui.text_colored("SRC", *_COL_YELLOW)
        imgui.text_colored("GPS", *_COL_GREEN)

        imgui.columns(1)

    # ── LOAD tab ───────────────────────────────────────────────────────────────

    def _ui_draw_load(self):
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

        # Header: matches fms_load.yaml columns — FPL | WPTS | MAX ALT | DIS
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
            is_sel   = r.get("is_selected", 0)
            wpts     = r.get("wpt_count", 0)
            max_alt  = r.get("max_alt_ft", 0)
            dist     = int(round(r.get("distance_nm", 0.0)))
            col      = _COL_ORANGE if not is_sel else _COL_WHITE

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

    # ── ROUTE tab (mirrors fms_fpl.yaml) ──────────────────────────────────────

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

        # Columns match fms_fpl.yaml: # | WPT | ALT | (scroll)
        imgui.columns(4, "route_hdr", border=False)
        imgui.text_colored("#", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("WPT", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("ALT", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("STATUS", *_COL_YELLOW)
        imgui.columns(1)
        imgui.separator()

        for row in range(1, self.LEGS_VISIBLE_ROWS + 1):
            idx = self._legs_fms_index_for_row(row)
            if idx < 0:
                imgui.text("")
                continue
            ident    = self._legs_read_row_ident(row) or "---"
            alt      = self._legs_read_row_alt(row)
            is_active = self._legs_read_row_is_active(row)
            is_sel    = self._legs_read_row_is_selected(row)

            if is_active and is_sel:
                col, status_str = _COL_GREEN, "ACT+SEL"
            elif is_active:
                col, status_str = _COL_GREEN, "ACTIVE"
            elif is_sel:
                col, status_str = _COL_YELLOW, "SEL"
            else:
                col, status_str = _COL_WHITE, ""

            imgui.columns(4, f"route_r{row}", border=False)
            imgui.text_colored(str(idx + 1), *_COL_DIM)
            imgui.next_column()
            imgui.push_style_color(imgui.COLOR_TEXT, *col)
            if imgui.button(f"{ident}##{row}"):
                self._cmd_legs_select_row(row)
            imgui.pop_style_color()
            imgui.next_column()
            imgui.text_colored(alt if alt else "--", *_COL_ORANGE)
            imgui.next_column()
            imgui.text_colored(status_str, *col)
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

        # Columns match fms_proc_dep.yaml: # | SID | RWY | UP/SEL/DN
        imgui.columns(4, f"{kind}_hdr", border=False)
        imgui.text_colored("#", *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored(label, *_COL_YELLOW)
        imgui.next_column()
        imgui.text_colored("", *_COL_YELLOW)   # no RWY at name level
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
        label_map = {"dep": "SID", "arr": "STAR", "app": "APP"}
        label     = label_map[kind]
        sel_name  = self._proc_selected_proc_name(kind)
        loaded    = self._proc_loaded.get(kind, "")
        transitions = self._proc_transitions(kind)
        idx       = self._proc_index.get(kind, -1)
        page_str  = self._proc_trans_list_page_str(kind)

        # Breadcrumb: SID > RNAV1  [Back]
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

        # Columns: # | NAME | RWY | UP/SEL/DN
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
