"""Simulation setup: what the user configures, saved next to the KiCad project.

The file is an *assembly*: an environment (ambient, convection, optional
enclosure) plus a list of boards. Each board carries its own placement, so
multi-board assemblies can be added without changing the format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

PROJECT_SUFFIX = ".pcbsim.json"
FORMAT_VERSION = 1


@dataclass
class Terminal:
    """A group of pads tied together externally (wire, connector, busbar)."""
    name: str
    pads: list[str] = field(default_factory=list)  # pad keys "REF.NUM"
    kind: str = "source"  # "source" (drives current) | "sink" (0 V return)
    current: float = 1.0  # A, sources only


@dataclass
class Link:
    """Lumped resistance between two pads: shunt, fuse, MOSFET Rds(on), 0R link."""
    name: str
    pad_a: str
    pad_b: str
    resistance: float = 0.001  # ohm


@dataclass
class ComponentPower:
    """A part's dissipation, put into the board under its pads (through theta_jb when known: junction
    temperature)."""
    ref: str
    power: float = 0.0  # W dissipated in the component body
    theta_jb: float = 0.0  # K/W junction-to-board, 0 = unknown


@dataclass
class Mount:
    """Heat path from pads (mounting holes, standoffs) to the chassis."""
    name: str
    pads: list[str] = field(default_factory=list)
    resistance: float = 10.0  # K/W total


@dataclass
class Enclosure:
    """A closed box around the board: the air inside it, its walls conduct, and its outside convects and
    radiates to the ambient."""
    enabled: bool = False
    length: float = 120.0  # inner dimensions, mm
    width: float = 80.0
    height: float = 30.0
    wall_thickness: float = 2.0  # mm
    wall_material: str = "Aluminium"
    emissivity_inside: float = 0.8
    emissivity_outside: float = 0.8


@dataclass
class Environment:
    """Where the board cools: ambient temperature, convection (natural correlations, forced air or fixed
    coefficients), radiation with the faces' emissivities, an optional enclosure."""
    ambient_c: float = 25.0
    orientation: str = "horizontal"  # horizontal | vertical
    convection: str = "natural"  # natural | forced | fixed
    airspeed: float = 1.0  # m/s, forced
    h_top: float = 10.0  # W/m^2K, fixed
    h_bottom: float = 5.0
    radiation: bool = True
    emissivity_mask: float = 0.9
    emissivity_copper: float = 0.1
    enclosure: Enclosure = field(default_factory=Enclosure)


@dataclass
class MeshSettings:
    """Cell sizes (0: chosen from the quality) and the electro-thermal loop's limits."""
    quality: str = "normal"  # draft | normal | fine
    thermal_cell_mm: float = 0.0  # 0 = automatic
    electrical_cell_mm: float = 0.0  # 0 = automatic
    adaptive: bool = True  # refine the thermal mesh where heat enters through small features
    max_iterations: int = 30
    tolerance_c: float = 0.02


@dataclass
class LayerMaterial:
    """A dielectric layer's material, with optional conductivities replacing the library's."""
    name: str  # dielectric layer name from the stackup
    material: str = "FR4"
    k_xy: float = 0.0  # 0 = from material library
    k_z: float = 0.0


@dataclass
class BoardMaterials:
    """The board's material choices: via plating, temperature-dependent copper resistance, copper thicknesses
    and dielectrics other than the stackup says."""
    plating_um: float = 25.0  # via / PTH barrel plating thickness
    temperature_dependent_resistance: bool = True
    copper_thickness_override: dict[str, float] = field(default_factory=dict)  # layer -> mm
    dielectrics: list[LayerMaterial] = field(default_factory=list)


@dataclass
class Placement:
    """Where a board sits in the assembly (one board for now)."""
    x: float = 0.0  # mm, reserved for multi-board assemblies
    y: float = 0.0
    z: float = 0.0
    rotation: float = 0.0
    flipped: bool = False


@dataclass
class BoardSetup:
    """One board of the assembly: its file, how current enters and leaves it (terminals, links), what heats it
    besides its copper (component power), how it is mounted, its materials and its mesh."""
    board_file: str  # relative to the project file
    placement: Placement = field(default_factory=Placement)
    terminals: list[Terminal] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    components: list[ComponentPower] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)
    materials: BoardMaterials = field(default_factory=BoardMaterials)
    mesh: MeshSettings = field(default_factory=MeshSettings)

    def component(self, ref: str) -> ComponentPower | None:
        """The part's power entry, None without one."""
        for c in self.components:
            if c.ref == ref:
                return c
        return None

    def pad_role(self, key: str) -> tuple[str, str] | None:
        """Return (kind, name) for a pad that belongs to a terminal/link/mount."""
        for t in self.terminals:
            if key in t.pads:
                return t.kind, t.name
        for l in self.links:
            if key in (l.pad_a, l.pad_b):
                return "link", l.name
        for m in self.mounts:
            if key in m.pads:
                return "mount", m.name
        return None


@dataclass
class Project:
    """The whole setup file: the environment and the boards."""
    environment: Environment = field(default_factory=Environment)
    boards: list[BoardSetup] = field(default_factory=list)
    version: int = FORMAT_VERSION

    # ---- persistence ----------------------------------------------------------
    def to_json(self) -> str:
        """The setup as indented JSON."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Project":
        """A setup from JSON (unknown keys ignored, missing ones at their defaults: older files load)."""
        return _from_dict(cls, json.loads(text))

    def save(self, path: Path) -> None:
        """Write atomically: to a temporary file, then renamed over the old one (never half a file)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Project":
        """Read a setup file."""
        return cls.from_json(path.read_text(encoding="utf-8"))


def _from_dict(cls, data):
    """Build a dataclass tree from plain dicts, ignoring unknown keys."""
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _convert(hints[f.name], data[f.name])
    return cls(**kwargs)


def _convert(tp, v):
    """A JSON value as type tp (from the dataclass's type hints): lists and nested dataclasses recursively, ints
    to floats where a float is declared."""
    origin = get_origin(tp)
    if origin is list:
        (inner,) = get_args(tp)
        return [_convert(inner, x) for x in (v or [])]
    if origin is dict:
        return dict(v or {})
    if is_dataclass(tp):
        return _from_dict(tp, v)
    if tp is float and isinstance(v, (int, float)):
        return float(v)
    return v
