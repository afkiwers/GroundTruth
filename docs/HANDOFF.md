# GroundTruth handover

## Current state (2026-10-20, `dead-edges`, `dead-time`, `jobs-fetching`, `issues-filter`, `qs-band` and audit round 6 merged into `main`)

**Start here**, and for what is left to do: [NEXT_STEPS.md](NEXT_STEPS.md) (what is done, what waits on the user, what is open, with the commands; rewritten 2026-10-19, round 5 added 2026-10-20). Then the dated "Session 10, …" entries below (newest first), "Where things are", and
[../CLAUDE.md](../CLAUDE.md) (environment, rules, commands, real boards) and [ARCHITECTURE.md](ARCHITECTURE.md)
(physics, code). The user may continue on another Claude account: everything needed is in the repo; the session
scratchpad is gone (reusable scripts are in `tools/dev/`).

- **Branch `dead-edges` (2026-10-20, from `main` 2fa4aea, merged into `main` at the user's "yes"):** the user, after the LTC7890's DTCA / DTCB
  pins were explained ("What's DTCA and DTCB exactly?", "How would we do it if we had 2 different dead times or even 3
  or 4?"), to the offer of one dead time per edge with the soft edge's node swing: "do it".
  - Converters have `dead_rise` (low side off → high side on; the LTC7890's DTCA) and `dead_fall` (high side off →
    low side on; DTCB) instead of `dead_time`; YAML `dead_time:` still sets both (written so when they are equal), and
    `dead_time` in a from_schematic list links both. The Converters form has **Dead time, rise** / **Dead time,
    fall**.
  - Loss [A48]: the hard edge (a buck's rise, a boost's fall: `thermal.edge_dead_times`) is reverse conduction for its
    whole dead time at the ripple's valley; the soft edge at the peak, minus the node swing 2·Q_oss / I_peak
    (`reverse_conduction`; ΔI from the inductor's L, `ripple_pp`). Q_rr needs the hard edge's dead time
    (`power.recovery`).
  - GT words per edge: `dead_rise:20ns`, `dead1_fall:10ns`, `conv1_dead_rise:15ns` (`tags.DEAD`, `EDGES`,
    `dead_time_of(..., edge)`: the most specific word per edge; at each level the edge's word before the one for
    both).
  - kraft conv1 / conv2 with U303's `dead:20ns` (both edges): the fall's node swing 7.1 / 8.1 ns, Q302 0.466 → 0.396 W
    (103.9 → 102.2 °C), Q304 0.594 → 0.524 W (103.8 → 101.6 °C) (out/dead_time/thermal_edges.log).
  - After the merge, at the user's "add the new para to conv2 on the schematic. keep the same dead time": U303 in
    flux-2 has `GT = dead:20ns, conv2_dead_rise:20ns, conv2_dead_fall:20ns` (label_project.py; backup
    out/kraft_backup_2026-10-20b/; KiCad had re-saved the files since the first label, which only moved the field).
  - Tests: test_dead_time 13 (the soft-edge closed form 2·I·t − 2·Q_oss, a swing longer than the dead time,
    reverse_conduction, the edge mapping, per-edge GT words, the settings' short and long forms).
- **Branch `dead-time` (2026-10-20, from `main` 61ba6dc, merged into `main` at the user's "merge"):** the user, of the Converters tab's edge time:
  "is that the dead time?", "why aren't we looking at dead time as well?", then "do all of it" (dead time and V_SD in
  the losses, Q_rr for silicon, a validation test, kraft numbers) and "add a GT field ... to the IC on the schematic
  for each converter that contains the intended dead time".
  - Converters get `dead_time`, `v_sd`, `q_rr` (settings, the Converters form with ◆ marks, ranges). A synchronous
    rectifier FET conducts in reverse for both dead times: 2·t_dead·f_sw·I·V_SD, out of its channel's share
    (`thermal.part_losses`, `dead_time_values`; A48). Defaults with a note: 20 ns (an adaptive controller's: the
    LTC7890 with DTCA / DTCB on GND, as kraft has them: R314 / R315 0 Ω), V_SD 2 V GaN / 0.8 V Si. Q_rr: Q_rr·V·f_sw
    in the main switch, a triangle I_rr = √(2·Q_rr·di/dt) over t_rr added in phase to the hot loop's pulse spectrum
    (`power.recovery`, `pulse_harmonics(rec=)`, the band to the triangle's corner), its snap-off I_rr·√(L/C_oss) in
    the `hot_loop` issue, the Vds safety check and the fix advisor (A49, [Mohan 2003]).
  - Fields: the FETs' `Vsd` and new `Qrr` (the rectifier side's) fill v_sd / q_rr; the controller IC's GT field
    `dead:20ns` / `dead1:20ns` (its SW1 pin's channel) / `conv1_dead:20ns` (the stage labelled conv1) fills dead_time
    (`tags.dead_times`, `dead_time_of`, `stage_ident`; the controller from `loop.controller`, split out of
    find_loop). Times parse with their unit (`values.parse_quantity(..., "s")`: 20ns, 0.02us, "20 nanoseconds").
  - kraft conv1 / conv2 (out/dead_time/): Q302 0.152 → 0.466 W, Q304 0.281 → 0.594 W at 20 ns and Vsd 1.6 V (the
    EPC2306 field); efficiency conv1 99.12 → 99.01 %, conv2 95.44 → 94.89 %. Thermal (normal quality, both stages,
    `thermal_check.py`, logs thermal_dt0.log / thermal_dt20_fields.log): Q302 96.1 → 103.9 °C, Q304 95.5 → 103.8 °C
    (both now just under 105 °C), Q303 120.4 → 127.1 °C, Q301 119.1 → 125.2 °C (the shared copper); the mesh's
    stated uncertainty ±7 K. `thermal_check.py` now applies the schematic fields as the plugin and CLI do (it ran
    without: Vsd fell back to 2 V).
  - After the merge, at the user's "add the proper deadtime to the converter on the schematic": U303 in flux-2
    (`C:\Users\iwers\OneDrive\Dokumente\github\flux-2`, dcdc_gan.kicad_sch + flux-2.kicad_pcb) got `GT =
    dead:20ns` (tools/dev/label_project.py; backup out/kraft_backup_2026-10-20/). The datasheet itself could not be
    fetched (analog.com reset the connection); the 20 ns adaptive figure for DTCx on GND is from its "Dead Time
    Control" section as search results quote it. Worth checking against the PDF.
  - Tests: tests/test_dead_time.py (11: the closed forms, capacity's linear term, the spectrum against a numerical
    Fourier transform of the waveform, the GT words, the fields, the settings); test_thermal (2) pinned to
    dead_time 0, test_power's energy balance with the dead-time term.
- **Branch `jobs-fetching` (2026-10-20, from `main` abfede4, merged into `main` at the user's "merge"):** the user, of a server job that seemed gone
  after closing and reopening the plugin and KiCad: "That was a fluke, it simply takes some time to update. maybe worth
  adding a message of some sort saying server data is being fetched". The Server jobs tab has a line over its lists:
  "Asking the compute server(s) for the jobs' state…" until the tracker's first round is back, then "Checked with the
  compute server(s) at HH:MM:SS" (`SyncTracker.on_round` / `last_round`, `ServerJobsPanel.set_sync`,
  `MainFrame._jobs_synced`). Tests: test_job_store (2).
- **Branch `issues-filter` (2026-10-20, from `main` ef78116, merged into `main` at the user's "commit, merge"):** three of the user's asks.
  - "add a filter to enable/hide entries based on what Checks are selected": the Issues tab's **Checks…**
    (`IssuesPanel.choose_checks`, remembered in ui.json `issues_hidden`); rows keep their number in the whole list;
    Draw markers on board leaves hidden checks out (`kipy_sink.draw_issues(hidden=)`). Found on the way: markers were
    numbered over the non-waived issues only, so a waived issue earlier in the list shifted every later marker's
    number off the tab's; now each marker carries its issue's number in the whole list.
  - "how can I see which via is the busiest", then "do this": the busiest via (largest share on the map) gets a white
    ring and a white-edged label (`plots._carriers`); share labels are placed apart once the view is final
    (`plots._place_labels`, LABEL_SPOTS, leader lines); clicking the **busiest via** tile zooms the maps 6 mm around
    it (`HeatmapPanel.focus_busiest`, `theme.Tile.set_click`) and, live, selects it in KiCad (`on_map_mark`).
  - "add the current in A as well to that hover-over note": DC-type via markers carry `current_a` (power.dc_view,
    dcload, bridge RMS with "rms", sharper); `plots.carrier_text`: "…: 9 % of the current, 0.905 A".
  - Tests: test_gui (filter, marker numbers, the tile), test_plot_text (labels apart, ring, hover). GUI tests 80
    passed. Checked on kraft conv2's output path (`out/issues_filter/`).
- **Branch `qs-band` (2026-10-20, from `main` 9782b76, merged into `main` at the user's "merge"):** the user, of a 16x server job solving a buck's
  board-wide model at 1592 MHz: "does it actually make sense to consider such high frequencies?", "especially because
  we are using quasi-static methods", then "I like that idea" (an automatic validity marker plus a max-frequency
  setting). `simulate.quasi_static_limit` (f = c/(4·size·√εr), A47) marks the board-wide model's emissions above it
  `indicative` (note, issue texts, a grey band on the spectrum); `analysis.max_frequency: auto | <Hz>` cuts the band
  (`band_top`; the cut range hatched and said "not computed", never passed). kraft numbers unchanged; tests/test_band.py
  (6). Why 1592 MHz: 10/(π·2 ns), detection's GaN edge (10 ns → 318 MHz). Full suite on the branch: 1011 passed, 1 skipped
  (`out/qs_band/full_suite.log`). Installers built from 6f3f60b for the user to try in parallel (laptop, release key
  present): `out/dist/groundtruth-0.1.42074-6f3f60b-pcm.zip`, `groundtruth-server-0.1.42074-6f3f60b.zip` (+ .sig). Still open from the user: a compute-server
  job no longer listed after the plugin and KiCad were closed and reopened (not looked at yet).
- **Branch `audit-6` (2026-10-20, from `main` 8f40781, merged into `main` as 534b469 at the user's "merge it first, then run a full suite"; on the laptop `iwers`/xps13, 4 cores):** the
  user's brief "Independent Audit: Numerical Accuracy, Solver Convergence, and Adversarial Validation" (pasted): do the
  solvers solve the physics accurately and reliably. The record is [NUMERICS_AUDIT.md](NUMERICS_AUDIT.md);
  VALIDATION_AUDIT.md "Round 6" summarises it; NEXT_STEPS item 7 what it left.
  - Verified as shipped (no change): GMRES rtol 1e-5 (kraft conv1 and SPI_SCK rerun at 1e-10/1e-11: ≤ 0.007 dB),
    the thermal tolerances (1e-5 of the rise), the cross-section solver (≤ 0.3 % of Cohn for w/b 0.02-8, s/b
    0.01-1), DC bends / branches / tees (≤ 0.8 % at 1x against a sheet solve with GroundTruth's pad feed), the
    metamorphic properties (NA-1 to NA-5).
  - Fixed, each with a test that fails on `main`: ISSUE-25 a failed DC path or bridge-leg solve left out of loss,
    efficiency and heat silently (`dc_unsolved`, `rms_missing`, a `dc_unsolved` warning); ISSUE-26 the thermal CG (a
    stall read as settled; a reused AMG hierarchy out of budget crashed with ValueError); ISSUE-27 the peak density
    over 0.25 mm (`peak_density`, A45; the node value diverged at corners); ISSUE-28 the inductance kernel's near
    field and self term exact (`kernel.tape_mutual`; a plane pair's sheet inductance read 1.5 / 3.9 / 20 % low at
    d/dz = 5 / 10 / 20); ISSUE-29 the plugin marks results of an earlier board / settings OUT OF DATE and Thermal /
    Capacity refuse them (Thermal takes the current thermal settings); ISSUE-30 zero copper / dielectric thickness
    said (35 µm everywhere for copper). A46: the plane pairs' missing fringing capacitance (model uncertainty).
  - Fixed, found on the real board: ISSUE-32, the reported worst emission was an interpolation once
    `analysis.max_solves` was spent (the worst-harmonic solve was skipped): kraft SPI_SCK at 1x +13.2 dB reported,
    +8.4 dB with every harmonic solved on the same code; now +7.85 dB, equal to the sweep with this round's kernel
    (`WORST_EXTRA` solves past the budget, notes for what stays interpolated).
  - Documented, not fixed: ISSUE-31, a track narrower than a cell reads +1 to +2.5 % resistive within ~10° of an axis
    and up to ±3.6 % at some angles at 1x (converging: ≤ 0.8 % at 4x); round 5 had checked 0°, 30°, 45° only and its
    summary said "any angle" (corrected). `tools/dev/angle_scan.py` maps it.
  - New: tests/test_numerics_audit.py (40), a window test, refsolvers' fed-pad sheet solve and windowed density;
    tools/dev/tol_check.py, thermal_tol_check.py, adversarial_dc.py, metamorphic_dc.py, cavity_analytic_converge.py,
    stress_inputs.py, angle_scan.py. Outputs: `out/audit6/`.
  - Full suite on the branch (`out/audit6/full_suite.log`, 43 min): 1001 passed, 1 skipped, 4 failed: three window
    tests that pass alone (load), test_view3d's KCL check (ISSUE-32 moved its frequency; corrected to the triangle
    inequality). The GUI probe on kraft: 11 stalls over 150 ms against `main`'s 15 (the worst of each paging during a
    SuperLU solve); memory and time of a conv1 Simulate unchanged. On `main` after the merge: 1005 passed, 1 skipped,
    0 failed (`out/audit6/full_suite_main.log`). A shareable summary: the Claude Docs page "GroundTruth Numerical
    Audit (Round 6)" (https://claude.ai/code/artifact/fce9eea5-397e-4761-beb7-253a5ddbf2bd, private until shared).
- **Branch `audit-5` (2026-10-19 / 20, from `main`, merged into `main` as 5fee321 at the user's "update handover,
  then commit, merge"):** the user's brief
  "Second-Round Independent Physics and Engineering Validation" (pasted), with a track narrowed from 0.4 to 0.1 mm
  followed through every calculation, then the user's "fix all of it" (and "keep going") for what the audit had left
  open. The record is [PHYSICS_AUDIT.md](PHYSICS_AUDIT.md) (model-by-model audit, reference register, track-width
  report, issue log ISSUE-11 to -24, benchmarks, the real board, final assessment); VALIDATION_AUDIT.md "Round 5"
  summarises it.
  - Baseline: `main` 68b85af from a separate worktree, 901 passed, 5 skipped (`out/audit5/baseline_full_suite.log`).
  - Fixed (each with a test against an independent result): the PEEC grid's strip widths and node areas are measured
    on the copper (ISSUE-11: a track's R' read 0.43-1.81 × ρ/(w·t) by width, angle and grid position, now within
    1 %); the copper heat Thermal gets is the solved I²R (ISSUE-12: up to 2 × on angled tracks); the sustained density
    counts only the net's own copper (ISSUE-13); the temperature map shows each cell's hottest point (ISSUE-14: a
    0.1 mm track 15 % cool); copper pieces of one net that do not touch are never joined (ISSUE-15: a broken track was
    solved as whole); the skin effect is the exact one-sided slab [Dowell 1966] (ISSUE-16: up to +58 % at 1-10 MHz);
    plate capacitance through mixed dielectrics in series (ISSUE-17); White's Sutherland constants, IPC-2221's current
    fit and Incropera's edition documented (ISSUE-18).
  - Fixed at "fix all of it" (2026-10-20): Simulate reads the board again and reloads it when KiCad's copy changed
    (ISSUE-19, `MainFrame._check_board`, `Board.fingerprint`; a run starts ~0.9 s later on kraft, no stall:
    `out/gui_probe/audit5/`); Thermal's heat holds the via barrels, the hot loop's switching current and the
    inductor's ripple (ISSUE-20, A43); a narrow band's change of width is taken in series along its strips for the
    resistance (ISSUE-21: the 2 mm neck −2.9 % at 1x, was −9 %; `peec._series_factor`, `Grid.rx / ry`, the inductance
    keeps `wx / wy`); a DC node's heat goes into its own copper (ISSUE-22: at fine quality an edge node's heat had made
    a hot spot 36 % over a track, `peec._heat_points`, A44); after Thermal every DC path at its temperature
    (ISSUE-23, `thermal.dc_at_temperature`, a note, and the CLI's JSON report now has `thermal` / `capacity`).
    Documented, not changed: normal quality's mesh budget on long narrow hot tracks (ISSUE-24; refining only the hot
    blocks read kraft's hottest parts 1.5-3 K cooler than fine, so the planner stays). REFERENCES: Dowell 1966,
    Foley 1990, Kreyszig 2011 added; A2 / A3 / A10 / A27 revised; A39-A44 new; the three references nobody could open
    (CISPR 25's average column and edition, IPC-2221's §6.2 text, Incropera's face-down coefficient) tried again and
    recorded as unverified (A10's peak limits checked against TI SLLA569B).
  - Thermal, validated: with the right heat, a long 0.075-0.4 mm track's temperature is within 2 % of an independent
    2D cross-section (`tests/refsolvers.py`), its current capacity within 2.4 %. 0.4 → 0.1 mm at 1 A: R, drop, loss,
    J × 4; the rise × 5.06 (heat × 4, the narrow track's spreading + 16 %, ρ(T) + 9 %); the capacity ÷ 2.1.
  - New: tests/test_track_width.py (TW-1 to -15), tests/test_physics_audit.py (PA-1 to -4), tests/refsolvers.py, a
    window test (`test_simulate_reads_a_board_changed_since_it_was_loaded`); tools/dev/track_width_check.py (the
    sweep, `--src` for another tree), track_capacity_check.py, dc_thermal_summary.py (`--quality`, `--refine-ring`),
    topology_diff.py. Outputs in `out/audit5/`.
  - On kraft_flux_mini_2 (`Downloads\flux-2`, main against this branch, PHYSICS_AUDIT.md section 7): the same
    pads joined (351, 0 differences); the stages' DC paths within −0.4 to −4.5 % (conv4's switch node +25 %: its R
    is the loss's, with the inductor's 0.18 A p-p ripple on 0.1 A), a hot-loop path per stage (conv1 16 mW);
    hottest copper 124.27 → 124.54 °C, copper heat at 20 °C 537 → 558 mW (now equal to the combined DC view's loss);
    the paths at temperature +20 to +29 % copper resistance (conv1's output 5.63 → 6.04 mΩ at 84 °C, drop 56 → 60
    mV). Emission margins within ±0.8 dB below 1 GHz; above it the 1x shifts (+1.5 to +8.9 dB) are the 1x grid's own
    error: at 2x the two trees agree within 0.4-1.5 dB (`out/audit5/run_converge.sh`).
  - Full suite on the branch: 960 passed, 5 skipped (two timing-sensitive tests failed while kraft ran beside the
    suite and passed alone); the quality gates clean.
  - Open from it (NEXT_STEPS "Open, technical"): normal quality's thermal mesh budget on long narrow hot tracks
    (ISSUE-24: fine quality; a planner refining only the hot blocks was tried and not kept), a width step in copper
    wider than 4 cells or at a junction (within ±d/2, A39), the references nobody could open (CISPR 25's average
    column and edition, IPC-2221's §6.2 text, Incropera 7th edition's face-down coefficient). Not run here: the board
    check against a live KiCad (the read it does is Reload board's). The plugin in KiCad is still the PCM one from
    before this round (no release key on this machine: NEXT_STEPS item 1).
  - To compare with the code before the round: `git worktree add <dir> 68b85af` and the tools' `--src <dir>/src`
    (the session's worktree was in its scratchpad and is gone).

- **Branch `fem-notes` (2026-10-19, from `main`, merged at the user's "commit and merge changes"):** the user asked
  for a FEM pathway beside PEEC (benefits, work, drawbacks, a switch between the two), then: "write this down under
  docs as potential FEM option ... I don't think it's worth right now but might be in the future". The findings and
  a plan are in [FEM_OPTION.md](FEM_OPTION.md) (NEXT_STEPS item 5); the spike is `tools/dev/fem_spike.py` (NGSolve in
  a venv of its own, never the project's). Nothing in `src/` changed. Open from it: LICENSE says GPLv3, `pyproject.toml`
  MIT (the user: use the license that benefits the functionality most; FEM_OPTION.md recommends GPL-3.0-or-later).
- **Branch `memory-ladder` (2026-10-19, from `main`, merged into `main` as 3171856 at the user's "Merge"):** an 8x Simulate of CE2264 on DELL-7920-PC failed
  after 82 min at a board-wide solve ("buck Avionics and Battery Charging/SW1", 243k nodes, solve 130 of 151), peak
  21.8 GB, 45 GB free, reported as "the thermal solve ran out of memory". The user: "this keeps happening! It should
  not fail, it should recognise the shortage and proactive work around it to finish the simulation!" The worker's
  traceback was dropped, so which allocation failed is not known; most likely a SuperLU growth step past the free
  commit, and the "retry alone" kept the model's cached factorisations. Now (ARCHITECTURE "Working around a
  shortage", REFERENCES A38): the memory gate before every factorisation (`peec.GATE`, measured fill
  `lu_nnz`/`lu_bytes`), the ladder (`simulate.MemoryRelief`: freed caches, least fill, the frequency left unsolved),
  threads re-planned from the measured fill and halved after shortages, the worker told its limit
  (`sysmem.LIMIT_ENV`), the server's message and traceback fixed. `tests/test_memory_relief.py`. **Not yet run on the
  real board** (it is on the user's machine, not here): the next step is the user's 8x CE2264 Simulate on the server
  with this build; its notes say what the run had to do.
  Also on this branch, the user: "remove the labels from the report. they are actually not helping": the report's
  maps have no text boxes (via shares, pin letters, switch names; `draw_heatmap(labels=False)`), the plugin's keep
  them.
- **Branch `job-version` (2026-10-19, from `main`, merged into `main` as 8cce357 at the user's "merge"):** the user: "add the server's plugin version to the
  table so users can see what plugin version was used for the sim", "add the plugin version to the report as well",
  "like the full one GroundTruth 0.1.41910 (9094c40, built 2026-10-19 12:05)". The server's job answers carry its
  build (`server.server_build`), kept as `server_version` in the job's entry: a **Version** column on the Server jobs
  tab (and in the details; "–" for older jobs and servers). `gpu.Run.summary()` records the build that solved
  (`solver["build"]`, kept through `split.merge_parts`), so the report's cover shows **Version** in full (the server's
  build for a server run) and the footer the build that wrote the page; the history's run meta has it too.
- **Branch `open-items` (2026-10-19, from `main`, merged into `main` as ec36b1f at the user's "merge and push"):** the user: "finish those -> Open technical items"
  (NEXT_STEPS rows 9-13; VALIDATION_AUDIT round 4 "Open items after round 4" and "SP-19 at 16x").
  - 16x (493 303 nodes, 1-1.5 GHz, ~3 h): the band's worst margin +14.1 / +17.6 / +15.8 dB at 4x / 8x / 16x, so the
    grid note now asks for 4x everywhere and gives ±2 dB above 1 GHz (`GRID_CHECK_F`, `GRID_ABOVE_DB`).
  - Interleaving: CE101 combined per group; end-to-end tests on a 2 kHz pair's CE101 and on two boosts.
  - GPU: `DeviceTooSmall` before uploading a model larger than 80 % of the free device memory.
  - A36: the return plane's loss from Johnson's current spread (`emi/path.plane_resistance`, closed form within
    0.1 % of the integral): kraft ADC_I_RAIL_1 +30.1, ADC_I_RAIL_2 +33.6, the SPI nets +0.3 dB over lossless.
  - README: the safety figures and the PDN grid comparison from the current board.
  - CLAUDE.md: the remote is `origin` (the rule not to push without asking stays).
  - Full suite (`out/full_suite_open_items.log`, 29 min): 892 passed, 5 skipped, 1 failed: test_remote_update's
    two-networks test assumed 192.168.0.6 is not one of the test PC's addresses; it is the user's (on a /30 since
    today, so its broadcast is .7, correctly). The test now uses a documentation address (192.0.2.6): 12 passed.
- **Branch `analog-defaults` (2026-10-19, from `main`, merged into `main` as 33d43ad at the user's "Yes"):** the user asked how useful the MHz-GHz
  results are for a signal of a few kHz (Net-(U301--)): not at all; it ran on detection's generic 10 MHz / 2 ns.
  The user: "use that information otherwise keep 10MHz". `detect._amplifier_signal` [A37]: a net with no class from
  its names, driven by an op-amp output with a slew rate or bandwidth, gets edge max(0.8·3.3 V / SR, 0.35 / BW) and
  frequency min(10 MHz, BW, SR / (π·3.3 V)); kraft's Net-(U301--) 5.28 µs at 48.2 kHz. The ADC paths start at the
  ZXCT1107 (an `open` model, no data) and keep the default; VALIDATION_AUDIT round 4 says what their numbers mean.
- **Branch `path-loss-gpu` (2026-10-19, from `main`, merged into `main` as 2a93735 at the user's "Yes"):** the user: "Fix those" (NEXT_STEPS items 6 and 7;
  VALIDATION_AUDIT Round 4 has the evidence).
  - Item 6: the signal path's lines are lossy (`emi/path.PathLine.alpha`, `simulate._line_loss`, REFERENCES A36):
    kraft's ADC_I_RAIL_1 +21.4 → +33.5 dB, ADC_I_RAIL_2 +35.4, the SPI nets +0.4 dB (with the plane's loss from
    Johnson's spread, branch `open-items`: +30.1, +33.6, +0.3 dB); the converters and the
    issue counts unchanged. Net-(U301--) stays +22.1 dB (a plane resonance at 1590 MHz, not its path).
  - Item 7: `gpu: auto` times the solver's convolution on the GPU against the CPU and uses it only from 1.5x (the
    RTX 3050 here: 0.3-0.8x, so auto keeps to the CPU and says why); one process at a time holds a GPU, the lock
    taken before anything runs on it (the speed check too). test_split's lead-split test passes with the GPU
    visible; `tools/dev/gpu_contention.py` measures CPU vs GPU, alone and in N processes.
  - The path rows in the report carry each line's width, foil, εr and tan δ; SP-9's independent ladder has the same
    losses as R' and G' per cell and agrees within 1 %.
  - Full suite with the GPU visible (`out/full_suite_gpu.log`, 31 min): 881 passed, 5 skipped, 1 failed (SP-9, its
    reference still lossless; fixed: test_validation_audit + test_path 132 passed, history and report 17 passed).
- **Branch `next-steps` (2026-10-18, from `main`, merged into `main` as 7013642 at the user's "Yes"; on the bigger machine, see CLAUDE.md "Environment"):**
  the user: "read docs/NEXT_STEPS.md and finish the work"; VALIDATION_AUDIT "Round 4" has the evidence.
  - The move: `out/` copied by the user, the venv rebuilt (python.org 3.13, the constraints file, PyTorch cu118 for
    the RTX 3050). The kraft board here is the user's copy `Downloads\flux\flux\flux.kicad_pcb`, **dated
    2026-10-14**: it has the GT and ADC labels but not U303's controller fields (10-17), and a few GND vias differ
    from the board round 2 used. Fine for the convergence work (1x reproduces round 3's numbers to 0.01). The
    current board is `Downloads\flux-2\flux-2.kicad_pcb` (2026-10-17, U303's fields; it reproduces round 2 where
    the code did not change): item 4 and every published number come from it.
  - Item 1 (SP-19): the 8x run. 4x vs 8x 0.8 dB rms below 1 GHz (converged), 2.9 dB above. The grid note asks for
    4x / 8x (`GRID_CHECK_8X_F`). 8x needed `peec.GMRES_RESTART_MIN` = 100: with 400 MB of Krylov basis the restart
    was ~26 at ~1 M unknowns and stalled; a plugin Simulate at 8x builds the same model. `cavity_converge.py` and
    `cavity_hotspots.py` got `--gmres-mb`.
  - Item 2: a finer copper sampling (0.075 mm, 12 × 12) does not narrow the neck's ±4 % scatter: `RASTER_MM` stays.
  - Item 3: `_line_of` tries three places on the three longest tracks: kraft's U301- had no cut; now none falls back.
  - Item 5, at the user's "Yes, build it": interleaved stages (`analysis/interleave.py`, ARCHITECTURE "Interleaved
    stages", help "Interleaved stages", REFERENCES A35): `interleave` + `phase_deg` per stage (form fields on the
    Converters tab), detection proposes a group per IC, the group's conducted and radiated sums on each member, its
    own issues. On kraft: conv1 0° / conv2 180° ("U303 phases"), conducted −38.6 dB together (−39.6 in phase),
    radiated −21.6 dB together (−25.4 in phase, −19.8 / −21.0 alone).
  - Item 4 (`out/kraft_refresh/now2/`, flux-2, 18 min on the CPU; VALIDATION_AUDIT's table under Round 2): the SPI
    margins +11.4 / +14.8 / +15.4 dB (2-3 dB lower than round 2), SW_CLK +40.5, the converters within 0.2 dB, the
    hot loops +6 %, conv1's loop unchanged (11.2 kHz, 77°, 20.2 dB). Two moved by 13-24 dB, both already on `main`:
    Net-(U301--) and /CPU/ADC_I_RAIL_1. The latter is a lossless line at a resonance: its 88 mm line into 5 pF is a
    quarter wave at 310 MHz and C304's ESL resonates with it, so the 7 % Z0 change of the field-solved cut (38.6 →
    36.0 Ω) took C304's share from 0.02 to 4.5 × the input current. **New next step:** loss in the path model's
    lines (copper and tan δ), so such peaks are damped as on a real board (NEXT_STEPS item 6).
  - `gpu: auto` on the 4 GB RTX 3050: the full kraft Simulate ran > 2 h on the GPU (memory full, 99 %) and 18 min
    on the CPU (`--gpu off`). Worse, `test_split.py::test_a_simulate_sent_to_a_member_is_split_by_the_lead_and_
    equals_one_run` never finishes with the GPU visible (> 4 h; its two split-part worker processes both solve on the
    one GPU), on `main` too, and passes in 36 s with `CUDA_VISIBLE_DEVICES=""`. The laptop had no usable GPU, so the
    suite never ran this path. The full suite here runs with the GPU hidden; NEXT_STEPS item 7.
  - Also: the Converters form's Update kept everything the form does not show except the control-loop overrides
    (`loop`): it keeps them now. The server's GPU probe gave no reason when CUDA loads but shows no device
    (`CUDA_VISIBLE_DEVICES` empty): it says so now.
  - Full suite with `CUDA_VISIBLE_DEVICES=""` (`out/full_suite.log`, 30 min): **876 passed, 4 skipped, 1 failed**,
    the failure being that probe gap (fixed, `tests/test_remote.py` then 24 passed). `tests/test_gpu.py` with the
    GPU visible: 6 passed, 2 skipped. undocumented 0, py311 0 rejected, ref_keys 103 / 103.

- **Branch `mil461h` (2026-10-18, from `main`, merged into `main` at the user's "merge what can be merged before I copy"):** the user: "Add both, let the user decide which
  revision". `analysis.mil461: G | H` (default G).
  - Read from both standards (the PDFs in `Downloads\GroundTruth audit sources`): only CE101 differs for the code.
    461H's 5.4.1 takes ships and submarines to 20 kHz (aircraft stay at 10 kHz), and CE101-1 goes on to 66 dBµA at
    20 kHz on the same line. Its relaxation is "the lesser of 20 log(I/3) or 35 dB"; 461G's is 20 log(I/3) to
    185 A, then 35 dB.
  - Code: `Ce101.revision`, `.range`, `ships_dc_h`; `power.ce101_transfer`, `bridge._ce101_complex_transfer` (the
    CE102 band's solves from 10 kHz [A34]); two more conducted solves at 14.1 and 20 kHz for ships under H.
  - Tests: the limit against both figures, the setting, a 12 kHz converter read only under H (the lead current at
    14.1 / 20 kHz against the analytic C-against-LISN divider within 5 %), a bridge's rows to 20 kHz.
  - Still waiting on the user: whether to model the combined emission of interleaved stages.
- **Branch `small-fixes` (2026-10-18, from `references`; both merged into `main` as 6e5f00a at the user's "Merge"):** at the user's "keep going".
  - The Mac launcher is committed executable, which fixes `test_the_mac_launcher_is_lf_and_executable`.
  - The grid note says a coarse grid moves a resonance's frequency too, not only its height.
  - README: the plane-displacement example is the current one (F.Cu/In1 by C1105/C1110 at 1020 MHz, 4.6×). The
    740 MHz cavity it quoted no longer appears.
  - Every model samples its copper at least 2 × 2 per cell (`simulate`, `sub = max(2, …)`), so cells of 0.15 mm
    and less carry strip widths too. The neck test case at 0.15 / 0.1 mm: 793 / 735 → 769 / 742 MHz. On kraft
    this only reaches 4x and up.
  - The user answered the 461H question with "Add both" (branch `mil461h`); interleaving is still open.
- **Branch `references` (2026-10-18, merged into `main` with `small-fixes` as 6e5f00a):** at the user's "add references of the
  assumptions/methods/theorems, also call them out locally where they are used, in all calculations".
  - [REFERENCES.md](REFERENCES.md) lists every source by key (68 sources: electromagnetics, lines and EMC, power
    electronics, heat transfer, numerics, standards by edition) and the model's assumptions A1–A33, each with its
    reason and evidence.
  - The code cites the keys where it applies them: solver, emi, analysis, components, rules, thermal, and the
    tests that check against exact results.
  - Rules the list follows: a source is cited only for what it contains; what has no published source is an
    assumption. Locations: the MIL-STD-461G ones were checked against the standard; the few book sections are the
    usual ones for those editions but were not re-read (the list says which); Wadell's section numbers were dropped.
  - Checks: `tools/dev/ast_same.py main` reports only comments and docstrings changed (35 files);
    `tools/dev/ref_keys.py` (new) finds 0 unknown keys and every listed key cited. CLAUDE.md has the rule for new
    code.
  - The user: "add a link to the help inside the plugin that links to the references.md so users can view it from
    there". The help has a section **References and assumptions** (`#references`, under "Under the hood"). It is
    REFERENCES.md rendered by `help.markdown_html` (headings, lists, tables, inline marks), so it reads in both help
    views and offline. A link opens the file itself. The methods section, the limits section and the contents link
    to it.
    - The package and the development install ship the file as `<plugin>/docs/REFERENCES.md`
      (`make_pcm_package.EXTRA`, `install_plugin.install`); `help.REFERENCES` looks there and in the repo's docs.
    - Test: `test_the_help_shows_the_references_with_every_source_and_assumption` (every key in the section), plus
      the package and install tests.
- **Branch `audit-3` (2026-10-18, merged into `main` as c3dbeb6 at the user's "Merge"):** the audit's round 3 (VALIDATION_AUDIT.md "Round 3"),
  at the user's four items:
  - "Fix this" (inner-layer Z0, solder mask): a path's line is the field-solved cut at its longest track
    (`simulate.line_cache`, `si.SectionCache`), with the closed forms as the fallback (SP-26). On kraft, the cut reads
    43.0-43.6 Ω outer and 35.6-41.7 Ω inner.
  - "Is that a bad thing?" (interleaved phases): no, not for DC or thermal. The switch-node variance stays in each
    node's own copper, so the cross term is exactly 0.
  - "Try to fix this" (resonances above 1 GHz): strips are as wide as their copper (`Grid.wx/wy/ox/oy`,
    `PlaneStack._widths`, `_narrow_self`, `_side_mutual`, `M_near`, CPU and GPU), and densities and heat follow
    (`node_field(copper=True)`, `Grid.copper_area`); SP-27.
    - The neck test case converges: 797-717 MHz where it was 722 to 900+.
    - On kraft above 1 GHz, the grid-to-grid rms went from 7.0 to 4.0 dB (1x vs 2x) and from 5.3 to 4.4 dB
      (2x vs 4x). The largest mode, F.Cu/In1 by C1105/C1110, is at 940 / 1020 / 1020 MHz at 1x / 2x / 4x (it was at
      1060 / 1100 / 1060). So 2x and 4x agree on it, and 1x places it 8 % low, inside 0.5-1 GHz.
    - The user chose "commit, keep digging": `GRID_SENSITIVE_F` is now 600 MHz.
    - **Open:** 2x and 4x still differ by several dB above 1 GHz (4x has a peak at 1260-1300 MHz). The sub-grid for
      cells ≤ 0.15 mm followed on `small-fixes` (2 × 2 at least: 769 / 742 MHz at 0.15 / 0.1 mm). Logs are
      in `out/converge/spi_sck_head_*`, `spi_sck_widths*` and `hot_new_*`.
  - The MIL-STD-461 sources are downloaded to `Downloads\GroundTruth audit sources`, including 461H (2026-04-17,
    supersedes 461G): RE102 aircraft is split into `re102_aircraft_external` (2 MHz-18 GHz), `re102_helicopter` and
    `re102_space` (10 kHz-18 GHz). CE101 to 20 kHz (461H) followed on branch `mil461h`.
  - Full suite before the last small edits: 859 passed, 1 skipped, 4 failed. Those were 3 GUI timing tests on the
    loaded machine and the launcher's git mode.
  - **Next, the user's request:** "add references of the assumptions/methods/theorems, also call them out locally
    where they are used, in all calculations".
- **Branch `audit-2` (2026-10-18, merged into `main` as c5c313e at the user's "Merge"):** the audit's round 2, at the user's "continue with fixing the other
  issues as well a continue your audit" (VALIDATION_AUDIT.md "Round 2"): the venv rebuilt in place (it had been
  moved from `PCB-EMI\.venv`; the editable install and every launcher pointed there); ISSUE-7 fixed (the switch
  node's DC path is RMS: a second DC run per stage, `dc_ripple`); ISSUE-9 (the MIL-461 LISN's 1 kΩ port resistor)
  and ISSUE-10 (the spread receiver's tuning search, 0.5-0.7 dB optimistic on wide sweeps) found and fixed; the boost
  loop checked against a cycle-by-cycle switching simulation, MIL-STD-461G's CE101 / CE102 / RE102 / LISN / Table II
  against the standard itself, the Tier 1 rules on constructed geometry. kraft_flux_mini_2 simulated with the old and
  new code: SPI margins ~1 dB lower, conv1's switch-node copper +42 %. Full suite: 852 passed, 1 skipped, 3 failed
  (the launcher's git mode; two GUI timing tests that pass alone).
- **Branch `validation-audit` (2026-10-17, merged into `main` as f171f99 at the user's "Merge and commit"):** the user's engineering validation of
  Signal paths, Converters and DC loads. The record, matrix, issue log and evidence are in
  [VALIDATION_AUDIT.md](VALIDATION_AUDIT.md), and the independent checks in `tests/test_validation_audit.py`
  (75 cases).
  - Fixed: a boost's losses / input current at D ≠ ½ (ISSUE-1); every path's line Z0 (closed forms out of range,
    centre-to-centre heights, centred inner layers: kraft read +21 % outer / +52 % inner; ISSUE-5); impossible net
    signals and contradictory converter voltages refused instead of crashing (ISSUE-8); a DC-load pin named twice
    (ISSUE-4); the boost's Middlebrook check (ISSUE-2); `current_a` = the fundamental (ISSUE-6); a boost without
    V_out (ISSUE-3).
  - Documented, not fixed: the switch node's averaged DC path reads its copper heat 6-17 % low (ISSUE-7).
  - The project now lives in `OneDrive\Dokumente\github\GroundTruth`, a fresh GitHub repo whose first commit of
    the files is fae19c9 (the audit is f171f99). The commits named below are
    in the old history, `OneDrive\Dokumente\PCB-EMI\.git`. The venv's editable install still points at
    `PCB-EMI\src` (pytest uses `src` from pyproject). Full suite on the branch: 835 passed, 1 skipped, 3 failed
    (not from the audit: the launcher's git mode, and two cluster-split tests that pass when run alone).
- **`main`** (2026-10-16) holds everything (last: `remote-update`, signed remote updates of the servers, merged
  6b6c707 at the user's "yes"; then `modern-help`, a modern help window, at the user's "merge"; then
  `server-address`, a server's chosen address, at the user's "do it"); no unmerged branches, no stashes, no extra
  worktrees. Merge history of
  session 10: (server-service and view-list-width merged 2026-10-14 at the user's "merge and install") (cluster milestone 1 merged b399ef1; **cluster-split, milestone 2, merged 9190ff3 on 2026-10-15** at the user's "merge and make a new installer and install locally", with the Server jobs tab's Resolution column and the help's "How the simulation works" section; **`server-jobs-all`** merged right after at the user's "merge everything", its waited-for jobs given the resolution too; then `server-status-paths`: a Path entry in the server's status block, where the install's program, Python, settings and logs are, the user: "add a path entry that shows where the binaries are stored on the PC"; then `cluster-status` 54ae76c: the cluster's status and history, the key shown as typed, every idle server gets an item of a split, why a Simulate ran whole, install_server.cmd log; then `split-capacity` 739284d: the split by each machine's memory and speed, learned from earlier runs; then dc3a981: `server-addresses` (real addresses, resizable details), `all-cores` (opt-in Use all cores), `report-images` (full-resolution report images); then `lu-size` 9da0da3: SuperLU's size limit handled; then 6c3717d: `dc-loads` (DC rails with loads) and `tht-pins` (through-hole pins joined around their barrel)). Then, at the user's "merge everything" (2026-10-15): `dcload-picker` (the DC
  loads tab's Fill from net…), `server-macos` (the compute server on macOS) and `mac-python` (its Python 3.11+ fix),
  main 1f6ac0b; then `remove-unreachable-job` and `pin-complete` at the user's "merge" (window-only: code
  cff05acc0eee unchanged, the servers need no update).
  Full suite on `main` 57602a8 (2026-10-16, 53 min): **635 passed, 1 skipped, 2 failed**, both since
  fixed in tests/test_gui.py and passing alone: the tab list lacked the DC loads tab (and its build step), and
  the Thermal button's 0.5 s GUI timing ran 0.6 s while kraft Simulates loaded the machine. An earlier run hung:
  the every-button test's Fill from net… opened real Single/MultiChoiceDialogs (now answered Cancel too). GUI stall-timing tests can fail on a loaded machine (a 5 s
  cold-start stall once, 0.5-0.9 s under load): rerun them alone.
- **Installers** (`out/dist/`, rebuild after any code change): the KiCad package `tools/make_pcm_package.py`
  (`groundtruth-<ver>-<commit>-pcm.zip`, KiCad: Plugin and Content Manager > Install from File; its
  plugins/tools/ carries the GPU support for a plugin on a PC with a graphics card: install_gpu.py, check_gpu.py,
  install_gpu_amd.cmd, GPU_README.md) and the server package `tools/make_server_package.py`
  (`groundtruth-server-<ver>-<commit>.zip`: install_server.cmd on Windows, install_server.command on a Mac;
  `--cuda cu126` installs PyTorch on a Windows server). The separate GPU package was dropped on 2026-10-14 ("remove
  the GPU addon"). **Current: main 57602a8, built 2026-10-16** (`groundtruth-0.1.41452-57602a8-pcm.zip`,
  `groundtruth-server-0.1.41452-57602a8.zip`), code fingerprint `cff05acc0eee` (protocol.code_id: every server
  and plugin of one cluster need the same). `main` 17adb8e (through-hole pin values) is `33dfd148bd4d`: no installer of it
  yet.
- **Branch `bode` (merged 2027843 on 2026-10-17 at the user's "Yes")**, from main c41daae:
  - M4 (the user: "can we add bode and phase plots as well as pole zero plots?"): `analysis/transfer.py` (vector
    fitting, poles / zeros), responses of signal paths, power stages (ringing exact, input transfer fitted) and PDN
    rails, the Heatmap tab's "Bode / poles and zeros" views, the report; ARCHITECTURE "Frequency responses". kraft:
    conv1's ringing 167 MHz Q 12.4 (exact), its input transfer fitted ±1.8 dB, +3V3's ICs ±0.2 dB; the ADC path's
    pole at 84.8 kHz comes from the default 25 Ω driver against C304 (the ZXCT1107 is a current output: a high
    driver_ohms would put it near 160 Hz; told the user). Left from the plan then (the fix advisor's proposed input
    filter as a response (Middlebrook), a phase panel in draw_pdn): done on `control-loop`.
  - The user's "Do it" (bootstrap nets) and "Fix this" (the GUI items), also on this branch: voltage ranges and riding
    nets in safety.py (ARCHITECTURE "Voltage ranges"; kraft now 42 errors / 13 warnings, C601 50 V at ~62 V); the
    Converters tab's spread fields (+ the stage stays selected after Update); the board load fills the tabs one per
    event and preloads analysis.pdn / si / transfer and ui.pin_complete in its worker; the Power integrity, Signal
    integrity and Converters tabs build in steps (every step < 100 ms, build_profile.py; the tab test counts 30).
    gui_probe on kraft copies: one net and the +3V3 PDN, cached: every tab switch after Simulate 0 ms (the
    330-410 ms of M1's probe no longer seen); left: a 312 ms stall when the status bar grows, a 199 ms generation-2
    garbage collection during a redraw (the kept models, MainFrame._last_sim, are many Python objects; freezing
    them would leak when they are dropped: not done), and the load's 155 ms (no sample). Full suite on ac300cf:
    745 passed, 1 skipped.
- **Branch `control-loop` (M5, merged 7074837 on 2026-10-17 at the user's "update docs and handoff, commit and merge, make installers"; full suite on 3a2a3c6: 762 passed, 1 skipped)**, at the user's "Go for it": `analysis/loop.py` (a buck's
  and, at the user's "Add boost support too", a boost's loop gain: parts from the board, the controller's figures
  from its InvenTree symbol fields, which the user asked for: "Those parameters could come straight from the
  schematic"); tests/test_loop.py (11). See ARCHITECTURE "Control loop". Full suite on 4bb6f9b (before the boost):
  752 passed, 1 skipped; on a1b8888 (boost, M4 leftovers): 758 passed, 1 skipped.
  - **kraft's schematic and board changed in place** at the user's "add those yourself" (KiCad closed first at the
    user's "Close kicad": CloseMainWindow, not a kill): U303 Control Mode peak current, Vref 0.8V, EA gm 1.8mmho,
    Vsense Max 50mV (ILIM floating), ITH Range 1V (confirmed from the datasheet's Figure 13: 0 mV at 0.4 V, 50 mV at 1.4 V; the PDF from
    web.archive.org, see ARCHITECTURE "Control loop"; no slope-compensation figure in it, so half the down-slope
    stays assumed); U502 Control Mode COT, U503 internal. Backup
    `out/kraft_backup_2026-10-17/` (dcdc_gan.kicad_sch, psu.kicad_sch, the .kicad_pcb). kicad-cli reads both.
    `tools/dev/label_project.py --field NAME` (new) wrote them. Results (`tools/dev/loop_check.py`, new; settings
    `out/m5/kraft.groundtruth.yaml` from real_settings.py): conv1 11.2 kHz / 77° / 22.6 dB, conv2 29 kHz / 91° /
    20 dB.
  - The M4 leftovers, at the user's "fix it": the fix advisor's proposed filter as responses (attenuation, and
    Middlebrook's two impedances in a "<stage> impedances" view: `transfer.unit_groups`), and draw_pdn's phase
    panel. See ARCHITECTURE "Frequency responses".
  - Window stalls, at the user's "Fix outstanding issues" (ARCHITECTURE "Plugin": Idle-time work, Garbage
    collection, the gui_probe table): gui_probe on kraft (out/m5 settings) found 39 stalls over 150 ms, worst
    952 ms, every tab switch 250-500 ms. Cause: the History tab's report page drawn right after each Simulate
    (25-50 matplotlib plots holding the GIL; the probe's new per-thread CPU column showed it) and full garbage
    collections walking the results. Explained to the user, who chose "go with a": `ui/idle.py` (the system's
    last-input age; the page's plots wait for QUIET_S = 3 s without input), `gil.settle_garbage` (one collection
    once the user is idle after new results, then gc.freeze). Now 7 stalls, worst 280 ms, tabs 0 ms. Left: some
    145-167 ms full collections during view switches; a view's maps take 150-300 ms to draw (not asked about).
    gui_probe: --cpu (opt-in; psutil's thread listing holds the GIL ~150 ms), the tabs before and after the
    status bar, a 15 s reading pause after Simulate, the probe as the user for ui/idle.py, UTF-8 output; py-spy
    --native is no use on Windows (see ARCHITECTURE "Measuring it").
- **Latest installers: main 6698557** (2026-10-17, `help-tabs` merged at the user's "merge": the help's sidebar lists
  the Power integrity and Signal integrity tabs and the safety checks, which were under "More"; F1 on those two tabs
  opened the workflow, now their sections; test_gui's help test checks every tab and section):
  `out/dist/groundtruth-0.1.41653-6698557-pcm.zip` and `groundtruth-server-0.1.41653-6698557.zip`, signature
  checked, the plugin's copy the same zip; code fingerprint `be553fdeeef3`, the same as 7074837's (help only).
- **Installers before: main 7074837** (2026-10-17, `control-loop` merged): `groundtruth-0.1.41652-7074837-pcm.zip`
  and `groundtruth-server-0.1.41652-7074837.zip`; code fingerprint `be553fdeeef3` (changed from 2027843's): every
  server needs Update all. No unmerged branches. Next: nothing planned; ask the
  user (open: the 145-167 ms full collections during some view switches, a view's 150-300 ms map drawing; the
  common-mode and ESC-stack work stay parked).
- **Installers before: main 2027843** (2026-10-17, `bode` merged): `groundtruth-0.1.41605-2027843-pcm.zip`, code
  fingerprint `61ff4a99301d`.
- **Installers before: main c41daae** (2026-10-17, M3 `signal-integrity` merged at the user's "commit and merge
  everything": signal integrity, KiCad tuning profiles, the Signal integrity tab, converter victims; **code
  fingerprint changed to `a38092bc8cf1`**: every server needs Update all): `out/dist/groundtruth-0.1.41591-c41daae-pcm.zip`
  and `groundtruth-server-0.1.41591-c41daae.zip`, signature checked, the plugin's copy the same zip. No unmerged
  branches.
- **Installers before: main 3d580e4** (2026-10-17, M2 `safety` merged at the user's "Merge and continue with M3":
  voltage-aware safety checks; **code fingerprint changed to `e6b199fedf32`**: every server needs Update all):
  `out/dist/groundtruth-0.1.41579-3d580e4-pcm.zip` and `groundtruth-server-0.1.41579-3d580e4.zip`, signature
  checked, the plugin's copy the same zip.
- **M3 (`signal-integrity`, merged c41daae on 2026-10-17)**, from main after 3d580e4, at the user's "Merge and
  continue with M3", then "Keep going": the 2D cross-section solver (`solver/xsection.py`, validated against Cohn
  and Hammerstad-Jensen within 0.2 %), `analysis/si.py` (impedance, diff_impedance, diff_skew, crosstalk, via_stub in
  Run checks), settings `si:`, the solder mask in the stackup, `groundtruth detect` suggests pairs by name, reports
  (console, JSON, HTML), the plugin's **Signal integrity** tab (12 tabs now, 24 build steps). Tests:
  tests/test_xsection.py (10), tests/test_si.py (12), a GUI test. See ARCHITECTURE "Signal integrity". Then the
  plan's last M3 item: a power stage's switch node -> victims (victims.py shared with the bridges; ARCHITECTURE
  "Victims and stacked boards", tests/test_stage_victims.py). Full suite on 37f8220+docs: 724 passed, 1 failed (the
  Bridges tab's GUI timing under load, passing alone); after the converter victims: test_stack, test_bridge,
  test_power, test_stage_victims and that GUI test pass (76). KiCad 10 tuning profiles: the user added a "Test" profile
  on kraft_flux_mini_2 (2026-10-17, "i added some"; not assigned to a class): read for targets and the new
  tuning_delay check (ARCHITECTURE "Signal integrity"). Type 1 = Differential confirmed (the user switched Test, "switched to diff").
- **M2 (merged 3d580e4)**: `analysis/safety.py` (net voltages; clearance_voltage by IPC-2221B Table 6-1,
  cap_voltage, esd_protection in Run checks; resistor_power and vds_rating after Simulate), settings `safety:`,
  fields Rated Voltage and Rated Power (InvenTree's names, as on kraft's parts; Voltage / Power read too;
  parse_quantity now reads "1/16W" as 0.0625 W, it read 1 W), tests/test_safety.py. Run checks runs without nets
  (the board-wide checks). See ARCHITECTURE "Voltage-aware safety checks" for the design and the kraft results. Full
  suite on the branch: 697 passed, 3 failed (2 GUI timing under load, passing alone; 1 CLI test fixed after). Open
  idea, offered to the user, no answer yet: the 27 kraft warnings are gate-drive / bootstrap nets of unknown voltage
  beside the switch nodes; following a bootstrap capacitor to its switch node would give them a voltage.
- **Installers before: main 5068d0c** (2026-10-17 01:45, M1 `pdn` merged: PDN impedance, the Power integrity tab;
  code `7315d29f38d6`): `out/dist/groundtruth-0.1.41560-5068d0c-pcm.zip` and
  `groundtruth-server-0.1.41560-5068d0c.zip`.
- **Installers before: main cf8e446** (2026-10-16, with `update-dialog`; code `0999565b0798`, unchanged since
  64c896a, so the servers on 0.1.41525 take its jobs): `out/dist/groundtruth-0.1.41527-cf8e446-pcm.zip` and
  `groundtruth-server-0.1.41527-cf8e446.zip`, signature checked. Branch `pdn` (M1) is in progress, not merged.
- **Installers before: main 64c896a** (2026-10-16 19:57, with M0 `weak-spots` merged: radiated limits, RE102 built
  in, spread spectrum in the radiated estimate, CE101): `out/dist/groundtruth-0.1.41525-64c896a-pcm.zip` and
  `groundtruth-server-0.1.41525-64c896a.zip`, signature checked. **Code fingerprint changed to `0999565b0798`**:
  every server needs this package (Preferences › Servers › Update all) before it takes the plugin's jobs.
- **Installers before: main 29fb7ad** (2026-10-16 19:40, with `server-test-dialog`: Test connection laid out, an
  older build with the same code offered for Update; code `ed1ff59c30b9`): `out/dist/groundtruth-0.1.41524-29fb7ad-pcm.zip`
  and `groundtruth-server-0.1.41524-29fb7ad.zip`, signature checked. The servers (0.1.41500) take its jobs and
  show as older build in Preferences › Servers: Update all brings them to 0.1.41524.
- **Installers before: main ad230a9** (2026-10-16 evening, with `server-jobs-list`: the Server jobs list safe with
  several windows and rebuilt from the servers; code `ed1ff59c30b9`, unchanged, so the servers take the plugin's
  jobs as they are; the queue-position fix needs the server update, Preferences › Servers):
  `out/dist/groundtruth-0.1.41521-ad230a9-pcm.zip` and `groundtruth-server-0.1.41521-ad230a9.zip`, signature
  checked. Branch `weak-spots` (M0: radiated limit, spread, CE101) is not merged.
- **Installers before: main 456e048** (2026-10-16 14:43, code `ed1ff59c30b9`, with server-address and the modern
  help): `out/dist/groundtruth-0.1.41494-456e048-pcm.zip` and `groundtruth-server-0.1.41494-456e048.zip`, signed
  with the same key (checked); the servers on 6b6c707 take it as a remote update. Before them, **installers of
  main 6b6c707** (code `befdf35d4376`): `out/dist/groundtruth-0.1.41471-6b6c707-pcm.zip`
  (carries the signed server package) and `out/dist/groundtruth-server-0.1.41471-6b6c707.zip` (+ `.sig`), checked: the
  signature verifies, the plugin's copy is the same zip, the Mac launcher 0755. **The release key** was made by this
  build on this laptop: `%LOCALAPPDATA%\GroundTruth\release\release_ed25519.pem` (public half 38534d7f…); the user
  keeps and backs it up. Every server (andre, xps13, the Mac) needs one manual install of this server package with
  remote updates on; then the plugin updates them (Preferences › Compute server › Servers).
- **The plugin on this laptop (xps13)** is the KiCad package 57602a8 (`KiCad\10.0\3rdparty\plugins\
  io_github_iwers_groundtruth`, the user installed it on 2026-10-16). The older development copy 6c3717d
  (`KiCad\10.0\plugins\io.github.iwers.groundtruth`) was removed at the user's "do it" (`tools/install_plugin.py
  --uninstall`, 2026-10-16). Update it with a new KiCad package (Plugin and Content Manager > Install from File);
  `tools/install_plugin.py` would make a second copy again. The user restarts KiCad afterwards and checks that
  Enable KiCad API is still ticked.
- **The user's cluster** (one key): **andre** (16 cores, lead with `--lead`), **xps13** (this laptop, 8 cores, a
  server installed here too) and **Andres-MacBook-Air.local** (Apple M2, 8 cores, 24 GB; Python 3.13 from python.org;
  installed 2026-10-15 from the 1f6ac0b package, ran a 16x kraft Simulate). andre and xps13 still run the 6c3717d
  server (code `f3ea22e99eb5`): they refuse the current plugin's jobs until updated with the 57602a8 server package
  (the fingerprint changed with `dcload-picker`). andre did not answer from 2026-10-15 08:28 (a job sat 12 h as "server
  not reachable"; it was removed by hand, `remote_jobs/1d1cf7b6a12d837d`): why is not known (off, a new address, or
  its server stopped; `install_server.cmd status` there).
- **Latest work (2026-10-15/16), details in the dated entries below:** signed remote updates of the servers; through-hole pin values (contact, joint,
  solder fill, pin material); DC loads Fill from net…; pin completion in the
  DC loads form; Remove from list for a job whose server can't be reached; the compute server on macOS (launchd) and
  its Python 3.11+ requirement (KiCad for macOS bundles 3.9); the server package keeping the Mac launcher executable.
- **Session 10's work, in the entries below:** contours with value labels, KiCad 11 note, resolution cell sizes,
  AMD GPU, input current, conv1/conv2 made similar (`mirror_vias.py`), map click → highlight in KiCad, issues per
  view, arrow widths, parts spread over their pads, contour widths, DC paths on the whole board with the stages'
  currents together; plus the kraft design studies (advice only, next entry).
- **Open items:**
  1. Done 2026-10-14 (branch `report-dc-combined`): the HTML report shows the stages' DC currents together first under Power stages (`html._combined_dc`: input current, copper loss, hotspots, the 12 most loaded vias, the map; `render_html(dc_combined=)` from Export report, the history page and the CLI). Before: not drawn (its
     issues are in it).
  2. The whole-board heatmap view for the other views: parked by the user ("let's park that then").
  3. Writing GT labels into the schematic needs KiCad 11 (kipy 0.8.0's schematic API fails on KiCad 10); the
     window shows the labels to add by hand. A live check of "Add KiCad selection" on the scratch copy
     `out/live_sch/` is untried.
  4. GPU timing on the user's AMD Radeon 860M machine is not measured (`tools/check_gpu.py` there).
  5. Board-wide resonances above 1 GHz are not grid-converged (a note says so; an 8x check needs a bigger machine).
  6. Not modelled (named in the design studies): inductor core loss, dead-time conduction, load steps and the
     control loop, output voltage ripple (hand estimates only).
  7. Parked by the user: stacked boards, common-mode cable modelling.
  8. andre and xps13 need the 57602a8 server package (their code differs from the plugin's); why andre stopped
     answering on 2026-10-15 is open.
  9. A Mac: the double-click of install_server.command with the fixed package is unconfirmed (the user ran it with
     bash). Offered, not asked for: servers behind a VPN (beacons on every adapter, Find servers through a known
     server, a README note); GT schematic labels for DC loads.
- Real boards are read-only; settings for them go to scratch folders.

**M1 PDN impedance (2026-10-16/17, branch `pdn`, merged into main 5068d0c at the user's "Yes"):** the user: "merge and let's start the next"
(M0 merged; M1 of the plan: PDN impedance and decoupling). Done so far (ARCHITECTURE "Power integrity"): settings
`pdn:`, analysis/pdn.py (the rail and its ground on the whole-board model, decaps, the source as R + jωL, each IC a
port, Z per port and what holds the rail, peaks, inductance above the decaps, target margin; issues pdn_impedance,
decap_missing), Simulate / split / CLI / JSON / console / HTML report (Power integrity part with the |Z| plot),
tests/test_pdn.py (analytic: planes' C, 1 and 4 decaps, the source's R, a cut-off IC), tools/dev/pdn_check.py.
kraft_flux_mini_2 +3V3 (target 0.55 Ω for a 0.3 A step; source the LDO U505): at 80k nodes (0.59 mm, 23 min here)
U1102 (STM32) +2.3 dB at 100 MHz, the others over the target at 100 MHz (U203 -14 dB, U301 -10 dB, U1103 -9.8 dB):
all at check_to, in the inductive rise (uncorrected). The user chose "Fine model per IC" then "Plugin tab + plot
view": with each IC's pins on a fine regional model (ARCHITECTURE "Pins on a fine model") the 0.86 and 0.59 mm grids
agree at 100 MHz within 1-20 % (my first "it halves" compared curves by solve index, not frequency: wrong);
corrected, U1102 -0.7 dB, U1101 -6.1, U1001 -6.7, U1103 -10.1, U305 -10.1, U301 -11.8, U203 -14.7 dB at 100 MHz.
Below a few MHz the regulator and the decaps: 15-200 mΩ. decap_missing: U1101 (3.9 mm), U203
(4.1 mm). J1101 (Tag-Connect, power_in pin) is no longer an IC port (connectors left out). Still to do: the user's
choices on check_to's default and the accuracy above ~50 MHz (a regional fine model per IC), then the plugin tab.
`pdn` has main merged in (update-dialog). Plugin: Power integrity tab (Find rails, form), PDN view on the Heatmap
tab (the curves; what holds the rail at each IC). gui_probe (kraft copy, PDN-only settings, local Simulate 760 s at 1x):
load 201 ms (the ground list was every net: 270 ms, now ground-like nets only); the PDN view 346 ms, tab switches
330-410 ms after Simulate: native (inside MainLoop), the known large-board pattern also on main (see the "Motor bridges" entry,
"Tab switches after Simulate 324–396 ms"). gui_probe and window_shot now use a private LOCALAPPDATA unless
--real-prefs (full suite on pdn e392c4c: 690 passed, 1 skipped, 1 failed: the window-construction timing, 0.176 s against 0.15, passes alone twice): the first PDN probe ran with the user's Preferences, so its Simulate went to the compute server (refused:
development code) and its job tracker read the user's job list.

**M0 "weak spots" (2026-10-16, branch `weak-spots`, merged at the user's "merge and let's start the next"):** the user asked what else would help an engineer
check a design is sound and chose all four proposals: M0 weak spots, M1 PDN impedance and decoupling, M2
voltage-aware safety rules, M3 signal-integrity extras, one milestone at a time (the plan:
`~/.claude/plans/snazzy-prancing-puddle.md`, copied into this entry's list below). M0 so far:
- The 3D drag test (`tools/dev/view3d_drag.mjs`): Edge 155 headless ignores `--window-size` (a 500×320 viewport,
  the drag point lay off the page); the script sets the viewport over DevTools. The page was fine.
- README: the combined DC view is in the HTML report; the Mac server is in use.
- **A radiated limit of the user's own** (`analysis: radiated_limit: {name, distance_m, points, bands}`,
  `emi/radiation.RadiatedLimit`): for MIL-STD-461G RE102 (later the same day built in by name, see CE101 below) or
  a customer's curve. Its band sets the radiated band (from 2 MHz for RE102 ground), its bandwidths Table II.
- **The small loop's field with the induction term** (`loop_field`): at RE102's 1 m the far-field formula is
  low below ~48 MHz (+3 dB at 48 MHz, +20 dB at 4.8 MHz); at CISPR's 10 m from 30 MHz it adds ≤ 0.11 dB, so the
  CISPR results barely move.
- **Spread spectrum in the radiated estimate** of a power stage (as conducted). And `spread_readings` with a peak
  detector reads the largest single pass where sweeps overlap; the power sum before read up to 6 dB above the
  undithered harmonic. This also changes the CONDUCTED dithered readings at high harmonics (lower, a few dB):
  kraft's earlier CE102 spread margins (Session 4) are slightly pessimistic.
- **The receiver simulation made fast and its tuning search fixed** (ARCHITECTURE "Radiated limits and spread
  spectrum"): the first test run hung an hour in the fix advisor's spread option, synthesising 200 MHz-wide radiated
  sweeps over 15 ms. Now whole modulation periods, a band-limited read per tuning, extrapolation beyond 2²⁰ samples.
  The search had missed the worst tuning of wide sweeps (up to 5 dB optimistic; now within 0.03 dB of a dense scan),
  so conducted spread readings move slightly up too. A test Simulate with the fix advisor: 114 s on main → 6 s
  (the advisor ranks its spread options without the radiated spread, then reads it for the chosen one).
  test_split's cancel test waited for split parts that never came (B was gone, the lead ran the job whole), the
  job finished and forget removed it (KeyError); it passed on main only because the slower first job had used up
  the shared deadline. Now it cancels as soon as the job started, split or whole.
- Found already done: the CE102 relaxation (`conducted_class: 115v|220v|270v|440v`, `emi/conducted.MIL461_RELAX`;
  the open item below said otherwise).
- **CE101** (first skipped as below the switching frequencies; the user: "this is a valid frequency range though
  ... implement"): `analysis.ce101: aircraft | ships` (ARCHITECTURE "Radiated limits and spread spectrum", CE101).
  The limits were read from the released MIL-STD-461G (a public copy, NASA's SSRI knowledge base); from the same
  PDF RE102's platform curves are now built in too (`radiated_limit: re102_ground_army` etc., at the user's "yes"). A bridge's
  commutation (6·f_e) is what CE101 sees; on the test bridge the leads resonate with the bus capacitors in band.
- Full suite on `weak-spots` 28fe736 (2026-10-16, 33 min, other tests running alongside): 668 passed, 1 skipped,
  1 failed: the Thermal button's 0.5 s GUI timing (0.58 s under load); passes alone.
- Not done (merged without it, the user went on to M1): the guided live-KiCad checklist with the user (Sharper
  map, Thermal button, Parts tab following the selection, board view hover/zoom). Still open.
- The merge changes the code fingerprint: every server needs the new package (Preferences › Servers › Update all).

**Update confirmation laid out; a server just started (2026-10-16, branch `update-dialog`):** the user: "that
update server dialog needs the same formatting as Test connection now has": `server_info.UpdateConfirmDialog`
(headline, a line per server with its version now and after, the notes; Update / Update all). Then: "it says no
other server with this key is heard. However, andre has the same key": the Mac had been up 1 s (just updated);
a status request 3 min later showed the cluster again (Mac lead, andre member). A server up for less than
LISTEN_S (30 s) now says it has not heard the others yet and to test again in a moment.

**Test connection laid out; an older build shown as out of date (2026-10-16, branch `server-test-dialog`):** the
user, of the one-paragraph Test connection message box: "Format this better!" Now `ui/server_info.py`: a dialog
with a headline, label/value rows, the cluster as a table and Copy details (`server_summary` is plain data, tested
without a display; remote.client.describe stays for the CLI). Then, with both servers "up to date" on 0.1.41500
next to the 0.1.41521 plugin: "should the server say outdated given that the plugin is newer?" Yes:
release.compare called the same code "same" whatever the build, so a build that changed only the server's own
program (outside the fingerprint, e.g. server-jobs-list's queue position) was never offered. `ui/server_updates.
compare_build`: an earlier build than the carried package is "older" ("takes this plugin's jobs"), and an
update is done when the server runs the package's build (it answered with the same code before too). Code
fingerprint unchanged.

**Server jobs lost from the list; "2 job(s) ahead" (2026-10-16, branch `server-jobs-list`):** the user, with the
Server jobs tab showing one Simulate "queued, 2 job(s) ahead" and nothing else current: "current jobs are lost
after a while, make sure the plugin querries the sever and rebuilds the list. Also when starting a new job it
queues it and currently is in position 2 with nothing on". Found (a read-only status request to the Mac with the
plugin's key, not printed): the Mac ran 7205dd4f (queued 17:55, started 18:17) and had 799379fa (split over the
Mac and andre, done 18:18, not fetched) and 08f9490c queued; the local job.json of all three ended in `}}`, so the
list dropped them. Two plugin windows were open (plugin.log: started 17:25 and 17:58), each Tracker rewriting every
job.json through the same temporary file. The queue position was right (the two jobs were running), except that a
split job's coordinator counted although it holds no slot. Fixed in `ui/job_store.py` (ARCHITECTURE "Compute
server", Several windows) and `remote/server.py` (`Job.split`, `_busy`); code fingerprint unchanged
(`ed1ff59c30b9`): the servers need no update for the plugin side; the position fix needs one (remote update).
The user's three damaged entries are repaired by the fixed plugin's first read.

**A server's address, its IPs in the status (2026-10-16, branch `server-address`, merged):** the user, with Preferences'
Servers showing only DELL-7920-PC (192.168.120.128, the lead) while andre (192.168.0.6) was in its cluster: "1. it
would be good if status shows the servers IP. 2. the andre server (192.168.0.6) is in the same network but used a
different address so it doesn't show up under servers. can we start a server with a known address like:
install_server.cmd install --key --ip and then get presented the available options?". Likely cause of the missing
row: the list was filled when Preferences opened, before andre joined (14:23); a PC with two cards was a real
weakness though. Now: `install --address` (pick from this PC's network cards, or give one; server.json `address`):
the server announces it alone; beacons also go to every own subnet's broadcast (Windows sends 255.255.255.255 out of
one card); status shows `Server: andre (192.168.0.6, …)` and `Address: announces …`; Preferences tries each address a
server announces and adds servers a network query hears. Code changed (cluster/server/cli): the servers take it as a
remote update (they run 6b6c707 with remote updates on). Tests: test_remote_update
`test_a_server_with_two_networks_...`.

**A modern help window (2026-10-16, branch `modern-help`, merged):** the user, with a screenshot of the help in wx's simple
HTML view: "can we make this help look more modern and better formatted?". The help now shows in a browser engine
(wx.html2.WebView: Edge WebView2 on Windows; KiCad 10's wxPython 4.2.2 has it) as `ui/help.py` `modern_page()`:
`ui/help_shell.html` with help.html's sections as cards, a grouped sidebar that marks the section in view, search
(Ctrl+F, marks, Enter: next), the theme's light/dark colours. help.html stays the one source (plain HTML for both);
the old view is the fallback (no engine, GROUNDTRUTH_HELP_CLASSIC, the page not loaded within 8 s or an engine
error). Found on the way: wx's synchronous RunScript hung outside the main loop (RunScriptAsync now); the shell's
comment named its placeholders (the page was pasted into it); the back-to-top button shared the id `top` with the
header (hiding it). Checked with headless-Edge screenshots (light, dark, tables); test_gui 70 passed.

**Signed remote updates of the compute servers (2026-10-16, branch `remote-update`, merged 6b6c707):** the user:
"Currently I have to physically go to the servers an update them manually. Can we have a feature that can be enabled
when the server is installed for the first time that allows it to update remotely? I envision that when the
groundtruth plugin is open and the user goes to preferences, it will show the user which server is out of date and
offers to update it through a button press". Asked: "Signed builds only" (not anyone with the access key: an update
runs as SYSTEM / root) and "Wait for running jobs"; then: "The signing key that is used to update the server should
be reset if the server is re-installed manually". ARCHITECTURE "Compute server", Signed remote updates.
- Turned on at the server: a first install asks "Allow remote updates? [y/N]", or `install_server.cmd install
  --remote-update on`; server.json `remote_update`. Preferences › Compute server › **Servers**: the cluster's servers
  with their versions (other-version ones included), Check / Update / Update all.
- The release key: made by the first `tools/make_pcm_package.py` / `make_server_package.py` on the build PC, in
  `%LOCALAPPDATA%\GroundTruth\release\release_ed25519.pem` (the user's; never commit, print or copy it here). A
  server trusts the public half of the package it was installed from by hand; every manual install resets it.
- The service's runner now runs the server as a child process and applies updates (exit code 75), with a roll-back
  when the new server ends within 120 s. **Every server needs one manual install** of a signed package with this
  feature (their runners are the old in-process ones), answering yes to remote updates; after that, updates come from
  the plugin. The Mac server needs `bash install_server.command install --remote-update on` once.
- Tests: tests/test_remote_update.py (10, incl. end to end: a real runner + server updated from a signed package of
  this tree, then a broken newer package rolled back), test_gui `test_preferences_list_the_servers_...`; test_remote,
  test_cluster, test_server_setup, test_cli pass. Not tried on the real servers yet (they need the manual install
  first). `cryptography` is new: in the server's requirements (SERVER_EXTRA) and the dev venv (`.[dev]`).
- Full suite on the branch (2026-10-16, 40 min): 650 passed, 1 skipped, 5 failed: three GUI timing tests over
  0.5 s under load (pass alone), the key-reset test (the installer's dry-run tests leave DRY on: fixed), and
  `test_view3d.py::test_the_grabbed_edge_follows_the_mouse`, which fails alone too: not touched by this branch,
  it passed this morning; Edge updated itself to 155 meanwhile (the test drives the page in headless Edge).
  Open: look at the 3D view's drag in Edge 155.

**Through-hole pin values (2026-10-16, branch `tht-pin-params`, merged 17adb8e):** the user, of a big connector's map
after `tht-pins`: "do we assume a solid connection between the connector and the via; e.g. filled with solder? ...
introduce a GT field parameter that can be assigned to connectors to define the solder joint resistance", then "We
should implement all of them but should allow to use default values for each when one or more are not entered".
Asked: the built-in pin stays today's copper-filled hole ("Keep today's copper fill"), a contact resistance is never
guessed ("0, with a note"). `analysis/pins.py` (ARCHITECTURE "Through-hole pin values"): `contact`, `joint`, `fill`,
`pin`, `pin_d`, `pin_r`, each from the GT field (`GT = conv_in, contact:0.5m, fill:75`), the part's settings `tht:`
(Parts tab, Pins column), the board's `tht:` (Parts tab, Pin defaults…), else built in. `fill`/`pin` set the pin
column between layers (`simulate.add_pin` `column`) and where a partly soldered pin feeds (`StackBuild.pin_feed`,
`dc_board_build` adds that layer); `contact`/`joint` add I·R to DC loads' drops (`pins_mv`) and I²·R in Thermal
(`part_losses`: DC loads' pins, converters' input/output connectors). Not yet: motor bridges' connectors, a power
stage's DC drop. Help: Parts tab › Through-hole pins, the label table, the Converters/Bridges label text.
- Tests: tests/test_pin_params.py (fill 10 % vs 50 % = I·0.4·T·(1/G_plating − 1/G_filled) within 2 %; contact and
  joint exactly I·R per pin per side and I²·R per pin; fallback per value; settings round trip), test_gui
  `test_the_parts_tab_edits_through_hole_pin_values`; the affected files (124 tests) passed.
- kraft_flux_mini_2, J101 (XT60) `fill:75, contact:0.5m`, `tools/dev/pin_check.py` (conv1, 5.46 A in): J101 gets
  0.030 W (2 pins x 5.46² A² x 0.5 mΩ; conv1 + conv2: 0.036 W at 6.0 A), the "without a contact resistance" notes
  go; conv1's input path 9.09 → 9.11 mΩ. Built in, J101.2 (V_BATT) is fed on F.Cu and sends 2.49 A down to In1;
  with fill 75 it is fed on In2 (the top of the solder) and all 5.46 A climbs to In1, 2.94 A on up the plating to
  F.Cu; J101.1 (ground) gathers its return at In2 instead of the F.Cu pad.
- Analysis code changed (code fingerprint): after merging, every server needs the new server package.

**Pin completion in the DC loads form (2026-10-15, branch `pin-complete`, merged 57602a8):** the user: "can you auto
complete the source pin source return as well as the loads?". `ui/pin_complete.py` (no wx; in ui/ so the servers'
code_id stays): `pin_table`, `offer` ranked per field (source: supply connectors first; return: the source part's
ground pin; a load: pins on the source's net, listed ones left out; a load's return: its own part's pin on the return
net), `complete_field` / `complete_load_line` (PIN/RETURN=CURRENT, nothing in the current). `dcloads_panel.PinPopup`:
a wx.PopupWindow + ListBox under the caret (EVT_CHAR_HOOK for Up/Down/Enter/Tab/Esc, Ctrl+Space), made on first use
(the tabs are built under a temporary top-level window: a popup made there was destroyed with it; wxPython's bool()
of a deleted window is False), the field focused before positions are read (Windows draws a multi-line field's grey
hint as text while unfocused). Kraft: 562 pins, 2 ms; gui_probe on a kraft copy: 0 stalls. Tests: test_dcload
`test_the_form_offers_the_pins_that_fit`, test_gui `test_the_dc_load_form_completes_pins`.

**Remove from list for a job on an unreachable server (2026-10-15, branch `remove-unreachable-job`, merged 57602a8):**
the user's Simulate handed to andre (192.168.10.127) at 08:28 sat 12 h in Current jobs as "server not reachable:
retrying", and Remove from list did nothing (it only tried Cancel there; the failure went to the status line). Now
Remove asks once, cancels on the server and removes the entry (`ui/job_stop.py` `stop_on_server`: in ui/, so the code
fingerprint stays); with the server unreachable a second question offers `remove_anyway`. Cancel job offers the same.
Test: test_remote `test_a_job_on_a_server_that_is_gone_can_be_removed`.

**Compute server on macOS (2026-10-15, branches `server-macos` and `mac-python`, merged 1f6ac0b):** the user: "the
server should support macs" (an Apple M2). Installed and running jobs on the user's M2 since 2026-10-15 ~20:30.
- tools/server_setup.py's Mac branch (`mac()`; hidden `--platform mac` for dry runs and tests): /Library/Application
  Support/GroundTruth (chown root:wheel, chmod go-rwx), the venv's bin/python3, a launchd daemon
  com.groundtruth.server (RunAtLoad, KeepAlive; `launchctl bootstrap/bootout/kickstart/print`), the Application
  Firewall (`socketfilterfw --add/--unblockapp` on the venv python's real file), --cuda ignored (CPU only).
  tools/install_server.command (bash, LF, sudo). server.local_addresses skips macOS's virtual interfaces (utun,
  awdl, llw, bridge, anpi, gif, stf, ap1).
- First try: "you do not have the appropriate rights" on the double-click. zipfile on Windows writes creator system
  MS-DOS, so Archive Utility ignored the 0755 mode; fixed (`make_server_package.zip_entry`: create_system 3, 0755 /
  0644). `bash install_server.command` works without the execute bit (and without Gatekeeper's question): it is what
  the user ran. The double-click with a fixed package is not confirmed yet.
- Second: KiCad.app's Python is **3.9** (KiCad 7 on the Mac then; KiCad 10 for macOS bundles 3.9.13 too, a KiCad
  forum thread): the server ran, every Simulate failed on `itertools.pairwise`. Fixed: the installer (and
  install_server.command) takes the first Python >= 3.11 (KiCad's, python.org's, Homebrew's, python3) or stops naming
  python.org's installer; a venv from an older Python is made anew; `groundtruth serve` refuses Python < 3.11. The
  user installed Python 3.13 from python.org.
- Third: Test connection said "PyTorch does not load ... a DLL missing: the Microsoft Visual C++ Redistributable"
  ("dlls missing on mac?"): the Windows GPU-probe text. A Mac server now probes nothing and says "a Mac (no CUDA on
  macOS, and an Apple GPU has no double precision for the solver)" (`server.MAC_GPU_NOTE`).
- The GroundTruth plugin itself would get Python 3.9 inside KiCad on a Mac (too old): only the server runs on a Mac.

**DC loads tab: Fill from net… (2026-10-15, branch `dcload-picker`, merged 1f6ac0b):** the user pressed Add on the empty form (the
loads' grey example looked like a value). Now every field has a grey example, an empty form says what to do, and
**Fill from net…** picks the rail net and its ground, then the source and the loads from `dcload.rail_candidates`
(every part on the rail with its own ground pin, connectors first; `guess_source`: the connector with the fewest
pads), and a current per load. boardgen `distribution_board()` is a passive distribution test board (2-pin J1, four 4-pin
connectors on a VDD pour, GND plane); tests in test_dcload.py and test_gui.py.

**Through-hole pins joined around their barrel (2026-10-15, branch `tht-pins`, merged 6c3717d):** the user, with a map of a big
through-hole connector pin (a bright point by the pin): "the current density around that area looks odd. We need to
find a way to better simulate those". A THT pad's pin was joined to each layer at the one node nearest its centre: the
whole current entered one cell (a hot spot ~5x the real density). Now `simulate.add_pin` joins it with up to 48
parallel branches around the drill's ring (ARCHITECTURE "Formulation"). Results with THT connectors change near the
pins (the far field and resistances hardly). kraft_flux_mini_2 conv1: path R, loss, issues unchanged; at the
XT60 (4.5 mm drill) the old model's hot spots inside the holes (46.5 A/mm² at J101.1, 38 at J101.2, r < 0.4 mm)
are gone (6.0 / 37.8 A/mm² on the ring and the real neck), and the pins now share current with every layer.

**DC loads (2026-10-15, branch `dc-loads`, merged 6c3717d):** the user, of a passive distribution board (a VDD pour
from the 2-pin connector to four 4-pin connectors): "how can I simulate a load across the highlighted trace?", then
"temperature/capacity as well". New: `dc_loads:` in the settings (DcLoadSpec: source, source_return, loads
[{pin, return, current}], max_drop), the DC loads tab, analysis/dcload.py (one DC run per rail on the whole-board DC
model; each load's supply/return drop from the node potentials; current_density, via_overload, dc_drop issues),
the rail as its own Heatmap view (view_of set, kind "dcload"), the report's DC loads part, console/JSON, the split
(an item), Thermal (heat, series parts' losses) and Current capacity (all loads scaled). Validated: a pour's drop =
I·L/(σwt) (4x converged +0.1 %, 1x -5.5 %), superposition, return drop, capacity √ law (ARCHITECTURE "DC loads").
Not tried on the user's board yet (it is on their machine).

**SuperLU's size limit (2026-10-15, branch `lu-size`, merged 9da0da3):** the user's flux board at 25x on the Dell failed after 30 min
("gstrf was called with invalid arguments" at buck conv1, 1.91 M nodes, solve 7 of 46; peak 21.9 GB, 52 GB free):
SuperLU's 32-bit counts overflow past ~2.1 billion entries. Now the factorisation is redone with diagonal pivots
only (least fill), later ones of that model at once; too large even so: those frequencies unsolved with a note
naming the limit, the job goes on (ARCHITECTURE "Preconditioner", SuperLU's size limit). Not yet tried on the real
model: if diagonal pivots don't fit either, the cure is 16x/20x, or a 64-bit solver (MKL PARDISO / UMFPACK 'zl':
a new dependency, not decided).

**Report images at full resolution (2026-10-15, branch `report-images`, merged dc3a981):** the user: "when exporting a
report or in history, make sure you export the highest resolution images available". The report drew every figure
at 130 dpi (a 25x map has more cells than that gives pixels) and ignored DC views sharpened with Sharper map. Now
maps at heatmap_dpi (>= 1.5 px per cell, up to 400 dpi), plots 200 dpi, sharpened views drawn, click for full size
(ARCHITECTURE "HTML report"). Not yet measured on a big board at 25x (expect tens of MB per report).

**"Use all cores" (2026-10-15, branch `all-cores`, merged dc3a981):** the user, of the Dell at 25x using 13 GB of 62 GB:
"why is it only using so little of the RAM?" (RAM follows the model size; the limit is how many solves can run at
once: after the first round one frequency per item and round). Opt-in Preferences "All cores" (with an (i) button
on its potential issues) / `--all-cores`: auto threads on every physical core, up to 4 frequencies per run and
round on a budget raised as much (ARCHITECTURE "Frequency planning"). Not yet measured on the Dell.

**Real addresses, a resizable details box (2026-10-15, branch `server-addresses`, merged dc3a981):** the user: Find servers showed
andre at 172.20.192.1 (its vEthernet: the answer left through the virtual adapter; the cluster's peer table took
addresses the same way). Servers now announce their real addresses and the receivers pick the reachable one
(ARCHITECTURE "Compute server", Addresses). And "the details job window can't be resized": the Server jobs tab is a
splitter (lists above, details below), its height kept in ui.json (`jobs_details_dip`). The user now runs a large board at
25x on DELL-7920-PC.

**Splits by each machine's memory and speed (2026-10-15, branch `split-capacity`, merged 739284d):** the second real split (andre +
xps13, kraft at 16x) sent one ~9 GB stage to xps13, the user's laptop with 2.2 GB free (KiCad, VS Code, Teams):
its memory watchdog stopped the part and andre redid it after its own ("it looks like the XPS stopped"). The user:
"use any machine to make use of the CPUs and RAM ... scale the work load depending on the machines capabilities".
Now items carry a memory estimate (then the measured peak), servers their free memory and measured speed, and the
plan puts an item only where it fits; an out-of-memory part moves to a server it fits and is remembered
(ARCHITECTURE "Compute server", Capacity-aware plan). Note: xps13 is this laptop (its service is installed here).

**The first real split ran whole (2026-10-15, branch `cluster-status`, merged 54ae76c):** andre (16 cores, lead) + xps13 (8 cores),
kraft_flux_mini_2 at 16x with two stages on (conv1, conv2; every net and conv3/conv4 off): the job ran on andre
alone (the user: "it's not sharing the load"). plan_split weighed servers by all their cores: 6/16 + 6/16 on andre
tied 6/8 on xps13 and the tie went to andre, so "the items fit one server" and the job went back whole (seen as
"Waited in the queue 0:05": the items child). Now `split.server_speed` counts cores up to SPEED_CORES (8; a
Simulate's solves use ~4 threads) over (1 + jobs there), and plan_split gives every idle server without an item
one from the server finishing last; a Simulate that runs whole carries the reason (`Job.note` → job info → the
plugin's details "Cluster: ..."). Test: test_two_stages_go_to_two_servers_whatever_their_cores. Needs the servers
and plugins updated (code changed). The log: `C:\ProgramData\GroundTruth\server.log` (admin-only folder).

**Cluster status with history (2026-10-15, branch `cluster-status`, merged 54ae76c):** the user, of `install_server.cmd status`:
"should list the size of the cluster and each PCs name/IP inside the cluster. also it should show if an individual
machine of the cluster disconnected at any time and how long it is connected to that cluster". Now: size and since
when, the lead (since when, how chosen), every server (name, IP, cores, memory, GPUs, jobs, time in the cluster,
last heard), other versions, the network (UDP port, listed peers) and the last 10 events (joined / lost / back /
restarted / lead); Test connection shows the short form. History in memory (and server.log). Also: a `--dry-run`
as a normal user no longer fails on an installed server's locked folder (`server_setup.readable`; the server is now
installed on this laptop too, which made test_install_server_cmd_runs_through_cmd_exe fail). The user also had two
keys after a second install: two keys = two clusters; `install_server.cmd install --key` on the second with the
first's key; laptops point at the lead (Find servers lists it first; `--lead` on the always-on machine).

**Waited-for server jobs listed (2026-10-14, branch `server-jobs-all`, merged 2026-10-15):** the user, with a Thermal running on the
server (andre) and an empty Current jobs list: "thermal sims are not shown". Only the handed-over jobs (Simulate,
Suggest fixes) were listed; Thermal, Current capacity, Check in Simulate and Compare what-if wait in the window
(`Remote.run`) and were never written to the jobs folder. Now `Remote.run(watch=)` reports them and
`jobs.watcher` lists them ("attached": the Tracker skips them), ending under Past jobs; `close_stale` at start ends
the entries of a window closed meanwhile (ARCHITECTURE "Compute server", Detached jobs).

**Cluster, milestone 2: the split (2026-10-14, branch `cluster-split`, merged 9190ff3 on 2026-10-15):** the user: "yes, and do the
splitting" (after merging milestone 1). A Simulate on a cluster is split per item (nets, power stages, bridges) by
the lead and merged into the same result (within the GMRES tolerance); a member passes Simulates on to its lead.
Design and code map: ARCHITECTURE "Compute server", Cluster, milestone 2. Tested with two real servers on this
machine (tests/test_split.py, ~10 min). **Not yet tried on the real servers** (ANDRE, DELL-7920-PC, ST650): the
next step is the speed test, a large board at 1x on one server against the cluster, results compared with
`tools/dev/result_diff.py`. Known limits: one big item (one huge stage) can't be split further; the item costs are
fixed guesses (a stage = 6 routes), measured times could refine them.

**Cluster, milestone 1 (2026-10-14, branch `cluster`, merged b399ef1):** the user: "can we have multiple servers
(cluster) to do the sim faster?"; asked: goal **one Simulate faster (split)**, **4 or more** servers; the user's plan
comment: "the server should scan the local network for others and whoever is first to find another should become
lead. Also, an optional option on startup should define a server as lead if needed". The approved plan has two
milestones (stop and show after each):
1. **Done on `cluster`:** discovery (signed UDP beacons on 8766, same key = same cluster), the lead election (first
   to find another; `--lead` wins; a gone lead replaced within ~20 s), the cluster in /v1/status, Test connection,
   `install_server.cmd status`; installer `--key` (asked, unechoed) / `--lead` / `--no-lead` / `--peers` /
   `--beacon-port`, the UDP firewall rule; Preferences **Find servers**. Details: ARCHITECTURE "Compute server",
   Cluster. Jobs still run where they are sent.
2. **Next (ask the user first):** the split. On the lead, a coordinator thread (not a job slot, so nothing deadlocks)
   plans per item (nets, power stages, bridges; greedy longest-first weighted by cores), sends each part as an
   ordinary `simulate` job with `nets=[items]`, `part=True` to a member (via `client.Remote`), re-runs a lost/failed
   part on the lead, cancels parts on cancel; a member that gets a Simulate redirects it to the lead. Parts skip
   `combine_dc` and keep the stages' DC branch currents as arrays plus a fingerprint of the whole-board DC model;
   a `merge` job concatenates routes/converters/bridges in the settings' order, dedupes and sorts issues, rebuilds
   the whole-board DC model (no solve), sums the parts' currents and runs combine_dc's view/issue code (refactored
   to take arrays). **Security requirement:** a beacon's source address is not authenticated, so before sending a
   peer a board, the lead must check over HTTP that the peer holds the key (signed challenge/response). Then the
   speed test on a large board (1x one server vs split over ANDRE + Dell + ST650, `result_diff.py`).
   The plan file was `C:\Users\iwers\.claude\plans\snazzy-prancing-puddle.md` (outside the repo; the summary above
   is what matters).

**The service works on ANDRE (2026-10-14):** after `install --new-key` from the 0324d23 server package: "GPU: NVIDIA GeForce RTX 3050 Laptop GPU" (16 cores, 31.7 GB, GPU auto). First real service install confirmed. The RTX 3050's double precision is weak (consumer card): suggested a 1x timing with gpu auto vs off. Branches `view-list-width` and `server-service` still unmerged.

**The service's processes found no libraries (2026-10-14, `server-service`):** after the restart fix ANDRE still said "PyTorch is not installed" with PyTorch in the venv: KiCad's bundled Python ignores PYTHONPATH (checked here: sys.path has no PYTHONPATH entry), so the GPU check, and every JOB process too, found neither the venv's libraries nor GroundTruth (jobs on the service would have failed). The service now runs on the venv's python.exe and `groundtruth_app.pth` puts app\ on its path. Tried here end to end: a real Simulate through the service's runner (12 s), the GPU check importing a stand-in torch from the venv, killing only the launcher ends the server. Only tools/ changed: same code ID, laptops unaffected.

**Install didn't restart the service (2026-10-14, `server-service`):** on ANDRE `install --cuda cu126` installed PyTorch, but the status still said "not installed": install (unlike update) did not stop the running task, so `schtasks /Run` was ignored (one instance) and the old server went on with its old GPU check. Now any install stops a running task first and waits until it has ended (`task_exists`, `stop(wait_s)`). Also: the users' address list leaves out virtual adapters (`server.local_addresses` with psutil: down, vEthernet/Hyper-V/WSL, VMware, loopback, link-local; ANDRE showed 172.20.192.1), and `--new-key`. The user then wrote "no GPU" (after stop/start?): waiting for the status's Why line and ANDRE's card. The user pasted the real key again: reminded (AI policy), advised --new-key.

**Status block (2026-10-14, `server-service`):** the user, of `install_server.cmd status`'s one long wrapped line (ANDRE: "No module named 'torch'": the service venv has no PyTorch, installed without --cuda): "this needs better formatting". `server_setup.status_block`: one fact per line within 78 columns (Service, Server, Version, Machine, GPU, Why, Fix, Jobs, Settings), `gpu_reason` turns the server's note into the cause and its one fix, `task_state` the task in a few words; shown by status and at the end of install. Only tools/ changed: the laptops' plugin stays valid. Note: the user ran install_gpu.py on this laptop by mistake (PyTorch cu126 in the KiCad plugin venv here, no NVIDIA GPU): offered to uninstall it, not done.

**"no GPU found" with no reason (2026-10-14, `server-service`):** the user's laptop ANDRE (16 cores, 31.7 GB) runs the service; status said "no GPU found". The probe swallowed why. Now `server.GPU_PROBE` (in its child process) reports the reason (PyTorch missing or not loading: --cuda / the VC++ Redistributable; a CPU-only PyTorch; no NVIDIA GPU or driver too old), kept as `gpu_note`, logged, in /v1/status and in `describe` (Test connection, install_server.cmd status). Asked the user for the torch check in the service venv and the card. `protocol.CODE_ID_SKIP` now also leaves out remote/server.py: server-side fixes deploy to the server alone (this change itself alters the ID once).

**install_server.cmd broke (2026-10-14, `server-service`):** the user, with the 6bcc0a6 server package: ". was unexpected at this time." before anything ran: the new echo "(it shows each step as it goes)" inside an if ( ... ) block ended the block at its ")". The wrapper now has no message inside a bracketed block (gotos instead; also no %ProgramFiles% echoed, which would be "(x86)" in a 32-bit console). Tests: every .cmd in tools/ and plugin/ checked for a bracket in an echo inside a block (it catches the broken line), and the wrapper run through cmd.exe without a UAC prompt (PowerShell echoed; the admin path with --dry-run).

**Installer output (2026-10-14, `server-service`):** the user, after a first real install on the Dell: "the installation experience sucks a bit. nothing seems to happen for a while. make it more interactive" (pip's output was lost: the venv's python.exe launcher again). `server_setup.run` relays every command's output through a pipe as it comes (indented) and prints "still working (m:ss so far)" after HEARTBEAT_S (15 s) of silence; numbered steps with what each does and its time (`Steps`); the address and key set apart at the end; the first window says the install continues in a new administrator window. Test in test_server_setup.py.

**Compute server as a service (2026-10-14, branch `server-service` on `view-list-width`):** the user: "make a service and installer". The server package (`tools/make_server_package.py`) with `install_server.cmd` (elevates, runs `server_setup.py` with KiCad's Python): C:\ProgramData\GroundTruth, own venv, scheduled task at boot as SYSTEM, firewall rule, folder ACL, status/key/start/stop/update/uninstall (ARCHITECTURE "Compute server", Service install; tools/SERVER_README.md). Not tried with the task/firewall/ACL on a real server yet (needs administrator rights: the user's first install is the test); the rest was tried here.

**Truncated view names; the code check (2026-10-14, branch `view-list-width`):** the user, with the Heatmap Route list cut at the box's width: "text is truncated". `results_panels._set_items` sets the Windows drop-down width to the longest entry (CB_SETDROPPEDWIDTH, at most the screen), the box's tooltip names the selected view. Found on the way: `protocol.code_id` hashed every .py file, so any window tweak made the server refuse the plugin's jobs until both were reinstalled; it now skips ui/ (CODE_ID_SKIP). The first server run (a large motor-drive board, 8x, Dell, GPU off) took 2:03:23 against the laptop's 29:37 at 1x; 8x moved the worst radiated margin by 0.7 dB (-20.5 -> -19.8), conducted by 0.1 dB: 1x is converged for the margins. Suggested: 1x on the server with gpu off / on, for a direct comparison.

**Memory shown for server jobs (2026-10-14, branch `job-history`):** the user: "on the client side, also report how much memory is in use by the simulation and how much is still available". `thermal_proc._run_child(stats=)` / `_Watchdog` publish rss, commit (Windows private bytes) and peak every check; the server's job info has mem_mb, commit_mb, peak_mb and `server_memory()` (free RAM, total, free commit); the tracker keeps them; the Server jobs tab has a Memory column ("uses X GB · Y GB free", an ended job's peak) and the details box the full line (`client.memory_text`); the waiting jobs' progress line includes it. Test in test_remote.py.

**Out of memory at 50 % RAM (2026-10-14, branch `job-history`):** the user: a server Simulate failed for want of memory while it used ~20 GB and the RAM sat at ~50 %. Cause (probable; the exact message not seen yet): Windows fails allocations at the commit limit (RAM + page file), and SuperLU commits ~6.5x the fill it touches, while the parallel solves were planned from the free physical RAM. `core/sysmem.available_bytes` is now min(free RAM, free commit: GlobalMemoryStatusEx ullAvailPageFile); test in test_solver.py. Advised on the server: a system-managed (or 1.5-2x RAM) page file; Task Manager, Memory, "Committed" shows used/limit.

**Full suite on main after the detached-jobs merge (2026-10-14):** 560 passed, 1 skipped, 2 failed (32 min): `test_the_window_builds_its_tabs_in_steps` (the new Server jobs tab missing from its list: fixed on `job-history`, 9 tabs, 21 steps) and the Thermal button's 0.54 s stall (timing, passes alone).

**The user's second server (2026-10-14):** Lenovo ThinkSystem ST650 V3, Xeon Silver 4514Y (16 cores), 128 GB DDR5 (8 channels), no GPU. Advised: CPU-only settings (`workers 2, threads 8` for several users, or `1 x 16`), likely faster than the Dell's P2200s in double precision (bandwidth); a GPU only with mixed precision (then RTX 6000 Ada / L40S 48 GB) or an A100-class card as it is; measure first. Proposed: a server list in Preferences (least busy / chosen), not built.

**Session 10, past jobs and errors (2026-10-14, branch `job-history`, merged with the memory changes at the user's "commit, merge and install"):** the user, on the Server jobs tab with a job running on the real server (it worked): "it would be nice if it would show elapsed time. Also, if there is an error. it should be saved somewhere. Can we add a past jobs view that shows the finished job regardless of success or error and if error a detailed error message?" and "also add the name of the computer it runs at. We have several big machines". Done: Current jobs / Past jobs, Server and Elapsed columns, the details box with the error in full (server `Job.detail` with the worker's traceback), Copy details (ARCHITECTURE "Compute server"). Each job remembers its server, so several servers work one after the other (Preferences holds one address at a time; choosing the server per job is not built).

**Session 10, detached server jobs (2026-10-14, branch `server-responsive`, merged into main at the user's "merge"):** the user: "Pick the result up later. A job should have some sort of ID and results should stay available for at least 24 hrs then expire. Once fetched, it should be removed. Also, the client should not need to be connected all the time. if the laptop sleeps and wakes up it should simply reconnect. The system client should not cancel the job unless the user cancels it ... Also, once a sim is done, but results not fetched yet, it should allow a new sim to start. The number or parallel jobs should be configurable somehow." Done (ARCHITECTURE "Compute server", Detached jobs; help "Compute server: set up and run (Server jobs tab)"): server jobs on disk (restart-safe), results 24 h (`keep_hours`), removed on fetch, settings file `server.json` (workers, threads, gpu, keep_hours, port; CLI overrides), a GPU per slot with several workers; the plugin hands Simulate and Suggest fixes over and is free at once, the Tracker brings results back (auto-shown when handed over from this window for the open board, else the Server jobs tab: Show results / Cancel job / Remove), never cancels on a lost connection. Tests: test_remote.py (16 incl. the slow ones), test_gui compute-server test. Not tried on the real server yet.

**Session 10, server froze loading PyTorch (2026-10-14):** first real job: the server logged "queued" but never "started", and the laptop said "can't be reached (timed out)" and offered to run it locally. Cause: `_probe_gpus` imported torch in a thread of the server process; loading the CUDA libraries the first time (2.6 GB, Defender) held the GIL for over a minute, freezing the HTTP handlers and the job slot. Fixed: the probe runs in a child process (`python -c`), not at all with --gpu off; the client's `_ask` keeps asking about a job it handed over (20 s per answer, up to LOST_AFTER_S = 120 s, progress "slow to answer"), then RemoteError ("may still be running there"), never ServerUnavailable (no local rerun offered). Tests in test_remote.py. The user's screenshot showed the real server's access key: reminded of the AI policy, advised --new-key.

**Session 10, server connected (2026-10-14):** Test connection from the laptop: DELL-7920-PC (192.168.120.128 on the office network), GroundTruth 0.1.41188 (94c2705), 16 cores, 62.7 GB, 2 x Quadro P2200 seen by PyTorch (after the Visual C++ Redistributable was installed there), 1 job at a time. Advised: start it with `--threads 8` ("auto" caps at 4), and a timing test: kraft at 8x on the server with --gpu off vs on (and the laptop at 2x) before deciding between mixed precision and milestone 2.

**Session 10, the empty server window: SOLVED (2026-10-14):** the cause was the known quirk of KiCad's plugin venv: its `Scripts\python.exe` is a launcher, and what KiCad's Python prints straight into a console window is lost (CLAUDE.md notes the same for install_gpu.py). The server was running, only silent. `groundtruth_server.cmd` now relays all output through pipes (the checks through a temp file, keeping their exit code; the server through `findstr /r "^"`, which passes each line on at once); confirmed by the user's double-click on the laptop (version, key, addresses, READY shown). The launcher is CRLF in the repo and install_plugin.py writes every .cmd with CRLF. The user's screenshot showed the laptop server's access key: reminded of the AI policy (secrets), advised --new-key.

**Session 10, server version and start-up steps (2026-10-14):** the user: on the server the window still showed only the launcher's lines (Python never printed "starting..."; Windows Terminal, so not a QuickEdit pause; the plugin folder is in OneDrive, which was not running there: a suspect, not confirmed), "Add the version number as well". `version.txt` in the plugin folder (make_pcm_package: version + commit + build time; install_plugin: "development install" + commit), `groundtruth.build_version()`; the launcher shows it before Python starts, then steps [1/3] Python, [2/3] GroundTruth + NumPy imported, [3/3] the server (PYTHONUNBUFFERED), so a stall shows where; the server's start line, /v1/status "version", Test connection and the version refusal name the build. Waiting for the user's run on the server with the new package.

**Session 10, server start-up (2026-10-14):** on the real server the window stayed empty after the launcher's lines (no key: it was printed only on the first start; probably also paused by a click: console QuickEdit). Now `cmd_serve` prints "starting...", the access key at every start, the addresses to enter (`server.local_addresses`: the PC's name and IPv4s) and `serve` a READY line (or why it could not listen); the launcher warns not to click into the window. PyTorch on the server failed with WinError 126 on torch\lib\shm.dll: the Microsoft Visual C++ Redistributable (x64) was missing there (advice given: aka.ms/vs/17/release/vc_redist.x64.exe, then check_gpu.py). Open, asked: the user's answer on mixed precision (single-precision FFT products with double-precision refinement, fallback to double near resonances) before milestone 2.

**Session 10, the server's GPUs (2026-10-14):** the user's compute server (Windows 11 Pro, local network, several users) has **2 x NVIDIA Quadro P2200, 5 GB each, driver 571.59** (CUDA 12.8 capable). Pascal (GP106, sm_61): FP64 is 1/32 of FP32 (~0.12 TFLOPS) and the solver runs complex128, but its GMRES products are FFT- and bandwidth-bound (200 GB/s per card), so the gain over the server's CPU is unknown: MEASURE before milestone 2 (install_gpu.py with the default cu126 build into the plugin venv there, check_gpu.py, then tools/dev/gpu_check.py on kraft: GPU vs CPU). 5 GB limits a model on the GPU (a solve that doesn't fit falls back to the CPU, counted in the result's solver note). Milestone 2 (one worker per GPU) only pays if a GPU beats the CPU cores it would share; otherwise --workers N with CPU threads split.

**Session 10, report and Preferences (2026-10-14, branch `report-dc-combined`, merged and installed at the user's "merge and install"):** (1) the user: add the combined DC map to the report: `html._combined_dc`, first under Power stages (open item 1, done). (2) The user, with a screenshot of the tall Preferences dialog on a laptop: "settings space is tight. make it a double row and add some vertical scroll bars for smaller screens. Divide it nicely and make it look appealing and professional": the groups now sit in two balanced columns (left: Simulation, Compute server, Thermal, Report; right: Heatmaps, Drawing on the board, History, Appearance) on a wx.ScrolledWindow, each column's labels one width, spacing in GAP units, a line above OK/Cancel; `_fit_to_screen` sizes it to the content (1111 x 572 px here, was ~475 x 1030) or to 92 % of the screen height with the pane scrolling, never under MIN_PANE_DIP. Checked by screenshots light and dark, squeezed to 380 px (scroll bar, buttons stay). Found on the way: `tools/dev/window_shot.capture` passed 64-bit handles to GDI without argtypes ("int too long to convert"): argtypes declared.

**Session 10, server help (2026-10-14):** the user: "Under help, in the plugin, add a section that explains how to set up the server and how to run it". Help section "Compute server: set up and run" (install in KiCad on the server, open the PCB editor once for the plugin venv, optional GPU, firewall TCP 8765 private, run, options, use from the laptop, security, updating); `plugin/groundtruth_server.cmd` (double-click launcher: finds `%LOCALAPPDATA%\KiCad\*\python-environments\*groundtruth*\Scripts\python.exe`, PYTHONPATH = its folder, `-m groundtruth serve --log %LOCALAPPDATA%\GroundTruth\server.log`); tested here with the installed plugin and KiCad's venv (Python 3.11.5, NumPy 2.4.2). make_pcm_package writes .cmd files with CRLF. The user asked whether KiCad must run on the server: no.

**Session 10, compute server (2026-10-13, branch `compute-server`, merged into main 2026-10-14 at the user's "Yes", milestone 1 done).** The user:
run the computation on "a very powerful PC, with loads of ram and several GPUs" while working in KiCad on their own
machine; the server runs Windows 11 Pro, on the local network, for several people (GPUs not confirmed yet). Done:
`groundtruth serve` + Preferences "Compute server" (see ARCHITECTURE "Compute server", help "Compute server", README).
Simulate, Suggest fixes, Check in Simulate, Compare what-if, Thermal and Current capacity go to the server; signed
requests, restricted unpickling, FIFO queue, Cancel, fallback to local. Tests: tests/test_remote.py (10 pass), the
GUI test passes; py311_check and undocumented clean. Full suite on the branch: 556 passed, 1 skipped (33 min, after fixing a test that read the freed dc_branch); not tried on the real
server. Next: (2) try it on the server: install the plugin's package there
(same version: the code_id check), `groundtruth serve`, firewall port 8765 (private network), Preferences on the
laptop, a kraft Simulate at 8x; (3) milestone 2: one worker per GPU, the queue in the window; (4) milestone 3: a
Windows service, TLS, a server package and guide, check_server. The user near their weekly limit at this point.

**Session 10, kraft design studies (2026-10-12/13).** Questions about kraft_flux_mini_2's converters, answered with
hand estimates and GroundTruth runs on board copies; nothing in the user's design was changed. Part numbers below
were suggested from web research and general knowledge; the user checks stock and datasheets.
- **Switching frequency, conv1 at 55 V -> 28 V, 10 A** (Resolution 2x + Thermal, copy `out/conv_similar/base/`,
  settings and reports in `out/fsw/`; "exit 1" in its sims.log = issues found):

  | | 300 kHz | 500 kHz | 750 kHz |
  |---|---|---|---|
  | Q301 | 77 °C (1.05 W) | 95 °C (1.64 W) | 117 °C (2.39 W), over 105 °C |
  | Q302 / L301 | 62 / 61 °C | 70 / 68 °C | 80 / 77 °C |
  | Loss / efficiency / I_in | 2.8 W / 99.0 % / 5.14 A | 3.4 W / 98.8 % / 5.15 A | 4.1 W / 98.5 % / 5.17 A |
  | Radiated margin | -16.1 dB | -20.6 dB | -24.1 dB |
  | Conducted margin | -37.1 dB | -38.0 dB | -35.7 dB |
  | Fix list: what passes radiated | 6 ns turn-on edge, +9.4 dB, 0.33 W | 4 ns edge + 0.62 nH loop | snubber + 6 ns + 0.77 nH, +7 W |

  Recommendation given: ~300 kHz, a resistor in TGUP1 (slower turn-on only; overshoot ~40 V -> 2.6 V), an input
  filter (~4 µH + 10 µF at J101, damped), check the LTC7890 ILIM against the higher peak (R309 ∥ R310 = 4 mΩ).
  Only conv1 heated the board in these runs (temperatures a little low).
- **GaN switches** (any pinout, >= 100 V): no part beats the EPC2306 by more than ~5 % at 10 A / 300 kHz; every
  current 100 V part has R_DS(on)·Q_oss ≈ 100-140 mΩ·nC, optimum ≈ 3.4 mΩ here. At 750 kHz a smaller die
  (EPC2204, 4.4 mΩ / 25 nC) saves ~0.5 W. Renesas' new RTP100E005G1FL (5 mΩ, claims -35 % switching loss) is the
  one to check once its datasheet is out. Paralleling two per position adds ~0.5 W at 300 kHz (Q_oss doubles).
  Bigger levers: top-side cooling (insulated for Q301, whose case is the switch node), shorter dead time.
- **SW1 stackup** (SW1 / GND / SW1 / GND / SW1 / GND): keep In1 GND (the hot loop's return 135 µm below); cutting
  the deeper GND under SW1 saves ~30 pF (~0.03 W); the In2/In4 SW1 pours carry current and heat.
- **Output ripple** (user's ranges: stage 1 12-28 V at 10 A, stage 2 5-13 V at 2.5 A, max 0.15 %; 300 kHz):
  the 22 µF 35 V X5R 1206 (C3216X5R1V226M160AC) keeps ~15 % at 28 V (≈ 3.3 µF each, ≈ 20 µF for six); the
  100 µF tantalum C301 barely smooths (70 mΩ) and runs at 80 % of its 35 V rating. Suggested shared BOM:
  L301 -> **Coilcraft XAL1510-103** (10 µH, 6.8 mΩ, Isat 26 A; needs ~5 mm more room: move C304/C315/R309/R310,
  keep Q301/Q302 and the input caps), L302 -> XAL1010-103 (drop-in); 8 × 22 µF 50 V X7R 1210 per stage (TDK
  C3225X7R1H226M250AC) -> stage 2 0.086 % nominal / 0.133 % at the tolerance corner, stage 1 ≈ 0.08-0.09 %.
  The user's InvenTree part **CL32B106KMVNNWE** (10 µF 63 V X7R 1210, ~5-6.5 µF at 28 V): 8 + hybrid bulk ≈ 40 mV
  (0.14 %) on stage 1; a second LC stage (1 µH + a few 4.7 µF 50 V 1206, feedback divider R306 before it) gets
  < 5 mV and also removes the GaN spikes. Ceramics are expensive for the user: low-ESR aluminium polymer
  (~12 mΩ, 4 parts) also beats the big ceramics (~16 mV).
- **Bulk capacitor** (C301 / C340, replacing C_TANT_100u0_35V_7360): 100 µF 50 V hybrid polymer, 10 × 10 mm,
  footprint `CP_Elec_10x10.5` (as C101): **Nichicon GYC1H101MCQ1GS** (4000 h at 135 °C, 28 mΩ, AEC-Q200; the
  longest life found), second source Panasonic EEH-ZT1H101P; EEH-ZC1H101P works (4000 h at 125 °C);
  EEH-ZA1H101P is out of stock. Its job is load steps and damping, not ripple.

**Session 10, DC paths together (2026-10-13):** the user: the ground map looked cut off ("are you saying that the right side is cut off?"), and "When simulating both sides of the converter, the currents are not added together. So one plot shows 5.15A the other says 1A"; plan accepted ("yes"). Branch `dc-board`, merged into main 2026-10-13 (full suite on main after the merge: 545 passed, 1 skipped, 30 min): `power.dc_board_build` (one whole-board copper model per Simulate, cached on the BoardModel, all plane nets plus every stage's `dc_nets`, pitch from DC_BUDGET = 3 x max_nodes x resolution, starting at the dielectric height below the stages' parts
as the region models did: a fixed 0.25 mm start widened the buck test's 1 mm track, its R 16 % low), every stage's DC paths solved on it (`dc_runs`, `_output_runs(dst=)`; the input path now also without a conducted standard), each path's signed branch currents kept (`ConverterResult.dc_branch`, rescaled with the input path), `power.combine_dc`: with two or more stages the vector sum of all paths' densities and the branch currents -> a view `all power stages` / `currents together` (first in the Route list; title with loads, total input current, copper loss) and its current_density / via_overload issues, which replace the stages' own DC-path issues (simulate()). `SimulationResult.dc_combined`. Test `test_the_stages_dc_currents_add_up_on_one_board_model` (two identical stages: combined = exactly 2x). kraft (conv1 10 A 300 kHz + conv2 2.5 A, res 2, out/part_spread/dc_combined.txt): whole-board DC model 503k nodes at 0.21 mm; combined input 5.40 A, copper loss 0.30 W, hotspot 72 A/mm² at (162.9, 55.6) on F.Cu (conv1's ground return), via Net-(U302-Vs+) @(154.80, 76.40) 0.96 A = 101 % of IPC-2221 (warning). Simulate 865 s vs 774 s on main (+12 %). CORRECTION of the region test before it (tools/dev/dc_region_check.py): the ground hotspot fell from 72.5 to 53 A/mm² on the whole board only because that run's grid was coarser (0.40 mm); on the whole board at 0.21 mm it is 71.5 again: the 5 mm box did not squeeze it, the hotspot is real; the box cost ~7 % of the ground return's R. Not done: the combined map in the HTML report (its issues are in it).

**Session 10, contour widths (2026-10-13):** the user: "make a setting for the thickness of the contour lines as well". Preferences, Drawing on the board: Contour line (mm) and Index contour line (mm) (ui.json contour_line_mm / contour_index_mm, `prefs_dialog._contour_widths`, defaults 0.04 / 0.1 as before), passed to `kipy_sink.draw_contours(line_mm, index_mm)`; help table; test in test_contours. Branch `contour-width`, merged and installed at the user's "merge".

**Session 10, parts over their pads (2026-10-13):** the user, on conv1's DC input map: "Pad 3 will take the current through the whole surface but the sim uses a single point it seems" (Q601). True: every lumped part was ONE branch between the first pad's centre node on each side. Branch `part-spread` (merged at "merge everything"): `simulate.part_pairs` / `pad_nodes` / `_spread`: up to MAX_PART_BRANCHES = 8 nodes under all the pads of each side (farthest-point sampled), paired side by side, each branch n·Z; consumers sum a part's branches (simulate.transfers, power.dc_summary, power._cap_shares, bridge_rms). Validation: `test_a_part_is_joined_to_the_copper_over_its_whole_pads` (`boardgen.split_strip_board`): +0.84 mΩ over the joined strip (expected ≈ 0.83), grid-converged (0.05 mm: 0.86); one branch gave 1.45 / 1.55 mΩ. kraft conv1 10 A 300 kHz: input path copper 3.71 -> 3.12 mΩ, density 73/113 -> 47/63 A/mm² (sustained/peak: the Q601 point was the hotspot), output 1.58 -> 1.39 mΩ, 76/119 -> 52/80; ground unchanged (72, real); hot loop 1.548 -> 1.536 nH, radiated -16.1 -> -15.9 dB; Q301 77.4 -> 77.1 °C (out/part_spread vs out/fsw/sim_300k). Results of every board with wide parts move (DC paths most). Full suite on the branch: 540 passed, 1 skipped, 2 GUI stall-timing failures (export report 0.55 s, bridges 0.75 s vs 0.5 s), both pass alone on the merged main.

**Session 10, arrow widths (2026-10-13):** the user: "when drawing current arrows on the PCB, can we change the line thickness of the arrows based on the current density. The contours simply don't work well enough"; a global setting, default min 0.01 mm, max 0.3 mm. Branch `arrow-width` (merged into `main` at the user's "merge everything"): `vectors.arrow_field(with_density=True)` adds each spot's mean density, `vectors.arrow_widths` maps it log-linearly over three decades below the peak of all the result's layers; `kipy_sink.draw_arrows` takes a fifth value per arrow (width) and lengthens the head of a thick arrow (>= 1.5 widths, <= 60 % of the arrow); Preferences "Drawing on the board": Thinnest / Thickest arrow line (ui.json arrow_min_mm / arrow_max_mm, `prefs_dialog._arrow_range`); help. Tests in test_vectors. **Parked by the user** ("let's park that then"): showing the whole board on the Heatmap instead of the solved region (a view of the model region only). Options discussed: (1) display only: axes to the board outline, copper outside the region greyed; (2) solve DC paths over the whole board (slower, more memory; the hot loop's HF models keep their region).

**Session 10, issues per view (2026-10-13):** the user, on the conv1 "DC switch node → L301" map: "why is this via shown causing an issue. we are not actually looking at that path" (the output path's current_density warning at (159.20, 76.10)). Every view of a stage drew all the stage's issues (`i.net == rr.net`). `plots.view_issues`: a DC path view marks only issues keyed `:dc:<path>` / `:via:<path>`, the hot-loop (and a bridge's RMS) view none of the DC paths'; test in test_plot_text; help line. Branch `map-issues-per-view`, merged and installed at the user's "merge and install".

**Session 10, map click (2026-10-13):** the user: after a Simulate, click a marked (overloaded) via on the Heatmap and "it should highlight it on the PCB". Branch `map-click` (installed from the branch at the user's "install it right away, so I can test it manually"; merged into `main` at their "merge"; full suite on main afterwards: 540 passed, 1 skipped, 36 min): `plots.draw_heatmap` records each marker's reference (`fig._gt_mark_refs`, parallel to `_gt_marks`), `plots.mark_ref_at`; `HeatmapPanel(on_mark=)` and `_press` (single left click, no toolbar tool); `MainFrame.on_map_mark` in the highlight thread: `highlight.point_highlight` (the via at the point, else pad, else track on the map's layer; a reference net is not lit), a part marker's footprint, or the issue's own `issue_highlight` when nothing is there; tooltip says "click: show it in KiCad"; help (Heatmap, Click). kraft: the SW1 via at (152.25, 86.00) is found. Tests: test_highlight, test_plot_text, test_gui. Not yet tried in live KiCad by me.

**Session 10, input current (2026-10-12):** the user: the Converters tab's I is the output (load) current; "can you calculate what the input current would be", "put it beside the connector like the resistance and voltage". Branch `input-current`, merged into `main` and installed at the user's "commit, merge install"; full suite on main afterwards: 538 passed, 1 skipped (30 min). kraft (res 2): conv1 I_in 3.09 A (lossless 3.05), 2.06 W lost, 98.8 %; conv2 0.262 A (0.236), 1.39 W, 90.4 % (Q_oss·V·f 1.2 W per high side dominates). `power.input_estimate`: V_in·I_in = V_out·I_out + P_loss, the losses `thermal.part_losses`' (conduction, hard edge, Q_oss, diode V_f, DCR, series parts) + the DC paths' copper (r_copper·I²); no gate drive / controller supply; missing values listed. `dc_runs` solves the input path at it (before the copper is known), `finish_converter` recomputes with the copper and scales the input path's figures (`_rescale_path`, DC is linear). The ground return now splits by Kirchhoff with that I_in (buck: input - pin I_in, low side I_out - I_in); the BOOST shares were swapped before (load D·I_in, low side (1-D)·I_in: fixed, `test_a_boosts_ground_return_splits_by_kirchhoff`, `boost_board(out_connector=True)`). Shown: the DC input view's title "expected input current … A (efficiency … %)", the stage's Heatmap line, a note with the loss break-down; Converters column "I out". Tests: `test_input_current_is_output_power_plus_losses_...` (hand sums). `conv-similar` merged after it (tools/dev/mirror_vias.py; conv2 with conv1's capacitor GND vias: 1.70 -> 1.65 nH, radiated -23.3 -> -22.6 dB, copy in out/conv_similar/).

**Session 10, AMD GPU (2026-10-12):** the user: "Add support for AMD Radeon(TM) 860M Graphics" (GPU). The solver needed no change (PyTorch on ROCm answers through `torch.cuda`; `gpu._find` already tests a complex128 FFT + product). Branch `gpu-amd`, merged into `main` and installed at the user's "merge, install it and provide a new installer for the AMD machine"; the GPU package carries `install_gpu_amd.cmd` (double-click: KiCad's Python runs install_gpu.py --amd, then check_gpu.py; refuses while KiCad runs; zipped with CRLF). `tools/install_gpu.py --amd [GFX]` installs `torch[device-<gfx>]==2.12.0+rocm7.14.1` from AMD's index alone (`repo.amd.com/rocm/whl-multi-arch`; it hosts every dependency, so no PyPI mixing); the chip from Windows' adapter names (`AMD_CHIPS`: 860M gfx1152, 880M/890M gfx1150, 8040S-8060S gfx1151, 740M-780M gfx1103, RX 7600-7900, RX 9060/9070; an 840M is gfx1152 or gfx1153: --amd needs the chip); with no option, AMD is chosen when there is no nvidia-smi and a known Radeon. Checked on the index 2026-10-12: Windows cp311 wheels (KiCad 10's Python) for 2.12.0+rocm7.14.1 exist for all those chips (2.13.0: Linux only); the wheel's METADATA (read by HTTP range) pins its device package and rocm[libraries]==7.14.1. `check_gpu.py` prints the ROCm version and a verdict (GPU not clearly faster: GPU off). Not tried on a real Radeon (none here): an 860M is integrated (shared LPDDR5X, FP64 at a small fraction of FP32), so it may well not beat the CPU; the user should run check_gpu.py there. Tests: `tests/test_install_gpu.py` (dry runs with stubbed cards).

**Session 10, resolution sizes (2026-10-12):** the user: Preferences' Resolution "shows 1x 2x etc which means nothing much to the human user"; append the cell size. It was there already (`prefs_dialog.resolution_label`), but only after a Simulate in the same window. Branch `resolution-cell-size`, merged into `main` and installed at the user's "commit, merge and install": every kept run records its coarsest model's 1x cell (`run.json` `cell_mm`, `history.cell_mm_of`; plugin and `simulate --history`); older runs give it from results.json's grid pitches (`Run.cell_mm`); the History tab reads it with the runs (in its worker) and Preferences uses it until this session's own Simulate. A stackup-only estimate was tried and dropped: kraft 0.135 mm vs the real 0.217 mm (the node budget binds on 6 layers). Tests: test_history, test_gui (`test_preferences_shows_cell_sizes_from_the_history_before_a_simulate`); test_gui + history + cli: two stall-timing failures under load, both pass alone (the bridges one also on main).

**Session 10, contours (2026-10-12):** the user: drawing the current arrows in KiCad "works really well", but the contours were "a bit of a waste of time": too coarse, and one colour (a User layer) so the density could not be read; asked for "little text on the contours like heights on maps". Branch `contour-map`, merged into `main` and installed at the user's "commit merge and install" (full suite on the merged `main`: 531 passed, 1 skipped, 28 min). Then `help-kicad11-note` merged and installed (a boxed help note: writing GT labels into the schematic needs KiCad 11). No unmerged branches. `results/contours.contour_map` replaces the 3 filled outlines (50/20/5 % of the peak, simplified to 400 points, every copper edge redrawn per level) with isolines at round levels (1-2-5 over the heatmap's three decades, decades bold; isotherms in round °C steps, every fifth bold), masked off the copper, Chaikin-smoothed (< 1/4 cell), simplified within 8000 segments, labelled along the line (upright, gap in the line, collisions avoided) plus a legend line; `kipy_sink.draw_contours` draws segments + rotated texts in one group. kraft SPI_SCK In1.Cu (0.217 mm grid): 9 levels 5..2000 A/m per A, 94 labels, ~3600 segments. `tools/dev/contour_preview.py` renders it over the heatmap. Tests: `tests/test_contours.py` (1/r field: circles within 1/4 cell, labels tangent). Not checked live in KiCad yet (closed): the label angle convention (counter-clockwise as seen, y down) and the time KiCad takes for ~3700 items are the things to look at.

**Session 10, latest (2026-10-12):** `signal-paths-tab` and `path-from-selection` merged into `main` (f54cfec) and
installed, branches deleted (full suite on the merged `main`: 525 passed, 1 skipped, 28 min). Then `help-signal-labels` merged and installed (help section "Labelling a signal path": which parts get sig_drv / amp_in, amp_out / sig_rx, how Detect joins them, the kraft ADC example, the steps; the Signal paths info box points to it; a sig or amp label naming a pin its part lacks, or amp_in without amp_out, is now a problem on the tabs' label line, `tags._pin_problems`). No unmerged branches. Not
yet done: a live check of "Add KiCad selection" and the label fallback on the scratch copy
`out/live_sch/kraft_flux_mini_2.kicad_pro` (KiCad was closed); the schematic label write needs KiCad 11 (kipy 0.8.0's
schematic API fails on KiCad 10, so the window shows the labels to add by hand). On the real kraft board the user still
has to Reload board, then Detect… with the two REPLACES rows ticked, to replace the stale ADC entries.

**Session 10, end (2026-10-11):** `fixes` merged and installed (full suite on `main`: 512 passed, 1 skipped, 31 min). No unmerged branches. Before: `code-docs` (docstrings and comments for the whole package, no code change:
see "Code documentation" below) merged and installed; `tools-docs` (the same for `tools/`) merged after it.

**Session 10 (2026-10-11, another account, continuing the handover):** the op-amp work (branch `opamp`, see "Op-amps in signal
paths", on top of `signal-override`: signal labels) passed the full suite (497 passed, 1 skipped, 33 min) and was
merged into `main` with `speed-groups` ("Solved in groups; window stalls" below) at the user's "commit merge and install"; plugin installed (restart KiCad). Full suite on the merged `main`: 501 passed, 1 skipped (27 min). No unmerged branches. The full suite on `main` (the power-stage fix) was run in a worktree at the
start of this session: 483 passed, 1 skipped, 5 GUI failures (stall-timing tests while other runs loaded the machine; all 5 pass alone): `main` is good. The user (2026-10-11): the floating +VDC pour on kraft's In3 is fixed on their board (kraft results move a little from the documented ones); stacked boards and common-mode cable modelling stay parked ("definitely interested but needs a lot more discussion"). The user asked for the ADC labels on kraft ("can you add those?"): `U301=sig_drv:4 U1102=sig_rx:9` (U301 is a TLV6001 buffer, pin 4 its output; R303 4.7 kΩ and C304 100 nF to the ADC pin PA2). Detection now joins labelled nets through 2-pin parts (`_labelled_path`). **Written on 2026-10-11** (`label_project.py ... --backup out/kraft_backup_2026-10-11 U301=sig_drv:4 U1102=sig_rx:9`, schematic sheets dcdc_gan / cpu and the board; the files before in the backup). `groundtruth detect` on the real board: /CPU/ADC_I_RAIL_1 U301.4 -> U1102.9, through R303. Example boards (the user: "move the test PCBs
under examples so user can find them easily"): `examples/` (opamp_path, series_path, rc_filter, termination, each
with its settings; `tools/make_examples.py` regenerates them from tests/boardgen.py; examples/README.md lists them).
`out/` cleaned at the user's request ("clear stale data under /out/"): kept audit (the kraft settings),
converge, dist, kraft_backup_2026-10-07, live; older paths named in this file (out/m3, out/sim*, out/tagcheck,
out/mem, out/kraft_trial ...) are gone: regenerate with the tools when needed.

**Session 9 (2026-10-11):** branch `signal-path` (signal paths across nets, a Thevenin source; see "Signal paths across nets" at the end of "Session 6"), merged and installed; milestone 2 (shunt parts) merged and installed too.

**Session 8, end (2026-10-10), the user near their weekly limit.** `grid-note` merged; the test suite runs in
one process (recommendation 5, done). `memory` merged (recommendation 3, see "Memory: free early"; its full suite in one
process: 469 passed, 1 skipped, 2 GUI failures: an export timing test 0.05 s over its limit under load, passing
alone, and the status-wrap test that fails on `main` too with the virtual display); plugin installed (restart
KiCad). Open: (1) the user's own checks in KiCad (recommendation 6: History's board copy
over the API, a full kraft Simulate watched in Task Manager); (2) board-wide resonances above 1 GHz are not grid-
converged (a note says so; an 8x check needs a bigger machine); (3) models built and solved in groups: done in session 10 ("Solved in groups"). Recommendation 4 (Bridges tab) and the exit-code fix are merged.

**Session 8, later (2026-10-10):** branch `cavity-convergence` (board-wide grid convergence; tracks of plane
nets kept joined on every grid) merged: see "Board-wide grid convergence" at the end of "Session 6"; above
1 GHz still open. Before it, branch `deterministic` (reproducible results) merged into `main` (444bad0) at the
user's "yes", plugin installed (restart KiCad): see "Reproducible results" at the end of "Session 6".

**Session 8 (2026-10-09/10):** branch `history` (from `main` c2b6b99) merged into `main` (77b467e) at the user's
"commit, merge and install" on 2026-10-10, plugin installed (restart KiCad): the simulation history, see
"Simulation history" at the end of "Session 6". Ask the user how it works in KiCad (the live board copy over the
API is untried). Found on the way and then changed at the user's
"Remove the limits, if the user decides thats the value, it's the value" (2026-10-10, same branch): unusual
values no longer block Simulate (kraft_flux_mini_2's L503, DCR 1.33 Ω, did); they are the info issue
`unusual_value` (ARCHITECTURE "Unusual values").

**Session 7 (2026-10-08):** `docs-coverage` (docs ↔ code coverage) and `physics-audit` (the physics audit of the
net and converter simulations, five fixes) merged at the user's "merge, install"; see "Physics audit" at the end
of "Session 6". Open: the kraft_flux_mini_2 rerun with the fixes and the 2×/4× board-wide convergence check.

**Current state (2026-10-07, end of session 6):** `main` has everything below up to "Export report and Vias
shown"; branch `heatmap-hover` (hover over markers, part outlines, the Preferences dialog with resolution /
threads / GPU / thermal quality / report author, remembered toggles, a docs review of README, ARCHITECTURE,
help, CLAUDE.md) is being merged at the user's "update docs etc, commit, merge, install". The newest sections are
at the end of "Session 6": "Bridge milestone 2", "After the merge", "Export report and Vias shown", "Heatmap hover,
part outlines, Preferences, resolution". Open for the user: real motor data is in (55 V bus); still
theirs: resistor models for the bus path's sensors and FET, gate resistors, the conducted standard, the thermal environment, a
memory/time measurement of 8× and above on their server; stacked boards are shelved.

Earlier: session 6 (`main` = also bridge milestone 2, see "Bridge milestone 2"; victims and stacked boards milestone 1, merged at the user's "go
for it", see "Victims and stacked boards"; the code cleanup; the motor bridges milestone 1 and the detection labels / GPU
README, see "Motor bridges" and "Detection labels" below, merged at the user's "commit and merge"; before that: M3 items 1 and 7, switching losses / hover explanations, "Speed
and progress" parts 1 and 2: the status line with time left, drain/source on the power-stage maps, the solver
speed-ups (~40 % faster Simulate) and the fix search's progress, the optional GPU backend ("GPU option"), the
heatmap Log scale, the Parts table fixes and the demo board in the plugin folder ("Heatmap scale"), wheel zoom,
carrying vias, the PCM package, the plane displacement tile and the cross-section ("Plane displacement and the
cross-section", merged at the user's "Merge now"), the 3D view (`view-3d`, merged at the user's second "Merge
now") and readable plot text with a scrolling Heatmap tab (`readable-plots`, merged at the user's "commit and
merge"). No unmerged branches. Open: a GPU measurement on the user's GPU machine; their other open choices in "Next
steps").
**Older reading order (superseded by "Current state" at the top):** read "Session 6" first, then
"Session 5" below (all merged into `main`; its next
steps), then "Plugin start-up" in "Session 4".** Then "Open items". Read
[../CLAUDE.md](../CLAUDE.md) for environment and rules, and [ARCHITECTURE.md](ARCHITECTURE.md) for how the code
works. The user may continue on another Claude account: everything needed is in the repo (the session scratchpad
is gone; reusable scripts are in `tools/dev/`). `main` holds the reviewed state; see "Where things are" for
unmerged branches.

## Rename: pcbemi → GroundTruth (session 4; branch `rename-groundtruth`, merged into `main` on 2026-10-05)
The user asked for a better name and a fancier icon and chose **GroundTruth** and the "loop" icon (a ground symbol
in a glowing current loop, `tools/make_icons.py`; `--preview X` renders all three candidates).
- Python package `groundtruth` (`src/groundtruth/`), commands `groundtruth` and `groundtruth-gui`. The PyPI
  distribution is **`groundtruth-pcb`**: `groundtruth` on PyPI is an unrelated package (an LLM hallucination
  detector), so CI must install `groundtruth-pcb` (or the repo URL), never `groundtruth`.
- KiCad plugin: "GroundTruth – return path / EMI analysis", identifier `io.github.iwers.groundtruth`, entry
  `plugin/groundtruth_action.py`, icons `plugin/icons/groundtruth-*.png`. Remove the old installed
  `io.github.iwers.pcbemi` folder (and its venv under `%LOCALAPPDATA%\KiCad\10.0\python-environments`) when
  installing the renamed plugin, or KiCad shows two buttons. **Done on the user's machine:** the old folder and venv are deleted; the renamed plugin was
  installed and works (button appeared after KiCad built its venv), and was **reinstalled from `main` at c5b63d1
  on 2026-10-05** (spread spectrum included; requirements unchanged, so the venv was kept). Re-run
  `tools/install_plugin.py` after every code change: the plugin is a copy.
- Report files are `groundtruth-report.*`. The repo folder stays `PCB-EMI`.
- **Second rename (session 4, branch `rename-settings`):** the "sidecar" is now the **settings file**,
  `<board>.groundtruth.yaml` (was `<board>.emi.yaml`): module `config/settings.py`, class `SettingsFile`, CLI
  option `--settings`, the plugin's **Save settings** button, `tools/dev/real_settings.py`. The user said the tool
  is brand new, so there is **no backward compatibility**: old `.emi.yaml` files are not read, and Clear EMI
  layers no longer removes groups drawn under the name pcbemi. Obsolete files were deleted.
- Everything below that mentions `groundtruth`, "settings file" or `.groundtruth.yaml` was written as `pcbemi`,
  "sidecar" or `.emi.yaml` before the renames (the docs were updated by a global replace; commit messages before
  the renames still use the old names).

## Where things are

**2026-10-13:** every session-10 branch is merged into `main` and deleted; there are no unmerged branches.

**`main`** holds the reviewed state. `section-3d` (plane displacement tile and the cross-section, milestone 1 of
the user's "Both") was merged (fast-forward) at the user's "Merge now" on 2026-10-06 and deleted, and so was
`view-3d` (the 3D view, milestone 2, and the GUI-stall fix) at their next "Merge now", and `readable-plots`
(larger plot text, the scrolling Heatmap tab) at their "commit and merge". `gpu-torch` and `heatmap-scale` (session 6, see "GPU
option" and "Heatmap scale") were merged (fast-forward) on 2026-10-06 and deleted. `sim-speed` (session 6: solver speed-ups and the fix search's progress, see
"Speed and progress", Part 2) was merged (fast-forward) on 2026-10-06 and deleted. `speed-progress` (session 6, its finished first part)
was committed and merged (fast-forward) at the user's request "commit and merge anything that is left" and
deleted; its open items are listed in "Speed and progress" (start them on a new branch from `main`). `switching-losses`
(session 6) was committed and merged (fast-forward, 5193621) at the user's request "commit and merge" and deleted.
`m3-thermal` (session 6) was merged (fast-forward, 8d99984) and deleted on 2026-10-05; before that session 5's `gui-responsive`, `add-selected-net` and
`issue-highlight` were merged (fast-forward) and deleted on 2026-10-05, and so were `thermal-plan`, `m1-theme` and
`m2-thermal` (M1 + M2 of THERMAL_PLAN.md, `main` at 3c30bb2, at the user's request "merge"). The earlier feature branches were
merged (fast-forward, linear history)
and then deleted at the user's request on 2026-10-05: `m4-multilayer` (M4, dffd167), `m5-fixes-capacitance` (M5
and session 3 step 1, 87b802f), `m6-power-loops` (M6 power stages, ec6b2a1), `rename-groundtruth` (the GroundTruth
rename, spread spectrum, real operating points, e2bcf21), `rename-settings` (the settings-file rename and the
heatmap layout fix, 4657908), `current-arrows` (current-density arrows, ee87e2e), `plugin-startup` (plugin
start-up, see "Plugin start-up"; merged at the user's request before the full suite was confirmed). Their commits are all in `main`'s history. Start new work on a new feature branch
from `main` and ask before merging. No remote is configured: this repo is the only copy.

## Session 6 (2026-10-05, new account): M3 started

- **Plugin venv:** after the KiCad restart, KiCad DID install the new requirements into the existing venv
  (`%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.iwers.groundtruth` has numba 0.68, pyamg 5.3, psutil
  7.2): KiCad 10 re-runs requirements.txt for an existing venv. The Thermal button itself is not yet confirmed by
  the user.
- The user said "keep working on it" to the M3 list; Claude took its own suggested order: 7 (memory watchdog),
  1 (current capacity), then 5/6 (part bodies, enclosure). **`m3-thermal`** (from main 27a220c; full suite 279 passed, 1 skipped; merged into `main` at 8d99984 at the user's request "merge", deleted, plugin reinstalled: restart KiCad):
  - **Memory watchdog** (`thermal_proc._Watchdog`, `ThermalMemoryError`; ARCHITECTURE "Memory watchdog").
  - **Current capacity** (`thermal.capacity_inputs` / `solve_capacity` / `run_capacity`,
    `thermal_proc.capacity_in_process`; Heatmap tab button **Current capacity**, `simulate --capacity`, setting
    `thermal.capacity_c`; ARCHITECTURE "Current capacity", README "Temperatures"). Per stage plus "all power
    stages" scaled together. Two passes (draft locate, then thermal.quality with the mesh planned at capacity).
    `solve_thermal` was split into `_build_model` / `_coupled` / `_result` (the plain Thermal result is unchanged:
    kraft_flux_mini_2 draft 143.1 °C before and after).
  - Tests: tests/test_thermal.py (watchdog, linear √ law, cross term, loss quadratics, fresh-solve check,
    junction limit, bounds, all-together), test_ui_parts (tiles), test_cli (--capacity), test_gui (button).
  - `gui_probe.py --simulate --thermal --capacity` (new flag) on kraft_flux_mini_2 with SW1+SW2 at draft
    (`out/m3/probe.groundtruth.yaml`): Current capacity 136-150 s, worst stall 0 / 234 ms (a drawn button's paint
    while a heatmap rendered: the known GIL kind); everything else as before. **The first probe run crashed with
    exit code 139 at its teardown** (after all steps and the report; `f.Destroy()` + `wx.Yield()` outside the
    main loop); the rerun with `-X faulthandler` and a probe without Simulate both exited 0. Not reproduced;
    if it recurs, read the faulthandler stack (probably a background render's CallAfter landing after the app).
  - `tools/dev/thermal_check.py --capacity [--limit C] [--cache FILE] [--theta Q301=2] [--linear]` (the cache
    keeps Simulate's stages: kraft_flux_mini_2 SW1+SW2 Simulate takes 211-270 s).
  - Real board (SW1+SW2, ASSUMED 4 mΩ / 5 mΩ, real operating points; `out/m3/capacity_real*.txt`): at the
    operating point Q301 reaches 143 °C (draft) / 147 °C (normal), so both per-stage capacities are at or near
    0 A (each stage alone keeps a part past 105 °C with the other at its operating point; SW1 1.1 A at draft vs
    0 A at normal: ill-conditioned, Q303 at 105 vs 107.8 °C with SW1 off). **All power stages together
    (normal): 44 % of both loads (SW1 4.42 A, SW2 2.21 A), limited by the copper at Q301 at 104.98 °C; a fresh
    solve at that load gives 104.99 °C.** 493 s for both passes and three searches (SW1, SW2, all).
- **The user's question about current arrows** ("in general I do agree with the sim results but in this
  screenshot the current seems to move in places where it should not be", screenshot of arrows drawn on the board
  around the LM5008 corner, x 141-166, y 94-113 mm). Investigated with the new `tools/dev/current_where.py`
  (current per view/layer/net in a box) and the SW1 hot-loop maps (`out/m3/sw1_hot_*.png`): it is SW1's 100 MHz
  hot-loop view. Findings told to the user:
  1. The +12V feed to the LTC7890 runs on B.Cu (0.2 mm) straight down between the switch columns (x ≈ 142.5),
     then to the via (151.6, 97.3) and the In2.Cu +12V pour to C522/C524/C525. With C330 (+12V/GND at the
     controller) it forms a loop under the hot loops: 6.3 % of SW1's loop current in C330, 6.3 % out through
     C522+C524+C525 (a closed induced loop, not leakage; real +12V noise coupling).
  2. 95 % of SW1's loop current goes to the LEFT input caps (C318-C332, x 122-134), ~1 % to the right ones next
     to SW1 (C324, C326, C320 …): no input cap has a GND via; the loop reaches the inner GND only through
     J101.1's barrel next to the left group (session 4's finding). C101/C102 take 6 % through the circled copper.
  3. Display: an arrow spot's fill is √(density) with a 1 % floor and uses density, so a 0.2 mm track with 6 %
     of the current looks like a plane with half of it.
  Offered (no answer yet): a what-if with GND vias at the input caps' GND pads (expect the loop to close at
  C324/C326 and the +12V current to drop); calmer arrows (proportional, or only the stage's own nets with a
  checkbox for coupled currents); list caps not between rail and ground (C330, C522 …) as "coupled", not as
  input capacitors, in the hot-loop tile.

- **Switching losses, Q_oss, hover explanations, form layout** (branch `switching-losses`, from main ce51c19;
  committed after testing at the user's request "commit and merge", merged into `main` at 5193621 and deleted; the
  plugin installed in KiCad is exactly this version, plus a stray unused copy of `ui/progress.py`).
  The user asked what the edge time is and how f_sw affects the simulation (answered: spike L·I/t, spectrum above
  1/(π·t), turn-on overlap loss; f_sw: harmonic positions, capacitor sharing at f_sw, both switching losses;
  not the DC currents or the loop inductance), then sent the EPC2306's dynamic table (C_OSS 482 pF, C_OSS(ER)
  593 pF, C_OSS(TR) 767 pF, Q_OSS 38 nC at 50 V; Q_GD 0.8 nC, Q_GS 4.1 nC, Q_G(TH) 3 nC, R_G 0.4 Ω, Q_RR 0)
  and said "yes, if possible add some mouse hover over explanations". Done:
  - `ConverterSpec.q_oss` (settings `q_oss: 38nC`, plausibility 1 pC-10 µC, Converters form field).
  - `part_losses`: the hard-switched transistor (buck high side, boost low side; a boost's diode is its high
    side now) gets ½·V·I·t·f per hard edge (synchronous one, diode two) and Q_oss·V·f (diode ½·Q_oss·V·f),
    C_oss·V² in place of Q_oss·V without q_oss. Before: two edges and C_oss·V²·f always, on the high side even
    for a boost. SW1 with the user's values (52.8 V, 5 A, 750 kHz, 2 ns, 3 mΩ, 6.3 mΩ, Q_oss 38 nC): Q301
    ≈ 0.10 + 1.50 + 0.04 = 1.65 W (was 1.44 W with C_oss 482 pF and two edges).
  - Hover explanations on every Converters and Nets field (label and field), and on their buttons
    (`FIELD_TIPS`, `BUTTON_TIPS`, `NET_TIPS`, `NET_BUTTON_TIPS`).
  - The clipped buttons in the user's screenshot (Converters tab on a laptop screen: "Mark reviewed" and
    "Detect…" cut in half, a stale "move selecte" picture): the form now scrolls (`form_win`, a ScrolledWindow
    with the form's width as its minimum; the list's minimum width is 320 px, it scrolls sideways) and the
    buttons stay under it; drawn buttons repaint on resize (`FULL_REPAINT_ON_RESIZE`); both panels repaint when
    their buttons re-wrap; no stretched last button in a row. Checked with `window_shot.py --size 1100x640`.
  - Found on the way: Update selected dropped a stage's spread-spectrum settings (not on the form); now kept.
  - gui_probe (no Simulate): one stall, 192 ms in `_build_pages`. `build_profile.py`: the Converters tab's build
    step takes ~200 ms here, and 200-280 ms on main (Nets 156-166 ms on main, ~115 ms now): over the 150 ms
    budget before this change too (its form grew in M2). Not fixed; the fix would be to build its form in a
    second event-loop step.
  - **The user changed kraft_flux_mini_2 to 6 copper layers** (saved 21:33, open in their KiCad): the capacity
    and arrow results above are from the 4-layer version (`out/m3/stages_sw12.pickle` is stale).

### Speed and progress (branch `speed-progress`: the "Done" part merged into `main`; the "Not done yet" part is open)
The user, right after "commit and merge" of `switching-losses`: "go through the plugin code and see if you can make
it more efficient/faster to run simulations. Check if you can speed up the 'suggest fixes' option for the what-ifs.
Currently its very slow and it's not clear from the loading bar how far it has got at any moment and how much more
is to do. This is the case in general when somethings loads. Also, in the heatmap, display the drain and source
locations better." Branch `speed-progress` from main 5193621. The session ran out of context mid-task; the user
then asked to "update docs ... for handover to other claude" and "commit and merge anything that is left", so the
finished part below was committed, merged into `main` (fast-forward), the branch deleted and the plugin
reinstalled (restart KiCad). Tests: the full suite gave 282 passed, 1 skipped and **4 GUI timing failures**
(`test_jobs_run_off_the_gui_thread` 0.72 s stall, `test_plugin_start_never_blocks_its_window[answers slowly]`
0.72 s, `test_a_new_board_gets_its_power_stages_detected` 0.52 s, `test_the_window_builds_its_tabs_in_steps`
Converters step 0.78 s) in a run that took 17 min instead of ~10 (the machine was loaded; the Converters build step
is not touched by this change). All four passed alone twice, and all 32 of `tests/test_gui.py` passed in one run
(141 s). If they fail again in a quiet full run, suspect the status line first: `_wrap_status` calls
`SendSizeEvent` (a whole-frame layout) whenever the wrapped line count changes, and the longer line with the
time can flip between one and two lines; the fix would be to show the time part in its own status field. Python
3.11 parse check of the changed files with KiCad's interpreter: ok. `gui_probe.py` without `--simulate`
(kraft_flux_mini_2, `out/m3/probe.groundtruth.yaml`): one stall, 165 ms in load + auto-detect (the tab build, as
before), none in Run checks / tabs / Save. The status line has not been watched during a real Simulate in the
plugin yet.

**Done (merged):**
1. **Status line with elapsed time and time left, for every job** (`ui/progress.py`, new, no wx): `ProgressClock`
   holds the job's message, fraction and start time; `line()` reads "msg · 37 % · 1:12 · about 2 min left" ("almost
   done" under 1 s; no estimate before 3 % and 5 s). The fraction never goes back (monotonic); the time left is the
   running estimate counted down between reports and blended (SMOOTHING 0.3) with t·(1−f)/f at each report.
   `MainFrame`: `_clock` + `_clock_timer` (1 s, so the elapsed time keeps counting through a long single step;
   stopped in `_finished` and in `_stop_timers` on EVT_WINDOW_DESTROY), `_start` / `_progress` / `_tick_clock`; the
   gauge follows `_clock.frac`; after a job of ≥ 10 s the status line ends with "· took m:ss". Tests:
   tests/test_progress.py (2), tests/test_gui.py `test_a_running_job_shows_how_far_it_is_and_how_long_it_has_run`.
2. **Drain and source on the power-stage heatmaps** (`power.switch_pins`, `ConverterResult.switch_pins`, kept out
   of the JSON summary; `plots._switch_pins`, `PIN_ROLES`, `_pad_outline`, called in `draw_heatmap` after `_route`):
   told from the pads' nets, no schematic pin names needed (high side: rail pad = D, switch-node pad = S; low side:
   switch node = D, ground = S; a diode K/A; an integrated stage VIN/SW/GND). Pads outlined on their own layer
   (cyan D/K/VIN, green S/A/GND, pink SW, with a dark halo), a role letter per (part, role) group and the part's
   name above its pads on every layer, legend "D = drain" etc. Test: tests/test_vectors.py
   `test_power_stage_maps_mark_the_switches_drain_and_source`. Not on the thermal views (optional). Not yet looked
   at on the real board (render SW1's maps with `window_shot.py --tabs Heatmap` or the CLI's HTML report).
3. **`tools/dev/profile_sim.py`** (new): cProfile of `simulate` (and `--suggest`: the fix search from that
   result, as the plugin does), the longest gaps between progress messages, the bar against the time, top functions
   by cumulative / own time; `--only`, `--threads 1` (cProfile sees only the calling thread: solver threads show
   as waiting), `--out FILE.prof`.

**Part 2: branch `sim-speed`** (from main ff2de83; the user: "Once merged. Continue to go through the plugin code
and see if you can make it more efficient/faster to run simulations ... >>>> See if we can utillise the GPU for some
sim calculations! <<<<<"). Details and measurements in ARCHITECTURE ("Formulation", "Reuse", "Compiled
multiply-add", "Parallel solving", "Fix suggestions"). Where the time went (kraft_flux_mini_2, 6 layers, SPI_SCK,
`tools/dev/profile_sim.py` / `solver_bench.py`): Simulate 139 s, of which 8 board-wide factorisations 103 s
(12.8 s each); a route-model iteration 110 ms (FFTs 20 %, layer-kernel multiply-add 17 %, Gram-Schmidt 25 %,
sparse products 11 %, triangular solves 11 %). Done:
1. **SuperLU `diag_pivot_thresh=0.1`** in symmetric mode (`LU_PIVOT_THRESHOLD`): board-wide factorisation 11.4 s →
   3.4 s, fill 10.4 → 5.8 M, preconditioner solve 31 → 21 ms, same residual. The single biggest win.
2. **GMRES** with Givens rotations (lstsq per step cost 18-44 ms at j = 120-200), BLAS `zgemv` Gram-Schmidt, and
   `max_its`.
3. **Borrowed factorisations with a budget** (`_Operator.borrow_budget`, `src`, `its`, `t_it`, `t_fact`): past
   1.5 × the source's home iterations + half a factorisation, factorise at f and continue from the solution so far.
   A wider borrow window (±60 %) only when a factorisation costs ≥ 100 iterations (`REUSE_FAR_ITERATIONS`); at 20 it
   made two borrows above 250 MHz take 2x the iterations on the real board (see ARCHITECTURE "Reuse").
4. **Compiled layer-kernel multiply-add** (`solver/_mac.py`, numba, serial + nogil; numpy fallback, `USE_NUMBA`)
   and flat strip indices: 9.4 → 3.3 ms per product, bit-identical.
5. **Fix search**: verification only for what the fixes can change (`verify_scope`, `SuggestResult.kept`, shown
   as "not simulated again …"; `--verify-all` for the old full run; rule checks of the kept nets re-run on the
   modified board), a phase plan for the bar (`analysis/progress.py` `Phases`, "k of n" in every message,
   `compare_results(progress=)`), Simulate's progress weighted by model size with model building and finishing
   as their own steps (`solve_cost`, `solve_setups(progress, rest)`).
6. Status bar does not shrink while a job runs (`_wrap_status`): every height change laid the window out again.
7. Deterministic order of a route's transfers (`finish_route`): C1105 and its +3V3 via carry the same current to
   1e-15 and noise picked which one the return_detour message named.
8. Dev tools: `solver_bench.py`, `result_diff.py`, `gpu_probe.py`; `profile_sim.py` counts factorisations/iterations.
**Results unchanged** (main in a `git worktree`, `result_diff.py`, `groundtruth simulate --nets /CPU/SPI_SCK "buck
SW1"` on the 6-layer board, JSON in `out/m3/cmp_main`, `cmp_branch2`): 2282 numbers, largest difference 0.00003 dB,
same harmonics, same iteration counts but one (96 vs 93), identical issue texts. **Time 283 s → 167 s** (with the
±60 % window at 20 iterations it was 178 s and two solves took 2x the iterations). SPI_SCK alone 139 → 76 s.
**GPU (the user's question):** this laptop has an Intel UHD 620 (OpenCL 3.0, fp64 yes, 24 units, shares the RAM's
bandwidth with the CPU); no NVIDIA/CUDA. `tools/dev/gpu_probe.py` (pyopencl in the dev venv only), the layer-kernel
multiply-add per product: route model 231x264: numpy 10.5 ms, numba 4.4 ms, GPU kernel 2.0 ms but 3.6 ms with the
copies; board-wide 120x210: numpy 4.2, numba 1.8, GPU 1.0 / 1.7 ms. The copies cannot be avoided: the
preconditioner (sparse LU solves, CPU-only work) runs every iteration, and the factorisations (the biggest cost
before item 1) are not GPU work at all. No GPU FFT library installs on Windows/Python 3.13 (pyvkfft has no wheel).
Verdict given to the user: not worth it on this machine. On a PC with an NVIDIA GPU, an optional CuPy path for the
FFT convolution, multiply-add and Gram-Schmidt might make an iteration ~1.5-2x faster (the preconditioner stays on
the CPU), at the price of a ~1 GB CUDA dependency in KiCad's plugin venv; offered, not built.
**The bar against the time** (`profile_sim.py --only SPI,SW_CLK --suggest`, 3 nets, `out/m3/branch_suggest3.txt`):
Simulate 157 s, and the bar now follows the clock (at 52 % of the time it showed 52 %, at 74 % 74 %; before,
all model building sat at 0 % and the bar ran ahead or behind by solve count). The fix search from that result took
38 s: 9 candidates placed, 3 SW_CLK stitching vias evaluated (9 solves, 3 factorisations), none selected, so no
checks and no verification. That run showed two flaws, fixed since: parallel steps reported at their start (all
three "k of 3" at once, then 37 s without a move; steps now move the bar when they finish, `with ph.step(...)`),
and the plan gave the conditional later phases 90 % of the bar (they now count half, evaluations at full cost).
**Tests and probe:** full suite 293 passed, 1 skipped (11 min); the final run before the merge had 293 passed and
`test_jobs_run_off_the_gui_thread` at 0.57 s (limit 0.5; it passed alone 3x and in all 32 of `tests/test_gui.py`).
**Merged** into `main` (fast-forward) at the user's choice "Commit + merge first" (2026-10-06), branch deleted; the
plugin was installed from it ("also install the plugin first"): restart KiCad. Python 3.11 parse ok. `gui_probe.py --simulate` (SW1+SW2 at draft, `out/m3/probe.groundtruth.yaml`;
logs `out/m3/probe_sim2.log` branch, `probe_sim_main.log` main from the worktree): **Simulate in the plugin 164 s on
the branch vs 281 s on main**; stalls over 150 ms 3 vs 7, worst 360 ms (three threads in `splu`) vs 418 ms (in
`_tick_clock` → `_wrap_status`); the end-of-run heatmap paint 292 vs 245 ms; tab build 169 vs 176 ms.
**Exit crash (pre-existing, intermittent; found and fixed in session 10: the probe's own, see "Simulate's multi-second stalls"):** the probe ended with exit code 139 on main (faulthandler: "Garbage-
collecting, no Python frame", during interpreter shutdown after the report) and in the first branch run (a worker
thread in matplotlib's `font_manager`); the second branch run exited 0. Daemon `LatestOnly` workers still inside
matplotlib at shutdown are one cause: `ui/background.py` now waits up to `EXIT_WAIT_S` (5 s) for them in an
`atexit` hook (test: `test_running_work_finishes_before_python_exits`). Not confirmed to cure it (one probe run
takes 4 min and the crash is intermittent); the GC kind may be wx objects collected after the app. If it recurs,
check whether the plugin itself (not only the probe's teardown) crashes when the window closes.
**Not done:** the fix search with selected fixes timed on the real board (a net whose fixes get selected; on the
4-layer board SPI_MOSI's did).

### GPU option (branch `gpu-torch`, from main 5745375)
The user, after "not worth it on this laptop": "We should have an option for GPU though, this laptop is used for
development but the plugin will run on a laptop with a much bigger GPU or even a big server." Their answers: GPU
vendor "Mixed / not known yet", library "PyTorch (Recommended)" (the same code runs on the CPU, so it is tested
here), sim-speed "Commit + merge first" (done, main 5745375, plugin installed from it). Done on the branch:
- `solver/gpu.py`: `probe(mode)` (cached; CUDA, i.e. also ROCm builds; a device must pass a complex128 FFT +
  matmul), `use(mode)` → `Run` (device, fallbacks, first error; `summary()`, `describe()`), `TorchMath` /
  `TorchVectors` (the device side of a solve), `release`, `derived`. peec.py never imports torch: `PlaneStack.solve`
  tries the device inside `gpu.use(...)` and falls back per solve; `_solve` and `_gmres` are shared by numpy and
  torch (`_HostMath`, `_HostVectors`). ARCHITECTURE "GPU backend" has the design.
- Setting `analysis.gpu` (auto default | on | off; YAML's bare on/off read as booleans are accepted), CLI `--gpu`
  on simulate, suggest and compare, CLI prints "solver: GPU <name>" (stderr) when the GPU was used; `simulate`,
  `suggest_fixes`, `aligned_worst`, `sharper_view` run inside `gpu.use`; `SimulationResult.solver`,
  `SuggestResult.solver`; the plugin's status line ends with " · solved on GPU <name>" (`main_frame.solver_note`),
  `GpuUnavailable` is an expected error there and in the CLI.
- `tools/install_gpu.py` (for the GPU machines: pip installs the CUDA build of torch into the KiCad plugin venv or a
  given Python, then checks it; `--cuda`, `--dry-run`), `tools/dev/gpu_check.py` (GPU vs CPU on a real board),
  pyproject extra `gpu`. The dev venv has torch 2.14.1+cpu (not the plugin venv).
- Tests: `tests/test_gpu.py` (5, on PyTorch's CPU device): device products = numpy to 1e-12; Simulate with the
  board-wide model and the fix search identical to the CPU; an out-of-memory solve falls back and is counted;
  modes without a GPU, the setting, the CLI's error.
- Checks: full suite 299 passed, 1 skipped (12 min); Python 3.11 parse ok; `gpu_check.py` on kraft_flux_mini_2
  SPI_SCK with `GROUNDTRUTH_GPU_DEVICE=cpu` (`out/m3/gpu_check_cpu.txt`): the device path gives the numpy results to
  7e-12 dB (394 numbers, same issues); PyTorch on a CPU is 0.53x numpy's speed (a code check, not the target).
  Nothing committed on the branch yet (ask the user before committing and merging).
- The user asked during this work why the current spreads so much in a heatmap "almost like magnetic field
  lines" (a test-suite window: `test_gui`'s layer-change board at 150 MHz). First answer: plane capacitance spread
  over the electrically small board. **Corrected** after checking: that GUI test runs without plane capacitance
  (conftest's default), so the spread is the return detouring to the only In1–In2 connections, the ICs' GND vias
  at x = 5 and 35: on In1 it runs under the F.Cu trace to x = 20, then fans out over the whole plane to U2's via
  at x = 35 (no trace above pulls it into a band); source-to-sink flow in a sheet, hence the field-line look; the
  log colour scale over 3 decades shows even the weak spread. With plane capacitance on, displacement current adds
  the same kind of spreading. The user then said yes to the stitched/unstitched picture and a linear colour scale
  (branch `heatmap-scale`, below).
- At the user's request "update the examples/gitlab-ci script to reflect the current settings, and my kicad
  kraft-flux settings yaml" (2026-10-06):
  - `examples/gitlab-ci.yml`: PyPI name groundtruth-pcb, pip cache, the settings that matter in CI, simulate with
    --verbose, JUnit/Code Quality reports and the baseline, new manual jobs emi-thermal (--thermal --capacity,
    [thermal] extra), emi-suggest, emi-simulate-gpu (a `gpu`-tagged runner, PyTorch cu126, --gpu on).
  - The user's own `github/kraft_flux_mini_2/kraft_flux_mini_2.groundtruth.yaml` (edited only because they asked;
    the GroundTruth window was not running): only the missing keys added with their defaults, through
    SettingsFile round-trip (`analysis.gpu: auto`, `analysis.via_plating_um`, `rules.via_load_warning/_error`, a
    `thermal:` and a `drawing:` section with comments); every existing line unchanged (diff checked); main's
    installed plugin reads it (ignores `gpu`). Their file has no nets and one converter (SW1, their values),
    Q601 still `short`, `conducted: cispr32` although session 4 named MIL-STD-461 their standard: not changed
    (their decisions; mention if relevant).
**Not done / to tell the user:** no speed measured on a real GPU (none here); they run `tools/install_gpu.py`
then `tools/dev/gpu_check.py BOARD SETTINGS --only SPI_SCK,SW1` on the GPU machine and send the output. Expected:
the iterations several times faster, the factorisations unchanged (CPU), so the gain depends on the factorisation
share (board-wide model ~1/3 of a solve after sim-speed). CUDA context start-up on the first solve not measured
(a possible GUI stall in the plugin: check with gui_probe there).

### Heatmap scale (branch `heatmap-scale`, from main 8a0f18b; `gpu-torch` was committed and merged at the user's "Yes")
- `draw_heatmap(scale="log" | "linear")`: linear = Normalize(0, peak), noted in the title; temperature views stay
  linear (°C). Plugin: **Log scale** checkbox next to Current arrows (greyed out for thermal views). Tests:
  `test_heatmap_draws_arrows_and_keeps_the_colour_bar_clear` (norms), a step in
  `test_converters_tab_adds_a_stage_and_simulate_shows_its_hot_loop`.
- The Heatmap tab's control row is a WrapSizer now (Route/Layer labels kept with their lists, Route list at least
  220 px): at 1100 px it just fit before a run, and the Route list grows to the longest view name after one, which
  would have pushed the last buttons out (window_shot at 1100 and 860 px checked; gui_probe: one 204 ms stall in the
  tab build, as before).
- `tools/dev/stitch_contrast.py` → `out/stitch_contrast.png`: the layer-change board without / with a GND via 1 mm
  from the signal via, log and linear (without: L 15.83 nH, 1.08x ideal; with: 15.17 nH, 1.03x; peak density
  ~1200 → ~3800 A/m per A, crowding into the via). Shown to the user.
- `ui/background.py`: the exit hook now drops waiting requests and only waits for the running one (`finish`);
  running queued renders after the app was gone printed "No wx.App created yet" from the render thread at the end
  of a GUI test. Errors in `on_error` itself are swallowed.
- Committed at the user's "commit oustanding files" (0be2a67, not merged). Then, on the same branch:
  - **Parts table fills the tab** (the user, with a screenshot of the narrow columns: "expand the table across the
    whole plane"): `parts_panel._fit_columns` spreads the spare width over Value / Footprint / Note (STRETCH
    1:2:3) on every grid resize; the Note column starts at most NOTE_MAX 360 px (Q601's ~180-character note made
    the whole table scroll sideways).
  - **KiCad showed "Plugin action 'Analyse return paths': ..\..\src\msw\window.cpp(539): 'SetFocus' failed with
    error 0x00000057"** (the user, after editing Q601 to a 6.5m resistor in the Parts tab). Cause (most likely):
    every cell edit rebuilt the whole table (`wx.CallAfter(self.refresh)`: DeleteRows/AppendRows), destroying a
    cell editor the user had meanwhile opened on the next cell; wx's focus restore then failed. Fixes: an edit
    updates its own row in place (`_update_row`, `_fill_row(new=False)` keeps the Model cell's editor); a full
    rebuild first closes an open editor (`DisableCellEditControl`, keeps its value); and the plugin's wx log goes
    to `plugin.log` (`app._LogToFile`, set in `plugin_main` around the main loop): on stderr KiCad pops every wx
    message up in a Messages dialog. Test: `test_parts_table_fills_the_tab_and_an_edit_updates_its_row_in_place`.
  - **Demo board in the plugin folder** (the user: "I would like that test board to be a demo board inside the
    plugin folder"): `tools/make_demo.py` writes `plugin/demo/` (checked in; install_plugin copies `plugin/`):
    `groundtruth_demo.kicad_pro` (minimal, KiCad fills it in), `.kicad_pcb` (layer_change_board(None), zones
    filled and saved by kicad-cli), `.groundtruth.yaml` (SIG U1.1→U2.1, 50 MHz, 1 ns, reviewed; default analysis,
    plane capacitance on) and `README.txt` (what to try). CLI simulate of it: 61 s here, 1 error
    (layer_change_stitch), 3 warnings (edge_proximity x2 at the via's antipads, plane_resonance +19.4 dB at
    1450 MHz). Test: `tests/test_demo.py`. Opening it from the plugin folder makes KiCad write its files there
    (prl, backups); a reinstall resets the demo.
  - Full suite (alone; two suites run in parallel ran out of memory): 301 passed, 1 skipped. The plugin was
    installed from this branch's working tree (demo, Parts fixes, Log scale) so the demo is in the plugin folder.
    **Committed and merged** into `main` (fast-forward) at the user's "commit an merge" (2026-10-06), branch
    deleted; the installed plugin is this code (restart KiCad).

### Bug: Suggest fixes "matmul: dimension mismatch (n,k=59814),(k=59815)" (branch `fix-derived-borrow`)
The user hit it in the plugin on the demo board (opened from the repo's `plugin/demo`). Cause, from sim-speed:
`_factorise` of a stack derived with `with_branches` (a fix candidate: one more branch) set its operator's `src`
to the base's borrow source, for the borrow budget; but `operator()` borrows from `o.src or o`, so the derived
stack's next frequency borrowed the BASE's factorisation (one branch smaller). It only happens when the base itself
has borrowed at those frequencies, i.e. after `drop_factorisations` (the plugin frees them after Simulate; the
fix search builds them again): CLI runs never showed it. Fix: `_Operator.home` (another stack's source, for the
budget only); `src` is always an operator of the same stack. Regression test
`test_a_derived_stack_borrows_only_its_own_factorisations` (fails on the old code with the user's ValueError).
Checked on the demo with the plugin's sequence (simulate, drop_factorisations, suggest from that baseline): the GND
stitching via selected (−22.5 dB predicted), layer_change_stitch and plane_resonance fixed, nothing new.
Also: `tools/install_plugin.py` no longer copies KiCad's working files from `plugin/` (lock, .kicad_prl, backups:
the user had the repo's demo open in KiCad), and `.gitignore` lists them. The demo's `.kicad_pro` in the repo was
expanded by the user's KiCad (left uncommitted: their open session).

### Wheel zoom (branch `wheel-zoom`, from main 53541a2; `fix-derived-borrow` merged at the user's "Yes, merge")
The user (screenshots of the stitched demo's maps): "it would be good if we could use vector graph that is
zoomable to present this heat map". Asked: wheel zoom in the plugin vs SVG export vs both → **"Wheel zoom in the
plugin"**; hiding layers under 5 % → **"Keep showing them"** (unchanged). Done:
- `draw_heatmap(view=(x0, x1, y0, y1))`: maps at those limits; `_arrows(view=...)` lays the arrows out over the
  cropped grid (a SimpleNamespace with node/d/x0/y0 is all arrow_field needs), n_across over the view, so zooming
  in shows more arrows; the colour scale stays the whole map's.
- `results_panels`: (on the tab's scrolling page since `readable-plots`: Ctrl + wheel; the wheel alone scrolls)
  `ZOOM_STEP` 1.25 per notch, `zoomed()` (about the cursor, shifted to stay on the board, None
  when back to the whole map), `HeatmapPanel._scroll` / `_press` (double-click) / `reset_zoom`, `ZoomToolbar`
  (Home also resets), shared by the tab and "Open in window"; the view resets on another route, stays on a layer
  change. A zoom re-renders only the maps (`redraw(maps_only=True)`) through the existing background renderer
  (LatestOnly: notches faster than renders coalesce; while a render is pending the next notch builds on the view
  asked for, not the one on screen). The toolbar's own rectangle zoom and pan still draw on the GUI thread.
- Tests: `test_a_zoomed_heatmap_shows_its_view_with_arrows_laid_out_for_it`,
  `test_the_mouse_wheel_zooms_the_heatmap_and_a_double_click_shows_it_all`; gui_probe has wheel-zoom steps.
- gui_probe --simulate (SW1+SW2 draft): wheel zoom 3 notches 0.72 s, 3 more 0.78 s, double-click 0.56 s per step
  (each including the probe's 0.4 s idle wait), worst stall 0 ms. Seen in the same run, not from the zoom: a 707 ms
  stall during Simulate with three threads in `splu` (earlier runs 360-418 ms; probably SuperLU's column ordering
  holding the GIL: idea, compute the MMD ordering once per stack and factorise later frequencies with NATURAL on
  the permuted matrix, which would also save the ordering time), and the probe's exit crash (139) again, so the
  atexit hook does not cure that kind.
- `.history/` (KiCad 10's local history, written when the user opened the repo's demo) in `.gitignore` and skipped
  by install_plugin. The repo's demo files (board, settings, project) were changed by the user's KiCad session:
  left uncommitted (`git checkout plugin/demo` restores the shipped demo; ask first).

### Carrying vias on the heatmaps (branch `plot-vias`, from main 66e1242; `wheel-zoom` merged, plugin installed)
- The user asked what the open circles are (issue markers: orange warning, red error; on the demo the two
  edge_proximity warnings beside the signal via), then "can we add the vias into the plots?". First version drew
  every via through a layer (338 on kraft_flux_mini_2, +50 ms per render); the user: "i don't think drawing them all
  makes sense ... Only the ones that carry current should be drawn. However, the current symbol is a bit small
  and hard to see". Now `plots._carriers`: the transfers (RouteResult.transfers: vias ▲, parts / plane capacitance
  ■, ≥ 2 % of the current) with a larger symbol (ms 9, dark outline, `CARRIER` green), their share as a label
  from `MIN_LABEL` 5 %, and a carrying via's pad ring and drill at true size (EllipseCollection in board units,
  for the wheel zoom; the board via nearest the transfer's grid node). The title note says "▲ via / ■ part
  carrying it (% of the current), ○ issue". The legend moved to the figure's bottom edge (under the first map
  it overlapped a second row's titles). Test: `test_heatmaps_mark_the_vias_carrying_current_with_their_share`.
- The user also asked to "add an option to draw all arrows on all layers at once" (Draw arrows on board draws
  one layer onto User.7 and replaces the previous). Claude asked where each copper layer's arrows should go (one
  User layer each vs all on User.7); the user interrupted that question with the via correction above, so it is
  **unanswered**: ask again.

### Plugin and Content Manager package (branch `pcm-package`, from main bbdee51; `plot-vias` merged)
The user (screenshot of KiCad's Plugin and Content Manager, Installed tab): "create a zip package so i can install
the plugin throug the manager". `tools/make_pcm_package.py` → `out/dist/groundtruth-<ver>-<commit>-pcm.zip` from
`git archive HEAD plugin src/groundtruth` (uncommitted edits, e.g. the user's demo play, never get in; the name
says the commit): `metadata.json` (identifier io.github.iwers.groundtruth, type plugin, license MIT, author as
the user gave it: "afkiwers & his good old mate claude", contact email iwers11@gmail.com), versions [{0.1.0 from pyproject, status testing, kicad_version 10.0, runtime "ipc"}],
`plugins/` (plugin.json, entry scripts, icons, requirements.txt, groundtruth package, demo), `resources/icon.png`
(make_icons.draw(64)). Layout and the runtime field from dev-docs.kicad.org/en/addons; metadata validated with
jsonschema against https://go.kicad.org/pcm/schemas/v1 (it caught: author.contact required, tags lowercase with
dashes). PCM installs to `<ver>/3rdparty/plugins/io_github_iwers_groundtruth`; with the copied install too every
button shows twice: `install_plugin.py --uninstall` removes the copy, and installing warns when the PCM copy
exists. Same plugin.json identifier, so the existing Python environment is reused. Test: `tests/test_package.py`.
Not tried in KiCad yet (the user installs it). Then the user: "create a script i can run from win console to
udpate the installer with the current files": `build_package.cmd` (repo root) runs the builder with
`--working-tree` (files on disk; the demo always from HEAD, since the repo's copy is the user's play copy; "-dirty"
in the name when plugin/ or src/groundtruth differ from HEAD), `--committed` for HEAD only; pauses at the end when
started with its own path (double-click, also from PowerShell). Versions: major.minor from pyproject + a patch
number of 10-minute steps since 2026-01-01 (schema: patch up to 6 digits), so every zip is a newer version to
the manager (e.g. 0.1.40024). The package is for another computer (the user: "no, the installer is for another
computer": keep the copied install here). At the user's "yes" the package also carries `tools/install_gpu.py` (as
`plugins/tools/install_gpu.py`, run there with KiCad's Python; same plugin identifier, so the same venv path), and
the description says so. Committed and merged at "yes commit and merge everything" (the user's demo play files in
`plugin/demo` excluded, as they asked to keep them local).

### Plane displacement and the cross-section (branch `section-3d`, from main 8c8eef9; merged)
The user (screenshot of the demo heatmap with a green ■ "1286 %" and the tile "main transfer 1286 % plane
capacitance In1.Cu–In2.Cu, 0.3 mm away"): "is there any benefit to provide a 3D view to view the current
densities?" Claude's assessment: a 3D view shows the vertical story (where the return changes planes) in one picture
but reads worse quantitatively (occlusion, a thin board needs exaggerated z); a cross-section along the route is
the quantitative way to show it. The user's answers: **"Both"** (the cross-section first, then a 3D view in the
browser) and, for the ■: **"Off the map, check physics"**. Plan: milestone 1 = A + B below (done on the branch);
milestone 2 = C, the 3D view: **ask the user before starting it**, after they have seen milestone 1.
- **A. Physics of the "1286 %"** (`tools/dev/transfer_check.py BOARD SETTINGS --only NET`): not a bug. The plane
  pair's displacement current and the vias joining the planes carry a resonant circulating current in antiphase;
  KCL across every cut closes to 1e-5. Demo: 12.9 A per A across In1–In2 at 1.45 GHz, 11.9 A back through the
  ICs' GND vias (the 29 pF pair with the vias' inductance, an L‖C tank, f_r ≈ 1.3 GHz, below f10 = 1.69 GHz).
  kraft_flux_mini_2 SPI_SCK: purely circulating (both return pins on one plane), 5.5× In2–In3 at 740 MHz (the
  thin cores' (1,0) mode). The model has no radiation loss: peak heights are upper estimates. Details in
  ARCHITECTURE "Circulating plane currents". Display: the maps no longer mark plane capacitance (`_carriers`);
  "main transfer" is the largest via/part (`simulate.point_transfers`); a new tile **plane displacement** ("12.9×
  signal", "In1.Cu–In2.Cu @ 1450 MHz", tooltip explains the resonance); `formats.share()` (moved from html.py)
  writes ≥ 1 as "×N signal" everywhere; HTML transfer table says "spread over the pair, @ f"; text output has a
  "plane displacement:" line. Capacitance transfers carry `"f"` and `"distributed"`. Tests: KCL + tank law
  (`test_capacitance.py`), no ■ for plane capacitance (`test_vectors.py`), the tiles (`test_ui_parts.py`).
- **B. Cross-section** (`analysis/section.py`, `report/plots.draw_section`, `RouteResult.section`): see README
  "Cross-section" and ARCHITECTURE "Cross-section along the route". In the plugin under the maps beside the
  spectrum (checkbox **Cross-section**; hover readout); in the HTML report after each heatmap; `--format html`
  writes `section_<net>_<receiver>.png`. (Replaced by the scrolling page, see "Readable plots and the scrolling
  Heatmap tab".) The Heatmap tab's layout changed then: maps 5 : lower row 2, small canvas
  minimum sizes (before, on a short window the 500 px minimum of the maps squeezed the spectrum to 0 px). The
  route spectrum's margins are in inches now (it is ~3 in wide beside the section). Validation
  (`tests/test_section.py`): image law within 0.5 %, KCL 1.0000, stitched vs unstitched layer change. Real board
  convergence: `tools/dev/section_check.py` (0.2–1.9 % mean per layer between 0.217 and 0.149 mm grids).
- **GUI**: the cross-section showed only when the row under the maps was ≥ 90 px (a squeezed 0 px canvas raised wx
  GDI errors; the rule went with the scrolling page). gui_probe on a kraft_flux_mini_2 copy (all nets and stages, 772 px wide window):
  `main` Simulate 408 s, worst stall 933 ms, 23 stalls > 150 ms; branch 359 s, 792 ms, 17; view, layer,
  arrows, cross-section and zoom steps 0 ms (a few ~180 ms without a stack sample on both). A first branch run
  stopped with "not enough memory" while the user's own GroundTruth window held 2.3 GB; the rerun was clean.
- **Display-dependent tests:** the session ran over RDP, first at 752×1402 (portrait), later 640×480 (the
  session disconnected). `test_converters_tab_adds_a_stage_and_simulate_shows_its_hot_loop` needs a window
  ≥ 1000 px wide ("Open in window") and `test_a_long_status_message_wraps_instead_of_being_cut_off` a 900 px
  window; both fail on such displays on `main` too (checked at 640×480). Full suite on the branch at 640×480:
  310 passed, those 2 failed, 2 skipped (the live-KiCad test and the one below). The new
  `test_the_cross_section_shows_under_the_maps_for_a_route` skips its main part below 800 px display height
  (it then checks that the section is hidden); in full it passed once the display was 1920×1080 again (shown,
  hover readout, hidden when unticked or in a short window, back when enlarged).
- **C. 3D view** (milestone 2, the user's "Yes, start it"; branch `view-3d`): `report/view3d.py`, an "Open 3D
  view" button on the Heatmap tab, `view3d_<stem>.html` beside the CLI's HTML report. See README "3D view" and
  ARCHITECTURE "3D view". Board-wide scene from `RouteResult.board_maps` (`simulate.board_maps`). Tests:
  `tests/test_view3d.py` (safe, self-contained page; scenes; the displacement map sums to the plane-capacitance
  transfer and the vias carry it back less 1 A, KCL), `test_open_3d_view_builds_the_page_in_the_background_and_opens_it`
  (GUI), `test_simulate_with_margin_gate` (CLI writes and links the page). Pages checked as headless-Edge
  screenshots (CLAUDE.md) on the demo, a stitched board and kraft_flux_mini_2 (SPI_SCK route and 740 MHz
  board-wide, U503 hot loop).
- **Regression from milestone 1, fixed on `view-3d`** (merged; `main` at 34a2e1a had it):
  `test_jobs_run_off_the_gui_thread` failed alone every time (GUI stall 0.68–0.84 s; 8c8eef9 passes).
  Showing the cross-section resized the other canvases, their next paint rendered on the GUI thread and waited for
  matplotlib's class-wide `Figure._render_lock` held by the background render. Fixed with
  `PrerenderedCanvas._on_paint` deferring to the arriving render and `_blank_canvases` (ARCHITECTURE,
  "Cross-section along the route"); the test passes 3/3 alone, longest gap 0.197 s.

### Readable plots and the scrolling Heatmap tab (branch `readable-plots`, from main 12dc787; merged at the user's "commit and merge")
The user, with a screenshot of the Heatmap tab at 1920 × 1080: "the legend and graph descriptions are very small and
hard to read"; then, while that was being done: "instead of having everything on one screen, make it scrollable
and have every graph occupy the entire width of the application so the user can scroll down to see the other
graphs". Details in ARCHITECTURE, "Cross-section along the route" ("The Heatmap tab's page", "Plot text").
- Text: 6–8 pt before; now titles 10, labels/notes 9, ticks/legends 8.5, bold map labels 8 pt, margins in inches
  from them; frequency ticks "100 MHz"/"1 GHz" (no axis label); the spectrum's legend on its title line; the
  maps' legend just under the maps and the colour bar beside them; the section's rows that carry nothing get no
  (white) band; dark theme colours the new figure texts. `tests/test_plot_text.py` (8 tests) checks size,
  clipping and overlaps at the plugin's and the report's sizes.
- Page: controls, tiles and toolbar fixed; maps, section, spectrum stacked full width on a `wx.ScrolledWindow`.
  **The wheel now scrolls the page; Ctrl + wheel zooms a map** (the enlarged window still zooms with the wheel
  alone); the readout shows that hint when the mouse is over no value. `tools/dev/window_shot.py --scroll` also
  captures the page scrolled to its end.
- Checked: demo screenshots light and dark at 1920 × 1040 (top and scrolled); the Heatmap GUI tests (new
  `test_the_heatmap_tab_scrolls_through_full_width_plots` replaces the section show/hide test). Full suite: 325
  passed, 1 skipped, 1 failed, `test_thermal_button_adds_a_temperature_view_and_its_issues`: a GUI stall of
  0.58–0.61 s in every run. Cause: the page's heights (`_fit_page` → `heatmap_height_in` → `_wrap`) measured
  text with matplotlib's `text_to_path` on the GUI thread, ~20 µs per character, every prefix of the title
  and note again. Fixed: `_width_in` sums per-character advances cached once (`_advance`; within 0.3 % of
  `text_to_path`, the note's wrap 0.17 ms). After it the test's longest GUI gap is 0.16–0.2 s, as on `main`
  (0.18–0.19 s: filling the view list and the tiles); the test then passed 5 of 6 runs (main 3 of 3).
- **Seen, not from this branch:** after Simulate of a power stage (the thermal test's board), wx prints
  "'SetTextColor' / 'SetBkColor' failed … 'BitBlt' failed (The handle is invalid)" twice; `main` prints the same
  two in the same run (checked in a worktree). Earlier such errors came from a canvas squeezed to 0 px; this one
  is not found yet (no crash, no visible effect).
- gui_probe `--simulate` on the reduced kraft_flux_mini_2 copy (SPI_SCK + U503; new steps: the page scrolled to
  the end and back): every view, layer, arrows, cross-section, scroll, zoom and tab step 0 ms except "heatmap
  view 1 again" 153 ms and "arrows off" 165 ms (no stack sample); Simulate's worst 240 ms (the GUI thread in
  matplotlib's resize while the solver and render threads held the GIL); no wx GDI messages.

### Motor bridges (branch `bridges`, from main fcd4b41; milestone 1 committed 738e16f and merged at the user's "commit and merge")
The user (2026-10-06): simulate the GaN half-bridges of a three-phase motor drive (a real board, read-only, open
in their KiCad), "another tab for half bridges ... like the Nets and converter tab where those nets are auto
discovered", "This should work universally for any half bridge design" (second example: an ESC). Their answers: a
new Bridges tab; the 3-phase bus ripple in milestone 1; bus 20–75 V, PWM 5–500 kHz, phase current 0–500 A, all
settings; six-step Hall commutation; the motor star or delta with its own parameters; GaN switches (EPC2302, two in
parallel per switch: 1.4 mΩ typ / 1.8 max each, C_oss 1000 pF @ 50 V, Q_oss 85 nC, V_SD 1.5 V); LISN at the
battery input. The approved plan was "Motor-drive half-bridges" (sections 1–9); design in ARCHITECTURE "Motor
bridges", user docs in README "Motor bridges".
- Done: settings, detection (the motor drive: one bridge, three legs with two FETs in parallel per switch, a
  driver per leg, RC snubbers, the input through a current-sense IC and series FETs; the ESC: one driver for all
  phases, wire tabs; the user confirmed it), the drive model, the line spectrum and receiver, the shared-region leg
  solve, the snubbed ringing, plots, tiles, text/JSON/JUnit, CLI, the Bridges tab. Tests: `tests/test_bridge.py`
  (16), `test_pwm_lines.py` (4), a Bridges GUI test.
- At placeholder values (`tools/dev/bridge_check.py`) the hot_loop errors come from L·I/tr at 2 ns and the leg's
  switched current (pessimistic until the user gives the real edge time).
- Grid convergence (100k vs 200k nodes, 235 s vs 333 s): leg inductances within 2 %, capacitor shares unchanged,
  conducted the same, radiated margins within 0.4 dB, ringing within 1 MHz. Converged.
- Build profile: the Bridges tab 208–233 ms (limit 350; Converters ~140). Full suite: 348 passed, 1 skipped.
- gui_probe `--simulate` on a copy of the real board (only the bridge, placeholder values): Simulate's worst stall
  203 ms; the bridge's drive view 0 ms; load 258 ms (the Bridges tab's build step; main 0 ms on this board, limit
  350). **Tab switches after Simulate 324–396 ms and one view change 360 ms: not from this branch**: `main` with
  one converter on the same board stalls the same (tabs 327–393 ms, double-click 479 ms) and prints the same wx
  GDI messages; before Simulate every tab switch is 0 ms. A large board's results make native tab switches slow:
  open, not investigated (the stack is inside wx's SetSelection). On the synthetic bridge board: no tab stalls,
  one 348 ms during Simulate (the status bar growing a line lays the window out while the solver threads hold the
  GIL).
- Window shots (light, 1920 × 1040): Bridges tab, Heatmap drive view and scrolled (capacitor bars, conducted
  receiver vs CISPR 32 B). Cosmetic, open: the Bridges tab opens with no bridge selected (empty form); the map
  buttons stay enabled on the drive view (they answer "pick one of its legs").
- py_compile with KiCad's 3.11 done; plugin installed. Shown to the user; still open: the real motor data (R_LL,
  L_LL, ke, pole pairs, star/delta), PWM scheme, edge time, operating points and conducted standard, and whether to
  start milestone 2.
- Milestone 2 (ask first): DC/RMS current density at up to 500 A, losses, thermal, capacity, fixes for legs.

### Detection labels and the GPU README (branch `bridges`, after 738e16f; merged at the user's "commit and merge")
The user: "To make the automatic recognition of converters and half bridges more robust, can we introduce labels
like hb_A for half bridge A or conv_in, conv_out_1 etc. … some could have a shared supply etc. do some research and
if it's worth it and implement an easy to understand labelling system. Under each tab, eg converter, add an info
field that explains the nomenclature for the specific tab." Then: "add some info to the readme on how to use the gpu
and what needs to be installed", and "Once done with everything update docs and handover and commit and merge".
- Done: a `GT` symbol field (`conv_hs/ls/sw/L/in/out`, `hb_hs/ls/in/out`, `ignore`, ids `conv1_hs`, `hbA_ls`,
  `conv_out_1`), why a field and not net labels, reused-sheet copies from the board's `path`/`sheetname`/
  `sheetfile`: ARCHITECTURE "Detection labels", README "Detection labels". Info boxes (Show/Hide) on the Nets,
  Converters and Bridges tabs, the board's label summary and problems on them and in `groundtruth detect`.
  README "GPU acceleration (optional)" under Install: requirements, `install_gpu.py` steps and options, the
  double-precision caveat, `analysis.gpu`, checking with gpu_check.py.
- Checked: unlabelled detection unchanged on the real motor-drive board, kraft_flux_mini_2 and the ESC. A
  labelled copy of the motor-drive board (`tools/dev/add_labels.py`): `hb_hs`/`hb_ls` once per FET in the three
  copies of its phase sheet → legs Phase A/B/C with phases A/B/C, `hb_in` used, `conv1_*` on one buck → "buck
  conv1", `ignore` drops a converter, bad labels reported clearly. Expanding an info box: 7–11 ms.
  Tests: `tests/test_tags.py` (7), a GUI test.
- **Not checked**: reading the field and the sheet through the live KiCad API (kipy `definition.items` →
  `Field`, `sheet_path`, `symbol_sheet_name`): only with kipy objects in a test. Check on the scratch board
  (`tools/dev/live_kicad.py`) when the user has it open.

### Labels on kraft_flux_mini_2 (2026-10-07, branch `labels-kraft`, merged 289ab52 at the user's "commit and merge" before they looked in KiCad)
The user: "Can you add labels to my Kraft flux schematic an check? Leave them afterward". KiCad did not have the
project open (no lock files); the folder is no git repo, so every file was copied to
`out/kraft_backup_2026-10-07/` first. New `tools/dev/label_project.py` sets `GT` in place: a hidden field after each
symbol's Reference in the .kicad_sch, and a copy of the footprint's own hidden Description property in the
.kicad_pcb (as Update PCB from Schematic writes it), keeping the CRLF line endings. Labels: conv1 Q301 hs / Q302 ls
/ L301 L, conv2 Q303 / Q304 / L302, conv3 U502 sw / D501 ls / L502, conv4 U503 sw / L503, J101
`conv1_in,conv2_in,conv3_in`, J104 conv1_out, J106 conv2_out (each power sheet is used once, so ids are safe).
- Checked first on a copy (out/kraft_trial): kicad-cli loads the schematic (the netlist lists all 14 GT fields) and
  plots the board; only additions (66 / 55 / 33 lines in dcdc_gan / psu / root sheet, 161 in the board). The real
  files got the same additions; the board equals the checked copy apart from the new fields' uuids. kicad-cli was
  not run in the real folder (it writes a .kicad_prl).
- Detection on the real board: all four converters from their labels (same parts, rails and connectors as
  unlabelled), no label problems. The user's own settings file (only "buck SW1") is unchanged (checksum) and still
  matches conv1 by its parts.
- Found and fixed: a labelled converter IC with a pin on both ends of its inductor (U503: SW and OUT) gave "inductor
  shares two nets"; now the end on the SW pin is the switch node (test added). And opening a settings file without a
  `bridges:` section looked for bridges only, then said "no nets, power stages or motor bridges detected": now
  "no motor bridges on this board".
- To undo: `label_project.py PROJECT --backup DIR --remove Q301 Q302 ...`, or copy the backup back.

### Code cleanup (2026-10-07, branch `cleanup` from main 8cd1f78; merged at the user's "Yes")
The user: "Go through the plugin code and check that the code is sound and not clumsy, remove AI slop, and try to
simplify cascaded loops where possible." The vendored thermal solver (`src/groundtruth/thermal/`, the user's own
PCB Thermal code) was left alone. Tools: ruff and vulture in the dev venv (not in pyproject), a loop-nesting script.
- Dead code removed (no callers anywhere): `simulate_route` ("kept for callers"), `next_frequency`, the route
  setup's unused `amplitudes`, `limit_at`, `knee_frequency`, `stripline_z0`, `CopperMap.other_nets`,
  `as_multipolygon`, `theme.Card`, `motor.PHASES`, `issues.SEVERITIES`, `tags.SWITCH_ROLES`, unused imports and loop
  variables. Kept although only tests use them: `dc_path`, `PlaneProblem`, `microstrip_z0` (physics validation).
- `Board.footprint()` was a linear scan, and four places built their own net → parts index on every call
  (`input_path` once per connector pin during detection): now `Board._indexes()`, cached per footprint list (a
  what-if copy with its own list gets its own; the GT label cache is keyed the same way, it used to be copied along).
- `detect_bridges` (185 lines, dict legs): `_Leg` dataclass, `_LegFinder` (labelled / unlabelled legs share the
  rail-ground and connector code), `_group_legs`, `_phase_labels`, `_bridge`; `tags.phase_from_name` instead of a
  copied regex; `_centre` for both detections. `find_route`: `pairwise` / `combinations` and `_pour_ports` (was 5
  loops deep). `_suggest_fixes` (327 lines): `_FixSearch`, one method per phase; the "skip a modification already
  suggested" loop (3 copies) is `unseen()`, the advice text (2 copies) `advice()`. `finish_bridge`: `_conducted`,
  `_column`, `DriveWaveform.hs_ac_rms()`. Thermal: `thermal_inputs` shared by the in-process and child-process
  solves. UI: one `PickDialog`, `button_row`, `parse_field`, `source_status` in `ui/widgets.py` instead of three
  copies each in the Nets / Converters / Bridges panels. `RouteResult.kind = "route"` (a class attribute, not in the
  JSON) so 34 `getattr(r, "kind", "")` became `r.kind`. `footprint_fields` lost three `except Exception: pass`
  (kicad-python >= 0.8 has every attribute).
- Results unchanged, checked: `tools/dev/detect_dump.py` on the two motor-drive boards, a labelled copy, kraft_flux_mini_2
  and its labelled trial copy: byte-identical to main. `groundtruth suggest` JSON on the demo board, the synthetic
  buck and kraft_flux_mini_2 (SW1, the greedy hot-loop pick): no difference to main. Full suite: 357 passed, 1
  skipped (one run had the bridges GUI timing test at 0.63 s under load; alone it passed 3 of 3).
- Not changed on purpose: the docstrings that quote the user or record what was measured (the project's way of
  keeping the why), `Optional[X]` style, the remaining broad `except` at API / thread / dialog boundaries (each
  reports or re-raises), the long but linear `config_from_data` and `prepare_converter`.

### Victims and stacked boards (2026-10-07, branch `stack` from main 89d82e8; milestone 1, merged 02ccde6)
The user: "The reason why I need to simulate the bldc half bridges is to see if the emi causes issues in signal
tracks as some ESCs reset under high load. This could be a rail getting unstable or the uC getting hammered." Then:
a single-board ESC never resets, "others that are stacked" do; "Start the milestone"; "I can't give you the files"
(the stacked designs stay with the user: they run the tool themselves). "No need to support KiCad 9" (a KiCad 9 net
fix was taken back out).
- Done: `stack:` and `victims:` settings, the stack merge, victim runs per leg, receiver noise (terminated victim
  nets in the model), injected current (plate capacitance to the phase node), part ground (information), issues,
  text, KPI tile, bars. Details and the dropped first approach: ARCHITECTURE "Victims and stacked boards"; user docs:
  README "What the bridge does to the rest of the board". `tools/dev/victim_survey.py` lists a board's ICs and
  sensitive nets and their distance from the bridge.
- Synthetic stack (bridge 48 V / 40 kHz / 30 A, logic board 5 mm above, 2 GND pins): NRST (bottom layer, facing the
  bridge, nearest plane 1.27 mm) 0.053 V with its 10 kΩ pull-up (1.67 V if a 50 Ω output drove it), 0.08 mA
  injected from leg A's pour (0.0034 pF); the Hall line behind the logic GND plane 0.02 V, none injected.
- Tests: `tests/test_stack.py` (7): placement fit, merged stackup z, settings, plate capacitance and shielding, the
  victims end to end, the termination divider (analytic ratio within 10 %), a wider gap couples less (C ∝ 1/h).
- The real motor-drive board (placeholder 48 V / 20 kHz / 100 A): Simulate 341 s with victims (230 s without). After three
  fixes found there (the bridge's own gate-drive nets were taken as victims: now every net of the bridge's stages is
  excluded; fine-pitch pins lost their grid node to a neighbour: `pad_node` searches out to 6 mm; the direction of a
  net from the pin types: a PWM line is read at the gate driver's input, a reset / Hall / crystal line at the
  microcontroller) every microcontroller net there sees under 0.05 V; two small pads inside a ground pour
  stay unresolved at this grid pitch and are named in the notes.
- Merged to main at the user's request ("go for it", 2026-10-07) after the full suite: 363 passed, 1 skipped, the
  Bridges-tab GUI timing test failed once on the loaded machine (0.68 s busy gap) and passed alone; branch
  `stack` deleted, plugin installed in KiCad. The user runs it on their stacked ESCs themselves (README "What the
  bridge does to the rest of the board"). Open (ask before starting): bridge milestone 2; the user's real motor /
  PWM / edge / conducted-standard data.
- **Stacks shelved** (the user, 2026-10-07: "let's not worry about stacks for now. Stacks would require to somehow
  merge several kicad projects"). Their real ESC stacks have five boards about 5 mm apart: phase A, phase C,
  phase B, a capacitor board, the logic board. Today's `stack:` merges ONE other board onto ours and the bridge
  is detected on our board, so such a stack (one leg per board, the bus on a separate board) would need N boards,
  legs and bus capacitors spread over boards, and inter-board current through the connectors. Not started: no
  plugin form, no thermal / drawing / 3D for stacks, no single-board control run. Single-board victims stay in use.


### Bridge milestone 2 (2026-10-07, branch `bridges-m2` from main 71c0f90; merged b7c7e70 at the user's "merge now")
The user: "let's do Bridge milestone 2"; plan approved as proposed, the sweep over phase current AND speed.
Plan: 1 losses from the drive waveform, 2 RMS current density battery → motor (checkpoint with the user after 1-2),
3 thermal and current capacity, 4 operating-point sweep (i_phase × speed, from the kept transfers, no new solves),
5 fixes (bridge issues to its legs' candidates), 6 analytic tests, 7 a real-board run, docs, install.
- Done (d267bf1 + fixes after it):
  - `motor.DriveWaveform.states`, `conduction()`, `node_voltages()`, `switching()`.
  - `bridge.bridge_losses`: per FET R_DS(on)·(i/n)², v_sd·|i|/n, ½·Δv·I·t_r and E_oss·(Δv/V)² at each turn-on;
    snubber R ½·C·Δv² per node swing; capacitor ESR; series parts R·I_rms² (a `short` model gets a note, no loss);
    turn-off counted soft.
  - `analysis/bridge_rms.py`: six unit DC solves (hs, ls per leg: battery pin → switch → motor pin) on a region of
    their own, weighted by the slow sources' correlation G (line spectrum below f_pwm/2), plus each leg's cond
    solves at n·f_pwm (n = 1..3) weighted by the cross spectra; RMS map per layer, vias (plated part holes left
    out), series parts, copper loss. `rms_view` on the Heatmap tab ("bridge J1  RMS currents"), KPI tiles,
    `formats.rms_text`, issues current_density / via_overload on the bridge.
  - Two bugs in `emi/pwm_lines.line_spectrum` (milestone 1) fixed: three breakpoints at one time (an open phase
    clamping at an edge) squeezed a ramp to 1e-18 s, ruining low lines (20× off at f_e); a jump on the period wrap
    was lost. The real board's conducted estimate rose 2.4 dB with the fix, the synthetic bridge's 2.2 dB. Earlier conducted numbers in these notes are from before the fix.
  - Sign trap: `DriveWaveform.ls` is the current from the node INTO ground (i = hs - ls); the unit solves take
    -ls.
  - Tests: L1 locked-rotor losses, R1 the slow/switching split of a chopped current, R2 the motor-pin via carries
    the phase RMS and the battery sensor the bus's slow RMS, S6 coincident breakpoints vs a sampled FFT.
- Checked on the real board at placeholder values and shown to the user at the checkpoint; their "yes" to
  steps 3-7.
- Steps 3-7 done (494830f thermal + capacity, af85db3 sweep + GUI stalls, then fixes, docs):
  - Thermal: a bridge's RMS map is heat (J², not vectors), its losses join the parts'; capacity per bridge in
    i_phase (copper ∝ s², losses fitted to the drive at 0.5/1/1.5×). Test T1 runs at 6 A: 30 A fuses the
    synthetic board's thin copper (a thermal runaway, correctly reported).
  - Sweep: `sweep: {i_phase: [...], speed_rpm: [...]}` (≤ 64 points; the Bridges form's Sweep fields); per point
    the drive, `RmsBasis.evaluate`, `caps_at`, losses, the receiver; L·di/dt and radiated scaled with the edge
    current. A Heatmap view "bridge J1  sweep (phase current × speed)". Test W1: the operating point's row equals
    the full result.
  - Convention fixed: `q_oss` / `c_oss` are one side's parallel switches together (the form's tip and the
    ringing already took c_oss so); the first real-board run passed per-FET values with a ×n that is now gone (same
    numbers for 2×85 nC = 170n).
  - Fixes: a bridge's issues are power issues; legs' candidates link to them. Test F1 on
    `bridge_board(unstitched="C")`: a GND via at Q6.2 selected and verified.
  - GUI stalls found by the Bridges GUI test (it failed alone at 0.54-0.76 s): `wx.lib.wordwrap` in the status
    bar (a wx call per word, each a timer tick for the GIL beside the receiver loop) → `wrap_words` with one
    `GetPartialTextExtents`; the bridge tail runs with a 0.5 ms switch interval. gui_probe on the test bridge:
    Simulate's worst stall 722 → 173 ms (`time.sleep(0)` in the receiver loop did not help: tried, removed).
- The real board's final run (victims on, a 3 × 3 sweep of phase current and speed): 377 s (victims ~110 s,
  sweep ~4 s per point).
- Full suite (2026-10-07): 370 passed, 1 skipped, 1 failed: `test_jobs_run_off_the_gui_thread` 0.53 s (limit 0.5;
  alone 10 of 11 runs pass on the branch, 4 of 4 on main; its board has no bridge, so only `wrap_words` of this
  branch runs there, with fewer wx calls than before: the known occasional C-call stall its comment describes).
  Plugin installed; merged at the user's "merge now" (b7c7e70), branch deleted.
- The user's real data (2026-10-07, after the milestone): the PWM scheme from a bench capture of one phase:
  hpwm_lon synchronous (120° HI PWM with LO complementary, 60° float, 120° LO held, 60° float), dead time ~200 ns
  between the gate signals, edges 60-150 ns at 1 A (current-limited by the node's C_oss; at 100 A ~1 ns, so 2 ns
  stays the worst case); a delta motor with a sinusoidal EMF, bus 55 V. `bridge_check --motor` takes the EMF shape
  as a 6th field.
- GPU package for another PC (`tools/make_gpu_package.py` → out/dist/groundtruth-gpu-<commit>.zip: install_gpu.py,
  check_gpu.py, GPU_README.md). The user's second PC: RTX 3050 Laptop GPU (4 GB), driver 527.37 (CUDA 12.0, too old
  for cu126): `install_gpu.py --cuda cu118` → PyTorch 2.7.1+cu118; check_gpu: GroundTruth uses the GPU, complex128
  FFT 1024² 35.9 ms CPU vs 2.15 ms GPU (17×). On that PC the plugin venv's python.exe printed nothing to the
  console (it ran fine): both scripts now relay the child's output through a pipe (`run_shown`).

### After the merge (2026-10-07): branches `bridge-idle-legs` → `help`, merged dc958e8 at the user's "merge"
(The GPU package commits 1a3db33..a1beebb went onto main directly, before these branches: scripts and docs only.)
- `bridge-idle-legs` (2b40f79, 4f6b206): a leg with no switching edge at the operating point (locked rotor: one
  leg held low, one open) gets no hot-loop / emi_margin issues, with a note (they showed "0.0 V of overshoot"
  errors at 0 rpm). SuperLU's failed allocation (RuntimeError "SUPERLU_MALLOC fails …") is raised as a
  MemoryError in `PlaneStack._factorise`, so `run_parallel` retries the solve alone; it stopped Simulate on the
  user's second PC (a large board, 4 GB free). Tests in test_bridge.py. PCM package
  groundtruth-0.1.40189-4f6b206-pcm.zip given to the user for that PC.
- `help`: the user asked how nets, converters and bridges play together in Simulate, then "the plugin should
  have a help button of some sort where this is explained in detail". `ui/help.html` (wx.html: simple HTML, the
  theme's colours filled in for @BG@ / @TEXT@ / @LINK@ / @WARN@), `ui/help.py` (HelpWindow, one per frame, opens
  at the tab's section), top bar **Help** and F1. Test `test_help_opens_at_the_tab_shown_and_every_link_lands`.
  Keep help.html current when features change. Offered, not started: an on/off tick per row on the Nets and
  Converters tabs so Simulate can skip rows.
- `help` also (e31bc1a): a starter/generator's generating run (55 A, 7000 rpm, 55 V, 200 ns dead time) hung for 2 h in
  `SixStep.run`: in a dead time with ~zero currents (rounding residue 3e-22 A kept a phase "conducting"), an open
  phase's clamp started it with a 1e-12 A seed and the drive (−8.7 V) took it back at once, at the same instant,
  forever. Fix: |i| < I_EPS (1 nA) is zero (except the seed just started), a phase whose seed dies within 1e-9 T
  stays open for the interval (`blocked`), > MAX_EVENTS (10 000) per interval raises DriveError (never a hang).
  Its points then solve in < 0.1 s; generating 55 A at 7000 rpm gives 48.9 A into the bus (2.7 kW), 82 A at 8000 rpm
  81 A (4.5 kW); cranking unchanged. Test `test_generating_near_the_bus_voltage_settles_
  and_balances` (energy balance with the reverse conduction). PCM package groundtruth-0.1.40191-e31bc1a-pcm.zip.

### Export report and Vias shown (2026-10-07, branch `report` from main 14ce12c; merged at the user's "commit, merge and install")
- The user: "Create an export function that exports the simulation data to a well formated html page ... very
  professional ... a well rounded style sheet". `report/html.py` rewritten (same `render_html` entry, new kwargs
  bridges / thermal / capacity / solver / config / progress / max_vias): cover (optional `report: {title,
  company, author, document}` settings), summary (KPIs, main findings, overview), filterable sortable issues,
  nets, power stages, motor bridges (drive, capacitors, conducted, legs, losses, RMS map, sweep, victims),
  temperatures and capacity, fixes, method and limits; sticky contents column, print CSS (A4, page breaks), dark
  mode on screen. Plugin top bar **Export report…** (FileDialog, a job, opens the page; export stall 0.15-0.19 s).
  CLI `--format html` passes everything too.
- The user saw 4 of 12 vias on kraft_flux_mini_2's SW1 "DC output L301 → J104.2": `dc_summary` kept the five
  busiest (and the map drew those). Now every via ≥ 1 % of the path current (bridge RMS: ≥ 1 % of the busiest;
  signal routes: ≥ 1 % of the return, was ≥ 2 % and 8 at most). They asked for a help note and "a global setting
  to set the max number of vias shown": **Vias shown** (Heatmap tab: All / 5 / 10 / 20 / 50), a per-user
  preference in ui.json with the theme (`theme.prefs` / `save_prefs`; `save_pref` no longer overwrites the file),
  passed to `draw_heatmap(max_vias=)` → `_carriers`; the export follows it; the checks see every via. The
  cross-section's side view draws the 8 biggest (`SECTION_CARRIERS`).
- Tests: plot `test_vias_shown_marks_the_busiest_and_keeps_the_parts`, GUI `test_export_report_writes_the_page_in_
  the_background` (prefs redirected to tmp_path), help topics include `export`; test_plot_text's REPORT sizes are
  the new report's. GUI + plot + CLI + UI tests on an idle machine: 65 passed, 1 failed
  (`test_the_window_builds_its_tabs_in_steps`, passed alone). build_profile: Bridges tab 276-286 ms (limit 350,
  was 208-233 before the sweep fields): the tightest step. GUI timing tests fail while the user simulates in
  KiCad on the same machine (seen 0.7-1.2 s): rerun them when it is idle.

### Heatmap hover, part outlines, Preferences, resolution (2026-10-07, branch `heatmap-hover` from main aa21814; not merged)
- The user, in a row: a hover message over issue circles ("what the issue is"); part outlines overlaid on the
  heatmaps ("on-the-fly"); a setting to hide the via markers; Sharper map as "a global setting ... standard res,
  than whatever responds to the sharper map and then a few even sharper steps. the server has lots of memory"
  (answered: it applies to Simulate itself; levels 1, 2, 4, 8, 16, 20, 25 × the nodes), each level with its cell
  size "(1mm x 1mm)"; then "a button beside the help button that opens a dialog with all global settings ...
  Otherwise the interface gets too cluttered".
- Done: `report.plots.mark_at` + fig._gt_marks (issue circles rank first, then vias/parts), the plugin's hover
  readout and the map's tooltip; legend "red / orange ○ issue" (the green rings are via pads). `Footprint.outline`
  (courtyard, else fab; file reader only, live KiCad gets pad boxes), `_part_outlines`. `ui/prefs_dialog.py`
  (Resolution with cell sizes from the last Simulate's coarsest model, Via markers, Vias shown, Part outlines,
  Theme) behind top bar **Preferences…**; all in ui.json. `analysis.resolution` scales every model's budget and
  divides its starting cell by √N (`_choose_pitch`, `RESOLUTIONS`, `RESOLUTION_MIN_MM` 0.02); Sharper map sets its
  own pitch (resolution reset); CLI `--resolution N`; `MainFrame._sim_config` (one object per settings +
  resolution, so Suggest fixes still reuses the last Simulate).
- Then (the user: "any other settings we can move there?" → "add them"): Preferences also holds solver
  threads, GPU, thermal quality (each "From the settings file" by default), the report's author and company;
  Current arrows, Log scale, Cross-section, Zoom to it and Highlight its net remember their state (`remember`).
  The arrows' tooltip said their length shows the density: their number does (help: "Reading a map"). The user
  could not find Export report on the other PC: it needs a package from 0.1.40211 on (the GPU package
  0.1.40191 predates it).
- Not measured yet: memory and time at 8× and above on a real board (the user's server); this laptop would run
  out on a large bridge board at high levels (the out-of-memory retry, then a message).

### Physics audit (2026-10-08, session 7, branch `physics-audit` from `docs-coverage`; merged at the user's "merge, install")
- The user: "look at the net and converter sims and make sure it makes sense, physically and the results are
  actually meaningful". Run: `tools/dev/real_settings.py` (fixed: the GT labels renamed the stages conv1..conv4)
  with `--spread-mode off` → `out/audit/kraft.groundtruth.yaml`, `groundtruth simulate` on kraft_flux_mini_2.
- **Checked by hand, sound:**
  - the radiation constant η0k²/4π, the ground reflection, peak → RMS, and the receiver sum;
  - `line_current`: SPI_SCK 1.66 mA at 20 MHz against 25 Ω, 5 pF and 2.8 pF of line;
  - the route model converges: SPI_SCK 15.11 → 15.15 nH and 21.2 → 21.3 mm² at 4×;
  - conv1's input current, 10·28/51.8 = 5.405 A;
  - R309 ∥ R310 = 4.0 mΩ;
  - the ringing frequency, 150.6 MHz;
  - the overshoot: 39.6 V, against 45 V from a time-domain cell;
  - the conducted LISN: 50 µH MIL; a capacitor divider of 6e-5 against 4.5e-5 solved;
  - the common-mode budget, 0.35 pF.

  conv3's −3.9 dB at 0.22 A is real: its 1 µF + 100 nF resonate with the trace to C101 near 0.7 MHz (C101 102 %).
- **Fixed:**
  1. **Converter spectrum without the node charge current.** It now applies `edge_charge_factor`
     |1 + Y_node·V/I|; the time-domain cell had SW1 8 dB and SW2 15 dB above the old model at the ringing.
  2. **A failed solve passed silently.** At 4× the 1.58 GHz board-wide solve failed after 416 iterations, the
     refinement stopped at 260 MHz, and +17.5 dB was reported at 780 MHz, held from 260 MHz. Now:
     - `GMRES_EXTRA_CYCLES` while the residual falls (that solve converges in 572 iterations);
     - the refinement closes in on a failure from below;
     - `unsolved_band` / `band_unsolved` warning.
  3. **Connector pins read as plated vias.** The XT30 J104.1 read 187 %. Now part holes are filled conductors and
     are left out of the IPC check.
  4. **Ceramics at nominal C.** New optional `C_bias` (Parts tab "C at bias"); a note names the ceramics still at
     nominal C.
  5. **Cosmetic:** currents print to 3 significant figures; a cascaded stage's note (`feeding_stage`); no negative
     L in the input-path rows (`_stage_row`).
- **Still open:**
  - The convergence of the board-wide model (SPI_SCK's 740 MHz plane resonance, +3 dB margin at 1×) needs the
    rerun at 2× and 4× with the fixed solver.
  - The kraft results with the charge current (conv1 / conv2 radiated about −21 / −23 dB, from the time-domain
    cell) need the rerun too.
  - The user's: C_bias of C324/C326/C329/C332 (10 µF at 51.8 V) and a C_oss for D501.

### Part parameters from schematic fields (2026-10-08, session 7, branch `part-fields` from `fix-cascaded`)
- **The request.** The user: "My parts' meta data (fields) in the schematic come from Inventree using HTTP libs ...
  use these values to pre-populate them ... when the user changes a parameter inside groundtruth it is saved but
  flagged ... with a little exclamation mark".
- **Their decisions:**
  - one concrete generic field name per parameter, no aliases; they rename their InvenTree parameter templates;
  - InvenTree sends units ("Rds": "3.8milliohm"); a bare number takes the datasheet unit per field, with a
    plausibility check;
  - flags also as an info issue and in the reports;
  - values stay in sync with the schematic on every open and run;
  - the Help gets a table of every accepted field, for setting up InvenTree.
- **Done:** see ARCHITECTURE `components/fields.py`. Tests: `tests/test_fields.py`, and the GUI test
  `test_schematic_fields_fill_the_forms_and_a_value_set_here_is_flagged`.
- **On kraft_flux_mini_2 today:** the fields are named `Rds`, `DC Resistance`, `Capacitance`, `ESR`, `Resistance`.
  Only Capacitance/ESR/Resistance match until the user renames the templates to `Rds(on)` and `DCR`. No part has
  Coss or Qoss yet.
- **`fix-cascaded` (cc7f7e4, not merged):** found by the audit rerun. A cascaded stage (conv4) kept its connector
  and printed 5.9 dBµV beside "not estimated".
- **Audit rerun results** (`out/audit/new1`, `new2`, `new4`):
  - conv1 radiated 52.9 dBµV/m (−22.9 dB) and conv2 53.5 dBµV/m (−23.5 dB), as the time-domain check predicted;
    the XT30 via overloads are gone; the C_bias notes name the ceramics.
  - **Not grid-converged: the board-wide plane resonance.**

    | Cell | SPI_SCK worst | Margin |
    |---|---|---|
    | 0.86 mm | 34.0 dBµV/m at 740 MHz | +3 dB |
    | 0.61 mm | 23.5 dBµV/m (740 MHz not solved) | +13.5 dB |
    | 0.43 mm | 29.0 dBµV/m at 740 MHz | +8 dB |

    The resonance moves (740 → 1020 MHz). A follow-up should study the cavity model's convergence.

### Report name, part names, board view (2026-10-08, session 7, branches `fix-report-name` → `board-view`)
- **Report file name.** "the html report name has a colon": live boards read `kicad-ipc:<name>`, and the Export
  dialog suggested that as the file name. Now `main_frame.report_stem` builds it from the settings file name, and
  the cover shows the plain name.
- **Part names.** "have a separate enable option for the part names, in parallel to the part outlines": a
  Preferences **Part names** switch (`names` in ui.json; it defaults to the old outlines setting).
  `_part_outlines(..., outlines, names)` and `render_html(names=)`.
- **Board view.** "use ibom or create an ibom style pcb export": they chose our own view, inside the HTML report
  (ARCHITECTURE "HTML report", Board). Their InteractiveHtmlBom is installed (PCM), and KiCad 10 still has pcbnew
  SWIG, if they ever want the real one.
- **Not yet seen by the user:** the board view's interaction (hover, click, zoom). Headless Edge shows the drawing
  only (`out/bv/`).

### Fix advisor for power stages (2026-10-08, session 7, branch `fix-advisor`)
- The user: "the automated suggest fix option is nice but it doesn't really provide incredible results ... help with
  actually properly fixing these issues". Planned and approved: power stages first; bridges and signal nets later.
- Done: ARCHITECTURE "Fix advisor"; README "How to fix it"; Help, Fixes tab; `tests/test_fix_advisor.py`; and the GUI
  test `test_simulate_lists_the_design_changes_and_one_is_checked_in_simulate`.
- **kraft_flux_mini_2 conv1** (real operating point, `tools/dev/fix_check.py`, output in `out/audit/fixes_conv1.txt`).
  Now: radiated −25.5 dB, conducted −19.9 dB, overshoot 39.6 V. Q301's Vds 100 V limits the overshoot target to
  12.9 V. The options:

  | Option | Result | Cost |
  |---|---|---|
  | Filter 1 µH + 2.2 µF (damped, Middlebrook-stable) | conducted +8.7 dB | |
  | 6 ns edge | clears the ringing (5 V); radiated −8.8 dB | 0.82 W |
  | Spread spectrum | +8.8 dB conducted | |
  | Snubber 2.7 nF + 1.2 Ω | radiated −6 dB | 5.7 W |
  | Loop alone (even at 0.73 nH) | not enough | |

  The combined option shows the closest mix and how far it falls short. The radiated figure is board-only (it
  doesn't include cables); see the parked common-mode idea in memory.

### Op-amps in signal paths (2026-10-11, branch `opamp` from `signal-override`; done, NOT merged)
- The user: "some signal paths have an opamp in the line, can we add a function to approximate opamps ... simply,
  amplification factor and maybe optional datasheet parameters that could be pulled in via inventree. like
  bandwidth, common mode rejection etc. if optional parameters are not defined its simply an amplifier." Design
  agreed ("yes"):
  - The op-amp splits the path into stages. Input side: driver → path → op-amp input (load: Cin ∥ Rin), its
    current returns from the op-amp's ground pin. Output side: V_out = A(f)·V_in behind Zout drives the rest to
    the receiver; that current returns from the receiver to the op-amp's ground pin (not the driver). So one
    current loop per stage (plus shunt loops inside each stage), combined like the shunt loops.
  - Part model kind `amplifier` (Parts tab; `pins: [in, out]` for dual/quad op-amps, else found by pin names
    +IN/IN+/INP and OUT): gain (V/V, required, negative = inverting), optional gbw (bandwidth = GBW/|gain|),
    bandwidth (closed-loop -3 dB, wins), z_out (default 1 Ω), c_in (default 2 pF), r_in (default open), slew
    (V/s: caps each output harmonic at SR/2πf, applied to the currents as a factor), cmrr (dB: stored and shown,
    NO effect: the path model is single-ended; tell the user so). Schematic fields (InvenTree): Gain (per design,
    "20dB" = 10), GBW, Bandwidth, Zout, Cin, Rin, Slew Rate ("20V/us"), CMRR. A symbol with a Gain field gets the
    amplifier model automatically (defaults.suggest_model).
- **Done (session 10):** the part model (`components/models.py`: kind `amplifier`, `amp_gain`, `amp_z_in`,
  `amp_z_out`, `amplifier_pins`), schematic fields (`fields.py`: Gain, GBW, Bandwidth, Zout, Cin, Rin, Slew Rate,
  CMRR with `_special` for gain/dB/slew; `defaults._suggest`: a Gain field or GT amp labels → amplifier), GT labels
  `amp_in:PIN, amp_out:PIN` (`tags.AMP`, `amp_label_pins`), routing through the op-amp (`route._amp_pads`),
  `emi/path.PathAmp` / `amplified_path` / complex sources / `PathLoad.z`, `simulate._stage_routes` /
  `_amplified_route` / `_merge_results`, the report's op-amp row, Help (Nets, Parts, labels), README (models,
  fields, labels), ARCHITECTURE ("Op-amp stages", "Several loops are combined per harmonic").
- **A fix found on the way (also for the shunt loops):** loops were combined at the solved frequencies and the sum
  interpolated; the weights' resonances got lost (up to 14 dB low on the op-amp board). `moment_curve` now
  interpolates each loop's moment vector and combines per harmonic. The shunt tests still pass with it.
- Tests (`tests/test_path.py`, 22 pass): op-amp circuit (gain, pole, slew), the op-amp board (two loops, return at
  U5.2, gain 10 vs 1 ≈ +20 dB below 1 GHz), GBW from fields, GT labels on a dual op-amp, the report.
- **Open:** the full suite on `opamp`; merge `signal-override` and `opamp` (ask); install. The user said "I am happy
  to rely on GT labels if that makes life easier" (done: amp_in/amp_out).

### Supply-looking signal nets (2026-10-11, branch `signal-override` from main 49cf52b; NOT merged)
- The user (screenshot: "/CPU/ADC_I_RAIL_1 is a supply net, not a signal"): "This is actually a sensitive signal to
  an ADC that measures the current of that rail 1, it should be allowed to go into the nets tab. We have to find a
  way to either mark them in the schematic as such". Done: measuring words in a name make it a signal
  (`detect.MEASURE_NAME`), and Add KiCad selection asks "add it anyway" for a supply-looking net
  (`suggest_net(as_signal=True)`). On kraft: /CPU/ADC_I_RAIL_1 is accepted (driver guessed U1102.9, the MCU's ADC
  pin: wrong way round, the source is the sense amplifier through R303/C304; it comes unreviewed). Detection
  unchanged on the three real boards (detect_dump identical). Tests: `test_a_net_that_measures_a_supply_is_a_signal`,
  GUI `test_a_supply_looking_net_picked_in_kicad_can_be_added_anyway`.
- Schematic marking: the user chose "GT field on a symbol" (over a KiCad net class): `GT = sig`, `sig:PIN`,
  `sig_drv:PIN`, `sig_rx:PIN` (ARCHITECTURE "Detection labels", README, Help, the Nets tab's info box). Test
  `test_signal_labels_mark_a_supply_named_net_with_its_driver_and_receivers`. For kraft's ADC net the user could
  set e.g. `GT = sig_drv:<the amplifier's output pin>` on U301 and `GT = sig_rx:9` on U1102 (not done: their
  schematic, only on request).

### Shunt parts on a signal path (2026-10-11, branch `path-shunts` from main 58a0c45; merged, installed)
- Milestone 2 of signal paths (the user: "commit merge and continue with next"). Parts from the path to ground or
  a rail are shunts in the circuit and, from 1 % of the current, current loops of their own on the plane models
  (ARCHITECTURE "Shunt parts on a signal path"). Code: `simulate.find_shunts`, `shunt_route`, `_shunt_loops`,
  `_loop_moments`, `ModelRun.loops/loop_m`, `RouteSetup.loop_weights`, `emi/path.PathShunt`, `loop_currents`
  (short groups split by the node's currents), the report's shunt rows, Help. Tests `tests/test_path.py` (16):
  RC low-pass circuit, a shunt at the end = load R, a 50 Ω part at the receiver = load_ohms (0.43 dB at most),
  a mid-path filter C is a loop and a 1 MΩ pull-down isn't, the report. boardgen `series_path_board(shunt=)`.
- Not done: the fix search re-solves only the main loop of a candidate (the shunt loops keep their baseline
  moments); the maps show the receivers' loop only.
- **Regression after the merge** (fixed on branch `fix-shunt-stages`): power stages (`ConverterSetup`) go through
  `band_moments` too and have no `loop_weights`: 50 errors in the full suite (test_power, test_bridge,
  test_fix_advisor, …). Merged before the suite had finished; the targeted tests covered only routes.

### Signal paths across nets, a Thevenin source (2026-10-11, branch `signal-path` from main 6565c1d; merged, installed)
- The user: "simulating a signal path that consist if multiple nets ... specify start end end ... series
  resistors, caps inductors etc along the path ... define an input excitation source in terms of internal
  resistance". Their choices: milestone 1 = series parts only (shunt parts to ground/rail = milestone 2: each a
  return loop of its own, one plane solve each); source = Thevenin (R + L, optional output C; load R ∥ C).
- Done (c62ea03): `emi/path.py` (`path_currents`: cascade per harmonic, complex mean current along the path),
  `simulate.path_elements` / `path_summary` / `RouteResult.path`, NetSpec `fall_time`, `driver_nh`,
  `driver_pf`, `load_ohms`, `through` (parse, write, validate), `trapezoid_harmonics(fall_time=)`,
  `route.find_route` / `signal_nets(through=)` and the blocking-part message (`_blocking`), Nets tab fields (and
  the Update bug: it dropped driver_ohms/load_pf), the report's "Signal path" table, ARCHITECTURE "Line current",
  Help (Nets tab), README. Tests: `tests/test_path.py` (10), boardgen `series_path_board`, GUI
  `test_the_nets_form_keeps_the_source_and_load_on_update`.
- Checked: kraft_flux_mini_2 SPI_SCK + SPI_MOSI (no series parts) against `main`: 907 numbers, largest
  difference 5e-14 dB (the path model reduces to the old line). Full suite in one process: 481 passed, 1 skipped,
  2 GUI failures: the status-wrap test (fails on `main` too, virtual display) and the Bridges tab stall test
  (passes alone). gui_probe on a kraft copy: 152 ms on load and **278-282 ms when the Issues tab is built, the
  same on `main` (270-278 ms)**: an existing stall, not from this branch; worth a look (the rule: < 150 ms).
- Open: merge (ask the user); milestone 2 (shunt parts on the path).

### Memory: free early, threads from memory, models kept only when they fit (2026-10-10, session 8, branch `memory` from main 41a693e; merged, installed)
- Recommendation 3 (the user: "do it", then "keep going and fix the outstanding bits"). Three parts:
  - **Free early** (`solve_setups(free_done=True)` from `simulate` unless `keep_factorisations`): after each round
    the factorisations of models no run still refines are freed. Results unchanged (a freed one is made again
    identically): `test_freeing_finished_models_between_rounds_keeps_every_number`.
  - **Threads from memory** (`memory_threads`, `solve_bytes`, `core/sysmem.available_bytes`, psutil): with
    `analysis.threads: auto`, no more parallel solves than fit in 80 % of the free memory (per solve: estimated
    fill x 16 B x 6.5 SuperLU commit + GMRES basis). ~1 GB per board-wide solve at 1x on kraft, ~4x that at 4x.
    A number the user chose is kept. Test `test_parallel_solves_are_capped_by_the_free_memory`.
  - **Models kept only when they fit** (plugin, `main_frame.keep_models`, `KEEP_MODELS_SHARE` 0.25,
    `SimulationResult.model_bytes` / `drop_models`): after Simulate the models stay for Suggest fixes only while
    they take < 1/4 of the free memory; else they go and Suggest fixes simulates its own baseline. GUI test
    `test_models_too_big_for_the_free_memory_are_not_kept_and_suggest_still_works`.
  - Help updated (Suggest fixes, Solver threads). `tools/dev/mem_profile.py` now prints the models' estimate and
    the memory with them dropped.
- **Measured** (kraft SPI_SCK + conv1, threads auto = 4, `mem_profile.py`, main vs branch): committed peak 9609 ->
  9432 MB, working-set peak 1719 -> 1667 MB, held with the result 1287 -> 1251 MB, 193 / 202 s (noise). Freeing
  early hardly lowers the peak: it falls in the first round, when every model is in use. Dropping the models:
  private 1251 -> 764 MB, working set 550 -> 205 MB (back to the level before the run; ~2.3 GB on the 7-stage
  run). `model_bytes` estimated 288 MB of the ~490 MB freed (low by ~40 %: it errs towards keeping).
  Not measured: the 7-stage run (8 min each way) and the thread cap at 4x (needs a machine short of memory).
- Not done of recommendation 3 then: building and solving models in groups. Done in session 10, see "Solved in groups; window stalls".

### A whole path from a KiCad selection (2026-10-11, session 10, branch `path-from-selection`, on `signal-paths-tab`)
- The user: "Can we select parts of a signal path like from micro to opa to current sense, just one bit of each net
  and then that will automatically add the GT field and parameters like currently is done manually?" Asked where
  the result goes: chose "Settings and the schematic" (falls back to the settings when KiCad can't).
- `analysis/selection.path_from_selection(board, config, nets, pins)`: orders the selected nets by the parts joining
  them (2-pin parts; small ICs, AMP_MAX_PADS, with input pins on one net and an output pin on the other = op-amp
  stage, giving the direction), else direction from the end pins' types / a selected pad; driver, receivers, Pass
  through (non-passing 2-pin parts), op-amp models (amplifier, pins in → out), GT labels (existing kept; an
  op-amp's own sig labels dropped). On kraft: the three ADC_I_RAIL_1 nets in any order give U302.3 → U301 →
  R303 → U1102.9 (as the hand labels did); unrelated nets fall back to adding each net (as before).
- Plugin: "Add KiCad selection" and the toolbar button (the inbox now carries selected pad ids, validated) try a
  path first (`MainFrame.add_nets` → `_add_path`): settings (an unreviewed same-name entry replaced, a reviewed one
  only when the user agrees), models, then `kipy_sink.write_symbol_labels` as a KiCad job.
- **The schematic can't be written yet**: kicad-python marks its Schematic API "KiCad 11", and in the installed
  0.8.0 `kipy.schematic_types` doesn't even import (BusEntryType missing from its protos). write_symbol_labels turns
  that, or KiCad's "no handler", into SchematicApiUnavailable; the window then shows the labels to add by hand
  (Symbol Fields Table). Untested live: KiCad was closed. To do with KiCad open: `out/live_sch/` (a copy of the kraft
  project) for the live check of the selection reading (pad ids) and the fallback message; once a KiCad with the
  schematic API (11) is used, the write itself.
- Tests: tests/test_selection.py; `test_several_nets_picked_in_kicad_become_one_path_with_its_models_and_labels`,
  `test_inbox_requests_carry_the_selected_pads_checked`, `test_schematic_labels_fall_back_when_kicad_cannot_take_them`.

### Signal paths tab (2026-10-11, session 10, branch `signal-paths-tab`)
- The user: "instead of using the nets tab, should we add another one for complete signal paths?" Asked
  (AskUserQuestion): make the Nets tab the paths tab (chosen) / a separate tab / leave it. Every `nets:` entry
  already is a path (driver → series parts, Pass through, op-amps → receivers), so no second list.
- Done: tab label "Signal paths" (help anchor stays `nets`, ui/help.topic_of maps it); columns Path, Status,
  Chain, Freq, Rise, Ampl., Class; the Chain column from `analysis/route.path_chain` (find_route's parts in order,
  values / op-amp gain, shunts to reference nets via the new `route.shunt_parts`, which `simulate.find_shunts`
  now uses too), computed in the tab's LatestOnly worker ("…" until it arrives, "no route: …"), again on every
  settings change (`MainFrame.on_config_change`); the selected path in full under the table; form label "Start
  net"; wording in help, README, messages. Internal names (`nets:`, NetsPanel, NetSpec) unchanged.
- Measured: tab build steps 56 / 20 / 79 ms (build_profile); gui_probe on a kraft copy: one stall at load 167-179
  ms (main: 157 ms, the same stall during the board load), 0 ms on the tab. Tests: `test_a_path_chain_lists_...`,
  `test_the_signal_paths_tab_shows_each_chain_and_the_selected_path`; affected files 136 passed.

### The ADC paths in the plugin (2026-10-11, session 10, branch `adc-path-ui`, merged)
- The user (screenshots of the Nets tab and KiCad): "selecting /CPU/.../.._1 does not go all the way to the U302".
  Their settings file still had detection's entries from before the fixes (ADC_I_RAIL_1 from C304.1, the pieces
  Net-(U301-+) / Net-(U301--), ADC_I_RAIL_2 from R337.2) and U301 / U305 as 'open' (value since changed to
  TLV6001IDCK: kept 'open' with "check model"); Detect never offered a listed name again; the Nets highlight lit
  only the named net.
- Fixed: `highlight._path_nets` (the whole path), `detect.refreshes` + `SettingsFile.refresh_detected` + the
  Detect dialog (REPLACES rows) and `groundtruth detect --write`, `merge_board` (labelled op-amps leave 'open').
  Tried on a copy of the user's settings file: both rails replaced (drivers U302.3 / U304.3, through R303 / R337,
  rail 2 kept off), the two pieces removed, U301 / U305 amplifiers, the highlight 3 nets up to U302. The user
  needs to Reload board and press Detect… (tick the REPLACES rows). Tests:
  `test_stale_detected_entries_are_replaced_and_their_pieces_removed`, `test_selecting_a_listed_net_lights_its_
  whole_path`, `test_detect_offers_to_replace_a_stale_unreviewed_net`.

### Kraft's current-sense ADC paths, end to end (2026-10-11, session 10, branch `fixes`)
- The user: "did you add the GT labels to simulate the ADC_I_RAIL_1 and ADC_I_RAIL_2 from the micro to the
  ZXCT1107 through the resitsor and the OPA (TLV6001DCK). The OPA needs labels too!" The first labels (U301
  sig_drv:4, U1102 sig_rx:9) covered only rail 1 from the buffer's output. Written in place now (label_project,
  backup `out/kraft_backup_2026-10-11b/`): U302 / U304 sig_drv:3, U301 / U305 amp_in:1, amp_out:4, U1102
  sig_rx:9, sig_rx:10. Tried first on copies (board: add_labels; whole project: label_project), diffs checked.
- Four bugs on the way, each with a test that fails without its fix:
  - Detection joined only labelled nets through 2-pin parts: with the op-amp labelled, the path broke into
    three pieces, one with U1102.9 as driver and receiver. `_labelled_path` now walks through 2-pin parts and
    labelled op-amps, trims unlabelled dead ends, counts the path's ends over all its nets; the guessed driver
    is never the labelled receiver (`test_labelled_ends_join_into_one_path_through_an_op_amp`).
  - Route finder: a track end joined only what its end point touched; R303's 0.4 mm track ends 0.02 mm outside
    the rotated pad (its round end inside): "route_missing". Now the end cap counts
    (`test_a_track_ending_just_short_of_a_pad_still_reaches_it`). May join a few more track ends on other boards
    (only overlapping copper of the route's nets, which KiCad connects too).
  - Op-amp stages: the stage after an op-amp paired its part step with the op-amp: R303 (4.7 kΩ) was modelled as
    U301 (1 Ω). Each stage now gets its own parts (`test_a_series_part_after_the_op_amp_keeps_its_own_impedance`,
    `opamp_path_board(r_after=...)`).
  - add_labels.py left an old GT property that nests three levels deep (a second one was read instead).
- Result on the labelled copy (both nets only, 270 s): ADC_I_RAIL_1 U302.3 → R304 (shunt) → U301 (gain 1) → R303
  → C304 (shunt) → U1102.9, worst margin +19.7 dB @ 1590 MHz; ADC_I_RAIL_2 via U305 / R337, +29.1 dB @ 770 MHz.
  The frequency and rise time are detection's generic 10 MHz / 2 ns: the user should set the real ones (the
  sense signal is slow; what matters is the switching noise it carries). The TLV6001's GBW (1 MHz) could go into
  its model (Parts tab) so the buffer's bandwidth counts.
- Also: the GT summary names op-amp labels; a flaky grouping test (free memory) pinned.

### Fixes after the documentation pass (2026-10-11, session 10, branch `fixes`)
- The user: "Fix what you think needs fixing", then "Keep going". Found by reading the code while documenting it,
  by a new test that clicks every button of every tab before and after a Simulate
  (`test_every_button_works_before_and_after_a_simulate`), by ruff (pyflakes and bugbear rules) and by the CLI
  on broken files:
  - Heatmap tab, Draw contours / Draw arrows before any Simulate: `AttributeError` on None (rr.kind read before
    the None check); now "run Simulate first".
  - **Settings left invalid**: `set_converters` (and set_nets, set_modifications, set_component) wrote the new
    list into the document before parsing it. An empty Converters form's Add was refused, but the invalid stage
    stayed in the document, and every later change failed on it (the Parts tab's Apply / Reset). Now all of them
    go through `SettingsFile._replace` (parse first, as set_bridges already did); the Nets tab's commit and the
    Parts tab's edits catch ConfigError like the other tabs (`test_an_invalid_list_is_refused_...`).
  - Preferences: wx 3.3 logged every row ("should be created as child of its wxStaticBox"); the rows are now
    children of their group's box (same look, screenshot checked).
  - CLI: a broken or binary board file now names the file (BoardLoadError) instead of the bare parser message
    or a UnicodeDecodeError.
  - Unused imports (ruff F401), window_shot's deprecated GetHDC.
- New: `tools/dev/py311_check.py` + `tests/test_py311.py`: the plugin's code compiles on KiCad's Python 3.11 (the
  venv's 3.13 accepts PEP 701 f-strings that 3.11 rejects; none found). `undocumented.py` now also finds functions
  inside loops / if / with blocks.
- Looked at and fine: ruff's B023 closures in src are all called within their loop iteration; the thermal job's
  pickles live in a private temp folder only this process and its child write; Python 3.12+ library calls: none.

### Code documentation (2026-10-11, session 10, branch `code-docs` from main; merged)
- The user: "go through the code and add description/summaries above functions/methods. Additionally, inside
  functions/methods explain what is happening and what assumption/technique was used/implemented. The goal is to
  have the code in a state where it can be easily read/understood by a human."
- Done for everything under `src/groundtruth` (core, geometry, components, config, cli, emi, results, ci, rules,
  solver, analysis, report, adapters, ui, and the vendored thermal solver): every function, method and class has a
  docstring (what it does, its inputs and result, the assumption made), and the less obvious bodies have comments
  naming the technique (Woodbury, union-find, scanline rasterisation, Peaceman's radius, Churchill-Chu, radiation
  linearisation, the CSR pattern reuse, the greedy refinement heap ...). One commit per package.
- **No behaviour change, checked per commit**: `tools/dev/ast_same.py` compares every changed file's AST, docstrings
  stripped, with HEAD (it caught one dropped line in peec.py, restored). `tools/dev/undocumented.py FILES` lists
  functions without a docstring: 0 in `src/groundtruth`. Full suite on the branch: 503 passed, 1 skipped (26 min).
- Merged into `main` at the user's "Do it" (plugin installed). Then branch `tools-docs`: every function in
  `tools/` too (the scripts' `main`s and helpers), and `undocumented.py` fixed to also find functions defined inside
  loops, `if` and `with` blocks (it had missed 9 nested helpers in `src`, now documented). 0 without a docstring in
  `src/groundtruth` and `tools`. Not done: `tests/` (test functions whose names say what they check).

### The window's last stalls (2026-10-11, session 10, branch `gui-stalls` from main 380ac5a; merged, installed; full suite 502 passed, 1 skipped, plus the fixed test)
- The user: "Do items 4-6" (tab builds at load 150-260 ms, the KPI tiles after results 180-360 ms, full garbage
  collections 170-410 ms). Measured on an idle machine first (gui_probe, kraft_flux_mini_2 copy, cached results,
  `--threshold-ms 80`): nothing over 150 ms except 162-168 ms (`_fit_page`, a build step); over 80 ms: `_fit_page`
  162, Converters build 155, Bridges form steps 97-131, the Heatmap tab's canvases 107, full collections 133-190
  (stalling the window 88-119 ms).
- Done: `build_steps` for the Nets (2), Converters (3) and Heatmap (2) tabs and five instead of three for Bridges
  (every step ≤ 90 ms in build_profile); `_fit_page`'s layout inside `Freeze`; the KPI tiles reused
  (`Tile.update`); `gil.freeze_long_lived` after the tabs (ARCHITECTURE "Tabs built in steps", "Garbage
  collection", "KPI tiles"). Tests: `test_the_window_builds_its_tabs_in_steps` (20 steps now),
  `test_the_kpi_tiles_are_reused_between_views_and_the_modules_are_frozen_once`.
- After (same probe): worst 132-142 ms in three runs, none over 150; full collections 113-167 ms (they mostly run
  in the render worker; the window waits ~95 ms). What is left over 80 ms: a few 95-107 ms steps at load, the
  first view after results (~135 ms: the Fixes tab's design rows and the first render request), collections.
  Further: freezing the results too would need unfreezing when they are replaced (their cyclic garbage); not done.

### Simulate's multi-second stalls (2026-10-11, session 10, branch `simulate-stalls` from main 25dba52; merged, installed; its full suite 502 passed, 1 skipped)
- The user: "fix this" (item 4: the window stalled 1-6 s during a full kraft Simulate, varying run to run).
- **gui_probe on kraft, all nets and stages, real Simulate** (no cache; `--available 1000000` = no groups, as before):

  | Code | Groups | Stalls > 150 ms | Worst |
  |---|---|---|---|
  | main before groups (3 runs, earlier) | no | 16, 38, 14 | 1114 ms, 6080 ms, 335 ms |
  | this branch, `--available 1000000` | no | 7 | 345 ms |
  | main with groups | yes | 8 | 233 ms |
  | this branch (groups + status line) | yes | 2 | 205 ms |

  The 6.1 s stall was in `_wrap_status`'s `SendSizeEvent` at the end of the job (the bar shrinking from two lines,
  every other thread idle); growing and shrinking the bar with all of kraft's results shown costs 0 ms (new probe
  steps), so it was the state of the machine then: ~19 GB committed on a 17 GB laptop. Without groups it came back
  in none of the later runs (one run, 345 ms), so the cause is not proven; groups halve the committed peak and no
  run with them stalled over 233 ms.
- **The remaining ~220 ms stalls during the solve**: the clock's status rewrite every second made a dozen wx calls,
  each giving up the GIL and waiting up to a timer tick to take it back beside the solver's threads; now two
  (`status_text`, `_char_extents`, only what changed set; ARCHITECTURE "Status bar"). The inbox timer's file reads
  likewise: `_check_inbox` waits while a job runs (a net sent from KiCad meanwhile is added after it). Test
  `test_a_clock_tick_sets_only_the_status_text`.
- **The probe's exit crash** (exit code 139, "Garbage-collecting", in most probe runs this session): gui_probe's
  own. The real file-mode entry (`gui_main`, window closed as a user closes it, after Run checks, every tab, Save
  settings) exited cleanly 6 of 6 times on kraft and 5 of 5 with a Simulate on the test board. The probe let its
  `wx.App` go (a local of `main()`) while its steps' closures still held windows in reference cycles, collected by
  Python's last collection after the app; now it closes the window inside the loop (`f.Close()`), joins its
  watchdog and collects before returning: 4 of 4 clean (3 of 3 crashed before).

### Solved in groups; window stalls (2026-10-11, session 10, branch `speed-groups` from main 49cf52b; merged, installed)
- The user: "fix that" for two open speed items: the Issues tab's build stall (270-282 ms) and recommendation 3's
  models solved in groups.
- **Groups** (`simulate.solve_groups`, `factor_bytes`; `solve_setups` returns the group count, `SimulationResult
  .groups`): when the factorisations kept at once (OPERATOR_CACHE per model, plus `threads` being made) would not
  fit in MEMORY_SHARE of the free memory, the runs are solved in groups, one after the other, each freed before the
  next (only with `free_done`, i.e. Simulate without `keep_factorisations`). A group is made of whole components:
  runs that share a stack (or a derived stack's base) are never split, so each model sees exactly the rounds it
  would see with everything together (`PlaneStack` decides borrowings from its own earlier rounds), and every
  number stays the same. The board-wide components go last: their refinement weighs the route and stage models'
  planned solves (`band_moments`) and the ringing (`planned_solved` hooks run after the first round of the group
  holding the setup's own model). Tests: `test_models_solved_in_groups_when_memory_is_short_keep_every_number`
  (route with plane capacitance; two power stages with conducted refinement and DC paths; identical JSON),
  `test_solve_groups_keep_shared_models_together_and_the_board_wide_last`.
- **Measured** on kraft_flux_mini_2, all 7 nets and stages, 4 threads (`mem_profile.py --available MB` pretends a
  free-memory figure; `--available 1000000` = together): together 406 s, committed peak 19 428 MB, working-set
  peak 3 484 MB; automatic on this laptop (~6-9 GB free): **12 groups, committed peak 10 367 MB (-47 %), working set
  2 412 MB (-31 %)**, 499 s (that run overlapped a 9-min pytest run: time not comparable; factorisations 136 vs
  137). `--available 3000` on an idle machine: 12 groups, committed peak 10 326 MB, working set 2 345 MB, **506 s:
  +25 % time** (a group solves its models alone: less parallel work per round, e.g. the conducted refinement's
  single solves no longer overlap the board-wide ones). `result_diff.py` against together: 9947 numbers, largest
  difference 0, for both. Next step if the time matters: fewer, larger groups (pack against the peak actually
  reached rather than OPERATOR_CACHE per model), or run the next group's first round beside the last one's
  refinement.
- **The Issues tab**: no longer stalls. build_profile: Issues 24 ms; gui_probe on kraft (all nets and stages):
  "tab Issues" 0 ms in four runs. The list's paint alone (30 rows) is ~80 ms idle, ~2.7 ms a row, longer with
  long messages (`issues_probe` in the session scratchpad; not a problem now).
- **What does stall** (gui_probe on kraft, all nets and stages; `gui_probe.py` now lists garbage collections over
  50 ms and the page faults within each stall, and `--cache R.pickle` reuses one Simulate's results, ~1 min a
  probe instead of 8):
  - During a real Simulate: 1.1 s, then 6.1 s (in `_wrap_status`'s `SendSizeEvent` when the job ended, every other
    thread idle), then 0.3 s in three runs: varies run to run, absent with cached results. The full run commits
    ~19 GB on this 17 GB laptop: paging is the likely cause (not proven: the page-fault counter came after those
    runs). Groups halve the committed peak, which should help; recheck with gui_probe (no cache) and its fault counts.
  - With cached results (the window alone): the first view's page heights measured text on the GUI thread
    (`plots._advance`, 307 ms with 13 500 page faults): now `plots.warm_text` runs in the render worker when the
    Heatmap tab is built (test `test_the_usual_characters_are_measured_in_a_worker_before_the_page_needs_them`).
    Left, pre-existing: building the Nets / Heatmap tabs and AddPage 180-260 ms at load, the KPI tiles 211-357 ms
    after results (inside `theme.Tile`'s native constructor: GIL / paging, not its own work), full garbage
    collections 170-410 ms (gen 2; they include finalizers, so not all of it blocks).
  - `tools/dev/gil_probe.py`: the report page and heatmap renders in a worker while this thread asks for the GIL
    every ms (plugin interval): longest waits 62-133 ms. The spread-spectrum receiver (`emi/spread.py`, 3 threads)
    29-38 ms. So neither holds the GIL long by itself.
  - The probe's teardown crashed once more with a segmentation fault after all steps (the known intermittent
    exit crash, "Exit crash" below); the next run with `-X faulthandler` exited 0.

### Grid note for resonances above 1 GHz (2026-10-10, session 8, branch `grid-note` from main ca9d6dc; merged)
- Recommendation 5 done on this branch: the whole suite in ONE process, 467 passed, 1 skipped, 1 failed
  (`test_plugin_start_never_blocks_its_window[answers slowly]`, a timing test: other tests ran beside it; alone
  3 of 3 passed), 27 min, no MemoryError (it ran out of memory before the memory work). CLAUDE.md updated.
- The user chose option 1 of three for the board-wide model's grid sensitivity above 1 GHz ("option 1 is good"):
  `simulate.grid_sensitive_note` adds a route note when its worst emission is above 1 GHz, comes from a plane
  resonance (loop moment >= 6 dB over its low-frequency value) and is within 10 dB of the warning level; it names
  the next resolution. ARCHITECTURE "Board-wide resonances above 1 GHz", Help (Preferences > Resolution). On kraft
  SPI_SCK: 1x worst +6.8 dB at 740 MHz, no note; 2x worst +7.0 dB at 1220 MHz with the note ("Check it at 4x").
  Test `test_a_worst_emission_from_a_plane_resonance_above_1_ghz_gets_a_grid_note`.

### Bridges tab built in steps (2026-10-10, session 8, branch `bridges-tab-steps` on `exit-code`; merged at the user's "start mergin what can be merged")
- Recommendation 4 (the user: "do it" for 3-6). The Bridges tab blocked the GUI thread 0.32-0.43 s while the window
  built its tabs, and `test_the_window_builds_its_tabs_in_steps` failed one run in four (on `main` too). Now
  `BridgesPanel(in_steps=True)` builds lists and buttons, then its form in three `build_steps` (`_form_drive`,
  `_form_parts`, `_form_legs`), which `MainFrame._build_pages` runs one event-loop step each in the hidden holder.
  build_profile: Bridges 60 + 97 + 67 + 107 ms; the test passed 5 of 5; gui_probe on a kraft copy: one stall over
  150 ms on opening (Converters 184 ms) instead of two (314 + 182 ms). GUI tests: 53 passed.

### Exit code 120 at the end of a run (2026-10-10, session 8, branch `exit-code` from main 10f93c9; merged with `bridges-tab-steps`)
- The user: "fix it" (the small things). A CLI run with its output redirected sometimes ended with "Exception
  ignored on flushing sys.stdout: SystemError" and exit code 120 (pytest runs too). Cause: a SuperLU host thread
  (daemon) still alive at interpreter teardown after making a factorisation. Fixed: `_lu_exit` (atexit) ends and
  joins the hosts; ARCHITECTURE "The hosts end at exit"; test
  `test_a_run_ends_with_its_own_exit_code_while_factorisations_are_alive`. `plugin/demo.zip` (untracked) was gone
  from disk by then (not deleted by Claude).

### Board-wide grid convergence, tracks kept joined (2026-10-10, session 8, branch `cavity-convergence` from main e88186c; merged at the user's "comit and merge")
- Item 2 of the recommendations: the board-wide model's grid convergence (SPI_SCK's 740 MHz resonance read 34.0 /
  23.5 / 29.0 dBµV/m in three Simulates at 1x / 2x / 4x). `tools/dev/cavity_converge.py` solves the board-wide
  model on the SAME frequencies at several resolutions (`out/converge/*.json`, `*.log`); `cavity_hotspots.py` shows
  where a resonance sits (displacement current per cell, branch currents).
- **740 MHz converged:** |m| (loop moment per ampere = effective loop area; 2x the area = +6 dB) peaks at 77 / 79 /
  77 mm² at 750 / ~745 / ~730 MHz on 1x / 2x / 4x (the trace loop alone: 19.7 mm²). The old spread was Simulate
  sampling the narrow peak at different harmonics on each grid.
- **Bug found and fixed (`_rasterise_lines`, ARCHITECTURE "Tracks of plane nets stay joined"):** a plane net's
  track walk stopped at cells another net owns, cutting tracks apart (213 / 87 / 74 cuts at 1x / 2x / 4x). A 78 mm
  +3V3 track on In3 floated at 1x and 2x and resonated at ~1.24 GHz: |m| 258 / 225 / 63 mm², the board's worst
  SPI_SCK margin at 1x (+3.2 dB at 1260 MHz). A GMRES tolerance of 1e-7 / 1e-8 gave the same 4x value (62.97 /
  62.99 mm²), so it was the model, not the solver. Now the walk follows the track's own cells and bridges real gaps
  with a lumped branch (`Grid.bridges`, `track_impedance`, kind `track`). Conductive pieces 29 / 38 / 19 → 14 / 5 /
  5. After the fix, 1x: worst +6.7 dB at 740 MHz.
- **Open (next, the user's "do it after fixing the smaller things"):** above 1 GHz the grids still disagree. 4x is
  smooth (24-66 mm²); 2x has a peak at 1220 MHz (183 mm², +4.1 dB), the V_BATT pour on F.Cu over +VDC on In1 tied
  through Q601 (6.5 mΩ model): the largest displacement current at (126-128.5, 113-115), Q601 2.4 A per A; 1x has
  peaks at 1100 and 1440 MHz. 4x needs 3-10x the GMRES iterations (980 MHz did not converge once, 867 iterations).
- Also found: a 32 x 1.4 mm +VDC pour on In3 (x 126-158, y 110-115) is floating on the real board (its two vias'
  rings don't touch it); the user: "Ignore for now". Via-layer connections missing at 1x (446 of 2262) are vias
  through another net's plane: only their unconnected ring is there, no difference electrically.
- Tests: `test_a_track_through_another_nets_pour_stays_joined_on_every_grid` (cut at 0.86 mm without the bridges),
  `test_a_track_bridge_has_the_tracks_resistance_and_partial_inductance` (DC R and Ruehli's bar inductance within
  5 %). Full suite: 255 + 1 skipped, 165, 45 of 46 GUI: `test_the_window_builds_its_tabs_in_steps` is a timing test
  near its 0.35 s limit on this laptop, failing one run in four on `main` too.

### Reproducible results (2026-10-10, session 8, branch `deterministic` from main a068aeb; merged 444bad0, installed)
- The user asked "any other improvements you would recommend?" and answered the list with "do it". Item 1 of it:
  the same board and settings must give the same numbers (two runs of `main` differed by up to 0.0022 dB and solves
  by ~20 % in iterations), so the History tab's comparisons show only real changes. Item 2, next: the board-wide
  model's grid convergence (SPI_SCK's 740 MHz resonance: 34.0 / 23.5 / 29.0 dBµV/m on three grids).
- Cause: `PlaneStack.operator` let a frequency borrow the nearest factorisation *cached at that moment* (which solve
  of a parallel round finished first, what `OPERATOR_CACHE` had evicted), and the wider borrow window and the borrow
  budget used measured seconds (`t_fact`, `t_it`).
- Now (ARCHITECTURE "Reuse", *Reproducible*): every factorisation decided is recorded (`_Made`: frequency, round,
  the frequency it is factorised at, home iterations), a frequency's choice is made once (`_plan`) from the
  records of earlier rounds only, `solve_setups` calls `next_round()` on every stack before each round and after
  the last; a freed factorisation is made again identically; home iterations = the fewest of the record's own
  round; budget `1.5 x home + 15` iterations (`BORROW_BUDGET`, `BORROW_EXTRA_ITS`); the ±60 % window
  (`PRECOND_REUSE_FAR`, `REUSE_FAR_ITERATIONS`) is gone (never reached on kraft: 5-79 iterations per factorisation).
- Measured on kraft_flux_mini_2 (`out/audit/kraft.groundtruth.yaml`, CPU): SPI_SCK + conv1 two runs at 4 threads
  and one at 2: all 3505 numbers and every iteration count identical (`result_diff.py`, md5 of the iterations);
  170 s vs 184 s on `main`, 47 vs 49 factorisations, 1965 vs 2041 GMRES iterations, 8 borrows and no budget
  fallback (main: 7 and 1, a 796 MHz borrow 0.25 away that ran into its budget, 135 + 63 iterations). All 7 nets and
  stages, two runs: all 9904 numbers identical, 403 and 407 s (`main` earlier: 459-464 s).
- Tests: `test_a_simulate_does_not_depend_on_the_order_its_solves_finish` (two nets 50/55 MHz sharing the
  board-wide model, solves run in order and reversed: fails on `main`), `test_borrowing_does_not_depend_on_what_
  finished_first_or_is_still_cached`, `test_the_borrow_budget_counts_iterations`, the two borrow tests updated.
  test_speed, test_solver, test_multilayer, test_capacitance (32), and test_whatif_suggest, test_capacitance,
  test_power, test_sharper, test_unsolved, test_fix_advisor (63) passed; then the full suite in three batches:
  255 passed + 1 skipped, 163 passed, 46 (GUI) passed.
- Not covered: the GPU backend (its sums need not be ordered). Seen on the way, not from this change (also with
  `main`'s code): Python sometimes exits with 120 after "Exception ignored on flushing sys.stdout: SystemError"
  when output is redirected; the report is complete.

### Solver memory (2026-10-10, session 8, branch `solver-memory` from main 79b2f60; merged bacbbba, installed)
- The user pasted a task: "Thoroughly Investigate and Optimise Memory Usage in the PCB Solver" (profile, find the
  causes, implement, validate on small / representative / large boards, repeated runs, failures, report).
- Done: ARCHITECTURE "Memory use of a Simulate" (causes, changes, the measurements, what is left). In short:
  kernels stored once as quadrants, the module kernel cache bounded by bytes, `OPERATOR_CACHE` 2, factorisations
  freed after the solves (`keep_factorisations` for the fix search's baseline), solutions without the unknown
  vector (`state_for`), the GMRES basis grown on demand, and the `HostedLU.__del__` race fixed (on `main` since the
  free-memory merge: a dropped factorisation could still be freed in the wrong thread). kraft_flux_mini_2, all 7
  nets and stages, 4 threads: committed peak 29.7 → 18.8 GB, working set 5.5 → 3.4 GB, same time; results within
  0.022 dB (main against itself: 0.0022 dB).
- Tools: `tools/dev/mem_profile.py`, `mem_boards.py`; the baselines ran from a `git worktree` of main.
- Next (not started): SuperLU's committed slack (~6.5x fill) is most of the committed peak; a different sparse
  solver would remove it (new dependency). Building models per route instead of all first.
- **A different sparse solver: discussed and parked** (the user, 2026-10-10: "let's leave it for now"). A
  complex-symmetric LDLᵀ (PARDISO via MKL; UMFPACK has no symmetric mode and is GPL) would halve the fill and
  remove the slack (estimate: committed peak ~9-10 GB instead of 18.8 on the 7-stage board) and factorise faster.
  Open questions before it: complex support in `pypardiso` / Windows wheels of `pydiso` for Python 3.11 and 3.13;
  ~150-250 MB of MKL in the plugin venv; MKL's OpenMP runtime beside PyTorch's on the GPU machines (OMP Error #15)
  and MKL threads x our 4 solve threads; results change at solver-tolerance level (revalidate on the real boards,
  the resonances); SuperLU stays as the fallback (CI, ARM). Suggested first step: a dev-venv-only prototype on
  kraft_flux_mini_2's board-wide matrix (memory, factorisation and solve time, iterations, results).
- Found on the way and fixed here: stacked boards (`stack:`) named their other boards relative to the settings
  file's folder, which is `groundtruth/` since the project-folder merge, so `test_stack.py` failed on `main`
  (3 errors). `SettingsFile.config` now sets `base_dir` to the board's folder.
- Tests (2026-10-10, in three batches): 155 + 44 + 258 passed, 1 skipped. Not passing: `test_a_long_status_message_
  wraps_instead_of_being_cut_off` (also on `main` and at 77b467e, where it passed in the morning: Windows reported
  one 640x480 virtual display, "WinDisc", no screen attached) and `test_the_bridges_tab_detects_a_motor_bridge_...`
  (GUI stall 0.50-0.58 s against 0.5 s, failing and passing on both `main` and the branch: the loaded laptop).
- Scenarios (`out/mem/`): different boards in one process (`mem_boards.py`, test board and kraft, 2 rounds) after
  each run 760, 779, 775, 779 MB private; failures (`mem_probe.py`, out of memory / error / Cancel, 3 each) back to
  the level before them.

### Switching rows off (2026-10-10, session 8, branch `enable-rows` from main 7153e3e; merged 07d8d85, installed)
- The user: "if one wants to not simulate a particular net, converter 'row' one has to delete it which then looses
  all the data entered. It would be good if one could simply enable/disable it to be exluded from the next sim
  run". Done: `enabled` on NetSpec / ConverterSpec / BridgeSpec, `Config.active`, a tick box per row on the Nets,
  Converters and Bridges tabs (greyed when off), CLI notes and `--nets` to run one anyway; Help, README,
  ARCHITECTURE (`config/settings.py`). Tests: `tests/test_enable.py` (4), one GUI test.

### Memory kept after a Simulate (2026-10-10, session 8, branch `free-memory` from main 355d064; merged 86c026b, installed)
- After the merge: the KiCad plugin venv has scipy **1.17.1** and leaks the same way (82 MiB per factorisation
  freed from another thread, `same` thread: none). kraft_flux_mini_2 copy (SPI_SCK + conv1, `mem_probe.py
  --fail memory --warm 2 --repeat 2`): 248 → 252 MB after two runs, 254 MB after two failed runs (peak 801 MB);
  before the fix one failure kept 528 MB and every run added more.
- The user (Task Manager screenshot: the plugin at 5.4 GB): "after a faild sim, memory is not released".
- **Cause** (`tools/dev/mem_probe.py`, new): scipy 1.18's SuperLU wrapper books its C allocations per thread; a
  factorisation freed from another thread than the one that made it never gave its memory back. Factorisations
  were made in `run_parallel`'s pool threads and dropped from the job thread, so every Simulate on 4 threads kept
  ~640 MiB private (8 × 63.4 + 8 × 15.9 MiB regions), successful or failed; with 1 thread nothing. **Fix:**
  `peec.hosted_splu` (host threads make and free every factorisation). Also `ui.progress.release_failure` in
  `MainFrame._start`: a failed job's exception chain held its models in a reference cycle until a gc.
- Measured (synthetic board, 4 threads): private 1341 → 3257 MiB over 4 runs before, 706 → 744 after; failed
  runs (out of memory, an error, Cancel) return to the level before them. ARCHITECTURE "Preconditioner" (Memory)
  and "Threading". Tests in `test_solver.py` and `test_progress.py`.
- Found on the way, not changed: the dev venv imports PyTorch for `analysis.gpu: auto` (~150 MB once); the
  plugin venv has no PyTorch.

### The history folder is `groundtruth\history` (2026-10-10, session 8, branch `history-name` from main d4abdcb; merged 9de1324, installed)
- The user: "just call it [history] and not ... groundtruth\groundtruth-history. It's already under groundtruth".
  `HISTORY_DIR = "history"`; docs, Help, Preferences, CLI help and tests follow. Quotes of the user that name
  `groundtruth-history` are kept as said. The user's folder from earlier today, `groundtruth\groundtruth-history`,
  is not read any more: rename it to `history` (its `runs` and `boards` straight inside).
- At the user's word, the uncommitted changes to the demo board (`plugin/demo/groundtruth_demo.kicad_pcb`,
  `.kicad_pro`, `groundtruth/groundtruth_demo.groundtruth.yaml`) were reverted (`git restore`); the untracked
  `plugin/demo.zip` was left.

### No folder per board in the history (2026-10-10, session 8, branch `flat-history` from main 86a095b; merged ffba9e6, installed)
- The user: "there is not reason to create a folder called kraft_flux_mini_2 under groundtruth-history, just use
  runs". Now `groundtruth/groundtruth-history/runs/` and `boards/`; `History(root, board=)`: `runs()` only the
  board's (by `run.json` `board.name`), `all_runs()` for sharing, deletes and renames. Test
  `test_two_boards_in_one_folder_share_the_history_folder_and_keep_their_runs_apart`.
- The user's kraft history from earlier today sits in `groundtruth-history\kraft_flux_mini_2\` and is not read
  any more: move its `runs` and `boards` up one level, or delete it.

### Board copies named after their run (2026-10-10, session 8, branch `board-names` from main f387c07; merged bc957b3, installed)
- The user, looking at a real history: the copy was `920e…c73.kicad_pcb.gz`, the run `2026-10-10_08-11-40`: "This
  should match better, maybe simple call the PCB zip file the same as the run". Now `boards/<run id>.kicad_pcb.gz`,
  named after the run that first kept that board; later runs on the unchanged board name it in `run.json`
  (`board.file`); deleting that run renames the copy after the next run using it. Runs from before keep finding
  their hash-named copy (fallback by SHA-256). ARCHITECTURE "Simulation history" *Layout* and *Untrusted on read*;
  tests in `tests/test_history.py` (sharing, rename on delete, forged names, old hash-named runs).

### The board's groundtruth folder (2026-10-10, session 8, branch `project-folder` from main 4e58a65; merged 172909d at the user's "commit, merge, and install", installed)
- The user: "can we have a kraft_flux_mini_2\groundtruth\ folder and in that folder ... groundtruth-history ...
  also in that folder save the kraft_flux_mini_2.groundtruth file and any other files that belong there. Also by
  default offer that folder first when saving a report. This keeps the kicad root folder cleaner". Asked about the
  existing files: "this is a new plugin, don't worry about compatibility with previous version". Asked
  `groundtruth\` or `.groundtruth\`: Claude recommended the plain name (user-facing files; a dot hides nothing in
  Explorer); `settings.PROJECT_DIR` changes it.
- Done: `settings.project_dir` / `settings_path` (only `<board folder>/groundtruth/<board>.groundtruth.yaml`;
  `_write_atomic` makes the folder), `history_for` under it, Export report's dialog opens there (made when
  missing); the demo's settings moved to `plugin/demo/groundtruth/` (with the user's uncommitted edits to it, git
  mv); `make_demo.py`, `gui_probe.py`, `window_shot.py`; tests (conftest `make` uses `settings_path`; `init`
  leaves only `groundtruth/` beside the board, a settings file beside the board is ignored; Export offers the
  folder). README, Help, ARCHITECTURE, CLAUDE.md, gitlab-ci example, GPU README.
- **The user's boards:** `kraft_flux_mini_2.groundtruth.yaml` (and any `groundtruth-history/` from 2026-10-10) in
  the KiCad root are no longer read: move them into `kraft_flux_mini_2\groundtruth\` by hand (Claude does not
  write next to the real boards), else the plugin starts a new settings file and detects again.
- The CLI's `--out-dir` stays `.` (CI artifacts); the 3D pages stay in `%LOCALAPPDATA%\GroundTruth\views`.

### Simulation history (2026-10-09, session 8, branch `history` from main c2b6b99; merged 77b467e, installed)
- The user: "it would be helpful if the simulation has a history of the results. I imagine a folder that contains
  the history of that board and is located under the projects root and every time the user simulates a copy of the
  PCB is saved in there and a record is created with the results." Their answers to four questions: folder
  `groundtruth-history/` (recommended), board copies shared + compressed (recommended), each record with the
  results as JSON **and** the HTML report (not the recommended JSON only), a History tab with compare
  (recommended).
- Done: ARCHITECTURE "Simulation history"; README "Simulation history" and the CLI; Help "History tab" and the
  Preferences rows. `report/history.py` (core, no wx), `ui/history_panel.py`, `ui/background.InOrder`,
  `Session.board_copy` / `kipy_source.board_text`, `MainFrame._history_inputs` / `keep_run` / `_keep_report`
  (`_report_args` now shared with Export report), Preferences **History: Keep runs, Report**, CLI
  `simulate --history` and `groundtruth history [--compare] [--json]`, `window_shot.py --runs N` (History tab with
  the two newest compared).
- **Tests:** `tests/test_history.py` (9), GUI `test_simulate_keeps_each_run_in_the_history_tab` and
  `test_the_board_copy_comes_from_kicad_with_unsaved_edits_else_from_the_saved_file`. `tests/conftest.py` now
  gives every test a private `%LOCALAPPDATA%` with `{"history": false}` (the GUI tests read the user's real
  ui.json before: their resolution 2× made them slower); `test_export_report_writes_the_page_in_the_background`
  writes the same into its own ui.json.
- The user, after the first version: "make sure that manually deleting data like the pcb files or the report
  doesn't crash the feature" → ARCHITECTURE "Simulation history", *Deleted or edited by hand*, with two core tests
  and the end of the GUI history test (report, board copy, whole folder deleted while the window is open).
- **Test status (2026-10-09):** the full suite in one process ran out of memory on this laptop (the user's own
  GroundTruth window held ~0.9 GB): `test_power.py` failed and errored in a block, then a stack overflow in a
  solver thread ended the run (exit 127). Rerun in parts: `test_power.py` 34 passed alone; `test_gui.py` 44 of 45
  (all history tests pass); the files after `test_power.py` 145 passed; the earlier files passed in the full run;
  `tests/test_history.py` 11 passed. The one failure is `test_the_window_builds_its_tabs_in_steps`: the **Bridges**
  tab's build step takes 0.35-0.39 s against its 0.35 s limit on `main` too (`build_profile.py`, alternating
  main/branch; the History tab's own step is ~60 ms), so not this branch; on `main` the test read the user's
  ui.json and happened to pass. Fix option: build the Bridges form in a second event-loop step.
- **gui_probe** (kraft_flux_mini_2 copy, SPI_SCK + conv1 only: the full settings ran out of memory beside the
  user's window; private prefs at 1×): run kept (board 4.9 MB → 1.0 MB gz, results 0.2 MB, report 2.9 MB written in
  the background while the probe clicked through the heatmap views); stalls as before (Bridges tab build 324 ms at
  load, ≤ 297 ms GIL during the solves, ≤ 179 ms in two heatmap steps); none in the History tab.
- **Not tried live yet:** `get_as_string` on the user's KiCad (kraft board: 4.9 MB of text over the API; size and
  time unknown; if KiCad refuses, the run says "saved file"). Ask the user to Simulate in KiCad and look at the
  History tab, and at the run's `run.json` `board.source`.
- Possible follow-ups (not asked for): Thermal / Current capacity added to the latest run; a "Load this run"
  that opens the board copy with its settings in a GroundTruth window (file mode); a size limit or "keep the last
  N" for the folder; a `.gitignore` hint.

### Next steps (session 6, continued)
000. Ask the user to open kraft_flux_mini_2 in KiCad and check the labels (Symbol Fields Table, GT column; Update
   PCB from Schematic should report no changes for them), and the live API read in the plugin (the Converters tab's
   line should list conv1..conv4); (merged already, at their word). Still open: the bridge data above.
00. `readable-plots` merged at the user's "commit and merge" and the plugin reinstalled: ask how the larger text
   and the scrolling Heatmap tab read in KiCad (restart KiCad), and whether Ctrl + wheel for the map zoom suits
   them. Open from it: the two wx GDI messages after a power stage's Simulate (also on `main`, see above).
0. Milestone 1 shown; the user: "Merge now" and, for the 3D view, "Yes, start it". Milestone 2 (see "C. 3D view"
   in "Plane displacement and the cross-section") done and merged at the user's "Merge now". The user has not
   tried Open 3D view in KiCad yet (restart KiCad: the plugin is installed); ask how it reads to them (spacing,
   labels, the board-wide view). Still open from earlier: "add an option to
   draw all arrows on all layers at once" (where to put each layer's arrows: one User layer per copper layer, or
   all on User.7; the user rejected drawing every via, see "Carrying vias").
1. **GPU option** (the user, 2026-10-06, after the GPU verdict: "We should have an option for GPU though, this
   laptop is used for development but the plugin will run on a laptop with a much bigger GPU or even a big
   server"; their answers: GPU vendor "Mixed / not known yet", library "PyTorch", sim-speed "Commit + merge
   first"): an optional PyTorch backend for the solver iterations, on branch `gpu-torch`. The plugin from
   sim-speed is installed; the user has not seen the status line, drain/source markers or the speed-up yet.
2. The user's choice among the three arrow follow-ups above (none chosen yet).
3. Done: `m3-thermal` merged and the plugin reinstalled (2026-10-05). Ask the user to restart KiCad and try Thermal and Current capacity.
4. M3 items 5 (part bodies / heatsinks: JEDEC θJC/θJB two-resistor model) and 6 (enclosure, mounting, solder-mask
   openings: PCB Thermal's solver has the lumped enclosure and mounts) were next in Claude's suggested order; then
   2 (A/B maps), 3 (sweeps), 4 (free DC path), 8 (sharper thermal maps). Ask before starting.
5. Still the user's: Q601 resistor 6.5m, real rds_on / dcr per stage (the capacity and temperature numbers above
   use assumed 4 mΩ / 5 mΩ), their verdict on the M1 look. They set SW1's 482 pF, 3 mΩ, 6.3 mΩ themselves and
   can now enter `q_oss` 38 nC. The board is 6-layer now: capacity and arrow results need a rerun.

## Session 4: real operating points, spread spectrum

Merged into `main` (fast-forward to e2bcf21, 2026-10-05, at the user's request). The user gave the real operating points and pointed to the
schematic notes; the user's standard is MIL-STD-461 (CE102). `tools/dev/real_settings.py BOARD OUT [--spread-mode
random|triangle|off --spread-rate HZ] [--test]` builds the scratch settings file with all of it (sources in its `SOURCES`).

| Stage | Operating point | Source |
|---|---|---|
| SW1 (Rail 1, J104) | 51.8 → 28 V, 10 A sustained, 787 kHz, spread 0 to +20 %, C_OSS 616 pF | user; LTC7891 datasheet f = 37 MHz / R_FREQ with R313 = 47k; EPC2306 datasheet |
| SW2 (Rail 2, J106) | 51.8 → 5.4 V, 5 A, otherwise as SW1 | user |
| LM5008 (+12V) | 51.8 → 12 V, 385 kHz, 0.22 A (design max; typ ~0.11 A) | schematic note (R_ON 249k); load traced: LT3970 input + LTC7890 EXTVCC gate drive (4 × 12.3 nC × f) |
| LT3970 (+5V) | 12 → 5 V, 557 kHz, 0.14 A | schematic note (R_T 249k); loads: BD33 LDO → 3V3 (STM32 etc.), R1S-0505 → isolated CAN |
| Q601 | 6.5 mΩ | schematic note (surge stopper sized for 15 A) |

Spread spectrum is on (PLLIN/SPREAD tied to DRV_CC through R320 0R; schematic note). Burst Mode at light load
(MODE pulled down). The datasheets (LTC7891 given by the user; analog.com blocks downloads) give the range but not
the modulation shape or rate.

**Results (787 kHz, `out/sim_spread_*` and `out/sim_real` reports, not committed):**
- SW1: hot loop 3.43 nH; L·di/dt 17 V; ringing 109 MHz Q 15 → **overshoot ≈ 43 V on 51.8 V ≈ 95 V peak vs the
  EPC2306's 100 V** (assumes r_damp 0.1 Ω and 2 ns edges: ask the user to measure the switch node).
- Current density at 10 A: output L301 → J104 131 A/mm² (error, 7.8 mΩ, 0.78 W incl. 0.40 W in the shunts);
  input J101 → Q301 (5.4 A) 114 A/mm² at Q601's +VDC pad (error); ground return 103 A/mm²; SW2's B.Cu ground neck
  ~118 A/mm².
- CE102 margins (basic 28 V curve; 51.8 V would relax it by ≈ 2.7 dB): SW1 −28.9 / −27.7 / −20.1 dB and SW2
  −15.8 / −12.2 / −4.2 dB for no spread / random every cycle / triangle 10 kHz; LM5008 +19.1 dB; U503 not
  estimated (fed from +12V). Spread spectrum alone does not pass: an input filter is needed (SW1 corner
  ≈ 100-175 kHz).
- Radiated still against CISPR 32 B (RE102 not implemented): SW1 −19 dB, SW2 −13.5 dB. CM budget 0.35-1 pF.

**Current arrows (session 4; branch `current-arrows` merged into `main` at ee87e2e and deleted; plugin reinstalled):** every heatmap (plugin, HTML, CLI PNGs) shows arrows for the current direction: equal small arrows whose number follows the current density (the user asked for that instead of arrows of varying length); the plugin's Heatmap tab has a "Current arrows" checkbox and a "Draw arrows on board" button (User.7, group `groundtruth-arrows`). See ARCHITECTURE "Current arrows". Checked on kraft_flux_mini_2 (SW1 hot loop, SPI_SCK return, `out/arrows/`). **Tried live (2026-10-05, KiCad 10.0.5, scratch board):** `tools/dev/live_kicad.py write out/live --keep --shot …` drew 222 arrows (666 segments, one `groundtruth-arrows` group) of the In1.Cu return current on User.7, next to the markers, contours and suggestions; `--keep` leaves the drawings for the user to inspect and clear. The user looked at it in KiCad and confirmed it works well. Never on a real design.

**Plugin start-up: "KiCad froze for a moment" (session 4; branch `plugin-startup`, merged into `main` and
deleted at the user's request).** The user
saw KiCad freeze after clicking the toolbar button ("This can't happen"). Measured on kraft_flux_mini_2 (read-only,
with the user's consent; `tools/dev/live_kicad.py timing --expect … ` and `freeze --seconds …`):
- KiCad's GUI thread was **never blocked longer than 46 ms** while the button was clicked and the board was read
  (the plugin's API reads: footprints 63 ms / 18 ms GUI block, zones 39/14 ms, pads per layer 12 ms).
- KiCad started the plugin (venv `Scripts\pythonw.exe` → KiCad's `bin\pythonw.exe`, parent = KiCad) and its window
  appeared **≈ 14 s later**, with nothing on screen meanwhile. Run by hand in the same venv, the same steps take
  ≈ 2.5 s (imports 1.7 s, connect + get_board 0.2 s, window 0.3 s), so the rest happens only under KiCad's launch.
  The cause is still open: the new start-up log records it on the next click.
- KiCad 10 starts an action's entry script **without** plugin.json's `args` (source: `API_PLUGIN_MANAGER::
  InvokeAction` calls `Execute({ pluginFile })`; the live command line had no "analyse"). So "Clear EMI layers"
  opened the main window instead of clearing.
- "Detect…" on the Converters tab ran `detect_converters` (4 s on this board) on the plugin's GUI thread.
- **Not the plugin's fault but worth knowing:** when the user closed the plugin window, it saved a new
  `kraft_flux_mini_2.groundtruth.yaml` next to the real board (settings live next to the board by design; a live
  test of the plugin on a real board writes there). The user asked why the Nets tab was empty: that file had no
  nets (the CLI runs use scratch settings under `out/`).

Changes on the branch:
- `ui/app.py` `StartWindow`: a small "GroundTruth – starting" window shown right after Python starts, with the
  step (connecting, loading the analysis modules, opening the window).
- `ui/startup.py`: start-up log at `%LOCALAPPDATA%\GroundTruth\plugin.log` (process creation, script start, each
  step until "board read"; times and counts only, trimmed at 200 kB).
- `plugin/groundtruth_clear.py`: the "Clear EMI layers" action's own entry script; `args` removed from plugin.json.
- Detect… on both tabs runs as a frame job (`MainFrame._panel_job`).
- A board whose settings list neither nets nor power stages (new file or empty) is auto-detected in the
  background, like `groundtruth detect --write` (orange until reviewed). Empty lists show a hint.
- The window title is "<board> – GroundTruth".
- `live_kicad.py freeze` also logs when KiCad's windows get disabled or the cursor turns busy (a modal loop pumps
  messages, so the WM_NULL probe alone would miss it) and each plugin window title.
- `ui/kicad_watch.py`: the window closes itself (saving settings like the close button) once the KiCad that
  started it exits. Found live: the user closed KiCad and the plugin window stayed open, orphaned. Windows: KiCad
  is the grandparent (venv launcher in between), found with a Toolhelp snapshot and watched through a SYNCHRONIZE
  handle (1 s wx.Timer, no API traffic, skipped while a modal dialog is open); POSIX: the parent pid changes.

## Session 5 (2026-10-05, after an account switch): "it should never not respond"

- The full suite on `main` (95afe96) passed: 170 passed, 1 skipped. The session-4 merge was fine.
- The user saw a test window marked "(Not Responding)" during that run and said **"it should never not
  respond"**. It was `test_gui.py`'s last frame: wx deletes a top-level window only at the next idle time of an
  event loop, which never came once the test's loop had ended, while pytest ran other tests on that thread.
  Tests now close windows with `close_window` (Destroy + one `wx.Yield`); the exit message "UnregisterClass
  failed … Class still has open windows" is gone with it.
- Taking the principle seriously, every GUI-thread stall of the plugin window was measured on the real board
  (`tools/dev/gui_probe.py`, on a copy) and removed. **`gui-responsive`** (from `main` 95afe96, merged
  later): start-up connects and imports in a worker (start window with a pulsing bar), the settings file is
  read in the board job and written by a background saver (atomic), heatmaps render in a background thread and
  are swapped in (`PrerenderedCanvas`), net combo boxes fill on drop-down, other native lists inside
  Freeze/Thaw, and the GIL switch interval is 0.5 ms except during solver jobs. Stalls over 150 ms on
  kraft_flux_mini_2 with a full simulation: 35 (worst 1.08 s) → 5 (worst 277 ms, all during the 6-minute
  simulation, from CPU contention). Details, numbers and the dead end (GUI thread priority) in ARCHITECTURE
  "Plugin". The rule is in CLAUDE.md.
- **New feature: add a track's net to the Nets table from KiCad** (**`add-selected-net`**, stacked on
  `gui-responsive`, merged later). The user wanted a right-click menu entry; KiCad 10's plugin schema has no
  context-menu (or hotkey) field, so the user chose (AskUserQuestion): select a track in KiCad (click or
  right-click), press the new toolbar button **Add selected net to GroundTruth** (green plus icon); the net
  appears in the open GroundTruth window of that board, which comes to the front, or a window opens. The Nets
  tab has **Add KiCad selection** too (live mode). Driver and receivers come from `detect.suggest_net` (Detect's
  per-net logic, also for unclassified nets), unreviewed; GND, supplies and power-stage switch nodes are refused
  with the reason. Hand-over between the button's process and the window: `ui/inbox.py` (ARCHITECTURE
  "Plugin"). Tested: 185 passed, 1 skipped. The user has since used it in KiCad (the empty-selection and
  supply-net messages under `nets-multi-delete` came from that).
- **A crash found and fixed on that branch:** the GUI tests crashed every time (heap corruption 0xc0000374, or
  an access violation) in `test_cancel_stops_a_running_simulation`. Cause: the new inbox `wx.Timer` is owned by
  the window but was stopped only in `on_close`; tests destroy windows without closing them, and the timer then
  fired into the freed window. Now `MainFrame` (and `StartWindow`) stop their timers on `EVT_WINDOW_DESTROY`;
  `test_a_destroyed_window_stops_its_timers` guards it. The session-4 full run that ended with exit code 127
  (before this timer existed) was probably a crash too; its cause is unknown (no recurrence since).

- **Issue highlight in KiCad** (branch **`issue-highlight`**, on `add-selected-net`; the user asked: "when
  clicking on an issue inside the ground truth plugin, highlight the affected trace/area on the PCB", then
  "once done commit and merge everything"). Clicking an issue selects its copper in KiCad, highlights its net
  and zooms to it, planes selected after the zoom, the plane layer made active; no design change (README
  "Showing an issue on the board", ARCHITECTURE "Showing an issue in KiCad"). New: `analysis/highlight.py`,
  `kipy_sink.show_items`/`clear_highlight`, `Session.show_issue` (KiCad lock with 0.5 s timeout, `net_lit`,
  `saved_layer`), IssuesPanel options (Zoom to it, Highlight its net, Show in KiCad). Every `Session` KiCad
  call now holds `Session.kicad_lock` (`_kicad_call`). The old click path (`Session.select`) is gone.
  - Research (workflow, 3 readers) found: KiCad 10.0.5 has no view/highlight API; RunAction names verified in
    its source (README/ARCHITECTURE list them and the ones to avoid). `Session.select`'s `wait=` was NOT a bug
    (`_when_ready` consumes it; now documented there).
  - An adversarial review workflow (4 reviewers + skeptics, run wf_2b139e24-2b6, journal under
    `~/.claude/projects/c--Users-iwers-OneDrive-Dokumente-PCB-EMI/<session>/subagents/workflows/`) was still
    running at the handover; its first findings were all fixed: only the issue's own nets are lit (a GND stitching
    via or return pin no longer lights up all ground), no highlight when KiCad found none of that copper (it
    would take the net under the mouse), the user's own highlight is not cleared, the active layer is given
    back, clicks during a KiCad job are refused instead of queued, an empty issue clears the previous one,
    closing during a show still cleans up, detours point at the first non-capacitance transfer, deterministic
    rapid-click test, error-path and close tests, stricter live_kicad.py write.
  - The review's remaining confirmed findings were fixed after the account switch (all in `analysis/highlight.py`,
    each with a test): a route's highlight uses the nets `find_route` passes through, not `signal_nets` (a 100 R
    pull-up used to pull the whole +3V3 rail into the zoom and the highlight); a current-density hotspot inside a
    pour picks only the pour's own copper (not a gate-drive via beside it) and anchors on the nearest such copper
    at any distance, so the view never fits a whole plane; `reference_net` shows every segment over the other
    net's fill (the issue's length is the sum of all pieces, so the old window around the longest piece missed
    pieces and took segments over GND). The one verdict judged not real: the active-layer complaint.
  - The GUI failure was `test_plugin_start_never_blocks_its_window` (0.74 s stall, only in the full run, passes
    alone): a full garbage collection of the suite's ~2.6 GB of leftovers inside the timed window. The test now
    collects before timing; a plugin process has no such heap.
  - **State:** full suite alone: 207 passed, 1 skipped. Merged into `main` (fast-forward) on 2026-10-05 as the user
    asked ("once done commit and merge everything"); `gui-responsive`, `add-selected-net` and `issue-highlight`
    deleted. Plugin reinstalled from `main` afterwards; the user restarted KiCad and tried it.
  - **After the merge (branch `nets-multi-delete`, merged into `main` the same day):** the user asked to select
    one or more nets and delete them at once: the Nets list is multi-select; Remove selected, the Delete key and
    Mark reviewed act on all selected rows, Ctrl+A selects all, Update selected needs one row. The user saw "Nothing
    with a net is selected" after clicking a pour: KiCad's API really reported an empty selection (the pour was only
    hovered or highlighted, not selected; it worked once its corner handles showed). The dialog and plugin.log now
    say what KiCad reported (empty, or item types without a net). A refused supply net now says where its current is
    analysed (its power stage on the Converters tab, matched by rail or connector pins). Full suite: 208 passed,
    1 skipped.
  - **`nets-highlight`** (merged into `main` at eb7db06 and deleted, at the user's request; the user, looking at the plugin: "when clicking on a Net it should highlight the
    ones in the pcb", "same for the Converters tab", "under the converters tab allow users to select one or more
    nets to remove them at once", and about the status bar: "the loading bar cuts off the text, add multiline
    text?"): clicking rows of the Nets or Converters tab shows them in KiCad (`nets_highlight`,
    `stages_highlight`, `MainFrame._show_rows`, same background thread and lock as issues; a zoom checkbox under
    each table); the Converters table is multi-select like the Nets table (shared `ui/listsel.py`); Mark
    reviewed keeps its rows selected; long status messages wrap onto up to 3 lines (ARCHITECTURE "Status bar").
    Full suite 213 passed, 1 skipped; `gui_probe.py` on kraft_flux_mini_2 (no simulate): 0 stalls over 150 ms.
    The user tried it in KiCad (it works).
  - **`power-path-nets`** (merged into `main` and deleted at the user's request; the user: "this net is part of the converter but gets added to the Nets
    table", about `Net-(U302-Vs+)`, then a screenshot with "the complete flow path" of SW1 selected):
    `power.stage_power_nets` lists every net of a stage's power flow with its role; detection skips them and Add
    selected net refuses them; clicking a stage on the Converters tab now lights its whole flow path (not only
    the switch node; never ground). Bug found on the way and fixed: `switch_inductor` took +5V as U503's switch
    node (V_OUT pin on L503's far side, sorted first); now the SW pin's net. This changes U503's power-stage
    model (its switch node). Re-run (`power_check.py BOARD out/sel/k.groundtruth.yaml --from-settings --only
    U503`, real operating point): hot loop 1.62 nH, DC switch node → L503 0.14 A, 5 A/mm²; radiated margin
    +72.5 dB; conducted still not estimated (J101 reaches +12V only through the LM5008 stage). The user's own settings file still lists
    `Net-(U302-Vs+)` / `Net-(U304-Vs+)` from the earlier auto-detection: they can remove those rows (multi-select).
    `test_plugin_start_never_blocks_its_window` failed again once in a full run (0.55 s): it now also
    `gc.freeze()`s the suite's objects during the timed start.
  - **`surge-stopper-fet`** (merged into `main` and deleted at the user's request, with the installer fix; the user, with a screenshot of J101 and Q601: "the fet from the voltage spike
    suppressor isolates the connector J101. so ideally, the input connector is J101.2, J101.1", unsure it could be
    detected): `detect.pass_switches` finds series pass FETs (ARCHITECTURE "Series pass FETs"); detection now
    gives SW1, SW2 and the LM5008 stage the input connector J101.2 / J101.1 through Q601, and suggests a `short`
    model for Q601 (written only over 'open'). Checked on kraft_flux_mini_2 with a fresh auto-detected settings
    file (`out/sel/fresh.groundtruth.yaml`): SW1's conducted estimate now runs (71.0 dBµV @ 0.5 MHz at the
    default 500 kHz / 1 A; before: not estimated). The user's own settings file next to the board was made before
    this: re-detecting does not touch stages already listed, so tell them to set Input connector J101.2, J101.1
    on SW1/SW2 and Q601's model (Parts tab: resistor 6.5m, the schematic note) by hand, or remove the stages and
    Detect… again.
  - **`api-pin-names`** (merged into `main` and deleted at the user's request; the user confirmed it live: V_BATT
    is lit; the user removed the stages, ran Detect… in the plugin, and SW1 still had no
    input connector, so V_BATT was not lit). Cause, found by reading the live board read-only: KiCad 10.0.5's API
    sends symbol pin names with the pin number appended (Q601: `G_1`, `S_2`, `D_3`); the file reader strips
    `_<n>`, the API reader did not, so the gate rule (and any other pin-name rule anchored at the end) failed
    only in the plugin. `kipy_source.pin_name` strips it now. Live re-check: Q601 recognised, SW1/SW2/LM5008 get
    J101.2 / J101.1, SW1's power flow starts at V_BATT. Lesson: a detection that works from the file can still
    fail live; check `read_board(kb)` on the live board when the plugin disagrees with the CLI.
  - **`parts-follow-selection`** (merged into `main` and deleted at the user's request; the user: "when clicking on
    a part, it should be selected in the parts list so users can edit properties quickly"): while the Parts tab
    shows, the window polls KiCad's selection every 500 ms in its own thread and selects those parts' rows
    (ARCHITECTURE "Parts tab follows KiCad's selection"). **Not yet tried in the user's KiCad**; `gui_probe.py`
    does not cover it (file mode).
  - **`heatmap-enlarge`** (merged into `main` and deleted; the user: "the heatmaps are useless in its current state. One needs to be able
    to enlarge the image"): `map_grid` layout, shared zoom across layers, "Open in window". Two bugs the user hit
    meanwhile, fixed on the same branch: (1) Draw contours / Draw arrows on kraft_flux_mini_2 failed with "KiCad
    drew N item(s) but did not group them": the board does not enable User.7 (only User.1-4); refused up
    front at first, enabled automatically since `m2-thermal` (`_drawing_layer`, the user asked for that). The user removed the drawings with Undo / a KiCad restart (the live board had no
    groups or User-layer shapes afterwards). (2) Their SW1 had `c_oss: 482F` (typed 482), so the ringing was 0 Hz
    and the margins +134 / +238 dB were meaningless: `settings.implausible` refuses such values before a run.
    The user must correct it to 482p (their file, not ours to edit).
    (3) `test_plugin_start_never_blocks_its_window` failed in full runs (0.53 s): the main window's ~0.3 s build
    was one block on the GUI thread. The tabs are now built one per event-loop step in a hidden holder
    (ARCHITECTURE "Tabs built in steps"): constructor ~20 ms, largest step ~130 ms; gui_probe on
    kraft_flux_mini_2: 0 stalls over 150 ms, every tab switch included. (4) Found on the way: EVT_WINDOW_DESTROY
    bubbles up from child windows, so closing a child frame stopped the main window's timers; `_stop_timers` now
    checks the window id. Full suite: 223 passed, 1 skipped. Merged into `main` at the user's request.
    **Open:** the user asked for "a much higher res heatmap" in the single-layer view (each pixel is a solver cell,
    0.26 mm for SW1's hot loop at the default 100 000-node budget). Measured on SW1 (real operating point,
    `power_check.py --budget`): 100 000 nodes = 0.26 mm, 46 s, 3.43 nH, radiated margin -19.5 dB; 400 000 nodes =
    0.14 mm, 206 s, 3.55 nH, -19.2 dB. The numbers are converged at the default; a finer grid only sharpens the
    picture. The user chose (AskUserQuestion) a per-view **Sharper map** button over raising the budget or
    smoothing the display.
  - **`sharper-map`** (merged into `main` and deleted at the user's request): the Heatmap tab's "Sharper map" (`analysis/sharper.py`, ARCHITECTURE "Sharper
    map"). On kraft_flux_mini_2: SW1's hot loop 0.26 -> 0.14 mm in 53 s, a DC path in 19 s; layer shares within
    0.01. Validation (`tests/test_sharper.py`): the return current across a cut under a microstrip is ~1 A on both
    grids and agrees within 10 %. **Not yet tried in the user's KiCad.**
  - **`thermal-plan`** (merged into `main` and deleted): the user asked to look at their "PCB Thermal"
    app (`C:\Users\iwers\Downloads\Thermal solver\Thermal solver`, package `pcbsim`, PySide6) and bring its
    features and styling into the plugin; they chose (AskUserQuestion) "Plan first". The plan is
    [THERMAL_PLAN.md](THERMAL_PLAN.md): M1 dark theme (wx 3.3.3 native dark mode in both environments) + KPI
    tiles + hover readout + via loading vs IPC-2221; M2 thermal solve of the power stages, GroundTruth's DC path
    currents (vector-summed) driving PCB Thermal's thermal model through a Board adapter; M3 optional extras.
    Open decisions in the plan: theme mode, ambient/limits and mounting, dependency size (numba).
  - **`m1-theme`** (merged into `main` and deleted; the user: "Do M1 and M2 and once done present M3 steps again"; theme: "follow the
    Windows system setting and let user switch"; "make the buttons etc look a bit nicer, give it more volume"):
    `ui/theme.py` (palettes, `init_app` before the first window, Theme choice in the new top bar, drawn `Button`
    / `Card` / `Tile`), `ui/kpi.py` tiles on the Heatmap tab, hover readout (`plots.map_value`), via loading vs
    IPC-2221 (`analysis/ipc.py`, `via_overload`, `analysis.via_plating_um`, `rules.via_load_*`).
    `tools/dev/window_shot.py` screenshots the window (PrintWindow); `gui_probe.py --theme`. Screenshots of the
    demo buck (dark/light Heatmap, dark Converters) were sent to the user. Full suite 233 passed, 1 skipped.
    **Known, not M1:** the Issues tab's first paint stalls ~200 ms in about half the runs, on `main` too (probe
    on kraft_flux_mini_2: native code, the list of 14 long messages).
    The user asked whether the GPU could speed things up: this laptop has an Intel UHD 620 (no CUDA, weak
    double precision, shared memory bus) and a 4-core i7-8550U; the time goes into SuperLU factorisations and
    GMRES, memory-bound and irregular. Answer given: no; better levers are profiling and a multithreaded sparse
    solver (MKL PARDISO), offered after M2.
  - **`m2-thermal`** (from `m1-theme`; merged into `main` and deleted): the thermal solve, docs/THERMAL_PLAN.md option A. PCB Thermal's
    solver copied to `src/groundtruth/thermal/` (its 32 tests in tests/thermal/, the Qt runner test left out),
    `thermal/adapter.py`, `analysis/thermal.py` (`copper_heat`, `part_losses`, `solve_thermal`, `run_thermal`,
    `ThermalResult`), settings `thermal:` and per-stage `rds_on`, `rds_on_low`, `dcr`, `diode_vf` (Converters form
    too), Thermal button, `simulate --thermal`, isotherms via Draw contours, `[thermal]` extra (numba 0.68 /
    llvmlite, pyamg, psutil: they install next to numpy 2.4.2 / 2.5.3 without a downgrade) and the plugin
    requirements. ARCHITECTURE "Thermal solve", README "Temperatures".
    Validation: strip 50 × 4 mm at 5 A: heat 0.1539 W = I²R; peak rise 9.09 K vs 8.94 K in PCB Thermal's own
    electrical model; the adapter gives PCB Thermal's own parse exactly (areas, stack, 90.29 °C both).
    kraft_flux_mini_2 SW1+SW2 with ASSUMED losses (R_DS(on) 4 mΩ, DCR 5 mΩ, real operating point otherwise,
    `tools/dev/thermal_check.py`): draft 143.1 °C (22 s), normal 147.5 °C (103 s, 1.1 M unknowns, ±2 K), hot spot
    Q301 (2.33 W: 0.82 W switching, 1.30 W C_oss·V²·f_sw, 0.22 W conduction); 6.66 W in = out. The user must
    give the real R_DS(on) / DCR (and θJB) for real numbers.
    Also on this branch: drawing layers are enabled automatically (the user: "when not enabled, enable
    automatically instead of throwing an error"; `kipy_sink._drawing_layer`, set_enabled_layers keeps every
    enabled layer, checked afterwards) and `gui_probe.py --thermal`.
    Found by `gui_probe.py --simulate --thermal` on kraft_flux_mini_2: (1) the full Simulate ran out of memory
    (~2 GB per model in parallel, ~5 GB free with Chrome/VS Code open): `run_parallel` now retries such solves
    alone; (2) Thermal in a thread stalled the window 528 ms (pyamg holds the GIL): it now runs in a child
    process (`analysis/thermal_proc.py`). Remaining stalls are the known Simulate/heatmap-render ones (GIL held
    by native code in the solver or matplotlib's Agg, 150-330 ms).
    Final probe (`gui_probe.py --simulate --thermal`, kraft_flux_mini_2): Thermal 0 ms stall, Simulate completes
    (5 stalls 166-299 ms, all the known kind). Full suite on the final state: 272 passed, 1 skipped (7 min 16 s);
    `src/groundtruth` compiles under KiCad's Python 3.11.5.
    **Merged** (fast-forward, `main` at 3c30bb2; the three branches deleted) and **the plugin reinstalled from
    `main`** on 2026-10-05. KiCad must be restarted so it rebuilds the plugin's venv with the new requirements
    (numba, pyamg, psutil); not yet confirmed by the user.

### Next steps (session 6) - in order
1. Confirm with the user that KiCad rebuilt the plugin's venv after the restart: the Thermal button runs (without
   pyamg it fails with a message naming the missing package). The venv lives under
   `%LOCALAPPDATA%\KiCad\10.0\python-environments`; `%LOCALAPPDATA%\GroundTruth\plugin.log` shows the start-up.
2. M3 was presented to the user at the end of session 5: ask which items to do (list in step 6).
3. Get the user's verdict on the M1 look (screenshots sent: dark/light Heatmap, dark Converters; Claude could not
   view images late in session 5, the image viewer rejected every file).
4. Tell the user (still true, as far as known): their settings file next to kraft_flux_mini_2 has `c_oss: 482F`
   for SW1 (should be 482p, now refused before a run); Q601 should be a resistor 6.5m; give rds_on / dcr (and
   theta_jb) per stage for real thermal numbers (session 5's 143-147 °C used assumed 4 mΩ / 5 mΩ). It may still
   list `Net-(U302-Vs+)` / `Net-(U304-Vs+)` as signal nets (from before `power-path-nets`): those rows can go.
   The file is the user's: never edit it yourself. (Session 6 update, 2026-10-06: c_oss is now 482pF, rds_on 3m,
   dcr 6.3m, q_oss 38nC, no nets listed; the user asked Claude to bring it up to the current schema, which added
   missing keys only, see "GPU option". Only edit it again when they ask.)
5. Live checks in their KiCad (the user clicks, Claude reads `plugin.log` / the live board read-only after
   `tools/dev/live_kicad.py owner`): Parts tab follows the KiCad selection, Sharper map, Thermal button, drawn
   buttons, Theme switch, auto-enabled drawing layer (User.7 on kraft_flux_mini_2: enabling it is a board-setup
   change; tell the user first). Not confirmed yet from earlier: Clear EMI layers clears, and the window closes
   when KiCad closes.
6. M3 (THERMAL_PLAN.md "M3"): presented at the end of session 5, the user has not chosen yet: current-capacity
   solve, A/B difference maps, sweeps, free DC path (option B), component bodies / heatsinks, enclosure and
   mounting settings, memory watchdog for the thermal process, sharper thermal maps.
7. Older open items: the ~12 s plugin start under KiCad (session 4; procedure below), the Issues tab's ~200 ms
   first paint (on main too), solver speed (the user asked about the GPU: no; offered profiling and MKL PARDISO),
   and the physics questions in "Open with the user" below.

**Measuring the ~12 s plugin start (step 7):** with the user, on kraft_flux_mini_2 in their KiCad: run
`tools/dev/live_kicad.py owner`, then `freeze --seconds 120` in the background and ask them to click the button
once. Read `%LOCALAPPDATA%\GroundTruth\plugin.log`: the gap between "process created" and "script started" is
Python's start under KiCad, later gaps are ours; then decide the fix. The plugin saves its settings into
`kraft_flux_mini_2.groundtruth.yaml` next to the real board (the user's file, made by the plugin).

**Open with the user:** the LTC7890's spread modulation (measure, or ask ADI); the
switch-node overshoot; whether to model the input filter (what-if series part); MIL-STD-461 edition and
platform for RE102; the verdict question below (SW_CLK vias vs SPI margins).

## In progress when the account switched (session 3)

All work is committed and merged into `main` (this stretch: 1022aa8 … 890ee72, see `git log`; the working tree was
clean). Tests passed (148 + 1 skipped) at 0fe14e4; since then only `analysis/suggest.py` changed (edab3e4, its
tests in `tests/test_whatif_suggest.py` pass): run the full suite once first.

**Next actions, in order** (details in items 1–2 below):
1. **Done at the end of session 3:** the fix search re-run on the real board with edab3e4 (589 s, 32 s more than
   without the gap solves). The check now sees the gaps: its baseline is SCK +13.4 dB @ 740 MHz (reported
   +15.6) and MOSI +13.9 dB @ 740 MHz (reported +14.9; the true minimum is +12.8 dB @ 780, one midpoint per gap
   does not reach it). Selected as before: S1, S2 (SW_CLK vias) and S3 (MOSI via), now with notes:
   S1 "also lowers SPI_SCK's worst margin +13.4 → +10.6 dB (at 740 MHz)"; S2 the same to +10.2 dB, and
   "SPI_MOSI +13.9 → +10.3 dB (at 1140 MHz)"; S3 "worst margin +13.9 → +14.9 dB". Verification (sampled
   differently, see remedy 2): SCK +15.6 → +15.5, MOSI +14.9 → +11.0, SW_CLK +34.0 → +41.0 dB, no new issue.
   **Design question for the user (ask before changing):** `harmonic_verdict` weighs dB against dB. So the SW_CLK
   vias' gain (4.6–5.9 dB, on a net 34 dB below the limit) outweighs ≈ 3 dB lost on SPI nets ≈ 13 dB below
   it. Those vias also fix two `layer_change_stitch` errors (CI fails on them). Options: count a gain only
   when its route is within 20 dB of the warning level; or judge by the board's minimum margin. Either one
   would drop the SW_CLK vias and leave the errors.
   To repeat the runs (Git Bash, from the repo):

   ```sh
   B=C:/Users/iwers/OneDrive/Dokumente/github/kraft_flux_mini_2/kraft_flux_mini_2.kicad_pcb
   S=<a scratch folder, never next to the board>
   .venv/Scripts/groundtruth init $B --settings $S/real.groundtruth.yaml
   # then replace the `nets:` list in $S/real.groundtruth.yaml with exactly these three entries:
   #   - {name: /CPU/SPI_SCK, driver: U1102.12, receivers: [U1103.6], freq: 20MHz, rise_time: 2ns, amplitude_v: 3.3}
   #   - {name: /CPU/SPI_MOSI, driver: U1102.14, receivers: [U1103.5], freq: 20MHz, rise_time: 2ns, amplitude_v: 3.3}
   #   - {name: /CPU/SW_CLK, driver: J1101.4, receivers: [U1102.28], freq: 10MHz, rise_time: 3ns, amplitude_v: 3.3}
   MSYS_NO_PATHCONV=1 .venv/Scripts/groundtruth suggest $B --settings $S/real.groundtruth.yaml --format json --out-dir $S/sug -v
   # ≈ 10 min; then, if needed (≈ 5 min each, read-only):
   MSYS_NO_PATHCONV=1 .venv/Scripts/python tools/dev/gap_probe.py $B $S/real.groundtruth.yaml $S/sug/groundtruth-suggestions.json /CPU/SPI_MOSI
   MSYS_NO_PATHCONV=1 .venv/Scripts/python tools/dev/fix_combo_probe.py $B $S/real.groundtruth.yaml $S/sug/groundtruth-suggestions.json /CPU/SPI_MOSI
   ```
   (Keep the settings file free of `converters:` for this run: the session-3 numbers are for the three nets only.)
2. **Done (session 4):** full test suite at 8e2e0bb: 148 passed, 1 skipped.
3. **Done (session 4):** `compare_results` compares worst margins over the same harmonics (`aligned_worst`,
   ARCHITECTURE "Selection"). Not yet re-run on the real board: the SCK/MOSI verification rows above were
   sampled differently (the check said SCK −3 dB at 740 MHz, the verification +15.6 → +15.5).
4. **Done (session 4):** DC output path and ground return on SW1/SW2, rows under "Power stages: final run". SW2's
   ground return: a narrow B.Cu GND pour neck near (125, 52.6), ≈ 5.2–5.7 mΩ, > 170 A/mm² (checked on 3 grids).
5. Then ask the user the design question in action 1, and the open items further below (1–3 and 6 need them).

Details:

1. **Item 7 on the real board: predictions vs verification.** `groundtruth suggest` on kraft_flux_mini_2 (settings file
   from `groundtruth init` + the 3 SPI/SWD nets of the reference table below, defaults) took 557 s (session 2: 691 s
   without the new check; the check itself was 36 board-wide solves). Selected: S1 + S2 (SW_CLK stitching vias,
   predicted −5.9 / −4.6 dB at 1050 MHz) and S3 (SPI_MOSI stitching via, −4.3 dB at 820 MHz). S2 was kept with
   the note "also lowers /CPU/SPI_SCK's worst margin +15.6 → +13.5 dB (at 820 MHz)" (S2 gains 4.6 dB, SCK stays
   above the 6 dB warning level). **Verified together:** SCK +15.6 → +15.5 dB, SW_CLK +34.0 → +41.0 dB, but
   **SPI_MOSI +15.0 dB @ 820 MHz → +11.0 dB @ 740 MHz** (resonance +5.8 → +9.2 dB), while each fix alone was
   predicted to change MOSI's worst margin by −0.3 to +0.5 dB. No new issue (all margins stay above 6 dB).
   Two explanations, to tell apart with `tools/dev/fix_combo_probe.py BOARD SETTINGS suggestions.json
   /CPU/SPI_MOSI` (≈ 5 min; it predicts each selected fix alone and all together on the baseline models):
   - **combination** (all together predicts ≈ +11 dB at 740 MHz): add a check of the selected set together
     (`_WorstCheck` with all fixes' branches: the probe shows how) and, when it loses more than 1 dB on a
     route, drop the fix whose removal helps most, or at least note it;
   - **model difference** (all together still predicts ≈ +15 dB): the low-rank update on the baseline grid
     misses what the rebuilt models (via as copper, antipads) do to the In1/In2 cavity. Then document the limit
     (the verification run is the arbiter) and consider re-verifying after dropping a fix that the verification
     shows harming a route.
   **Probe result (session 3, end): neither; it is sampling.** All three fixes together predict MOSI
   +15.0 → +16.2 dB (better) at every frequency the baseline solved, but the baseline never solved 740 MHz
   (MOSI's solves: 20, 60, 140, 260, 660, 820, 900, 1020, 1260, 1580 MHz). The verification run refined into
   the 660–820 MHz gap and found the peak there. Predicted per frequency (baseline → all fixes): 660 19.5 → 19.7,
   820 15.0 → 16.5, 900 18.1 → 21.5, 1260 17.7 → 25.8 dB. Next step: solve the baseline at 740 MHz:
   `tools/dev/gap_probe.py BOARD SETTINGS suggestions.json /CPU/SPI_MOSI` (≈ 5 min; baseline margins as solved
   and with 700/740/780 MHz added, then all fixes predicted there; the suggestions JSON comes from
   `groundtruth suggest … --format json --out-dir X`).
   **Gap probe result (done):** with 700/740/780 MHz solved, the baseline reads +13.6/+13.9/+12.8 dB there, so
   MOSI's true worst is ≈ +12.8 dB @ 780 MHz, not the reported +14.9 dB @ 820 MHz. All three fixes predicted
   there: **+10.9 dB @ 740 MHz; verified: +11.0 dB @ 740 MHz.** So both happened: the baseline's worst was
   optimistic (≈ 2 dB), and the fixes really cost MOSI ≈ 1.9 dB (12.8 → 10.9, still above the 6 dB warning).
   The low-rank prediction is accurate (0.1 dB) wherever it is evaluated; what is missing is **sampling**: the
   check and `compare_results` only see the frequencies each run happened to solve (`max_solves` = 10 per
   route), and the baseline left the resonance region unresolved. **Remedy:**
   - **done (edab3e4):** `gap_midpoints` + `_WorstCheck(extra=…)`: the log-midpoint harmonics of
     the gaps (> 15 %) next to near-limit solved harmonics are solved on the baseline once per route, then with
     each fix (test board: +17.1 → +28.2 predicted, +27.1 verified). Re-run on the real board: see "Next actions" 1 (≈ 10 min:
     the `groundtruth suggest` command in item 1 above); expected: MOSI's loss of ≈ 1.9 dB appears in the notes. Not
     done: adding those solves to the reported baseline (the route results stay as simulated);
   - in the verification, solve the after-run's worst frequency on the baseline (and the baseline's on the
     after-run) before `compare_results`, so before/after compare the same frequencies (here −1.9 dB, not the
     reported −4.0 dB);
   - alternatively or additionally raise the default `max_solves` for routes with plane capacitance.
   Also seen: S2 alone gave no prediction for MOSI in the combination probe (427 s, a solve failed: its via
   probably joins copper islands, so `with_branches` rebuilds the stack without the low-rank update), while
   the suggest run did predict it; worth a look.
2. **DC output path on the real board** (not run yet). Detection now finds output connectors J104.2/J104.1
   (SW1, through the current-sense shunt into `/DCDC Stage/DC_OUT_RAIL_1`) and J106.2/J106.1 (SW2); none for the
   diode buck (+12V) and U503 (+5V), which feed on-board loads. Run:
   `tools/dev/power_check.py $B $S/real.groundtruth.yaml --only SW1,SW2 --f-sw 500k --i-out 5 --v-in 24 --c-oss 600p
   --series Q601 --connector J101.2,J101.1` (detection supplies the output connectors; `--output` overrides) and
   add the output and ground-return rows to the power-stage reference table. The R now shows the copper and the
   series parts apart (e.g. the shunt, and Q601's 5 mΩ test model in the input path's 8.7 mΩ).

Tests: 148 (1 skipped: live KiCad), about 4 minutes. `test_gui.py::test_jobs_run_off_the_gui_thread` failed in
about 3 of 10 full runs (never in isolation): a 0.3 s GUI timer gap under CPU load from a worker holding the GIL in
a C call. Its limit is now 0.5 s, with messages on every assertion. If it still fails, read the message first.

## Session 3: what the user asked and what was done

1. "the gui hangs while computing. make sure that calculations are not done in the main gui thread."
   - Every slow step runs as a job in a worker thread (`MainFrame._start`), with Cancel.
   - Measured: the GUI thread never pauses more than 46 ms (`tests/test_gui.py`).
   - Committed in a2cbe5c (m5), together with the live KiCad test (see item 6 of the open list).
2. "can we improve runtime of a simulation by dividing work into multiple threads … Also, I am also interested in
   DCDC converter designs which frequency is around 10 kHz to 1 MHz." The user approved both steps and added:
   "make sure … copper pours are not neglected, This is very important. All DCDC designs will have pours as the
   current in that area is very high and current density/resistance would become a problem on a normal trace."
   - **Step 1** (m5, 87b802f):
     - a thread pool (`analysis.threads`, auto = physical cores, at most 4) that gives results independent of
       the thread count;
     - `f_ref = max(f0, 1 MHz)` for loop L, the ideal reference and the board-wide offset;
     - the CISPR 16 receiver bandwidth;
     - routes through pours solved as copper, with return statistics that exclude the route's own copper.
   - **Step 2** (m6): `analysis/power.py`, `emi/conducted.py`, `detect_converters`, `ui/converters_panel.py`,
     reports and CLI. The design and its validation are in ARCHITECTURE "Power stages".
   - The user named no converter types or standard. The model therefore covers sync bucks with discrete FETs,
     diode bucks, integrated converters and the boost hot loop, with selectable CISPR 32, CISPR 25 and
     MIL-STD-461 limits. The user should confirm which standard matters to them (ask).
3. "make sure to update the md files so I can hand this off to another claude account": done in 084b7d7, and kept
   current with every later commit.
4. "keep going until you run into your 5-hr limit!": the work after the first handoff commit, all on
   m6-power-loops, each commit with tests and these docs:

   | Commit | What |
   |---|---|
   | ac96dde | The input path's series parts join the model region (bug found by the conducted demo) |
   | 7b7934b | DC power paths (R, drop, loss, A/mm², via currents, `current_density`); each layer's copper thickness from the stackup |
   | c9fd990 | Stitching-via fix suggestions for power stages; model region widens when the loop closes far away |
   | 059e17c | A far input path gets its own model (the hot loop keeps its fine grid) |
   | 3000f38 | Greedy selection of power-stage vias; DC heatmap view tested in the plugin |
   | f5fa84c | Common-mode budget (`cm_estimate`, `c_stray`); `groundtruth info` counts converters |
   | 664504f | 100 nF capacitor candidates right at the switches; conducted refinement on curvature (fewer solves) |
   | b2aad53 | Performance: DC paths solved in the parallel phase, Gauss-Legendre nodes cached (model build 20 → 6 s), no redundant radiated solves on the inductive converter model (SW1 with the conducted path 85.6 → 31.0 s, L unchanged); a hot loop that can't close names the separate copper pieces |
   | 1f690ca | Input-filter guidance from the conducted deficit |
   | c6cddd8 | Converters tab: C stray field |
   | ea31890 | Converter solve rows labelled by model; final power-stage run on the current board |
   | 4695a9a | Distinct names for copies of a stage; compact loop-closure messages (third real board) |
   | 4fce867 | Boost conducted emission from the inductor ripple; GUI timing test made robust |
   | 1022aa8 | Item 7: each selected fix checked at every harmonic near the limit on every route (`_WorstCheck`, `harmonic_verdict`; replaces the single-harmonic cross-route check) |
   | 0fe14e4 | DC output path and ground return (`output_connector`, `_output_runs`, detection, Converters tab field); a DC path's R split into copper and series parts (`dc_resistance`); `tools/dev/fix_combo_probe.py` |

Small fixes on the way:
- The pour-section filaments now end exactly on the injection nodes. Before, they stopped at the pour's entry
  point up to a grid pitch away, so the loop was not closed and the moment depended on the origin.
- `transfers()` accepts `route=None`.
- `TraceSegment.i` is a per-filament current weight.
- `receiver_sum` / `receiver_bandwidth` take a bandwidth table.
- The report tables are envelope-thinned (`thin_rows`, `per_decade`).

## How to reproduce the power-stage results (kraft_flux_mini_2)

```sh
B=C:/Users/iwers/OneDrive/Dokumente/github/kraft_flux_mini_2/kraft_flux_mini_2.kicad_pcb
S=<a scratch folder, never next to the board>
.venv/Scripts/groundtruth init $B --settings $S/real.groundtruth.yaml
.venv/Scripts/groundtruth detect $B --settings $S/real.groundtruth.yaml            # lists 4 power stages + the signal nets
# hot loops with test parameters (no plane capacitance: ~40 s per stage; --cap adds the board-wide model)
.venv/Scripts/python tools/dev/power_check.py $B $S/real.groundtruth.yaml --only SW1 --f-sw 500k --i-out 5 --v-in 24 --c-oss 600p
# conducted estimate: Q601 (surge stopper) as 5 mOhm, LISN at the XT60
.venv/Scripts/python tools/dev/power_check.py $B $S/real.groundtruth.yaml --only SW1,D501 --f-sw 500k --i-out 5 --v-in 24 \
    --c-oss 600p --series Q601 --connector J101.2,J101.1
```

Use `MSYS_NO_PATHCONV=1` in Git Bash if a net name starting with "/" is passed on the command line.

## Reference results

### Signal nets (`kraft_flux_mini_2`, session 2 code; unchanged by session 3)
Settings file: SPI_SCK and SPI_MOSI at 20 MHz / 2 ns, SW_CLK at 10 MHz / 3 ns, all defaults (`plane_capacitance:
true`, `cavity_max_nodes` 40k → 0.71 mm, 31.7k nodes).

| Net | L vs ideal | Worst margin (class B) | Plane resonance | Board-wide bias at f0 |
|---|---|---|---|---|
| /CPU/SPI_SCK | 17.59 nH, 1.57× | +10.8 dB @ 780 MHz | +7.7 dB @ 780 MHz (In2–B.Cu) | −0.5 dB |
| /CPU/SPI_MOSI | 14.96 nH, 1.52× | +3.6 dB @ 740 MHz (warning) | +14.9 dB @ 740 MHz (In2–B.Cu) | −0.2 dB |
| /CPU/SW_CLK | 13.63 nH, 1.76× | +39.5 dB | +13.5 dB @ 1050 MHz (F.Cu–In1) | +5.6 dB |

- Runtime for the 3 nets with 4 threads: 189 s (302 s with 1 thread), on the board as the user modified it at
  15:57 on 2026-10-04. The results are identical.
- `suggest` for the 3 nets: 691 s (session 2). See ARCHITECTURE for what it selects.
- **On the board as saved at 18:35 (zones refilled), end of session 3.** The numbers changed because the board
  changed, not the code: the step-1 code (87b802f) and today's code (m6) give identical results on this board,
  digit for digit. Runtime is the same too: 145–150 s with 4 threads.

  | Net | L vs ideal | Worst margin | Plane resonance |
  |---|---|---|---|
  | /CPU/SPI_SCK | 19.20 nH, 1.71× | +15.6 dB @ 780 MHz | +4.2 dB @ 1020 MHz (F.Cu–In1) |
  | /CPU/SPI_MOSI | 15.76 nH, 1.64× | +15.0 dB @ 820 MHz | +5.8 dB @ 1020 MHz (F.Cu–In1) |
  | /CPU/SW_CLK | 13.64 nH, 1.76× | +34.0 dB @ 810 MHz | +11.9 dB @ 1050 MHz (F.Cu–In1) |

### Power stages: final run (board as saved at 18:35, final code of session 3)
All features on: split models, DC paths, common mode, filter guidance; plane capacitance off, Q601 as 5 mΩ, LISN at
J101; f_sw 500 kHz, 24 V, C_oss 600 pF on the GaN stages. SW1 and SW2 ran at a 5 A test current (65 s for both);
the LM5008 and LT3970 stages at 0.3 A (34 s for both). These are test operating points: ask for the real ones.

| Stage | Hot loop | Conducted (CISPR 32 B) | Radiated | DC switch node / input | Filter guidance |
|---|---|---|---|---|---|
| SW1 | 3.43 nH, rings 111 MHz (Q 15) | 85.0 dBµV @ 0.5 MHz (−39.0 dB) | −10.3 dB | 0.31 mΩ, 22 A/mm² / 8.7 mΩ, 53 A/mm² (warning, at Q601's +VDC pad) | f_c ≤ 37.5 kHz (3.8 µH + 4.7 µF) |
| SW2 | 2.83 nH, rings 122 MHz | 84.0 dBµV (−38.0 dB) | −10.9 dB | 0.22 mΩ, 21 A/mm² / 8.5 mΩ, 33 A/mm² | f_c ≤ 39.8 kHz |
| Diode buck | 3.47 nH | 49.6 dBµV (−3.6 dB) | +59 dB | 1.4 mΩ, 4 A/mm² / 19.7 mΩ, 4 A/mm² | f_c ≤ 288 kHz |
| U503 | 1.62 nH (1.78 before the +12V refill) | not modelled (no series path to J101) | +67 dB | 5.6 mΩ, 26 A/mm² | – |

**DC output path and ground return (session 4, same board and parameters, 62 s for SW1 + SW2).** Detection
supplies the output connectors. The hot loops and other paths are unchanged from the table above.

| Stage | DC output (5 A) | DC ground return (5 A) |
|---|---|---|
| SW1 | L301 → J104.2: 7.78 mΩ = copper 3.75 + shunts R309 2.36 + R310 1.67 (two 8 mΩ in parallel, 54/46 %); 66 A/mm² sustained on F.Cu (warning) | J104.1 → Q302 (2.5 A) + J101.1 (2.5 A): 1.03 mΩ; 52 A/mm² (warning); J104.1's THT pad carries 4.83 A |
| SW2 | L302 → J106.2: 7.62 mΩ = copper 3.59 + R329 2.36 + R328 1.67; 72 A/mm² (warning) | J106.1 → Q304 + J101.1: **4.15 mΩ, 122 A/mm² (error)** |

Grid check on SW2 (output model 0.24 mm → 0.18 mm): the output, input and switch-node paths agree within 3 %
(7.62 → 7.68, 8.52 → 8.76, 0.22 → 0.21 mΩ), **the ground return does not**: 4.15 → 4.84 mΩ, and its hotspot
moves from F.Cu (120.33, 74.71), a GND pour sliver between the pads of U304 (ZXCT1107), to B.Cu (125.60, 52.59),
open pour with no GND via within 3 mm. Thin zone copper resolves differently at the two pitches. Both grids put it
above 100 A/mm², so SW2's ground return is a real finding. **Third budget (0.14 mm, 240k nodes, 147 s):** 5.22 mΩ,
176 A/mm² at B.Cu (124.63, 52.59): the location is stable from 0.18 mm on, R converges (+0.69 then +0.38 mΩ,
heading for ≈ 5.5–5.7 mΩ), and the density keeps rising because the neck in the B.Cu GND pour there is narrower
than the grid (176 A/mm² is a lower bound). Show the user: SW2's ground return squeezes through a narrow B.Cu
pour neck near (125, 52.6) with no GND via within 3 mm, while SW1's return is 1.03 mΩ. With duty 0.5 (no `v_out`
given), half the return goes to the input connector; the real duty changes the split.

**Against MIL-STD-461 CE102 (the user's standard, session 4; same parameters, `power_check.py … --standard
mil461`, basic curve: 60 dBµV from 0.5 to 10 MHz).** SW1 84.0 dBµV @ 0.5 MHz, −24.0 dB (filter corner ≤ 89 kHz,
e.g. 3.2 µH + 1 µF, damped by 1.8 Ω + 4 µF); SW2 83.8 dBµV, −23.8 dB (≤ 90 kHz); diode buck 49.6 dBµV, +10.4 dB
(passes at 0.3 A); U503 not estimated (it is fed from +12V, the diode buck's output, not through series parts
from J101). Common-mode budget 1.2 pF per stage (CISPR 32: 0.24 pF). Radiated is still CISPR 32 class B.

The common-mode budget is 0.24 pF for every stage, and that is correct, not a bug. A 24 V, 500 kHz square wave
drives a flat 48 µA per pF at each odd harmonic below the edge corner. That reads 58.6 dBµV per pF on a LISN,
against a 46 dBµV limit at 0.5–5 MHz, so the edge rate doesn't matter below 30 MHz. Without a common-mode filter,
the switch nodes may couple at most 0.24 pF to the chassis.

### Power stages (`kraft_flux_mini_2`, earlier in session 3; history)
Test parameters for every stage: f_sw 500 kHz, 5 A, 24 V; C_oss 600 pF on the GaN stages; edges 2 ns (GaN, from
detection) and 10 ns (diode buck, U503). The real values are unknown: ask the user for f_sw, load currents,
input voltage and the FETs' C_oss. All four stages with the board-wide model took 332 s (run alongside the test
suite).

| Stage | Hot-loop L / R at 100 MHz | Overshoot | Who carries the edges (100 MHz) | Radiated worst |
|---|---|---|---|---|
| SW1 (Q301/Q302) | 3.42 nH / 59 mΩ | 8.5 V L·di/dt; rings at 111 MHz, Q 15, ≈ 19.9 V | C325 20 %, C318 16 %, C329 16 %, C327 15 %, C331 15 %, C332 12 % | 40.4 dBµV/m @ 111.5 MHz (−10.4 dB) |
| SW2 (Q303/Q304) | 2.83 nH / 42 mΩ | 7.1 V; rings at 122 MHz, Q 15, ≈ 19.6 V | C325 17 %, C329 16 %, C321 14 %, C318 13 % … | 41.1 dBµV/m @ 122.5 MHz (−11.1 dB) |
| Diode buck (U502/D501) | 3.47 nH / 70 mΩ | 1.7 V | C507 54 %, C504 47 % | +34.6 dB margin |
| U503 (LT3970) | 1.78 nH / 51 mΩ | 0.9 V | C502 37 %, C505 35 % | +41.4 dB margin |

**Layout finding to show the user (verify with them).** SW1's and SW2's hot
loops close through the XT60 GND pin J101.1 (94–95 % of the loop current passes that through-hole pad). The
input capacitors' GND pads (y ≈ 90–92 mm, x 120–165 mm) sit on a large F.Cu GND area with **no GND via at all**
in x 118–168 / y 86–104. That area reaches the inner GND planes only through J101.1 and 4 vias 12–18 mm away.
The low-side FETs' sources sit on a separate small F.Cu GND island with vias to the inner planes. The closest
capacitors (C324, C326, 10 µF, 8–10 mm from Q301) therefore carry only about 1 % each. GND vias at the input
capacitors' GND pads should cut the hot loop a lot. This is checked in the raw board data, not just in the
model (`board.vias` in that box: 46 +VDC vias, 0 GND).

**SW1 grid convergence (done, converged).**

| Budget | Pitch | L | Capacitor ranking |
|---|---|---|---|
| 50k nodes | 0.385 mm | 3.35 nH | C325 21–22 %, then C318, C329, C327, C331 at every budget |
| 100k nodes | 0.263 mm | 3.51 nH | same |
| 200k nodes | 0.198 mm | 3.45 nH (64 s) | same |

The spread is ±2.5 %. A small open question, inside that grid uncertainty: the same SW1 model had 88,768
nodes and gave 3.42 nH earlier in the session, but 87,059 nodes later. It is probably a net set changed by the
converter code in between. Compare `st.grids[k].nets` if it ever matters.

**Conducted demo (done) and the bug it found (fixed after the first handoff commit).** With Q601 modelled as
5 mΩ and the LISN at J101, the first run said "no loop current reaches the input connector" for SW1. Q601
(y = 110.6–118.5 mm) lay outside the model region, which spanned only the switches, the capacitors and the
connector pins. Q601 was therefore never a branch, and the input path was cut. The diode buck had the same
problem, hidden behind magnetic coupling into the cut-off V_BATT copper (+8 dB margin, wrong).

The fix: `path_parts` adds the input path's series parts to the region. The regression test is
`test_series_parts_on_the_input_path_are_in_the_model`; it fails without the fix.

Results after the fix (test parameters 500 kHz, 5 A, 24 V; CISPR 32 class B average; 178 s for the 3 stages):

| Stage | Conducted | Radiated | Hot loop |
|---|---|---|---|
| SW1 | 83.9 dBµV @ 0.5 MHz (−37.9 dB) | 38.4 dBµV/m @ 114.5 MHz (−8.4 dB) | 3.20 nH |
| SW2 | 83.8 dBµV @ 0.5 MHz (−37.8 dB) | 40.4 dBµV/m (−10.4 dB) | 2.68 nH |
| Diode buck | 73.8 dBµV @ 0.5 MHz (−27.8 dB) | +19.1 dB margin | 3.40 nH |

There is no input filter between V_BATT and +VDC (Q601 and capacitors only), so large conducted numbers are
expected for a pulsed 5 A. The real operating point decides.

**DC power paths on the real board** (same run, test current 5 A for every stage, duty 0.5, so 2.5 A input).
**5 A is far beyond the LM5008 (0.5 A) and LT3970 (0.35 A)**, so their flags are artefacts of the test current.
Rerun with the real currents.

| Stage | Switch node → inductor | Input J101.2 → high side |
|---|---|---|
| SW1 | 0.28 mΩ, 19 A/mm² sustained on In1; busiest via 0.60 A | 8.71 mΩ, 22 mV; 53 A/mm² sustained at (124.35, 110.99) next to Q601's single +VDC pad (warning) |
| SW2 | 0.21 mΩ, 20 A/mm² | 8.52 mΩ, 33 A/mm² |
| Diode buck | 1.44 mΩ, 67 A/mm² (warning at 5 A) | 19.7 mΩ, 69 A/mm²; via +VDC @(156.2, 95.3) 1.07 A |
| U503 | 5.59 mΩ, 441 A/mm² (error at 5 A; ≈ 31 A/mm² at its real 0.35 A) | not modelled (+12V doesn't reach J101 through series parts) |

The +12V zone was unfilled in the saved file during these runs, so U503's rail was tracks only. The user
refilled the zones and saved at 18:35 on 2026-10-04 (`groundtruth info` no longer warns). Results computed before
that, U503's especially, are on the old fill; rerun before quoting them. The board was also edited at 15:57.
Every reference number here is for the board as it was at that time.

**Fix suggestions on the real board** (`groundtruth suggest … --nets "buck SW1" "buck SW2"`, scratch settings file with the
detected converters and plane capacitance off, 245 s). They confirm the layout finding in numbers:

| Stage | Selected vias | Each predicted | Verified together | Radiated margin |
|---|---|---|---|---|
| SW1 | GND at C326.2, C322.2, C320.2 (the capacitors right of Q301/Q302) | −31 % L, −4 dB | 3.51 → 2.29 nH (−35 %) | −10.0 → −6.9 dB |
| SW2 | GND at C331.2, C318.2, C321.2 | −6 to −8 % | 2.88 → 2.35 nH (−18 %) | −10.7 → −7.4 dB |

+VDC stitching candidates show no gain: that side is well stitched. The three SW1 vias are nearly redundant
(one gives −31 %, three give −35 %). Since 3000f38 a greedy selection re-evaluates the rest with the chosen vias
in place and stops when the next one adds less than 1 %. Rerun with it (318 s): SW1 keeps 3 GND vias (C326.2
first, −31 %; then C322.2, −33 % in total; then C324.2, −35 %; verified 3.51 → 2.27 nH). SW2 keeps 2 (C331.2,
C318.2; verified 2.88 → 2.36 nH, same as 3 before).

**Split models (059e17c).** With the conducted path, one model reaching Q601 coarsened SW1's grid to 0.35 mm,
and L read 3.20 nH against the converged ≈ 3.45 nH. Now, when a model spanning the input connector and its
series parts would be coarser than the hot loop alone, the input path gets its own model (`_pitch`,
`_loop_model`). The hot loop keeps its fine grid; the input model gives the conducted transfer and the DC input
path. `test_a_far_input_path_gets_its_own_model_and_leaves_the_hot_loop_alone` checks that L is identical with
and without the connector. The real-board rerun of the conducted demo with split models is not done yet: expect
SW1 ≈ 3.45 nH.

**Performance of a power stage (SW1 with the conducted path, real board, 4 threads):** 85.6 s → 31.0 s.
- The conducted refinement splits only where log-log interpolation bends: 12 → 6 solves.
- The DC paths are model runs of their own, solved in the parallel phase instead of one after the other at the
  end.
- `kernel.leggauss` is cached. `segment_mutual` recomputed the Gauss-Legendre nodes (an eigenvalue problem) on
  each of about 35 000 calls (switch-package filaments × vias), which took the model build from 6 to 20 s.
- The inductive converter model no longer solves radiated harmonics beyond 30/100 MHz; its moment is flat there.

**Second real board (`ness_2_wifi`, 2-layer).** Detection finds the TPS560200 (U2, integrated, `PH` pin, L1 to +5V).
The simulation correctly refuses it: VDD has no copper at all (no tracks, vias or zones; that part of the board
is unrouted), and the message now names the separate pieces (`U2.1 (VDD) | U2.4 (VDD) | U2.2 (GND), C3, …`). L1's
Value is a Würth part number, so its suggested inductor model reads 7.8e10 H (harmless for the hot loop; fix the
value or the model in the settings file).

**Third real board (`kraftFlux-mini-4`, placed but unrouted).** Detection finds 7 stages: two copies of the
SW1/SW2 stages (RAIL A and RAIL A1), the LM5008 buck, an LM5164 (U501) and the LT3970. Copies now get distinct
names (`buck RAIL A/SW1`, `buck RAIL A1/SW1`). Every simulation fails cleanly because the board is unrouted:
compact messages name the separate copper pieces, or a pad no copper reaches.

## Open items and next steps (ask the user before starting)

**Needs the user:**
1. **MIL-STD-461 edition (F/G) and platform** (aircraft/ground/ship, service): they pick the RE102 radiated curve
   and whether CE101 applies. Since 2026-10-16 (`weak-spots`) RE102 is built in for every platform
   (`analysis.radiated_limit: re102_...`) and CE101 (`analysis.ce101: aircraft | ships`). Conducted uses
   CE102 (`analysis.conducted: mil461`; `conducted_class: 115v` etc. applies the standard's relaxation for those
   sources; a 28 V-class source such as kraft's 51.8 V keeps the basic curve).
2. **The LTC7890's spread-spectrum modulation** (shape and rate are not in the LTC7890/7891 datasheets): a
   spectrum-analyser look at SW1's switch node, or ADI. It decides between 1 and 9 dB of CE102 benefit.
3. **SW1's switch-node overshoot** (estimated ≈ 95 V peak on the 100 V EPC2306, see "Session 4"): ask for a
   measurement; the estimate rests on r_damp 0.1 Ω and 2 ns edges.
4. **The fix-search verdict question** (session 3, "Next actions" 1 of the old list below): SW_CLK vias that fix
   Tier 1 errors cost the SPI nets ≈ 3 dB.
5. **Show the layout findings** with the heatmaps (`out/sim_real/`, the plugin's Heatmap tab): SW1/SW2 hot loops
   closing through the XT60 GND pin, the current-density necks (SW1 output and input at Q601, SW2's B.Cu ground).

**Possible next features (not started):**
6. **Input filter as a what-if edit**: a series part (inductor/ferrite) cut into the input path plus capacitors,
   so the proposed π-filter can be checked in the copper before layout. Needs a new modification type that splits
   a net at a point and inserts a two-pad part.
7. Done 2026-10-16 (`weak-spots`): a user RE102 curve; CE101 (aircraft and ships DC).
8. Done 2026-10-16 (`weak-spots`): spread spectrum in the radiated estimate. Spread fields in the plugin's
   Converters tab: still open.
9. **Interleaved stages**: SW1 and SW2 of one LTC7890 run 180° apart; their input currents partly cancel. The
   conducted estimate treats each stage alone.
10. Not done from earlier lists: the load's ground return for on-board loads (+12V/+5V have no connector);
    trying the next candidate of an issue whose fix was dropped; adding the check's gap solves to the reported
    baseline; computing the switch-node capacitance to the chassis (mechanical).
11. **Performance ideas:** fold the plane-capacitance unknowns into the node admittance (≈ 20 % shorter GMRES
    vectors); a PARDISO backend. Pruned FFTs were measured (only 5 %) and dropped.

**Done (kept for reference):** merging (everything is on `main`); real operating points (session 4); fix
prediction across harmonics (1022aa8, edab3e4, 228bc4c); DC output path and ground return; power-stage fix
suggestions; DC current density; common-mode budget; boost conducted emission; input-filter guidance; the live
plugin test: `tools/dev/live_kicad.py write out/live --revert` passes on the scratch board, and the installed
toolbar button opens the window on kraft_flux_mini_2 (2026-10-04). Plugin facts: the venv is at
`%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.iwers.groundtruth` (KiCad's Python 3.11.5 with
`--system-site-packages`, wxPython 4.3.1 like the dev venv); the button appears only once the venv is built
(Refresh Plugins or a restart); the Plugin and Content Manager never lists it. Since then the user has used
Simulate, drawing on the board, the Converters tab, Add selected net and the issue / row highlights from the
button; see session 5's next steps for what is still untried. Live quirks: CLAUDE.md and ARCHITECTURE "Plugin".

## Known limitations (tell the user when relevant)
- Quasi-static: no retardation and no radiation resistance. The emission estimates are differential-mode (a
  net's loop, a converter's hot loop and input current). They are good for ranking layouts, not as a
  compliance prediction. Common mode is only a budget for the switch node's stray capacitance (or an estimate
  from a given `c_stray`), not a field solution.
- Converters:
  - The ringing Q depends on `r_damp` (default 0.1 Ω plus the loop resistance), which is an assumption.
  - The pulse amplitude is the load current, without inductor ripple or reverse recovery.
  - Integrated stages are modelled as a filament between their VIN and GND pins.
  - The conducted limit tables were entered from the published standards: verify them against the edition in
    use.
- Thermal (session 5): heat leaves only through the board's faces (no part bodies, heatsinks, enclosure or
  mounting yet: large parts come out conservative); a loss whose value is not given (rds_on, dcr, diode_vf) is
  left out with a note; switching losses use the stage's rise time and C_oss. M3 items 5 and 6 would close this.
- The return of a signal is injected at GND pins only. A CMOS driver also returns through VCC (via its decap);
  the model routes that through the decaps it can see.
