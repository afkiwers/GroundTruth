# Next steps

What is left after the validation audit's round 4 (2026-10-19), and how to pick each item up. For a Claude session:
read [../CLAUDE.md](../CLAUDE.md) (environment of this machine, rules, commands) and the top of
[HANDOFF.md](HANDOFF.md) first; this file says what to do next. The plan this file held before (the move to the
bigger machine and audit items 1-5, then 6-7) is in git history (`git log -p docs/NEXT_STEPS.md`); all of it is
done, with the evidence in [VALIDATION_AUDIT.md](VALIDATION_AUDIT.md) "Round 4".

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
