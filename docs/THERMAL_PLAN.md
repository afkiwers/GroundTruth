# Plan: bringing PCB Thermal into GroundTruth (for review)

2026-10-05. **Status: M1 and M2 are built and merged into `main`** (3c30bb2; HANDOFF
"Session 5"). **M3 (session 6, merged into `main` at 8d99984):** items 1 (current capacity, plus "all power stages" scaled
together) and 7 (memory watchdog) are built; the others are open. Decisions taken: the theme follows the Windows setting with a Theme switch in the top bar; drawn
buttons "with more volume"; option A with the adapter; numba included (it installs without touching numpy);
defaults 25 °C ambient, natural convection, limits 85 / 105 °C, normal quality. **M3 below is what is
outstanding.** The original plan follows; the decisions marked **Decide** were answered as above.

**Source:** the author's PCB Thermal app (package `pcbsim`, "PCB Thermal",
about 7 200 lines: a coupled electro-thermal solver with analytic validation tests, and a PySide6/Qt UI).
The folder has no licence file; this plan assumes the code is yours to reuse.

## What GroundTruth gains

| From PCB Thermal | In GroundTruth | Milestone |
|---|---|---|
| Dark theme, cards, KPI tiles | The plugin restyled in its palette | M1 |
| Hover readout | Value under the mouse on every heatmap | M1 |
| Via loading vs IPC-2221 capacity | Pass/fail per via on the DC paths GroundTruth already solves | M1 |
| Coupled electro-thermal solve (3D conduction, convection, radiation, adaptive mesh) | **Copper and part temperatures of each power stage at its operating point**, so "131 A/mm² at the L301 → J104 neck" becomes a temperature rise you can judge | M2 |
| Its roadmap: current-capacity solve, A/B difference maps, sweeps | "Max current for ΔT = 20 K", what a layout change bought | M3, optional |

## M1: styling and quick wins (no new physics)

**Dark theme.** Both the dev environment and the plugin's environment inside KiCad run wxPython 4.3.1 on
wxWidgets 3.3.3, which has native Windows dark mode (`wx.App.MSWEnableDarkMode`): lists, buttons, tabs,
scrollbars and dialogs draw dark, not only the panels around them.
- `ui/theme.py` with PCB Thermal's palette: background #14161b, panel #1b1e24, card #21252d, border #2e333d,
  text #e7e9ee, muted #9aa3b2, accent #4cc2ff, warn #ffcc66, error #ff6b6b, ok #7bd88f. RGB tuples only: the
  start-up worker imports the modules, so no wx objects at import time (CLAUDE.md rule).
- A `Card` panel (rounded border drawn on paint, title, muted subtitle) for the forms on the Nets and
  Converters tabs and for the metrics.
- matplotlib in the same palette (figure and axes background, text, grid); the inferno colour map already reads
  well on dark.
- Tabs stay native (dark mode draws them dark). PCB Thermal's accent-underlined tabs would need a custom tab
  bar: possible later, not worth it now.
- Risks: dark mode is marked experimental in wx 3.3. The Parts tab's `wx.grid.Grid` and list headers may need
  colours set by hand. Checked by running every tab on the real board and with `tools/dev/gui_probe.py`.
- **Decide:** always dark, or follow the Windows app setting (with a toggle)?

**KPI tiles** replace the Heatmap tab's one long summary line. A big value, a muted sub-line, coloured by margin:
red below 0 dB, amber within `rules.emi_margin_db`, green above.
- Route: return share on the main layer, loop L and ×ideal, worst radiated margin.
- Power stage: hot loop nH, L·di/dt, ringing, conducted / radiated margin, common-mode budget.
- DC path: R, drop and loss, sustained A/mm², the busiest via.

**Hover readout:** moving over a map shows the value under the cursor (|K| in A/m per A, or A/mm² for DC
views), the layer and the position, in the toolbar's message area. It is an array lookup, cheap enough for the
GUI thread.

**Via loading:** `dc_summary` already lists the five busiest vias of each DC path with their current. Add the
IPC-2221 capacity of each (PCB Thermal's `via_capacity`: drill and plating, 10 K rise), shown as % of capacity,
with an issue above 100 %. The plating becomes a setting (`analysis.via_plating_um`, default 25 µm, today the
constant `VIA_PLATING_M`). IPC-2221 is the conservative older standard; PCB Thermal uses it too. M2 replaces it
with the computed temperature.

Size: about one session. Tests: via capacity against a hand-computed value, hover lookup, a dark-mode GUI
smoke test, gui_probe.

## M2: thermal solve of the power stages

**What it answers:** for each power stage at its operating point (`i_out`, `v_in`, duty), the copper temperature
per layer (map and hot spot) and the temperature of its parts (FETs, inductor, shunts, the surge stopper's FET,
with a junction estimate where θJB is known). Issues when a limit is passed.

### How the heat gets in (the key decision)

GroundTruth already solves each stage's DC paths with **prescribed** currents: switch node → inductor (I_out),
input connector → high side (D·I_out), output and ground returns with the switching split (D·I through the
input connector's − pin, (1−D)·I through the low side).

**Option A (recommended): GroundTruth's DC currents drive PCB Thermal's thermal model.**
- Each DC path's current-density vectors (Jx, Jy in A/mm², already computed for the arrows) are resampled onto
  one grid per layer and **added as vectors**. Paths share copper (GND carries both returns), so adding squared
  densities would be wrong. Heat per cell: ρ·|J|²·t.
- Part losses go in as component powers, through the part's pads like PCB Thermal does.
- Copper resistivity follows temperature (α = 0.00393 /K): with the path currents prescribed, the heat per
  cell is scaled by 1 + α(T − 20 °C) and the thermal solve repeated, 2 to 4 times. That is first order: the
  current pattern itself is not re-solved with temperature-dependent conductivity (GroundTruth's PEEC grid has
  one conductivity per layer). Fine for pours, conservative for necks.
- Why: it keeps the switching split, which PCB Thermal's terminal model cannot express (it splits current
  between sinks by resistance alone). And the temperatures match the density maps you already look at.

**Option B: PCB Thermal's own electrical model**, with terminals on the stage's connectors and resistor links
for the parts. Less wiring and its validated cut-cell accuracy on thin traces. But the return split would be
wrong for a switching stage, and it would duplicate GroundTruth's DC model. Better kept for a later feature: "a
DC current path from these pads to those" (not tied to a converter).

### Board geometry

PCB Thermal reads the `.kicad_pcb` itself. In KiCad, GroundTruth reads the live board through the API, and the
saved file may be out of date.
- **Recommended:** an adapter from GroundTruth's `Board` to PCB Thermal's `Board`. The models are close:
  stackup, outline, footprints with pads, tracks, vias, filled copper. GroundTruth's pads carry polygons from the
  API. Missing on GroundTruth's side: non-plated holes (the file reader skips NPTH pads today), solder-mask
  openings (for the emissivity map: default to mask everywhere at first) and copper graphics.
- Fallback: PCB Thermal's parser on the saved file, refused when the board was changed since the last save.

### Inputs (new)

- Per stage, on the Converters tab: high- and low-side R_DS(on); inductor DCR; switching loss from what is
  already there (½·V·I·(t_r + t_f)·f_sw + C_oss·V²·f_sw). Shunts and the surge-stopper FET use their part
  models (Q601: 6.5 mΩ, I_in²·R). θJB per part, optional.
- A `thermal:` settings section, PCB Thermal's environment as is: ambient, board orientation,
  natural / forced / fixed convection, radiation, optional enclosure, mesh quality (draft / normal / fine), and
  the limits for the issues.
- **Decide:** ambient and limits for your work (e.g. 70 °C ambient, max ΔT 40 K or T_max 105 °C), and how
  refboard is mounted (open air, enclosure, standoffs to a chassis).

### Running it

- In PCB Thermal's worker process: below-normal priority, cancellable within a second, memory watchdog at 60 %
  of RAM. That fits the plugin's never-freeze rule better than a thread: the solve is heavy (its stress board:
  Normal 28 s / 1.3 GB, Fine 3.7 min / 4.3 GB).
- The plugin shows progress in the status bar. The CLI gets `groundtruth simulate --thermal`, and the HTML
  report a temperature section.

### Results in the plugin

- Temperature views per layer in the Heatmap tab (same layout, hot-spot marker, KPI tiles: T_max, hot spot,
  total loss, heat out by convection and radiation).
- A parts table with temperatures and junction estimates.
- Issues `thermal_hotspot` and `part_temperature`.
- "Draw isotherms on board", like the current contours.

### Code and dependencies

- PCB Thermal's solver (no UI) goes to `src/groundtruth/thermal/`: `kicad/board.py`, `solver/*`,
  `materials.py`, the setup data classes. Its validation tests go to `tests/thermal/`. kipy stays in the
  adapters (unchanged rule).
- New dependencies:
  - `pyamg`: needs scipy, wheels exist.
  - `psutil`.
  - `numba`: about 70 MB with llvmlite, only for three rasterisation kernels. Risk: numba lags numpy releases,
    and the plugin environment has numpy 2.4.2. If numba does not support it, installing it would downgrade
    numpy or fail. The fallback is to rewrite those three kernels with numpy or GroundTruth's own rasteriser.
  - All as an optional extra `groundtruth-pcb[thermal]`: CI without it keeps working, and the CLI says how to
    install it.
- **Decide:** is the larger plugin environment acceptable (about 80 MB with numba, or about 15 MB without)?

### Validation (CLAUDE.md: every physics feature against an analytic result)

- PCB Thermal's own cases come along: fin profile vs the analytic cosh solution, energy balance < 1e-6,
  convection correlations, via spreading mesh-independence.
- New: a straight strip heated by GroundTruth's resampled DC current vs the same strip in PCB Thermal's own
  electrical model, temperatures within a few %. Energy balance with the resampled heat (Σ ρ|J|²t·A = the
  path's I²R).
- Grid convergence on refboard: SW1's output path at Draft and Normal.

Size: two to three sessions.

## M3 (outstanding, optional)

1. **Current-capacity solve:** the load current that gives a chosen rise (e.g. ΔT = 40 K) per power stage, with
   the thermal model built once and only the heat rescaled (losses scale with I², switching with I).
2. **A/B difference maps:** ΔT and Δ|J| between two runs (a what-if edit against the plain board; the plugin
   already pairs those runs for Compare).
3. **Sweeps:** ambient, airflow and operating points as a batch (a table of hot-spot temperatures).
4. **Free DC path (option B):** a DC current between chosen pads, not tied to a power stage, with PCB Thermal's
   own electrical model (terminals and links), e.g. a heater, a bus bar, a connector to a load.
5. **Component bodies and heatsinks:** two-resistor JEDEC model (θJC, θJB) and a simple heatsink, so parts do not
   only lose heat through the board (today's results are conservative for large parts).
6. **Enclosure and mounting** in the settings (PCB Thermal's lumped enclosure and mounts exist in the solver
   but are not exposed yet), and solder-mask openings for the emissivity map.
7. **Memory watchdog for the thermal process** (PCB Thermal stops a run above 60 % of RAM): the plugin already
   solves in a child process (below-normal priority, Cancel kills it), without a watchdog yet.
8. **Sharper thermal maps:** show the adaptive mesh's refined patches in the heatmap (today the level-0 grid).

## Order

M1 → review → M2 (A + adapter) → review → M3 if wanted. M1 does not depend on the thermal decisions, so it can
start as soon as the theme question is answered.
