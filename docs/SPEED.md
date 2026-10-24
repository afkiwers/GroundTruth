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

### 4.3 The M2 at 8x (jobs A and B with the statistics)

To be filled once the M2 runs the `speed-stats` build (`out/dist/groundtruth-0.1.42665-477ef5a-pcm.zip`).

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
| 6 | Fewer inductive-model factorisations: a borrowed preconditioner with D(f) at the solve frequency and a rescaled Schur solve, the window widened as far as measured iterations allow; deterministic sharing within a round | ≤ tol | M | −3-8 % / −5-15 % | M2 |
| 7 | A faster sparse solver behind a backend interface: MKL PARDISO complex symmetric on Windows (ctypes on the `mkl` wheel, as pypardiso does for real matrices; pypardiso itself has no complex support), Apple Accelerate on macOS 15.5+ (complex LU / LDLᵀ, reusable symbolic analysis, multithreaded), PyAMG for the resistive and inductive models (already installed with Thermal); SuperLU stays the fallback | rounding (multithreaded order: reproducibility to keep or note) | L, new dependency | −10-30 % of what is left estimated first; after 4.2 **−30-50 % of the board-wide work** if factorisations get 3-5x and triangular solves 2x faster; half the factor memory, analysis once per model, exact memory planning, no 32-bit limit | M3 |

Expected: M1 → A ~30-50 min, B ~22-35 min with ≥ 16 GB free (the M2 at ~4 GB free ~60-85 / ~38-50 min); M1+M2 → A
~25-40, B ~18-30 min; M3 a further 10-30 % of all, 30-50 % of the board-wide work (4.2). To be checked against
4.3 and re-ranked before M1 starts.

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
