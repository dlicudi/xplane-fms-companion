"""ProceduresMixin — CIFP parsing and DEP/ARR/APP procedure browser.

Two independent views per kind (dep/arr/app):
  - Name list  (dep/list_row_N_*)  : always shows procedure names; tap to select/highlight.
  - Trans list (dep/trans_row_N_*) : always shows transitions for the selected name.
"""

import math
import os
from typing import Dict, List, Optional

from XPPython3 import xp

from fmscompanion.models import ProcedureInfo

# Maximum distance (nm) a resolved navaid may be from the airport centre before
# we consider it a name conflict and reject it.  Procedures rarely span more than
# 200 nm; 500 nm is a generous safety margin that still excludes global conflicts.
_MAX_PROC_FIX_DIST_NM = 500.0


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + (
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class ProceduresMixin:
    """Mixin providing SID/STAR/APP procedure browsing and FMS insertion."""

    # ── CIFP file access ──

    def _cifp_path(self, icao: str) -> Optional[str]:
        """Return path to CIFP file for airport, preferring Custom Data over default data."""
        system_path = xp.getSystemPath()
        for subdir in ("Custom Data", os.path.join("Resources", "default data")):
            path = os.path.join(system_path, subdir, "CIFP", f"{icao.upper()}.dat")
            if os.path.isfile(path):
                return path
        return None

    def _parse_cifp(self, icao: str) -> List[ProcedureInfo]:
        """Parse CIFP file for airport and return ProcedureInfo list (all modes)."""
        if icao in self._cifp_cache:
            return self._cifp_cache[icao]

        path = self._cifp_path(icao)
        if not path:
            self._log("No CIFP file for", icao)
            self._cifp_cache[icao] = []
            return []

        raw: Dict[tuple, list] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    rec_type, _, rest = line.partition(":")
                    rec_type = rec_type.strip()
                    if rec_type not in ("SID", "STAR", "APPCH"):
                        continue
                    rest = rest.rstrip(";")
                    fields = [fld.strip() for fld in rest.split(",")]
                    if len(fields) < 5:
                        continue
                    try:
                        seq = int(fields[0])
                    except (ValueError, TypeError):
                        seq = 0
                    proc_name = fields[2]
                    transition = fields[3]
                    fix_ident = fields[4]
                    if not proc_name or not fix_ident:
                        continue
                    pt = "APP" if rec_type == "APPCH" else rec_type
                    key = (pt, proc_name, transition)
                    raw.setdefault(key, []).append((seq, fix_ident))
        except Exception as exc:
            self._log("CIFP parse error", icao, exc)
            self._cifp_cache[icao] = []
            return []

        procedures: List[ProcedureInfo] = []

        for (pt, proc_name, transition), legs in sorted(raw.items()):
            if pt == "SID" and not transition.startswith("RW"):
                continue
            if pt == "STAR" and not transition:
                continue

            t_legs = sorted(legs, key=lambda x: x[0])
            wpts = list(dict.fromkeys(fix for _, fix in t_legs if fix))

            common_key = (pt, proc_name, "")
            if common_key in raw:
                c_legs = sorted(raw[common_key], key=lambda x: x[0])
                c_wpts = list(dict.fromkeys(fix for _, fix in c_legs if fix))
                wpts = wpts + [w for w in c_wpts if w not in wpts]

            if not wpts:
                continue

            if pt == "SID":
                rwy = transition[2:] if transition.startswith("RW") else transition
                display_name = f"{proc_name} {rwy}" if rwy else proc_name
                display_runway = rwy
            elif pt == "STAR":
                display_name = f"{proc_name} {transition}" if transition else proc_name
                display_runway = transition
            else:  # APP
                app_type = self._APP_TYPE_LABELS.get(proc_name[0], proc_name[0]) if proc_name else ""
                rwy = proc_name[1:] if len(proc_name) > 1 else proc_name
                display_name = f"{app_type} {rwy}".strip()
                display_runway = rwy

            procedures.append(ProcedureInfo(
                name=proc_name,
                proc_type=pt,
                transition=transition,
                waypoints=wpts,
                display_name=display_name,
                display_runway=display_runway,
            ))

        # Deduplicate APP entries: one per proc_name, preferring a non-blank transition (IAF)
        app_rep: Dict[str, ProcedureInfo] = {}
        for p in procedures:
            if p.proc_type != "APP":
                continue
            existing = app_rep.get(p.name)
            if existing is None:
                app_rep[p.name] = p
            elif existing.transition == "" and p.transition != "":
                app_rep[p.name] = p
        deduped: List[ProcedureInfo] = []
        seen_app: set = set()
        for p in procedures:
            if p.proc_type != "APP":
                deduped.append(p)
            elif p.name not in seen_app:
                deduped.append(app_rep[p.name])
                seen_app.add(p.name)
        procedures = deduped

        self._cifp_cache[icao] = procedures
        by_type = {}
        for p in procedures:
            by_type[p.proc_type] = by_type.get(p.proc_type, 0) + 1
        self._log("CIFP parsed", icao, "->", len(procedures), "procedures", by_type)
        return procedures

    # ── Procedure state helpers ──

    def _proc_airport_for(self, kind: str) -> str:
        return self.proc_dep_icao if kind == "dep" else self.proc_dest_icao

    def _proc_invalidate_cache(self, kind: str) -> None:
        self._proc_cache_valid[kind] = False

    def _proc_invalidate_trans_cache(self, kind: str) -> None:
        self._proc_trans_cache_valid[kind] = False

    def _proc_invalidate_both(self, kind: str) -> None:
        self._proc_cache_valid[kind] = False
        self._proc_trans_cache_valid[kind] = False

    def _proc_transitions(self, kind: str) -> List[ProcedureInfo]:
        """Return transitions for the currently selected procedure name, or [] if none selected."""
        name_idx = self._proc_name_idx.get(kind, -1)
        if name_idx < 0:
            return []
        names = self._proc_names.get(kind, [])
        if name_idx >= len(names):
            return []
        selected_name = names[name_idx]
        return [p for p in self._proc_procs.get(kind, []) if p.name == selected_name]

    def _proc_selected_proc_name(self, kind: str) -> str:
        name_idx = self._proc_name_idx.get(kind, -1)
        names = self._proc_names.get(kind, [])
        return names[name_idx] if 0 <= name_idx < len(names) else ""

    def _proc_selected_name(self, kind: str) -> str:
        """Display name of the selected transition."""
        transitions = self._proc_transitions(kind)
        idx = self._proc_index.get(kind, -1)
        return transitions[idx].display_name if 0 <= idx < len(transitions) else ""

    def _proc_selected_runway(self, kind: str) -> str:
        transitions = self._proc_transitions(kind)
        idx = self._proc_index.get(kind, -1)
        return transitions[idx].display_runway if 0 <= idx < len(transitions) else ""

    # ── Name-list page helpers ──

    def _proc_name_window_page(self, kind: str) -> int:
        n = len(self._proc_names.get(kind, []))
        if n <= 0:
            return 1
        return self._proc_name_window.get(kind, 0) // self.PROC_VISIBLE_ROWS + 1

    def _proc_name_list_page_str(self, kind: str) -> str:
        n = len(self._proc_names.get(kind, []))
        if n <= 0:
            return ""
        page = self._proc_name_window.get(kind, 0) // self.PROC_VISIBLE_ROWS + 1
        total = (n + self.PROC_VISIBLE_ROWS - 1) // self.PROC_VISIBLE_ROWS
        return f"{page}/{total}"

    def _proc_name_sel_count_str(self, kind: str) -> str:
        name_idx = self._proc_name_idx.get(kind, -1)
        n = len(self._proc_names.get(kind, []))
        if n <= 0 or name_idx < 0:
            return f"-/{self.PROC_VISIBLE_ROWS}"
        w = self._proc_name_window.get(kind, 0)
        row_on_page = name_idx - w + 1
        if 1 <= row_on_page <= self.PROC_VISIBLE_ROWS:
            return f"{row_on_page}/{self.PROC_VISIBLE_ROWS}"
        return f"-/{self.PROC_VISIBLE_ROWS}"

    # ── Trans-list page helpers ──

    def _proc_trans_window_page(self, kind: str) -> int:
        transitions = self._proc_transitions(kind)
        if not transitions:
            return 1
        return self._proc_window.get(kind, 0) // self.PROC_VISIBLE_ROWS + 1

    def _proc_trans_list_page_str(self, kind: str) -> str:
        transitions = self._proc_transitions(kind)
        n = len(transitions)
        if n <= 0:
            return ""
        page = self._proc_window.get(kind, 0) // self.PROC_VISIBLE_ROWS + 1
        total = (n + self.PROC_VISIBLE_ROWS - 1) // self.PROC_VISIBLE_ROWS
        return f"{page}/{total}"

    def _proc_trans_sel_count_str(self, kind: str) -> str:
        transitions = self._proc_transitions(kind)
        n = len(transitions)
        if n <= 0:
            return "0/0"
        idx = self._proc_index.get(kind, -1)
        w = self._proc_window.get(kind, 0)
        if idx >= 0:
            row_on_page = idx - w + 1
            if 1 <= row_on_page <= self.PROC_VISIBLE_ROWS:
                return f"{row_on_page}/{self.PROC_VISIBLE_ROWS}"
        return f"-/{self.PROC_VISIBLE_ROWS}"

    def _proc_max_aligned_window_start(self, n: int) -> int:
        if n <= 0:
            return 0
        return ((n - 1) // self.PROC_VISIBLE_ROWS) * self.PROC_VISIBLE_ROWS

    # ── Name-list cache ──

    def _proc_ensure_cache(self, kind: str) -> None:
        if self._proc_cache_valid.get(kind, False):
            return
        rows: Dict[int, Dict[str, object]] = {}
        _empty = {"plan_index": -1, "index": "", "name": "", "runway": "", "is_selected": 0, "status": ""}
        names = self._proc_names.get(kind, [])
        w = self._proc_name_window.get(kind, 0)
        name_idx = self._proc_name_idx.get(kind, -1)
        for row in range(1, self.PROC_VISIBLE_ROWS + 1):
            pi = w + (row - 1)
            if pi < 0 or pi >= len(names):
                rows[row] = _empty.copy()
                continue
            is_sel = int(pi == name_idx)
            rows[row] = {
                "plan_index": pi,
                "index": str(pi + 1),
                "name": names[pi],
                "runway": "",
                "is_selected": is_sel,
                "status": "SEL" if is_sel else "",
            }
        self._proc_rows_cache[kind] = rows
        self._proc_cache_valid[kind] = True

    # ── Trans-list cache ──

    def _proc_ensure_trans_cache(self, kind: str) -> None:
        if self._proc_trans_cache_valid.get(kind, False):
            return
        rows: Dict[int, Dict[str, object]] = {}
        _empty = {"plan_index": -1, "index": "", "name": "", "runway": "", "is_selected": 0, "status": ""}
        transitions = self._proc_transitions(kind)
        n = len(transitions)
        w = self._proc_window.get(kind, 0)
        idx = self._proc_index.get(kind, -1)
        for row in range(1, self.PROC_VISIBLE_ROWS + 1):
            pi = w + (row - 1)
            if pi < 0 or pi >= n:
                rows[row] = _empty.copy()
                continue
            proc = transitions[pi]
            is_sel = int(idx >= 0 and pi == idx)
            rows[row] = {
                "plan_index": pi,
                "index": str(pi + 1),
                "name": proc.display_name,
                "runway": proc.display_runway,
                "is_selected": is_sel,
                "status": "SEL" if is_sel else "",
            }
        self._proc_trans_rows_cache[kind] = rows
        self._proc_trans_cache_valid[kind] = True

    def _proc_read_row_str(self, kind: str, row: int, field: str) -> str:
        self._proc_ensure_cache(kind)
        return str(self._proc_rows_cache.get(kind, {}).get(row, {}).get(field, ""))

    def _proc_read_row_int(self, kind: str, row: int, field: str) -> int:
        self._proc_ensure_cache(kind)
        val = self._proc_rows_cache.get(kind, {}).get(row, {}).get(field, 0)
        return val if isinstance(val, int) else 0

    def _proc_read_trans_row_str(self, kind: str, row: int, field: str) -> str:
        self._proc_ensure_trans_cache(kind)
        return str(self._proc_trans_rows_cache.get(kind, {}).get(row, {}).get(field, ""))

    def _proc_read_trans_row_int(self, kind: str, row: int, field: str) -> int:
        self._proc_ensure_trans_cache(kind)
        val = self._proc_trans_rows_cache.get(kind, {}).get(row, {}).get(field, 0)
        return val if isinstance(val, int) else 0

    # ── Airport and procedure population ──

    def _proc_airports_from_fms(self) -> None:
        """Populate proc_dep_icao/proc_dest_icao from live FMS airport entries."""
        if self.proc_dep_icao and self.proc_dest_icao:
            return
        try:
            count = xp.countFMSEntries()
            if count <= 0:
                return
            dep_icao = self.proc_dep_icao
            dest_icao = self.proc_dest_icao
            if not dep_icao:
                for i in range(min(count, 6)):
                    info = xp.getFMSEntryInfo(i)
                    if getattr(info, "type", None) == xp.Nav_Airport:
                        icao = (getattr(info, "navAidID", "") or "").strip().upper()
                        if len(icao) == 4:
                            dep_icao = icao
                            break
            if not dest_icao:
                for i in range(count - 1, max(count - 7, -1), -1):
                    info = xp.getFMSEntryInfo(i)
                    if getattr(info, "type", None) == xp.Nav_Airport:
                        icao = (getattr(info, "navAidID", "") or "").strip().upper()
                        if len(icao) == 4:
                            dest_icao = icao
                            break
            if dep_icao or dest_icao:
                changed = (dep_icao != self.proc_dep_icao or dest_icao != self.proc_dest_icao)
                self.proc_dep_icao = dep_icao
                self.proc_dest_icao = dest_icao
                self._log("proc_airports_from_fms: dep=", dep_icao, "dest=", dest_icao)
                if changed:
                    self._proc_refresh()
        except Exception as exc:
            self._log("proc_airports_from_fms error:", exc)

    def _proc_refresh(self) -> None:
        """Reload procedures from CIFP for dep/dest airports."""
        dep = self.proc_dep_icao.strip().upper()
        dest = self.proc_dest_icao.strip().upper()

        dep_procs: List[ProcedureInfo] = self._parse_cifp(dep) if dep else []
        dest_procs: List[ProcedureInfo] = self._parse_cifp(dest) if dest else []

        self._proc_procs["dep"] = [p for p in dep_procs if p.proc_type == "SID"]
        self._proc_procs["arr"] = [p for p in dest_procs if p.proc_type == "STAR"]
        self._proc_procs["app"] = [p for p in dest_procs if p.proc_type == "APP"]

        for k in self.KINDS:
            self._proc_names[k] = list(dict.fromkeys(p.name for p in self._proc_procs[k]))
            self._proc_name_idx[k] = -1
            self._proc_name_window[k] = 0
            self._proc_index[k] = -1
            self._proc_window[k] = 0
            self._proc_cache_valid[k] = False
            self._proc_trans_cache_valid[k] = False
            self._proc_status[k] = "READY"
            self._proc_splice_point[k] = -1
            self._proc_loaded[k] = ""

        self._log("proc_refresh: dep(SID)", len(self._proc_procs["dep"]),
                  "arr(STAR)", len(self._proc_procs["arr"]),
                  "app(APP)", len(self._proc_procs["app"]))

    # ── Commands — name list ──

    def _cmd_proc_back(self, kind: str) -> None:
        """Clear the selected procedure name, resetting the trans list."""
        self._proc_name_idx[kind] = -1
        self._proc_index[kind] = -1
        self._proc_window[kind] = 0
        self._proc_invalidate_both(kind)
        self._log(f"proc_back({kind})")

    def _cmd_proc_scroll_up(self, kind: str) -> None:
        names = self._proc_names.get(kind, [])
        if not names:
            return
        w = self._proc_name_window.get(kind, 0)
        new_start = max(0, w - self.PROC_VISIBLE_ROWS)
        if new_start != w:
            self._proc_name_window[kind] = new_start
            self._proc_invalidate_cache(kind)

    def _cmd_proc_scroll_down(self, kind: str) -> None:
        names = self._proc_names.get(kind, [])
        n = len(names)
        if n <= 0:
            return
        w = self._proc_name_window.get(kind, 0)
        max_w = self._proc_max_aligned_window_start(n)
        new_start = min(w + self.PROC_VISIBLE_ROWS, max_w)
        if new_start != w:
            self._proc_name_window[kind] = new_start
            self._proc_invalidate_cache(kind)

    def _cmd_proc_select_row(self, kind: str, row: int) -> None:
        """Select (highlight) a procedure name. Does not drill in — stays on name list."""
        names = self._proc_names.get(kind, [])
        w = self._proc_name_window.get(kind, 0)
        pi = w + (row - 1)
        if not names or pi < 0 or pi >= len(names):
            return
        self._proc_name_idx[kind] = pi
        self._proc_index[kind] = -1
        self._proc_window[kind] = 0
        self._proc_invalidate_both(kind)
        self._log(f"proc_select_row({kind}, {row}) -> selected name '{names[pi]}'")
        # Auto-select if there is exactly one transition
        transitions = self._proc_transitions(kind)
        if len(transitions) == 1:
            self._proc_index[kind] = 0
            self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_previous(self, kind: str) -> None:
        w = self._proc_name_window.get(kind, 0)
        self._proc_name_window[kind] = max(0, w - 1)
        self._proc_invalidate_cache(kind)

    def _cmd_proc_next(self, kind: str) -> None:
        names = self._proc_names.get(kind, [])
        n = len(names)
        w = self._proc_name_window.get(kind, 0)
        max_w = max(0, n - 1)
        self._proc_name_window[kind] = min(w + 1, max_w)
        self._proc_invalidate_cache(kind)

    def _cmd_proc_clear_name(self, kind: str) -> None:
        self._proc_name_idx[kind] = -1
        self._proc_invalidate_both(kind)

    # ── Commands — transition list ──

    def _cmd_proc_trans_scroll_up(self, kind: str) -> None:
        transitions = self._proc_transitions(kind)
        if not transitions:
            return
        w = self._proc_window.get(kind, 0)
        new_start = max(0, w - self.PROC_VISIBLE_ROWS)
        if new_start != w:
            self._proc_window[kind] = new_start
            self._proc_index[kind] = -1
            self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_trans_scroll_down(self, kind: str) -> None:
        transitions = self._proc_transitions(kind)
        n = len(transitions)
        if n <= 0:
            return
        w = self._proc_window.get(kind, 0)
        max_w = self._proc_max_aligned_window_start(n)
        new_start = min(w + self.PROC_VISIBLE_ROWS, max_w)
        if new_start != w:
            self._proc_window[kind] = new_start
            self._proc_index[kind] = -1
            self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_select_trans_row(self, kind: str, row: int) -> None:
        transitions = self._proc_transitions(kind)
        w = self._proc_window.get(kind, 0)
        pi = w + (row - 1)
        if not transitions or pi < 0 or pi >= len(transitions):
            return
        self._proc_index[kind] = pi
        self._proc_invalidate_trans_cache(kind)
        self._log(f"proc_select_trans_row({kind}, {row}) -> index={pi}")

    def _cmd_proc_trans_previous(self, kind: str) -> None:
        transitions = self._proc_transitions(kind)
        if not transitions:
            return
        w = self._proc_window.get(kind, 0)
        idx = self._proc_index.get(kind, -1)
        end = min(w + self.PROC_VISIBLE_ROWS, len(transitions))
        self._proc_index[kind] = (end - 1) if (idx < w or idx >= end) else max(w, idx - 1)
        self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_trans_next(self, kind: str) -> None:
        transitions = self._proc_transitions(kind)
        if not transitions:
            return
        w = self._proc_window.get(kind, 0)
        idx = self._proc_index.get(kind, -1)
        end = min(w + self.PROC_VISIBLE_ROWS, len(transitions))
        self._proc_index[kind] = w if (idx < w or idx >= end) else min(end - 1, idx + 1)
        self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_clear_selected(self, kind: str) -> None:
        self._proc_index[kind] = -1
        self._proc_invalidate_trans_cache(kind)

    def _cmd_proc_refresh(self, kind: str) -> None:
        self._cifp_cache.clear()
        if not self.proc_dep_icao and not self.proc_dest_icao:
            self._proc_airports_from_fms()
        else:
            self._proc_refresh()

    def _find_proc_navaid(self, ident: str, center_lat, center_lon):
        """Resolve a procedure fix ident to a navaid ref, rejecting results that are
        unreasonably far from the airport (global name conflicts in Navigraph data).

        Falls back through Fix → VOR → NDB → Airport.  If the closest match of any
        type is further than _MAX_PROC_FIX_DIST_NM from the airport, returns
        xp.NAV_NOT_FOUND so the fix is skipped rather than inserted at wrong coords.
        """
        search_types = [xp.Nav_Fix, xp.Nav_VOR, xp.Nav_NDB, xp.Nav_Airport]
        for nav_type in search_types:
            ref = xp.findNavAid(None, ident, center_lat, center_lon, None, nav_type)
            if ref == xp.NAV_NOT_FOUND:
                continue
            if center_lat is not None and center_lon is not None:
                # Proximity check — reject navaids further than the threshold.
                try:
                    info = xp.getNavAidInfo(ref)
                    nav_lat = getattr(info, "latitude",  0.0)
                    nav_lon = getattr(info, "longitude", 0.0)
                    dist = _haversine_nm(center_lat, center_lon, nav_lat, nav_lon)
                    if dist > _MAX_PROC_FIX_DIST_NM:
                        self._log(
                            f"  skip '{ident}': resolved {dist:.0f} nm from airport "
                            f"({nav_lat:.2f}, {nav_lon:.2f}) — likely name conflict"
                        )
                        continue
                except Exception:
                    pass
            else:
                # No airport centre — only trust airport-type lookups; reject all
                # others to avoid global name conflicts with Navigraph data.
                if nav_type != xp.Nav_Airport:
                    continue
            return ref
        return xp.NAV_NOT_FOUND

    def _cmd_proc_activate(self, kind: str) -> None:
        """Insert selected procedure waypoints into the FMS."""
        transitions = self._proc_transitions(kind)
        idx = self._proc_index.get(kind, -1)
        if idx < 0 or idx >= len(transitions):
            self._log(f"proc_activate({kind}): nothing selected")
            return
        proc = transitions[idx]
        if not proc.waypoints:
            self._log(f"proc_activate({kind}): no waypoints for", proc.display_name)
            return

        apt_lat, apt_lon = None, None
        apt_icao = self._proc_airport_for(kind)
        if apt_icao:
            # Try multiple nav types — some simulators/navdata sets index airports
            # under different types, or the ICAO might also match a VOR/NDB.
            for _apt_type in (xp.Nav_Airport, xp.Nav_Fix, xp.Nav_VOR, xp.Nav_NDB):
                apt_ref = xp.findNavAid(None, apt_icao, None, None, None, _apt_type)
                if apt_ref != xp.NAV_NOT_FOUND:
                    try:
                        apt_info = xp.getNavAidInfo(apt_ref)
                        apt_lat = apt_info.latitude
                        apt_lon = apt_info.longitude
                        break
                    except Exception:
                        pass
            if apt_lat is None:
                self._log(f"proc_activate({kind}): WARNING could not resolve airport"
                          f" '{apt_icao}' — proximity check disabled for this insertion")

        proc_nav = []
        for ident in proc.waypoints:
            ref = self._find_proc_navaid(ident, apt_lat, apt_lon)
            proc_nav.append((ref, ident))

        write_idx = 0
        try:
            if proc.proc_type == "SID":
                count = xp.countFMSEntries()
                existing = [xp.getFMSEntryInfo(i) for i in range(count)]
                self._clear_fms()
                for ref, ident in proc_nav:
                    if ref != xp.NAV_NOT_FOUND:
                        xp.setFMSEntryInfo(write_idx, ref, 0)
                        write_idx += 1
                for info in existing:
                    try:
                        lat = getattr(info, "lat", getattr(info, "latitude", 0.0))
                        lon = getattr(info, "lon", getattr(info, "longitude", 0.0))
                        nav_id = getattr(info, "navAidID", "").strip()
                        if getattr(info, "type", None) == xp.Nav_LatLon or not nav_id:
                            xp.setFMSEntryLatLon(write_idx, lat, lon, info.altitude)
                        else:
                            ref = xp.findNavAid(None, nav_id, lat, lon, None, xp.Nav_Fix)
                            if ref == xp.NAV_NOT_FOUND:
                                ref = xp.findNavAid(None, nav_id, lat, lon, None, xp.Nav_VOR)
                            if ref != xp.NAV_NOT_FOUND:
                                xp.setFMSEntryInfo(write_idx, ref, info.altitude)
                            else:
                                xp.setFMSEntryLatLon(write_idx, lat, lon, info.altitude)
                        write_idx += 1
                    except Exception:
                        pass
                self._proc_splice_point["arr"] = -1
                self._proc_splice_point["app"] = -1
            else:
                # STAR or APP: replace previous insertion at splice point, or append
                splice = self._proc_splice_point.get(kind, -1)
                count = xp.countFMSEntries()
                if 0 <= splice <= count:
                    keep = [xp.getFMSEntryInfo(i) for i in range(splice)]
                    self._clear_fms()
                    write_idx = 0
                    for info in keep:
                        try:
                            lat = getattr(info, "lat", getattr(info, "latitude", 0.0))
                            lon = getattr(info, "lon", getattr(info, "longitude", 0.0))
                            nav_id = getattr(info, "navAidID", "").strip()
                            if getattr(info, "type", None) == xp.Nav_LatLon or not nav_id:
                                xp.setFMSEntryLatLon(write_idx, lat, lon, info.altitude)
                            else:
                                ref = xp.findNavAid(None, nav_id, lat, lon, None, xp.Nav_Fix)
                                if ref == xp.NAV_NOT_FOUND:
                                    ref = xp.findNavAid(None, nav_id, lat, lon, None, xp.Nav_VOR)
                                if ref != xp.NAV_NOT_FOUND:
                                    xp.setFMSEntryInfo(write_idx, ref, info.altitude)
                                else:
                                    xp.setFMSEntryLatLon(write_idx, lat, lon, info.altitude)
                            write_idx += 1
                        except Exception:
                            pass
                else:
                    write_idx = xp.countFMSEntries()
                self._proc_splice_point[kind] = write_idx
                for ref, ident in proc_nav:
                    if ref != xp.NAV_NOT_FOUND:
                        xp.setFMSEntryInfo(write_idx, ref, 0)
                        write_idx += 1

            self._legs_init_after_load()
            self._proc_loaded[kind] = proc.display_name
            self._proc_status[kind] = f"LOADED {proc.display_name}"
            self._log(f"proc_activate({kind}):", proc.proc_type, proc.display_name,
                      "waypoints=", len(proc.waypoints), "written=", write_idx)
        except Exception as exc:
            self._proc_status[kind] = f"ERR {exc}"
            self._log(f"proc_activate({kind}) error:", exc)

        self._proc_invalidate_trans_cache(kind)
