"""LegsMixin — scrollable LEGS list: state management and commands."""

import math

from XPPython3 import xp

_R_NM = 3440.065  # Earth radius in nautical miles


def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return _R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial bearing from point 1 to point 2, degrees magnetic (true here)."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _info_latlon(info):
    """Extract (lat, lon) from an XP FMSEntryInfo object; returns (None, None) if unavailable."""
    if not info:
        return None, None
    lat = getattr(info, "latitude", None) or getattr(info, "lat", None)
    lon = getattr(info, "longitude", None) or getattr(info, "lon", None)
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None, None
    return lat, lon


class LegsMixin:
    """Mixin providing the LEGS scrollable waypoint list."""

    # ── State helpers ──

    def _legs_fms_index_for_row(self, row: int) -> int:
        """Convert visible row (1-3) to 0-based FMS entry index. Returns -1 if out of range."""
        idx = self.legs_window_start + (row - 1)
        count = self._read_fms_entry_count()
        if idx < 0 or idx >= count:
            return -1
        return idx

    def _legs_ensure_visible(self):
        """Adjust window_start so legs_selected is visible in the 3-row window."""
        count = self._read_fms_entry_count()
        if count <= 0:
            self.legs_selected = -1
            self.legs_window_start = 0
            return
        if self.legs_selected >= 0:
            self.legs_selected = max(0, min(self.legs_selected, count - 1))
        if self.legs_selected >= 0 and self.legs_selected < self.legs_window_start:
            self.legs_window_start = self.legs_selected
        elif self.legs_selected >= 0 and self.legs_selected >= self.legs_window_start + self.LEGS_VISIBLE_ROWS:
            self.legs_window_start = self.legs_selected - self.LEGS_VISIBLE_ROWS + 1
        max_start = max(0, count - 1)
        self.legs_window_start = max(0, min(self.legs_window_start, max_start))

    def _legs_init_after_load(self):
        """Set LEGS to page containing active leg after a plan load."""
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                self.legs_selected = 0
                self.legs_window_start = 0
                return
            active = xp.getDestinationFMSEntry()
            active = max(0, min(active, count - 1))
            self.legs_window_start = (active // self.LEGS_VISIBLE_ROWS) * self.LEGS_VISIBLE_ROWS
            max_start = max(0, count - 1)
            self.legs_window_start = max(0, min(self.legs_window_start, max_start))
            self.legs_selected = active
            self._log("legs_init_after_load: selected=", self.legs_selected,
                      "window=", self.legs_window_start, "count=", count)
        except Exception as exc:
            self._log("legs_init_after_load error:", exc)
            self.legs_selected = -1
            self.legs_window_start = 0

    # ── Dataref readers (used by UI) ──

    def _legs_read_selected_index(self) -> int:
        count = self._read_fms_entry_count()
        if count <= 0 or self.legs_selected < 0 or self.legs_selected >= count:
            return 0
        return self.legs_selected + 1  # 1-based for display

    def _legs_read_active_index(self) -> int:
        return self._read_fms_active_index()  # already 1-based

    def _legs_read_entry_count(self) -> int:
        return self._read_fms_entry_count()

    def _legs_read_window_start(self) -> int:
        return self.legs_window_start + 1  # 1-based for display

    def _legs_read_page_indicator(self) -> str:
        """Return current/total pages, e.g. 3/7."""
        count = self._read_fms_entry_count()
        if count <= 0:
            return "0/0"
        page = self.legs_window_start // self.LEGS_VISIBLE_ROWS + 1
        total = (count + self.LEGS_VISIBLE_ROWS - 1) // self.LEGS_VISIBLE_ROWS
        return f"{page}/{total}"

    def _legs_read_selected_over_count(self) -> str:
        """Return selected row in current page over visible rows, e.g. 1/3."""
        count = self._read_fms_entry_count()
        if count <= 0:
            return "0/0"
        if self.legs_selected < 0:
            return f"-/{self.LEGS_VISIBLE_ROWS}"
        row = self.legs_selected - self.legs_window_start + 1
        if row < 1 or row > self.LEGS_VISIBLE_ROWS:
            return f"-/{self.LEGS_VISIBLE_ROWS}"
        return f"{row}/{self.LEGS_VISIBLE_ROWS}"

    def _legs_read_row_index(self, row: int) -> str:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return ""
        return str(idx + 1)  # 1-based display

    def _legs_format_ident(self, info) -> str:
        """Return waypoint ident; for lat/lon entries with empty navAidID, format coords."""
        if not info:
            return ""
        ident = (info.navAidID or "").strip()
        if ident:
            return ident
        lat = getattr(info, "latitude", None) or getattr(info, "lat", None)
        lon = getattr(info, "longitude", None) or getattr(info, "lon", None)
        if lat is not None and lon is not None:
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            return f"{ns}{abs(lat):.1f}{ew}{abs(lon):.1f}"
        return "?"

    def _legs_read_row_ident(self, row: int) -> str:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return ""
        info = self._safe_fms_entry_info(idx)
        return self._legs_format_ident(info)

    def _legs_read_row_alt(self, row: int) -> str:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return ""
        info = self._safe_fms_entry_info(idx)
        if not info or info.altitude <= 0:
            return ""
        return str(info.altitude)

    def _legs_leg_distance_nm(self, row: int) -> float:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return -1.0

        # G1000-style behavior: the active leg distance is live distance-to-waypoint.
        try:
            if idx == xp.getDestinationFMSEntry():
                nav_drefs = getattr(self, "_nav_drefs", None) or {}
                ref = nav_drefs.get("dis")
                if ref:
                    dis = xp.getDataf(ref)
                    if dis >= 0:
                        return float(dis)
        except Exception:
            pass

        # Non-active rows keep the leg-to-leg plan distance.
        if idx <= 0:
            return -1.0
        cur  = self._safe_fms_entry_info(idx)
        prev = self._safe_fms_entry_info(idx - 1)
        lat2, lon2 = _info_latlon(cur)
        lat1, lon1 = _info_latlon(prev)
        if None in (lat1, lon1, lat2, lon2):
            return -1.0
        return _haversine_nm(lat1, lon1, lat2, lon2)

    def _legs_leg_dtk(self, row: int) -> float:
        """Return desired track (degrees) from the previous FMS entry to this row's entry.
        Returns -1.0 if either entry is missing or has unresolved coordinates."""
        idx = self._legs_fms_index_for_row(row)
        if idx <= 0:
            return -1.0
        cur  = self._safe_fms_entry_info(idx)
        prev = self._safe_fms_entry_info(idx - 1)
        lat2, lon2 = _info_latlon(cur)
        lat1, lon1 = _info_latlon(prev)
        if None in (lat1, lon1, lat2, lon2):
            return -1.0
        return _bearing_deg(lat1, lon1, lat2, lon2)

    def _legs_read_row_is_active(self, row: int) -> int:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return 0
        try:
            return 1 if idx == xp.getDestinationFMSEntry() else 0
        except Exception:
            return 0

    def _legs_read_row_is_selected(self, row: int) -> int:
        idx = self._legs_fms_index_for_row(row)
        if idx < 0:
            return 0
        return 1 if idx == self.legs_selected else 0

    def _legs_read_row_status(self, row: int) -> str:
        is_act = self._legs_read_row_is_active(row)
        is_sel = self._legs_read_row_is_selected(row)
        if is_act and is_sel:
            return "A+S"
        elif is_act:
            return "ACT"
        elif is_sel:
            return "SEL"
        return ""

    # ── Commands ──

    def _cmd_legs_scroll_up(self):
        """Previous page (1-3, 4-6, 7-9, ...). Moves window by 3 waypoints."""
        count = self._read_fms_entry_count()
        if count <= 0:
            return
        new_start = max(0, self.legs_window_start - self.LEGS_VISIBLE_ROWS)
        if new_start != self.legs_window_start:
            self.legs_window_start = new_start
            self.legs_selected = -1
            self._log("legs_scroll_up: page", self.legs_window_start // self.LEGS_VISIBLE_ROWS + 1)
        else:
            self._log("legs_scroll_up: already at first page")

    def _cmd_legs_scroll_down(self):
        """Next page (1-3, 4-6, 7-9, ...). Partial last page OK."""
        count = self._read_fms_entry_count()
        if count <= 0:
            return
        max_w = ((count - 1) // self.LEGS_VISIBLE_ROWS) * self.LEGS_VISIBLE_ROWS
        next_start = self.legs_window_start + self.LEGS_VISIBLE_ROWS
        new_start = min(next_start, max_w)
        if new_start != self.legs_window_start:
            self.legs_window_start = new_start
            self.legs_selected = -1
            self._log("legs_scroll_down: page", self.legs_window_start // self.LEGS_VISIBLE_ROWS + 1)
        else:
            self._log("legs_scroll_down: already at last page")

    def _cmd_legs_previous(self):
        """Select previous waypoint within current visible 3-row page only."""
        count = self._read_fms_entry_count()
        if count <= 0:
            self.legs_selected = -1
            return
        start = self.legs_window_start
        end = min(start + self.LEGS_VISIBLE_ROWS, count)
        if end <= start:
            self.legs_selected = -1
            return
        if self.legs_selected < start or self.legs_selected >= end:
            self.legs_selected = start
        else:
            self.legs_selected = max(start, self.legs_selected - 1)

    def _cmd_legs_next(self):
        """Select next waypoint within current visible 3-row page only."""
        count = self._read_fms_entry_count()
        if count <= 0:
            self.legs_selected = -1
            return
        start = self.legs_window_start
        end = min(start + self.LEGS_VISIBLE_ROWS, count)
        if end <= start:
            self.legs_selected = -1
            return
        if self.legs_selected < start or self.legs_selected >= end:
            self.legs_selected = start
        else:
            self.legs_selected = min(end - 1, self.legs_selected + 1)

    def _cmd_legs_activate(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0 or self.legs_selected < 0:
                return
            target = max(0, min(self.legs_selected, count - 1))
            xp.setDestinationFMSEntry(target)
            info = self._safe_fms_entry_info(target)
            ident = info.navAidID if info else "?"
            self._log("legs_activate:", target, ident)
        except Exception as exc:
            self._log("legs_activate error:", exc)

    def _cmd_legs_direct_to(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0 or self.legs_selected < 0:
                return
            target = max(0, min(self.legs_selected, count - 1))
            fp = getattr(xp, "FMSFlightPlan_Active", getattr(xp, "ActiveFlightPlan", 0))
            if hasattr(xp, "setDirectToFMSFlightPlanEntry"):
                xp.setDirectToFMSFlightPlanEntry(fp, target)
            else:
                self._log("legs_direct_to: setDirectToFMSFlightPlanEntry not available (XP12 only)")
                return
            info = self._safe_fms_entry_info(target)
            ident = info.navAidID if info else "?"
            self._log("legs_direct_to:", target, ident)
        except Exception as exc:
            self._log("legs_direct_to error:", exc)

    def _cmd_legs_select_row_1(self):
        self._cmd_legs_select_row(1)

    def _cmd_legs_select_row_2(self):
        self._cmd_legs_select_row(2)

    def _cmd_legs_select_row_3(self):
        self._cmd_legs_select_row(3)

    def _cmd_legs_select_row(self, row: int):
        """Toggle selection: select the waypoint in row (1-3), or unselect if already selected."""
        idx = self._legs_fms_index_for_row(row)
        if idx >= 0:
            if idx == self.legs_selected:
                self.legs_selected = -1
                self._log("legs_select_row:", row, "-> unselected")
            else:
                self.legs_selected = idx
                self._log("legs_select_row:", row, "-> index", idx)

    def _cmd_legs_clear_selected(self):
        """Clear the selected LEGS waypoint from the route."""
        try:
            count = xp.countFMSEntries()
            if count <= 0 or self.legs_selected < 0:
                return
            target = max(0, min(self.legs_selected, count - 1))
            info = self._safe_fms_entry_info(target)
            ident = info.navAidID if info else "?"
            xp.clearFMSEntry(target)
            new_count = count - 1
            if new_count <= 0:
                self.legs_selected = -1
                self.legs_window_start = 0
            else:
                self.legs_selected = max(0, min(self.legs_selected, new_count - 1))
                max_start = max(0, new_count - 1)
                self.legs_window_start = max(0, min(self.legs_window_start, max_start))
            self._log("legs_clear_selected: cleared", target, ident)
            if hasattr(self, "_sync_route_state"):
                self._sync_route_state()
            if hasattr(self, "_save_state"):
                self._save_state()
        except Exception as exc:
            self._log("legs_clear_selected error:", exc)

    def _cmd_legs_clear_from_here(self):
        """Clear from selected waypoint to end of route."""
        try:
            count = xp.countFMSEntries()
            if count <= 0 or self.legs_selected < 0:
                return
            target = max(0, min(self.legs_selected, count - 1))
            for i in range(count - 1, target - 1, -1):
                xp.clearFMSEntry(i)
            new_count = target
            if new_count <= 0:
                self.legs_selected = -1
                self.legs_window_start = 0
            else:
                self.legs_selected = min(self.legs_selected, new_count - 1)
                max_start = max(0, new_count - 1)
                self.legs_window_start = max(0, min(self.legs_window_start, max_start))
            self._log("legs_clear_from_here: cleared from", target, "count was", count)
            if hasattr(self, "_sync_route_state"):
                self._sync_route_state()
            if hasattr(self, "_save_state"):
                self._save_state()
        except Exception as exc:
            self._log("legs_clear_from_here error:", exc)

    def _cmd_legs_clear_all(self):
        """Clear entire FMS route."""
        try:
            self._clear_fms()
            self.legs_selected = -1
            self.legs_window_start = 0
            self._log("legs_clear_all")
            if hasattr(self, "_sync_route_state"):
                self._sync_route_state()
            if hasattr(self, "_save_state"):
                self._save_state()
        except Exception as exc:
            self._log("legs_clear_all error:", exc)

    def _cmd_legs_direct_to_destination(self):
        """Direct-to the last FMS entry (destination)."""
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            dest_idx = count - 1
            fp = getattr(xp, "FMSFlightPlan_Active", getattr(xp, "ActiveFlightPlan", 0))
            if hasattr(xp, "setDirectToFMSFlightPlanEntry"):
                xp.setDirectToFMSFlightPlanEntry(fp, dest_idx)
            else:
                xp.setDestinationFMSEntry(dest_idx)
            info = self._safe_fms_entry_info(dest_idx)
            ident = info.navAidID if info else "?"
            self._log("direct_to_destination:", dest_idx, ident)
        except Exception as exc:
            self._log("direct_to_destination error:", exc)
