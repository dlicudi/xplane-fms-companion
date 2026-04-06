"""NavMonitorMixin — periodic flight-loop checks for nav advisories and fuel state.

Runs every 2 seconds via an XP flight loop. Reads live datarefs for:
  - Cross-track error (CDI deflection dots) → OFF COURSE advisory
  - Ground speed → suppress advisories on ground
  - Total fuel mass → available for the FUEL tab
  - Total fuel flow → available for the FUEL tab

All state is stored on self so UIMixin can read it during draw callbacks.
No coupling to the validator — nav_monitor works in flight, validator runs at load.
"""

from XPPython3 import xp

# Threshold for off-course advisory in CDI deflection dots.
# GPS LNAV full-scale = 2 dots (≈ 2 nm for en-route, ≈ 0.3 nm for approach).
# 1.5 dots is a useful early warning without being too noisy.
_XTK_WARN_DOTS = 1.5

# Minimum ground speed (kt) before we treat the aircraft as airborne.
# Suppresses advisory chatter while taxiing or sitting on the ramp.
_GS_AIRBORNE_KT = 40.0

# Scalar dataref paths
_NM_DREF_PATHS = {
    "xtk":     "sim/cockpit/radios/gps_course_deviation",  # CDI dots (signed, +R/-L)
    "gs":      "sim/cockpit2/gauges/indicators/ground_speed_kt",
    "fuel_kg": "sim/flightmodel/weight/m_fuel_total",       # total fuel remaining, kg
}

# Per-engine array dref — must be read with getDatavf and summed
_FLOW_DREF_PATH = "sim/cockpit2/engine/indicators/fuel_flow_kg_sec"  # [8], kg/s per engine


class NavMonitorMixin:
    """Mixin that runs a background flight loop to generate nav/fuel advisories."""

    def _nav_monitor_init(self):
        """Call from __init__ before any XP APIs are available."""
        self._nm_drefs: dict = {}
        self._nm_flow_ref = None          # array dref, handled separately
        self._nm_loop_ref = None
        self.nav_advisories: list = []   # list of str, read by UIMixin
        self.fuel_on_board_kg: float = 0.0
        self.fuel_flow_kg_s:   float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _nav_monitor_start(self):
        """Resolve datarefs and schedule the flight loop. Call from XPluginEnable."""
        self._nm_resolve_drefs()
        self._nm_loop_ref = xp.createFlightLoop(self._nm_flight_loop, phase=0)
        xp.scheduleFlightLoop(self._nm_loop_ref, 2.0, 1)
        self._log("NavMonitor: started (2 s interval)")

    def _nav_monitor_stop(self):
        """Destroy the flight loop. Call from XPluginDisable."""
        if self._nm_loop_ref is not None:
            try:
                xp.destroyFlightLoop(self._nm_loop_ref)
            except Exception:
                pass
            self._nm_loop_ref = None
        self.nav_advisories = []
        self._log("NavMonitor: stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _nm_resolve_drefs(self):
        for key, path in _NM_DREF_PATHS.items():
            try:
                ref = xp.findDataRef(path)
                self._nm_drefs[key] = ref if ref else None
            except Exception:
                self._nm_drefs[key] = None
        try:
            ref = xp.findDataRef(_FLOW_DREF_PATH)
            self._nm_flow_ref = ref if ref else None
        except Exception:
            self._nm_flow_ref = None
        self._log("NavMonitor drefs resolved:", {k: bool(v) for k, v in self._nm_drefs.items()},
                  "flow:", bool(self._nm_flow_ref))

    def _nm_read_fuel_flow(self) -> float:
        """Sum per-engine fuel flow array (kg/s total across all engines)."""
        if not self._nm_flow_ref:
            return 0.0
        try:
            out = []
            xp.getDatavf(self._nm_flow_ref, out, 0, 8)
            return sum(v for v in out if v > 0)
        except Exception:
            return 0.0

    def _nm_getf(self, key: str, default: float = 0.0) -> float:
        ref = self._nm_drefs.get(key)
        if not ref:
            return default
        try:
            return xp.getDataf(ref)
        except Exception:
            return default

    def _nm_flight_loop(self, sinceLast, elapsedTime, counter, refcon):
        try:
            self._nm_update()
        except Exception as exc:
            self._log("NavMonitor loop error:", exc)
        return 2.0  # reschedule in 2 seconds

    def _nm_update(self):
        gs_kt = self._nm_getf("gs")
        xtk   = self._nm_getf("xtk")

        advisories = []

        if gs_kt >= _GS_AIRBORNE_KT:
            if abs(xtk) > _XTK_WARN_DOTS:
                side = "R" if xtk > 0 else "L"
                advisories.append(f"OFF COURSE  {abs(xtk):.1f} dot {side}")

        self.fuel_on_board_kg = self._nm_getf("fuel_kg")
        self.fuel_flow_kg_s   = self._nm_read_fuel_flow()
        self.nav_advisories   = advisories
