# Next steps on the bigger machine

What is left from the validation audit ([VALIDATION_AUDIT.md](VALIDATION_AUDIT.md), "Remaining risks"), and how to
pick each item up on another computer. Written 2026-10-18, after round 3, the references pass and the 461H option.
For a Claude session: read [../CLAUDE.md](../CLAUDE.md) and the top of [HANDOFF.md](HANDOFF.md) first; this file
says what to do next, those say how to work in this repo.

**Status, 2026-10-18 evening (round 4, branch `next-steps`, on the bigger machine; VALIDATION_AUDIT "Round 4"):**
1 done (8x run: 4x converged below 1 GHz, ~3 dB left above; the grid note points at 4x / 8x; a GMRES restart floor
so 8x converges at all), 2 decided (a finer sampling does not help: `RASTER_MM` stays), 3 done (`_line_of` tries
more places: no kraft path falls back), 4 see its section, 5 built at the user's "Yes, build it"
(`analysis/interleave.py`). Item 4 done on the current board (`Downloads\flux-2`, 2026-10-17; VALIDATION_AUDIT's
table under Round 2, the README's loop figure). Items 6 and 7 below followed (branch `path-loss-gpu`, done; VALIDATION_AUDIT Round 4). Item 6 came out of item 4:

**6. Loss in the signal path's lines.** `emi/path.py` cascades lossless lines. /CPU/ADC_I_RAIL_1 (88 mm into the ADC's
5 pF, C304 at the start) is a quarter wave at 310 MHz; C304's ESL resonates with the line's input there, and the
7 % Z0 change of the field-solved cut moved its margin by 13 dB (C304's current 0.02 → 4.5 × the input). A real line's
copper resistance and tan δ damp such a peak. Add them (α = R/2Z0 + G·Z0/2, with R from the skin depth as
`_trace_resistance` and G from the stackup's tan δ) to `path_currents`' ABCD step, with a test against the lossy
line's closed form, then compare kraft's ADC paths.

**7. The GPU path on a small GPU, and several processes on one GPU.** On the RTX 3050 (4 GB) a full kraft Simulate
took > 2 h on the GPU against 18 min on the CPU, and a split Simulate whose two worker processes both pick the GPU
did not finish in 4 h (`test_split.py`, the lead-split test; on `main` too). Find out whether it is memory
(WDDM paging when 4 GB are full), the two processes contending, or a GMRES that stops converging on the GPU
(`tools/dev/gpu_check.py` compares results and speed); then let `gpu: auto` decline a GPU too small for the model
and give a split's parts the GPU one at a time. Until then run the suite with `CUDA_VISIBLE_DEVICES=""` on this
machine, and the GPU backend's tests (`tests/test_gpu.py`) separately.

The sections below are kept as they were written, for the commands.

| # | Item | Needs | Rough effort |
|---|---|---|---|
| 1 | Resonance heights above ~600 MHz still move with the grid | an 8× run (memory, time) | 2–6 h of computing, 1 h to evaluate |
| 2 | Copper narrower than 0.15 mm is sampled at 0.15 mm | an experiment, then a decision | 1–2 h |
| 3 | Line impedance falls back on the closed forms where no cut can be made | a check on kraft, then maybe a small fix | 1–2 h |
| 4 | Published kraft_flux_mini_2 numbers predate the fixes | one full Simulate of kraft, then doc edits | 1 h of computing, 1 h of edits |
| 5 | Combined emission of interleaved stages | **your decision first** (a new feature) | 1–2 days if wanted |

## 0. Moving the folder

**Copy:**
- this folder, `out/` included: it is not in git (`.gitignore`) but holds what the items below need.
  - `out/audit/kraft.groundtruth.yaml`: the scratch settings with SPI_SCK, SPI_MOSI and SW_CLK.
  - `out/kraft_refresh/kraft_all.groundtruth.yaml`: all 14 nets and the 4 converters.
  - `out/kraft_refresh/report_round2.json`: the last full kraft report.
  - `out/converge/`: the 1×/2×/4× convergence runs before and after the strip widths.
  - `out/audit_validation/venv_constraints.txt`: the venv's exact package versions.
- the board, read-only: `kraft_flux_mini_2` (here `C:\Users\iwers\OneDrive\Dokumente\github\kraft_flux_mini_2`). It is
  on OneDrive, so it may already be there; otherwise copy the whole project folder.
- optional: `Downloads\GroundTruth audit sources` (MIL-STD-461G and 461H as PDF, for checks against the standards).

**Git:** everything is merged into `main` (the last branch, `mil461h` with the MIL-STD-461G/H setting, at the
user's "merge what can be merged before I copy"). Nothing is pushed. Work on a new branch from `main`.

**The venv does not survive a move**: its launchers and its editable install point at the old folder (this happened
once already, VALIDATION_AUDIT round 2). Delete `.venv` and rebuild it. In PowerShell, from the repo:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -c out\audit_validation\venv_constraints.txt -e ".[plugin,dev,thermal]"
.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu   # the GPU tests' CPU build
```

On a machine with an NVIDIA GPU, install PyTorch's CUDA build into the dev venv instead (see pytorch.org for the
current command) and check it with `.venv\Scripts\python tools\check_gpu.py`. The solver then uses the GPU for the big
runs (`analysis.gpu: auto`). Never install PyTorch into the KiCad plugin's own venv except with `tools/install_gpu.py`.

**Check the move** (a few minutes):

```powershell
.venv\Scripts\python -m pytest -q tests\test_solver.py tests\test_ce101.py
.venv\Scripts\python tools\dev\ref_keys.py          # 102 keys cited, 102 in docs/REFERENCES.md, 0 unknown
```

**CLAUDE.md's "Environment" section describes this laptop** (paths, KiCad location, the plugin folder, no GPU).
Update it for the new machine before relying on it.

**Rules that still hold:** the real board is only read, never written or saved; settings for runs are scratch
copies under `out/`, never next to the board; do not change the user's KiCad configuration; do not push; ask
before merging into `main`; commit messages end with the `Co-Authored-By` line.

The commands below are for Git Bash from the repo folder. Set `B` to the board on the new machine:

```bash
B=C:/Users/iwers/OneDrive/Dokumente/github/kraft_flux_mini_2/kraft_flux_mini_2.kicad_pcb
export PYTHONIOENCODING=utf-8 MSYS_NO_PATHCONV=1      # MSYS_NO_PATHCONV: net names start with "/"
```

## 1. Resonance heights above ~600 MHz (SP-19)

**Where it stands.** On kraft_flux_mini_2's SPI_SCK board-wide model, 2× and 4× still differ by 4.4 dB rms at
1–1.5 GHz. 4× has a peak at 1260–1300 MHz that 2× lacks. The largest mode (the F.Cu ground pour against the In1
+3V3 plane, by C1105/C1110) agrees at 2× and 4× (1020 MHz) but sits 8 % low at 1×. Results above 600 MHz carry a
note that says so (`simulate.grid_sensitive_note`). Details: ARCHITECTURE.md "Board-wide resonances above 1 GHz".

**What to run.** 8× on the same 25 harmonics as the existing runs. 4× was 130 000 nodes and 38 minutes on the
laptop's 4 threads; 8× is about twice the nodes, and GMRES needs more iterations near the resonances, so plan for
hours. Use the GPU if there is one.

```bash
.venv/Scripts/python tools/dev/cavity_converge.py $B out/audit/kraft.groundtruth.yaml --only /CPU/SPI_SCK \
    --resolutions 8 --band 500e6,1500e6 --threads 8 --json out/converge/spi_sck_widths_8x.json \
    > out/converge/spi_sck_widths_8x.log 2>&1
.venv/Scripts/python tools/dev/converge_compare.py out/converge/spi_sck_widths.json \
    out/converge/spi_sck_widths_4x.json out/converge/spi_sck_widths_8x.json --table
```

If memory allows, 16× after it (`--resolutions 16`). A solve that runs out of memory is retried alone, then
reported; watch the machine's memory during the first frequencies.

**How to read it.**
- 4× and 8× within ~1–2 dB rms: 4× is good enough above 1 GHz. Say so in the grid note (point it at 4×, not "the
  next resolution"), in ARCHITECTURE's table and in VALIDATION_AUDIT's SP-19 row.
- 4× and 8× still apart: find which copper resonates at the peaks that disagree, on both grids, and compare:

  ```bash
  .venv/Scripts/python tools/dev/cavity_hotspots.py $B out/audit/kraft.groundtruth.yaml --only SPI_SCK \
      --f 1.28e9 --resolution 8 --top 12
  ```

  On 2026-10-18 the same comparison found that the mode was one mode at two frequencies, not a lost via
  (`out/converge/hot_new_*.log`). Look for copper narrower than the grid near the hot cells (item 2), or a
  connection that exists on one grid only (what a via joins on each grid:
  `tools/dev/via_layers.py $B out/audit/kraft.groundtruth.yaml --only SPI_SCK --at X Y --resolutions 4 8`).

## 2. Copper narrower than 0.15 mm

**Where it stands.** Every strip now carries its copper's width, but the copper is sampled at `RASTER_MM` = 0.15 mm
(at least 2 × 2, at most 6 × 6 per cell; `analysis/simulate.py`). A 0.35 mm neck therefore reads 0.29 or 0.43 mm wide,
depending on where it falls. The neck test case reads 717–797 MHz over seven grids, around ~750.

**What to try.** A finer sampling, without changing the code first:

```bash
.venv/Scripts/python tools/dev/neck_converge.py --grids 1 0.7 0.5 0.35 0.25 0.15 0.1
.venv/Scripts/python tools/dev/neck_converge.py --grids 1 0.7 0.5 0.35 0.25 0.15 0.1 --raster 0.075 --sub-max 12
```

**Decision.** If the finer sampling narrows the spread clearly (say to ±2 %), try it on kraft. The sampling costs
memory and time in the rasterisation (4× the samples per cell), not in the solve. Time `prepare_route` for SPI_SCK at
1× before and after. If kraft's results and timing are acceptable, change `RASTER_MM` and the `min(6, …)` cap in
`simulate.py`, update the docstrings and ARCHITECTURE ("A strip is as wide as the copper it crosses"), and run the
full suite.

## 3. Line impedance where no cut can be made (ISSUE-5's remainder)

**Where it stands.** A path's line is the field-solved cut at its longest track (`simulate._line_of`). Where no cut
can be made there, the closed forms take over: up to ~10 % high on very lopsided inner layers, no solder mask.

**What to run.** List which of kraft's paths fall back:

```bash
.venv/Scripts/python tools/dev/line_cut_check.py $B out/kraft_refresh/kraft_all.groundtruth.yaml
```

With the three-net settings none did (2026-10-18). Round 2 saw one stretch without a cut (U301−), which this check
on all 14 nets should show.

**If some fall back:** let `_line_of` try more places before giving up: the longest track at a quarter and three
quarters of its length, then the next longest track. Add a test in `tests/test_validation_audit.py` next to SP-26 (a
board where the longest track's middle has no cut and a later place does), and run the full suite.

## 4. Refresh the published kraft numbers

**Where it stands.** HANDOFF.md and a few README examples quote kraft results measured before the audit's fixes
(VALIDATION_AUDIT "Remaining risks": the SPI margins about 1 dB lower now, conv1's switch-node copper about 42 %
more). The plane-displacement example in the README is already current.

**What to run.** One full Simulate of all 14 nets and 4 converters on the scratch settings (the board only read, no
history), ~25 minutes on the laptop:

```bash
NETS=("/CPU/SPI_SCK" "/CPU/SPI_MISO" "/CPU/SPI_MOSI" "/CPU/SW_CLK" "/DCDC Stage/Rails/SW2" "/COMMS/CAN_RX"
      "/COMMS/CAN_TX" "Net-(U1001-VIB)" "Net-(U1001-VOA)" "/DCDC Stage/Rails/SW1" "Net-(U301-+)" "Net-(U301--)"
      "/CPU/ADC_I_RAIL_1" "/CPU/ADC_I_RAIL_2" "buck conv1" "buck conv2" "buck conv3" "buck conv4")
mkdir -p out/kraft_refresh/now
.venv/Scripts/groundtruth simulate $B --settings out/kraft_refresh/kraft_all.groundtruth.yaml --format json \
    --out-dir out/kraft_refresh/now --fail-on error -q --nets "${NETS[@]}" > out/kraft_refresh/now/console.txt 2>&1
.venv/Scripts/python tools/dev/result_diff.py out/kraft_refresh/report_round2.json \
    out/kraft_refresh/now/groundtruth-report.json --top 40
```

(`--fail-on error` exits 1 when the board has errors, which kraft has: that is not a failed run.)

**Then** go through the places that quote kraft numbers and update the ones that moved:
- the README's examples (search for `kraft_flux_mini_2`: the Sharper map's SW1 figures, the safety counts, the
  control loop's conv1, the signal-integrity Z0s, the PDN grid comparison);
- HANDOFF.md's "Current state";
- VALIDATION_AUDIT.md's round-2 table, as a new row "after rounds 3 and later".

Say in each place when the numbers were taken.

## 5. Interleaved stages (needs your decision)

**Today:** two phases of one controller (SW1 and SW2 of a dual buck) are analysed as separate stages, each with its
own hot loop, conducted emission and radiated estimate. For DC and thermal this is exact: their switching variances
live in different copper (VALIDATION_AUDIT round 3).

**What modelling it would mean:**
- Phases 180° apart cancel part of each other's input current at the LISN (at odd harmonics of f_sw, ideally all of
  it at the fundamental) and add at the even ones.
- Their hot loops' fields add with the same phase shift.
- Needs: an interleave group and a phase per stage in the settings (detection can propose it from a dual
  controller's pins); the conducted line currents and the loop moments of a group added as phasors per harmonic,
  with n·φ for harmonic n; the receiver reading the sum; reports per group as well as per stage; tests against two
  ideal sources 180° apart (the fundamental cancels, the second harmonic doubles).
- About 1–2 days with tests and docs. Ask the user before starting.

## Before committing anything

```powershell
.venv\Scripts\python -m pytest -q                       # the full suite, ~45-75 min; GUI timing tests can fail on a
                                                        # busy machine: re-run them alone
.venv\Scripts\python tools\dev\undocumented.py          # 0 functions without a docstring
.venv\Scripts\python tools\dev\py311_check.py           # the plugin code stays Python 3.11
.venv\Scripts\python tools\dev\ref_keys.py              # new physics cites its sources (docs/REFERENCES.md)
.venv\Scripts\python tools\dev\ast_same.py main         # for a docs-only change: "only comments and docstrings changed"
```

Update HANDOFF.md (a dated entry at the top), VALIDATION_AUDIT.md where a remaining risk changes, and Help
(`src/groundtruth/ui/help.html`) when users would notice the change.
