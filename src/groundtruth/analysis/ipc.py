"""IPC-2221 current capacity (the conservative older chart fit, as PCB Thermal uses it).

I = k · ΔT^0.44 · A^0.725, A in mil², internal conductors k = 0.024. A via barrel is an internal conductor of
cross-section π·(d + t)·t (d the drill, t the plating: the barrel's mean circumference times its wall)."""
from __future__ import annotations

import math

MIL_MM = 0.0254
K_INTERNAL = 0.024


def via_capacity(drill_mm: float, plating_um: float = 25.0, rise_c: float = 10.0) -> float:
    """Current (A) a via barrel carries for a `rise_c` K temperature rise."""
    t_mm = plating_um * 1e-3
    # the barrel's wall as a conductor: mean circumference pi (d + t) times wall thickness t, in square mils
    area_mil2 = math.pi * (drill_mm + t_mm) / MIL_MM * (t_mm / MIL_MM)
    return K_INTERNAL * rise_c ** 0.44 * area_mil2 ** 0.725
