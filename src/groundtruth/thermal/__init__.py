"""Electro-thermal solver, from the user's PCB Thermal app (package `pcbsim`, its solver without the Qt UI).

The modules are PCB Thermal's own (kicad/board.py: its board model; solver/: rasterisation, adaptive mesh,
electrical and thermal models, convection, the coupled loop; materials.py; project.py: the setup data model),
with their analytic validation tests in tests/thermal/. GroundTruth drives the thermal model with its own DC path
currents (analysis/thermal.py; docs/THERMAL_PLAN.md, option A) on a board adapted from GroundTruth's model
(thermal/adapter.py). Needs the [thermal] extra: numba, pyamg, psutil.
"""

__version__ = "0.1.0"
