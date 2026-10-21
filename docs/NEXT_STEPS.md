# Next steps

What is left after the validation audit's rounds 4 (2026-10-19) and 5 (2026-10-20), and how to pick each item up.
For a Claude session:
read [../CLAUDE.md](../CLAUDE.md) (environment of this machine, rules, commands) and the top of
[HANDOFF.md](HANDOFF.md) first; this file says what to do next. The plan this file held before (the move to the
bigger machine and audit items 1-5, then 6-7) is in git history (`git log -p docs/NEXT_STEPS.md`); all of it is
done, with the evidence in [VALIDATION_AUDIT.md](VALIDATION_AUDIT.md) "Round 4".

## Open (2026-10-21): after the electro-thermal and pin-share merges

The electro-thermal coupling (`thermal.electrothermal`; kraft: settled in 2 rounds, T_max unchanged) and the pin
shares (a pin's copper counted once, [A51]) are in `main`. Still open, with the details in the top entries of
[HANDOFF.md](HANDOFF.md) (kraft with the pin-share fix: the busiest via by Q304 0.81 → 0.64 A, T_max unchanged):
three GUI timing tests to rerun alone, and a new plugin package (the one in `out/dist`, 0.1.42214, has neither) once
the user wants it.

## Done (2026-10-20): audit round 6

The numerical audit (stopping tests, convergence against independent references, adversarial copper, metamorphic
properties, stale state, failure handling) and its fixes (ISSUE-25 to -32), on branch `audit-6`. The record is
[NUMERICS_AUDIT.md](NUMERICS_AUDIT.md), the summary VALIDATION_AUDIT.md "Round 6". What it left open is under "Open,
technical" (item 7).

## Done (2026-10-20): audit round 5

The physics audit with a track narrowed from 0.4 to 0.1 mm followed through every calculation, and every fix it led
to (ISSUE-11 to -23), merged into `main` from branch `audit-5` at the user's "update handover, then commit, merge".
The record is [PHYSICS_AUDIT.md](PHYSICS_AUDIT.md), the summary VALIDATION_AUDIT.md "Round 5", the session's entry
the top of [HANDOFF.md](HANDOFF.md). Full suite on the branch: 960 passed, 5 skipped. What it left open is under
"Open, technical" (item 6).

## Done (2026-10-18 / 19)

| # | Item | Outcome | Where |
|---|---|---|---|
| 1 | Board-wide resonances above 600 MHz vs the grid | 8x run: 4x converged below 1 GHz (0.8 dB rms vs 8x), ~3 dB left above; the grid note asks for 4x / 8x; `peec.GMRES_RESTART_MIN` so 8x converges | ARCHITECTURE "Board-wide resonances above 1 GHz" |
| 2 | Copper narrower than the 0.15 mm sampling | a finer sampling does not narrow the ±4 % scatter: `RASTER_MM` stays | ARCHITECTURE "A strip is as wide as the copper it crosses" |
| 3 | Line impedance without a cut | `_line_of` tries 3 places on the 3 longest tracks: no kraft path falls back | ARCHITECTURE "signal path" |
| 4 | Published kraft numbers | refreshed from the current board (`Downloads\flux-2`) | VALIDATION_AUDIT table under "Round 2" |
| 5 | Interleaved stages | `analysis/interleave.py`: phasor sums per group, detection proposes groups | ARCHITECTURE "Interleaved stages", help |
| 6 | Lossless signal-path lines | copper and dielectric loss in `emi/path.py` [A36] | ARCHITECTURE "signal path" |
| 7 | The GPU path on a small GPU | `gpu: auto` times the GPU against the CPU (from 1.5x), one process per GPU | ARCHITECTURE "GPU backend" |
| 8 | Analog nets on 10 MHz / 2 ns | detection takes a driving op-amp's slew rate / bandwidth [A37] | ARCHITECTURE "suggest_net" |
| 9 | Resonance heights above 1 GHz: 16x | the worst margin settles to ±2 dB from 4x on, 8x no closer: the grid note asks for 4x and gives ±2 dB above 1 GHz | ARCHITECTURE "Board-wide resonances above 1 GHz" |
| 10 | Interleaving: CE101, boosts | CE101 combined per group; end-to-end tests on a 2 kHz pair and on two boosts | ARCHITECTURE "Interleaved stages" |
| 11 | GPU memory | a model larger than 80 % of the device's free memory is solved on the CPU before upload | ARCHITECTURE "GPU backend" |
| 12 | A36: the plane's loss | from Johnson's current spread in closed form (`plane_resistance`), not "as the strip" | REFERENCES A36 |
| 13 | README figures | safety counts and PDN grids re-taken on the current board | README |

All merged into `main`; the full suite on `main` after the last of them (cebe708, GPU visible, `out/full_suite_final.log`):
**885 passed, 5 skipped, 0 failed** in 29 min.

## Waiting on the user

1. **The plugin in KiCad is the old version.** KiCad here has GroundTruth from the Plugin and Content Manager
   (`Documents\KiCad\10.0\3rdparty\plugins`); `tools/install_plugin.py` would add a second copy (every button
   twice). A new package needs the user's release key (`%LOCALAPPDATA%\GroundTruth\release`, still on the
   laptop): `tools/make_pcm_package.py` would create a new key here, which the user's compute servers do not
   trust. Once the user has copied the key: `.venv\Scripts\python tools\make_pcm_package.py`, then Plugin and
   Content Manager > Install from File with `out/dist/groundtruth-<ver>-<commit>-pcm.zip`.
2. **The analog nets in the user's kraft settings.** Detection's op-amp rule (item 8) applies to nets detected or
   added from now on; the existing entries Net-(U301--), /CPU/ADC_I_RAIL_1 and _2 still say 10 MHz / 2 ns, so their
   "emissions" at 310 MHz / 1.59 GHz describe a signal that does not exist (VALIDATION_AUDIT round 4). Re-detect
   them or set the real signal (a few kHz, µs edges). The ADC paths start at the ZXCT1107s (U302, U304), modelled
   `open`: give them `Gain` and `Bandwidth` fields to make them amplifier stages, and check that their pins are
   recognised (`components.models.amplifier_pins`: else `amp_in` / `amp_out` GT labels, as on U301).

## Open, technical

3. **Untestable here:** the GPU path on a big GPU (its speed-up, the memory guard on a large model). On a target
   machine: `python tools/check_gpu.py`, `tools/dev/gpu_contention.py --procs 2`, `tools/dev/gpu_check.py BOARD
   SETTINGS --only SPI_SCK`.
4. **Model limits, documented, nothing planned:** a board-wide resonance's margin above 1 GHz is good to about
   ±2 dB (SP-19); a resonance set by copper narrower than the pitch scatters about ±4 % from grid to grid (how the
   grid falls on the geometry: a finer copper sampling did not reduce it); the strip's own edge crowding is left
   out of the line loss (A36); the emission estimate is quasi-static and differential-mode (no cables).
5. **A FEM solver beside PEEC, for later:** investigated 2026-10-19 and not planned for now (the user: "I don't
   think it's worth right now but might be in the future"). The findings (toolchains on Windows, a 3D eddy-current
   spike with NGSolve against PEEC), the options and a phased plan with a PEEC / FEM / Both switch are in
   [FEM_OPTION.md](FEM_OPTION.md); `tools/dev/fem_spike.py` reruns the spike.
6. **Left by audit round 5, documented** (PHYSICS_AUDIT.md sections 5 and 8): normal thermal quality's mesh budget
   on long narrow hot tracks (ISSUE-24: part of a 100 mm, 0.05-0.1 mm track kept 0.15 mm cells and read 5-12 % cool
   there while the note estimated 2 K; fine quality resolves it; refining only the hot blocks covered the track but
   read kraft's hottest parts 1.5-3 K cooler than fine, so it was not kept; a planner that refines a track's band
   without its eight neighbours but keeps them around pads would be the next try, checked with
   `tools/dev/dc_thermal_summary.py --quality fine` against normal on kraft); a width step in copper wider than 4
   cells or at a junction (within ±d/2, A39: `tools/dev/neck_check.py` for a neck); the references nobody could
   open (CISPR 25's average column and edition, IPC-2221's §6.2 text, Incropera 7th edition's face-down coefficient);
   the plugin's board check before a run not yet tried against a live KiCad (`tools/dev/live_kicad.py`).

7. **Left by audit round 6** (NUMERICS_AUDIT.md section 12): a neck shorter than about two cells reads up to ~20 %
   of its own resistance low at 1x (≤ 0.36 square; 4x resolves it); a track narrower than a cell +1 to +3.6 %
   resistive at shallow and some angles at 1x (ISSUE-31); the peak density ~10 % low at 1x (the cell is
   the 0.25 mm window); the plane pairs have no fringing capacitance (A46, a cavity ~+1.5 % high); GMRES's one norm
   over volts and amperes measured harmless on kraft (`tools/dev/tol_check.py` checks another board); the board's
   part index and plane cache keyed by list identity (safe today); the radiated solve budget can leave a gap
   away from the worst unresolved (now said in a note: ISSUE-32); the GPU path and a live KiCad not exercised in
   round 6.

## Before committing anything

```powershell
.venv\Scripts\python -m pytest -q                       # the full suite, ~30 min on this machine (GPU visible is
                                                        # fine since 2026-10-19); GUI timing tests can fail on a
                                                        # busy machine: re-run them alone
.venv\Scripts\python tools\dev\undocumented.py          # 0 functions without a docstring
.venv\Scripts\python tools\dev\py311_check.py           # the plugin code stays Python 3.11
.venv\Scripts\python tools\dev\ref_keys.py              # new physics cites its sources (docs/REFERENCES.md)
.venv\Scripts\python tools\dev\ast_same.py main         # for a docs-only change: "only comments and docstrings changed"
.venv\Scripts\python tools\dev\gui_probe.py BOARD --settings S   # for a GUI change: no stalls over 150 ms
```

Update HANDOFF.md (a dated entry at the top), VALIDATION_AUDIT.md where a remaining risk changes, and Help
(`src/groundtruth/ui/help.html`) when users would notice the change.
