"""FmsIOMixin — FMS file parsing, loading into X-Plane FMS, and plan navigation commands."""

import math
import os
import time
from typing import List, Optional

from XPPython3 import xp

from fmscompanion.models import FlightPlanEntry, FlightPlanInfo


class FmsIOMixin:
    """Mixin providing FMS file I/O: plan discovery, parsing, loading, and navigation commands."""

    # ── Plan directory ──

    def _plans_dir(self) -> str:
        system_path = xp.getSystemPath()
        return os.path.join(system_path, "Output", "FMS plans")

    # ── Plan list refresh ──

    def _refresh_plan_list(self):
        t_total_0 = time.perf_counter()
        plans_dir = self._plans_dir()
        self._log("Refreshing plans from", plans_dir)

        selected_filename = (
            self.plans[self.index].filename
            if 0 <= self.index < len(self.plans) else None
        )

        self.plans = []
        self.loaded = 0
        self._invalidate_list_cache()

        if not os.path.isdir(plans_dir):
            self.browser_list_window_start = 0
            self.index = -1
            self._set_status("NO DIR", f"Missing folder: {plans_dir}")
            self._invalidate_list_cache()
            self._publish_state()
            return

        t_scan_0 = time.perf_counter()
        filenames = []
        snapshot_rows = []
        with os.scandir(plans_dir) as it:
            for entry in it:
                if not entry.name.lower().endswith(".fms") or not entry.is_file(follow_symlinks=False):
                    continue
                filenames.append(entry.name)
                try:
                    st = entry.stat(follow_symlinks=False)
                    snapshot_rows.append((entry.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        current_keys = set(snapshot_rows)
        self._entry_parse_cache = {
            k: v for k, v in self._entry_parse_cache.items() if k in current_keys
        }
        t_scan_ms = (time.perf_counter() - t_scan_0) * 1000.0

        t_parse_0 = time.perf_counter()
        for filename in filenames:
            full_path = os.path.join(plans_dir, filename)
            info = self._parse_fms_file(full_path)
            if info is not None:
                self.plans.append(info)
        t_parse_ms = (time.perf_counter() - t_parse_0) * 1000.0

        t_sort_0 = time.perf_counter()
        self.plans.sort(key=lambda p: (-p.file_timestamp, p.filename.lower()))
        self.plans = self.plans[:self.PLAN_LIST_MAX_PLANS]
        t_sort_ms = (time.perf_counter() - t_sort_0) * 1000.0

        if not self.plans:
            self.index = -1
            self.browser_list_window_start = 0
            self._set_status("EMPTY")
        else:
            if selected_filename is not None:
                self.index = next(
                    (i for i, p in enumerate(self.plans) if p.filename == selected_filename), -1
                )
            elif self.index >= len(self.plans):
                self.index = -1
            if self.index >= 0:
                self._plan_list_ensure_index_visible()
            else:
                self.browser_list_window_start = self._plan_list_align_window_start(
                    self.browser_list_window_start, len(self.plans))
            self._set_status("READY")
        self._invalidate_list_cache()
        t_publish_0 = time.perf_counter()
        self._publish_state()
        t_publish_ms = (time.perf_counter() - t_publish_0) * 1000.0
        self._perf_log(
            f"refresh_total_ms={(time.perf_counter() - t_total_0) * 1000.0:.2f}",
            f"scan_ms={t_scan_ms:.2f}",
            f"parse_ms={t_parse_ms:.2f}",
            f"sort_ms={t_sort_ms:.2f}",
            f"publish_ms={t_publish_ms:.2f}",
            f"files={len(filenames)}",
            f"plans={len(self.plans)}",
        )

    # ── FMS file parsing ──

    @staticmethod
    def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R_NM = 3440.065
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.sin(dlon / 2) ** 2)
        return R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _parse_fms_file(self, path: str) -> Optional[FlightPlanInfo]:
        dep = "----"
        dest = "----"
        cycle = ""
        dep_runway = ""
        dest_runway = ""
        sid = ""
        star = ""
        filename = os.path.basename(path)

        stat = None
        try:
            stat = os.stat(path)
            file_mtime = float(stat.st_mtime)
            file_timestamp = float(getattr(stat, "st_birthtime", stat.st_mtime))
        except OSError:
            file_mtime = 0.0
            file_timestamp = 0.0

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [line.strip() for line in f.readlines()]
        except Exception as exc:
            self._log("Skipping unreadable file", filename, exc)
            return None

        for line in lines[:20]:
            if line.startswith("CYCLE "):
                cycle = line.split(" ", 1)[1].strip()
            elif line.startswith("ADEP "):
                dep = line.split(" ", 1)[1].strip()
            elif line.startswith("ADES "):
                dest = line.split(" ", 1)[1].strip()
            elif line.startswith("DEPRWY RW"):
                dep_runway = line.split("RW", 1)[1].strip()
            elif line.startswith("DESRWY RW"):
                dest_runway = line.split("RW", 1)[1].strip()
            elif line.startswith("SID "):
                sid = line.split(" ", 1)[1].strip()
            elif line.startswith("STAR "):
                star = line.split(" ", 1)[1].strip()

        if stat is not None:
            cache_key = (
                filename,
                getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
                stat.st_size,
            )
        else:
            cache_key = None

        if cache_key is not None and cache_key in self._entry_parse_cache:
            entries = self._entry_parse_cache[cache_key]
        else:
            entries = self._parse_fms_entries(path)
            if cache_key is not None:
                self._entry_parse_cache[cache_key] = entries

        waypoint_count = len(entries)
        idents = [e.ident for e in entries]
        max_altitude = max((e.altitude for e in entries), default=0)

        total_distance = 0.0
        for i in range(1, len(entries)):
            total_distance += self._haversine_nm(
                entries[i - 1].lat, entries[i - 1].lon,
                entries[i].lat, entries[i].lon,
            )

        stem = os.path.splitext(filename)[0]
        if dep != "----" and dest != "----":
            display_name = f"{dep} {dest}"
        else:
            display_name = stem

        return FlightPlanInfo(
            filename=filename,
            full_path=path,
            display_name=display_name,
            dep=dep,
            dest=dest,
            cycle=cycle,
            dep_runway=dep_runway,
            dest_runway=dest_runway,
            sid=sid,
            star=star,
            waypoint_count=waypoint_count,
            total_distance_nm=round(total_distance, 1),
            waypoint_list=",".join(idents),
            max_altitude=max_altitude,
            file_timestamp=file_timestamp,
            file_mtime=file_mtime,
        )

    def _parse_fms_entries(self, path: str) -> List[FlightPlanEntry]:
        entries: List[FlightPlanEntry] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith(("I", "A", "CYCLE", "NUMENR", "DEPRWY", "DESRWY", "SID", "STAR")):
                    continue
                parts = line.split()
                if len(parts) < 6:
                    continue
                try:
                    entry_type = int(parts[0])
                    ident = parts[1]
                    altitude = int(float(parts[3]))
                    lat = float(parts[4])
                    lon = float(parts[5])
                except (TypeError, ValueError):
                    continue
                entries.append(
                    FlightPlanEntry(
                        entry_type=entry_type,
                        ident=ident,
                        altitude=altitude,
                        lat=lat,
                        lon=lon,
                    )
                )
        return entries

    def _get_cached_entries(self, plan: FlightPlanInfo) -> List[FlightPlanEntry]:
        """Return parsed FMS entries for plan, using the entry cache when possible."""
        try:
            stat = os.stat(plan.full_path)
            cache_key = (
                plan.filename,
                getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
                stat.st_size,
            )
        except OSError:
            cache_key = None
        if cache_key is not None and cache_key in self._entry_parse_cache:
            return self._entry_parse_cache[cache_key]
        entries = self._parse_fms_entries(plan.full_path)
        if cache_key is not None:
            self._entry_parse_cache[cache_key] = entries
        return entries

    # ── XP FMS write helpers ──

    def _clear_fms(self):
        count = xp.countFMSEntries()
        for index in range(count - 1, -1, -1):
            xp.clearFMSEntry(index)

    def _load_entry_into_fms(self, index: int, entry: FlightPlanEntry):
        nav_type = self.FMS_TYPE_TO_NAV.get(entry.entry_type)
        if nav_type == xp.Nav_LatLon:
            xp.setFMSEntryLatLon(index, entry.lat, entry.lon, entry.altitude)
            return

        nav_ref = xp.NAV_NOT_FOUND
        if nav_type is not None:
            nav_ref = xp.findNavAid(None, entry.ident, None, None, None, nav_type)

        if nav_ref != xp.NAV_NOT_FOUND:
            xp.setFMSEntryInfo(index, nav_ref, entry.altitude)
            return

        self._log("FMS nav lookup fallback", entry.ident, entry.entry_type, entry.lat, entry.lon)
        xp.setFMSEntryLatLon(index, entry.lat, entry.lon, entry.altitude)

    # ── Plan navigation commands ──

    def _cmd_previous(self):
        """Select previous plan within current visible page only."""
        if not self.plans:
            self._set_status("EMPTY")
            self._invalidate_list_cache()
            self._publish_state()
            return

        start = self.browser_list_window_start
        end = min(start + self.PLAN_LIST_VISIBLE_ROWS, len(self.plans))
        if end <= start:
            self._set_status("READY")
            self._invalidate_list_cache()
            self._publish_state()
            return

        if self.index < start or self.index >= end:
            self.index = end - 1
        else:
            self.index = max(start, self.index - 1)

        self._set_status("READY")
        self._invalidate_list_cache()
        self._publish_state()

    def _cmd_next(self):
        """Select next plan within current visible page only."""
        if not self.plans:
            self._set_status("EMPTY")
            self._invalidate_list_cache()
            self._publish_state()
            return

        start = self.browser_list_window_start
        end = min(start + self.PLAN_LIST_VISIBLE_ROWS, len(self.plans))
        if end <= start:
            self._set_status("READY")
            self._invalidate_list_cache()
            self._publish_state()
            return

        if self.index < start or self.index >= end:
            self.index = start
        else:
            self.index = min(end - 1, self.index + 1)

        self._set_status("READY")
        self._invalidate_list_cache()
        self._publish_state()

    def _cmd_refresh(self):
        self._refresh_plan_list()

    def _cmd_load(self):
        plan = self._selected_plan()
        if plan is None:
            if hasattr(self, "_mark_route_unloaded"):
                self._mark_route_unloaded()
            else:
                self.loaded = 0
            if self.plans:
                self._set_status("SELECT", "Tap a row or turn E1, then LOAD")
            else:
                self._set_status("EMPTY")
            self._publish_state()
            return

        try:
            entries = self._get_cached_entries(plan)
            if not entries:
                if hasattr(self, "_mark_route_unloaded"):
                    self._mark_route_unloaded()
                else:
                    self.loaded = 0
                self._set_status("LOAD FAIL", "No loadable FMS entries found")
                self._publish_state()
                return

            self._clear_fms()
            for index, entry in enumerate(entries):
                self._load_entry_into_fms(index, entry)

            xp.setDisplayedFMSEntry(0)
            active_idx = 0
            if len(entries) > 1 and entries[0].entry_type == 1:
                active_idx = 1
            xp.setDestinationFMSEntry(min(active_idx, len(entries) - 1))
            self._log("Loaded FMS plan", plan.filename, "entries=", len(entries))
            self.loaded = 1
            self.loaded_filename = os.path.splitext(plan.filename)[0]
            self.loaded_index = self.index + 1
            self.loaded_sid = plan.sid
            self.loaded_star = plan.star
            self.loaded_distance_nm = plan.total_distance_nm
            self._set_status("LOADED")
            self._legs_init_after_load()
            new_dep = (plan.dep or "").strip().upper()
            new_dest = (plan.dest or "").strip().upper()
            if new_dep != self.proc_dep_icao or new_dest != self.proc_dest_icao:
                self.proc_dep_icao = new_dep
                self.proc_dest_icao = new_dest
                self._proc_refresh()
        except Exception as exc:
            if hasattr(self, "_mark_route_unloaded"):
                self._mark_route_unloaded()
            else:
                self.loaded = 0
            self._set_status("LOAD ERR", str(exc))

        self._publish_state()

    def _cmd_open_fpl(self):
        cmd = xp.findCommand("sim/GPS/g1000n1_fpl")
        if cmd:
            self._log("Executing sim/GPS/g1000n1_fpl")
            xp.commandOnce(cmd)
            self._set_status("FPL OPEN")
        else:
            self._set_status("NO FPL CMD", "sim/GPS/g1000n1_fpl not found")
        self._publish_state()
