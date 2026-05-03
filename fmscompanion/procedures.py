"""ProceduresMixin — CIFP parsing and DEP/ARR/APP procedure browser.

Two independent views per kind (dep/arr/app):
  - Name list  (dep/list_row_N_*)  : always shows procedure names; tap to select/highlight.
  - Trans list (dep/trans_row_N_*) : always shows transitions for the selected name.
"""

import math
import os
from typing import Dict, List, Optional

from XPPython3 import xp

from fmscompanion.models import FlightPlanEntry, ProcedureInfo

# Maximum distance (nm) a resolved navaid may be from the airport centre before
# we consider it a name conflict and reject it.  Procedures rarely span more than
# 200 nm; 500 nm is a generous safety margin that still excludes global conflicts.
_MAX_PROC_FIX_DIST_NM = 500.0

# Approach types that use an ILS/LOC-family frequency and inbound course
_ILS_APP_CHARS = frozenset("ILXB")  # ILS, LOC, LDA, LOC BC

# Datarefs for NAV1 radio state (read + write)
_NAV1_FREQ_DREF   = "sim/cockpit/radios/nav1_freq_hz"
_NAV1_COURSE_DREF = "sim/cockpit/radios/nav1_obs_deg_mag_pilot"

# Estimated localizer distance beyond the stop-end of the runway (nm).
# Used to offset the navaid search point toward where the localizer antenna sits.
_LOC_OFFSET_NM = 2.0


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

    @staticmethod
    def _entry_ident(entry: FlightPlanEntry) -> str:
        return (getattr(entry, "ident", "") or "").strip().upper()

    def _merge_sid_with_plan(
        self,
        proc_nav: List[tuple],
        original: List[FlightPlanEntry],
    ) -> List[tuple]:
        """Keep the departure airport first, then SID legs, then the remaining route.

        The original plan already contains the airport and the filed route beyond the
        SID exit. When applying a new SID, resume the original route after the first
        SID/route join point instead of appending the whole plan and duplicating fixes.
        """
        if not original:
            return proc_nav

        merged: List[tuple] = []
        dep_entry = original[0]
        dep_ident = self._entry_ident(dep_entry)
        sid_idents = [
            (ident or "").strip().upper()
            for _, ident in proc_nav
            if (ident or "").strip()
        ]

        merged.append(("entry", dep_entry))
        seen = {dep_ident} if dep_ident else set()

        for ref, ident in proc_nav:
            ident_key = (ident or "").strip().upper()
            if ident_key and ident_key in seen:
                continue
            if ref == xp.NAV_NOT_FOUND:
                continue
            merged.append(("proc", ref, ident))
            if ident_key:
                seen.add(ident_key)

        resume_at = 1
        if sid_idents:
            sid_set = set(sid_idents)
            for i, entry in enumerate(original[1:], start=1):
                if self._entry_ident(entry) in sid_set:
                    resume_at = i + 1

        for entry in original[resume_at:]:
            ident_key = self._entry_ident(entry)
            if ident_key and ident_key in seen:
                continue
            merged.append(("entry", entry))
            if ident_key:
                seen.add(ident_key)

        return merged

    def _merge_arrival_with_route(
        self,
        proc_nav: List[tuple],
        original: List[FlightPlanEntry],
    ) -> List[tuple]:
        """Splice STAR/APP into the live route before the destination airport."""
        if not original:
            return [("proc", ref, ident) for ref, ident in proc_nav if ref != xp.NAV_NOT_FOUND]

        proc_idents = [
            (ident or "").strip().upper()
            for _, ident in proc_nav
            if (ident or "").strip()
        ]
        dest_entry = original[-1] if original and getattr(original[-1], "entry_type", None) == 1 else None
        route_body = original[:-1] if dest_entry else list(original)

        splice_at = len(route_body)
        if proc_idents:
            proc_set = set(proc_idents)
            for i, entry in enumerate(route_body):
                if self._entry_ident(entry) in proc_set:
                    splice_at = i
                    break

        merged: List[tuple] = []
        seen = set()

        for entry in route_body[:splice_at]:
            ident_key = self._entry_ident(entry)
            merged.append(("entry", entry))
            if ident_key:
                seen.add(ident_key)

        for ref, ident in proc_nav:
            ident_key = (ident or "").strip().upper()
            if ref == xp.NAV_NOT_FOUND:
                continue
            if ident_key and ident_key in seen:
                continue
            merged.append(("proc", ref, ident))
            if ident_key:
                seen.add(ident_key)

        if dest_entry:
            dest_ident = self._entry_ident(dest_entry)
            if not dest_ident or dest_ident not in seen:
                merged.append(("entry", dest_entry))

        return merged

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

    def _cmd_apply_recommended(self, kind: str, display_name: str) -> bool:
        """Select and immediately activate a procedure by its display_name.

        Handles the three-level selection (name → transition → activate) in one
        call so the UI can wire a single button per recommendation.
        Returns True if the procedure was found and activated.
        """
        # Find the target ProcedureInfo
        target = next(
            (p for p in self._proc_procs.get(kind, []) if p.display_name == display_name),
            None,
        )
        if target is None:
            self._log(f"apply_recommended({kind}): '{display_name}' not found")
            return False

        # Select the procedure name (opens the transitions view for that name)
        names = self._proc_names.get(kind, [])
        if target.name not in names:
            self._log(f"apply_recommended({kind}): name '{target.name}' not in names list")
            return False
        self._proc_name_idx[kind] = names.index(target.name)
        self._proc_invalidate_both(kind)

        # Select the transition within that name
        transitions = self._proc_transitions(kind)
        for i, proc in enumerate(transitions):
            if proc.display_name == display_name:
                self._proc_index[kind] = i
                break
        else:
            self._log(f"apply_recommended({kind}): transition '{display_name}' not found")
            return False

        self._cmd_proc_activate(kind)
        return True

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
                        # X-Plane internally prefixes some airports with "X" (e.g. EDDB → XEDDB)
                        if len(icao) == 5 and icao.startswith("X"):
                            icao = icao[1:]
                        if len(icao) == 4:
                            dep_icao = icao
                            break
            if not dest_icao:
                for i in range(count - 1, max(count - 7, -1), -1):
                    info = xp.getFMSEntryInfo(i)
                    if getattr(info, "type", None) == xp.Nav_Airport:
                        icao = (getattr(info, "navAidID", "") or "").strip().upper()
                        # X-Plane internally prefixes some airports with "X" (e.g. EDDB → XEDDB)
                        if len(icao) == 5 and icao.startswith("X"):
                            icao = icao[1:]
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

    def _cmd_proc_select_row(self, kind: str, pi: int) -> None:
        """Select (highlight) a procedure name by absolute index. Does not drill in."""
        names = self._proc_names.get(kind, [])
        if not names or pi < 0 or pi >= len(names):
            return
        self._proc_name_idx[kind] = pi
        self._proc_index[kind] = -1
        self._proc_window[kind] = 0
        self._proc_invalidate_both(kind)
        self._log(f"proc_select_row({kind}, {pi}) -> selected name '{names[pi]}'")
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

    def _cmd_proc_select_trans_row(self, kind: str, pi: int) -> None:
        transitions = self._proc_transitions(kind)
        if not transitions or pi < 0 or pi >= len(transitions):
            return
        self._proc_index[kind] = pi
        self._proc_invalidate_trans_cache(kind)
        self._log(f"proc_select_trans_row({kind}) -> index={pi}")

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
        if hasattr(self, "_wind_refresh_dep"):
            self._wind_refresh_dep()
        if hasattr(self, "_wind_refresh_arr"):
            self._wind_refresh_arr()
        if hasattr(self, "_save_state"):
            self._save_state()

    # ── ILS / LOC lookup and radio tuning ────────────────────────────────────

    def _lookup_ils_info(self, proc, apt_icao: str):
        """Return (freq_khz, course_deg) for an ILS/LOC-type approach, or (None, None).

        freq_khz is in X-Plane units (10 Hz steps, e.g. 10900 = 109.00 MHz).
        Result is cached so repeated UI redraws don't hammer the navaid API.
        """
        if not proc or proc.proc_type != "APP":
            return None, None
        if not proc.name or proc.name[0] not in _ILS_APP_CHARS:
            return None, None

        cache = getattr(self, "_ils_info_cache", None)
        if cache is None:
            self._ils_info_cache: Dict[tuple, tuple] = {}
            cache = self._ils_info_cache
        key = (proc.name, apt_icao)
        if key in cache:
            return cache[key]

        result = self._do_ils_lookup(proc, apt_icao)
        cache[key] = result
        self._log(f"ILS lookup {proc.display_name} @ {apt_icao}: freq={result[0]} crs={result[1]}")
        return result

    def _do_ils_lookup(self, proc, apt_icao: str):
        # Resolve airport position for proximity filtering
        apt_lat, apt_lon = None, None
        apt_ref = xp.findNavAid(None, apt_icao, None, None, None, xp.Nav_Airport)
        if apt_ref != xp.NAV_NOT_FOUND:
            try:
                apt_info = xp.getNavAidInfo(apt_ref)
                apt_lat = float(apt_info.latitude)
                apt_lon = float(apt_info.longitude)
            except Exception:
                pass
        if apt_lat is None:
            return None, None

        # Expected magnetic heading from runway designator, e.g. "06L" → 60.0°
        expected_hdg: Optional[float] = None
        try:
            expected_hdg = float(proc.display_runway.rstrip("LRC")) * 10.0
        except (ValueError, AttributeError):
            pass

        # Localizer antenna sits beyond the stop-end of the runway; offset the
        # search point in the reciprocal direction so findNavAid returns the right
        # navaid first at multi-ILS airports.
        search_lat, search_lon = apt_lat, apt_lon
        if expected_hdg is not None:
            recip_rad = math.radians((expected_hdg + 180.0) % 360.0)
            deg_per_nm = 1.0 / 60.0
            search_lat = apt_lat + _LOC_OFFSET_NM * deg_per_nm * math.cos(recip_rad)
            cos_lat = max(math.cos(math.radians(apt_lat)), 1e-6)
            search_lon = apt_lon + _LOC_OFFSET_NM * deg_per_nm * math.sin(recip_rad) / cos_lat

        for nav_type in (xp.Nav_ILS, xp.Nav_Localizer):
            for slat, slon in ((search_lat, search_lon), (apt_lat, apt_lon)):
                ref = xp.findNavAid(None, None, slat, slon, None, nav_type)
                if ref == xp.NAV_NOT_FOUND:
                    continue
                try:
                    nav_info = xp.getNavAidInfo(ref)
                    heading  = float(getattr(nav_info, "heading",   0) or 0)
                    freq     = getattr(nav_info, "frequency", None)
                    if not freq:
                        continue
                    # Reject if heading doesn't match the expected runway (±25°)
                    if expected_hdg is not None:
                        diff = abs(((heading - expected_hdg) + 180) % 360 - 180)
                        if diff > 25.0:
                            continue
                    # Reject if the navaid is implausibly far from the airport
                    nav_lat = float(getattr(nav_info, "latitude",  0))
                    nav_lon = float(getattr(nav_info, "longitude", 0))
                    if _haversine_nm(apt_lat, apt_lon, nav_lat, nav_lon) > 15.0:
                        continue
                    return int(freq), round(heading, 1)
                except Exception:
                    continue

        return None, None

    # ── NAV1 radio read / write ───────────────────────────────────────────────

    def _ils_read_nav1_freq(self) -> Optional[int]:
        """Read current NAV1 active frequency (10 Hz units). Returns None on failure."""
        try:
            ref = xp.findDataRef(_NAV1_FREQ_DREF)
            return int(xp.getDatai(ref)) if ref else None
        except Exception:
            return None

    def _ils_read_nav1_course(self) -> Optional[float]:
        """Read current NAV1 OBS course (degrees magnetic). Returns None on failure."""
        try:
            ref = xp.findDataRef(_NAV1_COURSE_DREF)
            return float(xp.getDataf(ref)) if ref else None
        except Exception:
            return None

    def _cmd_tune_nav1(self, freq_khz: int) -> None:
        """Write NAV1 active frequency (10 Hz units, e.g. 10900 = 109.00 MHz)."""
        try:
            ref = xp.findDataRef(_NAV1_FREQ_DREF)
            if ref:
                xp.setDatai(ref, int(freq_khz))
                self._log(f"Tuned NAV1 → {freq_khz / 100.0:.2f} MHz")
        except Exception as exc:
            self._log("tune_nav1 error:", exc)

    def _cmd_set_nav1_course(self, course_deg: float) -> None:
        """Write NAV1 OBS/course (magnetic degrees)."""
        try:
            ref = xp.findDataRef(_NAV1_COURSE_DREF)
            if ref:
                xp.setDataf(ref, float(course_deg))
                self._log(f"Set NAV1 course → {course_deg:.1f}°")
        except Exception as exc:
            self._log("set_nav1_course error:", exc)

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
                # Re-insert original plan entries (from .fms file) rather than
                # the current FMS state, which may carry over bad navaid lookups
                # from previous procedure activations.
                plan = self._selected_plan()
                original = self._get_cached_entries(plan) if plan else []
                merged = self._merge_sid_with_plan(proc_nav, original)
                self._clear_fms()
                for item in merged:
                    if item[0] == "proc":
                        _, ref, ident = item
                        xp.setFMSEntryInfo(write_idx, ref, 0)
                        write_idx += 1
                        continue
                    _, entry = item
                    try:
                        self._load_entry_into_fms(write_idx, entry)
                        write_idx += 1
                    except Exception:
                        pass
                self._proc_splice_point["arr"] = -1
                self._proc_splice_point["app"] = -1
            else:
                original = self._live_fms_entries() if hasattr(self, "_live_fms_entries") else []
                merged = self._merge_arrival_with_route(proc_nav, original)
                self._clear_fms()
                write_idx = 0
                for item in merged:
                    if item[0] == "proc":
                        _, ref, ident = item
                        xp.setFMSEntryInfo(write_idx, ref, 0)
                        write_idx += 1
                        continue
                    _, entry = item
                    try:
                        self._load_entry_into_fms(write_idx, entry)
                        write_idx += 1
                    except Exception:
                        pass
                self._proc_splice_point[kind] = write_idx

            self._legs_init_after_load()
            self._proc_loaded[kind] = proc.display_name
            self._proc_status[kind] = f"LOADED {proc.display_name}"
            self._log(f"proc_activate({kind}):", proc.proc_type, proc.display_name,
                      "waypoints=", len(proc.waypoints), "written=", write_idx)
        except Exception as exc:
            self._proc_status[kind] = f"ERR {exc}"
            self._log(f"proc_activate({kind}) error:", exc)

        self._proc_invalidate_trans_cache(kind)
