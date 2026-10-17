"""Simulation results: saved as one .npz (arrays) with an embedded JSON summary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .raster import Grid


@dataclass
class ElectricalMap:
    """One net's DC solution on one copper layer: potential, current density (magnitude and components) and
    Joule heat per cell."""
    net: str
    layer: str
    grid: Grid
    V: np.ndarray
    J: np.ndarray
    Jx: np.ndarray
    Jy: np.ndarray
    P: np.ndarray


@dataclass
class Patch:
    """A refined window of the thermal mesh: cells of ``grid`` are ``level`` halvings of the base cell."""
    level: int
    grid: Grid
    T: np.ndarray  # (n_copper_layers, ny, nx), NaN where the window has no cell of this level
    Q: np.ndarray


@dataclass
class Results:
    """A solve's results: the temperature of every slab of the stack and the heat put into each copper layer on
    the base grid, the refined patches, and the electrical maps."""
    summary: dict
    grid: Grid
    slabs: list[dict]  # name, kind, z_bot, z_top, layer
    T: np.ndarray  # (n_slabs, ny, nx)
    Q: np.ndarray  # (n_copper_layers, ny, nx) heat input W/mm²
    copper_layers: list[str]
    emaps: list[ElectricalMap] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)  # finer cells over T and Q, coarsest first

    def copper_slab(self, layer: str) -> int:
        """The index of a copper layer's slab in T."""
        for i, s in enumerate(self.slabs):
            if s["kind"] == "copper" and s["layer"] == layer:
                return i
        raise KeyError(layer)

    def save(self, path: Path) -> None:
        """One .npz: the arrays by name, and everything else as JSON stored in a byte array ("meta"); written to a
        temporary file and renamed into place."""
        arrays = {"T": self.T, "Q": self.Q}
        meta = {
            "summary": self.summary,
            "grid": _grid_dict(self.grid),
            "slabs": self.slabs,
            "copper_layers": self.copper_layers,
            "emaps": [],
            "patches": [{"level": p.level, "grid": _grid_dict(p.grid)} for p in self.patches],
        }
        for k, p in enumerate(self.patches):
            arrays[f"p{k}_T"] = p.T
            arrays[f"p{k}_Q"] = p.Q
        for k, m in enumerate(self.emaps):
            meta["emaps"].append({"net": m.net, "layer": m.layer, "grid": _grid_dict(m.grid)})
            for name in ("V", "J", "Jx", "Jy", "P"):
                arrays[f"e{k}_{name}"] = getattr(m, name)
        arrays["meta"] = np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8)
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, **arrays)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Results":
        """Read results saved by save()."""
        with np.load(path) as z:
            meta = json.loads(bytes(z["meta"]).decode("utf-8"))
            emaps = []
            for k, em in enumerate(meta["emaps"]):
                emaps.append(ElectricalMap(em["net"], em["layer"], Grid(**em["grid"]),
                                           *(z[f"e{k}_{n}"] for n in ("V", "J", "Jx", "Jy", "P"))))
            patches = [Patch(pm["level"], Grid(**pm["grid"]), z[f"p{k}_T"], z[f"p{k}_Q"])
                       for k, pm in enumerate(meta.get("patches", []))]
            return cls(meta["summary"], Grid(**meta["grid"]), meta["slabs"], z["T"], z["Q"],
                       meta["copper_layers"], emaps, patches)

    def layer_fields(self, name: str, layer: str) -> list[tuple[np.ndarray, Grid]]:
        """[(array, grid)] of T or Q on a copper layer: the base map, then finer patches on top."""
        k = self.copper_layers.index(layer)
        base = self.T[self.copper_slab(layer)] if name == "T" else self.Q[k]
        return [(base, self.grid)] + [(getattr(p, name)[k], p.grid) for p in self.patches]


def _grid_dict(g: Grid) -> dict:
    """A grid's parameters for the JSON metadata."""
    return {"x0": g.x0, "y0": g.y0, "dx": g.dx, "nx": g.nx, "ny": g.ny}
