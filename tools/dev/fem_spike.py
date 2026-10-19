"""FEM feasibility spike (docs/FEM_OPTION.md), not part of GroundTruth: a 3D eddy-current solve (A formulation,
lowest-order Nedelec elements, NGSolve) of a closed trace loop 0.5 mm over a 35 um copper plane. The plane's
induced current under one long side of the loop is its return current; it is compared with image theory
K(y) = I/pi * h/(h^2 + y^2) [Johnson 1993], as tests/test_solver.py does for the PEEC solver. Prints the mesh size,
the unknowns, the time and the memory.

NGSolve is not a GroundTruth dependency: run this in a venv of its own (about 800 MB with MKL), never the project's
or KiCad's:

    py -3.13 -m venv %TEMP%\\femvenv
    %TEMP%\\femvenv\\Scripts\\python -m pip install ngsolve psutil mkl      # mkl: the PARDISO direct solver
    %TEMP%\\femvenv\\Scripts\\python tools/dev/fem_spike.py 1e6 1.0 pardiso

Arguments: frequency (Hz), the plane's mesh size (mm), "pardiso" for MKL's PARDISO (else NGSolve's sparse
Cholesky, which breaks down on this complex, nearly singular system: NaN).

Measured 2026-10-19 on the 16 GB laptop (laptop), 1 MHz: plane mesh 1.0 mm -> 294 226 tets, 345 148 unknowns, mesh
43-103 s, PARDISO 58-145 s, 6.7-6.8 GB, L = 42.9 nH (hand estimate ~48 nH), return profile 22 % off image theory;
0.5 mm -> 450 835 tets, 528 396 unknowns, mesh 178 s, the solve did not finish in 40 min."""
import math
import sys
import time

import numpy as np
import psutil
from netgen.occ import Box, Glue, OCCGeometry, Pnt
from ngsolve import (BilinearForm, CoefficientFunction, GridFunction, HCurl, Integrate, LinearForm, Mesh, Norm,
                     SetNumThreads, TaskManager, curl, dx, ngsglobals)

ngsglobals.msg_level = 0
f = float(sys.argv[1]) if len(sys.argv) > 1 else 1e6
plane_h = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
mm = 1e-3
t = 0.035 * mm              # the plane's copper
h = 0.5 * mm                # the trace's height above the plane's top
w, tt = 0.2 * mm, 0.035 * mm
L, Wl = 30 * mm, 10 * mm    # the loop: long sides along x at y = +-5 mm
mu0, sigma = 4e-7 * math.pi, 5.8e7
t0 = time.time()

air = Box(Pnt(-35 * mm, -25 * mm, -12 * mm), Pnt(35 * mm, 25 * mm, 12 * mm))
air.mat("air")
# only the outer box's faces get the boundary condition: every OCC face is named "default", the copper/air
# interfaces too, and dirichlet="default" pinned A = 0 on the copper (no induced current at all)
air.faces.name = "outer"
plane = Box(Pnt(-25 * mm, -15 * mm, -t), Pnt(25 * mm, 15 * mm, 0))
plane.mat("cu")
plane.maxh = plane_h * mm
# the loop: four bars carrying an impressed current (no conductivity)
z0, z1 = h, h + tt
bars = [Box(Pnt(-L / 2, Wl / 2 - w / 2, z0), Pnt(L / 2, Wl / 2 + w / 2, z1)),
        Box(Pnt(-L / 2, -Wl / 2 - w / 2, z0), Pnt(L / 2, -Wl / 2 + w / 2, z1)),
        Box(Pnt(-L / 2 - w, -Wl / 2 - w / 2, z0), Pnt(-L / 2, Wl / 2 + w / 2, z1)),
        Box(Pnt(L / 2, -Wl / 2 - w / 2, z0), Pnt(L / 2 + w, Wl / 2 + w / 2, z1))]
for k, b in enumerate(bars):
    b.mat(f"src{k}")
    b.maxh = 0.1 * mm
geo = Glue([air - plane - sum(bars[1:], bars[0]), plane] + bars)
mesh = Mesh(OCCGeometry(geo).GenerateMesh(maxh=6 * mm, grading=0.25))
t_mesh = time.time() - t0

# impressed current density (A/m^2) for 1 A: +x on the y = +5 bar, -x on the y = -5 bar, closing along y
J = 1.0 / (w * tt)
src = {"src0": (J, 0, 0), "src1": (-J, 0, 0), "src2": (0, -J, 0), "src3": (0, J, 0)}
J0 = mesh.MaterialCF({k: CoefficientFunction(v) for k, v in src.items()}, default=CoefficientFunction((0, 0, 0)))
sig = mesh.MaterialCF({"cu": sigma}, default=0)
omega = 2 * math.pi * f

# curl(1/mu0 curl A) + j w sigma A = J0, with a small regularisation in the air (the gauge)
fes = HCurl(mesh, order=1, complex=True, nograds=True, dirichlet="outer")
A, v = fes.TnT()
a = BilinearForm(fes, symmetric=True)
a += 1 / mu0 * curl(A) * curl(v) * dx + 1j * omega * sig * A * v * dx + 1e-6 / mu0 * A * v * dx
lf = LinearForm(fes)
lf += J0 * v * dx
gfA = GridFunction(fes)
SetNumThreads(8)
with TaskManager():
    a.Assemble()
    lf.Assemble()
    t1 = time.time()
    inv = a.mat.Inverse(fes.FreeDofs(), inverse="pardiso" if "pardiso" in sys.argv else "sparsecholesky")
    gfA.vec.data = inv * lf.vec
    t_solve = time.time() - t1
# L from the field energy (2W/I^2), R from the plane's loss. Not from integral(J0 . A): the bars' corners leave the
# impressed current slightly divergent, which puts a large gradient into A that the energy does not see (it gave
# 6e12 nH)
Lm = Integrate(Norm(curl(gfA)) ** 2 / mu0, mesh)
Pl = Integrate(sig * omega ** 2 * Norm(gfA) ** 2, mesh, definedon=mesh.Materials("cu"))
Jind = -1j * omega * sig * gfA                       # the induced current density in the plane
# the return profile across y under the y = +5 mm side, at x = 0: K(y) = integral of Jx through the copper (A/m)
ys = np.linspace(Wl / 2 - 4 * mm, Wl / 2 + 4 * mm, 33)
zs = np.linspace(-t + t / 8, -t / 8, 4)
K = np.array([abs(np.mean([Jind(mesh(0.0, yy, zz))[0] for zz in zs])) * t for yy in ys])
dy = ys - Wl / 2
heff = h + tt / 2 + t / 2                            # image distance: the trace's and the plane's mid-planes
K_img = 1 / math.pi * heff / (heff ** 2 + dy ** 2)
core = np.abs(dy) <= 2 * heff
err = np.max(np.abs(K[core] - K_img[core]) / K_img.max())
print(f"f={f:.3g} Hz plane_maxh={plane_h} mm: {mesh.ne} tets, {fes.ndof} dofs ({sum(fes.FreeDofs())} free); "
      f"mesh {t_mesh:.1f} s, solve {t_solve:.1f} s, RSS {psutil.Process().memory_info().rss / 2**30:.2f} GB")
print(f"  loop L = {Lm * 1e9:.2f} nH, R = {Pl * 1e3:.3f} mOhm")
print(f"  return profile under the side vs image theory: peak K {K.max():.0f} vs {K_img.max():.0f} A/m, "
      f"max deviation within +-2h {err * 100:.1f} % of the peak")
