"""Physical layer stack (thicknesses, z positions, material properties)."""

from __future__ import annotations

from dataclasses import dataclass

from ..kicad.board import Board
from ..materials import COPPER, dielectric_for
from ..project import BoardMaterials


@dataclass
class Layer:
    """One layer of the physical stack with its z extent and thermal properties."""
    name: str
    kind: str  # copper | dielectric
    t: float  # mm
    z_bot: float  # mm from board bottom
    z_top: float
    k_xy: float
    k_z: float
    rho_cp: float
    material: str

    @property
    def z_mid(self) -> float:
        """The layer's centre height."""
        return 0.5 * (self.z_bot + self.z_top)


def build_stack(board: Board, mats: BoardMaterials) -> list[Layer]:
    """Layers ordered top (F.Cu) to bottom, z measured up from the board bottom."""
    overrides = {d.name: d for d in mats.dielectrics}
    layers: list[Layer] = []
    for sl in board.stackup:
        if sl.kind == "copper":
            t = mats.copper_thickness_override.get(sl.name, sl.thickness)
            layers.append(Layer(sl.name, "copper", t, 0, 0, COPPER.k, COPPER.k, COPPER.rho_cp, "Copper"))
        else:
            ov = overrides.get(sl.name)
            mat_name = ov.material if ov else sl.material
            d = dielectric_for(mat_name)
            kxy = ov.k_xy if ov and ov.k_xy > 0 else d.k_xy
            kz = ov.k_z if ov and ov.k_z > 0 else d.k_z
            layers.append(Layer(sl.name, "dielectric", sl.thickness, 0, 0, kxy, kz, d.rho_cp, d.name))
    # z from the bottom up: walk the layers from B.Cu upwards, stacking their thicknesses
    z = 0.0
    for l in reversed(layers):
        l.z_bot = z
        z += l.t
        l.z_top = z
    return layers


def copper_z(stack: list[Layer]) -> dict[str, float]:
    """{copper layer: its centre height}."""
    return {l.name: l.z_mid for l in stack if l.kind == "copper"}
