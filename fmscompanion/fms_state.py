"""FmsStateMixin — live FMS state reads from the X-Plane SDK, plus waypoint and map commands."""

from XPPython3 import xp


class FmsStateMixin:
    """Mixin providing live FMS state reads and waypoint/map range commands."""

    # ── Live FMS state readers ──

    def _safe_fms_entry_info(self, index: int):
        try:
            count = xp.countFMSEntries()
            if count <= 0 or index < 0 or index >= count:
                return None
            return xp.getFMSEntryInfo(index)
        except Exception:
            return None

    def _read_fms_entry_count(self) -> int:
        try:
            return xp.countFMSEntries()
        except Exception:
            return 0

    def _read_fms_active_index(self) -> int:
        try:
            return xp.getDestinationFMSEntry() + 1
        except Exception:
            return 0

    def _read_fms_active_ident(self) -> str:
        info = self._safe_fms_entry_info(xp.getDestinationFMSEntry())
        return info.navAidID if info else "----"

    def _read_fms_active_altitude(self) -> int:
        info = self._safe_fms_entry_info(xp.getDestinationFMSEntry())
        return info.altitude if info else 0

    def _read_fms_displayed_index(self) -> int:
        try:
            return xp.getDisplayedFMSEntry() + 1
        except Exception:
            return 0

    def _read_fms_displayed_ident(self) -> str:
        info = self._safe_fms_entry_info(xp.getDisplayedFMSEntry())
        return info.navAidID if info else "----"

    def _read_fms_displayed_altitude(self) -> int:
        info = self._safe_fms_entry_info(xp.getDisplayedFMSEntry())
        return info.altitude if info else 0

    def _read_fms_first_ident(self) -> str:
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return "----"
            info = self._safe_fms_entry_info(0)
            return info.navAidID if info else "----"
        except Exception:
            return "----"

    def _read_fms_first_altitude(self) -> int:
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return 0
            info = self._safe_fms_entry_info(0)
            return info.altitude if info else 0
        except Exception:
            return 0

    def _read_fms_last_ident(self) -> str:
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return "----"
            info = self._safe_fms_entry_info(count - 1)
            return info.navAidID if info else "----"
        except Exception:
            return "----"

    def _read_fms_last_altitude(self) -> int:
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return 0
            info = self._safe_fms_entry_info(count - 1)
            return info.altitude if info else 0
        except Exception:
            return 0

    # ── Waypoint navigation commands ──

    def _cmd_wp_next(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            current = xp.getDisplayedFMSEntry()
            next_idx = (current + 1) % count
            xp.setDisplayedFMSEntry(next_idx)
            self._log("wp_next: displayed", next_idx)
        except Exception as exc:
            self._log("wp_next error:", exc)

    def _cmd_wp_previous(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            current = xp.getDisplayedFMSEntry()
            prev_idx = (current - 1) % count
            xp.setDisplayedFMSEntry(prev_idx)
            self._log("wp_previous: displayed", prev_idx)
        except Exception as exc:
            self._log("wp_previous error:", exc)

    def _cmd_wp_activate(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            displayed = xp.getDisplayedFMSEntry()
            xp.setDestinationFMSEntry(displayed)
            info = self._safe_fms_entry_info(displayed)
            ident = info.navAidID if info else "?"
            self._log("wp_activate: destination set to", displayed, ident)
        except Exception as exc:
            self._log("wp_activate error:", exc)

    def _cmd_wp_direct(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            displayed = xp.getDisplayedFMSEntry()
            fp = getattr(xp, "FMSFlightPlan_Active", getattr(xp, "ActiveFlightPlan", 0))
            if hasattr(xp, "setDirectToFMSFlightPlanEntry"):
                xp.setDirectToFMSFlightPlanEntry(fp, displayed)
            else:
                self._log("wp_direct: setDirectToFMSFlightPlanEntry not available (XP12 only)")
                return
            info = self._safe_fms_entry_info(displayed)
            ident = info.navAidID if info else "?"
            self._log("wp_direct: direct-to set to", displayed, ident)
        except Exception as exc:
            self._log("wp_direct error:", exc)

    def _cmd_clear_fms_entry(self):
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            displayed = xp.getDisplayedFMSEntry()
            xp.clearFMSEntry(displayed)
            info = self._safe_fms_entry_info(displayed)
            ident = info.navAidID if info else "?"
            self._log("clear_fms_entry: cleared", displayed, "now showing", ident)
        except Exception as exc:
            self._log("clear_fms_entry error:", exc)

    # ── Map range commands ──

    def _cmd_map_range_down(self):
        refs = self.map_cmd_refs.get(self.map_mode)
        if refs and refs[0]:
            xp.commandOnce(refs[0])
            self._log("map_range_down", self.map_mode_names[self.map_mode])

    def _cmd_map_range_up(self):
        refs = self.map_cmd_refs.get(self.map_mode)
        if refs and refs[1]:
            xp.commandOnce(refs[1])
            self._log("map_range_up", self.map_mode_names[self.map_mode])

    def _cmd_map_toggle(self):
        self.map_mode = 1 - self.map_mode
        self.string_values["map_mode"] = self.map_mode_names[self.map_mode]
        self._log("map_toggle ->", self.map_mode_names[self.map_mode])
