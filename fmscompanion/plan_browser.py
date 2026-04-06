"""PlanBrowserMixin — plan list display, sorting, and selection commands."""

import os
import time
from typing import Dict, Optional


class PlanBrowserMixin:
    """Mixin providing the scrollable plan list browser (display, sort, row selection)."""

    # ── Window helpers ──

    def _plan_list_max_aligned_window_start(self, n: int) -> int:
        """Max valid window_start for n plans (0-based indices), page-aligned by 3."""
        if n <= 0:
            return 0
        return ((n - 1) // self.PLAN_LIST_VISIBLE_ROWS) * self.PLAN_LIST_VISIBLE_ROWS

    def _plan_list_align_window_start(self, w: int, n: int) -> int:
        """Snap w down to a multiple of 3 and clamp to [0, max_aligned]."""
        max_w = self._plan_list_max_aligned_window_start(n)
        aligned = (max(0, int(w)) // self.PLAN_LIST_VISIBLE_ROWS) * self.PLAN_LIST_VISIBLE_ROWS
        return max(0, min(aligned, max_w))

    # ── Row cache ──

    def _invalidate_list_cache(self) -> None:
        self._list_cache_valid = False

    def _ensure_list_cache(self) -> None:
        if self._list_cache_valid:
            return
        t0 = time.perf_counter()
        rows: Dict[int, Dict[str, object]] = {}
        n = len(self.plans)
        w = self.browser_list_window_start
        page = w // self.PLAN_LIST_VISIBLE_ROWS + 1 if n else 0
        page_count = (n + self.PLAN_LIST_VISIBLE_ROWS - 1) // self.PLAN_LIST_VISIBLE_ROWS if n else 0

        for row in range(1, self.PLAN_LIST_VISIBLE_ROWS + 1):
            pi = w + (row - 1)
            if pi < 0 or pi >= n:
                rows[row] = {
                    "plan_index": -1,
                    "index": "",
                    "filename": "",
                    "timestamp": "",
                    "dep": "",
                    "dest": "",
                    "route": "",
                    "wpt_count": 0,
                    "max_alt_ft": 0,
                    "distance_nm": 0.0,
                    "is_selected": 0,
                    "status": "",
                }
                continue

            plan = self.plans[pi]
            dep = (plan.dep or "").strip()
            dep = "" if (not dep or dep == "----") else dep
            dest = (plan.dest or "").strip()
            dest = "" if (not dest or dest == "----") else dest
            if dep and dest:
                route = f"{dep} {dest}"
            else:
                route = dep or dest
            is_selected = int(self.index >= 0 and pi == self.index)
            rows[row] = {
                "plan_index": pi,
                "index": str(pi + 1),
                "filename": os.path.splitext(plan.filename)[0],
                "timestamp": self._format_file_timestamp(plan.file_timestamp),
                "dep": dep,
                "dest": dest,
                "route": route,
                "wpt_count": int(plan.waypoint_count),
                "max_alt_ft": int(plan.max_altitude),
                "distance_nm": float(plan.total_distance_nm),
                "is_selected": is_selected,
                "status": "SEL" if is_selected else "",
            }

        self._list_rows_cache = rows
        self._list_cache_valid = True
        self._perf_log(
            f"list_cache_rebuild_ms={(time.perf_counter() - t0) * 1000.0:.2f}",
            f"plans={n}",
            f"selected={self.index}",
            f"window_start={w}",
            f"page={page}",
            f"page_count={page_count}",
        )

    # ── Row readers ──

    def _format_file_timestamp(self, ts: float) -> str:
        if ts <= 0:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except Exception:
            return ""

    def _plan_list_read_row_plan_index(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("index", ""))

    def _plan_list_read_row_filename(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("filename", ""))

    def _plan_list_read_row_timestamp(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("timestamp", ""))

    def _plan_list_read_row_dep(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("dep", ""))

    def _plan_list_read_row_dest(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("dest", ""))

    def _plan_list_read_row_route(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("route", ""))

    def _plan_list_read_row_wpt_count(self, row: int) -> int:
        self._ensure_list_cache()
        return int(self._list_rows_cache.get(row, {}).get("wpt_count", 0))

    def _plan_list_read_row_max_alt_ft(self, row: int) -> int:
        self._ensure_list_cache()
        return int(self._list_rows_cache.get(row, {}).get("max_alt_ft", 0))

    def _plan_list_read_row_distance_nm(self, row: int) -> int:
        self._ensure_list_cache()
        return int(round(self._list_rows_cache.get(row, {}).get("distance_nm", 0.0)))

    def _plan_list_read_row_status(self, row: int) -> str:
        self._ensure_list_cache()
        return str(self._list_rows_cache.get(row, {}).get("status", ""))

    def _plan_list_read_selected_row(self) -> int:
        w = self.browser_list_window_start
        n = len(self.plans)
        if self.index >= 0 and 0 <= self.index < n:
            row_on_page = self.index - w + 1
            if 1 <= row_on_page <= self.PLAN_LIST_VISIBLE_ROWS:
                return int(row_on_page)
        return 0

    def _plan_list_read_page_indicator(self) -> str:
        n = len(self.plans)
        if n <= 0:
            return ""
        page = self.browser_list_window_start // self.PLAN_LIST_VISIBLE_ROWS + 1
        total = (n + self.PLAN_LIST_VISIBLE_ROWS - 1) // self.PLAN_LIST_VISIBLE_ROWS
        return f"{page}/{total}"

    def _plan_list_read_selected_over_count(self) -> str:
        nrows = self.PLAN_LIST_VISIBLE_ROWS
        if not self.plans:
            return "0/0"
        if self.index < 0:
            return f"-/{nrows}"
        w = self.browser_list_window_start
        row = self.index - w + 1
        if row < 1 or row > nrows:
            return f"-/{nrows}"
        return f"{row}/{nrows}"

    def _plan_list_read_sort_key_label(self) -> str:
        return "NAME" if self.plan_sort_key == 0 else "DATE"

    def _plan_list_read_sort_dir_label(self) -> str:
        return "DESC" if self.plan_sort_desc else "ASC"

    def _plan_list_read_window_page(self) -> int:
        n = len(self.plans)
        if n <= 0:
            return 1
        w = self.browser_list_window_start
        page = w // self.PLAN_LIST_VISIBLE_ROWS + 1
        return max(1, int(page))

    # ── Sort ──

    def _sort_plans(self):
        """Reorder self.plans in place (does not change selection index)."""
        if not self.plans:
            return
        if self.plan_sort_key == 0:
            self.plans.sort(key=lambda p: p.filename.lower(), reverse=self.plan_sort_desc)
        else:
            if self.plan_sort_desc:
                self.plans.sort(key=lambda p: (-p.file_timestamp, p.filename.lower()))
            else:
                self.plans.sort(key=lambda p: (p.file_timestamp, p.filename.lower()))

    def _plan_list_apply_sort(self, key: Optional[int] = None, desc: Optional[bool] = None):
        if not self.plans:
            self._set_status("EMPTY")
            self._invalidate_list_cache()
            self._publish_state()
            return

        sel_fn = self.plans[self.index].filename if 0 <= self.index < len(self.plans) else None
        if key is not None:
            self.plan_sort_key = 1 if int(key) else 0
        if desc is not None:
            self.plan_sort_desc = bool(desc)

        self._sort_plans()

        if sel_fn is not None:
            self.index = next((i for i, p in enumerate(self.plans) if p.filename == sel_fn), -1)
        if self.index >= 0:
            self._plan_list_ensure_index_visible()
        else:
            self.browser_list_window_start = self._plan_list_align_window_start(
                self.browser_list_window_start, len(self.plans))
        sort_key = "NAME" if self.plan_sort_key == 0 else "DATE"
        sort_dir = "DESC" if self.plan_sort_desc else "ASC"
        self._set_status(f"SORT {sort_key} {sort_dir}")
        self._invalidate_list_cache()
        self._publish_state()

    def _plan_list_ensure_index_visible(self):
        if not self.plans:
            self.browser_list_window_start = 0
            return
        if self.index < 0:
            return
        n = len(self.plans)
        max_w = self._plan_list_max_aligned_window_start(n)
        page_start = (self.index // self.PLAN_LIST_VISIBLE_ROWS) * self.PLAN_LIST_VISIBLE_ROWS
        self.browser_list_window_start = max(0, min(page_start, max_w))

    # ── Sort commands ──

    def _cmd_list_sort_filename(self):
        self._plan_list_apply_sort(key=0)

    def _cmd_list_sort_timestamp(self):
        self._plan_list_apply_sort(key=1)

    def _cmd_list_toggle_sort_key(self):
        self._plan_list_apply_sort(key=1 - self.plan_sort_key)

    def _cmd_list_sort_asc(self):
        self._plan_list_apply_sort(desc=False)

    def _cmd_list_sort_desc(self):
        self._plan_list_apply_sort(desc=True)

    def _cmd_list_toggle_sort_direction(self):
        self._plan_list_apply_sort(desc=not self.plan_sort_desc)

    # ── Scroll and select commands ──

    def _cmd_list_scroll_up(self):
        """Previous page: 1-3, 4-6, 7-9, …"""
        if not self.plans:
            return
        new_start = max(0, self.browser_list_window_start - self.PLAN_LIST_VISIBLE_ROWS)
        if new_start != self.browser_list_window_start:
            self.browser_list_window_start = new_start
            self.index = -1
            self._invalidate_list_cache()
            self._log("list_scroll_up: page",
                      self.browser_list_window_start // self.PLAN_LIST_VISIBLE_ROWS + 1)
            self._set_status("READY")
            self._publish_state()

    def _cmd_list_scroll_down(self):
        """Next page: 1-3, 4-6, 7-9, … partial last page OK."""
        if not self.plans:
            return
        n = len(self.plans)
        max_w = self._plan_list_max_aligned_window_start(n)
        next_start = self.browser_list_window_start + self.PLAN_LIST_VISIBLE_ROWS
        new_start = min(next_start, max_w)
        if new_start != self.browser_list_window_start:
            self.browser_list_window_start = new_start
            self.index = -1
            self._invalidate_list_cache()
            self._log("list_scroll_down: page",
                      self.browser_list_window_start // self.PLAN_LIST_VISIBLE_ROWS + 1)
            self._set_status("READY")
            self._publish_state()

    def _cmd_list_select_row_1(self):
        self._cmd_list_select_row(1)

    def _cmd_list_select_row_2(self):
        self._cmd_list_select_row(2)

    def _cmd_list_select_row_3(self):
        self._cmd_list_select_row(3)

    def _cmd_list_select_row(self, row: int):
        """Tap a row to select that plan. Tapping the same row again keeps it selected."""
        pi = self.browser_list_window_start + (row - 1)
        if not self.plans or pi < 0 or pi >= len(self.plans):
            return
        self.index = pi
        self._log("list_select_row", row, "-> plan index", pi)
        self._invalidate_list_cache()
        self._set_status("READY")
        self._publish_state()
