# groundtruth architecture

How the code is organised, what the physics is, and why things were decided the way they are.
Status and next steps are in [HANDOFF.md](HANDOFF.md).

## Data flow

```
.kicad_pcb ──file_source──┐                         ┌── rules/checks.py (Tier 1) ──┐
                          ├─> core.board.Board ─────┤                              ├─> Issues ─> report/* (text, json, junit,
KiCad (IPC) ─kipy_source──┘        + settings file        └── analysis/simulate.py ──────┘            gitlab-codequality, html)
                                   (config)              (Tier 2, solver/peec.py)               ui/* (plugin), kipy_sink (draw)
```

- `core/board.py`: KiCad-independent dataclasses: `Board`, `Stackup` (copper layers with z, dielectrics
  with thickness, εr and tan δ), `Track` (incl. arcs), `Via` (layer span), `Footprint`/`Pad` (absolute
  position, pin type and function from the schematic; `Footprint.outline`: courtyard, else fab polylines, set by
  the file reader's `_part_outline` only), `Zone` (filled polygons per layer).
- `core/issues.py`: the `Issue` record every check, solver step, report and the plugin share (check id, severity,
  net, message, location in mm, layer, offending KiCad uuids, file line for CI, `key` discriminator, `details`,
  waiver). `CHECKS`: default severity and title per check id; `SEVERITY_RANK`, `resolve_severity` (settings
  overrides), `fingerprint` (below, 16 hex digits; falls back to the location without uuids or key), `dedupe`,
  `sort_issues`.
- `adapters/file_source.py`: S-expression parser (`adapters/sexpr.py`, keeps line numbers for CI reports).
  Handles KiCad 10 `(net "name")` and old `(net 3 "name")` formats. Pads are footprint-local, rotated by the
  footprint angle with KiCad's convention (CCW on screen, y down; see `geometry/shapes.rotate`). Pad angles in
  the file are already absolute. Zone fills are "fractured" polygons; `clean_polygon` (`buffer(0)`) restores holes.
- `adapters/kipy_source.py` / `kipy_sink.py`: IPC API (kicad-python ≥ 0.8). Footprint children from
  `get_footprints()` have **absolute** positions. Units are nm. Drawing goes into groups named `groundtruth-*`,
  one commit each, so clearing removes exactly our items.
- `config/settings.py`: `groundtruth/<board>.groundtruth.yaml` (`settings_path`, in `project_dir`: the board's `groundtruth` folder, which also holds the simulation history and is the Export dialog's first folder; the user, 2026-10-10: "This keeps the kicad root folder cleaner"; a settings file beside the board is not read), round-tripped with ruamel (comments kept). Holds the nets
  (manual or `source: auto, reviewed: false`), part models, analysis/rule settings, waivers and the
  drawing layers. Merging after board edits refreshes only untouched suggestions. Nets, converters and bridges
  have `enabled` (written only as `enabled: false`; the user: "if one wants to not simulate a particular net,
  converter 'row' one has to delete it which then looses all the data entered"): `Config.active(names)` is the
  settings without the rows switched off, unless named; `simulate`, `run_checks` and `suggest_fixes` start with
  it, `validate` checks only active rows, the CLI lists what it left out. The plugin's tables have a tick box per
  row (`ui/listsel.SelectableRows._init_enable`, `_show_enabled`, `_ticked`); "Update selected" keeps the state.
  Tests: `tests/test_enable.py`, GUI `test_a_row_unticked_on_the_nets_tab_is_kept_but_left_out_of_simulate`.
- `components/models.py`: `ComponentModel`, the two-terminal model a part is replaced with (`MODEL_KINDS` short,
  open, resistor, cap, inductor, ferrite; `MODEL_PARAMS` per kind, also the Parts tab's columns and the YAML keys).
  `impedance(f)` (None = open; a short is `SHORT_OHMS` 1 mΩ; a cap ESR + j(ωESL − 1/ωC), C from `capacitance()`:
  the optional `C_bias`, its capacitance at the DC voltage, else C; `OPTIONAL_PARAMS` are never "missing"),
  `passes_signal` (short,
  ferrite, inductor, resistor ≤ `SERIES_PATH_MAX_OHMS` 1 kΩ), `pins` (the two pins a multi-pin part's model joins,
  e.g. a current-sense IC's IP+/IP−; `model_pads` / `model_nets`), `to_dict` / `from_dict`.
- `components/defaults.py`: `suggest_model(fp)` from the reference prefix (`ref_prefix`, which drops a stacked
  board's `name:` prefix), value and footprint name: DNP → open, net tie → short, R (0R → short), C (ESR 20 mΩ
  MLCC or 0.1 Ω electrolytic, ESL by package `package_esl` 0.2–1.8 nH, electrolytic 3 nH), ferrite from
  "600R@100MHz", L (DCR 50 mΩ), F (fuse) → short, everything else open (ICs, connectors, transistors: ports only).
  Used by `groundtruth init`, detection, bridges, victims and the Parts tab's reset. Then (`fields=True`, the
  default) the symbol's fields override what they give (`fields.model_values`), listed in the model's
  `from_schematic`; `suggest_model(fp, fields=False)` is the plain suggestion (what a field replaces as a default).
  `is_electrolytic(fp)`.
- `components/values.py`: `parse_value` (schematic notation: 4k7, 1R5, 22u0, 100n, 10uF/16V; DNP/NC → None),
  `parse_quantity(text, quantity, assumed)` (a part parameter as a symbol field holds it: InvenTree's pint strings
  "3.8milliohm", "616 picofarad", "6.9 mΩ", datasheet "3.8m", "4m7"; a unit of another quantity is refused, a bare
  number takes `assumed`, a number with an exponent is SI), `parse_impedance_at` (ferrite "600R@100MHz" → (Ω, Hz)),
  `format_si`.
- `components/fields.py`: part parameters from the schematic (session 7, the user's InvenTree parts reach the
  symbols as fields through KiCad's HTTP library). `PARAMS`: one concrete field name per parameter (Rds(on), Coss,
  Qoss, Vsd, Vf, Cj, DCR, ESR, ESL, Cbias, Capacitance, Inductance, Resistance), with the quantity, the unit a bare
  number is in, the usual range (outside it: used, with a note), an example and what it feeds; the plugin's help table and the README's come
  from it (`ui/help.field_rows`). `read(fp, name)` → `Reading(value, text, source, note)`; `stage_values` maps a
  converter's or bridge's parts onto its keys (rds_on from the high side or switch IC, rds_on_low from the low side,
  c_oss/q_oss from the switch-node device × the side's parallel count, a catch diode's Cj and Vf, the inductor's DCR
  via `power.switch_inductor`; parallel parts that disagree give nothing, with a note); `model_values` a part
  model's (`MODEL_FIELDS`).
  - **Linked vs set here:** `ConverterSpec`/`BridgeSpec`/`ComponentModel.from_schematic` lists the keys whose value
    comes from the fields (written in the YAML). `_sync` refreshes them, fills empty keys (and, for part models,
    keys still at the plain suggestion), unlinks a key whose field is gone (back to empty / the plain default) and
    never touches the others. `apply_fields(config, board)` runs it for every stage and part: on `merge_board`
    (`SettingsFile.sync_fields` writes what changed: plugin open, `groundtruth init`) and in memory before every
    CLI analysis (`cli._settings`). `fill_stage` for one stage: detection (`detect._from_fields`, so a new stage
    starts from its fields; a bridge's default v_sd only where no Vsd field gave one) and the forms (`link_form`:
    unchanged and schematic-equal values stay linked, emptied ones refill). `use_schematic` (the forms' Use
    schematic values) relinks every key the fields give.
  - **Differences:** `differences(config, board)` → `Difference(kind, name, key, ours, schematic)` for keys set
    here that differ by more than `REL_TOL` (1e-4); `rules/checks._schematic_differs` makes each an info issue
    `schematic_differs` at the part (a stage's first part). `stage_marks` gives the forms' ◆ / ! per key
    (`ui/widgets.FieldMark`, `with_mark`), the Parts table its Schematic column (`PartsPanel._schematic`) and a
    warn-coloured cell. Power stages and bridges note their linked values (`power.schematic_note`), so every
    report lists them.
- `geometry/shapes.py`: primitive geometry in mm shared by the adapters and the copper builder: `rotate`,
  `arc_points` (3-point arcs, 5 µm chord tolerance), `track_shape` / `track_centerline`, `pad_shape` (circle,
  oval, roundrect, else a rotated box; custom pads from their polygon), `clean_polygon`, `polygonize_outline`
  (Edge.Cuts line work → board polygons, inner contours as cut-outs, tiny gaps tolerated), `to_shapely` /
  `from_shapely`, `multipolygon`.
- `geometry/copper.py`: `CopperMap`: shapely geometry per (layer, net), all copper and zone fills
  ("planes") separately, cached.
- `analysis/route.py`: Dijkstra over a graph of track endpoints, vias (vertical edges), pads and series
  parts whose model "passes signal" (short, ferrite, inductor, resistor ≤ 1 kΩ). Gives ordered
  `RouteSegment`/`RouteVia` steps. `return_pad()` picks the return pin (nearest reference-net pad of the same part).
- `analysis/detect.py`: net auto-detection (see below).
- `rules/checks.py`: Tier 1.
- `solver/kernel.py`, `solver/peec.py`: Tier 2 numerics. `analysis/simulate.py` builds the problem from
  the board.
- `emi/spectrum.py` (trapezoid harmonics, microstrip/stripline Z0), `emi/radiation.py` (loop field,
  CISPR 32 tables).
- `report/formats.py` (text, JSON, JUnit, GitLab Code Quality), `report/html.py` (the HTML report, see "HTML report"),
  `report/boardview.py` (its interactive board, see "HTML report"),
  `report/plots.py` (every figure, plugin and report alike), `report/view3d.py`.
- `results/contours.py` (rewritten 2026-10-12: the user found the old three filled 50/20/5 % regions "a waste of
  time", one colour and unreadable): `contour_map(grid, Z, peak, scale, low, unit, what)` → `ContourMap` (lines,
  labels, legend), like a topographic map's height lines. Levels at round values: 1-2-5 steps over the heatmap's
  three decades (`log_levels`), or round steps in °C from the ambient for a thermal view (`linear_levels`); every
  decade (or fifth step) an index line. Isolines (contourpy, masked off the copper) rather than filled regions,
  Chaikin-smoothed and simplified within MAX_SEGMENTS (8000); each long line carries its value along it, upright,
  the line broken under the label, overlapping labels moved or dropped (`_place_labels`). The Heatmap tab's Draw
  contours (`MainFrame.on_draw_contours`, a job; on a thermal view isotherms) → `kipy_sink.draw_contours(board,
  cmap, layer, label, line_mm, index_mm)` on the settings' `drawing.current_layer`, line widths from Preferences
  (0.04 / 0.1 mm). `tools/dev/contour_preview.py` renders it over the heatmap as a PNG without KiCad.
  `results/vectors.py`: see "Current arrows".
- `cli.py`: the `groundtruth` command (`build_parser`: init | detect | info | check | simulate | suggest | compare;
  `cmd_*`, `_run` for check and simulate). Exit codes `EXIT_OK` 0, `EXIT_ISSUES` 1 (issues at or above
  `--fail-on`, or a margin below `--max-dbuv-margin`), `EXIT_CONFIG` 2 (`CliError`, `GpuUnavailable`); report
  files under `OUTPUT_NAMES`; baselines through `report.formats`. Never imports wx or kipy (`tests/test_cli.py`).
  `__main__.py` makes `python -m groundtruth` the same command. pyproject entry points: `groundtruth` =
  `cli:main`, `groundtruth-gui` = `ui.app:gui_main` (the plugin window on a board file, file mode).
- `ci/refill.py`: `--refill-zones` for CI. `refill_copy` copies the board and its .kicad_pro / .kicad_dru /
  .kicad_prl into a temporary folder, runs `kicad-cli pcb drc --refill-zones --save-board` there (exit 0 or 5
  accepted) and returns the copy; the original is never written. `find_kicad_cli`: PATH, then the Windows
  `Program Files\KiCad\*\bin` (newest first), Linux and macOS locations; `RefillError` otherwise.
- `thermal/` (PCB Thermal's solver, vendored) with `thermal/adapter.py` (`to_thermal_board`), driven by
  `analysis/thermal.py`: see "Plugin" → "Thermal solve".

## Tier 1 rule checks (`rules/checks.py`)

The route is split into runs (consecutive segments on one layer). A run's reference layer(s) are the nearest
adjacent copper layer(s) in the stackup, both when equidistant (stripline). "Plane" means **zone fills**
only: tracks on the plane layer cut slots, they don't form a plane. Checks:
- `gap_crossing`: run centreline minus plane copper; interior pieces ≥ `min_gap_mm`. The message names both
  nets when it's a split (e.g. +3V3/GND). It drops to warning if the other neighbour plane covers it.
- `off_plane`: uncovered piece touching a run end and longer than max(1 mm, 2h) (shorter ones are via
  antipads / pad escapes).
- `layer_change_stitch` / `layer_change_decap`: at each layer change, the plane nets on both reference
  layers at the via. Same net: needs a via/THT pad of that net spanning both within `stitch_distance_mm` (2).
  Different nets: needs a cap (or short) between them within `decap_distance_mm` (5).
- `edge_proximity`: morphological closing (0.25 mm) of the plane, holes < 1 mm² ignored. Covered trace within
  `edge_factor`·h (3h) of the edge, excluding run ends and gap surroundings.
- `reference_net` (info): plane under the trace isn't a reference net.
- Fingerprints: sha1(check | net | offending KiCad uuids + discriminator), so they're stable across
  unrelated edits.

## Tier 2 solver (quasi-static PEEC)

### Formulation (`solver/peec.py`)
- All plane layers share one uniform xy grid (pitch d). Nodes are labelled by net (`build_grid` with a
  `{net: geometry}` dict), so different nets are never joined even when their clearance is narrower than d.
  With `sub` > 1 (the simulation uses a sub-grid of ≤ 0.15 mm, see "Board → problem"), a node is its cell's
  largest same-net copper piece and a strip exists where that copper crosses into the neighbouring cell's
  piece. With `sub` = 1 (point sampling), a strip exists if both end nodes and the strip midpoint are the
  same net. **Seeds** force a node at small vias/pads of plane nets.
- Unknowns, in this order: x/y strip currents per layer, plane-pair capacitance branches, lumped/vertical
  `Branch` currents (vias, THT pads, parts), and node potentials (one grounded node per connected component).
  A through-hole pad's pin (`simulate.add_pin`, 2026-10-15; the user, of a big connector pin: "the current density
  around that area looks odd") is joined to each layer by `pin_joints` parallel branches (one per ~1.5 cells of the
  drill's circumference, 1 to PIN_JOINTS_MAX 48) between points on a circle at drill/2 + d/2, each k x the pin's
  impedance: joined at the one node nearest its centre (add_vertical, still used for vias), the whole current
  entered one cell, a 20.8 A/mm² hot spot at 1 A for a 2 mm pin where the barrel wall carries 4.55 (I/(2π·r·t)).
  tests/test_pins.py: J(r) analytic within 3 % at 3 and 5 mm, the wall peak within 30 % (3.8 / 4.3 / 5.1 at
  0.25 / 0.125 / 0.0625 mm cells). The ▲ markers add a pin's joints per layer pair (`transfers`).
  - KVL per branch: `Z·I − A·V = −jω·ψ`
  - KCL: `Aᵀ·I = J` (+1 A at the receiver return node, −1 A at the driver's)
- Strip impedance: `R_s(f)` (skin effect, per-layer copper thickness) plus `jω·L_p`. The mutual partial
  inductance between parallel strips depends only on the grid offset and Δz, so each layer pair is a
  **convolution kernel** (`kernel.strip_kernel`). Gauss quadrature is used for near neighbours, filaments
  far away, and sub-strip numeric integration for the self term (the GMD formula is 5 % low for square strips).
- Kernels are embedded circularly (they're even, so their FFT is real), FFT size P = next_fast_len(2n−1).
  The product is evaluated in **double precision**. Single precision was 3× faster, but its ~1e-7 noise made
  GMRES stall near plane resonances (10 517 iterations at 1.7 GHz).
- **Plane-pair capacitance** (`PlanePair`): wherever two layers both have copper at a node and no layer in
  between does, a vertical branch with `C = ε0·εr·d²/t` and admittance `jωC + ωC·tanδ`. εr, t and tanδ come
  from the board stackup (`Stackup.epsilon_between / dielectric_between / loss_tangent_between`). They
  count for connectivity, so islands joined only through capacitance are kept and can carry eddy and
  displacement currents.
- `ψ` = ∫A·dl of the known signal current (exact straight-segment vector potential, 3×3 quadrature for
  strips near the trace, centre point far away). Vertical branches of kind `via` also couple to vertical
  signal-path segments (signal vias) through `segment_mutual`.
- Loop impedance: `Z = R_trace + (V_rcv − V_drv) + jω(L_trace + ψ·I)`. Magnetic moment
  `m = ½ Σ r × I dl` over the closed loop (trace chain, strips, branches, displacement currents).
- **GMRES** (`_gmres`, own implementation): right-preconditioned, so it stops on the true residual
  (rtol 1e-5). Classical Gram-Schmidt as two BLAS `zgemv` calls per step on the basis in place (no conjugate
  copies), with a second pass only when cancellation happened (DGKS). Restart length min(200, memory budget of
  400 MB). The small least-squares problem is kept triangular with **Givens rotations** (O(j) per step);
  `np.linalg.lstsq` on the whole Hessenberg matrix each step cost 18 ms at j = 120 and 44 ms at j = 200, more
  than the rest of a step on long solves. `max_its` stops it unconverged (the borrow budget, below). After its 8
  restarts a solve with its own factorisation gets up to `GMRES_EXTRA_CYCLES` (24) more while each lowers the
  residual to at most `GMRES_PROGRESS` (0.7) of the last one; a stagnating solve stops at once. A big model's
  restart is short (its memory budget: 52 on kraft_flux_mini_2's board-wide model at 4× resolution), and near a
  plane resonance it converges steadily but slowly: that 1.58 GHz solve fell ~2× per cycle and finished in 11
  cycles, where the 8 failed (audit, session 7).
- **A solve that still fails** is kept as `None` in its run. The refinement (`next_frequencies`) then closes in
  on the first failure above each solved frequency from below (log-midpoint harmonics, first in line), and
  `unsolved_band` names the band above the last solved frequency when a failure lies there and the limit
  reaches above it: `_interp_loglog` holds the moment at its last value there, so a resonance in that band is
  missed. `RouteResult.unsolved` = {"from", "failed"} and a `band_unsolved` warning (`unsolved_issue`, routes and
  power stages). Before, the 4× run above stopped refining at 260 MHz and reported a +17.5 dB margin at 780 MHz
  (held from 260 MHz) without a word, where the solved 1× model had +3 dB.
- **Preconditioner**: the block system `[[D, −A], [Aᵀ, 0]]`, solved exactly through the Schur complement
  `Aᵀ D⁻¹ A` (SuperLU, MMD_AT_PLUS_A, SymmetricMode, **diag_pivot_thresh 0.1**). S is complex symmetric;
  symmetric mode only keeps its A+Aᵀ ordering when it takes the diagonal pivots, which SuperLU's own advice
  says needs a small threshold. With the default 1.0 it pivoted off the diagonal: kraft_flux_mini_2's 6-layer
  board-wide model (34 000 unknowns) took 11.4 s and 10.4 M fill per factorisation, 3.4 s and 5.8 M with 0.1,
  same residual (8e-12); each preconditioner solve 31 → 21 ms. COLAMD (6.7 s), nested dissection on the grid
  (12 s), complex64 (16 s) and larger supernodes (no gain) were slower. The route models (diag) factorise in
  0.35 s either way.
  - **Memory: every factorisation is made and freed in a host thread** (`hosted_splu`, `_LuHost`, `HostedLU`;
    2026-10-10, the user: "after a faild sim, memory is not released", 5.4 GB). scipy 1.18's SuperLU books its C
    allocations per thread: a factorisation freed from another thread than the one that made it never gave its
    memory back (`tools/dev/mem_probe.py`: 8 × 63.4 MiB + 8 × 15.9 MiB new committed regions per Simulate on 4
    threads, ~640 MiB private per run, also after successful runs; 1 thread: none; 82 MiB per factorisation in a
    two-thread test). The factorisations were made in `run_parallel`'s pool threads and dropped from the job thread
    (`drop_factorisations`, a failed run's frames). Now the caller hands `splu` to an idle host thread (a new one
    when all are busy, so factorisations stay as parallel as before) and waits; `HostedLU.__del__` sends its last
    reference back to that host. Solving from any thread is fine. After: flat (706, 739, 742, 744 MiB private over
    4 runs, against 1341, 1982, 2620, 3257). Tests: `test_a_factorisation_is_made_and_freed_in_its_host_thread`,
    `test_factorisations_dropped_in_another_thread_give_their_memory_back` (fails with plain `splu`: 82 MiB per
    round). Ruled out on the way: thread pools per run, FFT workers, OpenBLAS threads, numba, a lock around
    `splu` (only made and freed in different threads leaks).
  - **SuperLU's size limit** (2026-10-15, the user's 25x Simulate of a 1.9 M-node power stage on the Dell failed after
    30 min: "SystemError: gstrf was called with invalid arguments", peak 21.9 GB with 52 GB free). SuperLU counts
    with 32-bit integers: past ~2.1 billion entries (~34 GB complex) a growth step overflows and comes back as
    "invalid arguments". `PlaneStack._factorise` then calls `_factorise_diagonal`: diag_pivot_thresh 0 (no
    off-diagonal pivots, only the ordering's fill; a preconditioner needs no more); the model's later
    factorisations take it at once (`lu_fallbacks`); too large even so: `lu_too_large`, a SolverError naming the
    limit and the cure for every later one (the frequencies unsolved with that note, the job goes on). The run
    notes the fallback. Tests: tests/test_lu_size.py (the overflow imitated).
  - **The hosts end at exit** (`_lu_exit`, atexit). A daemon thread that had made a SuperLU factorisation and was
    still alive when the interpreter was torn down (even with its factorisation freed) left an exception set:
    Python printed "Exception ignored on flushing sys.stdout: SystemError ... returned a result with an exception
    set" and exited with 120 instead of the run's code whenever its output was redirected (CI, logs). Plain
    threads that had ended, and hosts without a factorisation, were fine. Now each host gets a stop job and is
    joined (≤ `LU_EXIT_WAIT_S`); factorisations still alive are freed where they are dropped from then on. Test:
    `test_a_run_ends_with_its_own_exit_code_while_factorisations_are_alive` (exit 120 without the stop).
  - `diag`: D = diag(Z), strip R + jωL_self and the branch impedances. Used by the route models, which are
    inductive.
  - `sheet`: per grid position, the strips of all layers form one block that keeps every plane pair's
    sheet inductance μ0·Δz per square (`_sheet_blocks`). That is the cavity mode diag(Z) misses. Used by the
    board-wide model, where diag needs thousands of iterations above ~600 MHz.
- **Reuse** (`PlaneStack`): the plane system doesn't depend on the route, so one stack serves every route
  and candidate.
  - Operators (factorisations) are cached per frequency, `OPERATOR_CACHE` (2) per stack. A freed one is made
    again when needed, identically.
  - A factorisation is **borrowed** as the preconditioner by frequencies within ±25 % of it (log ratio 0.25,
    `PRECOND_REUSE`). Measured on the 6-layer board-wide model: a 100 MHz factorisation used at 160 MHz needed
    54 iterations instead of 53, at 200 MHz 56; a 400 MHz one at 640 MHz 81 instead of 63, at 1.2 GHz 996
    (127 s). A wider window (±60 %) where a factorisation costs 100 iterations or more was tried: with it at 20
    iterations, two borrows 1.5x away above 250 MHz took 123 and 141 iterations instead of 62 and 66. A borrow
    has a **budget** (`borrow_budget`): 1.5 × the iterations the source needed at home, plus 15. Past it the
    solver factorises at f and continues from the solution so far (before: only after 8 failed GMRES cycles,
    then from scratch).
  - **Reproducible**: the same board and settings give bit-identical results, whatever the thread count or
    timing. Which factorisation a frequency uses is decided once (`PlaneStack._plan`) from the factorisations
    decided in *earlier rounds* (`_made`, `next_round`; `solve_setups` starts a round before each batch of
    parallel solves, after the last one too). A source's home iterations are the fewest of its own round's
    unguessed solves at its frequency. Before, a solve borrowed the nearest factorisation cached at that
    moment, so the result depended on which solve of a round finished first and on what the cache had evicted,
    and the wider window and the budget used measured seconds (factorisation time, time per iteration): two
    runs of `main` on kraft_flux_mini_2 differed by up to 0.0022 dB, and solves by 20 % in iterations
    (`test_a_simulate_does_not_depend_on_the_order_its_solves_finish` runs the solves in and against their
    order). The wider window went with it: on kraft_flux_mini_2 no model reached 100 iterations per
    factorisation (route models ~5, the board-wide model 39-79). Checked on the CPU only: the GPU backend's
    sums may not be in a fixed order.
  - `with_branches()` adds candidate vias or capacitors as a low-rank (Woodbury) update of the base
    factorisation. A full rebuild happens only if the new branches join separate islands.
  - `solve(f, guess=[...])` warm-starts GMRES from a least-squares combination of guess vectors.
    Candidates start from the baseline solution, which saves about a third of the iterations. Across
    frequencies a warm start doesn't help, so it isn't used there.
- **Kernel cache** (`_kernel_pair`): spectra are reused across solves on the same grid.
- **Compiled multiply-add** (`solver/_mac.py`, numba): the L_p product's Σ_s K[t,s]·F[s] per frequency pixel
  was N² numpy multiplies and adds, each a pass over memory (9.4 ms per product on the 6-layer route model);
  fused per pixel it is 3.3 ms, bit-identical. Serial on purpose (the solves already run in parallel threads,
  and numba's default thread pool aborts when two threads launch parallel loops at once), `nogil`, compiled on
  first use and cached by numba. Kernels kept pixel-major per stack (`_pixel_major`); strips scattered and
  gathered by precomputed flat indices (`_strip_index`). Without numba (a plain CLI install) numpy does it
  (`USE_NUMBA`, `_kernel_mac`). Tried and dropped: a pruned 2-D FFT that skips the zero padding (7 % faster:
  strided 1-D passes are slower than SciPy's 2-D FFT).
- **Measured** (kraft_flux_mini_2, 6 layers, SPI_SCK alone, `tools/dev/profile_sim.py`): Simulate 139 s → 76 s;
  factorisation time (summed over threads) 103 s → 25 s; one board-wide solve 19.0 → 6.7 s, one route-model solve
  5.95 → 5.15 s (`tools/dev/solver_bench.py` times each part of an iteration). SPI_SCK + SW1: 283 s → 167 s, the
  results the same to 0.00003 dB (`tools/dev/result_diff.py` against main).
- **GPU backend** (`solver/gpu.py`, PyTorch, optional; `analysis.gpu` auto | on | off, CLI `--gpu`): the user's
  plugin runs on machines with big GPUs and servers, the development laptop has none (its Intel UHD 620 ran the
  multiply-add in 2.0 ms against numba's 4.4 ms, 3.6 ms with the copies: `tools/dev/gpu_probe.py`). PyTorch
  because the same code runs on the CPU (`GROUNDTRUTH_GPU_DEVICE=cpu`), so the device path is tested here and in
  the suite (`tests/test_gpu.py`); CUDA, and ROCm on Linux, are the targets (vendor not known yet).
  - `PlaneStack.solve` does the iterations with a math object: `_HostMath` (numpy/SciPy, as before) or
    `gpu.TorchMath`. Both share `_gmres` (its vector work through `_HostVectors` / `gpu.TorchVectors`; the Givens
    rotations stay on the CPU, the basis on the device) and `_solve` (guess, borrow budget, fallback).
  - On the device: the L_p product (`torch.fft.fft2` with padding, the multiply-add as an einsum over the real
    view), the incidence products (real CSR × the complex vector's real view), diag(Z)⁻¹ or the sheet blocks
    (Dinv = Dr + j·Di, two real CSR products), Gram-Schmidt (`mv` with a conjugate view). On the CPU: the
    factorisations and `op.solve_S`; per iteration only the node-sized vector crosses (~1 MB), the Hessenberg
    column comes back (j+1 numbers).
  - Device copies: per stack the incidence matrices (`_gpu`; a with_branches stack gets its own, `gpu.derived`)
    and per grid the kernels and strip indices (`_gpu_kernels`, shared with the derived stacks, so fix candidates
    do not upload them again); per operator R, z and Dinv. `drop_factorisations` frees them (`gpu.release`).
  - `gpu.use(mode)` around simulate, the fix search, the aligned comparison and Sharper map; `auto` without a GPU
    or PyTorch stays on the CPU (importing PyTorch takes 2 s, the GIL held ≤ 31 ms at a time: no GUI stall). Any
    device failure in a solve except SolverError (out of memory, a missing operation) → that solve again on the
    CPU, counted (`Run.fallbacks`, the first error), shown with the result ("solved on GPU X (2 solve(s) done on
    the CPU instead: …)"). Double precision only (single precision stalls GMRES near resonances).
  - Measured here only on PyTorch's CPU device (correctness): the device products equal numpy's to 1e-12, a
    Simulate with the board-wide model gives the same harmonics, margins (≤ 1e-3 dB) and issues, the fix search the
    same fixes. Speed on a real GPU: not measured yet (`tools/dev/gpu_check.py` on the GPU machine).
  - Deployment on another PC: `tools/install_gpu.py` puts PyTorch with CUDA into the KiCad plugin venv,
    `tools/check_gpu.py` (any Python) asks the installed plugin's own device probe and times a complex128 2-D FFT
    on CPU and GPU; both ship in the KiCad package's plugins/tools/ with install_gpu_amd.cmd and GPU_README.md
    (the separate GPU package, make_gpu_package.py, was dropped on 2026-10-14; a compute server installs PyTorch
    with `install_server.cmd install --cuda`).
- **Quadrature cache** (`kernel.leggauss`): Gauss-Legendre nodes and weights per order, computed once.
  `segment_mutual` used to recompute them, an eigenvalue problem, on each call, and a power stage's model
  makes about 35 000 such calls (switch-package filaments × vias). Its model build went from 20 to 6 s.

### Board → problem (`analysis/simulate.py`)
Two models per route, because one grid can't be both fine enough for the loop next to the trace and large
enough for the whole board's plane cavities:

| | Route model ("fine") | Board-wide model ("cavity") |
|---|---|---|
| Region | bbox of route + return pins + `margin_mm` (10), clipped to the outline | the whole board outline |
| Pitch | `grid_mm: auto` → clamp(h, 0.05, 0.25) mm, coarsened to `max_nodes` (100k) | max(h, 0.25) mm, coarsened to `cavity_max_nodes` (40k) |
| Physics | R + jωL (inductive) | R + jωL + plane-pair capacitance |
| Preconditioner | diag | sheet |
| Solved at | f0 (all harmonics when capacitance is off) | f0 + harmonics, adaptively |
| Shared | per route | by every route on the board (built once, factorisations shared) |

Both pitches are divided by √`analysis.resolution` and both budgets multiplied by it (see "Plugin" →
"Resolution"); at 1× the table holds.

- Combination: `m(f) = m_route(f0) + [m_board(f) − m_board(f0)]` (complex vectors, `band_moments`). The route
  model gives the inductive loop next to the trace, which barely changes above a few MHz. The board-wide model
  adds what plane capacitance changes (displacement current, resonances), and its own grid bias at f0 cancels.
  A multiplicative ratio was tried first and was wrong when the coarse model overestimated the loop at f0.
  The bias is reported (`cavity.bias_db`); above 10 dB a note says the board-wide grid can't resolve that route.
- Plane layers (`analysis.planes: auto`): every copper layer with zone copper of a non-signal net in the
  region, plus the route's reference layers and the return-pin layers. `nearest` gives the old single-plane mode.
- Plane nets: reference nets plus nets with zone fills there (the board-wide model excludes all configured
  signal nets). Each layer's geometry is all copper of those nets (zones, tracks, pads, vias).
- **Rasterisation keeps the copper topology** (`build_grid(..., sub)`, `RASTER_MM = 0.15`). Copper is sampled
  on a sub-grid of pitch ≤ 0.15 mm. Each cell keeps its largest copper piece, and two cells are joined when
  that copper crosses their border. Before this, only node points were sampled. Thin webs between rows of
  via antipads then vanished, and the user's GND pours fell apart into 7–17 pieces per layer, joined only by
  vias. Those pieces were LC resonators whose frequencies moved with the grid. The fix took the board-wide
  model's grid-to-grid spread from 5.5 to 1.9 dB rms (0.71 vs 0.49 mm, 25 frequencies, 200 MHz–1.6 GHz) and
  its f0 bias from up to +14 dB to −0.5 dB. Plane-pair capacitance uses each cell's copper fraction
  (`Grid.fill`).
- **Tracks of plane nets stay joined on every grid** (`_rasterise_lines`). Their centrelines are walked
  through the grid and their cells and strips forced, so escapes narrower than the grid conduct. The walk takes
  the nearest of the four cells around each point that already holds the track's net (else the nearest free
  one) and joins consecutive cells along the L-path with fewer foreign cells. A cell another net owns (a track
  beside a pour: the pour's piece of the cell is larger) is never taken: the cells before and after it are
  joined by a **bridge**, a lumped branch with the track's resistance (skin depth) and partial self inductance
  (`Grid.bridges`, `track_impedance`, branch kind `track`, left out of the layer transfers). Before, the walk
  took the nearest cell only and stopped at a foreign one. On kraft_flux_mini_2 (6 layers) that cut tracks
  213 times at 1×, 87 at 2× and 74 at 4× (all models of SPI_SCK). A 78 mm, 0.4 mm +3V3 track on In3, tied to
  its pour at one end, floated free at 1× and 2× and was joined at 4×: free, it resonated at ~1.24 GHz (a
  half-wave line), the board's worst SPI_SCK margin (+3.2 dB at 1260 MHz at 1×, |m| 258 → 225 → 63 mm² at
  1× / 2× / 4×). Separate conductive pieces of the board-wide model: 29 / 38 / 19 → 14 / 5 / 5 at 1× / 2× /
  4×; what is left is mostly a +VDC pour on In3 that is floating on the board itself (its two vias' rings
  don't touch it). `tools/dev/cavity_converge.py` solves a net's board-wide model on the same frequencies at
  several resolutions (Simulate picks different harmonics on each grid, which hid this).
- **Board-wide resonances above 1 GHz** are grid-sensitive (kraft_flux_mini_2, SPI_SCK, 500 MHz-1.5 GHz every
  20 MHz at 1x / 2x / 4x, `out/converge/spi_sck_fixed.json`; |m| per ampere, the trace loop alone 19.7 mm²):
  - 740 MHz (In2-In3 cavity): 77 / 79 / 77 mm² at 750 / ~745 / ~730 MHz: converged (0.6 dB).
  - ~1 GHz plane mode: 1000 / 980 / 1020 MHz, 68 / 52 / 66 mm²: consistent.
  - +3V3/GND pair (F.Cu-In1) with the decaps C1105, C1104, C1110: 1100 / 1220 / 1240 MHz, 93 / 183 / 59 mm².
    The frequency settles; the height doesn't: at 2x the V_BATT pour around Q601 resonates near the same
    frequency and the two reinforce (Q601 carries 2.4 A per A there), at 4x they are apart
    (`tools/dev/cavity_hotspots.py`).
  - 1440 MHz, GND F.Cu-In1, 142 mm²: at 1x only.
  4x needs 3-10x the GMRES iterations (200-900; once not converged at 980 MHz), and 8x would take hours per
  frequency on a laptop, so it isn't proven which grid is right up there. Simulate says so instead
  (`grid_sensitive_note`, the user's choice of three options): when a net's worst emission is above 1 GHz, comes
  from a plane resonance (the loop moment ≥ 6 dB over its low-frequency value) and is within 10 dB of the warning
  level, its notes say the peak can change by several dB with the grid and name the next resolution to check.
- Vertical branches: each via/THT pad of a plane net chains its connections between consecutive plane
  layers where its own net's copper is (a GND via through a +3V3 area of In2 doesn't connect there).
  `via_impedance`: barrel R (25 µm plating) plus partial self-L.
- Parts whose model isn't `open` and that sit between two plane nets (decaps, 0R, beads) are lumped branches
  with model Z(f) + 0.3 nH mounting. Since 2026-10-13 a part is spread over the whole area of all its pads on each
  side (`simulate.part_pairs`): up to MAX_PART_BRANCHES = 8 nodes under the pads per side (farthest-point sampled),
  paired side by side across the part, each branch n·Z so the n in parallel are Z. Before, one branch joined the
  first pad's centre on each side, so a wide part (a FET's drain pad and fingers, an inductor's terminals, a
  shunt) fed its current into one grid node: a spreading resistance that grew on finer grids (validated:
  `test_a_part_is_joined_to_the_copper_over_its_whole_pads`, two strips joined by a full-width 1 mΩ part:
  +0.84 mΩ over the strip, expected ≈ 0.83; one branch gave +1.45 mΩ at 0.1 mm, +1.55 at 0.05 mm). Consumers sum
  a part's branches: transfers (fraction), dc_summary (current; losses per branch add up), _cap_shares,
  bridge_rms (RMS add, conductances add).
- Return injection: return pad's own-net node on its layer, then the same net on another layer (note), then
  any plane copper within 5 mm (note).
- Loop L was verified to change < 1 % from d = h/2 to 2h on the route model.
- **Routes through copper pours** (switch nodes, power nets):
  - The route finder joins items that touch the same fill polygon of their net with a "zone" edge, so a route
    can run through its net's pour.
  - In the solver the pour is solved copper, not a line. `make_chain_with_pours` stops the filaments where the
    route enters the pour (pour segments and the vias between them form one section) and resumes them where it
    leaves. `RouteExcitation(injections=...)` injects +1 A and −1 A at the pour's grid nodes there, and the
    filaments run exactly to those nodes, so the loop stays closed and the moment origin-independent.
  - Return statistics (layer shares, reference layer, transfers) leave out the route's own copper. The heatmap
    still shows the pour's current density.
  - Tier 1 doesn't call a gap next to the route's own copper a split.
  - Tests: `tests/test_pours.py`. A 4 mm pour 0.235 mm over its plane matches Hammerstad's per-length
    inductance within 10 %, carries the full 1 A across its width, and crowds the current into its edge cells
    (1.8× the centre).

### Frequency planning
- Harmonics come from the trapezoid spectrum up to the analysis band. Solves happen **at harmonics**
  (`initial_frequencies`: f0 plus `solve_points − 1` log-spaced harmonics in the limit band, 30 MHz–6 GHz),
  so no interpolation error appears where a solved emission is evaluated.
- `next_frequencies` refines until `max_solves` (10) per route:
  - Each gap between solved neighbours gets an uncertainty. It is the change of |m| across the gap or, with
    plane capacitance, 3 dB per octave of width, whichever is larger, because a resonance can hide in a wide
    gap whose ends look alike.
  - Gaps under 3 dB count as resolved. Gaps whose estimated emission stays 20 dB below the warning level are
    skipped.
  - Of the rest, the gap closest to the limit is split at its log-midpoint harmonic.
  - With plane capacitance, the harmonic with the worst estimated margin then gets its own solve, unless one
    lies within ±5 %. The reported worst case is therefore a solved value, not an interpolation across a
    possible resonance.
- `solve_setups` runs each shared model frequency by frequency over all routes, so one factorisation serves
  every route at that frequency.
- **Parallel solving** (`run_parallel`, `analysis.threads`: `auto` means the physical cores, at most 4):
  - The first round's solves (all routes, models and planned frequencies) run in a thread pool, then each
    refinement round, then the per-route finish step. SciPy's SuperLU, the FFTs and the compiled multiply-add
    release the GIL. The Gram-Schmidt products do not: numpy's `@` and SciPy's f2py `zgemv` both hold it for the
    whole call (measured: a 5 ms ticker thread stalled 87 / 102 ms during one 86 / 100 ms call), so those parts
    of parallel solves take turns (a few ms each per step).
  - Each worker gets `cpu_count / threads` FFT workers (`thread_budget`, a thread-local setting read by
    `_conv`).
  - `PlaneStack.operator` is thread-safe. A frequency being factorised has an in-flight `Event`, and other
    threads wait for it instead of factorising twice. The kernel cache and `BoardModel`'s shapely builds are
    locked.
  - Refinement takes **one frequency per route per round**, exactly as one thread would. Taking the top k gaps
    per round changed the results: it spent the budget elsewhere and missed a 740 MHz resonance on the real
    board.
  - **"Use all cores"** (2026-10-15, opt-in: `analysis.all_cores`, per machine from Preferences or `--all-cores`,
    never in the file; the user, of the Dell at 25x using 13 GB of 62 GB: "make it an opt-in 'use all cores'
    setting. and add an info icon to explain its potential issues"). `solver_threads` auto = every physical core
    (no MAX_THREADS cap); `refine_width(config, threads, runs)` = threads // runs refining, 1 to ALL_CORES_K (4), the
    k given to next_frequencies per round; the run budget max_solves x the width at the start, so the extra
    frequencies come on top of the sequential ones (the 740 MHz lesson above). Not reproducible across core counts;
    the result notes it. Test: tests/test_all_cores.py (a plane-capacitance board: 14 board-wide solves against 10,
    the same worst margin).
  - Measured on the real board (3 nets): 4 threads 189 s vs 1 thread 302 s, with identical results.
- **Reference frequency** `f_ref = max(f0, 1 MHz)`. Loop L, the ideal comparison and the board-wide offset are
  taken there: below about 1 MHz the return follows resistance, not inductance, so a converter switching at
  10 kHz is judged in its inductive regime.
- **Receiver bandwidth** (`emi/radiation.receiver_sum`). An EMI receiver reads all harmonics within ±RBW/2 at
  once: CISPR 16 uses 120 kHz below 1 GHz and 1 MHz above, and conducted standards use their own tables.
  Harmonics of a periodic signal are phase-locked, so they add coherently. Without this, a 10 kHz converter
  would read up to 20·log(RBW/f0) dB too low. Report rows are one per receiver bin, the bin keyed by its start
  frequency, and for converters at most 100 per decade (`thin_rows`).
- **Ideal reference**: the route's own reference layers, solid within the outline minus a keep-out around
  traces on them, stitched every 2 mm plus a return via 1 mm from each signal via. `l_ratio` = L / L_ideal.
  The moment comparison uses `m_ref = max(|m_ideal|, route_length × h)`, because a straight F→B route has
  near-cancelling halves and a tiny ideal |m| would exaggerate dB.
- Outputs per route: `RouteResult` with solves per frequency (L, R, |m|, iterations), the board-wide solves
  (`cavity_solves` with `m_used_mm2`, the combined moment the emission uses), layer share, per-layer |K| maps,
  `transfers` (vias/parts carrying return between layers/nets, merged per item, return-pin vias excluded,
  every one with ≥ 1 % of the current, plus one "plane capacitance A–B" entry per plane pair with ≥ 0.02 A per A;
  the maps may mark fewer, `max_vias`), emission table, worst margin, `resonance`.
- **Line current** (`emi/path.path_currents`, built by `simulate.path_elements`): trapezoid harmonics
  `c_n = 2AD|sinc(nD)||sinc(n f0 tr)|` (a `fall_time` of its own: `c_n = 2|X_n|`, `X_n = A/(j2πn)·[sinc(n f0 tr) −
  e^(−j2πnD) sinc(n f0 tf)]`) from a **Thevenin source**: `driver_ohms` (25 Ω) + jω·`driver_nh`, optional output
  capacitance `driver_pf`. The route is a **signal path**: a lossless line per stretch between series parts (its
  own Z0 and εeff from the stackup, `simulate._line_of`: an outer layer a microstrip, Hammerstad & Jensen 1980
  with their thickness corrections, on the dielectric height to its plane; an inner layer an offset stripline,
  Wheeler's stripline for each half combined, on the dielectric heights to both neighbours; validated against
  the 2D field solver, VALIDATION_AUDIT.md ISSUE-5: on kraft_flux_mini_2 within +3-4 % (outer, the solder mask
  not modelled) and ±8 % (inner) of the as-built cut, where the IPC-2141 / single-correction forms on the foils'
  centre spacing read +21 % and +52 %), each series part its component model's impedance
  (`ComponentModel.impedance`: R, L, ferrite, C with ESR/ESL; no model: a short), solved as a cascade per
  harmonic. The receivers load it with `load_pf` (5 pF each) and optionally `load_ohms` in parallel (a
  termination). The emission uses the **complex mean current along the path** (lines: Ī = I_in·sin(βl)/βl −
  j(V_in/Z0)(1 − cos βl)/βl; parts with their pad-to-pad length), because the plane solver drives the whole loop
  with one current. One stretch without parts is exactly the earlier `line_current` (test). Before 2026-10-10
  only a resistor's R was added to the source; ferrites, inductors and capacitors were ignored. The earlier V/Z0
  overestimated the fundamental of a short line by 27 dB (a short line is a capacitive load, not Z0). `current_a`
  on a net overrides it. A path crosses series parts whose model passes a signal (0R, R ≤ 1 kΩ, ferrite,
  inductor) and the parts named in the net's `through` (an AC-coupling capacitor, a larger resistor); when no path
  is found, `route_missing` names the parts leading on that don't pass.
- **Shunt parts on a signal path** (`find_shunts`, `_shunt_loops`, `emi/path.PathShunt`): a 2-net passive part
  (R, C, L, ferrite, 0R) from a path net to a reference net or a poured net (a rail), placed at the nearest point
  of the route to its path pin, is a shunt in the circuit. The current it takes returns through the plane from
  its other pin, so it is a **loop of its own**: the route up to its place, a stub to its pin, the part, injected
  at its return pin (`shunt_route`), solved on the same fine and board-wide models after the main solve (the same
  factorisation, another right-hand side; only the moment is kept, `ModelRun.loops` / `loop_m`). The moments
  add as complex vectors weighted by the loop currents over the path's mean current (`setup.loop_weights`,
  `_loop_moments` in `band_moments`; with the board-wide model each loop is m_fine(f0) + [m_board(f) −
  m_board(f0)]). Loop currents (`PathResult.loop_currents`): the drops of the groups' mean currents between
  shunts, so each stretch carries its own mean current; a group shorter than 2 % of the path (and 1 mm) takes
  the previous group's mean split as the currents divide at its node (with a point current there, standing waves
  on a 40 mm line made the weights ±16 at 3 GHz and the loops' 5 % geometric difference several dB). A shunt
  gets a loop only when its own current reaches 1 % of the path's at some harmonic (`SHUNT_MIN_SHARE`; a 1 MΩ
  pull-down doesn't, a 100 kΩ one on a lightly loaded path does); the others stay in the circuit and their
  current goes with the next loop. A loop that can't be built (its return pin far from plane copper) is left out
  with a note. The maps and transfers show the receivers' loop. Validation (`tests/test_path.py`): a 50 Ω part
  just beyond the receiver gives what `load_ohms: 50` gives with one loop to 0.10 dB median, 0.43 dB at most
  (the two loops' few mm of different geometry); an RC low-pass mid-path takes the current above its corner.
  The report shows the path per net (stretches, parts with |Z| at the worst harmonic, currents, receiver voltage).
- **Op-amp stages** (part model `amplifier`, `_amplified_route`, `_stage_routes`, `emi/path.amplified_path`,
  `PathAmp`; 2026-10-11, the user: "simply, amplification factor and maybe optional datasheet parameters ...
  pulled in via inventree"). The route passes an amplifier from its input pin to its output pin
  (`amplifier_pins`: the Parts tab's `pins`, else GT `amp_in`/`amp_out` labels, else the pin names +IN/IN+/INP and
  OUT of a single op-amp). The path is cut there into stages: the source (or the op-amp before) drives a stage,
  whose end is the next op-amp's input (`amp_z_in`: c_in 2 pF ∥ r_in) or the receivers; the next stage is driven by
  gain(f) × the voltage at that input behind `z_out` (1 Ω). gain(f) = gain / (1 + jf/bw), bw = `bandwidth` or `gbw`
  / max(|gain|, 1). Each stage's current returns to its source's return pin (the driver's, or the op-amp's nearest
  reference-net pin: its GND), so every stage is a current loop of its own (with its shunts), combined like the
  shunt loops; the main loop is the last stage's: the maps, loop inductance and ideal comparison refer to it.
  Weights are loop currents per volt of the source; setup.currents the source's harmonics × the slew cap (each
  output harmonic at most slew / 2πf; a crude large-signal limit, the harmonics are not redistributed). CMRR is
  stored and shown, without effect: the path is single-ended. Validation (`tests/test_path.py`, boardgen
  `opamp_path_board`): gain 1 passes the input voltage on, a pole is -3 dB at its bandwidth and -20 dB/decade
  above, the slew cap; on the board, two stages and loops, the output side returning at U5.2, gain 10 against 1
  +19.5 … +21 dB below 1 GHz (the input stage's own line charge is not amplified), more where the two loops cancel at
  gain 1 (~3 GHz).
- **Several loops are combined per harmonic** (`moment_curve`): each loop's moment vector is interpolated (linear
  in log f) to every harmonic and combined there with that harmonic's weights. Combining at the solved
  frequencies and interpolating the sum (as first done for the shunt loops) missed the weights' resonances: on the
  op-amp board the sum interpolated from 60 MHz to 3.2 GHz, where the output line turned the loops' weights
  opposite, came out up to 14 dB low.
  Tests: `tests/test_path.py` (series LC, AC coupling, a matched line, source L and C, the fall time against a
  numerical Fourier series, a board path through R, L and C).
- Emission: `|E| = 1.316e-14 f² |m| Ī / r`, ×2 for ground reflection, compared with CISPR 32 class A/B (QP at
  10 m below 1 GHz, average at 3 m above). `|m(f)|` is log-log interpolated between solved harmonics.
- `plane_resonance` (warning): the combined moment rises more than `resonance_db` (10 dB) above its f0 value
  at a solved harmonic. The message names the plane pair carrying the most displacement current there.

### Accuracy of the board-wide model (measured on the user's 4-layer board, SPI_SCK)
- Between resonances, the 0.71 and 0.49 mm grids agree within 1 dB, 16 of 25 frequencies.
- Resonances appear on both grids at the same frequencies (336, 734, 1345 MHz). Their peak heights differ
  by up to 6.7 dB, because a high-Q peak that moves by a few percent changes the value at a fixed frequency a lot.
  A margin set by a resonance peak is therefore good to several dB, which the report should be read with.
- Adding damping (a loss-tangent floor of 0.05–0.1) only reduced the spread from 5.3 to 3.3 dB rms before
  the rasterisation fix. That fix was the real cause, so no damping is added.
- Cost: 0.71 mm (32k nodes) ≈ 5–8 s per frequency, 0.49 mm (65k nodes) ≈ 25–40 s.
- **Circulating plane currents** (session 6, `tools/dev/transfer_check.py`, `simulate.layer_crossing`). A
  "plane capacitance" transfer is the net displacement current across a plane pair, |Σ I_cap|, from the
  board-wide model at the worst harmonic, and it can exceed the signal current. That is not a slip: KCL holds
  across every cut between plane layers to 1e-5 (Σ displacement + Σ via/part current = what the return pins put
  above the cut), and above 1× the two are in antiphase (179–180°): a current circulating through the plane
  capacitance and the vias joining the planes. Demo board (40×20 mm, In1/In2 GND joined only at the ICs,
  1.035 mm apart, 29 pF): 12.9 A per A across at 1.45 GHz with 11.9 A back through the vias, below the cavity's
  f10 = 1.69 GHz; it is the L‖C tank of the plane capacitance and the two vias: below the peak the share follows
  the tank's current divider x/(1 − x), x = (f/f_r)², with f_r = 1.31–1.34 GHz from 150–950 MHz. kraft_flux_mini_2
  (6 layers, SPI_SCK, both return pins on the same plane, so nothing has to cross): purely circulating,
  5.5× across In2–In3 and 4.3× across In4–B.Cu at 740 MHz, the 0.135 mm cores' (1,0) mode (691 MHz for
  88 mm). The model has no radiation loss, so such a peak's height is an upper estimate. The displacement is
  spread over the pair, so the maps don't mark it; the "plane displacement" tile and the transfer lists give
  it with its frequency (`transfers()` adds `"f"`, `"distributed"`), and "main transfer" is the largest via or
  part (`point_transfers`).

### Memory use of a Simulate (session 8, branch `solver-memory`)
The user: "Thoroughly investigate and optimise memory usage in the PCB solver". Measured with
`tools/dev/mem_profile.py` (private bytes = committed memory, which hits the commit limit and raises MemoryError,
and working set = Task Manager's "Memory", sampled every 50 ms; a census of the live solver objects near the peak),
`tools/dev/mem_boards.py` (several boards in one process) and `tools/dev/mem_probe.py` (failures, Cancel).
- **What a Simulate holds.** `_simulate` builds every model first (each route's fine and board-wide models, each
  power stage's and bridge's) and keeps them to the end; the result keeps them for the fix search. Per model: the
  kernel spectra (`_kernels`), the incidence matrices A / Ar / ArT, the masks and index arrays, cached
  factorisations (`OPERATOR_CACHE` per model, each a SuperLU object), and the stored solutions. Per solving
  thread: a GMRES Krylov basis and the FFT work arrays.
- **What was too much** (kraft_flux_mini_2, all 7 nets and stages, 4 threads, before):
  - SuperLU commits ~6.5x its fill (scipy allocates from a fill estimate and grows by doubling, never trims;
    untouched, so committed but not in the working set): 41 factorisations at the peak, ~2.6 GB of fill, held
    through the finishing phase although it uses none of them (it solves only new models).
  - The kernels were kept twice, as a dict of full per-pair spectra still holding their complex FFT buffers (the
    `.real` of a complex result is a view) and as a full (P0, P1, N, N) copy for the compiled multiply-add: 723 MB.
    The module kernel cache held 48 such complex-backed pairs for the process's life.
  - Each solution stored the solver's unknown vector as well as the currents and potentials it is made of, and its
    `I_branch` / `I_cap` were views that kept that vector alive anyway: 612 MB for 93 solutions.
  - The GMRES basis was allocated at its full restart length (up to 400 MB) every restart.
  - A race in `HostedLU.__del__` (the cross-thread SuperLU leak's fix): a factorisation dropped while its host
    thread was idle could still be freed in the dropping thread, its memory kept; with the factorisations
    dropped before finishing it kept +335 MB per Simulate on a test board, and `main` kept 13.8 GB after the large
    run with its result released.
- **Changes** (all in `solver/peec.py` unless noted):
  - kernels stored once, as one quadrant of each spectrum (they are even in both axes to 4e-17), `_kq`
    (P0//2+1, P1//2+1, N, N); the compiled loop (`_mac.py`) mirrors the index; `kernel()` / `Kx` / `Ky` unfold
    full arrays on demand (numpy fallback, GPU backend, tools). 624 → 72 bytes per padded pixel for 6 layers.
    Keeping only the N(N+1)/2 distinct pairs (42 bytes) made the compiled loop 10-35 % slower (a pair lookup in the
    inner loop); with all N² pairs it runs as before (16.1 against 15.2 ms at 462 x 528, 6.1 against 7.3 at
    240 x 420; identical output). The module cache keeps quadrants, bounded at `_KERNEL_CACHE_BYTES` 128 MB.
  - `OPERATOR_CACHE` 4 → 2, and `simulate` frees every factorisation once the solves are done
    (`drop_factorisations`), unless `keep_factorisations` (the fix search's own baseline, whose candidates are
    low-rank updates of them).
  - solutions keep no unknown vector (`Solution.state_for(stack)` rebuilds it exactly for warm starts) and copy
    `I_branch` / `I_cap`.
  - the GMRES basis grows from `GMRES_FIRST_ROWS` (16) by doubling, and is reused across restarts.
  - `HostedLU.__del__` hands its host a list holding the only reference.
- **Measured** (4 threads; "before" = `main` at 79b2f60 in a worktree, same inputs, one after the other on an
  otherwise idle laptop):

  | Board | | Committed peak | Working-set peak | With result | After it is released | Time |
  |---|---|---|---|---|---|---|
  | kraft, 7 nets and stages | before (2 runs) | 29 686 / 29 873 MB | 5 479 / 5 511 MB | 16 089 / 4 730 MB | 13 759 / 2 409 MB | 464 / 460 s |
  | | after | 18 813 MB (−37 %) | 3 420 MB (−38 %) | 2 318 MB | 836 MB | 459 s |
  | kraft, SPI_SCK + conv1 | before (3 runs) | 13 893–13 899 MB | 2 292–2 363 MB | 1 596–1 607 MB | 841–844 MB | 200, 226, 179 s |
  | | after (2 runs) | 9 592–9 598 MB (−31 %) | 1 869–1 876 MB (−20 %) | 1 287–1 297 MB (−19 %) | 778–786 MB | 215, 177 s |
  | test board (layer change) | before (2 runs) | 2 603–2 606 MB | 531–532 MB | 975–978 MB | 745–748 MB | 10, 8 s |
  | | after (4 runs) | 1 824–1 891 MB (−29 %) | 488–532 MB | 944–947 MB | 729–730 MB, flat | 7 s |

  ("After it is released" before varied with the `HostedLU.__del__` race.) Near the peak on the 7-stage board:
  factorisations 41 → 23 (fill 2 601 → 1 443 MB), kernels 723 → 102 MB, solutions 612 → 323 MB; factorisations
  made 127 → 132. Results: the test board and SPI_SCK + conv1 agree to 1e-12 and 2e-6 (rounding through GMRES:
  the mirrored kernel quadrants; one solve 202 → 201 iterations); the 7-stage run differs by up to 0.022 dB at
  conv1's highest harmonics, where a board-wide solve borrowed another (equally valid) factorisation with the
  smaller cache (88 → 194 GMRES iterations). Two runs of `main` itself differ by up to 0.0022 dB there (which
  factorisation a solve borrows depended on the threads' timing; reproducible since: "Reuse"), so the cache size adds about ten times that.
- **Since** (branch `memory`): factorisations of models no run still refines are freed after each solve round
  (`solve_setups(free_done)`); with `threads: auto` the parallel solves are capped by the free memory
  (`memory_threads`, `solve_bytes`: estimated fill x 16 B x 6.5 + the GMRES basis per solve); the plugin keeps a
  Simulate's models for Suggest fixes only while they take < 1/4 of the free memory (`keep_models`,
  `SimulationResult.model_bytes` / `drop_models`), else Suggest fixes simulates its own baseline.
- **Groups** (session 10): when the factorisations kept at once would not fit in the free memory, the models are solved in groups, one after the other (`solve_groups`): whole components of runs sharing a stack, the board-wide model last, so every number is the same. kraft_flux_mini_2, 7 nets and stages: committed peak 19.4 → 10.4 GB, working set 3.5 → 2.4 GB (12 groups on a 17 GB laptop).
- **Left** (the next steps): SuperLU's committed slack (~6.5x its fill) dominates the committed peak; only a
  different sparse solver (PARDISO, UMFPACK, a complex-symmetric LDLᵀ) would remove it, a new dependency. The
  working-set peak is mostly the live factorisations' fill (1.4 GB of 3.4 GB on the large board), then the
  solutions (323 MB: dense complex grids per solved frequency), the incidence matrices and index arrays (373 MB).
  Building the models one route at a time instead of all first would lower the peak further, at the price of the
  shared factorisations between routes on the same board-wide model.

### Validation (tests)
- Microstrip over a solid plane: return profile `K(y) = h/π(h²+y²)` within 6 %, loop L within 3 % of
  Wheeler, |m| = length·h.
- Low frequency spreads the current along the resistive path; slots raise L steadily; an edge split
  creates an in-plane moment; a full cut raises `SolverError` → `return_path_broken`.
- 4-layer layer change: the nearest stitching via carries the transfer (0.8 mm via: 71 %), and a farther via
  gives larger L and |m|. A +3V3 reference without decaps carries no return (In2 share < 2 %, L 1.40×
  ideal). With decaps at the ICs, C3 carries ~27 %.
- Plane capacitance (`tests/test_capacitance.py`): parallel plate C matches ε0 εr A/t exactly; a 40×20 mm
  plate pair resonates at f10 = c/(2a√εr) within 3 %; in a simulation the board-wide model runs at the
  harmonics, and the displacement current across a missing stitching via grows from < 5 % at 50 MHz to
  > 50 % at 1.5 GHz. Across the In1|In2 cut, displacement + via current = −1 A per A at every frequency (KCL,
  the return climbs from In2 to In1); below the plane/via tank's resonance the displacement share follows
  x/(1 − x), x = (f/f_r)², within 5 % (f_r from 200 MHz predicts 400 and 600 MHz; measured 0.8 % off at
  400 MHz, 4 % at 700 MHz, where the distributed cavity starts to show), and
  above it the displacement exceeds the signal current in antiphase with the vias.
- Cross-section (`tests/test_section.py`): a microstrip 0.235 mm over its plane carries (2/π)·atan(W/h) of the
  return within ±W (within 0.5 % at W = 2h, 5h, 20h) and all of it across the whole board (1.0000, in phase);
  at a stitched layer change the return swaps planes within 0.5 mm of the signal via, unstitched it crosses
  over gradually with ~30 % left on the plane the signal left.
- Rasterisation (`tests/test_raster.py`): a row of antipads leaving 0.14 mm webs stays one piece with the
  sub-grid (point sampling cuts it), a 0.3 mm slot still cuts on a 0.5 mm grid, and plane-pair capacitance
  follows the copper fraction of perforated planes within 3 %. A track with 45° bends through another net's
  pour stays one piece from 0.3 to 0.86 mm cells (cut at 0.86 mm without its bridges); a bridge has the DC
  resistance l/(σwt) and Ruehli's partial inductance of a bar within 5 %.
- Fixes (`tests/test_whatif_suggest.py`): what-if vias/caps make the Tier 1 issue disappear, candidates
  respect clearance and edge rules, the suggested stitching via is verified to fix the layer change, a
  same-net slot gets a fill, and `suggest --write` → `check --what-if` → `compare` works end to end.

## Power stages (`analysis/power.py`, `emi/conducted.py`)
A converter's switches commutate the load current within the edge time. That current flows around the hot
loop as a trapezoid pulse train (`ConverterSpec.pulse()`: amplitude = load current for a buck, input current
for a boost; duty = the high-side conduction time). The inductor current is continuous and stays out of it.

- **Elements** (`elements_of`): the high side (rail → switch node), the low side (switch node → ground, a FET
  or catch diode), or one integrated stage (rail → ground, both switches in the IC). Parallel parts split the
  current equally.
- **Filaments** (`switch_filaments`): per element, a known current runs from each pad's grid node up
  `PACKAGE_H_MM` (0.25 mm), across to the package centre, and over and down to each pad on the other net. Pads
  share the current by area, which is what `TraceSegment.i` (a per-filament current weight) was added for.
  Injections: −share at the "from" pads, +share at the "to" pads. The filaments end exactly on the injection
  nodes, so the loop is closed.
- **Model region**: the cell, the rail capacitors within 20 mm (or within 5 mm beyond the nearest one), the
  input connector, the series parts of the input path (`path_parts`: a surge stopper beyond the connector once
  cut the input path) and the inductor's switch-node pad, plus a margin (`POWER_MARGINS_MM` 5 mm). If the loop
  does not close (the injections don't balance), the margin widens to 10, then 20 mm, with a note. A capacitor
  ground patch stitched to the plane only farther away must stay in the model, because it is the detour to
  find. Layers: all plane layers there plus the component sides.
- **Split models** (`_pitch`, `_loop_model`): when one model spanning the input path (connector, series
  parts) would get a coarser pitch than the hot loop's region alone, estimated from the plane layers of each
  region, the input path gets its own model with the LISN.
  - The hot-loop model (`fine`) gives L, the maps, the capacitor shares (f_sw is then solved on it), the
    radiated estimate and the switch-node DC path.
  - The input model (`cond`) gives the conducted transfer and the DC input path.
  - `_transfer` and `next_conducted` use only the `cond` solutions when the models differ.
  - On the real board a single model reaching the surge stopper made SW1's grid 0.35 mm (L 3.20 nH against the
    converged 3.45).
- **Nets**: every zone net in the region, plus the rail, switch node and ground, plus the input path. The input
  path is a BFS from the rail through parts with a series model (short, ≤ 10 Ω resistor, inductor, ferrite).
  A series FET such as a surge stopper needs a short or resistor model; its gate net is not a plane net, so
  the FET becomes a two-net branch.
- **LISN**: `with_branches([Branch(+pin, −pin, 2·Z_lisn(f))])`, a Woodbury update of the shared
  factorisation. The two lines' LISNs are in series for the differential-mode loop. LISN models
  (`emi/conducted.LISNS`):

  | Standard | Model |
  |---|---|
  | CISPR 32 | nominal 50 Ω ∥ (50 µH + 5 Ω) |
  | CISPR 25 | 50 Ω ∥ 5 µH |
  | MIL-STD-461 | the drawn network (50 µH, 8 µF + 5 Ω, 0.25 µF to the port) |

- **Runs** (all on the same stack): `fine`, `cond` and the board-wide `cavity`. `ConverterSetup` duck-types
  `RouteSetup` for `solve_setups`, which gained `extra_runs` with their own `refine(run, config)`, a
  `planned_solved()` hook after the first round, and `model_label`.

  | Run | Frequencies | Purpose |
  |---|---|---|
  | `fine` | 30 and 100 MHz (plus the radiated harmonics when there is no board-wide model) | L, maps |
  | `cond` | f_sw plus 2.5 log-spaced points per decade over the standard's range, refined where the log-log interpolation may be off by > 1 dB (slope change against the neighbouring gaps × width / 8: a power law is not refined, a self-resonance notch is) near the limit, at most 12 solves | ripple sharing, conducted band |
  | `cavity` | harmonics above 30 MHz | radiated emission and plane resonances, as for nets |

- **Loop inductance** (`loop_inductance`): a series-LC fit through X(30 MHz) and X(100 MHz), so
  L = (ω2X2 − ω1X1)/(ω2² − ω1²), removes what is left of the capacitors' reactance. It falls back to X/ω at
  100 MHz.
- **Ringing** (with `c_oss`):
  - f_r = 1/(2π√(L·C_oss)) and Q = √(L/C)/(r_damp + Re Z_loop), with `r_damp` defaulting to 0.1 Ω (ringing
    that dies out within a few cycles).
  - The pulse spectrum is multiplied by |1/(1 − x² + jx/Q)|, x = f/f_r, and by `edge_charge_factor`
    |1 + Y_node·V/I|: the edges move the node by V in tr, so the loop carries Y_node·V_sw on top of the load pulse
    I (C_oss·dv/dt at a bare node, plus a snubber's current). KCL at the node with the loop equation gives
    i = H·(I_pulse + Y_node·V_sw) exactly for the linear cell (stiff input, L + r, a forced node edge, the gated
    load current), and both are trapezoids with the same duty and edges, so the sum is the pulse times that
    factor; with Y = jωC it is √(1 + (ωCV/I)²), the two in quadrature. The charge per volt is `q_oss`/V where
    given (C_oss is larger at low voltage), else `c_oss`. Test: a time-domain integration of that circuit
    (with and without a snubber) matches pulse·|H|·factor to 0.2 % per harmonic. Before (session 7 audit), the
    spectrum had the load pulse alone, while the overshoot below already assumed the voltage edge: against a
    time-domain switching-cell simulation of kraft_flux_mini_2's SW1 (10 A, 51.8 V, 616 pF, 1.81 nH) it read
    8 dB low at the ringing, SW2 (5 A) 15 dB; with the factor both are within ~2 dB, on the high side (the
    real turn-off is slower, driven by the load current).
  - Overshoot = V·exp(−πζ/√(1−ζ²))·|sinc(f_r·tr)|: an underdamped step response, reduced by the ramp.
  - The L·di/dt overshoot is reported too. The larger of the two, as a fraction of the rail voltage, sets the
    `hot_loop` severity (`overshoot_warning` 0.25, `overshoot_error` 0.5).
- **Conducted estimate** (`conducted_arrays`):
  - The input transfer H(f) = |I_LISN|/I_loop comes from the solves, interpolated log-log.
  - Line current per harmonic = pulse current × H. LISN voltage = I·|Z_lisn|·|R_m/Z_meas|, taken as RMS.
  - The standard's receiver bandwidth sums neighbouring harmonics, and the result is compared with the
    average limit.
  - CW harmonics read the same in peak, quasi-peak and average, as long as f_sw exceeds the receiver
    bandwidth.
  - Limits: CISPR 32 class A/B (AC mains port), CISPR 25 voltage method classes 1–5 (band-limited), and
    MIL-STD-461 CE102 (basic curve plus the voltage relaxations). They were entered from the published tables:
    verify them against the edition in use.
- **Results** (`ConverterResult`, a `RouteResult` subclass, so the heatmap, spectrum, text, JSON and HTML code
  takes it):
  - loop L and R, the L·di/dt overshoot, the ringing estimate;
  - capacitor shares at 100 MHz and at f_sw, with the LISN's share listed too;
  - the input transfer, conducted rows and worst case, radiated rows and worst case, the plane resonance;
  - maps of the loop current at 100 MHz, over all copper, the loop's own included;
  - `transfers`: the vias and parts that carry it.
- **DC power paths** (`dc_runs`, `dc_analysis`, `dc_path`), solved at `DC_F` = 10 Hz (no LISN) on one
  whole-board copper model shared by every stage (`dc_board_build`, see "Whole-board DC model" below; the stage's
  own models only when that build fails). At 10 Hz the problem is resistive: the skin depth is far larger than
  the copper, and the jωL and capacitor terms vanish.
  - The paths:
    - The switch node carries `i_out` from the high-side "b" pads (share D) and the low-side "a" pads
      (share 1−D) to the inductor's switch-node pads. For an integrated stage it runs from the IC's pads on the
      net it shares with an inductor (`switch_inductor`).
    - The input carries the expected input current I_in (`input_estimate`, below; `i_out·D` without losses) from
      the connector's plus pin to the high side's rail pads (only with an input connector).
    - With `output_connector` (`_output_runs(dst=)`, on the whole-board DC model; without it on a model of its
      own around the stage, both connectors and the series parts between, whose region widens 5 → 10 → 20 mm
      while a path doesn't close):
      - the output carries `i_out` from a buck's inductor (its pads off the switch node) or a boost's high-side
        rail pads to the connector's plus pin, through series parts (`input_path` from the output net);
      - the ground return, one solve on the ground copper, split by Kirchhoff at the ground node with the
        expected I_in. Buck: `i_out` enters at the connector's minus pin, I_in leaves through the input
        connector's minus pin (the input current's return) and I_out − I_in through the low side's ground pads
        (D·I_out and (1−D)·I_out without losses). Boost: I_in leaves at the input connector's minus pin, I_out
        comes back from the load ((1−D)·I_in without losses) and the rest from the low side (D = the high side's
        conduction time, `ConverterSpec.pulse`; the boost's shares were swapped before 2026-10-12,
        `test_a_boosts_ground_return_splits_by_kirchhoff`). Without an input connector a buck's whole return goes
        to the low side and a boost's is not modelled.
      On the test buck the via currents of the ground return match Kirchhoff exactly (3 A at the load's via,
      0.75 A at J1, 2.25 A over Q2's three vias), and the output track's R is its square count within 3 %.
    - `detect_converters` finds the output connector like the input one (`_connector_for` from the output net,
      never through the switch node or the input rail).
  - Each path lists every via with ≥ 1 % of its current (`dc_summary`), with its IPC-2221 capacity for a 10 K rise
    (`analysis/ipc.via_capacity`, the plating barrel as an internal conductor); `via_overload` fires at
    `rules.via_load_warning` (100 %) / `via_load_error` (150 %) of it. A part's plated hole (a branch named
    `pad REF.N`) is left out, as in `bridge_rms`: its soldered pin carries the current. `add_vertical(...,
    filled=True)` gives such a hole (of a part that is not DNP) the resistance of its whole cross-section,
    l/(σπr²), not the plating's l/(σ·2πr·25 µm) (`via_impedance(filled=)`). kraft_flux_mini_2's XT30 pins read
    113–187 % of the plating's capacity before (audit, session 7).
  - Every power stage notes its ceramic capacitors of 1 µF and up still at their nominal C on a rail of
    `NOMINAL_BIAS_V` (10 V) or more (`nominal_ceramics`; electrolytics by `defaults.is_electrolytic`), pointing to
    the Parts tab's C at bias.
  - Solve rows (`_stage_row`) carry X_ohm; below the capacitors' self-resonance the loop is capacitive and
    L_nH is None (it read −1829 nH at 10 kHz). A stage whose rail is another buck's output (`feeding_stage`)
    says it is cascaded instead of blaming series switches for the missing input path.
  - Each pad's current is spread evenly over all grid nodes inside its outline (`_pad_nodes`). One node per pad
    would add a spreading-resistance singularity of about 0.1 mΩ, as much as a whole path.
  - R = Re(Σ aₙVₙ) for 1 A. The density is |K|·I/t per layer, with t from the stackup:
    `BoardModel.t_phys` → `StackBuild.t` → `PlaneLayer.thickness`, which until session 3 was always 35 µm. The
    via density is I/(π·drill·25 µm).
  - "Sustained" averages the density over the copper within a 1 mm window (`sustained`, `uniform_filter`
    restricted to copper cells).
  - The `current_density` issue fires when the sustained density or a via's plating density exceeds
    `rules.current_density_warning` (50) or `current_density_error` (100) A/mm².
  - `dc_view` makes a copy of the result whose heatmap is a DC density map; the plugin and the HTML list these
    views.
  - **Whole-board DC model** (`dc_board_build`, session 10): one inductive-free copper model per Simulate, cached
    on the `BoardModel` (`model._dc_board`), over the board outline's bbox, with every plane net plus every
    stage's `dc_nets` (rail, switch node, ground, input and output nets and the nets through their series parts)
    on every layer carrying plane copper. Pitch: `_choose_pitch` from the dielectric height below the stages'
    parts (as the region models start), coarsened until DC_BUDGET (3) × `max_nodes` × resolution holds. Why:
    the 5 mm box around a stage cut kraft_flux_mini_2's ground return at its edge (−7 % on its R), and the
    stages' currents can only add on one grid. A fixed 0.25 mm start widened the test buck's 1 mm output track
    (R −16 %); on kraft the budget binds first (503k nodes at 0.21 mm at resolution 2, Simulate +12 %). One
    factorisation serves every DC solve of every stage. The hot loops' high-frequency models keep their regions.
  - **Expected input current** (`input_estimate`): V_in·I_in = V_out·I_out + P_loss, with the parts' losses that
    Thermal uses (`thermal.part_losses`: conduction, the hard edge, Q_oss·V·f, a diode's V_f, the inductor's
    DCR, series parts) plus, once the DC paths are solved, their copper (r_copper·I²). Not counted: gate drive,
    the controller's supply, core loss, dead-time conduction. `dc_runs` solves the input path at the estimate
    without copper; `finish_converter` recomputes it with the copper and scales the input path's figures
    (`_rescale_path`, DC is linear; also `dc_branch`). Shown in the DC input view's title, the stage's Heatmap
    line and a loss break-down note. Test: `test_input_current_is_output_power_plus_losses_over_the_input_voltage`.
    The result's `duty` is the high side's conduction time in both topologies (`ConverterSpec.pulse`): a boost's
    is its rectifier's V_in/V_out, so `part_losses` gives a boost I_L = I_out/d, its rectifier d and its switch (the
    low side) 1 − d (until 2026-10-17 it read d as the switch duty: right only at ½; VALIDATION_AUDIT.md ISSUE-1).
  - **Switch node RMS (not modelled):** the switch-node path feeds the averaged current (D from the high side,
    1 − D from the low side) where the copper carries each in turn: its copper loss and heat read D·(1−D)·I²·R_hl
    low (R_hl the resistance between the switches' pads along the node), 6-17 % of the path's loss on the test
    buck (VALIDATION_AUDIT.md ISSUE-7).
  - **Stages together** (`combine_dc`, called by `simulate()` after the stages finish; `SimulationResult.
    dc_combined`): every path keeps its signed branch currents (`ConverterResult.dc_branch[path] = (build,
    I_branch·I)`), so with two or more stages on the one model their density vectors and branch currents add
    (superposition: the DC solve is linear). From the sum: the hotspots (`_density_peaks`), every via with
    ≥ 0.01 A against IPC-2221, the copper's Joule loss Σ ρ|J|²·t·d², the total input current. The view is a
    copy of the first stage with net COMBINED_NET ("all power stages") and path COMBINED_VIEW ("currents
    together"), first in the Heatmap tab's Route list (`main_frame`); its `current_density` / `via_overload`
    issues replace the stages' own DC-path issues (the copper carries all loads at once). Stages solved on
    different models (the whole-board build failed) are not combined, with a note. Not in the HTML report's maps
    yet (its issues are). Test: `test_the_stages_dc_currents_add_up_on_one_board_model` (two identical stages:
    exactly 2× one stage everywhere).
  - The ground return of the load isn't analysed: the load's location is unknown.
  - Validation: a 4 mm × 35 µm pour gives R per length = 1/(σwt) within 3 % (measured +2.6 %, width
    quantisation on a 0.1 mm grid), its middle density I/(wt), and 70 µm copper halves both. A 3 A buck with a
    1 mm switch-node track shows ≈ 73 A/mm² sustained there (I/(wt) = 86) and raises `current_density`.
- **Boost conducted emission** (`boost_conducted`, `ripple_harmonics`). A boost draws its inductor current from
  the input, so its pulsed hot loop sits on the output:
  - The ripple ΔI = V_in·D/(L·f_sw), with D = 1 − V_in/V_out and L from the inductor's model, is an
    asymmetric triangle with c_n = ΔI·|sin(nπD)|/(π²n²·D(1−D)).
  - It gets its own input-side model: a pseudo element from the inductor's input pads to the low side's
    ground pads, plus the input capacitors (on the inductor's input net), the connector and the series
    parts, with the LISN.
  - The conducted run uses these harmonics (`cond_freqs`/`cond_currents`). The DC input path ends at the
    inductor and carries the inductor current.
  - Test: ΔI matches the formula, the divider matches C1 against the LISNs within 5 %, and the reading at
    f_sw is c_1·H·|Z_LISN| within 0.3 dB.
- **Input-filter guidance** (`input_filter_advice`): from the conducted rows that miss `emi_margin_db`, the
  missing attenuation A at each harmonic gives f_c = min f·10^(−A/40) for a π-filter (series L, plus C at the
  connector, with the input capacitors on the other side). C is the smallest standard value keeping
  L ≤ 4.7 µH; the damping is R = √(L/C) with 4·C. It is advice text, not modelled in the copper.
- **Spread spectrum** (`emi/spread.py`; converter `spread`, `spread_mode`, `spread_rate`; session 4). The
  switching frequency moves between f0 and f0·(1 + spread), so harmonic n sweeps n·f0 … n·f0·(1 + spread). What
  that does to a reading depends on the receiver and on the modulation, so the receiver is simulated rather
  than estimated: the harmonic's complex baseband signal over one dwell (15 ms, MIL-STD-461G Table III), a
  Gaussian filter with the standard's 6 dB bandwidth tuned across the sweep (coarse grid, refined to RBW/8), and
  the detector (`conducted.detector`: peak for MIL-STD-461, average for the CISPR average limits). Gains are
  simulated at ≤ 16 log-spaced harmonics in the standard's range and interpolated; overlapping sweeps of
  neighbouring harmonics (n·spread ≥ 1) add their overlapping share as power. For f0 ≤ RBW the spread is ignored
  (conservative). Applied to the conducted rows, the refinement margins and the common-mode budget; not to the
  radiated estimate. Validation (`tests/test_spread.py`): sinusoidal FM reads the largest Bessel line |J_k(β)|;
  a slow sweep reads ≈ 1; a fast linear sweep reads the Gaussian-chirp gain (1 + (μa/π)²)^-1/4; random hops
  match the Rayleigh mean (average detector). Findings: a peak detector sees a slow sweep at full amplitude;
  random hopping every cycle is a phase random walk, the line broadens to only ~(2π·spread)²·var·f0/π (tens of
  kHz) and gains ~1 dB at the fundamental, while a 10 kHz triangle gains ~9 dB (kraft_flux_mini_2 SW1).
  Datasheets (LTC7890/7891) give the range (0 to +20 %) but not the modulation: treat it as a bracket.
- **Common mode** (`cm_estimate`):
  - The switch-node voltage is a trapezoid (rail voltage, duty, edges), and its harmonics V_n drive
    I_n = 2πf·C·V_n through the stray capacitance C to the chassis or heatsink.
  - The current returns through both lines' LISNs in parallel, so each carries and reads half (a floating EUT).
  - The reading is linear in C. The budget is therefore 1 pF × 10^(worst margin at 1 pF / 20), reported with
    the frequency where it binds.
  - With `c_stray` the estimate is reported too, and a `conducted_emission` issue (key `cm`) is raised when its
    margin is below the warning level.
  - Test: the budget matches the hand formula, `c_stray` = budget gives 0 dB, and 2 × budget gives −6.02 dB.
  - The capacitance itself is not computed: it is dominated by the mechanics (heatsink tab, housing).
- **Fix suggestions** (`suggest.power_via_candidates`, `_PowerEvaluator`):
  - Candidates: the rail and ground pads of the rail capacitors and the switches that have no via or THT pad
    of their net within `stitch_distance_mm`. Each gets a via of the pad's net nearby (`via_candidates`,
    normal placement rules), landing in the net's copper on the pad's layer and on the nearest other layer that
    has it there.
  - Prediction: ΔL and Δ|m| at 100 MHz on the converter's fine model, as a Woodbury update with a warm start.
  - Capacitor candidates (`power_cap_candidates`): 100 nF / 0.4 nH (0402) between the rail and ground, anchored
    between the high side's rail pads and the low side's ground pads (an integrated stage: its pins), placed
    by `cap_candidates` with a via per pad to its net's plane.
  - Selection (`greedy_pick`): from the 8 best single candidates (ΔL ≤ −1 %), add one at a time. Each round
    re-evaluates the remaining candidates with the chosen ones in place (`_PowerEvaluator.evaluate_set`, in
    parallel) and adds the one that lowers the combined L most. It stops when the best addition gains less than
    1 %, or at `max_fixes`. On the real board, three single GND vias at neighbouring capacitors each gave
    −31 %, but together only −35 %: the first one already closes the detour.
  - Converter issues no longer receive the signal-net advice texts.
  - `compare_results` adds rows of kind "converter" (hot-loop L, radiated and conducted margins).
  - Test: a capacitor whose GND patch is stitched 6 mm away gets a GND via next to its pad, predicted −14 % L.
    The verification run shows −7.5 %: the "after" model no longer needs the widened region, so it is a
    smaller, finer model. Compare L values of different regions with that in mind.
- **Detection** (`detect_converters`):
  - A net between an inductor and a FET, a diode or an IC's SW/LX pin is a switch node.
  - A FET's other power net is its most-padded net, preferring ground, poured or power-named nets and never the
    gate. If it is ground, the FET is the low side; otherwise it is the high side and its net is the rail.
  - A diode with its cathode on the switch node and its anode on ground is a catch diode. With its anode on the
    switch node it is a boost diode.
  - An IC with a SW pin and no FET becomes the high side (with a catch diode) or the integrated stage. Its rail
    and ground come from its VIN and GND pin names.
  - Voltages are parsed from net names (+12V, +3V3). A rail lower than the inductor's far net means a boost.
  - EPC/GaN parts default to a 2 ns edge.
  - `detect_nets(skip=...)` leaves out the detected switch nodes.
- **Validation** (`tests/test_power.py`, `tests/test_emission.py`):
  - Hot-loop L per length of a 4 mm VIN pour 0.235 mm over GND matches Hammerstad within 10 % (measured
    −3.6 %).
  - Below the capacitor's self-resonance, the LISN current divider matches Z_C/(Z_C + 2·Z_LISN) within 3 %
    (measured 0.1–1.1 %).
  - Identical capacitors placed symmetrically share 49.5/50.5 %. A 100 nF next to the switch takes 67 % at
    100 MHz, and a far 10 µF takes 99 % at the switching frequency.
  - The SW pour of a discrete buck carries the full loop current between the FETs.
  - Ringing f/Q follow their formulas; the limit lines and LISN impedances match the standards' formulas.
  - Detection works on discrete and integrated stages, the settings file round-trips, and the CLI runs `detect`,
    `simulate`, JSON, JUnit and HTML end to end.

## Radiated limits and spread spectrum (2026-10-16, branch `weak-spots`)
- `emi/radiation.RadiatedLimit` (settings `analysis: radiated_limit`, `config.settings._radiated_limit`): a user
  curve (log-f linear between points, NaN outside), its distance and bandwidth table (`MIL461_RBW` = MIL-STD-461G
  Table II, or CISPR 16's). `AnalysisSettings.limit_spec()` / `limit_text()` replace `limit_class` at the call
  sites (simulate, power, fix_advisor, report). RE102's platform curves are built in (`RE102_LIMITS`, by name in the
  settings), read from the released MIL-STD-461G's Figures RE102-1 to -4 (first left to the user: secondary sources
  disagreed; the standard itself turned up with CE101). The submarine internal curve's low end is read off the
  figure (not labelled there). The radiated band follows the
  limit (`limit_range`: CISPR 30 MHz-6 GHz, a user curve its points): `initial_frequencies(band=)` and the stages'
  harmonics from half its low end.
- `loop_field` is the small loop's broadside E_phi with the induction term, |1 + 1/(jkr)|: MIL-STD-461's 1 m is
  near field below ~48 MHz (k r = 1); at CISPR's 10 m from 30 MHz it adds at most 0.11 dB.
- Spread spectrum in a stage's radiated estimate: `emission_arrays(spread=)` with `spread.spread_readings` (as the
  conducted estimate), where f_sw exceeds every bandwidth used. `spread_readings` for a peak detector now reads the
  largest single pass where sweeps overlap (the harmonics are f0 apart, more than the RBW: one in the filter at a
  time); the power sum, kept for an averaging detector, read up to 6 dB above the undithered harmonic in the radiated
  band and a few dB pessimistic at high conducted harmonics (kraft-like 750 kHz, 20 %: n = 40 read -18 dB instead of
  the own pass's -25 dB).
- The receiver simulation (`emi/spread._gain`) had to change for radiated sweeps (a 20 % sweep at 1 GHz is
  200 MHz wide; 15 ms of it would be ~10⁸ samples, the first test run sat in it for an hour):
  - `_dwell_plan`: triangle/sine synthesise whole modulation periods (≥ 4/RBW) instead of the 15 ms dwell (the
    filter output is periodic once settled); random hopping a shorter stationary stretch, its peak scaled by
    sqrt(ln(RBW·dwell)/ln(RBW·d)) (Rayleigh). Beyond MAX_SAMPLES = 2²⁰: not simulated, `_extrapolate` continues the
    last simulated slope, clipped to [-1/2, 0] in log-log (the wide-sweep limit; never optimistic).
  - `_read` filters only the bins within ±4 RBW of the tuning and inverse-transforms those in a short array (the
    envelope on a coarser time grid, ≥ 16 samples per 1/RBW): within 0.03 dB of the full inverse FFT, ~1000× less
    work per tuning, tunings batched as rows of one 2-D inverse FFT. The fix advisor ranks its spread options by
    the conducted margin without the radiated spread (`Change.radiated_spread=False`) and reads the radiated
    estimate with spread for the chosen one only (random hopping over 30 MHz-6 GHz still takes ~10 s). A test
    Simulate with the fix advisor (test_dc_paths_of_a_discrete_buck...): 114 s on main, 6 s now.
  - The tuning search found a lesser peak on wide sweeps (5 dB optimistic at a 27 MHz triangle sweep: the worst
    tuning lies 1-3 % of the width in from an end, between the 25 coarse tunings). Now: the outer 5 % of each end
    at RBW/4, random hopping the whole band at RBW/2 (≤ 2000 tunings), refined around the three best. Checked
    against a dense RBW/8 scan: within 0.03 dB. This also changes conducted spread readings slightly (up).
- The CE102 relaxation for 115-440 V sources was already applied (`MIL461_RELAX`).
- **CE101** (the user: "this is a valid frequency range though ... implement"; limits read from the released
  MIL-STD-461G of 11 December 2015, Figures CE101-1 to -4, Table II): `emi/conducted` `Ce101` (platform, source V,
  load A), `CE101_CURVES` (CE101-4 curves #1/#2, CE101-1 ships DC with its load-current relaxation; the AC ship
  curves CE101-2/-3 are not entered), `CE101_BANDS` (10 Hz to 1 kHz, 100 Hz to 10 kHz), `ce101_arrays` / `ce101_rows`
  (thinned by excess over the limit: the limit falls 20 dB/decade). The test reads each power lead with a current
  probe while the LISNs stay: below 10 kHz the LISN branch is `CE101_LEAD` (50 µH + 50 mΩ per line to a stiff source,
  the 0.25 µF port), from 15 Hz to just below 10 kHz (`lisn_loop_impedance`; the DC solves at 10 Hz keep CE102's
  network, whose series 8 µF keeps them out of it). The 50 mΩ (LISN coil, 2 m lead, source) sets the Q of the leads'
  series resonance with the bus capacitors (~2.4 kHz with 45 µF: Q ~14; ~200 without it).
  - Solves (`power.ce101_points`): 30 Hz-9.9 kHz, 4 per decade, for a bridge leg or a converter with harmonics below
    10 kHz, in the conducted run; `ConverterSetup.ce101_freqs` keeps them out of the CE102 transfer and its refinement
    budget (`_transfer(ce101=)`); `next_ce101` bisects gaps whose log-log interpolation bends (≥ 0.5 dB), up to 20.
  - Converters (`_ce101_stage`): harmonics in band times the transfer (`ce101_transfer` on the result), or a note
    (above 10 kHz the lead current comes from load steps, burst mode, a motor drive: not modelled).
  - Bridges (`ce101_bridge_rows`): each leg's high-side line spectrum below 10.5 kHz (lines every f_e) times its
    complex CE101 transfer, added, read by `pwm_lines.receiver`; per sweep row `ce101_margin_db`. The test bridge
    (48 V, 30 A, 3000 rpm, 7 pole pairs, 3 × 15 µF): 140 dBµA at 2.1 kHz = 6·f_e, 37 dB over curve #1, on the
    resonance's flank (tests/test_bridge.py, tests/test_ce101.py).
  - Results: `ce101`, `ce101_worst`, `ce101_limit` (stage and bridge), issue `conducted_emission` key `…:ce101`, the
    text report (`formats.ce101_lines`), HTML cards (`html._ce101_card`), the emission plots' CE101 panel, KPIs
    (`kpi._ce101_row`), the sweep plot (dotted).

## DC loads (`analysis/dcload.py`, `ui/dcloads_panel.py`)
The user, of a passive distribution board (a VDD pour from a 2-pin connector to four 4-pin connectors):
"how can I simulate a load across the highlighted trace?", with temperatures and capacity.
- Settings: `dc_loads: [{name, source, source_return, loads: [{pin, return, current}] (or "PIN/RET=I" text),
  max_drop, enabled, note}]` -> `DcLoadSpec` / `DcLoad` (`_dc_load_from_data`, `dc_load_problems`: the returns on all
  or none; `set_dc_loads`; `validate` checks the pins; Config.active; the names share the converters' namespace).
- Model: `power.dc_board_build` adds the rails' pin nets, the nets between the source and a load through series
  parts (`input_path`) and the parts' sides. `prepare_dc_loads`: one `ModelRun` at DC_F per rail with the
  injections per A of its total (`_area_shares` over each pad's own nodes: +1 at the source, -I_k/I at each load,
  +I_k/I at each load's return, -1 at the source's return); `DcLoadSetup` duck-types a setup for `solve_setups`
  (fine run only, no refinement). `finish_dc_load`: `dc_summary` (density, vias, parts, loss; r_mohm = loss / I²),
  each load's drop from `sol.V` at its pad's nodes weighted by their feed (supply: source - load; return: load
  return - source return; x the total), the result as its own view (sheet_current ... = the DC map, `view_of` =
  the path name, kind "dcload"), issues (current_density and via_overload as a stage's path, key
  `dcload:<name>:dc:` / `:via:`; dc_drop above max_drop).
- Wiring: `SimulationResult.dc_loads` / `dc_load_setups`; finish_issues combines them with the stages (combine_dc;
  its view is always kind "converter" so thermal doesn't count a rail twice); split items "dcload"; thermal
  `_stages` takes kind dcload, `part_losses` the rail's series parts, `capacity_inputs` scales a rail's loads
  together (nominal = its total); `result_views`, `_thermal_inputs`, `_ready`, auto-detect, KPI `_dc` (+ worst
  drop), metrics, plots/view3d treat it as a stage's DC view; report part "DC loads" (`_dc_load`), console
  `dc_load_text`, JSON `dc_loads`. Sharper map: not for a rail (raise the resolution).
- The form (`ui/dcloads_panel.py`): **Fill from net…** (the user pressed Add on an empty form: typing every
  connector's pins was the hard part) asks for the rail's net and its ground, then lists `dcload.rail_candidates`
  (every fitted part with a pad on the rail: its first rail pin, its own pin on the ground net, connector or not;
  connectors first) to pick the source (`guess_source`: the connector with the fewest pads) and the loads (the
  connectors preselected) and a current; the returns go in only when the source and every load have one. **Pin
  completion** (the user: "can you auto complete the source pin source return as well as the loads?"):
  `ui/pin_complete.py` (no wx, so tests run it; in ui/ so protocol.code_id is unchanged) makes `pin_table` (each
  connected REF.PAD with its net and part) when the board loads and ranks `offer` per field: source = connector
  pins on supply-looking nets first, ground last; return = the source part's own ground pin first; a load = pins
  on the source pin's net (not the source, not the loads listed); a load's return = its own part's pin on the
  return pin's net (else a ground net). Typed text matches a pin's start, or from two letters its net.
  `complete_load_line` splits PIN/RETURN=CURRENT at the caret (nothing in the current) and gives the suffix that
  follows a taken pin ("/" when the source has a return, else "="). `PinPopup` shows it: a wx.PopupWindow with a
  ListBox under the caret, keys through EVT_CHAR_HOOK (before Tab's navigation and the multi-line field's new
  line), made on first use under the field's top-level window (the tabs are built under a temporary top-level
  first), the field focused before positions are read (Windows draws a multi-line field's hint as its text while
  unfocused). 2 ms for kraft's 562 pins.
- Validation (tests/test_dcload.py): the drop along test_pours' 4 mm pour = I·ΔL/(σ·w·t) (4x: +0.1 %, also 16x;
  1x: -5.5 %, the 0.24 mm cells widen the pour's edges), loads superpose, nothing flows past the last load, the
  return adds a positive drop, Thermal + capacity √ law (2x current for 4x rise), report and console.

## Power integrity: PDN impedance (`analysis/pdn.py`, M1, branch `pdn`)
The plan's M1 (the user chose it with M0, M2, M3: "anything else that would be useful for an electronics engineer to
check their design is sound").
- Settings `pdn:` (config.PdnSpec, `set_pdn`, validated against the board): rail, ground, voltage (from the name:
  `rail_voltage`), ripple, i_step (target = V·ripple/i_step), source (`find_source`: a power_out pin on the rail, else
  a connector; `none`), vrm_r / vrm_l, parts (`supply_parts`: power_in pads on the rail plus a ground pad; without
  pin types U/IC references), check_to, decap_mm.
- Model (`prepare_pdn`): `BoardModel.build` over the board with the rail and its ground on every copper layer and
  plane-pair capacitance (the cavity budget `cavity_max_nodes` x resolution); the decaps are its lumped part
  branches (part_impedance); the source `with_branches` as vrm_r + jωvrm_l between its pads (`_source_branch`).
  `copper_pieces`: the DC copper (strips, vias, tracks; not parts or capacitance) in pieces; the rail's main piece is
  the one its source feeds, else the one with most supply pads; an IC whose pads land elsewhere is left out with a
  note before solving (kraft_flux_mini_2 at 0.86 mm: U203's 0.25 mm QFN pads and U305 read 1.7 MΩ at 1 MHz, stray
  capacitance; such a nearly floating port also slowed every solve: the first 40k-node run sat over an hour).
- Ports (`PortSet`): one injection set per IC (+1 A over its supply pads by area, -1 A over its ground pads),
  solved together per frequency (one factorisation; GMRES 35-60 iterations each on kraft's board model);
  `PortSolution` keeps Z per port and the branch currents only (not the field maps). PdnSetup: the run is an extra
  run (refined by `refine`: the worst port's log-log bend, up to MAX_SOLVES 40); `fine` the same model with nothing
  planned (as a power stage's conducted run). 10 kHz-1 GHz, 4 per decade.
- `finish_pdn`: per port |Z|, phase, what holds the rail (the part with the largest share of the port current, its
  branches added; "planes" under 5 %), peaks, `_inductance` (median Im(Z)/ω between the series minimum and the first
  anti-resonance), the worst margin below check_to; `_decaps_near`; issues pdn_impedance, decap_missing.
- Validation (tests/test_pdn.py, boardgen.pdn_board, planes 0.2 mm apart, 231 pF): bare planes 1/(ωC_pp) within
  0.1 %; one and four 1 µF decaps 1/(ω(nC + C_pp)) within 1 %, their minimum ESR/n plus copper; the source's 50 mΩ
  at low frequency; the target and its issue; a cut-off IC left out.
- Pins on a fine model (`_local_runs`, `_local_correction`, the user's choice "Fine model per IC"): per IC two
  regional models (its pads and the decaps within 9 mm, + 3 mm), at the fine pitch (dielectric height,
  analysis.max_nodes) and at the board-wide pitch on its lattice (the region snapped to it), solved at 30 / 100 /
  300 MHz; ΔL = Im(Z_fine - Z_coarse)/ω where both are inductive, median, added in series (jωΔL) to the IC's curve.
  Without a decap in the region none (a small plane capacitor: the pitches differ in capacitance). Test board:
  -43 % at 100 MHz on 1.26 mm cells, within 1 % with it (1.26 / 0.59 / 0.30 mm).
- Real board (kraft_flux_mini_2 +3V3, LDO U505 as source), corrected: 40k nodes (0.86 mm, 12 min here, 5 ICs: U203 and
  U305 cut off) and 80k (0.59 mm, 29 min, all 7) agree at 100 MHz within 1-20 % (U1102 0.571 / 0.596 Ω, margins
  within ~1.5 dB); ΔL 0.04-0.9 nH. (A first comparison read "halves from 0.86 to 0.59 mm": it compared the curves by
  solve index, not by frequency, after different refinements; wrong.)
- Not yet: warm starts across frequencies (measured: -20 % iterations only).

## Voltage-aware safety checks (`analysis/safety.py`, M2, branch `safety`)
The plan's M2. No new solve: what each net's voltage asks of the copper and the parts.
- `net_voltages`: {net: (V, why)}. Grounds (`is_ground`) and reference nets 0 V; `pdn.rail_voltage` from the name;
  PDN rails; per converter its hot-loop rail (v_in, a boost's v_out) and its switch node (`power.switch_inductor`)
  at that voltage, the inductor's far side at v_out (boost: v_in); bridges' bus and phases at v_bus; then the
  settings' `safety: voltages` (forced); last `_through_series_parts`: a union-find over parts with (nearly) no drop
  (short, inductor, ferrite, resistor <= SERIES_MAX_OHM 10 Ω; the stage inductors skipped: a switch node's peak is
  not its output's), each group taking its highest known voltage for its unknown nets. A model without `pins` on a
  FET (a pass FET given a short / resistor) joins its non-gate, non-Kelvin pads (`detect.GATE_FN`, `KELVIN_FN`):
  kraft's V_BATT through Q601. Unknown nets are taken as 0 V.
- **Voltage ranges** (2026-10-17, the user: "Do it" to giving bootstrap nets the switch node's voltage):
  `voltage_model` -> `Volts` (peak = net_voltages, lo, rides). Switch nodes (converters, bridge phases) lo 0;
  `_riding`: a FET's (Q/M/T) gate rides its single source pin's net (GATE_FN / GATE_FN_SOURCE, not Kelvin); a
  bootstrap net (a C from a switch node to a net that also reaches a U or D part: a snubber's middle does not) rides
  the node; then through resistors ≤ GATE_R_MAX (100 Ω), ferrites and shorts (TGUP / TGDN through the gate
  resistors). Riding nets' peak = their base's + the drive (safety.gate_drive_v, 10 V), lo = the base's. `dv(a, b)`:
  the drive when both ride one base, else max(hi_a - lo_b, hi_b - lo_a). Clearances and capacitors use dv (a
  bootstrap capacitor sees the drive). `_without_own_pads`: a part's pads on both nets on the layer are left out of
  the gap one side at a time (min of a-without-them vs b, a vs b-without-them). kraft (out/audit): the 27 unknown-net
  warnings were mostly gate / bootstrap nets; now 42 errors and 13 warnings: the bootstrap / gate nets beside their
  own switch nodes are gone, the swing shows SW1 / SW2 / D501-K 0.20 mm from +VDC, the gate and driver nets
  (61.8 V at the top of a cycle) 0.20 mm from GND (the board's 0.2 mm default clearance against IPC's 0.60 mm at
  52 V uncoated; coated: none), and C601 (10 nF, 50 V) from U601's GATE to ground now sees ~62 V (Q601's gate above
  +VDC): a cap_voltage error worth checking.
- `check_clearances` (Run checks, `run_checks` without a net subset): per copper layer the nets above CHECK_FROM_V
  (30 V); an STRtree over the layer's net geometries (`CopperMap.get`) queried with the hot net buffered by the
  spacing its voltage could need; per pair `required_mm(|ΔV|)` (IPC2221B rows: B1 inner, B2 outer, B4 outer coated;
  above 500 V per volt; `clearance_mm` a user table for outer layers) against the shapely distance, the issue at the
  midpoint of `nearest_points`. Both voltages known: error; the other unknown: warning (the hint names it). B3 and
  A5-A7 are not built in (no two sources agreed on them).
- `check_capacitors`: the voltage between a `cap` model's two nets against `cap_rating` (the Rated Voltage field, or Voltage: `Param.also`; else
  `_RATING` in the value), derated per `cap_kind` (CAP_DERATING, `cap_derating`); unrated caps at >= 10 V in one
  info issue. `check_esd`: connector references (J, P, X, CN, CON; not JP) whose pins (not ground, not unconnected:
  KiCad's `unconnected-(...)` or a single-pad net) have no `is_esd` part pad on the same net within `esd_mm`.
- `after_simulate` (in `finish_issues`, after `combine_dc`, so the combined DC paths are there; `split.merge_parts`
  and the server worker pass the board): `part_losses` (I_path² · the part's r_mohm of each DC path, combined, per
  stage, DC loads; bridges' RMS rms_a² · r_ohm; the largest per part) against `resistor_rating` (the Rated Power field, or Power;
  else RESISTOR_WATTS by package, 0612 shunts 1 W; R references or Resistor libraries only: Q601's resistor model
  is a FET; unrated ones dissipating >= 50 mW listed in one info issue); `vds_rating`: peak = the rail the node swings to (v_in; a boost's v_out) + the
  larger of the stage's overshoot_v and its ringing's, against the smallest Vds field of its switches.
- Run checks with no nets listed now runs (the board-wide checks); Simulate still needs something to solve.
- Validation (tests/test_safety.py, `boardgen.safety_board`): the table at its band edges and above 500 V; tracks of
  known gaps on outer / inner layers, coated; an unknown net's warning and its voltage given; ratings from value and
  field, derating; ESD found, missing, unconnected pin skipped, named connectors; voltages through a 0R and a pass FET
  (not its gate); I²R against the path current; Vds warning and error.
- kraft_flux_mini_2 (out/audit settings, uncoated): 16 errors (+VDC / V_BATT / SW1 / SW2 / the diode buck's node,
  51.8 V, 0.20-0.31 mm from GND, +12V, +5V, +3V3 and conv2's 5.4 V output; B2 asks 0.60 mm), 27 warnings (gate
  drive, bootstrap and controller pins of unknown voltage), 0 coated; ESD infos J101, J104, J106, J1101; 23
  cap_voltage warnings from the parts' Rated Voltage fields (100 V ceramics on +VDC at 52 %, 35 V ones on the 28 V
  output at 80 %, a 6.3 V one on +3V3 at 52 %), none over its rating. 5-10 s. Simulate conv1 + conv2 (286 s):
  vds_rating warnings, the nodes at 89.6 / 89.9 V (51.8 V + the ringing's 37.8 / 38.1 V; L·di/dt alone 7.5 / 3.8 V)
  against the EPC2306's Vds 100 V; the same overshoot the hot_loop check and the fix advisor's Vds target use. Losses:
  R309 / R310 (8 mΩ, 0612) 0.21 / 0.19 W, Q601 0.19 W, R328 / R329 0.05 W: nothing over 50 % of a rating.

## Control loop (`analysis/loop.py`, M5, branch `control-loop`; boosts at the user's "Add boost support too")
The plan's M5, at the user's "Go for it"; the controller's figures as InvenTree fields at the user's "Those
parameters could come straight from the schematic and are part of the symbol that comes from InvenTree".
- Fields (components/fields.py, parts "controller"): Vref, EA gm (quantity "S": mho / S with prefixes in `_special`),
  EA Ro, EA Gain, EA GBW, Vsense Max, ITH Range, Current Sense Gain, Slope Compensation ("V/s": a prefixed volt now
  parses, "50mV/us"), Ramp; `CONTROL_MODE` "Control Mode" (text). LTC7890 (from the datasheet as quoted by a search:
  VFB 0.8 V, gM 1.8 mmho at ITH 1.2 V; V_SENSE(MAX) per VPRG not obtained: the analog.com and Mouser PDFs refused
  automated downloads).
- `find_loop`: the controller by a pin (SW / PHASE / LX, normalised `_norm`: 'V_{FB1}' -> 'VFB1') on the switch node
  (or the integrated stage's IC), its channel = that pin's digits; `_pin` by kind (fb, comp, sense±) and channel.
  COMP net: C to ground -> Chf, R to ground -> r_shunt, R to a node with a C to ground -> Rc / Cc, R-C or C to FB ->
  zf (op-amp). FB net: parts to `output_nets` (the inductor's far side and nets joined through shunts ≤ 10 Ω,
  shorts, ferrites) on top, the rest below (a non-ground net: an AC ground, noted). Sense: the nets the SENSE pins
  reach directly or through ≤ 100 Ω filters; resistors < 1 Ω between both sides in parallel; else the inductor's DCR.
  Values: the part's model (resistor / cap), else its value text (`_value`; compensation parts are often 'open').
- `analyse_loop`: `Rat` (numpy poly arithmetic), Y_o from `output_admittance` (C at bias, ESR), the plant (current:
  Z'·F_h/R_i; voltage: V_in/V_ramp/(1 + (sL+R_L)Y_o)), the compensator (gm·Z_comp·H with H = Y_top/(Y_top + Y_bot);
  or an ideal op-amp Y_top/Y_f); a 2000-point sweep to 5·f_sw for f_c (|T| = 1, log-interpolated), PM (180° + the
  unwrapped phase there), f180 / GM above f_c; poles / zeros by `roots_scaled` (in s/ω_sw), coinciding pairs dropped
  (`transfer.cancel_pairs`, 0.5 %: each output capacitor adds an ESR pole-zero pair). COT / internal / boost / a
  missing field: `error` with what to give.
- power.`_stage_loop` (finish_converter, not a bridge leg): ConverterResult.loop (summary), the response among
  res.responses (so the Bode view and the report show it), issues `loop_phase_margin`, `loop_crossover`; kpi
  "phase margin" tile; formats "control loop (...)" line. Settings: ConverterSpec.loop (overrides, written back).
  A controller without any loop field (no Control Mode, no figure, no loop: override) gets no stage note, only
  `loop.error` in the summary: an unset board stays quiet.
- Validation (tests/test_loop.py, `discrete_buck_board(loop=True)`: U8 with SW1 / ITH1 / V_FB1 / SENSE1±, 2.2k + 22n
  + 22p, 100k / 25k, a 10 mΩ RS through 10R filters): parts found; T equals Ridley's model written out (1e-9) with
  the sampling pair at f_sw/2, Q_p; f_c within 10 % of gm·Rc·H/(2π·R_i·C); voltage mode's LC pair and Q; the margins
  at |T| = 1; COT and missing fields; Simulate end to end and a 9° margin issue (Cc 220 pF).
- **Boost** (`spec.topology == "boost"`): `output_nets` starts from the rail (a boost's output, behind its
  rectifier; the inductor's far side is its input). D' = V_in/V_out, I_L = I_out/D', a = I_L/V_out, S_n =
  R_sense·V_in/L, S_f = R_sense·(V_out − V_in)/L. Peak current mode: the averaged peak-current law (Erickson &
  Maksimovic ch. 12: i_L = i_c − M_a·T_s·d − D²T_s/2·m̂1 − D'²T_s/2·m̂2) with the duty cycle taken quasi-statically
  from the inductor (buck d = v/V_in, boost d = D'·v/V_out; the same steps give Ridley's buck term T_s·(m_c·D' − ½)/L
  exactly) gives i_L = v_c/R_i − G·v with G = T_s·D'²·(m_c − ½)/L; the output node Y_o·v = D'·i_L − I_L·d with the
  inductor's d = (D'·v + Z_L·i_L)/V_out: G_vc = (D' − a·Z_L)/(Y_o + a·D' + G·(D' − a·Z_L))/R_i·F_h (F_h as the buck's).
  Its dominant pole (2/R + G·D')/(C − G·a·L): Ridley's 2/(RC) plus the current loop's D'³ term. Voltage mode
  (Erickson ch. 7): G_vd = V_out·(D' − a·Z_L)/(Z_L·Y_o + D'²)/V_ramp. The RHP zero D'²·R/L in figures["f_rhp"];
  power `loop_crossover` also above LOOP_RHP_MAX (0.2) of it. A CCM check for both (I_L below half the ripple:
  a note). Tests: `boardgen.boost_board(loop=True)` (U8, RS 10 mΩ at the input in series with L1, 10k + 10n + 100p,
  290k / 10k): the parts on VOUT, T against the model written out (1e-9), the RHP zero and the pole exact, Q_p,
  voltage mode's pair at D'/(2π·sqrt(LC)) with Q = D'·R·sqrt(C/L); Simulate: Rc 100k gives the RHP issue.
- kraft_flux_mini_2 (U303's fields set on 2026-10-17 with `tools/dev/label_project.py --field`, at the user's "add
  those yourself"; backup `out/kraft_backup_2026-10-17/`): Control Mode peak current, Vref 0.8V, EA gm 1.8mmho,
  Vsense Max 50mV (ILIM floating: 45/50/55 mV), ITH Range 1V (first an estimate; then read off the datasheet's
  Figure 13 "Maximum Current Sense Threshold vs V_ITHx": every ILIM line from 0 mV at 0.4 V to its maximum at 1.4 V;
  analog.com and Mouser refuse automated downloads and the InvenTree copy needs a login, but
  web.archive.org/web/2024id_/<the analog.com URL> serves the PDF, and r.jina.ai/<URL> its text); U502 COT,
  U503 internal. `tools/dev/loop_check.py` (out/m5 settings from real_settings.py): conv1 f_c 11.2 kHz, PM 77°,
  GM 22.6 dB; conv2 29 kHz, 91°, 20 dB (slope compensation assumed half the down-slope). The crossover scales
  with k = Vsense Max / ITH Range.

## Frequency responses, Bode and pole-zero (`analysis/transfer.py`, M4, branch `bode`)
The user (2026-10-17): "can we add bode and phase plots as well as pole zero plots?"; all four kinds chosen (signal
paths, power-stage filters, PDN, control loop: M5), as Heatmap views and in the report.
- `Response` (name, owner, quantity: voltage gain / current ratio / impedance / loop gain, f, complex h, poles,
  zeros in rad/s, exact or fitted with fit_error_db, notes; summary() decimates to 200 points). `pz_rows`: f0 and Q
  (a conjugate pair once), `fmt_hz` / `fmt_root` (3 digits; RHP).
- `vector_fit`: the real basis (a pair (a, a*) as 1/(s−a)+1/(s−a*) and j/(s−a)−j/(s−a*)), weights 1/|H| (relative),
  relaxed pole relocation (σ = Σc̃φ + d̃ with Re Σσ = K), new poles = eig(A − b·c̃ᵀ/d̃) of the matching real
  realisation (a pair [[a', a''], [−a'', a']], b [2, 0]), unstable ones mirrored, 12 iterations per order, orders
  2, 4 … 24 until 1 % RMS; residues (and d, e) by least squares with columns scaled to unit norm; poles contributing
  < 1e-4 of max|H| pruned when the fit stays as good; zeros = finite generalised eigenvalues of
  [[A, b], [c, d]] − s·[[I, 0], [0, −e]]. `fitted_response` leaves poles / zeros beyond 10x the band out of the lists
  (noted). `exact_response` for rational models (np.roots).
- Owners: signal paths (`simulate.path_responses`: RouteSetup.response_fn, set where the path model is built, plain
  `path_currents(..., ones).v_end` or `amplified_path` with the op-amps' linear models; amp_gains per stage; 300
  log points from a hundredth of the first harmonic to the band's top); power stages (`power.stage_responses` in
  finish_converter: `ringing_rational` (exact; snubbed_response in s) from setup.ringing_model, set in
  apply_ringing; the input transfer from cond's solves (fine's too only when it is the same build: a split input
  path's hot-loop model has no LISN branch; that bug failed three test_power tests before the check), order ≤ 10);
  PDN rails (finish_pdn: each port's complex Z, use_e). RouteResult / ConverterResult / PdnResult `.responses`;
  summaries in JSON.
- Display: `BodeView` (kind "bode", `bode_views` after every other view in `main_frame.result_views`);
  results_panels CURVE_KINDS ("pdn", "bode") replace the PDN-only checks (no layers, arrows, board drawing),
  heat() → `plots.draw_bode` (|H| dB or |Z| Ω log; unwrapped phase; × / ○ at f0), canvas2 → `plots.draw_pz`
  (symlog axes, labels hidden inside the linear band, the four lowest poles labelled); kpi `_bode` (dominant pole);
  html `_responses` (both PNGs and a table) in _net / _stage / _pdn; formats `response_lines`.
- Validation (tests/test_transfer.py): RC low-pass, series RLC impedance (SRF and Q, with e), 4th order with two
  pole pairs and a real zero, a delay (coarse, noted), the summary; the RC path (2.74 MHz = 1/(2π·58 Ω·1 nF) within
  1 %), the op-amp stage (1 MHz = GBW/gain), the ringing rational = snubbed_response, a converter's two responses,
  the one-cap PDN board's poles at its peaks; a GUI test of the view.
- Left over from the plan, done on `control-loop` at the user's "fix it": the fix advisor's proposed π-filter as
  responses (`fix_advisor.filter_responses`, called by `_filter`: its attenuation `filter_transfer` (complex;
  `filter_ratio` is its magnitude) fitted from a tenth of its corner to the conducted band's top, the filter's
  output impedance as the converter sees it (fitted) and the converter's input impedance −V²/P (exact, 180°): the
  two curves of Middlebrook's criterion); `transfer.unit_groups` splits an owner's responses into ratios (dB) and
  impedances (Ω, "<owner> impedances") for bode_views and html._responses (draw_bode has one magnitude axis);
  `plots.draw_pdn` has the ports' phase below |Z| (the phase was already in the ports). Tests:
  test_fix_advisor (the curves equal filter_transfer, z_out's peak middlebrook's, −V²/P at 180°, the split views),
  test_pdn (the bare rail's −90°, the one-cap rail's sign change).

## Signal integrity (`solver/xsection.py`, `analysis/si.py`, `ui/si_panel.py`, M3, branch `signal-integrity`)
The plan's M3, at the user's "Merge and continue with M3" (2026-10-17).
- **Cross-section solver** (`solver/xsection.py`): Laplace ∇·(εr∇φ) = 0 by finite volumes on a rectilinear grid,
  potentials at nodes, εr per cell (each cell adds εr·(dy/2)/dx and εr·(dx/2)/dy to its four edges), Neumann box.
  Conductors are Rects (id 0 the reference, 1..n signals; y0 == y1 a zero-thickness strip), dielectrics Slabs.
  Charges by the discrete Gauss law (Σ (Aφ) over a conductor's nodes), the Maxwell C with the dielectrics and C0 in
  air, L = μ0ε0·C0⁻¹. Z0 = 1/(c·sqrt(C·C0)); Zdiff from L_d = L11 + L22 - 2L12 and C_d = 1/(P11 + P22 - 2P12)
  (P = C⁻¹); crosstalk K_b = (L_m/L + C_m/C)/4, K_f = (L_m/L - C_m/C)/2.
- **Grid**: breakpoints at every rectangle and slab edge; the finest cell hmin = `signal_scale` / EDGE_DIV (10): the
  signal conductors' widths, thicknesses and gaps to other copper (not the 10 µm mask, which made 0.5 µm cells and
  millions of nodes on kraft); the step at a breakpoint grows with its distance d from the signal (hmin + 0.2·d), so
  far copper edges get coarse cells; growth 1.15, the coarsest a fortieth of the box; breakpoints closer than hmin/4
  merged. **Richardson**: the charge error at strip edges falls in proportion to h, so C = 2·C(h/2) - C(h) (-2.1 %
  and -1.1 % become -0.15 % against Cohn). splu with MMD_AT_PLUS_A in symmetric mode (half COLAMD's fill). A kraft
  cut: ~120×120 nodes, 0.1-0.2 s for all four factorisations.
- **Validation** (tests/test_xsection.py): Cohn's exact zero-thickness stripline (w/b 0.25-1) and edge-coupled
  stripline (Z_even, Z_odd; K_b from them; K_f = 0 in a uniform dielectric) within 0.5 % (measured 0.2 %);
  Hammerstad & Jensen 1980 microstrip (u 0.5-2) within 0.5 %; coupled microstrip K_b against 1/(1 + (D/h)²) within
  a factor 2 and K_f > 0 (inductive); a wide strip near the parallel plate.
- **Board cuts** (`SectionCache`): a line perpendicular to the track through the cut point, ±20 field heights (1-15
  mm), intersected with every layer's net geometries (`CopperMap.get`, an STRtree per layer); intervals per
  (layer, net). The signal is the piece at the centre (on a pad, a via or a corner much wider than the track: no
  cut); a pair's N its nearest piece on the layer; **every other piece of the nets is a 0 V neighbour** (the net's
  own track further along, under it on B.Cu, read 26 Ω on kraft's SPI_MISO when driven as part of the line). Points
  on the net's pads and vias are skipped. Vertical: foils at their z, dielectric slabs from the substrate surfaces to
  the inner foils' centres (prepreg fills the gaps between traces), the masks (`Stackup.mask_top/bottom`, read from
  F.Mask / B.Mask; KiCad's 0.01 mm / εr 3.3 when absent) on the outer surfaces, air 20 field heights. Cache key:
  the pieces with offsets rounded to 1 µm near the centre, 5 % of the distance farther out (cuts along a straight
  run share one solve). `prefetch` solves the distinct cuts in `solver_threads` threads (SuperLU releases the GIL:
  kraft 17.8 s -> 9.6 s on 2 threads).
- **Checks** (`run_si_checks`, from `run_checks`): impedance profiles of the listed nets and the targets' nets
  (`impedance_targets`: names, fnmatch patterns, KiCad net classes from the .kicad_pro's netclass_patterns /
  netclass_assignments); pairs (`check_pairs`: Zdiff where N is within 4 widths, else 2·Z0 and uncoupled; the routed
  lengths per layer plus the via barrels between used layers, each layer's delay from its cuts); crosstalk
  (`check_crosstalk`: per aggressor segment, other nets' parallel segments (within 15°) within 6 field heights
  (0.5-3 mm), overlap length, gap at its middle; grouped per victim and layer, one single-grid cut at the closest
  stretch; supply and ground nets are no victims); via stubs (`check_stubs`: the barrel beyond the outermost used
  layers, f = c/(4·l·sqrt(εr between)), against stub_factor × 0.35/t_r). `find_pairs` / `detect_pairs` /
  `pair_defaults`: names; a bare P/N or H/L only after a digit or a clock / data stem (VCAP / VCAN are no pair).
- **Settings** `si:` (SiSettings: impedance [ImpedanceTarget], pairs [DiffPairSpec], crosstalk_max, stub_factor,
  step_mm); writers set_si_pairs / set_si_targets / set_si_limits.
- **KiCad 10 tuning profiles** (`kicad_profiles`, `profile_nets`; the format learned from the user's "Test" profile
  on kraft_flux_mini_2, 2026-10-17): the .kicad_pro's tuning_profiles > tuning_profiles_impedance_geometric:
  profile_name, type (0 single, 1 differential: the user switched Test to Differential to show it), target_impedance (Ω, 0 none), enable_time_domain_tuning,
  layer_entries {signal_layer, top/bottom_reference_layer ("UNDEFINED" none), width / diff_pair_gap in nm, delay in
  attoseconds per mm (100 ps/cm = 10 000 000)}, via_overrides (as). Net classes name theirs (classes >
  tuning_profile). A single profile's target is its class's nets' Z0 target (10 %, on its layers); a differential
  one's is the Zdiff of a pair whose nets are in its class (when the pair has none); a profile on KiCad's Default
  class applies to the listed nets only. Nets of a profiled class are cut like listed nets. **tuning_delay**
  (`check_profile_delays`): with time-domain tuning on, each layer's unit delay against the cross-sections'
  length-weighted sqrt(εeff)/c (a pair: its differential mode), over 10 % an issue: KiCad turns lengths into delays
  with the typed figure. kraft copy with Test on a class of /CPU/SPI_*: 100 ps/cm against 59.9-60.9 ps/cm, a tuned
  delay comes out at 60 %.
- **Output**: CheckResult.si (SiResult: NetProfile, PairResult, crosstalk and stub rows); `formats.si_text`, JSON
  "si", the HTML part "Signal integrity" (tables, `plots.draw_impedance`: a range bar per net / pair over its target
  band); the plugin's Signal integrity tab (`_done(si=)` from Run checks, a local Simulate's res.checks.si, and a
  server's result).
- kraft_flux_mini_2 (detected nets + CAN pair): see README "Signal integrity". gui_probe: no stall on the tab or
  after Run checks; the one load stall (211 ms) is the Power integrity tab's `_rail_changed`, older.

## Through-hole pin values (`analysis/pins.py`, 2026-10-16)
The user, of a big connector's map: "do we assume a solid connection between the connector and the via; e.g. filled
with solder? ... introduce a GT field parameter ... to define the solder joint resistance", then "implement all of
them but should allow to use default values for each when one or more are not entered". Asked: the built-in default
stays the old model (copper filling the hole), and a contact resistance is never guessed (a note instead).
- Values `pin`, `pin_d`, `pin_r`, `fill`, `joint`, `contact`; `params_for` takes each from the GT field (words beside
  the labels: `tags.parse` skips `is_pin_word`s and reports bad ones), the part's `ComponentEntry.tht` (settings
  `components.J1.tht`, the Parts tab's Pins column, `set_component_pins`; kept when the model is rewritten:
  set_component, sync_fields, merge_board), the board's `Config.tht` (top-level `tht:`, Pin defaults…,
  `set_pin_defaults`), else built in; `PinParams.source` says where each came from.
- The column (`simulate.add_pin` with `pin_column`): per piece between two layers, the soldered length at
  `column_conductance`'s soldered σ·A (built in: σ_Cu·π·r², as before; a material: rod + solder gap (SOLDER_RHO) +
  plating 2·π·r·t; pin_r replaces the rod) in series with the unsoldered length at the plating's (the pin is not
  joined to the hole there), + jωL as via_impedance. The soldered height (`solder_span`) runs from the side opposite
  the part. A plated hole's inner layers are always joined to its plating (made with the board), so a partial fill
  never cuts a layer off. `pins.plain` parts keep the old code path exactly.
- Where a partly soldered pin is fed: `feed_layer` (the layer nearest the part inside the soldered height) in
  `StackBuild.pin_feed`, taken by `power._pad_node` instead of the part's side; `dc_board_build` adds those layers
  (`feed_layers`) even when they hold nothing else of the net (the current climbs the hole).
- `contact` and `joint` are in series at the fed pins: the model feeds currents, so on one pin they change no copper
  current: `dcload.finish_dc_load` adds I·R per pin (`pin_currents`, `series_of`) to the drops (`pins_mv`, in the
  report's Loads table, the console and the KPI); `thermal.part_losses` adds I²·R per fed pin at its part (DC loads'
  pins; converters' input connectors at the input current, output connectors at I_out; a connector shared by
  several stages sums them), so it also counts in `input_estimate`'s losses. `series_note`: a connector pin with
  NOTE_A (3 A) or more and no contact value. Not yet: motor bridges' connectors, a power stage's DC drop.
- Validation (tests/test_pin_params.py): a pin soldered 10 % vs 50 % changes the drop by I·0.4·T·(1/G_plating -
  1/G_filled) (2 %); contact and joint add exactly I·R per pin to each side's drop and I²·R per pin to the part's
  loss, the copper loss unchanged; the fallback per value; the settings round-trip.

## Fix advisor for power stages (`analysis/fix_advisor.py`)
Added in session 7. The user: "the automated suggest fix option is nice but it doesn't really provide incredible
results ... help with actually properly fixing these issues". The layout search below can't remove 28 dB of
conducted emission or a ringing set by C_oss; design changes can.

- **The re-evaluation core.** `evaluate(setup, Change)` recomputes a stage's figures for a change on its own solves.
  It returns an `Outcome`: radiated and conducted worst margin, f_ring, Q, overshoot, CM budget, extra loss.
  - What stays: the loop L and R (`res.loop_nh`, `loop_mohm`), the moment per ampere (`band_moments`), the input
    transfer (`_transfer`).
  - The pipeline is finish_converter's, shared with it:
    - `power.stage_spectrum`: pulse, ringing or snubbed response, `edge_charge_factor`, overshoot. `apply_ringing`
      calls it too.
    - `power.pulse_harmonics`: another edge time on the stage's own grid.
    - then `emission_arrays`, `conducted_arrays` (with a spread) and `cm_estimate`.
  - `Change` fields: `rise_time`, `snubber (C, R)`, `loop_scale` (L and moment × k, the same shape), `filter`,
    `spread`.
  - Losses: edges ½·V·I·Δt·f_sw per hard edge (`thermal._synchronous`), a snubber C·V²·f_sw, a filter inductor
    I_in²·DCR.
  - `evaluate(setup)` with no change reproduces the stage's reported worst rows, Q and overshoot exactly (test). A
    slower edge or a proposed snubber matches a full Simulate of the stage with that change in its settings.
- **The options.** `stage_options(setup, board)` is called in `finish_converter`, giving `ConverterResult.fixes`
  (dicts). Its first row is the stage as it is.
  - `targets`: margin ≥ `rules.emi_margin_db`; overshoot ≤ `overshoot_warning`·V_in, or ≤ 0.8·Vds − V_in where the
    switches carry a `Vds` field (`components/fields.PARAMS`).
  - The kinds:
    - **snubber:** a grid of C ∈ {1–4}×C_oss (E12) and R ∈ {0.5–2}×Z0 (E24). It offers the best damping and the
      lowest loss that meets the overshoot target.
    - **edge:** t_r × 1.5, 2, 3.
    - **loop:** a bisection for the largest L that meets the radiated and overshoot targets, limited to what a layout
      reaches (`LOOP_MIN_SCALE` 0.4×, `LOOP_MIN_NH` 0.4 nH; below that "not enough", which counts as clearing
      nothing).
    - **combined:** when no single change clears ringing and radiated, the cheapest mix of snubber × edge ×
      loop {1, 0.75, 0.5}.
    - **filter:** `input_filter_advice` made concrete. L is E6 up; C is nominal E6 up at `X7R_AT_BIAS` 0.5; the
      damping R_d = √(L/C), C_d = 4C is placed on the converter or connector side, whichever passes `middlebrook`
      (Z_out = Z_in,caps ∥ (Z_L + Z_C ∥ Z_LISN) from f_c/10 to f_sw/3 against V_in²/P_in, 6 dB apart). Its
      attenuation comes from `filter_ratio`, a Norton current divider between the stage's input capacitors (their
      models) and the LISN loop, tested against a nodal solve.
    - **spread:** the best of `SPREADS` through the receiver simulation.
    - **cm:** the choke for 1, 10 and 50 pF strays with 4.7 nF Y capacitors.
  - Ranked by the checks cleared, then the loss.
- **Where it shows:**
  - the Fixes tab's second list (`FixesPanel.show_design`, after every Simulate); **Check in Simulate**
    (`MainFrame.on_check_design`) simulates the stage again with a snubber (`snubber_c` / `snubber_r` on the spec),
    edge or spread applied in memory;
  - the HTML report's "How to fix it" table (`html._fixes`), the text report's `fix_lines`, and JSON (`fixes` on the
    converter);
  - a converter's `snubber` in the settings: `[C, R]` parts (detected like a bridge leg's, `detect._snubber`) or
    `{C, R}` values.
- **Tools:** `tools/dev/fix_check.py BOARD SETTINGS [--only X]` or `--test-buck` prints them.
- **Not yet:** bridge legs and signal nets.

## Fix suggestions (`analysis/suggest.py`, `analysis/whatif.py`, `config/modifications.py`)
- What-if edits (`Modification`: via, cap, fill) live in the settings file under `modifications:`.
  `apply_modifications` returns copies of board and config with real items added: a `Via`; a two-pad
  footprint `WHATIFn` (pads 1 mm apart, through vias to both nets) with its own cap model; a filled `Zone`.
  Every check and the solver treat them like any other copper. Plain CI runs never apply them.
- `PlacementChecker`: STRtree of other nets' tracks, pads and vias per layer (0.2 mm clearance), the board
  outline minus 0.5 mm, and `on_plane` (the net's own plane copper at the spot on the layers it must join).
- Candidates per issue (rings of 12 angles around the signal via or crossing, spread out, nearest first):
  stitching vias at 0.8–2.2 mm, capacitors at 1.5–4 mm, fills for same-net slots (crossing piece buffered by
  max(2h, 0.5 mm), minus other nets' copper), advice text for issues without a geometric fix.
- `_Evaluator`: predicts each candidate's ΔL at f0 on the route model and its Δ|m| at the baseline's worst
  harmonic, through the same combination the simulation uses (`m_route(f0) + m_board(fw) − m_board(f0)`).
  `PlaneStack.with_branches` adds the via/cap as branches on both models (Woodbury update of the shared
  factorisations), and GMRES starts from the baseline solution. A fill needs a rebuilt route model and keeps
  the baseline's board-wide part. On the layer-change test board, the prediction at the worst harmonic
  (−21.6 dB) matched the verified gain (+19.0 dB) within 3 dB.
- Selection:
  - The best candidate per issue is kept if it gains ≥ 0.3 dB or 1 % L. Tier 1 issues keep their best
    candidate even without a gain.
  - A candidate predicted to raise the moment by more than 1 dB (`MAX_HARM_DB`) is never selected, Tier 1
    included. A stitching via can shift a plane resonance onto the worst harmonic: on the user's board, two
    SW_CLK stitching vias were predicted at +0.9 and +9.7 dB, and verification showed SW_CLK's margin
    dropping by 10 dB.
  - At most `max_fixes` per net are kept.
  - Each selected via or capacitor is then checked at **every harmonic near the limit of every route**
    (`_WorstCheck`): a fix can move a plane resonance onto a harmonic that was not the worst before, for its
    own route or another one (on the user's board in session 2, the SPI_MOSI via moved one onto SPI_SCK's
    660 MHz). The board-wide model of each route is re-solved with the fix (with_branches, warm start) at every
    solved harmonic whose baseline estimate is within `REFINE_MARGIN_DB` (20 dB) of the warning level, and the
    route model at f0; the other harmonics keep their baseline. Routes with no harmonic that close are skipped.
    First, each route's baseline is solved at the log-midpoints of the gaps (wider than `GAP_RATIO` 1.15) next
    to its near-limit harmonics (`gap_midpoints`); these solves join the check's before and after curves (not
    the reported baseline). Reason: the route's own refinement stops at `max_solves` and can leave a resonance
    in a gap. On the user's board SPI_MOSI's reported worst was +14.9 dB @ 820 MHz, but solving 700/740/780 MHz
    gave +12.8 dB @ 780; the three selected fixes predicted there gave +10.9 dB @ 740 MHz, and the verification
    (rebuilt models) +11.0 dB @ 740 MHz: the low-rank prediction is accurate where it is evaluated.
    The moments are recombined as in `band_moments` and give the predicted worst margin over the spectrum
    (`Suggestion.worst`). All solves of all checks run as one frequency-sorted parallel batch, and the routes
    on one board-wide model share one derived stack per fix (and its cached Woodbury operators). On the
    layer-change test board: +17.1 dB → +27.8 dB predicted, +27.1 dB verified.
  - `harmonic_verdict` drops a fix that lowers its own route's worst margin by more than 1 dB, or another
    route's by more than 1 dB when that loss exceeds the fix's own gain or ends below the warning level.
    Smaller trade-offs are kept with a note ("also lowers …"). The session-2 case (MOSI +3.6 → +11.4 dB,
    SCK +10.8 → +9.2 dB) is kept. Not done: trying the next candidate of an issue whose fix was dropped.
  - `simulate` runs on a copy with all selected fixes, for **what they can change** (`verify_scope`): every
    route and power stage with a selected fix, every route whose worst margin is within `REFINE_MARGIN_DB` of the
    warning level (a fix anywhere can move a plane resonance onto its harmonics), and every power stage whose
    model region contains a fix. The rest keeps its baseline results (listed as `SuggestResult.kept`, shown under
    the comparison) and gets only its rule checks re-run on the modified board. Before, the verification
    simulated every route and power stage, as long as a whole Simulate, with no progress shown (the bar sat at
    95 %). `verify_all=True` / `groundtruth suggest --verify-all` keeps the old full run. `compare_results` and
    `issue_changes` give before/after per route and the issues fixed/new (by fingerprint).
  - **Progress** (`analysis/progress.py` `Phases`): each phase (baseline, candidates, evaluations, the power
    stages' greedy rounds, gap midpoints, the checks near the limit, the verification, the aligned comparison)
    gets the part of what is left of the bar that its estimated solver work has against the estimate of
    everything after it. The unit is a solve weighted by its model's unknowns (`simulate.solve_cost`); later
    phases re-estimate with what is known by then, so only the unused part of the bar is re-divided, and reports
    never go back. Every message counts its steps ("evaluating candidates: SPI_MOSI, add a GND stitching via
    (3 of 12)"). Simulate uses the same plan: model building (k of n), solves weighted by model size ("solve 7
    of at most 24", refinement may stop early), and the finish step (the ideal reference is a solve) with its
    own share instead of 99 %.
  - `compare_results` (verification, `groundtruth compare`, the plugin's compare) compares the worst margins over
    the same harmonics (`aligned_worst`): each run refines its own spectrum, so the two worst cases can sit at
    harmonics the other run never solved. Both runs are solved at both worst harmonics (≤ 2 extra board-wide
    solves per run and route, kept out of the stored results; `band_moments(setup, extra)`), then each worst
    margin is taken over its curve. Each run's own value stays in `worst_margin_sampled_db`, and the text
    shows it when alignment changed it. Test: the same board with 4 and 10 solves per route reads +21.0 vs
    +17.1 dB on its own and +17.06 vs +17.06 dB aligned.

## Net auto-detection (`analysis/detect.py`)
- Candidate nets: not power (name regex, power_in/out pins, reference nets), not unconnected, grouped
  through signal-passing series parts (never into power nets).
- Class: regex on the net leaf name, then on schematic pin names (`pinfunction`, trailing `_<pad>` stripped).
  The table (`CLASSES`) has default f/tr and a score: 3 = clearly fast, 2 = likely, 1 = moderate
  (UART, CAN, PWM), 0 = slow (I2C). `groundtruth detect` defaults to `--min-score 2`.
- Endpoints: pads of parts with an `open` model (ICs, connectors, crystals), not TP/MH/fiducials/DNP. For
  switching nodes, inductor/ferrite pads on the net itself. Needs ≥ 2 different parts.
- Driver: highest `DRIVER_RANK` (output 4 > open collector 3 > bidirectional 2 > untyped 1 > passive 0.5 >
  input 0). Ties go to the part with the most pads (confidence low).
- A net matched only by a pin name that has zone copper is skipped (a poured supply).
- `suggest_net(board, config, net)`: the same per-net logic (`_suggest`) for a net the user picked (KiCad
  selection), which never filters a signal out: no class gives the generic class `GENERIC` (10 MHz / 2 ns, to
  set), fewer than two logic parts fall back to all pads, a poured net is flagged; such doubts go into the
  reasons and make the confidence low. Refused with the reason: nets not on the board, reference nets, supply
  nets (`is_power_net`), and switch nodes of the configured power stages. A supply-looking net the user picks is
  added after a question (`suggest_net(as_signal=True)`, `MainFrame.add_nets`): the user's /CPU/ADC_I_RAIL_1, an
  ADC input measuring rail 1's current, was refused for the RAIL in its name. `is_power_net` now takes a name
  with a measuring word (`MEASURE_NAME`: ADC, SENSE, SNS, MEAS, MON, IMON, VMON, ISNS, FB, FEEDBACK) as a signal
  unless a power pin is on the net (detect_dump: the same detection on kraft_flux_mini_2 and ness_2_wifi).

## Motor bridges (`analysis/motor.py`, `analysis/bridge.py`, `emi/pwm_lines.py`, `ui/bridges_panel.py`)
A three-phase BLDC drive (a starter/generator, an ESC): three half-bridge legs on
one bus, the motor winding as the inductor. Milestone 1 (branch `bridges`, merged): detection, the six-step drive,
the legs' hot loops, the bus ripple shared by the capacitors and the conducted emission of all legs together.
- **Settings** (`config/settings.py`): `BridgeSpec` (operating point, switch data, `MotorSpec`, `LegSpec`s) in a
  `bridges:` section. `Config.stages()` = converters + every bridge's legs, each leg an internal
  `ConverterSpec(topology="custom", bridge=, phase=, snubber=, phase_output=)` never written back; every place
  that walked `config.converters` for power nets (highlight, sharper, `BoardModel.converter_nets`, `power_nets`,
  `suggest_net`) walks `stages()`. Missing operating or motor data is a `simulation_blockers` entry, checked only
  at Simulate, so a detected bridge stays editable. `set_bridges` parses through a probe dict before it changes
  the doc.
- **Detection** (`detect_bridges`): a leg is a node with a transistor to a non-ground rail and one to ground and
  no inductor on the node or behind series parts (that is a converter); `fet_terms` finds gate/drain/source by
  pin name (EPC's GN/DN/SN too). Legs are grouped by (rail, ground), and only above three legs split by driver
  or output connector. The input is found through series parts with current-sense ICs (IP+/IP−) and pass FETs
  closed (models with `pins`: a model may join only two of a part's pins, `model_pads`/`model_nets`), or as two
  wire tabs (`_split_input`). Drivers: the IC with an SW/SHx pin on the node; three-phase drivers (DRV8300) are
  matched per phase by the pin's phase letter, and their HIN/LIN nets are listed (information only: the PWM lives
  in firmware, never read). Where the guesses fail, `GT` labels on the symbols (`hb_hs`, `hbA_ls`, `hb_out`,
  `hb_in`) make it explicit: see "Detection labels".
- **Drive model** (`motor.py`, numpy only): star equivalent per phase for both connections, R = R_LL/2 + R_on,
  L = L_LL/2; a delta only changes the EMF shape the terminals see (winding EMF 0.75·ke for a trapezoid, whose
  terminal peak is 4/3 of the winding's) plus a circulating current from the triplen EMF (reported). Each PWM
  period is a few intervals with closed-form RL solutions (`_Lin`); the floating phase clamps at −V_sd / V+V_sd
  through the off switches (the outgoing phase's current returns into the bus). N = 6·n_s PWM periods per
  electrical period (≤ 24 000; the speed is rounded and noted). Steady state: a Newton fixed point over 120° (a
  cyclic phase shift), the duty by an Illinois search on the mean sourcing current; unreachable → issue
  `bridge_operating_point`. EMF shape `MotorSpec.emf` (trapezoidal BLDC | sinusoidal PMSM). Tests M1–M6 (RL steady
  state, dead-time means, energy balance, bus AC = I·√(D(1−D)), star = delta for a sinusoidal EMF, the fixed point
  against a brute-force run).
  - **Event loop guards** (since a starter/generator's generating point hung Simulate): a current below `I_EPS` (1 nA) is
    zero (the events' rounding residue); an open phase whose clamp start the drive takes straight back (its seed
    current dies within 1e-9 T) stays open for the rest of that interval (`blocked`), instead of starting and
    dying forever; more than `MAX_EVENTS` (10 000) events in one interval raise `DriveError`, never a hang.
    Test at the user's operating point with the energy balance.
- **Spectrum** (`pwm_lines.py`): the switch currents are piecewise linear, so their Fourier series is exact from
  the slope changes; breakpoints at fixed in-period positions are summed by an FFT over the period index. Every
  sideband f_e′ apart is kept (the motor's electrical frequency modulates the PWM lines). The receiver
  (`receiver`) sums the lines through a Gaussian RBW at each tuning and takes the envelope by an inverse FFT:
  peak or average detector. Tests S/R in `tests/test_pwm_lines.py` (against `trapezoid_harmonics`, a brute-force
  DFT, Parseval, one and two lines in the RBW).
- **Board solve** (`bridge.py`): the drive first; each leg becomes a stage with `i_out` = its largest switched
  current and the model's duty, prepared by `prepare_converter(model, leg, region=)` on **one shared region**
  (all legs' pads, the rail capacitors near them, the input path), so all legs use the same cached build. Legs
  skip DC runs, the conducted table, common mode and the input filter. `finish_bridge` interpolates each leg's
  complex transfer to the input (|T| log-log, phase from the ratio to leg A), forms I_line = Σ T_k·X_k, reads it
  with the receiver against the limit, gives each capacitor's ripple RMS from its share at f_pwm and the worst
  leg's L·di/dt and radiated margin. The snubber enters the ringing (`snubbed_response`): node impedance
  C_oss ∥ (R_s + C_s); without one it reduces to the converters' formula. A leg with no hard edge at the operating
  point (a locked rotor holds one commutation step: one leg low, one off; edge < 1 % of the largest) loses its
  `hot_loop` / `emi_margin` issues, with a note; its solves stay for the sweep's other speeds.
- **Views**: a `BridgeResult` (kind "bridge") comes before its legs (ordinary converter views). Its maps canvas
  draws the drive (`draw_bridge`: phase and bus currents, a commutation zoomed), the middle one the capacitors'
  RMS bars, the spectrum the receiver reading. Map actions (contours, arrows, Sharper map) are refused there.
  KPI tiles in `ui/kpi._bridge`; text `bridge_text`, JSON `bridges`, JUnit per bridge.
- **One bridge by hand:** `tools/dev/bridge_check.py` runs
  one bridge with options (`--motor R,L,KE,POLE_PAIRS[,star|delta[,trapezoidal|sinusoidal]]`: the 6th field is the
  EMF shape), `--budgets` for convergence, `--sweep-i/--sweep-speed`, `--no-victims`.
- **Milestone 2** (branch `bridges-m2`, merged): losses, RMS currents, thermal, capacity, the sweep, fixes.
  - **Losses** (`bridge_losses`): the waveform keeps each leg's state (`DriveWaveform.states`); `conduction()`
    gives each side's channel mean square and reverse-conduction mean, `switching()` sums over the switching
    instants (the record's repeated breakpoints, plus the period's wrap) the voltage a side takes over times the
    current at its turn-on (overlap ½·Δv·I·t_r), Δv² (output charge E_oss·(Δv/V)², E_oss = Q_oss·V of one side's
    switches together, as the form and the ringing take C_oss) and every node swing Δv² (a snubber's ½·C·Δv²);
    an open phase's node is v_n + e_k from the conducting legs. Turn-off is soft (GaN), the gate drive left out.
    Capacitors ESR·I_rms², series parts R·I_rms² (a `short` model's 1 mΩ is a numerical stand-in: a note, no
    loss). Test L1: locked rotor, constant current, every term by hand.
  - **RMS currents** (`bridge_rms.py`): the copper is linear, so the mean square density is a quadratic form in
    the sources. Slow sources (below f_pwm/2): each leg's hs and ls (battery pin → that side's switches → motor
    pin; `DriveWaveform.ls` runs from the node INTO ground, the unit solves take −ls), six DC solves on a region
    of their own (`rms_runs`, solved with the first leg's runs); their correlation G = mean·meanᵀ + ½·Re Σ X·Xᴴ over
    the slow lines. Fast: each leg's hot loop at n·f_pwm (n = 1..3, the conducted solves it already has), the
    high sides' cross spectra per band (the energy above band 3 put in it). `RmsBasis` keeps the unit fields and
    branch currents so any waveform re-weights them (the sweep; `drive_spectra` gives a waveform's mean, slow lines
    and per-band cross spectra). Maps per layer (fast resampled onto the slow grid), sustained/peak, vias by name
    (every via with ≥ 1 % of the largest; a part's plated hole is not a via), series parts, copper loss. Shown as a
    Heatmap view (`rms_view`: a copy of the first leg with the RMS maps, as `dc_view` does for a DC path); `_rms_issues`
    raises `current_density` and `via_overload` from them as the DC paths do. Left out: the motor's PWM ripple
    current. Tests R1 (the slow/fast split of a chopped current against the pulse train's
    Fourier series), R2 (the motor-pin via carries the phase RMS; the battery sensor the bus's slow part).
  - **Thermal**: `_bridges` takes the bridge results with RMS maps; `_layer_currents` also resamples each one's RMS
    map; its J² is added as heat (mean squares, not vectors); `part_losses` + the bridge's losses (`_merged`, one row
    per part). Capacity: a bridge is a group of its own (its copper heat ∝ s², its losses a quadratic fitted to the
    drive at 0.5/1/1.5 × i_phase, `_bridge_terms` over `_bridge_losses_at`: the drive again at s·i_phase, `caps_at`,
    series parts × s). The
    plugin, CLI and thermal process pass `BridgeResult`s through. Test T1.
  - **Sweep** (`sweep`): per (i_phase, speed): `six_step`, `RmsBasis.evaluate`, `caps_at` (each capacitor's
    shares of the new switching currents), `bridge_losses`, `conducted_rows` (the receiver on the kept
    transfers); L·di/dt and the radiated margin scaled with the largest switched current. Test W1: the point at
    the operating point equals the full result. Shown as a Heatmap view of its own (`sweep_view`,
    `draw_bridge_sweep`, `kpi._sweep`, `sweep_text`).
  - **Fixes**: a bridge's issues are power issues (not route advice); each leg's hot-loop candidates (stitching
    vias, a capacitor at the switches) link to the leg's or its bridge's issue (test F1).
  - **Line spectrum fixes** (`pwm_lines`): three breakpoints at one time are one jump (a ramp squeezed between
    them was a few float steps long, its slope change ruined the low lines by 20×); a breakpoint on the period's
    end moves to the start (a jump on the wrap was lost). Test S6.
  - **GUI stalls**: the status bar wraps with one `GetPartialTextExtents` call (`wrap_words`; wordwrap's call per
    word cost a timer tick each beside a busy job: 0.7 s), and the bridge tail (receiver, sweep) runs with a
    short GIL switch interval (one thread by then): Simulate's worst stall 722 → 173 ms on the test bridge.
  - Not done: H-bridges (two legs), stacks of several boards (shelved by the user), the gate drive's loss.

## Victims and stacked boards (`analysis/victims.py`, `analysis/stack.py`)
The user: some stacked ESCs (a logic board over the power board) reset under high load; a single-board ESC never
does. The question: does the bridge's switching reach the microcontroller (a moving ground, noise on reset / PWM /
Hall lines)? The stacked designs cannot be shared, so the tool runs on them at the user's; it is validated on
synthetic stacks (`tests/boardgen.py` `bridge_board(header_gnd_pins=)` + `logic_board()`).
- **Power stages too** (M3, 2026-10-17): `find_victims(board, config, stages, rail, ground, input_connector)` takes
  switching stages instead of a BridgeSpec (the reference falls back to the first low side's or integrated stage's
  ground pad); `stage_rows` (one stage's rows from its victim run: noise M·I/t_r per probe, part ground shift,
  injected C·V/t_r from its switch node), `finish_rows` (order, limits, checked), `victim_limit`, `victim_text` are
  shared by `bridge._victims` (the worst leg per victim) and `power._stage_victims`. A converter of its own (not a
  bridge leg) gets `_stage_victim_run` in `prepare_converters` when `victims.enabled` and something is found: its hot
  loop (cell, rail caps, the inductor's switch-node pad) plus `region_points`, the victims' terminations, planned at
  LOOP_F_LO / LOOP_F; ConverterSetup.extra_runs already carried `victim_run`, so the scheduler solves it with the
  stage. Rows on ConverterResult.victims, `victim_coupling` issues on the stage, lines in `converter_text`, a table in
  the HTML stage. Validated (tests/test_stage_victims.py, `discrete_buck_board(victim=(x0, x1, y))`: a 10k pull-up
  R5 to an STM32 U5): the noise is M·I/t_r at U5.4 (0.15 mV near the loop), the far line has under half the
  coupling, the stage's own loop_nh unchanged within 2 %, an issue over a lowered limit. kraft_flux_mini_2 conv1
  (out/audit settings): Simulate 169 s -> 221 s with the victims (the model then reaches U1102 / U1101 and their
  reset and debug nets), loop 1.491 nH and the conducted margin -20.8 dB unchanged; noise 20.6 mV at U1102.28 on
  SW_CLK (4.1 pH), 10.8 mV on the supervisor's reset, 4.1 mV on SW_DIO, 1.5 mV on NRST, all far under 0.3 V; U1102
  and U1101's grounds 1.7-1.8 V peak against J101.1 (0.35 nH; information: no reference pin set). The debug pattern
  takes SW_CLK / SW_DIO since (it wanted SWCLK).
- **Stack** (`stack.py`, settings `stack:`): the other board merged into one `Board`: placement from the joined
  connector pins (`fit_placement`: 2-D Kabsch, mirrored or not, the residual in the notes), its layers renamed
  `<name>:<layer>` at their z (`_combined_stackup`: above or below, its top facing us or not), an air dielectric in
  the gap, its parts and nets prefixed except the nets a pin joins to ours (one net), and each joined pin a `Via`
  (`uuid` "stack pin …") from its facing layer to ours, so the solver gives it a vertical branch with the pin's
  inductance. Applied at the start of `simulate()` (also for Suggest, through it); not in the thermal solve or
  on-board drawing yet. `ref_prefix` skips the board prefix (logic:C5 is a capacitor).
- **Victims** (`victims.py`, settings `victims:`): found by part value (microcontrollers, reset supervisors) and
  net name (reset, crystal, Hall, debug, PWM), or listed. Per bridge, each leg's hot loop is solved once more
  (`ConverterSetup.victim_run`, one of its `extra_runs()`, planned at LOOP_F_LO and LOOP_F) on a model whose region also holds
  the victims' pads and tracks, the reference pin and the stack's pins, with the victim nets as solved copper.
- **Receiver noise, how and why**: each victim net gets a driver branch (its pull-up's value from the settings or
  the footprint, else `drive_ohms`) and a receiver branch per receiver (`input_pf`), each to the ground copper
  nearest the pin (`ground_node`: the nearest layer in the stackup with ground there). The receiver voltage
  V(rx) − V(gnd at rx) per A of loop current comes straight from the solution; M = |v|/ω at LOOP_F, peak M·I_edge/t_r.
  First tried: φ_gnd(drv) − φ_gnd(rx) − jω∫A·dl with A summed from the solved strips as point currents. That
  failed a consistency test (`φ` between two points of a currentless plane must equal −jω∫A·dl along it; off by up to
  2.5× and in sign, since 0.8 mm strips as points are wrong 0.2 mm above a plane). Putting the net into the model
  uses the solver's own coupling; the divider test (10 kΩ vs 50 Ω driver: the receiver's share changes by
  |R + Z_in|/|50 + Z_in| ≈ 32, matched within 10 %) checks the terminations.
- **Injected current**: `facing_capacitance`: victim tracks (flat-ended buffers) intersected with the leg's switch-
  node copper on every other layer, minus the copper of any layer between (shielded), ε0·εr·A/h; × V_bus/t_r. Plate
  formula test and a plane-between test.
- **Ground of a part**: φ of its ground pins against the reference pin, L_g = Im(Δφ)/ω, information only unless the
  user sets `reference` (a part's ground against a distant battery terminal does not reset it; the synthetic stack
  showed it barely changes with the number of ground pins, since no current crosses them).
- **Results**: `BridgeResult.victims` rows (kind net / injected / part, worst leg, value, `limit`, `checked`),
  `victim_coupling` issues, text in `bridge_text`, a KPI tile (largest receiver noise), and bars of peak/threshold
  beside the capacitor bars (`draw_bridge_caps`).

## Detection labels (`analysis/tags.py`)
**Signal labels** (2026-10-11): `sig`, `sig:PIN`, `sig_drv:PIN`, `sig_rx:PIN` (`SIG`, `BoardTags.signals`,
`signal_marks`: {net: drivers, receivers}, cached per board). A marked net is never a supply (`is_power_net`),
is always suggested (Detect, Add KiCad selection, also when it carries a power stage's current), and its labelled
driver and receivers replace the guessed ones. Labelled nets joined by 2-pin parts are one path (`_labelled_path`): the parts that don't pass a signal go into its `through`, and it is named after a net with a real name (kraft_flux_mini_2: U301's buffer output, sig_drv:4, through R303 4.7 kΩ to the ADC pin, sig_rx:9 on U1102: /CPU/ADC_I_RAIL_1, driver U301.4, through R303; C304 to GND is a shunt of the path). The user chose a symbol field over a KiCad net class (proposed:
a `GT_SIGNAL` net class from a directive label) for the ADC input /CPU/ADC_I_RAIL_1, refused as a supply.

An optional `GT` field on schematic symbols says what a part is in a power stage (`conv_hs`, `conv1_L`, `hbA_ls`,
`hb_out`, `ignore`; user docs in README "Detection labels"). The user asked for "labels like hb_A … to make the
automatic recognition more robust"; options weighed:
- **Net labels** (`hb_A` as a net name): rename nets the designer chose, and roles belong to parts (which FET is
  the high side), not nets. Not used.
- **Component classes / net classes** (KiCad 9+): meant for design rules; one class per part; a class on a reused
  sheet's symbol is the same in every copy. Not used.
- **A symbol field** (chosen): reaches the board as a footprint `(property "GT" ...)` (checked: a real board's
  footprints carry their MPN, Rating, Config … fields), is read live through kipy (`FootprintInstance.definition
  .items` → `Field`; not yet checked against a running KiCad), changes nothing in the layout, bulk-editable in the
  Symbol Fields Table.
- **Reused sheets**: KiCad keeps only reference and unit per sheet copy (checked in `phase.kicad_sch`'s
  `(instances ...)`), so a field is the same in all copies. The board has each footprint's `(path
  "/<sheet uuids>/<symbol uuid>")`, `(sheetname "Phase B")`, `(sheetfile "phase.kicad_sch")`: `Footprint.sheet`
  (the path without the symbol: the copy's key), `sheet_name`, `sheet_file`, also from kipy (`sheet_path`,
  `symbol_sheet_name`, `symbol_sheet_filename`). Grouping (`board_tags`): parts with the same kind and id form a
  stage; without an id, or when the id recurs in several copies of the same sheet file, each copy is its own stage
  (named after the sheet; a leg's phase from its sheet name when no id and no connector pin gives one).
- **Detection** (`detect.py`): labelled converters first (`_tagged_converter` → `_converter_at`, the former loop
  body, with the labelled high/low sides taken as given, not guessed from which side reaches ground), then the
  inductor search over unlabelled parts. A labelled stage that cannot be built leaves its parts to the search and
  says why (`BoardTags.problems`). Bridges: labelled legs from their `hb_hs`/`hb_ls` parts (any reference prefix:
  `_switch_fets` includes them), then the unlabelled leg nodes; the phase from the label, the connector pin, the node
  name, the sheet name, in that order. `conv_in`/`conv_out`/`hb_in`/`hb_out` restrict the connector search
  (`_connector_search(among=)`, `_split_input(among=)`), falling back to the whole board with a reason. `ignore`
  parts are left out of `_switch_fets`, `pass_switches` and the inductor search.
- **Feedback**: `BoardTags.summary()` (cached on the board as `_gt_tags`, so detection's problems join the parse
  problems): `groundtruth detect` prints it, the Converters and Bridges tabs show it in their `InfoBox`
  (`ui/widgets.py`: a one-line header with Show/Hide over a read-only text; the Nets tab's explains net naming).
  The help texts live in `tags.HELP`. `tools/dev/add_labels.py` writes a labelled copy of a board for trying labels.
- Tests: `tests/test_tags.py` (parsing, reused-sheet grouping, a leg and a converter only labels find, `ignore`,
  a labelled input among two connectors, a half-labelled converter, the kipy reader, the CLI), and a GUI test.

## Cross-section along the route (`analysis/section.py`, `report/plots.draw_section`)
- `route_section(sol, st, route, own, half_mm)`, called in `finish_route` with the map's solution (`rep`) and in
  `sharper._route`; stored in `RouteResult.section` (not in the JSON summary). Stations along `route.steps`
  from the driver, every max(grid pitch, length/400); at each a cut along the normal, ±half_mm, sampled every
  half pitch (midpoint rule); complex Kx, Ky from `Solution.node_field` interpolated bilinearly
  (`scipy.ndimage.map_coordinates`, 0 off the grid), the route's own nets' nodes zeroed (a pour it runs through
  carries the signal). Per plane layer I_l(s) = −Σ (K·t)·du, A per A: positive = flowing back as the return.
- Plotted: Re(I_l), the part in phase with the 1 A signal (the excitation is real). Across all the copper
  Σ_l I_l = 1 at any frequency (KCL), so the sum inside the corridor is the share that stays near the trace;
  a dip = a lateral detour. The corridor is ±20 h (1–5 mm, `corridor_mm`): (2/π)·atan(20) = 96.8 % of an ideal
  microstrip return. A cut near a corner or past the end of the route (where the return converges on the
  receiver's return pin) counts what crosses it there; near a signal via the cut catches the current curling
  round the antipad (local peaks above 100 %).
- `draw_section`: top, each layer's share and the dashed sum Σ; bottom, a side view, copper layers as equal rows
  (real thicknesses differ too much to read), planes coloured RdBu ±100 %, the signal with a dark halo on its
  layers, its layer changes, the carrying vias/parts (`point_transfers`, the largest `SECTION_CARRIERS` (8) of them,
  since `transfers` now keeps every carrier ≥ 1 %; projected onto the route by `station_at`) as green bars with
  staggered labels (dashed with the distance beyond 2× the corridor). The plugin's hover over it reads the shares
  at the mouse (`fig._gt_section`, `section_value`). Layer
  colours as swatches beside the row labels; margins in inches from the text sizes (see "Plot text" below).
  With little height the axis label moves into the title, then the share plot goes (side view alone). In the
  plugin it is the middle plot of the Heatmap tab's scrolling page (below). **A canvas whose size changes**
  froze the window (0.68–0.84 s in `test_jobs_run_off_the_gui_thread` when the section came or went in the old
  one-screen layout): matplotlib's resize marks a canvas undrawn, its next paint renders the figure on the GUI
  thread, and every figure's draw takes one class-wide lock (`Figure._render_lock`, matplotlib 3.11), held
  meanwhile by the background thread rendering the new view. Fixes: `PrerenderedCanvas._on_paint` paints only
  the background while `defer()` (the panel's `rendering()`) says a rendered figure is on its way, and
  `_blank_canvases` clears the three figures before the section comes or goes (they are rendered again right
  after). Longest GUI gap in that test 0.675 → 0.197 s.
- **The Heatmap tab's page** (`HeatmapPanel`, session 6; the user: "make it scrollable and have every graph
  occupy the entire width of the application"). Controls, tiles and the matplotlib toolbar stay at the top (the
  toolbar is made on the canvas's parent, the page, and reparented to the panel); below, a `wx.ScrolledWindow`
  (`ALWAYS_SHOW_SB`, so its client width never jumps) holds the maps, the section and the spectrum, each full
  width. Heights (`_fit_page`, before the sizes are read for a render): the maps the page's visible height, or
  `plots.heatmap_height_in` when the maps are limited by the width (a wide board: no empty bands above and below
  them), at least `MAPS_MIN_PX`; the section `plots.section_height_in` (an inch of share plot, 0.3 in per
  layer); the spectrum `SPECTRUM_IN`. A page resize (`_page_resized`) sets `_resize_pending` (so `rendering()` is
  True and the canvases paint only their background) and renders the view again via one `wx.CallAfter` per burst
  of size events; no timer, because wx timers do not fire while the GUI tests wait with `wx.Yield`. The wheel:
  matplotlib binds `_on_mouse_wheel` by name, so `PrerenderedCanvas` overrides it: on the page the wheel scrolls
  it (`scroll_page`, one notch = 3 units of `SCROLL_PX`), Ctrl + wheel goes to matplotlib (the map zoom) and is
  marked handled so Windows does not also pass it to the page; the enlarged window's canvas (no `page`) zooms
  with the wheel alone. Replaced the one-screen layout (maps 5 : row 2, section beside the spectrum), whose lower
  row was too low for readable text and needed a show/hide rule (`SECTION_MIN_PX`, `_refit_section`) against
  wx GDI errors on a canvas squeezed to 0 px.
- **Plot text** (`report/plots.py`, session 6; the user: "the legend and graph descriptions are very small and
  hard to read"): sizes `FS_MARK` 8 (bold labels on the maps), `FS_TICK` 8.5, `FS_TEXT` 9, `FS_TITLE` 10 points;
  at the plugin's 100 dpi 9 pt is 12.5 px, the window's own text. Margins are computed in inches from them
  (`_lh`; `_width_in` sums per-character advances from matplotlib's font, measured once per character by
  `_advance`, no renderer: `text_to_path` costs ~20 µs a character and stalled the GUI thread 0.6 s when the
  page's heights were worked out there; `_wrap`; `_frame` shrinks
  margins in proportion on a figure too small for them instead of raising). Frequency axes carry their unit on
  the ticks (`_freq_axis`: "100 MHz", "1 GHz", the 2 and 5 steps under two decades) and need no label; legends
  sit on the title's line or in a row of their own above the plot (`_title_and_key`), never over data. The
  dark theme (`theme.style_figure`) also colours figure texts, figure legends and left titles.
  `tests/test_plot_text.py` renders every figure at the plugin's and the report's sizes and fails on text under
  8 pt, text outside the figure, and any title, note, legend, axis label or row of tick labels overlapping
  another or another plot (labels at vias on one map may overlap each other: that is the board's density).
- Real board (kraft_flux_mini_2 SPI_SCK, route model, `tools/dev/section_check.py`): 0.217 vs 0.149 mm grids
  agree within 0.2–1.9 % mean per layer (single stations next to pads and vias up to 44 %); In4 carries
  50–70 % under the B.Cu run, 68 % of the return stays within ±2.7 mm.

## 3D view (`report/view3d.py`)
- One self-contained HTML page per view (`view3d_html`, `write_view3d`): JSON scene data in a
  `<script type="application/json">` and a small inline script that builds a CSS 3D scene (`preserve-3d`, a
  `perspective` container, `rotateX`/`rotateZ` on a camera div): no WebGL, no library, nothing from the network.
  Chosen over plotly.js (3.5 MB, a new dependency) and pyvista/VTK (a large native dependency in KiCad's venv).
- Scenes (`view3d_data`): the view's own maps (route model, power stage or DC path) and, with plane capacitance,
  `RouteResult.board_maps` (`simulate.board_maps`, filled in `finish_route` from `crep`, the board-wide solution
  at the worst harmonic): per layer |K| and (Kx, Ky) at that solution's dominant phase, per plane pair the
  displacement current density at the same phase (A/m² per A, + = upper → lower), and its vias/parts (computed
  without the return-pin rule: at a resonance the vias at the return pins carry the circulating current; demo:
  the ICs' GND vias, 4.84 + 4.82 = 10.64 − 1 A per A, KCL). Not in the JSON summary.
- Each layer is an image rendered with matplotlib Agg (transparent PNG, the heatmap's norm and colours, alpha
  0.10 + 0.85·n^0.8, arrows from `arrow_field`), placed at equal z steps in stack order with the board outline and
  the signal as SVG polylines on it. Bars are two crossed ribbons hanging from the upper layer (`rotateX(-90deg)`).
  Displacement sheets (RdBu, ±the 99th percentile) only for pairs with ≥ 10 % of the busiest pair's Σ|J|
  (`DISPLACEMENT_MIN`; on kraft_flux_mini_2 the pairs across a perforated plane only cluttered the stack), half-way
  between their planes. Labels live in a 2D overlay at the projected positions of 1 px anchors
  (`getBoundingClientRect`), moved down where they overlap.
- Security: a CSP meta (`default-src 'none'; img-src data:; style-src/script-src 'unsafe-inline'`), the JSON with
  `<`, `>`, `&` escaped (it cannot close its script element), board names written with `textContent`, file names
  from `file_stem` (letters, digits, `_.-` only). Plugin: `HeatmapPanel.open_3d` builds the page in a
  `LatestOnly` worker, writes it under `views_dir()` and calls `webbrowser.open`. CLI: `_write_pngs` writes
  `view3d_<stem>.html`, `render_html(view3d=True)` links it (`#board` selects the board-wide scene).
- Checked in Edge (headless screenshots, see CLAUDE.md): the demo board, a stitched layer change, kraft_flux_mini_2
  SPI_SCK (route model and the 740 MHz board-wide model) and its U503 hot loop. Page sizes 0.1–1.3 MB, built in
  0.4–1.6 s.

## HTML report (`report/html.py`)
- One self-contained page (figures as embedded PNGs, CSS and a small script inline, nothing from the
  network). Image resolution (2026-10-15, the user: "make sure you export the highest resolution images
  available"): the plots at `DPI` 200; each map at `plots.heatmap_dpi` (at least PX_PER_CELL 1.5 pixels per model
  cell across its panel, from draw_heatmap's own layout, between DPI and MAP_DPI_MAX 400); `render_html(sharper=)`:
  views sharpened in the plugin replace the ones they match (`_view_key`: kind, net, receiver, view_of; the
  plugin passes every view with `sharper`); a click toggles a figure to its natural size (`figure.full`). A test
  buck: 0.8 -> 1.2 MB at 1x, 0.9 -> 1.7 MB at 16x for sharing and CI artifacts: the plugin's top-bar **Export report…** (`MainFrame.on_export`, a job,
  then opened in the browser) and the CLI's `--format html` (`check`/`simulate`; `suggest` writes
  `groundtruth-suggestions.html` with the fixes).
- `render_html(board, issues, routes, mode, settings, suggestions=, converters=, view3d=, bridges=, thermal=,
  capacity=, solver=, config=, progress=, max_vias=, outlines=, names=)`. Sections (`part`, numbered, each in the contents
  column): cover (board, date, mode, settings file, where it was solved (`gpu.describe`), version, a non-1×
  resolution, and `config.report`), Summary (KPI cards, the 8 worst findings), Board (`report/boardview.py`, below),
  Issues (severity filter,
  sortable), Signal nets (`_net`), Power stages (`_stage`, with each DC path's `dc_view`), Motor bridges (`_bridge`:
  drive, capacitor bars, legs, `rms_view`, `sweep_view`, victims), Temperatures (`_thermal`, capacity rows), Suggested
  fixes, Method and limits (`_appendix`: grid, resolution, node budget, standards). A bridge's legs appear under their
  bridge, not as power stages.
- `_Figures` renders every figure through `figure_png` and reports progress per figure against a precomputed count
  (the plugin's export is a job with a bar); its `heat()` passes the plugin's `max_vias`, `outlines` and `names` on,
  so the report's maps mark what the window's do. The CLI passes none (every carrying via, no outlines or names).
- **Board** (`report/boardview.py`, session 7, the user: "use ibom or create an ibom style pcb export"; they chose
  our own view over running their installed InteractiveHtmlBom, which needs KiCad's pcbnew and the saved file, so
  the CLI's reports have it too). It replaced the static `draw_board` overview.
  - `board_data(board, views, issues, config)`: compact JSON in a `<script type="application/json">` (`</`
    escaped). It holds:
    - edges, and tracks per layer (arcs via `arc_points`);
    - vias;
    - pours per layer (`to_shapely`, simplified 0.02 mm);
    - parts, sorted as iBOM sorts them (`_natural`), with pads (`pad_shape`, a side mask, THT on both),
      outline, bbox, side and the model text;
    - the analysed views' `route_xy` and parts;
    - the issues in the Issues table's order (`ordered_issues`, the same numbers), with the parts they concern.
  - `BOARD_JS` draws it on a canvas:
    - front, or back mirrored as iBOM does; a copper layer's pours (alpha) and tracks, vias, pads;
    - outlines and references sized to the part, the routes, and numbered severity-coloured issue markers;
    - pan, wheel zoom, double-click to fit; colours from the page's CSS variables (dark mode);
    - a Parts and an Issues list (filter): hover a row → the part lit on the board; click → zoom to it; click a
      part or marker on the board → its row and an info line.
  - `BOARD_CSS`; print: the canvas only.
  - Tests: `tests/test_boardview.py`, including `node --check` of the script when Node is installed.
- `ReportSettings` (`config/settings.py`, settings `report:` title / company / author / document) fills the cover;
  the plugin fills an empty author or company from the user's Preferences (`on_export`). Screen: a sticky contents
  column that follows the page (IntersectionObserver), light/dark by `prefers-color-scheme`; print: A4 with page
  breaks before the main sections. All text through `html.escape`.

## Simulation history (`report/history.py`, `ui/history_panel.py`)
The user (session 8): "a folder that contains the history of that board and is located under the projects root and
every time the user simulates a copy of the PCB is saved in there and a record is created with the results". Their
choices: the folder `groundtruth-history/` (now `groundtruth/history/`, the user: "It's already under
groundtruth"); unchanged boards share one zipped copy; each run keeps its results as JSON **and** the HTML report;
a History tab with a comparison (and the CLI).
- **Layout** (`history_for(board_path)` → `History(<board folder>/groundtruth/history, board=<board
  file name>)`, `settings.project_dir`; no folder per board, the user: "there is not reason to create a folder
  called kraft_flux_mini_2 under groundtruth-history, just use runs"): `runs()` lists the runs whose `run.json`
  `board.name` is the board's (or names none), `all_runs()` every run, which sharing, deleting and the renames
  use (two boards in one folder share the folder; their copies differ by hash anyway).
  `boards/<run id>.kicad_pcb.gz` (each distinct board once, `gzip` level 6, mtime 0; the kraft board's 4.9 MB
  text zips to 1.0 MB; named after the run that first kept it, the user: "This should match better, maybe simple
  call the PCB zip file the same as the run" (it was `<sha256>.kicad_pcb.gz`); later runs on the same board (same
  SHA-256) name that file in `run.json` `board.file`; deleting the run it is named after renames it after the
  earliest run still using it and updates their `run.json`, `_rename_orphaned_copies`) and `runs/<YYYY-MM-DD_HH-MM-SS>[-k]/` with `run.json`, `results.json` (the text of
  `formats.to_json`, as `simulate --format json`), `settings.groundtruth.yaml` and `report.html`. A run's folder is
  made with an exclusive `mkdir` (two runs in a second get `-2`, `-3`); every file goes through `.part` +
  `os.replace`, so a half-written run has no `run.json` and `runs()` skips it.
- **`run.json`**: format, time (local, with offset), `run_meta` (version, `by` plugin/cli, board name/path/
  `source`, resolution, what-if and the edits' labels, `only` (CLI `--nets`), seconds, solver), the board's and the
  settings' SHA-256, the note, and `headline(results)`: the issue counts and per route / stage / bridge
  (`kind`, `name`: routes `net driver->receiver`) the radiated and conducted worst margins and frequencies,
  `loop_nh`, `l_ratio`, `error`. The list and the comparison read only `run.json`; the issues come from
  `results.json`.
- **Untrusted on read** (the folder may come with a shared project): `run.json`'s `board.file` is used only when it
  is a run id + `.kicad_pcb.gz` (`BOARD_FILE`, fullmatch: no paths); `board_bytes` tries it, then any other run's
  copy with the same SHA-256, then the first version's `<sha256>.kicad_pcb.gz`, each unpacked with a 1 GB cap
  (`MAX_BOARD_BYTES`) and checked against the run's SHA-256; run ids must match `RUN_ID`, deletion only removes `runs/<id>` folders and
  then the board copies no run names; notes are one line, at most 2000 characters.
- **Deleted or edited by hand** (the user: "make sure that manually deleting data like the pcb files or the report
  doesn't crash the feature"): every file may go at any time. A run without a readable `run.json` is left out of
  the list; `Run` reads its fields type-checked (`counts`, `items`, `_part`), so a garbled `run.json` shows blanks;
  `results.json` or the settings copy missing → `compare` says so in `missing` and still compares the figures; a
  board copy missing or not gzip → `HistoryError` ("missing or damaged"), and the next run on the same board
  writes it again; `delete` of a run already gone counts it; the whole folder gone → no runs, the next Simulate
  starts it again; a report for a run deleted while it renders → `write_report_checked` raises `HistoryError`.
  The History tab checks a report or folder in its worker before opening it ("no longer there"), and any failed
  request puts its message in the status line and reads the list again (`_failed`; `refresh` reports its own
  errors directly, so this cannot loop). Tests: `test_files_deleted_or_edited_by_hand_never_crash_it`,
  `test_a_deleted_board_copy_is_made_again_by_the_next_run_on_that_board`, and the end of the GUI history test.
- **`changes(runs)`**: per run what differs from the one before (board, settings, resolution, what-if).
  **`compare(a, b)`**: rows keyed by (kind, name) with (a, b, b − a) for the margins, `loop_nh` and `l_ratio`; the
  issues fixed and new by fingerprint (waived ones left out); `how` (board / settings changed, resolution, what-if);
  the settings' unified diff (`difflib`, at most 200 lines). Each run's worst margin is its own refinement's (no
  aligned harmonics as in `suggest.compare_results`: the solutions are not kept).
- **Plugin.** The board copy is taken in the load job right after `Session.load` (`Session.board_copy`): live,
  `kipy_source.board_text` (kipy's `get_as_string`, a read-only `SaveDocumentToString`, unsaved edits included),
  else the saved file (`source` "saved file"); file mode, the file. It is kept in memory (`MainFrame._board_copy`) so the copy is the
  board analysed, not what KiCad holds by the time of Simulate. `run(True)` takes `_history_inputs` on the GUI
  thread (the settings frozen with `SettingsFile.frozen_text`, a deep copy, ~60 ms on a 200-part board); the
  Simulate job calls `keep_run` after the solve (gzip + JSON, under a second; a failure only notes itself in the
  status line). `_done` refreshes the History tab and hands the report page (`_report_args`, shared with Export
  report, without the fix search) to `_history_reports`, an `InOrder` worker (`ui/background.py`: FIFO, never
  drops a request; `stopping` set at exit so a render ends at its next figure). Closing the window hides it and
  waits up to `REPORT_CLOSE_WAIT_S` (60 s) for a report being written. The History tab's reads, notes, compare,
  board unpacking and deletes run in its own `InOrder` worker; `history_busy()` covers both workers (tests,
  `window_shot.py`). Preferences: `history` and `history_report` (both on by default) in ui.json.
- **CLI.** `simulate --history` (`_keep_run`: the board file's bytes, the settings as loaded, the report rendered
  in line); `history BOARD [--compare RUN [RUN]] [--json]` (`History.find`: an id, its start, or 1 = the newest).
- Tests: `tests/test_history.py` (layout, sharing, ordering, find, compare, notes, deletes, hash and path checks,
  damaged runs, the CLI), `tests/test_gui.py` (`test_simulate_keeps_each_run_in_the_history_tab`, the live board
  copy and its fallback). `tests/conftest.py` gives every test a private `%LOCALAPPDATA%` whose ui.json turns the
  history off, so GUI tests also no longer read the user's own preferences.

## Current arrows (`Solution.sheet_vectors`, `results/vectors.py`)
- `Solution.node_field` averages the strip currents onto the nodes (as `sheet_current` always did) and keeps
  the complex (Kx, Ky). AC fields are complex, so `sheet_vectors` takes them at the **dominant phase**
  φ = ½·angle(Σ I²) over every strip of every layer (the principal axis of the complex field), with the sign
  that correlates with Re(I) (the 1 A excitation is real, so arrows follow the signal); cached per solution.
  DC solutions are real. Tests: a DC strip points + → − with |J| = the density map; a microstrip's plane
  return flows back under the trace and sums to −1 A per A; the result does not change when the whole
  solution is phase-shifted.
- Stored as float32 next to `sheet_current`: `RouteResult.sheet_vectors` (routes, converter hot loops) and
  `ConverterResult.dc_vectors[path]` (A/mm², set by `dc_analysis`; `dc_view` hands them to the view). Not
  in the JSON summaries.
- `arrow_field(grid, K, Kx, Ky, n_across | spacing_mm, max_arrows, gamma, ref)`: **equal small arrows whose number
  follows the current** (the user's choice, session 4: one standard arrow everywhere, more of them where more
  current flows). The map is cut into spots of b×b nodes (`n_across` across, or `spacing_mm` on the board);
  a spot's fill is (mean |K| / ref)^gamma, capped at 1, 0 below FLOOR (1 %); an 8×8 ordered-dither (Bayer)
  pattern switches on that fraction of the spots, evenly spread and identical on every run; each arrow sits
  at its spot's copper centroid (jittered ±¼ spot where the spot is all copper), points along the spot's mean
  current, and is 0.7 spot spacings long. gamma 0.5 compresses the decades a density spans and stays
  monotonic. `ref` = `reference_density`: the current-weighted 95th percentile of all layers shown (the level
  95 % of the current flows below), shared by the panels of a figure and by the board drawing. Weighting by
  current was needed on the real board: a plain percentile put a DC output path's reference at the level of
  the idle pour around it (arrows everywhere), and per-panel references turned a layer's numerical noise into
  full fill. Plots: 45 spots across one panel, 30 for several (`draw_heatmap`, quiver, `angles="xy"`, shaft a
  sixth of the arrow). Board: 0.8 mm spots (0.56 mm arrows), at most 1500 (`kipy_sink.draw_arrows`: shaft +
  two ±25° head strokes, group `groundtruth-arrows`).
- **Arrow widths on the board** (session 10, the user: "high density, thick arrows, low density very thin arrows";
  the contours then were not good enough): `arrow_field(with_density=True)` adds each spot's mean density as a
  fifth value; `arrow_widths(densities, peak, w_min, w_max)` maps it log-linearly over the heatmap's three
  decades, w_max at the result's peak over all layers (so the layers compare), w_min three decades below and
  lower. w_min / w_max are Preferences ("Thinnest / Thickest arrow line", ui.json `arrow_min_mm` /
  `arrow_max_mm`, 0.01 / 0.3 mm; `prefs_dialog._arrow_range` swaps them if reversed); a thick arrow gets a longer
  head. The plots' arrows stay equal. Test `test_board_arrows_get_thicker_with_the_current_density`.

## Plugin (`plugin/`, `ui/`)
- **Top bar** (`MainFrame.__init__`, drawn `theme.Button`s): Reload board, Refill zones (live), Run checks,
  Simulate, Thermal, **Export report…** (see "HTML report"), the "with what-if edits" switch, Cancel, Save settings,
  Clear EMI layers (live), and on the right **Preferences…** and **Help**. There is no theme selector any more:
  the theme is a preference. **Help** (and F1, an accelerator) opens `ui/help.py` `HelpWindow`, one window raised
  again, scrolled to the shown tab's anchor (`topic_of`, `TOPICS`); links outside the page open in the browser. A
  feature that changes a tab or what Simulate computes updates help.html in the same branch.
  - Since 2026-10-16 (the user: "can we make this help look more modern and better formatted?") it shows
    `modern_page()` in a browser engine (`wx.html2.WebView`: Edge WebView2 on Windows, WebKit on a Mac; KiCad 10's
    wxPython 4.2.2 has Edge and RunScriptAsync): `ui/help_shell.html` (CSS from the theme's palette, `css_vars`; a
    sidebar of the sections in `GROUPS` that marks the one in view; search with marks, Ctrl+F, Enter for the next)
    around help.html's sections (`sections()`: the parts between its `<hr>`s, each `<a name><h3>`, as cards;
    `<font color=@WARN@>` as `.warn` spans; the old contents list left out). help.html stays plain HTML that wx's own
    `wx.html.HtmlWindow` renders: `page()` is the fallback (no engine, GROUNDTRUTH_HELP_CLASSIC, or the page not
    loaded within LOAD_WAIT_MS / an engine error: `_not_loaded`, its CallLater stopped on EVT_WINDOW_DESTROY).
    Scripts run with RunScriptAsync: the synchronous RunScript waits in a loop of its own, which hung outside the
    main loop (a test) and would nest the window's events. A link to another page is vetoed in the view and opened
    in the browser (EVT_WEBVIEW_NAVIGATING). Opening takes ~0.06 s on the GUI thread; the page is there ~0.8 s later.
- **Preferences vs settings.** A board's settings file describes the board and its requirements (nets, stages,
  bridges, models, rules, analysis budgets) and gives the CLI and the plugin the same results. The user's own
  choices for *this computer* live in `%LOCALAPPDATA%\GroundTruth\ui.json` (`ui/theme.prefs` / `save_prefs`,
  merged writes), edited in **Preferences…** (`ui/prefs_dialog.py`): Simulate's resolution, solver threads, GPU
  and thermal quality (the last three "From the settings file" by default), the heatmaps' via markers / vias
  shown / part outlines, the report's author and company, the theme; and the working toggles' last state
  (`remember`). `MainFrame._sim_config` builds the config Simulate and Suggest fixes run with (the board's plus
  `prefs_dialog.overrides` and `analysis.resolution`), one object per combination so the fix search can tell the
  last Simulate still applies; `_thermal_inputs` applies the thermal quality; `on_export` the author / company.
  Tests patch `theme.pref_path` (or LOCALAPPDATA) so they never write the user's file.
- **Resolution** (`analysis.resolution`, never in the settings file; the plugin's Preferences, the CLI's
  `--resolution`, both one of `simulate.RESOLUTIONS` 1/2/4/8/16/20/25): `_choose_pitch(..., resolution)` starts at
  the dielectric height (or grid_mm) / √N and allows N × the node budget, for the route, power-stage, DC/RMS and
  board-wide models alike, so a model whose budget did not bind gets finer too; `RESOLUTION_MIN_MM` 0.02. The
  dialog shows each level's cell from the last Simulate's coarsest model (`MainFrame._note_cells`,
  `resolution_label`); the report's cover and appendix name a non-1× factor. Sharper map sets its own pitch
  (resolution reset to 1, its budget 4 × max_nodes × the resolution). Test `tests/test_resolution.py`.
- **Heatmap markers and outlines.** `draw_heatmap` records each map's markers on its figure (`fig._gt_marks`:
  issue circles rank 0, vias and parts 1); `mark_at` answers the plugin's hover (`HeatmapPanel._hover`: readout,
  cut to `READOUT_CHARS`, and the full text as tooltip; an issue wins over a via in reach).
  **Click** (session 10, the user: click an overloaded via "and it should highlight it on the PCB"): the figure
  also keeps what each marker stands for (`fig._gt_mark_refs`: the Issue or the via / part transfer dict);
  `mark_ref_at` finds it under the click (`HeatmapPanel._press` → its `on_mark` callback), `MainFrame.on_map_mark` shows it in KiCad in
  the highlight thread, newest click winning: an issue as the Issues tab does (`issue_highlight`), a via or part
  marker as the copper at that point (`highlight.point_highlight`: via, else pad, else track within POINT_MM
  0.15 mm; its net lit unless a reference net). Without a live KiCad the status line says so. Which issues a view
  marks: `plots.view_issues` (a DC path's view only that path's `:dc:<path>` / `:via:<path>` issues; the
  stage's other views none of its DC paths'; the user saw the output path's overloaded via on the switch-node
  map). Tests in `test_highlight.py`, `test_plot_text.py`, `test_gui.py`.
  `max_vias` limits the via markers to the busiest (`_carriers`; `HeatmapPanel.max_vias()`: 0 with Via markers
  off, else Vias shown, None = all); every carrier with ≥ 1 % of the current is in the results (DC paths, bridge
  RMS maps, signal routes; before the report branch they were cut to the largest few). `Footprint.outline`
  (courtyard, else fab; the file reader only) feeds `_part_outlines` (top solid, bottom dashed, with references);
  live KiCad and outline-less parts get a box around their pads. `set_display` redraws with new preferences.
- `plugin/plugin.json` (IPC schema v1): actions `analyse` (`groundtruth_action.py`), `add-net`
  (`groundtruth_add_net.py`, toolbar button with a green plus) and `clear` (`groundtruth_clear.py`). KiCad 10
  starts an action's entry script **without** plugin.json's `args` (`API_PLUGIN_MANAGER::InvokeAction` runs
  `Execute({ pluginFile })`), so each action has its own script. The schema has no context-menu or hotkey
  field: plugins cannot add right-click entries. Manifest: identifier `io.github.iwers.groundtruth`, runtime
  python ≥ 3.11, scope `pcb`; `analyse` and `add-net` have toolbar buttons (`show-button`), `clear` none (the
  window's top bar has Clear EMI layers too); light and dark icons at 24/48 px in `plugin/icons/` (drawn by
  `tools/make_icons.py`).
- **Entry scripts**: each takes `time.time()` on its first line (for the start-up log), puts its own folder on
  `sys.path` (the bundled `groundtruth` copy lives next to it) and calls `ui/app.py` `plugin_main` with its action:
  `groundtruth_action.py` passes `sys.argv[1:]` (empty from KiCad, so `analyse`: the window), the other two pass
  `["add-net"]` and `["clear"]`. `clear` (`app._clear`) opens no window: it connects, `kipy_sink.clear` removes the
  items of every `groundtruth-*` group and the groups in one commit (one undo step), and a message box gives the
  count. `plugin/requirements.txt` is what KiCad installs into the plugin venv: kicad-python ≥ 0.8, numpy, scipy,
  shapely, ruamel.yaml, matplotlib, contourpy, wxPython ≥ 4.2.2, and numba, pyamg, psutil (the thermal solve; numba
  also compiles `solver/_mac.py`). `plugin/demo/` is the demo project (`tools/make_demo.py`: the test suite's
  layer-change board, zones filled by kicad-cli, checked in). Delivery: `tools/install_plugin.py` (a copy, below)
  or `tools/make_pcm_package.py` (a Plugin and Content Manager zip, `out/dist/groundtruth-<ver>-<commit>-pcm.zip`,
  installed to `3rdparty/plugins/io_github_iwers_groundtruth`; `build_package.cmd` wraps it); only one of the two
  may be installed.
- **Add selected net** (`ui/app.py` `_add_selected_net`, `ui/inbox.py`): every action is a process of its own,
  so the button's process reads KiCad's selection (`kipy_source.selected_nets`: the nets of selected tracks,
  arcs, vias, pads, zones, copper shapes) and writes a request (net names only) into the board's inbox,
  `%LOCALAPPDATA%\GroundTruth\windows\<board key>\requests\`. The board's window registered itself there
  (`window.json`, its PID) once it had read the board, and takes requests on a 0.5 s timer
  (`MainFrame._check_inbox` → `add_nets`). The button waits up to 3 s for that and ends; with no live window it
  opens one itself (reusing its KiCad connection), which takes the request after reading the board. So the
  window stays the only writer of the settings file. `AllowSetForegroundWindow` lets the window come to the
  front. Requests are validated (≤ 50 names of ≤ 300 characters) and written atomically.
  `requirements.txt` is installed by KiCad into the plugin venv. `tools/install_plugin.py` copies
  `plugin/` + `src/groundtruth` into `<Documents>/KiCad/<10+>/plugins/io.github.iwers.groundtruth`.
- **Start-up** (`ui/app.py` `plugin_main`): the entry script's first line takes the time. A small
  `StartWindow` ("GroundTruth – starting", pulsing bar) appears as soon as wx is imported; a worker thread then
  connects to KiCad and imports the analysis modules (numpy, scipy, matplotlib, shapely: ~1.7 s) while the start
  window's event loop keeps running. Then `MainFrame` replaces it. No module creates wx objects at import time
  (colours are RGB tuples), because the imports run in that worker. `ui/startup.py` logs every step with times
  to `%LOCALAPPDATA%\GroundTruth\plugin.log`. `ui/kicad_watch.py` closes the window when the KiCad that started
  it exits (KiCad is the grandparent: KiCad → venv launcher `Scripts\pythonw.exe` → KiCad's `pythonw.exe`).
- **Thermal solve** (`analysis/thermal.py`, `groundtruth/thermal/`, docs/THERMAL_PLAN.md M2 option A):
  `groundtruth/thermal/` is PCB Thermal's solver (kicad/board.py, solver/*, materials, project; no UI; its tests
  in tests/thermal/). Its modules: `kicad/board.py` (its board model and `load_board`, with its own `kicad/sexpr.py`
  and `kicad/geometry_utils.py`), `materials.py` (`Conductor`, `Dielectric`, `COPPER`), `project.py` (setup data:
  `BoardSetup`, `ComponentPower`, `Environment`, `MeshSettings`, …), `solver/stack.py` (`build_stack`),
  `solver/geometry.py` (copper per net and layer), `solver/raster.py` (numba scanline coverage), `solver/mesh.py`
  (block-structured adaptive mesh: `Mesh`, `plan_refinement`, `shape_samples`, `HeatSamples`), `solver/thermal.py`
  (3-D conduction: `ThermalModel`, `component_heat_areas`), `solver/convection.py` (h correlations),
  `solver/common.py` (`Progress`, cancellation, the SPD solver), `solver/coupled.py` (`plan_mesh`; its `run` and
  `solver/electrical.py`'s own DC model, `solver/results.py` and `solver/worker.py` (PCB Thermal's process runner)
  are used only by tests/thermal or not at all: GroundTruth brings its own currents and its own child process,
  `thermal_proc.py`). `thermal/adapter.py` (`to_thermal_board`) maps GroundTruth's Board to PCB Thermal's
  (checked: identical areas, stack, counts and solved temperatures against PCB Thermal's own parser). `copper_heat` resamples every power
  stage's DC-path current vectors (`dc_vectors`, A/mm²) onto one grid per layer (the finest path pitch) and adds
  them as vectors (paths share copper), heat ρ|J|²t·A per sample; `part_losses` from the operating point and the
  DC paths' series parts (switching on the hard-switched transistor, `main` = a buck's high side or a boost's low
  side; `_synchronous`: a FET rectifier or an integrated stage → one hard edge ½·V·I·t·f and Q_oss·V·f, a diode →
  two edges and ½·Q_oss·V·f; C_oss·V² stands in for Q_oss·V without `q_oss`; session 6, after the user's EPC2306
  datasheet: C_OSS 482 pF vs Q_OSS 38 nC at 50 V); `solve_thermal` plans PCB Thermal's adaptive mesh from those samples, builds its
  Mesh/ThermalModel, puts the samples on the copper nodes (`mesh.locate`), and iterates with copper resistivity
  following the node temperature (currents prescribed: first order). Validation (tests/test_thermal.py): a
  50 × 4 mm strip at 5 A: 0.1539 W = analytic I²R; peak rise 9.09 K vs 8.94 K from PCB Thermal's own electrical
  model (1.7 %); energy balance. Plugin: the "Thermal" job solves in a child process
  (`analysis/thermal_proc.py`: `python -m groundtruth.analysis.thermal_proc IN OUT`, pickled inputs and result,
  progress lines on stdout, no console window, below-normal priority, Cancel kills it): pyamg's C++ holds the
  GIL for half a second at a time, a 528 ms window stall when it ran in a thread. The CLI solves in-process.
  The result is a `ThermalResult` (a RouteResult with `kind="thermal"`, °C maps in `sheet_current`,
  `heat_scale="linear"`) in the Heatmap list. `solve_thermal` is three steps that the capacity search reuses:
  `_build_model` (mesh plan, Mesh, ThermalModel, the samples' nodes), `_coupled` (the iteration, from any start
  temperature) and `_result` (maps, parts, issues).
- **Memory watchdog** (`thermal_proc._Watchdog`, M3, after PCB Thermal's runner): the parent checks the child every
  0.5 s and kills it with `ThermalMemoryError` (a MemoryError: the window shows the message only) above 60 % of the
  RAM, or when less than 5 % (≥ 0.5 GB) is free while the child holds more than that. It sums the process tree:
  a venv's `python.exe` is a launcher whose child is the interpreter (killing the launcher ends the child: job
  object, checked). The child's peak memory goes to `mesh["memory_mb"]`. Test: a 20 MB limit stops a solve.
- **Current capacity** (`thermal.capacity_inputs`, `solve_capacity`, M3): per stage the load-current scale s at
  which max(T, junction estimates) = `thermal.capacity_c` (default max_c), the others at their operating points,
  and, with several stages, all scaled together (`ALL_STAGES`). The heat is exact in s: the DC solve is linear,
  so a stage's copper heat is |s·J_stage + J_rest|² = a·s² + b·s + c per sample (b: the cross term in shared
  copper), and part_losses at s = 0, 1, 2 give each part's quadratic (conduction, DCR ∝ s², switching and a
  diode ∝ s, C_oss·V²·f constant). Part heat per watt (`component_heat` with 1 W each) makes the parts' heat
  vector a sum. One model, each trial a `_coupled` solve warm-started from the last temperatures (the AMG
  hierarchy is reused: 2-5 s per trial on the test buck at draft, after an 8 s build); bracketed secant on s²
  (`_search`) to 0.25 K, bounds "zero" (over the limit at no load) and "above" (under it at 10x). **Two passes**:
  PCB Thermal's mesh refines where the heat is, so a model planned for the operating point gave a capacity whose
  fresh solve was 1.2 K off on the test buck; pass 1 (draft, planned for the operating point, 2 K) finds each
  capacity, pass 2 (thermal.quality, planned for the envelope of the heat with each stage at its capacity)
  settles it; the fresh solve then agrees to 0.5 K. Validation: linear case (α = 0, fixed h, no radiation) gives
  exactly 2x the current for 4x the rise; a fresh solve at the found current hits the limit; the cross term
  against copper_heat. On kraft_flux_mini_2 (assumed 4 mΩ / 5 mΩ) a stage's capacity is ill-conditioned when the
  other stage alone keeps a part near the limit (SW1: 1.1 A at draft, 0 A at normal: Q303 at 105 vs 107.8 °C
  with SW1 off); the "all power stages" row is the meaningful number there: 44 % of both loads at normal
  quality, confirmed by a fresh solve to 0.01 K.
- **Out of memory in parallel** (`simulate.run_parallel`): a solve that raises MemoryError beside others is
  retried alone after the pool (gc in between); alone it fails with a message (close programs, lower
  analysis.max_nodes). Found when kraft_flux_mini_2's full Simulate (four stages, four nets, ~2 GB per model
  at once) failed with ~5 GB free next to the user's other programs. SuperLU reports its own failed allocation
  as a `RuntimeError` ("SUPERLU_MALLOC …"); the factorisation in `solver/peec.py` re-raises it as `MemoryError`,
  so it is retried alone like any other.
- **Theme** (`ui/theme.py`, after PCB Thermal): `init_app` runs right after the wx.App is created, before the
  first window (wxWidgets 3.3's `SetAppearance` refuses later): System follows Windows, Light / Dark force it;
  the choice is the `theme` key of `ui.json`, set in Preferences (since the top bar's theme selector went), and
  so takes effect the next time GroundTruth opens. Native controls draw light or dark themselves and are
  left native on purpose: a list with non-default colours (set, or inherited from a panel's foreground) is
  custom-drawn per cell by Windows and cost 150-290 ms on its first paint. `style_tree` only colours labels
  named "muted"; `style_figure` darkens the matplotlib figures in the render thread (only that figure). `Button` (a wx.Control drawn with a
  GraphicsContext: gradient body, shadow line, top highlight, hover / pressed / focus / disabled; sends
  EVT_BUTTON), `Tile` (the KPI tiles). No wx objects at import: colours stay hex strings until used.
- **KPI tiles and readout** (`ui/kpi.py`, `HeatmapPanel._show_tiles`, `report.plots.map_value`): tiles per view
  kind with statuses from the settings' rules; `draw_heatmap` records `fig._gt_maps[ax] = (layer, grid,
  values, unit)` and a motion callback (`_hover`, kept across `PrerenderedCanvas` swaps) looks the value up, or
  what a marker under the mouse is (`mark_at`, above).
- **Tabs built in steps** (`MainFrame._build_pages`): the tabs' ~200 native controls cost ~2 ms each on the GUI
  thread, three times that when created inside the shown window (Converters: 316 ms there, ~110 ms hidden;
  `Freeze` does not help). The constructor makes only the frame, top bar, status bar and an empty notebook
  (~20 ms); each tab is then built in a hidden holder frame and `Reparent`ed in, one per `wx.CallLater` step
  (largest ~130 ms), and the board is read after the last. `busy()` is true until then, so anything waiting for
  an idle window waits for the tabs too. `tools/dev/build_profile.py` times each step. Before, the window's
  ~0.3 s build was one block, and `test_plugin_start_never_blocks_its_window` failed in full runs (0.53 s).
  A panel may list more `build_steps`, run one per step while it is still in the holder: the Bridges tab builds
  its lists and buttons first, then its ~45-control form in three steps (`BridgesPanel(in_steps=True)`). In one
  step it blocked the GUI thread 0.32-0.43 s, at the limit, and `test_the_window_builds_its_tabs_in_steps`
  failed one run in four; now 60-125 ms per step, and on a kraft_flux_mini_2 copy `gui_probe.py` shows one stall
  over 150 ms when the window opens (Converters, 184 ms) instead of two (314 and 182 ms).
  Since session 10 the Nets (2), Converters (3), Heatmap (2) and Bridges (5) tabs have `build_steps`
  (`in_steps=True` from the window; built at once otherwise, as tests and tools construct them): every step
  ≤ 90 ms in `build_profile.py` (Nets 130-170, Converters 155-240, Heatmap's three canvases ~110 ms before).
- **Garbage collection** (`ui/gil.freeze_long_lived`): once per process, when the window has its tabs, the
  objects alive then (the modules: ~110 000 objects; the window's) go into Python's permanent generation
  (`gc.freeze`). A full collection holds the GIL from start to end, whichever thread triggers it, and walked
  them all every time. They live as long as the process, so nothing collectable is kept; only once, since a
  test process opens many windows. kraft_flux_mini_2's results: full collections 133-190 → 113-167 ms (the
  results and the render worker's figures are most of what is left).
  Since 2026-10-17 (`gil.settle_garbage`, `MainFrame._settle_when_idle`): after new results show, a 500 ms timer
  (stopped on destroy with the others) waits until no job runs, no map is drawn and the user has been idle for
  `idle.QUIET_S`; then `gc.unfreeze()`, one `gc.collect()` (the replaced results' cycles) and `gc.freeze()` of
  everything alive, results included. gc.freeze does not keep anything alive (reference counting still frees);
  only cycles dying later wait for the next settle. Before, every automatic full collection walked the results:
  gui_probe on kraft counted about eight of 150-360 ms while the views were looked through.
- **Idle-time work** (`ui/idle.py`): `input_idle_s()` is the system's time since the last mouse or keyboard input
  (GetLastInputInfo on Windows, Quartz CGEventSourceSecondsSinceLastEventType on macOS, else inf: no waiting),
  `wait_until_quiet(stop)` sleeps until it reaches `QUIET_S` (3 s). The history's report page
  (`MainFrame._keep_report`) waits for it before it starts and before each plot (render_html's progress
  callback): a plot is one long C call holding the GIL (Agg drawing, PNG encoding), and the 25-50 plots right
  after a Simulate stalled view and tab switches up to 1.3 s (gui_probe with per-thread CPU: the
  groundtruth-history-report thread 1.1-1.5 s CPU in each). The user chose this ("go with a") over drawing the
  page only when it is opened. `MainFrame._closing` (set in on_close) ends the waiting: nobody uses a closing
  window. tests/conftest.py sets `QUIET_S` to 0 (a person at the machine would hold the tests up); test_idle.py,
  test_gui `test_idle_time_work_waits_for_the_user`. gui_probe stands in for the user: `input_idle_s` counts from
  its last step, and it pauses READ_PAUSE_S (15 s) after Simulate as someone reading the results would.
- **KPI tiles** (`HeatmapPanel._show_tiles`, `theme.Tile.update`): another view gives the tiles already there
  its figures; only missing ones are made. `_fit_page` lays the scrolling page out once inside `Freeze`.
- **Destroy events bubble**: `EVT_WINDOW_DESTROY` is a command event, so a child window's destruction (the tab
  holder, the heatmap window) reaches the frame's handler too. `_stop_timers` acts only on the frame's own id
  (wxPython may return a new wrapper for a dying window, so not `is self`); before, closing a child frame would
  have stopped the inbox, KiCad-watch and Parts-follow timers.
- `ui/main_frame.py`: `Session` abstracts live (kipy) vs file mode. Tabs: Nets (`ui/nets_panel.py`, incl.
  Detect… dialog, review status), Converters (`ui/converters_panel.py`: stages with Detect…, edit form, review
  status), Bridges (`ui/bridges_panel.py`: motor bridges with Detect…, edit form, review status), Parts
  (`ui/parts_panel.py` `PartsPanel`: a `wx.grid` of every part with value, footprint, model and its parameters,
  edits saved to the settings; a filter by reference, value, footprint or prefix; set the filtered parts to one
  model, or reset them to `suggest_model`; `select_refs` follows KiCad's selection, below), Issues
  (`results_panels.IssuesPanel`: a click shows the issue in KiCad, see below; draw markers, waive), Heatmap
  (`ui/results_panels.py` `HeatmapPanel`, with `PrerenderedCanvas`, `HeatmapWindow`, `ZoomToolbar`: view + layer
  selector, the scrolling page of maps, cross-section or a bridge's capacitor bars, and spectrum, KPI tiles, draw contours / arrows on User.7, Open in window, Open 3D view, Sharper
  map, current capacity), Fixes (`ui/fixes_panel.py`: suggest, draw ticked on User.8, place ticked vias, add
  ticked to what-if, compare). The top bar's switch "with what-if edits" applies the settings file's enabled
  edits to Run checks / Simulate. The Heatmap and Issues tabs' toggles keep their last state (`remember`).
- **Showing an issue in KiCad** (`analysis/highlight.py`, `kipy_sink.show_items`, `MainFrame.on_issue_selected`).
  An issue names at most one offending segment (Tier 1: the segment nearest the piece's midpoint, a via repeated
  per layer pair, sometimes a zone, a footprint or '') and simulation and power-stage issues name nothing, so
  `issue_highlight` finds the copper in the board model by check (the table is in the README): `focus` (zoomed
  to; its nets highlighted), `context` (selected after the zoom: planes wider than 25 mm never enter the focus),
  `layer` (made active: the reference plane of gap, edge and foreign-plane issues), `zoom_out` (2 steps for a
  focus under 3 mm, 1 under 9 mm), `lit` (the focus copper on the issue's own nets: KiCad's highlight lights up
  the nets of everything selected at that moment, so ground vias, return pins and reference nets stay out of it).
  KiCad 10.0.5 has no API to move the view or highlight a net directly, so `show_items` selects through raw
  `AddToSelection` (unknown ids are skipped; `GetItemsById` would fail when none exists) and runs KiCad's own
  editor actions through `RunAction`, in this order: select `lit`, `pcbnew.EditorControl.highlightNetSelection`
  (only if KiCad found some of `lit`: with no copper selected it would take the net under the mouse), select the
  rest of the focus, `common.Control.zoomFitSelection` + `zoomOutCenter` steps, select the context, set the
  layer. `pcbnew.EditorControl.clearHighlight` runs only for a highlight we set (`Session.net_lit`), so the
  user's own stays; `Session.saved_layer` gives the user's active layer back with the next issue without a
  layer and at close. Names verified in the 10.0.5 source; RAS_INVALID is reported, not fatal. No commit, no
  undo step. A restart after AS_BUSY part-way runs the whole sequence again (it is idempotent).
  Avoid from the API: `highlightNet` and `highlightItem` (their int/pointer parameter arrives as bool: the
  mouse's net, or just a cleared selection), `zoomPreset`, and anything that starts an interactive tool (every
  later request answers AS_BUSY until the user presses Esc). KiCad master has a real `FocusOnItems` call
  (2026-09); switch to it once a release has it.
  The Nets and Converters tables use the same path (`MainFrame._show_rows`): `nets_highlight` (all copper of the
  selected nets, lit; pours by the plane rule) and `stages_highlight` (the stages' parts plus the copper of their
  whole power flow, lit, from `power.stage_power_nets`; never ground).
- **Series pass FETs** (`detect.pass_switches`): a surge stopper's or reverse-polarity FET (Q601 between J101's
  V_BATT and +VDC on kraft_flux_mini_2) has the default `open` model, so the connector search (`input_path`
  crosses only short/resistor/inductor/ferrite models) stopped at it. A FET with a gate pin, drain and source on
  two nets that are neither ground nor an inductor's (switching FETs always touch their switch node's inductor)
  is a pass switch. `_connector_for` searches again with those taken as closed (`_closed`: a config copy with
  short models) when the plain search finds nothing, and `DetectedConverter.models` carries a `short` model for
  each one crossed. `SettingsFile.add_detected_converters(specs, models)` / `add_suggested_models` write them
  only over an `open` model, so the user's R_DS(on) stays. With the model set, the solver's part branches
  (`StackBuild`: any non-open model between two plane nets) close the LISN path.
- **A stage's power nets** (`power.stage_power_nets`, `detect.power_nets`): input path, rail, switch node, the
  inductor's far side, the output path to the output connector (`input_path` through shunts, ferrites, 0R),
  the output return and ground, each with its role. `detect_nets` always skips them for the settings' stages
  (callers add the stages just detected through `skip=`), and `suggest_net` refuses them with the role.
  Found on kraft_flux_mini_2: `Net-(U302-Vs+)` (L301 to the 8 mΩ shunts, a ZXCT1107's Vs+ input on it) was
  detected as a signal before. For an integrated stage, `switch_inductor` takes the IC's SW/LX pin net first:
  the LT3970's V_OUT pin also shares L503 (from the +5V side), and "+5V" sorted before the SW net. `ui/listsel.SelectableRows` gives both tables multi-select, Delete/Ctrl+A, and sends the
  selected rows once the selection has not changed for `SHOW_SETTLE_MS` (300 ms, a restarted `wx.CallLater`; a
  Shift-click sends one event per row). Since 2026-10-17: the user saw the window lag clicking through the
  Converters tab in KiCad; every row passed over was shown (KiCad selected, highlighted and zoomed each), and
  `highlight._finish` measured every zone's extent with Python's min/max over ~140 000 fill points (40-200 ms per
  click, holding the GIL); now numpy, and the board's planes once per board (`highlight._planes`, cached in the
  board's `__dict__`, built in the load worker in live mode): 1-4 ms per stage. An
  empty selection shows an empty Highlight, which clears our highlight. Table refreshes (DeleteAllItems) send no
  deselect events on Windows, so a commit does not clear KiCad's view; Mark reviewed reselects its rows.
- **Sharper map** (`analysis/sharper.py`, `MainFrame.on_sharper`): rebuilds only the shown view's own model
  (`prepare_route` / `prepare_converter` on the board and settings the view came from, `_views_from`, which
  covers what-if runs) with `grid_mm` = the view's pitch / 2 and 4x `max_nodes`, solves the map's frequency
  only (a route: `map_frequency` on its fine model; a hot loop: LOOP_F; a DC path: its DC model), and returns a
  copy of the view with the new maps, layer shares and transfers; everything else is the full run's. The
  pitch must be set, not only the budget: where the budget does not bind, `auto` uses the dielectric height.
  The board-wide capacitance model is off for it (a route's map always comes from its fine model).
- **Heatmap layout** (`report.plots.map_grid`, `draw_heatmap`): the rows x columns that make each layer map
  largest for the figure's and the region's aspect; the maps share x and y (zoom/pan one, all follow).
  `results_panels.HeatmapWindow` ("Open in window") is one more canvas of the tab's view (its maps), rendered by the same
  background renderer; a resize re-renders it 300 ms after it settles.
- **Drawing layer** (`kipy_sink._drawing_layer`): every draw_* makes sure the board enables its layer before
  clearing or creating anything. Found on kraft_flux_mini_2 (User.1 to User.4 only): KiCad 10.0.5 created 1770
  arrow segments on User.7 but rejected the group. A missing layer is enabled (the user asked for that rather
  than an error) with `set_enabled_layers(copper count, every enabled non-copper layer + the new one)`: that
  call DELETES the content of any layer left out and cannot be undone, so the result is checked and a lost
  layer stops the drawing (`DrawingLayerError`). `take_enabled()` feeds the window's status line.
- **Unusual values** (`settings.implausible`, `rules/checks._unusual_values`): a converter's or bridge's value
  outside the usual range for its kind (c_oss, rise_time, f_sw, dcr ...; once found as the user's c_oss of 482 F,
  which made the ringing 0 Hz) is the info issue `unusual_value`, with what to write if a unit is missing. Until
  2026-10-10 `validate` refused such values and so blocked Simulate; kraft_flux_mini_2's L503 (DCR field 1330 mΩ
  = 1.33 Ω, a real value) was refused that way. The user: "Remove the limits, if the user decides thats the value,
  it's the value". So nothing blocks a run; a schematic field outside `Param.lo … hi` is used too, with a note.
- **Parts tab follows KiCad's selection** (`MainFrame._follow_kicad_selection`, `Session.selected_parts`,
  `kipy_source.selected_references`, `PartsPanel.select_refs`): KiCad 10 sends plugins no selection events, so
  a 500 ms `wx.Timer` (stopped on destroy) asks, only while the Parts tab shows and the window is visible, in
  its own `LatestOnly` thread. `selected_parts` never waits: it takes the KiCad lock without blocking and asks
  with `wait=0` (AS_BUSY gives KiCadBusyError at once), so a running job, a show or a KiCad dialog only skips
  that look. Only a changed selection moves the table (the user may have picked another row since).
- **Status bar** (`MainFrame.SetStatusText`, `_wrap_status`): the native MSW field draws one line, so a
  `StaticText` over field 0 shows the text word-wrapped to up to `STATUS_LINES` (3) lines (the rest cut with " …",
  the whole text as tooltip), the bar growing through `SetMinHeight` (0 = native height again for one line; while a
  job runs it only grows, since each height change lays out the whole window). The wrapping is `wrap_words`: one
  `GetPartialTextExtents` call per paragraph instead of `wx.lib.wordwrap`'s call per word, each of which gave up the
  GIL and cost a timer tick beside a busy job (0.7 s; see "Motor bridges" → "GUI stalls"). `StatusBar` keeps
  the texts (`GetStatusText(0)` and the tests read them) and leaves the native field 0 empty: its one line showed
  through at the label's left edge on a two-line bar (the user's screenshot, 2026-10-17). Field 1 holds the gauge and
  no text ("saved …" under it peeked out below the gauge; it was never visible otherwise). Since session 10 a rewrite with nothing else
  changed is two wx calls (the bar's text and the label's): the text is kept in Python (`status_text`), the
  field's rectangle until a resize, each character's width measured once (`_char_extents`, one
  `GetPartialTextExtents` for unseen characters), the tooltip and the label's size set only when they change.
  The clock's rewrite every second made a dozen calls and stalled the window ~220 ms during a Simulate. The
  KiCad inbox (`_check_inbox`) is not read while a job runs (its file reads gave up the GIL too: ~200 ms).
- **Threading** (`MainFrame._start`): the GUI thread only handles events and shows results. Everything slow
  is a *job* in one worker thread, one job at a time:
  - reading the board (IPC or file), checks, simulation, fix search, what-if application, contours, Sharper map,
    the report export (the Thermal job's solve itself runs in a child process, see "Thermal solve");
  - every KiCad call (draw, place, clear, refill).

  A job that fails or is cancelled has its traceback formatted, then `ui.progress.release_failure` clears the
  frames of its exception chain: an exception re-raised through `run_parallel`'s futures is a reference cycle, and
  it kept a failed Simulate's models and factorisations until Python's cycle collector happened to run
  (`tools/dev/mem_probe.py --fail error`: 1122 MB private after the failure without it, 738 MB with it, the 746 MB
  before). Test: `tests/test_progress.py` `test_release_failure_frees_what_a_failed_jobs_frames_hold_without_a_gc`.

  Showing a clicked issue is not a job (it would refuse during a simulation and flicker the toolbar): it runs in
  its own `LatestOnly` thread (the newest click wins). kipy's connection (one socket) is still never used by two
  threads, because every `Session` method that talks to KiCad holds `Session.kicad_lock` (`_kicad_call`). A show
  waits at most 0.5 s for it and is otherwise refused (KiCadBusyError in the status bar), so a click never moves
  the user's view seconds later, after a long KiCad job. Closing hides the window, queues the highlight clean-up
  behind a show still on its way (`_highlighted` is set when a show is submitted) and waits up to 3 s for it.

  Jobs report progress via `wx.CallAfter`, and their buttons are disabled meanwhile. **Cancel** sets a flag
  that the progress callback turns into a `Cancelled` exception at the next step. The tabs' **Detect…** buttons
  run as jobs too (`MainFrame._panel_job`; power-stage detection takes 4 s on kraft_flux_mini_2). Reading the
  settings file (the YAML parse, ~0.5 s) is part of the board-reading job (`_open_settings`).
  The status line of a running job comes from `ui/progress.py` `ProgressClock` (no wx): "message · 37 % · 1:12 ·
  about 2 min left". The fraction is kept monotonic (the gauge follows it, not the raw report); the time left is
  t·(1−f)/f, shown from 3 % and 5 s on, counted down between reports and blended 70/30 with each new estimate.
  `MainFrame._clock_timer` (1 s) redraws the line so the elapsed time counts through a long single step; it is
  stopped in `_finished` and in `_stop_timers`. The estimate is only as good as the jobs' fractions: Simulate
  and the fix search weight their steps by estimated solver work (`analysis/progress.py`, see "Fix
  suggestions"); the other jobs report plain step counts.
- **Background "newest only" workers** (`ui/background.py` `LatestOnly`): for GUI work where only the latest
  request matters, one thread each; a request still waiting when a newer one arrives is dropped. The threads are
  daemons; an `atexit` hook gives them up to 5 s to finish (a worker still inside matplotlib at shutdown crashed
  gui_probe with an access violation, see HANDOFF "Speed and progress").
  - Settings saves (`ui/saver.py`): every edit saves. The GUI thread only copies the document
    (`SettingsFile.snapshot`, ~60 ms); the worker emits the YAML (~400 ms) and writes it atomically (temporary
    file + `os.replace`). Closing the window waits for the last write (`flush`).
  - Heatmap renders (`HeatmapPanel.redraw`): every canvas's figure (maps, cross-section or a bridge's capacitor
    bars, spectrum, and the "Open in window" maps; ~0.4 s for maps + spectrum of a 4-layer board) is built
    and rendered on new `Figure`s with an offscreen `FigureCanvasAgg` in the worker; `PrerenderedCanvas.
    show_rendered` swaps the figure into the wx canvas and reuses the rendered pixels (no second render). It
    relies on matplotlib internals (3.11: `_canvas_callbacks` is moved over so the toolbar keeps its zoom/pan
    connections, `_original_dpi`, `_lastKey`, `_isDrawn`, `_create_bitmap`); if those change it falls back to
    rendering on the GUI thread, and `tests/test_gui.py` notices (`canvas.reused`). Toolbar zoom/pan and window
    resizes still render on the GUI thread (matplotlib's own path, ~0.25 s per step on a real board).
  - Native list fills are slow on Windows (~0.6-2 ms per item): the net combo boxes are filled only when they
    drop down (`ui/widgets.py` `set_lazy_items`), other lists inside `Freeze`/`Thaw`.
- **The GIL switch interval** (`ui/gil.py`). While a job runs Python code, the GUI thread waits for the GIL
  before every Python handler and every virtual call into a Python-derived wx window. CPython waits one switch
  interval for it, and on Windows a 1-5 ms wait lasts a timer tick (~15.6 ms): filling the Parts grid took 7.9 s
  instead of 14 ms beside a busy thread (KiCad's Python 3.11.5; 18 s on 3.13). Below 1 ms the wait rounds to zero
  and the GUI thread gets the GIL at once (71 ms), so the GUI processes run at 0.5 ms (`responsive_gui()` in
  `plugin_main` and `gui_main`). But then a waiting thread spins instead of sleeping; the solver's own threads
  wait for the GIL too, and their spinning made the stalls during a simulation more frequent. So solver jobs
  (Simulate, fix search, compare) switch back to Python's 5 ms while they run (`MainFrame._start(solver=True)`,
  `SolverInterval`). A real-board simulation takes the same time at either interval (84-89 s). Exception inside
  Simulate: the motor-bridge tail (`finish_bridge`: receiver, sweep) runs after the thread pool, on one thread, so
  `simulate()` drops the interval to ≤ 0.5 ms around it and restores it after (worst stall 722 → 173 ms on the test
  bridge). Tried and dropped: the GUI thread at above-normal priority (a spinning high-priority waiter starves the
  normal-priority GIL holder: worse).
- The GIL is not held by the numeric code: `splu` (both orderings), its `solve`, sparse products and
  conversions, `scipy.fft`, `numpy.linalg` all release it (a thread running them delays the main thread
  ≤ 22 ms). What is left during a simulation is CPU contention from the solver threads.
- **Measuring it:** `tools/dev/gui_probe.py BOARD --settings S --simulate` opens the window on a copy of the
  board (never next to it), steps through load, checks, simulation, every heatmap view and every tab, and prints
  every GUI-thread stall over 150 ms with the GUI thread's stack and the other threads' stacks, the garbage
  collections in it, the GIL switch interval and the threads that used the most CPU meanwhile (psutil, every
  CPU_EVERY = 0.25 s: listing the threads every 30 ms slowed a kraft Simulate from 870 to 2200 s). The tabs are
  walked before and after the status bar changes height. py-spy 0.4.2 is in the venv, but `--native` can't keep
  up on Windows (49 min behind after 15) and `--subprocesses` gives no native frames there: attach with
  `--pid` to the real interpreter (the venv's python.exe is a launcher with a child process). On
  kraft_flux_mini_2 (3 nets, 4 power stages, ~6.5 min simulation):

  | | stalls > 150 ms | worst | board load | heatmap view switch | during the simulation |
  |---|---|---|---|---|---|
  | before | 35 | 1076 ms | 587 ms | 0.4-0.7 s each | ≤ 1076 ms |
  | background render, settings in the job, lazy lists | 4 | 603 ms | 603 ms | none | ≤ 331 ms |
  | + 0.5 ms interval always | 12 | 513 ms | none | none | ≤ 513 ms |
  | + GUI thread above normal | 18 | 743 ms | none | one 695 ms | ≤ 743 ms |
  | per-job interval | 5 | 277 ms | none | none | ≤ 277 ms |
  | 2026-10-17, after M2-M5 (Bode views, DC paths, loops; the history report page drawn at once) | 39 | 952 ms | 174 ms | ≤ 481 ms; every tab switch 250-500 ms | ≤ 490 ms |
  | **+ idle-time work (history page, garbage settle; current)** | **7** | **280 ms** | **152 ms** | **≤ 219 ms; tabs none** | **≤ 280 ms** |

  The 2026-10-17 rows use `out/m5/kraft.groundtruth.yaml` (real_settings.py: 3 nets, 4 stages, ~13 min
  Simulate). The tab-switch stalls were the history report thread too: with its plots drawn only while the user
  is idle every tab switch is 0 ms. Left: full collections of 145-167 ms during some view switches (the objects
  made since the settle, mostly the render worker's matplotlib figures).

  `tests/test_gui.py` drives the window through `MainLoop`, including the plugin start with a KiCad that
  answers slowly.
- Live KiCad 10.0.5 quirks handled in the adapters:
  - The stackup items arrive marked as disabled, with no dielectric materials. Thicknesses are taken from
    KiCad, εr and tan δ from the saved file (`file_source.load_stackup` parses only the file header).
  - A group created in the same commit as its items is rejected. So the items are one commit and the group a
    second, and the group is verified.
  - While a dialog is open, KiCad answers AS_BUSY: `when_ready` retries.
  - KiCad centres text by default. Labels are left-aligned and stacked so they don't cover the markers or
    each other.
- `kipy_sink.place_vias`: builds a raw `board_types_pb2.Via` (through, PST_NORMAL, F.Cu circle, drill F→B)
  and wraps it with `Via(proto=...)`; setting the padstack on a fresh `Via()` did not stick. One commit, not
  grouped (it's a design change), and the matching what-if edit is switched off so it isn't counted twice.

## Compute server (`remote/`, session 10, branch `compute-server`)
The user: "run some sort of server that runs on a very powerful PC ... work on my personal machine in KiCad and use
the other powerful pc to do all the computation"; Windows 11 Pro server, local network, several users.
- `remote/protocol.py`: signed requests (HMAC-SHA256 over method, path+query, time, nonce, body hash; MAX_SKEW_S 300,
  NonceCache against replay), responses signed for the request's nonce; bodies = zlib(pickle); `SafeUnpickler`
  builds only groundtruth classes (types, never functions), NumPy's array/dtype reconstruction and plain builtins
  (the globals were traced on Simulate, thermal and capacity pickles); `code_id()` = hash of the package's .py files
  (CRLF-normalised); access key from GROUNDTRUTH_SERVER_KEY or %LOCALAPPDATA%\GroundTruth\server.key.
- `remote/server.py` `ComputeServer`: ThreadingHTTPServer, endpoints GET /v1/status, POST /v1/jobs?kind&client&code
  &numpy, GET /v1/jobs/ID[/result], DELETE /v1/jobs/ID; FIFO queue, `workers` slots; each job in its own process
  through `thermal_proc._run_child(module="groundtruth.remote.worker", raw=True)` (progress lines, Cancel kills it,
  memory watchdog at 85 % of the RAM / workers); results kept compressed until fetched and forgotten (DELETE) or 1 h.
  Refuses another code_id or NumPy major (409).
- `remote/worker.py`: kinds simulate (factorisations and models dropped), suggest (its before/after without
  models), thermal, capacity, sleep (tests only, GROUNDTRUTH_SERVER_TEST); the server's --threads / --gpu replace the
  job's (the resolution stays the user's).
- `remote/client.py` `Remote.run(kind, args, progress)`: submit, poll every 0.5 s (queue position, the server's
  progress "(on host)"), the plugin's Cancel (raised in progress) DELETEs the job; ServerUnavailable (not reachable,
  key, version) vs RemoteError (the job failed there).
- Plugin: Preferences group "Compute server" (ui.json compute / server / server_key, Test connection in a thread);
  `main_frame.simulate_on`, `_server(local)`; run, on_check_design, on_suggest, on_compare, on_thermal, on_capacity
  go there (thermal/capacity inputs are prepared locally, `thermal_proc.run_in_process(server=)`); `_start(local=)`:
  on ServerUnavailable `_finished` asks "Run it on this computer instead?". A server result keeps no models (Suggest
  fixes simulates its own baseline). `simulate()` now clears `ConverterResult.dc_branch` after combine_dc: it held
  the whole-board DC model (memory for as long as the window showed the result) and its lambdas blocked pickling.
- Tests: `tests/test_remote.py` (key, replay, version, queue order and positions, cancel from both sides, failure,
  Simulate equal to local, thermal and capacity from a server Simulate, Suggest fixes), `test_gui.py
  ::test_simulate_on_a_compute_server_and_here_when_it_is_gone`.
- **Detached jobs** (2026-10-14, the user: a job has an ID, results wait at least 24 h and go once fetched; "the client
  should not need to be connected all the time ... if the laptop sleeps and wakes up it should simply reconnect";
  only the user cancels; a finished but unfetched job must not block the next one; the number of parallel jobs
  configurable):
  - Server: every job on disk in the data folder (`<id>.json` state, `<id>.in` input until run, `<id>.out` result;
    `_restore` at start queues unfinished jobs again, a stopped running job starts over); `keep_s` 24 h
    (`keep_hours`), `_expire`; DELETE of a finished job removes its files; a finished job holds no slot. Settings
    `server.json` next to the key (`settings()`: DEFAULTS, the file, CLI overrides; made on the first start).
    With several workers and GPUs, slot k gets `CUDA_VISIBLE_DEVICES` = k mod GPUs (`_gpu_for`, the probe's count).
    GET /v1/jobs[?client=] lists jobs. The GPU probe runs in a child process (importing torch in the server process
    held the GIL for over a minute on the user's server: requests timed out).
  - Client: `run` waits a lost connection out (`wait_state` / `_retry`, every RETRY_S, never given up: the plugin's
    Cancel ends it); one-request helpers state / fetch(raw=) / forget / jobs.
  - Plugin (`remote/jobs.py`, no wx): `LocalJobs` in %LOCALAPPDATA%\GroundTruth\remote_jobs\<id>\ (job.json,
    input.pickle = board + cfg + `portable_keep` history inputs, result.bin); `Tracker` thread every POLL_S (5 s,
    poked after a hand-over): state, on done fetch + store + forget on the server, "lost" when the server no longer
    knows it. `MainFrame._hand_over` (Simulate and Suggest fixes with a server: submit, keep, return `HandedOver`,
    the window free), `_job_changed` (auto-shows a result handed over from this window, `_session_token`, for the
    open board, when nothing was shown since, `_shown_at`; else a status note), `on_show_job` (loads the result,
    `result_views`, `keep_run` with the history rebuilt from the path, then forgets the job here), `on_cancel_job`,
    `on_remove_job` (a queued or running job: asked once, cancelled on the server and removed, `ui/job_stop.py`
    `stop_on_server`; its server unreachable: a second question, `remove_anyway`; before 2026-10-15 Remove only
    tried Cancel, and a job on a server gone for good stayed listed); the **Server jobs** tab (`ui/jobs_panel.py`). The tracker stops with the window
    (`_stop_timers`); the jobs stay for the next start.
  - **Several windows, the list rebuilt** (2026-10-16, `ui/job_store.py`, the user: "current jobs are lost after a
    while, make sure the plugin querries the sever and rebuilds the list"): two plugin windows (processes, each a
    Tracker) wrote every job.json through one shared `job.json.tmp`; a shorter write over a longer one left a byte
    (`...}}`) and `LocalJobs.get` returned None: a running and a queued Simulate and a finished result left the
    list (reproduced: 2131 unreadable reads in 1.5 s with two writers). `SafeJobs`: a temporary file per process and
    thread, the replace retried while a reader holds the file (PermissionError on Windows), a damaged job.json
    repaired (`raw_decode`), finished states never set back (another window's stale running / lost), removals
    remembered (`removed/`). `SyncTracker.reconcile` every RECONCILE_S (30 s): GET /v1/jobs?client= on every known
    server (the jobs' and Preferences'), a missing queued / running / done Simulate or Suggest fixes listed again
    (`recovered`, no input.pickle: its result can't be shown, `has_inputs`); a job "lost" whose result.bin is here
    (another window fetched it) is ready. `close_stale` ends a waited-for job only when its window's process
    (`pid`) is gone, not when another window starts. In ui/ (not remote/jobs.py) so the code fingerprint stays.
    Server: a split job's coordinator (`Job.split`) holds no slot and is not counted in a queue position (`_busy`;
    it showed "2 job(s) ahead" with one job in a slot).
  - The jobs the window waits for (Thermal, Current capacity, Check in Simulate, Compare what-if on the server) are
    listed too (2026-10-14, the user: "thermal sims are not shown"): `Remote.run(watch=)` (or `Remote.watch`, which
    thermal_proc's server path uses) reports submitted / every state / end; `jobs.watcher` (made by
    `MainFrame._watch`) keeps an "attached" entry from them (the Tracker skips attached entries), ending shown /
    failed / cancelled; `jobs.close_stale` at the window's start ends the entries of a window closed meanwhile. The
    What column shows their label.
  - The tab has **Current jobs** and **Past jobs** (`remote.jobs.PAST`: shown, failed, cancelled, lost; kept until
    removed; `LocalJobs.finish` deletes a job's input and result files but keeps its job.json), the server computer
    (`host` in the server's job info and POST answer, `server_name`) and the elapsed time (`jobs_panel.elapsed`:
    waiting since handed over, the server's run_s at the last poll counted on, the run time once ended; a 1 s
    wx.Timer stopped on the tab's own EVT_WINDOW_DESTROY). A failure is kept in full: the server's `Job.detail`
    (the exception and the worker's stderr: its traceback; also logged), the tracker's `detail`, the details box
    and Copy details.
  - Tests: test_remote.py (restart + expiry + removal on fetch, settings file, a GPU per slot, never give up and
    only the user cancels, the tracker brings a job back, "lost"), test_gui.py (hand-over, auto-show, fallback).
- **Service install** (2026-10-14, the user: "make a service and installer"): `tools/server_setup.py` (install |
  update | status | key | start | stop | uninstall, `--dry-run`), run as administrator with KiCad's Python by
  `tools/install_server.cmd` (elevates itself); packaged by `tools/make_server_package.py` with the groundtruth
  package, requirements.txt, version.txt and `tools/SERVER_README.md`. Into C:\ProgramData\GroundTruth: app\
  (the package), venv\ (made from KiCad's Python; the plugin's requirements without kicad-python and wxPython,
  `--cuda` adds PyTorch), run_server.py (sets GROUNDTRUTH_HOME and PYTHONPATH, logs to service.log, runs `serve` in
  a loop: Task Scheduler restarts only a task that fails to start), the server's own files (protocol.HOME_ENV moves
  key, server.json, jobs, logs there). It runs on the **venv's python.exe**: KiCad's own Python ignores PYTHONPATH
  (built so), and a service on it started job processes and a GPU check that found neither GroundTruth nor the venv's
  libraries (found on the user's laptop ANDRE: "PyTorch is not installed" with PyTorch installed; jobs would have
  failed too). `groundtruth_app.pth` in the venv's site-packages puts app\ on every venv process's path. The venv's
  python.exe is a launcher, but it ties the interpreter to itself (a job object): killing it, as schtasks /End
  does, ends the server (tried).
  The service is a scheduled task (boot trigger, SYSTEM S-1-5-18, ExecutionTimeLimit PT0S, restart on failure,
  priority 5) rather than a Windows service (pywin32's service host does not run reliably from a venv); icacls
  restricts the folder to SYSTEM and Administrators by SID; netsh opens the port for private and domain profiles.
  A job process watches its parent and grandparent (`worker._end_with_parent`) and exits when the server is gone.
  Tried here without task/firewall/ACL: the venv from KiCad 10's Python, the libraries from PyPI (~3 min), the
  runner started the server and it answered. Tests: tests/test_server_setup.py, test_remote's orphan test.
- **A server's address** (2026-10-16, the user: andre (192.168.0.6) in DELL-7920-PC's cluster (192.168.120.128)
  missing from Preferences' Servers; "it would be good if status shows the servers IP"; "can we start a server with
  a known address ... and then get presented the available options?"): server.json `address` (`install --address`:
  `server_setup.ask_address` lists `cluster.adapters()` (network card, IPv4) through the install's own Python, or
  checks a given one; `auto` clears it). `ComputeServer.addresses(announced=)`: the chosen one alone in the beacons'
  `addresses`, first in the status (`addresses`, `address`; `status_block` prints them). `Cluster.targets` also sends
  each own address's subnet broadcast (`subnet_broadcasts`, from the card's netmask): Windows sends
  255.255.255.255 out of one card only. Preferences' `server_updates.check`: each member at the first of
  [the lead's address for it, its announced addresses] that accepts a connection (`cluster.reachable`), plus the
  servers a `find_servers` query heard that the lead does not name (by host and port).
- **Signed remote updates** (2026-10-16, the user: "Currently I have to physically go to the servers an update
  them manually ... show the user which server is out of date and offers to update it through a button press";
  asked: signed builds only, a busy server waits for its running jobs; "The signing key that is used to update the
  server should be reset if the server is re-installed manually"):
  - `remote/release.py`: Ed25519 (the `cryptography` library: the server's requirements, `SERVER_EXTRA`; the dev
    venv; not the plugin). The build PC's private key `private_key_path()` (%LOCALAPPDATA%\GroundTruth\release,
    made by the first build); `make_server_package.build` puts its public half (`release_key.pub`) in the package
    and signs the zip's bytes; `make_pcm_package` builds the server package of its own build into the plugin
    (`plugins/server_update/`, `bundled`) and writes it to out/dist too (one version for both installers).
  - Trust: `server_setup.trust_release_key` copies the installed package's `release_key.pub` into the install folder
    on every manual install/update (a package without one removes it); apply-update never touches it.
  - Server: `POST /v1/update?sig=` (`ComputeServer._take_update`): refused unless `remote_update` (server.json,
    `--remote-update on|off`, asked on a first install by `ask_remote_update`), GROUNDTRUTH_SERVICE (set by the
    runner), a trusted key, a valid signature, a newer `version_number` (no replay of an older build). Taken: the zip
    in updates/, `self.update` holds the slots (queued jobs stay queued), `_update_when_idle` after the last running
    job writes updates/pending.json and sets `exit_code` = UPDATE_EXIT (75); `serve()` returns it. Status:
    `remote_update`, `update` (waiting/installing, else updates/last.json).
  - Runner (RUNNER template): the server now runs as a child process (`serve_once`; a restart loads new code; the
    child ends with its runner: GROUNDTRUTH_PARENT_PID). On 75: `apply_update` unpacks the zip (paths checked) and
    runs the NEW package's `server_setup.py apply-update` (`cmd_apply_update`: app.new, pip only when the
    requirements changed, app -> app.prev, app.new -> app, a new run_server.py, last.json); an updated server that
    ends within HEALTH_S (120 s) -> `roll_back` (app.prev back, last.json "rolled back"). The runner itself picks up
    its new template at the next service start. Servers with the old in-process runner need one manual install.
  - Plugin: `client.Remote.update`; `ui/server_updates.py` (`check`: the configured server, its cluster members and
    `other_versions`, each asked; `row_of` with `release.compare`; `update`: send, follow the status until the
    server runs the plugin's code or its newer last.json says failed / rolled back); `prefs_dialog.ServersPanel`
    (Preferences › Compute server › Servers: the list, Check / Update / Update all, background threads, following
    stops on EVT_WINDOW_DESTROY).
  - Tests (tests/test_remote_update.py): signatures and versions; the endpoint's refusals and the busy server;
    apply-update and its failure; the runner's apply / roll-back / unsafe zip; the key reset; the signed package;
    the Preferences logic; end to end: a real runner + server updated by a signed package from this tree, then a
    broken newer one rolled back by the runner.
- **macOS** (2026-10-15, the user: "the server should support macs", an Apple M2): `groundtruth serve` itself is
  platform-neutral (thermal_proc's Windows flags are guarded; psutil everywhere; `core.sysmem.commit_available` is
  None off Windows). server_setup.py's Mac branch: launchd daemon `com.groundtruth.server` in /Library/LaunchDaemons
  (`launchd_plist`: RunAtLoad, KeepAlive, ThrottleInterval 10, output to service.log, run_server.py on the venv's
  bin/python3; `launchctl bootstrap system` / `bootout` / `kickstart -k` / `print` for start, stop, state),
  /Library/Application Support/GroundTruth chowned root:wheel and chmod go-rwx, the Application Firewall allows the
  venv python's real file (`socketfilterfw`), KiCad.app's framework python3 found by glob, --cuda ignored and
  `gpu_reason` explains (no CUDA, no float64 on Apple GPUs). `install_server.command` (bash, sudo, LF, 0755 in the
  zip: make_server_package's `zip_entry` sets external_attr and create_system 3: written on Windows with creator
  MS-DOS, macOS's Archive Utility ignored the mode and the double-click was refused). **Python 3.11 or newer**:
  KiCad for macOS bundles Python 3.9 (a server on it ran, but every job failed importing the analysis:
  `itertools.pairwise`), so `kicad_python` on a Mac takes the first Python >= MIN_PYTHON among KiCad.app's,
  python.org's (/Library/Frameworks), Homebrew's and python3 (`mac_pythons`, `python_version` runs each), else stops
  with `too_old_text` (python.org's installer); `make_venv` remakes a venv from an older Python; `groundtruth serve`
  refuses to start below 3.11. A Mac server probes no GPU: `server.MAC_GPU_NOTE` (the Windows probe's advice,
  `--cuda` and a missing DLL, read like a broken install). Tested as `--dry-run --platform mac`
  (tests/test_server_setup.py) and on the user's M2 (2026-10-15).
- **Cluster, milestone 1: discovery and the lead** (2026-10-14, branch `cluster`; the user: "can we have multiple
  servers (cluster) to do the sim faster?", goal one Simulate faster, 4 or more servers; "the server should scan
  the local network for others and whoever is first to find another should become lead. Also, an optional option
  on startup should define a server as lead if needed"; plan in HANDOFF):
  - `remote/cluster.py`: UDP beacons every BEACON_S (5 s) to 255.255.255.255:BEACON_PORT (8766) and to the static
    peers (server.json "peers", for other subnets), JSON signed with HMAC-SHA256 over the sorted JSON with the
    access key (the key never travels), dropped when older than MAX_SKEW_S, our own, or unsigned; same key but
    another code_id: "foreign" (shown, never used). Peer table: heard within ALIVE_S (20 s). Election (`elect`,
    pure, called after every round and beacon): with peers and no claim around, claim (time); rank = (not forced,
    claim time, host, id), the best is the lead everywhere; a loser drops its claim; alone: standalone (a forced
    lead keeps its claim). Find servers: a signed "find" query with a nonce, answered unicast with a signed
    "answer" beacon carrying the nonce (no replay of answers). The socket is SO_EXCLUSIVEADDRUSE on Windows (no
    other process can bind the port and take the datagrams).
  - Trust: the peer's address is the datagram's source, not signed: a captured beacon could be resent from another
    address within MAX_SKEW_S. The table is for display and election only; milestone 2 must check over HTTP that a
    peer holds the key (a signed challenge) before sending it any board.
  - `ComputeServer(beacon_port=0, lead, peers)`: the cluster is off unless `serve` turns it on (DEFAULTS
    beacon_port 8766; tests run many servers); a UDP port that can't be opened leaves it standalone (logged);
    /v1/status "cluster" {role, lead, members, other_versions, forced_lead, beacon_port} or {role standalone,
    discovery False}; `client.cluster_text` (Test connection), `server_setup.status_block` (Cluster lines).
  - `serve --lead/--no-lead --peers --beacon-port`; `server_setup install --key` (alone: asked with input(), shown as typed so a
    long key can be checked, `clean_key` strips quotes and a copied label; ≥16 characters; written to server.key;
    `key_check` lists the servers answering to it, cluster.find_servers), `--lead/--no-lead`, `--peers`, `--beacon-port`; the firewall
    rule gets a UDP rule for the beacon port (private, domain) under the same name (uninstall removes both).
  - **Addresses** (2026-10-15, the user: Find servers listed andre at 172.20.192.1, its Hyper-V/WSL vEthernet: Windows
    sent the answer out of the virtual adapter). Every server announces its real IPv4 addresses (beacon info
    `addresses`, server.local_addresses); `cluster.address_candidates(source, announced, own)` orders them: the
    source when announced or loopback, the announced ones on our subnet, the source when on our subnet, the other
    announced ones, the source; `Peer.address` = the first (`Peer.source` keeps the datagram's); the coordinator's
    `_peers` tries them in that order; Find servers checks them with a TCP connection (`reachable`).
  - Preferences **Find servers** (`PreferencesDialog.on_find` in a thread, `cluster.find_servers`, 2 s; the host
    name when it resolves to the answering address, else the address).
  - **History and the status** (2026-10-15, the user: "list the size of the cluster and each PC's name/IP ... show
    if an individual machine of the cluster disconnected at any time and how long it is connected"): `Cluster`
    keeps per server (`who` = host@address, which survives a restart, unlike the id) when its current connection
    began (`joined`), `together_since`, `lead_since`, and `events` (joined, lost with the time it had been in, back,
    restarted = a new id from the same host and address, lead with how it was chosen, alone; the last EVENTS_KEPT,
    each also logged). `settle()` (the loops' elect) notes lead changes. The status adds size, me (this server's
    info and start), members with joined_at and lead, static_peers, events, now; the server's beacon info adds its
    running and queued job counts. Shown by `server_setup.cluster_lines` (the last HISTORY_SHOWN events; list
    entries wrap under their text) and `client.cluster_text` (Test connection: size, lead, members' time, the last
    losses). In memory only: a server's restart starts its history anew.
  - Tests: tests/test_cluster.py (signing, election rules, forced lead, a gone lead, real UDP between two clusters
    and two ComputeServers, Find servers right/wrong key, installer dry run + key + settings), test_gui.py
    ::test_find_servers_lists_the_cluster_and_fills_the_address.
- **Cluster, milestone 2: one Simulate split over the servers** (2026-10-14, branch `cluster-split`):
  - Items and merge (`analysis/split.py`, no board needed): `split_items` (enabled nets, stages, bridges with a
    cost: a route per receiver 1, a stage 6, a bridge 6 per leg + 1), `plan_split` (longest first, to the server
    that finishes soonest by speed = min(cores, SPEED_CORES 8) / (1 + jobs there), then every idle server without an item gets one; cores alone sent both of the user's two stages to the 16-core lead), `merge_parts` (the checks of the part that ran them
    all; routes, stages, bridges sorted into the settings' order; notes once; solver devices joined; issues through
    `simulate.finish_issues`, the tail of `_simulate` shared with it: combine_dc, the stages' DC-path issues
    dropped when combined, dedupe in the single run's order, waivers, sort).
  - Parts (`simulate(part={"checks", "job_nets"})`): only the part's items simulated; the checks part runs
    `run_checks` with the job's nets on the unfiltered settings; no combine_dc: `power.compact_dc_branch` turns each
    stage path's (model, branch currents) into (`DcBoard`, currents at its vias): layers, grids, thicknesses,
    pitch, the vias (index, name, radius, position) and a fingerprint (sha256 of rounded figures, stable across
    processes). `combine_dc` now works on DcBoards only (a whole-board model is turned into one,
    `dc_board_of`, cached on it), equal fingerprints = one model. `SimulationResult.part` keeps the issues by origin.
  - Coordinator (`remote/split.py` `SplitRun`, a thread per split job, no slot held: no deadlock between servers
    splitting over each other): the slot that picks a Simulate (not `whole`, cluster peers heard,
    `ComputeServer._may_split`) starts it and moves on. Items from a short "items" child (the server never
    unpickles a board); peers checked with a signed status (their key proven before a board goes out; the
    beacon's address alone is not trusted); fewer than two items, no peer answering, or all items on one server:
    the job goes back to the front of the queue marked `whole`. Each share is a "part" job (args: the job's input
    bytes unopened, the names, the checks flag): this server's at the front of its own queue (`submit(front=,
    parent=)`), the peers' through `client.Remote.submit` (CLIENT_KINDS lets "part" in). Followed every POLL_S:
    progress weighted by cost ("split over 2 servers: andre 40 %, DELL waiting"); done parts fetched and removed;
    a peer unreachable for LOST_S (120 s), or failing/cancelling its part, has the part re-run here (a note); a
    failure of this server's own part fails the job. Cancel cancels every part; a server stopping leaves the job
    queued and drops its local parts at restart (`Job.parent`), the peers' parts expire. All in: a "merge" child
    (`merge_parts`) makes the job's result.
  - Lead redirect: a member answers POST simulate (without `direct=1`) with 307 {redirect: the lead's address};
    `Remote.submit` checks the lead's signed status, submits there with direct=1 and becomes the lead's Remote
    (`_hand_over` stores `remote.host`, so the tracker follows the job at the lead); no answer: back to the member
    with direct=1.
  - Results agree with one run within the GMRES tolerance (the borrow plan depends on the items solved together).
  - **Capacity-aware plan** (2026-10-15, the user: "The idea of splitting the computation is to use any machine to make
    use of the CPUs and RAM. So you should scale the work load depending on the machines capabilities"; the first
    real split sent a 16x stage, ~9 GB, to the user's laptop with 2 GB free, its watchdog stopped it). An item can't
    be split further, so RAM decides where an item may go and speed how many: `split_items` gives each item
    `mem_gb` (GB_PER_MNODE 6 x its models' node budget, max_nodes x resolution, + the cavity budget for a net);
    `fits` = within FIT_SHARE 0.8 of a server's free memory (status free_gb; the lead: server_memory);
    `plan_split` assigns only to fitting servers (none fits: the one with most free memory), LPT by speed, idle
    servers get an item they fit. `remote/split.Stats` (data folder, split_stats.json) keeps per (board source,
    item, resolution) the measured peak memory (a part's peak_mb; a one-item part exactly, several: an upper bound)
    and per host the speed (cost units / s from parts of SPEED_MIN_S 60 s or more, smoothed); measured speeds are
    used when every server of the plan has one. A failed part with "memory" in its error: `record_too_big` (need >
    that server's free memory) and `_move` to the untried fitting server finishing it soonest (`_target`), also
    the lead's own part; nothing fits: the job fails saying so. When the lead itself fits nothing but a member does,
    the job goes there as a single part (note "run on X ... too little memory free"). The progress shows moves.
  - Tests: tests/test_split.py (items and plan; parts through protocol pickling merged = the whole run: issues,
    stage figures, combined DC maps and vias; different DC models not combined; two real servers: a Simulate sent
    to the member is split by the lead and equals one run, no jobs left; a member stopped mid-run has its part
    redone by the lead; cancel cancels the parts).
- Not yet: TLS, a `check_server` tool; mixed precision (proposed, waiting for the user's GPU timing); measured
  part times to refine the item costs.

## Decisions log (with the user)
- Python; Tier 1 rules + Tier 2 2D solver (not full-wave); CLI + YAML settings file + HTML first, then a **real
  KiCad plugin** (user request) on the **IPC API only**. SWIG is gone in KiCad 11. **KiCad 10 and later**
  only (user corrected "KiCad 9").
- Any copper layer count (user request). wxPython UI. Settings-file storage (not footprint fields). Markers and
  contours on User layers.
- CLI for **GitLab CI** (user request): exit codes 0/1/2, Code Quality + JUnit, baseline/waivers,
  `--refill-zones` via kicad-cli.
- Nets: manual list. Later auto-detection was added, which **must stay reviewable/editable** (user).
- Default severity thresholds approved by the user: no stitch via within 2 mm = error, loop > 2× ideal =
  error (1.25× warning).
- Round 3: M5 fix suggestions **and** plane-to-plane capacitance from the physical stackup
  (user: "we do know physical board stackup, use it").
- Round 3, second session (user: hook the capacitance into the board simulation, do all fix suggestions,
  check the implementation and improve performance):
  - two models (route + board-wide) instead of one huge grid, because LU fill with capacitance is 3D-like;
  - solving at real harmonics with margin-driven refinement;
  - the line-current model instead of V/Z0;
  - topology-preserving rasterisation, after a grid study on the user's board showed fragmented pours;
  - capacitors and fills are suggested but never placed automatically; vias can be placed after confirmation.
- Session 3 (user: the GUI must not hang; use several threads to speed up simulations; DC-DC converters at
  10 kHz–1 MHz). The user insisted copper pours must not be neglected: "All DCDC designs will have pours as the
  current in that area is very high". The results:
  - all slow work runs as worker-thread jobs, with Cancel;
  - a thread pool for the solves, with results independent of the thread count;
  - f_ref = max(f0, 1 MHz), the receiver bandwidth, and pours solved as copper;
  - a converter model (hot loop + capacitors + LISN), which is not a signal route;
  - conducted standards selectable (CISPR 32, CISPR 25, MIL-STD-461), because the user named no standard.
- Session 10 (2026-10-12/13):
  - board contours like a topographic map (round levels, value labels, index lines), because a User layer has one
    colour; then arrow widths by current density on the board (the user found them more useful than contours);
  - the expected input current from an energy balance with the parts' losses, shown beside the connector;
  - a lumped part spread over its whole pads (up to 8 parallel branches of n·Z per side): one node per side
    crowded Q601's current into one point and overstated the DC density (input path peak 113 → 63 A/mm²);
  - every stage's DC paths on one whole-board model and their currents added ("the currents are not added
    together"): the DC checks run on the total, since the copper carries all loads at once; the per-stage views
    stay. A whole-board heatmap for the other views was parked by the user.
