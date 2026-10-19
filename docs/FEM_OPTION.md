# A FEM solver beside PEEC: findings and a plan for later

**Status (2026-10-19): investigated, not planned.** The user asked for "a pathway using FEM to analyse currents
etc.", with users able to switch between the current solver and a FEM mode, and for its benefits, work and
drawbacks. After the findings below: "I don't think it's worth right now but might be in the future." This file
keeps what was learned so the work can start from here if it is picked up again.

## Summary

- For the core job, the return currents in the copper layers at quasi-static frequencies, the existing PEEC solver
  is the better-matched method. It discretises only the copper (no air), and its inductance products are FFT
  convolutions on one grid. A 3D FEM solve of the same physics was about 100x slower and needed gigabytes where
  PEEC needs seconds (the spike below).
- FEM pays off in three places PEEC can't easily reach:
  1. **3D detail in small regions**: via barrels as tubes, copper thickness, a package's height, skin and proximity
     effect in 3D, magnetic cores and shields. This is the hot loop of a power stage.
  2. **Full-wave effects**: retardation, radiation loss at the plane edges, cables and enclosures, i.e. the
     assumptions A1, A7 and A11 (docs/REFERENCES.md).
  3. **Geometry that follows the copper exactly**: no rasterisation onto a grid, so none of the neck scatter or grid
     dependence of SP-19 (docs/VALIDATION_AUDIT.md).
- The cheapest worthwhile first step would be a FEM "detail mode" for power stages only, run on the compute servers,
  with a mode that shows FEM and PEEC side by side.

## What PEEC does now, and where a FEM backend would plug in

`solver/peec.py` is a quasi-static PEEC solver [Ruehli 1974]: every copper layer rasterised on one xy grid, strip
currents and node potentials as unknowns, partial inductances as FFT convolution kernels, plane-pair capacitance,
vias and parts as lumped branches, right-preconditioned GMRES with a sparse Schur-complement preconditioner
(SuperLU). See the module docstring and ARCHITECTURE.md.

Everything after the solve (heatmaps, transfers, emission, issues, the report, History) reads one structure,
`peec.Solution`: per-layer strip currents `I_x`, `I_y`, node potentials `V`, branch currents, the loop impedance
`Z_loop` and the dipole moment `m`, on the layers' grids. A FEM backend that returns a `Solution` resampled onto
the same grids leaves all of that unchanged. That is the seam.

What would not carry over without extra work:
- Suggest fixes evaluates its candidates as low-rank (Woodbury) updates of PEEC factorisations.
- Warm starts, the GPU path and "Sharper map" are PEEC-specific.
- The memory planning (`peec.GATE`, `simulate.MemoryRelief`) knows SuperLU's behaviour, not a FEM solver's.

## Findings

### Toolchains (checked 2026-10-19 with pip, for Windows win_amd64)

| Package | Python 3.11 (KiCad's) | Python 3.13 (venv) | License | Notes |
|---|---|---|---|---|
| NGSolve + Netgen (mesher, OCC geometry) | wheel | wheel | LGPL-2.1 | the most complete option; ~190 MB installed |
| Intel MKL (PARDISO direct solver for NGSolve) | wheel | wheel | Intel Simplified Software License | ~600 MB; NGSolve's `inverse="pardiso"` needs it |
| Gmsh | wheel | wheel | GPL-2.0-or-later | mesher only |
| scikit-fem | pure Python | pure Python | BSD-3 | small, assembly only; brings no fast 3D solver |
| pypardiso | pure Python (+ MKL) | same | MIT + MKL's | PARDISO for SciPy matrices |
| `mfem` on PyPI | 0.0.3 placeholder | same | – | not the real PyMFEM (Linux only) |
| pyelmer | wrapper | wrapper | – | needs Elmer installed separately |
| Palace, PETSc (petsc4py), MUMPS, FEniCSx | none | none | – | no Windows wheels |

So on Windows a FEM backend means NGSolve (with or without MKL), or scikit-fem with SciPy's solvers for 2D work.

### The spike (`tools/dev/fem_spike.py`)

A 3D eddy-current solve (A formulation, lowest-order Nédélec elements, NGSolve 6.2.2608): a closed trace loop
(30 x 10 mm, 0.2 mm wide) 0.5 mm over a 35 µm copper plane, at 1 MHz (skin depth 66 µm, so the copper is meshed as a
volume). The plane's induced current under one long side is the return current, compared with image theory
[Johnson 1993], as `tests/test_solver.py::test_return_current_profile_matches_image_theory` does for PEEC. Run on the
16 GB laptop (laptop), 8 threads:

| | Mesh | Unknowns | Time | Memory | Result |
|---|---|---|---|---|---|
| PEEC (the existing tests) | grid | – | 2.8 s (profile), 0.65 s (L) | small | profile within 6 % of image theory; L within 3 % of Wheeler |
| FEM, plane mesh 1.0 mm | 294 226 tets | 345 148 | mesh 43-103 s, PARDISO solve 58-145 s | 6.7 GB | L = 42.9 nH (hand estimate ~48 nH: two 30 mm sides at ~18 nH, the ends ~6 nH each); return profile 22 % off |
| FEM, plane mesh 0.5 mm | 450 835 tets | 528 396 | mesh 178 s, solve not finished in 40 min | > 16 GB (swapping) | – |

What it showed:
- **The physics works.** The return current gathers under the trace as image theory says, and L is about right.
  But matching PEEC's accuracy needs a plane mesh near h/5 = 0.1 mm under the trace, which is far beyond the
  0.5 mm case that already did not fit a 16 GB laptop with a direct solver.
- **The aspect ratio drives the cost.** Copper is 35 µm thick against boards 100 mm wide, and the air around it
  must be meshed too. Local refinement only where the current gathers, an iterative solver (BDDC/AMS) and thin-shell
  or surface-impedance copper are prerequisites, not refinements.
- **Skin depth.** Above ~5 MHz the skin depth is below the copper thickness: meshing it is out of the question at
  board scale, so the copper needs a surface-impedance boundary condition (SIBC) or a thin-shell formulation.
- **Pitfalls found** (kept in the script's comments):
  - Every OCC face is named "default", so `dirichlet="default"` also pins A = 0 on the copper/air interfaces.
  - NGSolve's `sparsecholesky` returns NaN on this complex, nearly singular system; PARDISO (MKL) works.
  - An impressed current that is slightly divergent (the loop's corners) puts a large gradient into A, so
    ∫J·A gave 6e12 nH. Take L from the field energy instead, or make the source discretely divergence-free.
- **Install size.** NGSolve + Netgen ~190 MB, MKL ~600 MB: too large to bundle with the KiCad plugin; it would be an
  optional add-on (as `tools/install_gpu.py` does for PyTorch), installed by default on the compute servers.

## Options

| Option | What it adds | Limits it would remove | Effort | Drawbacks |
|---|---|---|---|---|
| **A. 3D eddy-current FEM, small regions** | real via barrels, copper thickness, package height, skin and proximity in 3D, cores and shields; a power stage's hot-loop inductance from first principles | A2-A4, A30 (flat sheets, filament vias, a fixed package height) | 6-10 weeks | heavy: compute server first; meshing real KiCad geometry is the main risk |
| **B. 2.5D FEM per plane pair** (cavity / Helmholtz, as PDN tools do) | PDN impedance and board resonances on meshes that follow the copper, with edge-radiation loss | "resonance peaks are upper estimates", the ±4 % neck scatter, part of SP-19's ±2 dB above 1 GHz | 3-5 weeks | plane pairs only: no current distribution under a trace |
| **C. 3D full-wave FEM** (Nédélec, PML) | the radiated field directly; retardation, cables, enclosures: common mode | A1, A7, part of A11 | 3+ months | hours per frequency sweep on a server (needs model-order reduction); a large validation effort |
| **D. 2D FEM per layer, DC / low frequency** | DC and IR-drop maps without rasterisation | the raster error on narrow copper | 2-3 weeks | overlaps the thermal solver's DC solve and Sharper map; a modest gain |

The effort figures include the repo's rules: analytic validation tests for every new physics feature, and a
grid-convergence check on a real board. They are rough.

## A plan, if it is picked up

**Phase 0: the foundation (~1 week)**
- **License.** Make it GPL-3.0-or-later: the LICENSE file already is GPLv3, but `pyproject.toml` says MIT. GPL
  lets the plugin use NGSolve (LGPL), Gmsh (GPL) and scikit-fem (BSD), the most functionality (the user, 2026-10-19:
  "In terms of licensing, use the license that benefits the functionality most"). MKL keeps its own license: an
  optional, separately installed component, as PyTorch is. NGSolve's iterative solvers do not need it.
- **A solver backend interface** at the model-run level, `build(region, layers, branches, ports)` and
  `solve(f) -> Solution`, with PEEC as the first implementation.
- **The switch.** A Preferences choice **Solver: PEEC (fast) / FEM (3D detail) / Both (compare)**, recorded with
  each run (History warns when two compared runs used different solvers) and named on the report cover and on each
  view. "Both" shows the two results side by side: that is how users learn when to trust which.
- **`tools/install_fem.py`** for the optional add-on, like `tools/install_gpu.py`.

**Phase 1: a FEM detail mode for power stages (~6-10 weeks)** (option A, a stage's hot-loop region only)
- KiCad copper, pads, vias (tubes) and packages turned into OCC solids, meshed with local refinement where the
  current gathers.
- Excitation by the same switch current injections PEEC uses, so the two are directly comparable.
- An SIBC or thin-shell treatment of the copper above ~5 MHz; an iterative solver (BDDC / AMS) instead of a direct
  one.
- Results resampled to the layer grids as a `Solution`.
- Validation tests: microstrip against Wheeler, the return profile against image theory, a via against the coax
  formula, two-wire inductance.
- **Checkpoint:** refboard's conv1 and conv2 hot-loop inductance, FEM against PEEC, with time and memory on
  the compute server (server-pc, 62 GB). Decide on phase 2 from that.

**Phase 2 (after the checkpoint):** option B for PDN and resonances.
**Phase 3 (only if needed):** option C, if the emission figures are to become a compliance prediction rather than a
ranking of layouts.

**In FEM mode, at first:**
- Only power stages are solved with FEM. Nets, the board-wide model, DC paths and PDN stay PEEC, and each view says
  which solver made it.
- Suggest fixes keeps PEEC's low-rank candidate updates; FEM could verify the best candidates.
- Sharper map and the GPU path stay PEEC only.

## Drawbacks and risks
- 10-100x slower and memory-hungry: practical on the compute servers; on a laptop only for small regions.
- Meshing real boards (35 µm copper, slivers, dense via fields) is the largest technical risk; the spike used
  boxes, not KiCad geometry.
- Two solvers to maintain, which will disagree in places; the compare mode is what makes that useful rather than
  confusing.
- The optional add-on is ~800 MB with MKL.

## Running the spike again

`tools/dev/fem_spike.py` (its docstring has the setup). NGSolve goes into a venv of its own, never the project's or
KiCad's:

```sh
py -3.13 -m venv %TEMP%\femvenv
%TEMP%\femvenv\Scripts\python -m pip install ngsolve psutil mkl
%TEMP%\femvenv\Scripts\python tools/dev/fem_spike.py 1e6 1.0 pardiso    # frequency, plane mesh (mm), solver
```
