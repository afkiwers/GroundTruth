# Speed of a Simulate: research, measurements and plan

The user, 2026-10-24, with a screenshot of an 8x Simulate of refboard (four converters) on the M2 Mac compute
server at 48:47 and 94 %: "do a thorough research on how to optimise the calculations to cut down on computation
time", then "also mention estimated time savings". The plan agreed that day: **M0** measure (branch `speed-stats`),
**M1** result-preserving speed-ups, **M2** fewer solves and factorisations, **M3** a faster sparse solver (the only
trade-off option the user chose; the others are recorded in section 6). Each milestone on its own branch, shown on
the user's boards before the next starts.

## 1. What the kept runs show

Every plugin Simulate is kept (`<board folder>/groundtruth/history/runs/*/run.json` and `results.json`). refboard's, all
on 192.0.2.145 (the M2: 8 cores, 4 performance and 4 efficiency, 24 GB, fanless):

| Run | Res | Items | Board-wide model | Board-wide solves (GMRES its) | Time |
|---|---|---|---|---|---|
| 10-24 07:26 | 1x | 6 nets (settings 329e49…) | 33 972 nodes | 18 (41-75) | **66 s** |
| 10-24 10:15 | 8x | **the same 6 nets, the same settings file** | 256 077 | 18 (74-178) | **5 538 s** |
| 10-24 12:57 | 8x | the 6 nets, other settings changed | 256 077 | 18, identical iteration counts | 7 195 s |
| 10-23 13:27 / 10-22 20:15 | 2x / 4x | 10 nets | 65 764 / 130 003 | 34 (49-86 / 60-200) | 200 s / 474 s |
| 10-18 / 10-20 / 10-19 | 1x / 8x / 8x | conv1 / conv1+2 / four converters | 34k / 256k / 256k | 10 / 22 / 32 (122-981) | 51 / 1 450 / 4 030 s |
| 10-15, 10-16 (two) | 16x | conv1+conv2, the same solves three times | 493k; local models 1.2-1.35 M, DC 3.55 M | GMRES failures noted | 4 845 / 2 639 / 3 443 s |

The same job took **84x longer at 8x than at 1x**, while its work grows about 20-25x (8x the unknowns, about 2.4x
the GMRES iterations, factorisations about n^1.5). From 2x to 4x the time grew 2.4x, from 4x to 8x 12-15x.
**Measured since (4.3):** the same 8x job, solve for solve, took 974 s on the M2 with 15.8 GB free: most of the 84x
was the Mac's state during those runs (memory beside other programs), not the computation.

## 2. Where a Simulate's time goes

- **A solve** (`peec.PlaneStack.solve` → `_gmres`, right-preconditioned restarted GMRES [Saad 2003]): each iteration is
  one L_p product (`Lp_matvec` → `_conv`: batched `fft2`/`ifft2` and the numba multiply-add `_mac.kernel_mac`), one
  preconditioner application (`_HostMath.precond`: the Schur complement Arᵀ·D⁻¹·Ar [Benzi 2005] solved by SuperLU
  [Demmel 1999], single-threaded) and a classical Gram-Schmidt step (`_HostVectors.project`, zgemv, which holds the
  GIL). A frequency more than ±25 % (`PRECOND_REUSE`) from an earlier round's factorisation gets its own.
- **Per net**: a route model (one solve) and the board-wide model every net and stage shares (4-13 solves).
- **Per converter** (`power.prepare_converter`): the hot loop (0.5 / 30 / 100 MHz), the input path for the conducted
  band (9-12 frequencies from 150 kHz to 12 MHz), a victims model (30 / 100 MHz), the board-wide model (4-11), and
  about five DC paths on the whole-board DC model at 10 Hz (`dc_board_build`). At 8x the local models are 600-720k
  nodes, the DC model 1.8 M.
- **Scheduling** (`simulate.solve_setups` → `run_parallel`, in rounds): threads from `solver_threads` (at most 4),
  cut by `memory_threads`; every factorisation passes the memory gate (`peec.GATE`).

## 3. Root causes, by impact

1. **High resolutions run one solve at a time.** `memory_threads` divides the free memory by the largest model's
   `solve_bytes` over *all* runs and uses that for *every* round. `solve_bytes` counts SuperLU's fill × 16 B ×
   `LU_COMMIT` 6.5 (a Windows commit-charge effect [A38]; untouched pages cost nothing on macOS) plus a full 101-row
   Krylov basis (`gmres_restart`; the basis only grows as the iterations need it). At 8x, before the first
   factorisation: a route model ≈ 7-8 GB, an input-path model ≈ 9 GB, the DC model ≈ 22 GB. So on the M2 every round
   runs one solve, and so does a converter job on any machine with less than ~57 GB free. The memory gate also frees
   every cached factorisation and takes least-fill pivots when one estimate exceeds 80 % of the free memory. This is
   likely why a 25x run on the Dell used 13 of 62 GB (the user, 2026-10-15). Measured here at 1x already: see 4.1.
2. **Factorisations of the large inductive models**: about 12 per converter of 600-720k nodes at 8x (the conducted
   band spans 4.4 in ln f: about 9 windows of ±25 %), about 50 for four converters.
3. **Same-frequency solves done separately**: the three SPI nets share all four board-wide frequencies; conv1 and
   conv2 (both 750 kHz) share 10 of 11; a signal path's shunt loops are solved again at the same frequency
   (`simulate.solve_at`).
4. **The DC model is a full complex PEEC solve at 10 Hz** (L_p kernels, FFT products, complex SuperLU, a GMRES basis)
   of what is a resistive problem: at 10 Hz the inductance moves |I| by about (ωL/R)² ≈ 1e-7.
5. **SuperLU itself**: sequential LU (no LDLᵀ for this complex symmetric matrix), ordering and symbolic analysis
   repeated at every frequency although the pattern never changes, ~6.5x its fill committed on Windows, a 32-bit size
   limit (the 25x failures).
6. **Iterations grow with resolution** (41-75 at 1x, 122-286 at 8x, outliers to 980; inductive solves hit the limit
   at 16x): the `diag` and `sheet` preconditioners leave out the in-plane mutual inductance.
7. **Identical work recomputed** (10:15 and 12:57; the three 16x runs) and **the board-wide model gaining nothing past
   4x** (ARCHITECTURE "Board-wide resonances above 1 GHz": 4x vs 8x 0.8 dB rms below 1 GHz, the worst margin within
   ±2 dB of 16x from 4x on). Recorded, not planned (section 6).
8. **The machine**: the M2 Air throttles under long loads (no fan), has 4 slower efficiency cores, and had ~4 GB free
   beside the user's other programs: part of the 5 538 vs 7 195 s spread for identical work.

## 4. Measurements

The statistics (`solver/stats.py`, M0) are kept with every Simulate in `result.solver["stats"]` and so in every kept
run's run.json; `tools/dev/run_stats.py RUN` prints them. Per model: factorisations (seconds, fill, made again,
the memory gate's wait), solves, iterations, the seconds of GMRES and of each part of an iteration; per round the
threads and what limited them; per phase the wall time; CPU time, memory, swap.

### 4.1 This laptop (laptop: 4 cores / 8 threads, 15.8 GB, 1.8 GB available), job A at 1x

`out/speed/laptop_jobA_1x.json` (`tools/dev/profile_sim.py … --resolution 1 --no-profile --json`; a test run beside
it, so the times are high; the shares are what counts):

- threads: 4 by cores, **1 by memory** (1.76 GB available, the largest solve the board-wide model's ~1.0 GB
  estimate): every round ran one solve; CPU 18 % of all cores.
- phases: checks 9 %, models 22 %, solves 61 %, finishing 8 %.
- board-wide model (33 972 nodes): 8 factorisations 45 s (fill 6.3 M), 23 solves, 1 244 iterations, 81 ms each:
  **preconditioner 46 % (SuperLU's triangular solves 33 %)**, L_p product 26 % (FFTs 15 %, multiply-add 6 %),
  Gram-Schmidt 14 %.
- route models (75-80k nodes): 103-127 ms per iteration: L_p 44 % (FFTs 22-26 %), preconditioner 25 % (SuperLU
  14 %), Gram-Schmidt 14 %.

Job B at 2x on this laptop (`out/speed/laptop_jobB_2x.json`, 1.7 GB available): 2 406 s.

- threads: 4 by cores, **1 by memory** (the DC model's solve budgeted at 5.21 GB); 13 groups (the factorisations
  kept at once did not fit); CPU 24 % of all cores.
- **The memory gate freed cached factorisations 57 times**, and they were made again: the board-wide model made 28
  factorisations (311 s, ~11 s each) of which **14 were repeats** (~155 s); its LISN-derived stacks redid their
  low-rank updates too. Where memory is short, freeing to make room costs real time.
- the board-wide model (65 764 nodes) is ~56 % of the run: conv1's and conv2's 11 solves took ~2 640 iterations each
  (two borrows past their budget re-factorised); two refinement rounds of three solves took 357 and 400 s.
- the conducted-band models (159-182k nodes) ~490 s of GMRES (FFT-bound: L_p 37-39 %); hot loops and victims models
  ~240 s; **the DC paths 32 s** (16 solves): at 2x the DC model costs little time, its weight is the memory estimate
  that makes the whole run single-threaded.
- totals: 79 factorisations 430 s, 68 low-rank updates 8 s, 109 solves, 9 736 iterations 1 824 s.

### 4.2 Per piece at 4x (`tools/dev/solver_bench.py`, this laptop, 2 FFT threads)

`out/speed/bench_jobA_4x.log` (job A, `--only SPI_SCK`; ms per call, the shortest of three):

| | Route model (292 285 nodes, 861 171 unknowns, `diag`) | Board-wide model (129 979 nodes, 481 888 unknowns, `sheet`) |
|---|---|---|
| L_p product (FFTs + multiply-add) | 180 (132 + 43) | 93 (52 + 18) |
| SuperLU triangular solve (solve_S) | 62 | **108** |
| sparse products (A twice, D⁻¹ twice) | 51 | 42 |
| Gram-Schmidt step | 46 (29 vectors) | 37 (50 vectors) |
| one iteration, about | 340 | 280 |
| one factorisation (fill) | 2.5 s (9.9 M) | **38.4 s (31.4 M, 480 MB) = ~137 iterations** |
| 3 right-hand sides together: FFT / SuperLU solve | 101 vs 3 × 34 / 137 vs 3 × 62 | 40 vs 3 × 14 / 297 vs 3 × 108 |

- On the board-wide model **SuperLU is the largest cost**: its triangular solve is ~39 % of an iteration, and a
  factorisation costs as much as ~137 iterations while a solve takes 60-200: a solve that factorises spends 40-70 %
  of its time in SuperLU's factorisation. A faster sparse solver (#7) is worth more than estimated first.
- On the route models the **FFTs** are the largest part (~40 % of an iteration) and the factorisation is cheap.
- Several right-hand sides together save little: stacked FFTs nothing, SuperLU's k-column solve 9-26 %. Lock-step
  solves (#5) are worth less than estimated first.

`out/speed/bench_jobB_conv1_4x.log` (job B, conv1's own models; its models, the whole-board DC model and the victims
model included, built in 91 s):

| | Hot loop (302 180 nodes, `diag`) | Input path, conducted band (313 753 nodes, `diag`, + the LISN) |
|---|---|---|
| L_p product (FFTs + multiply-add) | 177 (125 + 42) | 167 (117 + 38) |
| SuperLU triangular solve | 65 | 78 |
| sparse products | 58 | 53 |
| Gram-Schmidt step (28 / 27 vectors) | 44 | 42 |
| one iteration, about | 345 | 340 |
| one factorisation | 5.3 s (11.7 M) = ~15 iterations | 5.2 s (with the LISN's low-rank update) |

- A converter's own models are **FFT-bound** like the route models; their factorisations are cheap (~15
  iterations' worth at 4x), so fewer factorisations (#6) help job B less than estimated first, and a faster FFT
  (pyFFTW / MKL, section 6) more. Building the models is a phase of its own (91 s here at 4x: M0's "models").

### 4.3 The M2 at 8x (jobs A and B with the statistics)

**Job A** (2026-10-24 18:28, build 0.1.42666, `history/runs/2026-10-24_18-28-14`): **974 s**. Every solve took the
same iterations as in the 10:15 and 12:57 runs of the same board (5 538 and 7 195 s): the same computation, 5.7-7.4x
faster. The code added since does not speed anything up, so this morning's runs lost that time to the Mac's state,
most likely memory: this run peaked at 13.3 GB with one solve at a time; it started with 15.8 GB free and swapped
196 MB, where the morning's had other programs beside them (~4 GB free in the user's screenshot). The clean 8x run
is 14.8x the 1x run (66 s), within the ~20-25x of the work itself. Section 1's "84x" was mostly the machine.

- threads: 4 by cores, **1 by memory** (13.97 GB available, the route model's solve budgeted at 8.03 GB); every round
  one solve; CPU 1 212 s in 969 s wall (16 % of the 8 cores). The memory gate freed cached factorisations 4 times.
- phases: models 41 s (4 %), **solves 885 s (91 %)**, finishing 36 s (4 %).
- 18 factorisations 108 s (the board-wide model's 8 in 82 s: ~10 s each, 68.9 M fill; ~4x quicker than this
  laptop's 4x one); 40 solves, 3 730 GMRES iterations **878 s**.
- board-wide model (256 077 nodes, 954 976 unknowns): 23 solves (5 of them shunt loops), 2 820 iterations, 226 ms
  each: **SuperLU's triangular solves 53 %**, Gram-Schmidt 27 %, the L_p product 10 % (FFTs 4 %: eight cores).
- route models (585-697k nodes, 1.7-2.0 M unknowns): 222-280 ms per iteration: **Gram-Schmidt 36-42 %**, SuperLU
  21-25 %, L_p 24-26 %. Ideal references (236-391k nodes): SuperLU 22-28 %, L_p 26-34 %, Gram-Schmidt 17-21 %.
- memory: up to 11.8 GB sampled, 13.3 GB peak, 9.1 GB free at the least.

What it means for the plan: on the M2 the time is GMRES iterations in one thread; their largest parts are SuperLU's
triangular solves (M3 aims there) and Gram-Schmidt over a basis of up to 100 vectors of 1-2 M unknowns (also the
largest piece of a solve's memory: ~2-3 GB on a route model). With 13 GB already used by one solve, two or three at
once (M1 #1) fit on the 24 GB Mac only with less memory per solve (the basis first) and nothing else open; the
estimates of section 5 are re-based on 974 s for job A.

**Job B** (2026-10-24 19:46, `history/runs/2026-10-24_19-46-44`): **4 520 s** (10-19's run of the four converters:
4 030 s, with older settings and code; the conv1 / conv2 board-wide solves now take 3 910 iterations each against
3 391 then).

- threads: 4 by cores, **1 by memory** for the whole run (11.94 GB available; the largest solve the **DC model's,
  budgeted at 20.89 GB**); 13 groups; CPU 15 % of the 8 cores. The memory gate freed cached factorisations 74 times:
  the board-wide model made 31 factorisations (348 s, ~11 s each), **16 of them repeats** (~180 s).
- phases: models 71 s, **solves 4 422 s (98 %)**, finishing 9 s.
- time by model kind (rounds): **board-wide 2 928 s (65 %)**, conducted-band models ~970 s (21 %), hot loops 254 s
  (6 %), victims 224 s (5 %), **DC 46 s (1 %)**.
- board-wide: conv1 and conv2 11 solves each, 3 910 iterations each (~355 per solve; three borrows past their budget
  re-factorised), ~1 000 s of GMRES each; conv3 1 651 iterations 420 s; 255 ms per iteration: **SuperLU 52 %,
  Gram-Schmidt 28 %**, L_p 10 %.
- conducted-band, hot-loop and victims models (580-720k nodes, 1.7-2.1 M unknowns): 256-348 ms per iteration:
  **Gram-Schmidt 36-43 %**, SuperLU 21-26 %, L_p 20-23 %; their factorisations cheap (2-3 s each).
- the DC model (1 797 392 nodes, 5.3 M unknowns): **one factorisation, 8.2 s** (81 M fill); 16 solves of 1-2
  iterations, 35 s. Its estimate (20.9 GB) is ~10x what it uses, and it alone makes the whole run single-threaded.
- totals: 86 factorisations 491 s (11 %), 72 low-rank updates 14 s, 108 solves, 14 243 iterations **3 907 s (87 %)**.
- memory: up to 10.0 GB sampled, 15.0 GB peak; 17.9 GB free at the start, 5.0 GB at the least; swap in 1.8 GB.

### 4.4 What the two 8x runs say

Across jobs A and B on the M2, GMRES iterations are ~88 % of the time and run on one thread. Of the iterations' time:
**SuperLU's triangular solves ~45 %, Gram-Schmidt ~32 %**, the L_p product (FFTs and multiply-add) ~14 %, the rest
~9 %. Factorisations are ~11 % (a third of the board-wide ones repeats where the memory gate freed them). The DC
model costs 1 % of the time; only its estimate matters. One solve keeps the process at 13-15 GB, so more solves at
once on the 24 GB Mac need less memory per solve too (the Krylov basis: 1.5-3 GB per solve; the solutions' dense
grids; the cached factorisations).

## 5. Options and estimated savings

Reference jobs on the M2: **A** = the 6 nets at 8x (92-120 min), **B** = the four converters at 8x (52-67 min).
Estimates from the code paths and the kept runs, to be replaced by M0's measurements; savings compound on what is
left, they don't add.

| # | Change | Results | Effort | Est. saving A / B | Milestone |
|---|---|---|---|---|---|
| 1 | Memory-true scheduling: commit factor per platform (measured), the basis a model really needs (its measured iterations), threads per round from that round's own tasks, tasks admitted by memory (big and small side by side) | identical | M | −45-60 % / −40-55 % with ≥ 16 GB free; −15-35 % on the M2 at 4-8 GB free | M1 |
| 2 | DC paths as a real resistive network (conductance Laplacian factorised once in float64; no kernels, no GMRES) | ≤ 1e-6 relative | M | — / −5-15 %, and no 22 GB estimate serialising converter runs | M1 |
| 3 | GIL-free Gram-Schmidt (numba nogil) | rounding | S | lets #1 scale: +10-25 % parallel efficiency | M1 |
| 4 | GMRES checks the true residual when its estimate first meets the tolerance (not 0.5·tol or the cycle's end) | ≤ tol | S | −3-8 % | M1 |
| 5 | Lock-step solves of one model at one frequency (batched FFTs, k-column SuperLU solves; identical LISN branches share a derived stack) | rounding | M | −15-25 % / −8-12 % estimated first; **−2-6 %** after 4.2 (stacked FFTs gain nothing, k-column solves 9-26 % of solve_S) | M2 |
| 6 | Fewer inductive-model factorisations: a borrowed preconditioner with D(f) at the solve frequency and a rescaled Schur solve, the window widened as far as measured iterations allow; deterministic sharing within a round | ≤ tol | M | −3-8 % / −5-15 % estimated first; job B **−2-5 %** after 4.2 (its models factorise in ~15 iterations' worth); more on the board-wide model (~137) | M2 |
| 7 | A faster sparse solver behind a backend interface: MKL PARDISO complex symmetric on Windows (ctypes on the `mkl` wheel, as pypardiso does for real matrices; pypardiso itself has no complex support), Apple Accelerate on macOS 15.5+ (complex LU / LDLᵀ, reusable symbolic analysis, multithreaded), PyAMG for the resistive and inductive models (already installed with Thermal); SuperLU stays the fallback | rounding (multithreaded order: reproducibility to keep or note) | L, new dependency | −10-30 % of what is left estimated first; after 4.2 **−30-50 % of the board-wide work** if factorisations get 3-5x and triangular solves 2x faster; half the factor memory, analysis once per model, exact memory planning, no 32-bit limit | M3 |

Expected: M1 → A ~30-50 min, B ~22-35 min with ≥ 16 GB free (the M2 at ~4 GB free ~60-85 / ~38-50 min); M1+M2 → A
~25-40, B ~18-30 min; M3 a further 10-30 % of all, 30-50 % of the board-wide work (4.2). To be checked against
4.3 and re-ranked before M1 starts.

### 5.1 Re-ranked with the measurements (M0, 2026-10-24)

Baseline on the M2 at 8x, measured: **job A 974 s**, **job B 4 520 s** (4.3, 4.4).

| # | Change | What the measurement says | Est. saving A / B | Milestone |
|---|---|---|---|---|
| 1 | Memory-true accounting and per-round thread planning (as #1 above), and no cache-freeing where the memory is really there | both runs single-threaded (15-16 % CPU); job B 16 repeat factorisations (~180 s) | −30-40 % / −30-45 % with two solves at once | M1 |
| 2 | Less memory per solve, so that #1 can run two or three at once on the 24 GB Mac: the solutions' dense grids, the cached factorisations of large models, models whose runs are done | one solve keeps 13-15 GB | (makes #1's saving possible on the M2) | M1 |
| 3 | A faster Gram-Schmidt (nogil, parallel over the basis; memory-bound zgemv reaches ~20-25 GB/s on one core of a ~100 GB/s machine) | ~32 % of the iterations' time | −12-18 % | M1 |
| 4 | GMRES stops on the true residual when its estimate first meets the tolerance | — | −3-8 % | M1 |
| 7 | Faster sparse solver (PARDISO / Accelerate / PyAMG behind a backend) | SuperLU's triangular solves ~45 % of the iterations' time, factorisations ~11 % | −25-35 % of what is left | M3 |
| 2 (old) | DC paths as a resistive network | the DC model is 1 % of job B's time; only its estimate matters (fixed by #1) | ~1 % | dropped |
| 5, 6 | Lock-step solves; fewer factorisations beyond the repeats | 4.2: k columns save 9-26 % of a SuperLU solve; the remaining factorisations ~8 % | −2-6 % | deferred |

Expected, compounded: job A 974 s → ~650 s (#1-2) → ~530 s (#3-4) → **~370-420 s with M3 (6-7 min)**; job B 4 520 s →
~2 800 s → ~2 300 s → **~1 550-1 750 s with M3 (26-29 min)**. On a machine with more free memory (the Dell) #1
goes further (three or four solves at once).

### 5.2 M1 as built (branch `speed-m1`, 2026-10-25)

- **#1 Memory-true accounting.** `peec.lu_commit()`: SuperLU's factor is Windows' commit (6.5x the fill, [A38])
  only on Windows; macOS and Linux count resident pages, at most ~2x the fill while an array grows (`LU_RESIDENT`,
  [A67]). `simulate.basis_rows`: a solve plans the basis rows its model's most iterations so far need (x1.5, in the
  basis's doubling steps; `PlaneStack.its_max`), 16 rows for a DC solve before its model was measured, the full
  restart length otherwise. Each round's solves are admitted by memory (`simulate.Admission` in `run_parallel`):
  each plans `solve_bytes` at its frequency against 80 % of the memory available when the round starts, the
  largest first, on the cores' threads; a solve that does not fit waits, one larger than everything runs alone.
  Before, every round ran on `memory_threads` of the largest model of the whole run (both 8x jobs: one at a time).
  The statistics keep per round how many ran at once (`at_once`; `run_stats.py` prints it).
- **#2 / #3 Less memory per solve, faster Gram-Schmidt: the Krylov basis in single precision.** Two float32
  planes; every product with them in float64 (`solver/_mac.gs_project`, `gs_combine`: numba, nogil, in column
  blocks; mixed-precision GMRES [Lindquist 2020] [Aliaga 2023], [A66]). Half the basis memory (a 2 M-unknown route
  model's 101 rows: 3.2 → 1.6 GB per solve) and half the bytes per pass. On this laptop a 64-row pass over 1 M
  unknowns: 87-97 ms single-threaded against `zgemv` on the complex128 basis 103 ms on one core, 90 ms on four;
  the laptop's memory bus (~20 GB/s) is already full with one core, the M2's (~100 GB/s; `zgemv` reached ~32 GB/s
  there) is not. Iterations as with complex128 down to 1e-7 (tests/test_speed.py), 3x more at 1e-8: used for
  tolerances ≥ 1e-6 (`BASIS32_MIN_TOL`; the solver's default 1e-5), complex128 below. Splitting `zgemv` over Python
  threads was 4x slower (strided copies); interleaved complex64 in numba 3x slower than BLAS (no vectorisation).
- **#4 The true-residual stop.** `_gmres` checks the true residual when the recurrence's first meets the tolerance
  and stops there (it went on to half the tolerance first): 3-4 fewer iterations per solve on refboard.

Measured on this laptop (refboard, 1x, `main` 9716e21 against the branch, one after the other; `out/speed/m1/`):

| Job | main | speed-m1 | Results |
|---|---|---|---|
| A, the six nets | 246 s | 215 s (−13 %) | largest difference 0.022 dB (SW_CLK's emissions; its route solve 32 → 29 iterations); the same 40 issues |
| B, the four converters | 1 074 s | 905 s (−16 %) | conducted within 0.005 dB, the issues' margins within 0.0003 dB |

The laptop (a few GB available beside its other programs) runs its large solves one at a time either way: these are
mostly #3 and #4. The 0.022 dB
is the size of a solve stopping at the tolerance instead of half of it (the same as `main` showed for an equally
valid factorisation choice, ARCHITECTURE "Memory use"). Job B's issue list differs only in the voltage clearance
check, which does not use the solver: the same pairs named the other way round, one of +VDC's three pairs folded
into another (an iteration order between processes; noted in HANDOFF). `result_diff.py` reads `f_3db` as a dB value
and matches issues by position: its "680 dB" lines are 1e-6 relative and a reordering.

**On the M2** (2026-10-25, build 0.1.42747 from 113ae1c, the same board and settings as 4.3):

| Job | M0 (4.3) | M1 | What changed |
|---|---|---|---|
| A, the six nets (`runs/2026-10-25_07-46-51`) | 974 s | **652 s (−33 %)** | the 18 board-wide solves 3 at once (721 → 438 s; 226 → 314 ms per iteration side by side); the largest solve planned at 3.3 GB (8.0); peak 13.3 → 9.8 GB; no cached factorisation freed (4); 3 730 → 3 487 iterations; CPU 16 → 26 % |
| B, the four converters (`runs/2026-10-25_08-48-10`) | 4 520 s | **3 512 s (−22 %)** | up to 3 solves at once; no cached factorisation freed (74, 16 of them made again); 14 243 → 12 781 iterations; board-wide rounds 2 194 s (63 %): SuperLU 47 %, Gram-Schmidt 31 % of an iteration |

Job A ran with 10.1 GB available at its start (15.8 in 4.3), job B with 17.9 GB; job B swapped in 4.1 GB (the
Mac's free memory fell to 4.4 GB; the process peaked at 11.6 GB).

What job B still loses: **13 groups** (`solve_groups`) solved one after the other, as before. They are planned once,
before any model is factorised, with 4 threads x the largest run's estimate in flight (the DC model's, 8.7 GB from
`FILL_PER_UNKNOWN` against the ~2.6 GB it takes once measured). With admission in each round that in-flight term
is one solve, not four. The groups make the three converters' conducted bands refine one stage at a time: 18 rounds
of a single solve, ~470 s. Estimated with the groups planned for one solve in flight: job B −300 to −400 s, job A
−50 to −80 s (its four route rounds of one solve would be one).

## 6. Researched, not planned (the user's choice, 2026-10-24)

- **Board-wide model capped at 4x** (a setting, default min(resolution, 4)): −25-45 % at 8x, −60 % and more at 16x; 8x
  and 16x numbers move within the documented grid error.
- **Reusing unchanged solves across runs** (a cache keyed by the solved system's own arrays, right-hand side, frequency
  and the solver code): a re-run like 10-24 12:57 (2 h, the same iterations as 10:15) would take minutes.
- **Krylov and preconditioner research**: a complex64 Krylov basis (half the basis memory and Gram-Schmidt traffic;
  compressed-basis GMRES), deflated restarts (GMRES-DR), recycling a subspace across frequencies and right-hand sides
  (GCRO-DR [Parks 2006]; SciPy's `gcrotmk` reuses one), near-field inductance in the preconditioner: −20-50 % of the
  iterations if it works, and the 16x failures.
- From PyPEEC (section 7): another FFT library (pyFFTW, mkl_fft) where the FFTs dominate; processes instead of threads
  for the frequency solves (no GIL); stopping a solve once its output (PyPEEC: the complex power; here the loop
  moment) has converged.

## 7. Comparison: PyPEEC and FFT-PEEC

PyPEEC [Guillod 2025] (open source, Python) solves 3D quasi-magnetostatic PEEC problems on voxels with the same
ingredients as GroundTruth: a regular grid, so the inductance matrix is block-Toeplitz and its product an FFT
convolution of circulant embeddings [Lee 1986]; a sparse preconditioner from the **Schur complement** (its
`factorization_options: schur: true`), factorised by SuperLU ("always available but typically slower"), PARDISO
through Pydiso ("typically faster") or PyAMG ("slow but uses less memory, with some risk of convergence issues");
GMRES or GCROT; FFTs by SciPy, FFTW, MKL or CuPy (GPU); frequency sweeps in parallel processes; an optional stop
once the complex power has converged; a condition-number check. Its paper reports 10⁷ unknowns in 80 s (a 4 mm²
air-core inductor at 40.68 MHz, 32-core EPYC) and, for eddy-current problems, over 70 % of the time in the FFT
products. What it leaves out: hierarchical matrices and model-order reduction.

Differences that matter here: GroundTruth's board-wide model has plane-pair capacitance (cavity resonances; the
`sheet` blocks keep each pair's cavity inductance in the preconditioner), which makes many more iterations near
resonances than an inductor needs; and on the board-wide model SuperLU, not the FFTs, is the largest cost (its
triangular solves ~33-39 % of an iteration at 1x and 4x, a factorisation worth ~137 iterations at 4x: 4.1, 4.2),
while the route models are FFT-bound like PyPEEC's. So the order here is: parallel solves first (#1), then the
sparse solver (#7, as PyPEEC's PARDISO), then the FFT library for the local models if 4.3 confirms it at 8x.

FFT-PEEC [Torchio 2021] extends FFT-accelerated PEEC to capacitive and inductive effects together with "an ad hoc
preconditioner"; Romano et al. [Romano 2023] propose a surface-impedance PEEC formulation and "a novel
preconditioner" that extend FFT-accelerated PEEC to PCB power electronics up to the GHz range. Both are the closest
published relatives of the board-wide model; their preconditioners are the starting point if the Krylov research
(section 6) is taken up (the full texts were not reachable from here: both repositories refused the download).

## 8. How to measure

```
.venv/Scripts/python tools/dev/run_stats.py <board>/groundtruth/history/runs/<time>     # a kept run's statistics
.venv/Scripts/python tools/dev/profile_sim.py BOARD SETTINGS --resolution 4 --no-profile --json OUT.json
.venv/Scripts/python tools/dev/solver_bench.py BOARD SETTINGS --only NET --models board-wide,route --resolution 4
.venv/Scripts/python tools/dev/export_schur.py BOARD SETTINGS OUT_DIR --only "buck conv1" --models conducted,DC
```

## Sources

- [Guillod 2025] T. Guillod, C. R. Sullivan, "PyPEEC: A 3D quasi-magnetostatic solver using an FFT-accelerated PEEC
  method with voxelization," *Journal of Open Source Software*, vol. 10, no. 113, 6644, 2025. doi:10.21105/joss.06644.
  Solver options: github.com/otvam/pypeec, examples/config/tolerance.yaml.
- [Torchio 2021] R. Torchio, F. Lucchini, J.-L. Schanen, O. Chadebec, G. Meunier, "FFT-PEEC: A fast tool from CAD to
  power electronics simulations," *IEEE Trans. Power Electron.*, vol. 37, no. 1, pp. 700–713, 2022 (online 2021).
  doi:10.1109/TPEL.2021.3092431.
- [Romano 2023] D. Romano, I. Kovacevic-Badstuebner, G. Antonini, U. Grossner, "Efficient PEEC iterative solver for
  power electronic applications," *IEEE Trans. Electromagn. Compat.*, vol. 65, no. 2, pp. 546–554, 2023.
  doi:10.1109/TEMC.2023.3238394.
- [Lee 1986] D. Lee, "Fast multiplication of a recursive block Toeplitz matrix by a vector and its application,"
  *J. Complexity*, vol. 2, no. 4, pp. 295–305, 1986.
- [Parks 2006] M. L. Parks, E. de Sturler, G. Mackey, D. D. Johnson, S. Maiti, "Recycling Krylov subspaces for
  sequences of linear systems," *SIAM J. Sci. Comput.*, vol. 28, no. 5, pp. 1651–1674, 2006.
- Apple, Accelerate, Sparse Solvers: `SparseFactor` for complex double matrices and `SparseFactorizationLU`, macOS 15.5
  and later (developer.apple.com/documentation/accelerate).
- pypardiso (PyPI): "doesn't currently support complex numbers"; MKL PARDISO itself solves complex symmetric systems
  (matrix type 6).
- [Saad 2003], [Benzi 2005], [Demmel 1999], [A38]: docs/REFERENCES.md.
