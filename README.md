# X-Plane FMS Companion

A standalone XPPython3 plugin that acts as a copilot assistant for the stock X-Plane FMS and G1000. It loads flight plans, validates them, helps you set departure and arrival procedures, and monitors route state — all through a plugin window inside the sim.

---

## What it does

- **Load flight plans** from `Output/FMS plans/` and push them into the X-Plane FMS
- **Browse SID / STAR / approach** procedures from CIFP data and insert them into your route
- **Validate the route** on load — missing procedures, discontinuities, duplicate fixes, suspicious jumps
- **Monitor route state** in flight — cross-track error, active leg plausibility, advisory messages
- **Estimate fuel and time** to destination based on current ground speed and burn rate
- **Suggest runway / approach** based on the sim's surface wind model (advisory only)

## What it is not

- Not a replacement FMS or avionics system
- Not a Garmin G1000 emulator
- Not an autopilot or LNAV/VNAV engine
- Not a Cockpitdecks integration (by design — it runs fully standalone)

The G1000 flies the route. This plugin watches it, checks your work, and tells you what to fix.

---

## Requirements

- X-Plane 11 or 12
- [XPPython3](https://xppython3.readthedocs.io/) installed
- `xp_imgui` (XPPython3 ImGui wrapper) for the plugin window

Install `xp_imgui` via the XPPython3 package installer. The plugin loads without it but the window will be disabled.

---

## Installation

1. Clone or download this repository.
2. Run the deploy script to copy the plugin into your X-Plane installation:

```bash
python scripts/deploy.py
```

Or copy manually:
- `PI_FMSCompanion.py` → `X-Plane/Resources/plugins/PythonPlugins/`
- `fmscompanion/` → `X-Plane/Resources/plugins/PythonPlugins/fmscompanion/`

3. Start or restart X-Plane. The plugin appears under **Plugins → X-Plane FMS Companion → Show / Hide FMS Window**.

---

## Plugin window tabs

| Tab | Purpose |
|---|---|
| **NAV** | Live flight data: active waypoint, XTK, DTK, GS, ETE, BRG, TRK, ETA |
| **ROUTE** | Scrollable FMS legs list — select, activate, direct-to, clear |
| **LOAD** | `.fms` file browser — sort, preview, load into FMS |
| **PROC** | SID / STAR / approach browser — select and insert procedures |
| **CHECK** | Route validation issues with severity, messages, and suggested actions |
| **FUEL** | ETE to destination, ETA, fuel on board, burn rate, estimated destination fuel |

---

## Project structure

```
PI_FMSCompanion.py        ← XPPython3 entry point
fmscompanion/
  models.py               ← All dataclasses (route, procedures, validation, nav status, fuel)
  fms_io.py               ← FMS file parsing and loading into the XP FMS SDK
  fms_state.py            ← Live FMS state reads (active leg, entry count, idents)
  procedures.py           ← CIFP parsing, procedure browser, FMS insertion
  legs.py                 ← Scrollable LEGS list with selection/activation/direct-to
  plan_browser.py         ← Plan list display, sorting, row selection
  ui.py                   ← Dear ImGui window and tab layout
scripts/
  deploy.py               ← Deployment helper
```

---

## Design principles

**Advisory, not automatic.** The plugin flags problems and suggests fixes. You press the button; it does not act without you.

**Cockpitdecks-independent.** This plugin registers no datarefs and exposes no commands. It is a self-contained tool. Cockpitdecks integration remains available separately in `CockpitdecksFMS/`.

**No pretending to control internal G1000 logic.** Approach arming, CDI mode switching, and LOC capture happen inside Laminar's avionics. This plugin works at the flight plan level — it cannot reach below that.

**Testable core.** `validator.py`, `fuel_time.py`, and the data model have no X-Plane dependency and can be tested with plain Python and sample `.fms` files.

---

## Roadmap

**MVP (current focus)**
- Standalone plugin with LOAD, NAV, ROUTE, PROC, CHECK, FUEL tabs
- Route validation on load
- Procedure browser and insertion

**Phase 2**
- Off-course detection with correction suggestions
- Active leg plausibility checks
- Wind-based approach and runway ranking

**Phase 3**
- Procedure mismatch detection against CIFP
- Departure transition auto-suggestion from the .fms runway
- Action buttons for safe auto-fixes (advance active leg, clear duplicate)

**Future**
- Web deck UI for tablet use
- Dataref/command exposure for external tool integration

---

## License

MIT — see [LICENSE](LICENSE).
