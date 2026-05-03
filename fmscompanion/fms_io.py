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

    def _read_plan_text(self, path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    # ── XP FMS write helpers ──

    def _clear_fms(self):
        count = xp.countFMSEntries()
        for index in range(count - 1, -1, -1):
            xp.clearFMSEntry(index)

    def _load_fms_plan_text(self, plan_text: str, source: str = "") -> bool:
        """Load an X-Plane 11+ .fms buffer through the native flight-plan API.

        This lets X-Plane/G1000 resolve airport identifiers from the plan text
        instead of forcing us through findNavAid() + setFMSEntryInfo(), which can
        expose internal X-prefixed airport records such as XEDDB.
        """
        loader = getattr(xp, "loadFMSFlightPlan", None)
        if not loader:
            return False
        try:
            loader(0, plan_text)
            self._log("Loaded FMS plan via loadFMSFlightPlan", source or "<buffer>")
            return True
        except Exception as exc:
            self._log("loadFMSFlightPlan failed", source or "<buffer>", exc)
            return False

    @staticmethod
    def _fms_line(entry: FlightPlanEntry, route_tag: str) -> str:
        ident = (entry.ident or "----").strip().upper()
        tag = (route_tag or "DRCT").strip().upper()
        return (
            f"{entry.entry_type} {ident} {tag} {float(entry.altitude):.6f} "
            f"{entry.lat:.6f} {entry.lon:.6f}"
        )

    def _build_route_entry_fms_text(self, parsed) -> str:
        ok_tokens = [t for t in parsed if getattr(t, "entry", None) is not None]
        if not ok_tokens:
            return ""

        dep = ok_tokens[0].entry.ident if ok_tokens[0].entry.entry_type == 1 else ""
        dest = ok_tokens[-1].entry.ident if ok_tokens[-1].entry.entry_type == 1 else ""
        lines = ["I", "1100 Version"]
        if dep:
            lines.append(f"ADEP {dep}")
        if dest:
            lines.append(f"ADES {dest}")
        lines.append(f"NUMENR {len(ok_tokens)}")

        last_idx = len(ok_tokens) - 1
        for i, token in enumerate(ok_tokens):
            entry = token.entry
            if i == 0 and entry.entry_type == 1:
                route_tag = "ADEP"
            elif i == last_idx and entry.entry_type == 1:
                route_tag = "ADES"
            else:
                msg = (getattr(token, "message", "") or "").strip()
                route_tag = msg[4:].strip().upper() if msg.startswith("via ") else "DRCT"
            lines.append(self._fms_line(entry, route_tag))

        return "\n".join(lines) + "\n"

    def _write_entries_into_fms(self, entries: List[FlightPlanEntry]) -> None:
        self._clear_fms()
        for index, entry in enumerate(entries):
            self._load_entry_into_fms(index, entry)

    def _clear_fms_flight_plan(self, flight_plan) -> None:
        count_fn = getattr(xp, "countFMSFlightPlanEntries", None)
        clear_fn = getattr(xp, "clearFMSFlightPlanEntry", None)
        if not count_fn or not clear_fn:
            return
        count = count_fn(flight_plan)
        for index in range(count - 1, -1, -1):
            clear_fn(flight_plan, index)

    def _write_route_entry_into_flight_plan(self, entries: List[FlightPlanEntry]) -> bool:
        """Write typed routes through the XP12 flight-plan entry API.

        EDDB's airport navref can display as XEDDB. For typed routes, prefer
        named lat/lon entries so the visible G1000 rows use the literal idents
        the user parsed, rather than X-Plane's nav database aliases.
        """
        set_info = getattr(xp, "setFMSFlightPlanEntryInfo", None)
        set_latlon_id = getattr(xp, "setFMSFlightPlanEntryLatLonWithId", None)
        set_latlon = getattr(xp, "setFMSFlightPlanEntryLatLon", None)
        if not set_info or not set_latlon_id or not set_latlon:
            return False

        flight_plan = getattr(xp, "Fpl_Pilot_Primary", 0)

        try:
            self._clear_fms_flight_plan(flight_plan)
            for index, entry in enumerate(entries):
                ident = (entry.ident or "").strip().upper()
                nav_type = self.FMS_TYPE_TO_NAV.get(entry.entry_type)

                # For custom route rows, display fidelity matters more than
                # preserving every navref. The G1000 can resolve/direct-to named
                # lat/lon rows, and this avoids EDDB -> XEDDB.
                if 0 < index < len(entries) - 1:
                    set_latlon_id(flight_plan, index, entry.lat, entry.lon, entry.altitude, ident)
                    continue

                if nav_type == xp.Nav_LatLon:
                    set_latlon_id(flight_plan, index, entry.lat, entry.lon, entry.altitude, ident)
                    continue

                nav_ref = xp.NAV_NOT_FOUND
                if nav_type is not None:
                    nav_ref = xp.findNavAid(None, ident, entry.lat, entry.lon, None, nav_type)

                if nav_ref != xp.NAV_NOT_FOUND and self._nav_matches_position(nav_ref, entry):
                    set_info(flight_plan, index, nav_ref, entry.altitude)
                else:
                    set_latlon_id(flight_plan, index, entry.lat, entry.lon, entry.altitude, ident)

            self._log("Route entry loaded via FMSFlightPlan entry API:", len(entries), "entries")
            return True
        except Exception as exc:
            self._log("FMSFlightPlan entry API failed:", exc)
            return False

    # Max distance between the .fms-file coord and the SDK's resolved navaid.
    # Same idents exist across regions (GRICE: Scotland and Louisiana); catches
    # cross-ocean ambiguity without tripping on routine nav-db coordinate drift.
    _NAV_MATCH_TOLERANCE_NM = 25.0

    def _load_entry_into_fms(self, index: int, entry: FlightPlanEntry):
        nav_type = self.FMS_TYPE_TO_NAV.get(entry.entry_type)
        if nav_type == xp.Nav_LatLon:
            xp.setFMSEntryLatLon(index, entry.lat, entry.lon, entry.altitude)
            return

        nav_ref = xp.NAV_NOT_FOUND

        if nav_type == xp.Nav_Airport:
            # Some airports are stored in the navaid database with an internal
            # "X" prefix (e.g. "XEDDB" for EDDB) due to custom scenery layering.
            # Try Fix/VOR/NDB first — many airports have their ICAO code as a
            # named fix at the threshold, which displays without the X prefix.
            for try_type in (xp.Nav_Fix, xp.Nav_VOR, xp.Nav_NDB):
                candidate = xp.findNavAid(None, entry.ident, entry.lat, entry.lon, None, try_type)
                if candidate == xp.NAV_NOT_FOUND:
                    continue
                try:
                    cinfo = xp.getNavAidInfo(candidate)
                    cid = (getattr(cinfo, "navAidID", "") or "").strip().upper()
                except Exception:
                    continue
                if cid == entry.ident and self._nav_matches_position(candidate, entry):
                    nav_ref = candidate
                    break
            # Fall back to the airport navaid ref (may display as "XEDDB").
            if nav_ref == xp.NAV_NOT_FOUND:
                nav_ref = xp.findNavAid(None, entry.ident, entry.lat, entry.lon, None, nav_type)
        elif nav_type is not None:
            # Pass lat/lon so the SDK returns the NEAREST match of that type —
            # without it, duplicate idents resolve to whichever row comes first.
            nav_ref = xp.findNavAid(None, entry.ident, entry.lat, entry.lon, None, nav_type)

        if nav_ref != xp.NAV_NOT_FOUND and self._nav_matches_position(nav_ref, entry):
            xp.setFMSEntryInfo(index, nav_ref, entry.altitude)
            return

        self._log("FMS nav lookup fallback", entry.ident, entry.entry_type, entry.lat, entry.lon)
        xp.setFMSEntryLatLon(index, entry.lat, entry.lon, entry.altitude)

    def _nav_matches_position(self, nav_ref, entry: FlightPlanEntry) -> bool:
        """Reject a nav match whose position is more than _NAV_MATCH_TOLERANCE_NM
        from the entry's coords — the SDK returned a different navaid than the
        .fms file intended, so fall back to a raw lat/lon entry."""
        try:
            info = xp.getNavAidInfo(nav_ref)
        except Exception:
            return False
        lat = getattr(info, "latitude", None)
        if lat is None:
            lat = getattr(info, "lat", None)
        lon = getattr(info, "longitude", None)
        if lon is None:
            lon = getattr(info, "lon", None)
        if lat is None or lon is None:
            return False
        dist_nm = self._haversine_nm(float(lat), float(lon), entry.lat, entry.lon)
        if dist_nm > self._NAV_MATCH_TOLERANCE_NM:
            self._log(
                f"Nav match too far for {entry.ident}: {dist_nm:.1f} nm "
                f"from .fms coords — using LatLon fallback"
            )
            return False
        return True

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

            loaded_via_plan = False
            try:
                plan_text = self._read_plan_text(plan.full_path)
                loaded_via_plan = self._load_fms_plan_text(plan_text, plan.filename)
            except Exception as exc:
                self._log("Could not read FMS plan text", plan.filename, exc)
            if not loaded_via_plan:
                self._write_entries_into_fms(entries)

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

    # ── Route entry (typed routes) ──────────────────────────────────────────

    def _aircraft_position(self):
        """Return (lat, lon) of the user aircraft, or (None, None) if unavailable."""
        try:
            lat_ref = xp.findDataRef("sim/flightmodel/position/latitude")
            lon_ref = xp.findDataRef("sim/flightmodel/position/longitude")
            if not lat_ref or not lon_ref:
                return (None, None)
            return (float(xp.getDataf(lat_ref)), float(xp.getDataf(lon_ref)))
        except Exception:
            return (None, None)

    def _cmd_simbrief_fetch(self):
        if self._simbrief_fetching or not self.simbrief_id.strip():
            return
        from fmscompanion import simbrief
        self._simbrief_fetching = True
        self._simbrief_result = None

        def _on_done(route, error):
            self._simbrief_result = (route, error)
            self._simbrief_fetching = False

        simbrief.fetch_route(self.simbrief_id, _on_done)

    def _cmd_route_entry_parse(self):
        """Parse self.route_entry_text into self.route_entry_parsed.

        Seeds the chained-position lookup with aircraft position so the first
        middle token resolves to a nearby match instead of whichever same-ident
        fix the nav db yields first.
        """
        import importlib
        from fmscompanion import airway_db, route_parser
        importlib.reload(airway_db)
        importlib.reload(route_parser)
        parse_route_string = route_parser.parse_route_string
        lat, lon = self._aircraft_position()
        self.route_entry_parsed = parse_route_string(self.route_entry_text,
                                                     hint_lat=lat, hint_lon=lon)
        ok    = sum(1 for t in self.route_entry_parsed if t.status == "ok")
        total = sum(1 for t in self.route_entry_parsed if t.category != "filler")
        self.route_entry_status = f"{ok}/{total} resolved" if total else ""

    def _cmd_route_entry_load(self):
        """Load the resolved waypoints into the X-Plane FMS.

        Skipped tokens (airways, procedures, unknown) are ignored — procedures
        can be applied via the DEP/ARR/APP tabs after the base route is loaded.
        """
        parsed = self.route_entry_parsed
        airports = [t for t in parsed if t.category == "airport"]
        if len(airports) < 2 or airports[0].status != "ok" or airports[-1].status != "ok":
            missing = []
            if not airports or airports[0].status != "ok":
                missing.append("departure")
            if len(airports) < 2 or airports[-1].status != "ok":
                missing.append("arrival")
            self._set_status("LOAD ERR", f"Unresolved airport: {', '.join(missing)}")
            self._publish_state()
            return

        entries = [t.entry for t in parsed if t.entry is not None]
        if not entries:
            self._set_status("LOAD ERR", "No resolvable waypoints — PARSE first")
            self._publish_state()
            return

        try:
            # Prefer loading via .fms text buffer as it correctly sets ADEP/ADES
            # (Origin/Destination) in the FMS header. But X-Plane silently drops
            # airports it doesn't know (e.g. addon airports not in the nav DB),
            # so verify the entry count and fall back if any were lost.
            fms_text = self._build_route_entry_fms_text(parsed)
            loaded_ok = False
            if fms_text:
                loaded_ok = self._load_fms_plan_text(fms_text, "CUSTOM")
                if loaded_ok and self._read_fms_entry_count() < len(entries):
                    self._log("loadFMSFlightPlan dropped entries — falling back to lat/lon method")
                    loaded_ok = False
            if not loaded_ok:
                if not self._write_route_entry_into_flight_plan(entries):
                    self._write_entries_into_fms(entries)

            xp.setDisplayedFMSEntry(0)
            active_idx = 1 if len(entries) > 1 and entries[0].entry_type == 1 else 0
            xp.setDestinationFMSEntry(min(active_idx, len(entries) - 1))

            total_dist = sum(
                self._haversine_nm(entries[i - 1].lat, entries[i - 1].lon,
                                   entries[i].lat, entries[i].lon)
                for i in range(1, len(entries))
            )

            self.loaded = 1
            self.loaded_filename    = "CUSTOM"
            self.loaded_sid         = ""
            self.loaded_star        = ""
            self.loaded_distance_nm = round(total_dist, 1)
            self._set_status("LOADED")
            self._legs_init_after_load()

            # Refresh procedures for new dep/dest so the DEP/ARR/APP tabs
            # are populated for any SID/STAR/APP the user wants to add next.
            new_dep  = entries[0].ident  if entries[0].entry_type  == 1 else ""
            new_dest = entries[-1].ident if entries[-1].entry_type == 1 else ""
            if new_dep != self.proc_dep_icao or new_dest != self.proc_dest_icao:
                self.proc_dep_icao  = new_dep
                self.proc_dest_icao = new_dest
                self._proc_refresh()

            if hasattr(self, "_sync_route_state"):
                self._sync_route_state()
            if hasattr(self, "_save_state"):
                self._save_state()
            self._log("Route entry loaded:", len(entries), "entries")
        except Exception as exc:
            self._mark_route_unloaded() if hasattr(self, "_mark_route_unloaded") else None
            self._set_status("LOAD ERR", str(exc))

        self._publish_state()

    def _cmd_sync_from_fms(self):
        """Re-derive cached plan state from the live FMS contents.

        Called after manual edits in the native G1000 so the LOAD/NAV/CHECK
        tabs line up with what's actually in the box — recomputes total
        distance, re-detects DEP/DEST airports, clears SID/STAR claims
        (which can no longer be trusted after a manual edit), marks the
        plan name with a trailing ' *', and re-runs validation + wind.
        """
        count = self._read_fms_entry_count()
        if count <= 0:
            self._mark_route_unloaded()
            self._set_status("EMPTY", "FMS has no entries")
            self._publish_state()
            return

        entries = self._live_fms_entries()
        total_dist = 0.0
        for i in range(1, len(entries)):
            total_dist += self._haversine_nm(
                entries[i - 1].lat, entries[i - 1].lon,
                entries[i].lat, entries[i].lon,
            )

        # Force endpoint re-detection — user may have swapped DEP or DEST.
        self.proc_dep_icao = ""
        self.proc_dest_icao = ""
        self._proc_airports_from_fms()

        if not self.loaded_filename:
            self.loaded_filename = "LIVE"
        elif not self.loaded_filename.endswith(" *"):
            self.loaded_filename = f"{self.loaded_filename} *"
        self.loaded_sid = ""
        self.loaded_star = ""
        self.loaded_distance_nm = round(total_dist, 1)
        self.loaded = 1

        self._run_validation()
        self._wind_refresh_dep()
        self._wind_refresh_arr()
        if hasattr(self, "_save_state"):
            self._save_state()

        self._set_status("SYNCED", f"{len(entries)} entries, {total_dist:.0f} nm")
        self._log("Synced from FMS:", len(entries), "entries",
                  f"{total_dist:.1f} nm",
                  "dep=", self.proc_dep_icao, "dest=", self.proc_dest_icao)
        self._publish_state()

    def _cmd_open_fpl(self):
        ref = getattr(self, "fpl_command_ref", None)
        if ref:
            self._log(f"Executing {self.avionics_name} FPL command")
            xp.commandOnce(ref)
            self._set_status("FPL OPEN")
        else:
            self._set_status("NO FPL CMD",
                             "No supported FPL command for this aircraft")
        self._publish_state()
