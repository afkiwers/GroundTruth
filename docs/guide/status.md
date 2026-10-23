# Status and roadmap

What is done and what is planned. Back to the [README](../../README.md).

- **Done (latest, October 2026):**
  - the sources of every formula, method and theorem, and the model's assumptions, in [docs/REFERENCES.md](../../docs/REFERENCES.md) and in the help (References and assumptions); the code cites them where it uses them;
  - radiated limits other than CISPR 32: MIL-STD-461G RE102 built in, or a limit curve of your own; spread spectrum in the radiated estimate; MIL-STD-461G CE101;
  - power integrity: PDN impedance per IC against a target, the Power integrity tab;
  - voltage-aware safety checks: clearance by IPC-2221B, capacitor and FET voltage ratings, resistor power, ESD protection, voltage ranges of bootstrap and gate-drive nets;
  - signal integrity: a 2D cross-section solver, impedance, differential impedance and skew, crosstalk, via stubs, KiCad 10 tuning profiles, the Signal integrity tab; a power stage's switch node as a source of noise on other nets (victims);
  - frequency responses: Bode plots, poles and zeros of signal paths, power stages and PDN rails (vector fitting);
  - the control loop of buck and boost stages, with the controller's figures from its schematic fields;
  - DC rails with loads (the DC loads tab) and through-hole pins joined around their barrel;
  - spread-spectrum fields in the Converters tab.
- **Done (compute server):** analyses handed to one or more servers on the local network, a cluster that splits a Simulate by item and by each machine's memory and speed, a Windows service and a macOS launchd daemon, signed remote updates from the plugin.
- **Done (the plugin):**
  - **Preferences…** (per computer): Simulate's resolution (1–25× the nodes, also `--resolution`), solver threads, GPU, thermal quality, via markers, part outlines, the report's author and company, the theme; the working toggles remember their state;
  - Results tab: hover readout over issue circles and via/part markers, part outlines, every via and part with at least 1 % of the current marked, Sharper map, wheel zoom, cross-section along the route, plane displacement tile, 3D view in the browser;
  - **Export report…** (and `--format html`): one self-contained HTML report;
  - the simulation history: every Simulate kept with its board copy, settings, results and report; the History tab and `groundtruth history` compare runs;
  - **Help** (F1): the plugin's own help;
  - the Plugin and Content Manager package (`build_package.cmd`), the demo board in the plugin folder;
  - the optional GPU backend (PyTorch) with its installer and a package for another PC.
- **Done (motor bridges, milestones 1–2):**
  - detection of half-bridge legs and bridges, the Bridges tab, detection labels (`GT` symbol field);
  - the six-step drive model, each leg's hot loop, conducted emission at the input, bus-capacitor ripple;
  - losses, the RMS current density from battery to motor, thermal and current capacity, operating-point sweeps, fix suggestions for the legs;
  - victims on the same board (noise at microcontroller nets, injected current, ground shift).
- **Done (thermal):** copper and part temperatures of the power stages (PCB Thermal's solver), switching losses, current capacity.
- **Done (earlier):**
  - renamed from pcbemi to GroundTruth, with a new toolbar icon;
  - spread-spectrum switching (`spread`, `spread_mode`, `spread_rate`), with the EMI receiver simulated;
  - interleaved stages (`interleave`, `phase_deg`): a controller's phases added as phasors, conducted and radiated;
  - DC output path and ground return of power stages (`output_connector`), with the resistance split into copper and series parts (current-sense shunts);
  - fix search: each selected fix is checked at every harmonic near the limit on every net, and before/after comparisons are made at the same harmonics.
- **Done (M6, power stages):**
  - DC-DC hot loops with pours as solved copper;
  - capacitor current sharing;
  - ringing and overshoot estimates;
  - conducted emission through a LISN (CISPR 32, CISPR 25, MIL-STD-461);
  - converter detection and the plugin's Converters tab;
  - parallel solving (`analysis.threads`), receiver bandwidth, switching frequencies down to 10 kHz.
- **Done (M5 and plane capacitance):**
  - plane-pair capacitance from the physical stackup in a board-wide model solved at the signal's harmonics
  - the `plane_resonance` warning
  - the line-current emission model
  - topology-preserving rasterisation
  - what-if edits (vias, capacitors, fills) with `compare` and `--what-if`
  - `suggest` with placement rules, solver predictions and a verification run
  - the plugin's Fixes tab, including placing vias in KiCad
- **Done (M4):** multi-layer return paths, net auto-detection with review, and per-layer heatmaps with a list of the vias/parts carrying the return.
- **Done (M1–M3):**
  - parsing (any copper layer count), part models, settings file
  - Tier 1 checks
  - single-reference-plane solver with lumped bridging parts
  - CLI and CI reports
  - wx plugin
- **Tested live on KiCad 10.0.5**, on a scratch board, with `tools/dev/live_kicad.py`: reading the board, checks, simulation, fix search, drawing markers, contours and suggestions, selecting items, placing a via, and clearing. The installed plugin's toolbar button works: it opens the window on a real 4-layer board.
- **Not done yet:**
  - modelling an input filter as a what-if edit (needs a series part on the input path; until then, draw the filter into the design; the fix advisor's proposed filter is shown as frequency responses);
  - common-mode currents on cables;
  - the combined emission of interleaved stages (SW1 and SW2 of one controller are analysed separately);
  - H-bridges (two legs) as motor bridges;
  - stacked boards beyond the experimental two-board `stack:` (on hold: a stack of several boards with a leg on each needs a merge of N boards);
  - part bodies, heatsinks, an enclosure and mounting in the thermal model; part outlines in live KiCad mode (boxes around the pads until then).

The plugin window stays responsive. Reading the board, the analysis and every call into KiCad run in the background, and **Cancel** stops a running analysis. If KiCad shows a dialog or runs a tool in the PCB editor, it refuses all requests; the plugin then waits a few seconds and asks you to close the dialog or press Esc.

For contributors (and for picking the work up in a new session): [CLAUDE.md](../../CLAUDE.md),
[docs/HANDOFF.md](../../docs/HANDOFF.md) (status, open problems, next steps),
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) (physics, data flow, design decisions),
[docs/REFERENCES.md](../../docs/REFERENCES.md) (the sources of every formula, method and theorem, and the model's
assumptions, which the code cites where it uses them) and [docs/VALIDATION_AUDIT.md](../../docs/VALIDATION_AUDIT.md)
(the independent checks of the results).
