# Working in KiCad

Showing issues on the board, drawing on it, and fixes with what-if edits. Back to the [README](../../README.md).

## Showing an issue on the board (plugin)

Click an issue in the **Issues** tab and KiCad shows the copper it is about: the copper is selected, its net is highlighted (everything else dimmed) and the view zooms to it. Planes that matter are selected too, but without pulling the zoom out to them, and for plane issues the plane's layer becomes the active one, so a slot or an edge is drawn on top. This changes nothing in the design: no undo step, and the board stays unmodified.

| Issue | Shown |
|---|---|
| Gap crossing | The trace segments over the gap, plus the plane(s) on both sides of it (two nets for a split plane). |
| Off plane / edge proximity / other net's plane | The stretch of trace concerned, plus the plane that stops near it or carries the return. |
| No reference / no plane at all | Every segment of the net on that layer. |
| Layer change without a stitch or decap | The signal via. The nearest stitching via or bridging capacitor is in the zoom when it is close, and only selected when it is far. |
| Missing route | Both pins and the copper already on the net. |
| Simulation issues | The whole route and its pins. For a detour, also the via or part carrying most of the return. For a split plane, both return pins. |
| Power stages | The switches, the capacitors closing the hot loop and the switch-node copper (only the switch node is highlighted, never the rail or ground). For conducted emission, the input connector instead of the capacitors and switch node. For a current-density hotspot, the copper at the hotspot. |

Only the issue's own signal net (for power stages, the switch node) is highlighted. Stitching vias, return pins and planes are selected without lighting up their ground.

**Settings and status.**
- The status bar says what was shown, or that the copper is no longer on the board (re-run the checks after editing).
- **Zoom to it** and **Highlight its net** turn those steps off.
- **Show in KiCad**, a double-click or Enter shows the selected issue again after you moved the view in KiCad.

**What gets restored.**
- A net highlight you set yourself in KiCad stays until an issue brings its own. A highlight GroundTruth set is cleared by the next issue that has none, by `~` in KiCad, and when the GroundTruth window closes.
- Your active layer comes back with the next issue that has no layer of its own, and when the window closes.

**Limits.**
- A click while GroundTruth is drawing or reading in KiCad is not queued (it would move your view later): the status bar asks you to click again.
- In file mode (`groundtruth-gui`), a click only says in the status bar what the issue is about and where.
- The highlight uses KiCad's own editor actions, which KiCad does not promise to keep. A KiCad version without them still gets the selection, and the status bar names the missing action.

**Signal paths and power stages.** The **Signal paths** and **Converters** tabs work the same way. Clicking a net highlights it in KiCad: all its pads, tracks and vias are selected and lit, and its pours are selected. Clicking a power stage shows its switches and capacitors (once simulated) and highlights its whole power flow: the input path, the rail, the switch node, the inductor's output side and the output path to the output connector. Ground is never highlighted, because it would light up the whole board. Ctrl- or Shift-click selects several rows, and they are shown together. Deselecting every row clears GroundTruth's highlight. **Zoom KiCad to the selected nets / power stages** (under each table) turns the zoom off. In both tables, several selected rows can be removed at once with **Remove selected** or the Delete key, or marked reviewed with **Mark reviewed**; Ctrl+A selects all rows. **Update selected** edits one row at a time. The tick box at the start of a row (Nets, Converters and Bridges) switches it off without losing anything you entered: it is saved as `enabled: false`, shown greyed, and left out of Run checks, Simulate and the fix search (and not checked against the board). On the command line a switched-off row runs only when `--nets` names it, and the run lists the rows it left out.

**Export report.** **Export report…** in the top bar writes the results shown (the last Run checks or Simulate, the thermal and capacity views, the last fix search) as one self-contained HTML page: a cover, a summary with key figures, the main findings (one line per check: its counts, its worst case, a link to its rows) and every net, stage and bridge at a glance (operating point, margins, overshoot, efficiency), the board as an interactive view in the manner of InteractiveHtmlBom (front/back, a copper layer's pours and tracks, the parts' pads, outlines and references, the analysed routes and stages, every issue as a numbered marker; pan, zoom, a parts list that finds a part on the board), the issues (filter and sort in the browser), every net, power stage and motor bridge with its figures and tables, temperatures, fixes, and the method and limits. It prints as an A4 document (the browser's Print → PDF). The cover's lines come from the optional `report: {title, company, author, document}` section of the settings file; where it names no author or company, the plugin takes them from **Preferences…**. The CLI's `--format html` writes the same page.

**Simulation history.** Every **Simulate** in the plugin is kept in the board's `groundtruth/` folder, so earlier results stay at hand after the board or the settings changed:

```
groundtruth/history/
  boards/<YYYY-MM-DD_HH-MM-SS>.kicad_pcb.gz
                                   each distinct board once, zipped, named after the run that first kept it
                                   (later runs on the unchanged board use that copy)
  runs/<YYYY-MM-DD_HH-MM-SS>/
    run.json                       when and how it ran (resolution, what-if edits, time taken), which board and
                                   settings, the headline figures, your note
    results.json                   the results, as `simulate --format json`
    settings.groundtruth.yaml      the settings file as the run used it
    report.html                    the report page, as Export report writes it (made in the background
                                   while the window is left alone: its plots would make it stutter)
```

In KiCad the board copy is the open board with any edits not saved yet (a read-only request to KiCad; if KiCad cannot hand it over, the saved file, and the run says so). The **History** tab lists the runs, newest first, with what changed since the run before (board, settings, resolution, what-if), the errors, warnings and worst margins; it keeps a note per run, compares two runs (margins and loop inductance per net, stage and bridge, the issues fixed and new, the settings lines that changed), opens a run's report and board copy, and deletes runs with the board copies no other run needs. **Preferences…** turns keeping runs, or just their report page, off. On a 6-layer, 200-part board a run takes about 0.2 MB of results and 2.9 MB of report, plus 1 MB for the board copy when the board changed. The folder holds copies of the board and its results: share or check it in as you would the board. A board copy is unpacked only after it matches the SHA-256 its run recorded. Two boards in one folder share `groundtruth/history/`: each run records its board, and each board lists only its own runs. The CLI keeps a run with `simulate --history` and reads the folder with `groundtruth history`.

**Help.** **Help** in the top bar (or F1) opens the plugin's own help at the tab you are on: the workflow, what Simulate does with the nets, converters and bridges listed and how their results come together, every tab, Thermal, drawing, the detection labels, the settings file, speed and memory, and the limits of the numbers (`src/groundtruth/ui/help.html`; keep it current with the features).

**Look.** The window follows the Windows app setting, light or dark (PCB Thermal's palette), and **Theme** in **Preferences…** picks one for good (it applies the next time the window opens). The buttons are the plugin's own, with some volume; **Simulate** is the main action.

**Results tab layout.** The plots sit on a page that scrolls, each as wide as the window: the current maps first (they fill the window's height, or less for a wide board), then the cross-section along the route (with a caption under it that explains its plots: each layer's share of the return, and the green bars, the vias and parts that carry the return from one layer to another), then the emission spectrum. The mouse wheel scrolls the page; Ctrl + wheel over a map zooms it. The controls, the KPI tiles and the toolbar stay at the top. The plots' titles, labels and legends read at the size of the window's own text (9 to 10 pt; tick labels 8.5 pt, the bold labels on the maps 8 pt).

**KPI tiles.** The Results tab shows each view's key numbers as tiles, coloured by your rules: margins (red below 0 dB, amber within `rules.emi_margin_db`), overshoot, current density, via load. Hover over a tile for what it means. Moving the mouse over a map shows the value under it (A/m per A, A/mm² or °C), the layer and the position, next to the toolbar; over an issue's circle the issue (severity, check, message; in full as the map's tooltip), over a via's or part's marker its name and share of the current.

**Preferences.** **Preferences…** (top bar, next to Help) holds the user's settings on this computer, for every board, in `%LOCALAPPDATA%\GroundTruth\ui.json`: **Resolution** (below), **Solver threads**, **Use every core** (`--all-cores` on the CLI), **GPU** and the **Thermal quality** (each by default *From the settings file*: the board's `analysis.threads`, `analysis.gpu`, `thermal.quality`), **Via markers**, **Vias shown**, **Part outlines**, **Part names**, the report's **Author** and **Company** (where the board's `report:` names none), the **Theme**, whether every Simulate is kept in the board's history (and with its report page; see "Simulation history"), the **Compute server** (its address and key, **Test connection**, **Find servers**, and **Servers** with **Update** / **Update all**; see [Compute server](compute-server.md)), and under *Drawing on the board* the arrow line widths (**Thinnest / Thickest arrow line**, 0.01 / 0.3 mm) and the contour line widths (**Contour line** / **Index contour line**, 0.04 / 0.1 mm). OK keeps them and redraws the maps at once. The working toggles (Current arrows, Log scale, Cross-section, Zoom to it, Highlight its net) keep their last state there too.

**Resolution.** Simulate's grid on this computer: every model (routes, power stages, bridges, the board-wide plane model) gets 1, 2, 4, 8, 16, 20 or 25 times the nodes and cells 1/√ of that in size, also where the node budget does not bind (the cell starts at the dielectric height / √N; `analysis.resolution`, never written to the settings file). After a Simulate each level in Preferences shows its cell size, e.g. "4× (0.21 × 0.21 mm)", the coarsest model's. Memory and time grow about as much as the nodes: 2× to 4× on a laptop, 8× and up on a machine with lots of memory; a solve that runs out of memory (SuperLU's allocation too) is retried alone, then reported. The standard grid is converged for the numbers; finer grids sharpen the maps. **Sharper map** halves the cell of what was simulated (4× its nodes). The CLI: `groundtruth simulate|suggest|compare --resolution N`; the report names it.

**Part outlines and names.** **Part outlines** (Preferences) draws every part's outline over the maps: its courtyard (F/B.CrtYd), else its fabrication outline, else a box around its pads (live KiCad mode reads no outlines yet, so the boxes); top-side parts solid, bottom-side dashed. **Part names** writes each part's reference at its middle, with or without the outlines (on a busy map the names can crowd it). Preferences for every board, like **Vias shown**; the exported report follows them.

**Via loading.** Every via on a power stage's DC path is checked against its IPC-2221 capacity for a 10 K rise, from its drill and the plating (`analysis.via_plating_um`, default 25 µm): `via_overload` warns at `rules.via_load_warning` (100 %) and is an error at `rules.via_load_error` (150 %, about a 25 K rise). IPC-2221 is conservative; the thermal solve gives the actual temperature. A part's plated hole is not checked: its soldered pin carries the current between the layers, so the solver gives it the resistance of the hole's whole cross-section, not of the plating (a DNP part's hole stays plating only).

**Sharper map.** Each pixel of a heatmap is one cell of the solver's grid (0.35 mm for kraft_flux_mini_2's SW1 hot loop). **Sharper map** solves the shown view again with half the cell size (4x the nodes) in the background, about 20 s for a route and up to a minute for a power stage, and shows the result in its place. The numbers in the summary line stay as analysed: the default grid is already converged for them (SW1, 2026-10-18: 1.57 vs 1.53 nH, -19.8 vs -20.8 dB radiated margin at 4x the nodes), so the finer grid sharpens the picture, not the results.

**Larger heatmaps.** The layer maps are laid out to be as large as the region allows: side by side for a tall, narrow region, in a grid for a square or wide one. Zooming or panning one map (toolbar) shows the same spot on every layer.

**What carries the current.** On every heatmap, a green ▲ marks a via and a green ■ a part that carries the current to another layer or net: every one with at least 1 % of the current (of a DC path's or a bridge's RMS current, or of a route's return), with its share next to it from 5 % up ("67 %"). A carrying via is also drawn at its true size (pad ring and drill), which shows when you zoom in. **Via markers** (Preferences) unticked hides them all (parts' ■ and issue circles stay); **Vias shown** limits them to the 5, 10, 20 or 50 busiest; both are a preference for every board, kept with the theme in `%LOCALAPPDATA%\GroundTruth\ui.json`, which the exported report follows too. The checks (current density, via overload) look at every via whatever is shown. The cross-section's side view draws the eight biggest. Open circles mark issues on the net (orange: warning, red: error).

**Plane displacement.** With `analysis.plane_capacitance` on, current also crosses between two facing planes through their capacitance. That current is spread over the whole plane pair and comes from the board-wide model at the worst harmonic, so it has no marker on the map; the tile **plane displacement** gives it (e.g. "12.9× signal, In1.Cu–In2.Cu @ 1450 MHz"), and so do the report's transfer list and the text output. Above 1× it is a resonance: the plane capacitance and the vias joining the planes form a tank, and a current several times the signal current circulates through both, in antiphase (they still add up to what has to cross, `tools/dev/transfer_check.py` shows it frequency by frequency). On the demo board the two GND planes, joined only at the ICs, resonate at 1.45 GHz; on kraft_flux_mini_2 the GND pour on F.Cu and the +3V3 plane on In1 by C1105 and C1110 resonate at 1020 MHz (at 2× and 4× resolution; 940 MHz at 1×) with 4.6× the signal current across them. The model has no radiation loss, so the height of such a peak is an upper estimate. **Main transfer** is always the via or part carrying most of the return.

**Cross-section.** Below the maps, a signal route's return current along the route, layer by layer, driver on the left. Top: the share of the return each plane layer carries past each point of the route within ±20 dielectric heights of the trace (1 to 5 mm), and their sum (dashed, Σ): where the sum dips, the return has left the route. Bottom: a side view of the copper layers, each plane coloured by its share (red: flowing back as the return, blue: flowing with the signal), the signal on its layers with its layer changes, and the vias and parts carrying the return between layers as green bars with their share (dashed with their distance when they are far from the route). It shows at a glance whether the return follows the signal from plane to plane: with a stitching via next to a layer change it swaps planes right there; without one it crosses over gradually and part of it stays on the plane the signal has left. Shares are the part in phase with the signal. The mouse over it gives every layer's share at that point. Untick **Cross-section** to leave it out. Power stages have no route, so no cross-section; the HTML report has it for each net (and `--format html` also writes `section_<net>_<receiver>.png`).

**3D view.** **Open 3D view** (Results tab) shows the selected view's layers stacked in 3D in your web browser: each plane layer's current map with its arrows (the heatmap's colours, more transparent where less current flows), the board outline, the signal on its layers with its layer changes (grey bars), and the vias and parts carrying the current between layers (green bars with their share). Drag to turn and tilt, wheel to zoom, double-click to reset; sliders set the layer spacing (exaggerated: a board is a millimetre thick) and the maps' opacity, and checkboxes hide layers. With plane capacitance there is a second view, **Board-wide model**: that model's plane currents at the worst harmonic, the displacement current between each pair of planes (red: downwards, blue: upwards) and the vias' currents, so a "plane displacement 12.9× signal" can be seen circulating between the planes and the vias that join them. The page is built in the background, written to `%LOCALAPPDATA%\GroundTruth\views\view3d_<net>_<receiver>.html` (overwritten for the same view) and needs no internet: everything is in the file, and its content policy lets it load nothing else. It holds the board's maps and names: share it like the board. `--format html` writes the same pages next to the report and links them (add `#board` to open the board-wide view).

**Wheel zoom.** Hold Ctrl and turn the mouse wheel over a map to zoom in or out about the point under the mouse (in the **Open in window** window the wheel alone zooms); all layers follow. Each zoom is drawn again (in the background, so the window keeps responding) with the current arrows laid out for what is in view: zooming in shows the flow in more detail, not the same arrows larger. The colours keep the whole map's scale. Double-click a map, or press the toolbar's Home button, to see the whole map again. Changing the layer keeps the zoom; another route starts with its whole map. The colour field itself is the solver's grid (one cell per node), so close up it shows the cells; **Sharper map** solves the view on a finer grid. **Open in window** shows the heatmap in a window of its own that you can enlarge or maximise; it follows the route, layer and arrows chosen in the tab.

**Drawing layers.** Markers, suggestions, contours and arrows go on User.9, User.8 and User.7 (settings: `drawing`). A drawing layer the board does not have yet is enabled on the board first, and the status line says so. That is a change to the board setup, which Clear GT drawings does not take back: disable the layer in Board Setup › Board Editor Layers if you want it gone. (Without it, KiCad would create the shapes invisibly and refuse to group them, and only Undo would remove them.)

**Unusual values are used as written** (your value is the value) and listed as the info issue `unusual_value`, with what to write if a unit is missing: a C_oss of `482` is read as 482 F (`482p`?), a rise time of `2` as 2 s (`2n`?), a switching frequency of `750` as 750 Hz (`750k`?). Nothing blocks a run; a stage with such a value gives results that follow from it.

**Parts follow KiCad.** While the **Parts** tab shows, clicking a part (or several) in KiCad's PCB editor selects its rows in the Parts table, with the cursor on its Model cell, so you can edit it right away. A filter that hides the part is cleared. The window checks KiCad's selection twice a second, and only while that tab shows.

**Long status messages** (a refused net's reason, for example) wrap onto up to three lines; the status bar grows for them, and the whole text is also in its tooltip.

**How far a job is.** While something runs (Simulate, Suggest fixes, Thermal, …), the status line reads "what it is doing · 37 % · 1:12 · about 2 min left": the elapsed time counts every second, also through a long single step, and the estimate appears after the first few percent and is smoothed so it does not jump. A job that took 10 s or more ends with "took m:ss". Simulate and the fix search split the bar by how much solver work each step is (a solve on a large model counts more than one on a small model), and their messages count the steps: "evaluating candidates: SPI_MOSI, add a GND stitching via (3 of 12)", "solve 7 of at most 24" (refinement can finish early, then the bar jumps ahead).

**GPU.** With PyTorch installed on a machine with an NVIDIA (or, on Linux, AMD) GPU, the solver's iterations run
there: see "GPU acceleration (optional)" under Install for what to install and how. The status line after Simulate
or Suggest fixes ends with "solved on GPU <name>" when the GPU did the work; a solve the GPU cannot finish (out of
its memory, say) is done on the CPU and counted there.

**Drain and source.** A power stage's heatmaps mark its switches' power pads: D (drain, cyan) and S (source, green) on a FET, K / A on a diode, VIN / SW / GND on an integrated stage, told from the pads' nets. The pads are outlined on their own layer; the letters and the part's name show on every layer, so you can see where a switch sits above an inner plane.

## On-board drawing (plugin)

All drawing happens in one undo step. The items are grouped in `groundtruth-*` groups, so **Clear GT drawings** removes exactly what the plugin drew. The layers can be changed under `drawing:` in the settings file.

- `User.9`: issue markers (circle plus number, severity and check).
- `User.8`: suggested fixes (cross plus id, kind and predicted change).
- `User.7`: a contour map (**Draw contours on board**) like a topographic map's height lines: lines at round values of the current density (1-2-5 steps over three decades below the peak; on a thermal view isotherms in °C), each decade drawn thicker, with the values written along the lines and a legend line above the board; and current arrows (**Draw arrows on board**), thicker where the current density is higher. Both for the route and layer selected in the Results tab.

**Current arrows.** Every heatmap (plugin, HTML report, CLI PNGs) carries small arrows, all the same size, that point the way the current flows. Their number shows the current: where it concentrates, the arrows crowd together; where little flows, only a few are scattered; idle copper has none. Like field lines, the arrow density follows the current density (square-root compressed, so a return current that falls off by decades beside its trace still shows its spread). All layers of a view share one scale, so a layer carrying little current also shows few arrows. Untick **Current arrows** in the Results tab to hide them. AC currents (return currents, hot loops) are drawn at the phase where they are largest, in the direction of the signal's own current; DC paths are drawn as they are. On the board the arrows are line segments on the current-drawing layer, removed by **Clear GT drawings** like everything else the plugin draws; there their line width shows the density too, from the thinnest width three decades below the peak to the thickest at the peak (Preferences).

**Click a marker.** After a Simulate, clicking a marker on a map (an issue's circle, an overloaded via, a via or part carrying current) shows it in KiCad: the via, pad or track at that point is selected and its net lit (not a ground net), as the Issues tab does for an issue. Each view marks only its own issues: a DC path's map shows that path's overloaded vias, not another path's.

**Log scale.** The heatmap colours span three decades below the peak, so even weak current spread over a whole plane shows (a return that has to detour to a far via, or cross between planes through their capacitance, fans out like field lines). Untick **Log scale** in the Results tab for a linear scale from 0 to the peak: it shows where most of the current flows. Temperature views are always linear. `tools/dev/stitch_contrast.py` draws the same route without and with a stitching via at its layer change, on both scales.

Vias placed from the Fixes tab are real design changes. They are not grouped, so **Clear GT drawings** leaves them alone.

## Fix suggestions and what-if edits

`groundtruth suggest` (or **Suggest fixes** on the plugin's Fixes tab) works through the issues it finds:

| Issue | Suggested fix |
|---|---|
| Layer change, same reference net, no stitching via | a stitching via of that net next to the signal via |
| Layer change between different nets, no decap | a capacitor between the two nets next to the signal via |
| Trace crossing a split between two nets | a bridge capacitor across the split next to the crossing |
| Trace crossing a slot in its own plane | a copper fill that closes the slot |
| Return detour, plane resonance or EMI margin | stitching vias at the route's layer changes, plus advice |
| Off-plane, edge proximity, non-ground reference | advice text only |

1. **Placement:** candidates are placed only where they would be legal. They must lie inside the board outline, keep 0.2 mm from other nets' tracks, pads and vias, and land on the right plane copper. Other nets' zones are not obstacles, because zones refill around a new via.
2. **Prediction:** each candidate's change in loop inductance comes from the route model at the fundamental. Its change in loop moment at the worst harmonic includes the plane-capacitance model, so a fix that damps or moves a plane resonance is judged where it matters. A via or capacitor only adds a branch to the models, so the prediction reuses the baseline factorisations and starts from the baseline solution.
3. **Selection:** the best fix per issue is kept, up to `--max-fixes` per net. A fix predicted to raise the emission by more than 1 dB is never selected, even when it would satisfy a rule. That happens, for example, when a stitching via moves a plane resonance onto a harmonic. Plane resonances are shared by the whole board, so each selected fix is checked again at every harmonic within 20 dB of the warning level, on every net. A fix is dropped when it lowers its own net's worst margin by more than 1 dB. It is also dropped when it lowers another net's worst margin by more than 1 dB and either that loss is bigger than its own gain or the other net falls below the warning level. Smaller trade-offs are kept, and the note says so (for example "worst margin +3.6 → +11.4 dB; also lowers /CPU/SPI_SCK's worst margin +10.8 → +9.2 dB"). Unselected candidates stay in the list with a note.
4. **Verification:** the selected fixes are applied to a copy of the board, and the analysis runs again for everything they can change: the nets and power stages that got a fix, every net within 20 dB of the warning level (a via or capacitor anywhere on the board can move a plane resonance onto its harmonics), and every power stage with a fix inside its model. That gives before/after numbers and lists the issues that are fixed or new. Nets far below the limit and power stages no fix touches keep their results; their rule checks run again on the modified board, and the comparison lists them as "not simulated again". `--verify-all` simulates everything again.

`--write` stores the suggestions in the settings file under `modifications:`, with the selected ones enabled and the prediction in the note. You can also write the entries by hand:

```yaml
modifications:
  - {type: via, at: [121.0, 122.6], net: GND, size: 0.6, drill: 0.3, enabled: true}
  - {type: cap, at: [118.4, 120.1], nets: [GND, +3V3], layer: F.Cu, C: 100n, ESR: 0.02, ESL: 0.4n, enabled: true}
  - {type: fill, layer: In2.Cu, net: GND, polygon: [[110, 120], [114, 120], [114, 123], [110, 123]], enabled: false}
```

Positions are in mm. A via's `size` and `drill` default to 0.6 and 0.3 mm, and `layers: [first, last]` makes it a
blind or buried via (default: through). A capacitor defaults to `layer: F.Cu`, 100 nF, ESR 0.02 Ω and ESL 0.4 nH,
with `angle` its rotation in degrees. A fill needs at least three polygon points. `enabled` defaults to true;
`source: suggested` and `note` are written by `suggest --write`.

What-if edits are applied only by `compare`, `check/simulate --what-if` and the plugin's **with what-if edits** switch. They become ordinary board items on a copy (a via, a two-pad capacitor with its own model, a zone), so every check treats them like real copper. `groundtruth compare` prints per route the loop inductance ratio, the moment, the worst margin and the plane resonance, before and after, plus the issues fixed and introduced. The worst margins are compared at the same harmonics: each run picks its own harmonics to solve, so both runs are also solved at the other's worst harmonic. When that changes a value, the run's own figure is shown in brackets.

On the plugin's **Fixes** tab you can:
- tick suggestions and draw them on `User.8` as crosses with labels,
- place the ticked vias in KiCad as real vias in one undo step, after a confirmation, then refill the zones and run DRC,
- add them to the settings file's what-if edits,
- compare the board with and without the enabled edits.

Capacitors and fills are not placed automatically: a capacitor belongs in the schematic, and a fill changes zone priorities.
