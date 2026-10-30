"""Write small synthetic KiCad 10 boards for tests (any copper layer count)."""
from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

Pt = tuple[float, float]


def _u() -> str:
    return str(_uuid.uuid4())


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def fracture(outline: Sequence[Pt], holes: Sequence[Sequence[Pt]]) -> list[Pt]:
    """Join holes to the outline with zero-width slits, like KiCad's filled_polygon."""
    pts = list(outline)
    for hole in holes:
        h = list(hole)[::-1]  # opposite winding
        # bridge from outline vertex 0 to the hole's vertex 0 and back
        pts = [pts[0], *h, h[0], *pts]
    return pts


@dataclass
class BoardGen:
    layers: int = 2
    size: Pt = (40.0, 20.0)
    dielectrics: Optional[list[float]] = None      # thickness between consecutive copper layers
    epsilon_r: float = 4.5
    items: list[str] = field(default_factory=list)
    cu_thickness: float = 0.035                     # every copper layer, mm
    mask: Optional[float] = None                    # solder mask thickness (mm) in the stackup; None: none written

    def __post_init__(self):
        n = self.layers
        self.cu = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]
        if self.dielectrics is None:
            self.dielectrics = [1.51] if n == 2 else [0.2] + [1.0 / max(n - 3, 1)] * (n - 3) + [0.2]
        assert len(self.dielectrics) == n - 1

    # ------------------------------------------------------------- primitives
    def track(self, start: Pt, end: Pt, net: str, layer: str = "F.Cu", width: float = 0.2) -> str:
        u = _u()
        self.items.append(f'\t(segment\n\t\t(start {start[0]} {start[1]})\n\t\t(end {end[0]} {end[1]})\n'
                          f'\t\t(width {width})\n\t\t(layer {_q(layer)})\n\t\t(net {_q(net)})\n\t\t(uuid {_q(u)})\n\t)')
        return u

    def polyline(self, pts: Sequence[Pt], net: str, layer: str = "F.Cu", width: float = 0.2) -> list[str]:
        return [self.track(a, b, net, layer, width) for a, b in zip(pts, pts[1:])]

    def via(self, at: Pt, net: str, layers: tuple[str, str] = ("F.Cu", "B.Cu"), size: float = 0.6,
            drill: float = 0.3) -> str:
        u = _u()
        self.items.append(f'\t(via\n\t\t(at {at[0]} {at[1]})\n\t\t(size {size})\n\t\t(drill {drill})\n'
                          f'\t\t(layers {_q(layers[0])} {_q(layers[1])})\n\t\t(net {_q(net)})\n\t\t(uuid {_q(u)})\n\t)')
        return u

    def zone(self, net: str, layer: str, outline: Sequence[Pt], holes: Sequence[Sequence[Pt]] = (),
             filled: bool = True) -> str:
        u = _u()
        pts = " ".join(f"(xy {x} {y})" for x, y in outline)
        fill = ""
        if filled:
            fpts = " ".join(f"(xy {x} {y})" for x, y in fracture(outline, holes))
            fill = f'\t\t(filled_polygon\n\t\t\t(layer {_q(layer)})\n\t\t\t(pts {fpts})\n\t\t)\n'
        self.items.append(f'\t(zone\n\t\t(net {_q(net)})\n\t\t(layer {_q(layer)})\n\t\t(uuid {_q(u)})\n'
                          f'\t\t(min_thickness 0.2)\n\t\t(fill yes)\n\t\t(polygon\n\t\t\t(pts {pts})\n\t\t)\n{fill}\t)')
        return u

    def mask_rect(self, layer: str, x0: float, y0: float, x1: float, y1: float) -> str:
        """A filled rectangle on a solder-mask layer (F.Mask / B.Mask): an opening in the mask there."""
        u = _u()
        self.items.append(f'\t(gr_rect\n\t\t(start {x0} {y0})\n\t\t(end {x1} {y1})\n\t\t(stroke\n\t\t\t(width 0)\n'
                          f'\t\t\t(type default)\n\t\t)\n\t\t(fill yes)\n\t\t(layer {_q(layer)})\n\t\t(uuid {_q(u)})\n\t)')
        return u

    def plane(self, net: str, layer: str, holes: Sequence[Sequence[Pt]] = (), inset: float = 0.3) -> str:
        w, h = self.size
        return self.zone(net, layer, [(inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)],
                         holes)

    def footprint(self, ref: str, value: str, lib_id: str, at: Pt, pads: Sequence[dict], angle: float = 0.0,
                  layer: str = "F.Cu", attrs: str = "smd") -> str:
        u = _u()
        pad_txt = []
        for p in pads:
            ptype = p.get("type", "smd")
            layers = p.get("layers") or ([layer, "F.Mask"] if ptype == "smd" else ["*.Cu", "*.Mask"])
            drill = f'\t\t\t(drill {p["drill"]})\n' if "drill" in p else ""
            px, py = p["at"]
            pin = ""
            if p.get("pinfunction"):
                pin += f'\t\t\t(pinfunction {_q(p["pinfunction"] + "_" + str(p["number"]))})\n'
            if p.get("pintype"):
                pin += f'\t\t\t(pintype {_q(p["pintype"])})\n'
            # a custom pad: its anchor (`size`) and polygon primitives (`primitives`: point lists around the pad)
            prims = ""
            if p.get("primitives"):
                polys = "".join("\t\t\t\t(gr_poly\n\t\t\t\t\t(pts " + " ".join(f"(xy {x} {y})" for x, y in pts)
                                + ")\n\t\t\t\t\t(width 0)\n\t\t\t\t\t(fill yes)\n\t\t\t\t)\n" for pts in p["primitives"])
                prims = (f'\t\t\t(options\n\t\t\t\t(clearance outline)\n\t\t\t\t(anchor rect)\n\t\t\t)\n'
                         f'\t\t\t(primitives\n{polys}\t\t\t)\n')
            pad_txt.append(
                f'\t\t(pad {_q(str(p["number"]))} {ptype} {p.get("shape", "rect")}\n\t\t\t(at {px} {py} {angle})\n'
                f'\t\t\t(size {p.get("size", (0.6, 0.6))[0]} {p.get("size", (0.6, 0.6))[1]})\n{drill}'
                f'\t\t\t(layers {" ".join(_q(l) for l in layers)})\n\t\t\t(net {_q(p["net"])})\n{pin}{prims}'
                f'\t\t\t(uuid {_q(_u())})\n\t\t)')
        self.items.append(
            f'\t(footprint {_q(lib_id)}\n\t\t(layer {_q(layer)})\n\t\t(uuid {_q(u)})\n\t\t(at {at[0]} {at[1]} {angle})\n'
            f'\t\t(property "Reference" {_q(ref)}\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n\t\t)\n'
            f'\t\t(property "Value" {_q(value)}\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n\t\t)\n'
            f'\t\t(attr {attrs})\n' + "\n".join(pad_txt) + "\n\t)")
        return u

    def label(self, ref: str, gt: Optional[str] = None, sheet: Optional[str] = None,
              sheet_file: str = "phase.kicad_sch"):
        """Give footprint `ref` a GT field (a detection label) and/or put it in a schematic sheet copy, as KiCad 10
        writes them: (property "GT" ...), (path "/<sheet uuid>/<symbol uuid>"), (sheetname ...), (sheetfile ...).
        Copies of one sheet: the same sheet_file, different sheet names (one uuid per name)."""
        key = f'(property "Reference" {_q(ref)}'
        k = next(i for i, it in enumerate(self.items) if key in it)
        extra = ""
        if gt is not None:
            extra += f'\t\t(property "GT" {_q(gt)}\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)\n\t\t)\n'
        if sheet is not None:
            sheets = self.__dict__.setdefault("_sheet_ids", {})
            sid = sheets.setdefault(sheet, _u())
            extra += (f'\t\t(path "/{sid}/{_u()}")\n\t\t(sheetname {_q(sheet)})\n'
                      f'\t\t(sheetfile {_q(sheet_file)})\n')
        self.items[k] = self.items[k].replace("\t\t(attr ", extra + "\t\t(attr ", 1)

    def fields(self, ref: str, **values: str):
        """Give footprint `ref` symbol fields as KiCad 10 copies them onto the footprint (hidden properties), e.g.
        fields("Q1", **{"Rds(on)": "3.8milliohm", "Coss": "616 picofarad"}): an HTTP lib part's parameters."""
        key = f'(property "Reference" {_q(ref)}'
        k = next(i for i, it in enumerate(self.items) if key in it)
        extra = "".join(f'\t\t(property {_q(name)} {_q(v)}\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)\n'
                        f'\t\t)\n' for name, v in values.items())
        self.items[k] = self.items[k].replace("\t\t(attr ", extra + "\t\t(attr ", 1)

    # ------------------------------------------------------------- composite parts
    def ic(self, ref: str, at: Pt, signal_net: str, gnd_net: str = "GND", layer: str = "F.Cu",
           value: str = "IC", gnd_offset: Pt = (0.0, 1.0), pintype: str = "", pinfunction: str = "") -> str:
        """Two-pad stand-in for a chip: pad 1 = signal, pad 2 = ground, `gnd_offset` from pad 1."""
        return self.footprint(ref, value, "Package_SO:Fake_IC", at, [
            {"number": 1, "at": (0, 0), "net": signal_net, "pintype": pintype, "pinfunction": pinfunction},
            {"number": 2, "at": gnd_offset, "net": gnd_net, "pintype": "power_in", "pinfunction": "GND"},
        ], layer=layer)

    def two_pin(self, ref: str, value: str, at: Pt, net1: str, net2: str, package: str = "0402",
                kind: str = "R", angle: float = 0.0, layer: str = "F.Cu") -> str:
        lib = {"R": f"Resistor_SMD:R_{package}_1005Metric", "C": f"Capacitor_SMD:C_{package}_1005Metric"}[kind]
        return self.footprint(ref, value, lib, at, [
            {"number": 1, "at": (-0.5, 0), "net": net1, "size": (0.5, 0.6)},
            {"number": 2, "at": (0.5, 0), "net": net2, "size": (0.5, 0.6)},
        ], angle=angle, layer=layer)

    # ------------------------------------------------------------- output
    def text(self) -> str:
        layer_ids = {"F.Cu": 0, "B.Cu": 2}
        for i in range(1, self.layers - 1):
            layer_ids[f"In{i}.Cu"] = 2 + 2 * i
        layers = "\n".join(f'\t\t({layer_ids[n]} {_q(n)} signal)' for n in self.cu)
        stack = []
        mask = (f'\t\t\t(layer "{{}}"\n\t\t\t\t(type "{{}} Solder Mask")\n\t\t\t\t(thickness {self.mask})\n\t\t\t)'
                if self.mask is not None else "")
        if mask:
            stack.append(mask.format("F.Mask", "Top"))
        for i, name in enumerate(self.cu):
            stack.append(f'\t\t\t(layer {_q(name)}\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness {self.cu_thickness})\n\t\t\t)')
            if i < len(self.dielectrics):
                kind = "core" if i == (len(self.cu) - 1) // 2 else "prepreg"
                stack.append(f'\t\t\t(layer "dielectric {i + 1}"\n\t\t\t\t(type {_q(kind)})\n'
                             f'\t\t\t\t(thickness {self.dielectrics[i]})\n\t\t\t\t(material "FR4")\n'
                             f'\t\t\t\t(epsilon_r {self.epsilon_r})\n\t\t\t\t(loss_tangent 0.02)\n\t\t\t)')
        if mask:
            stack.append(mask.format("B.Mask", "Bottom"))
        thickness = sum(self.dielectrics) + self.cu_thickness * self.layers
        w, h = self.size
        edge = "\n".join(
            f'\t(gr_line\n\t\t(start {a[0]} {a[1]})\n\t\t(end {b[0]} {b[1]})\n\t\t(stroke\n\t\t\t(width 0.05)\n'
            f'\t\t\t(type default)\n\t\t)\n\t\t(layer "Edge.Cuts")\n\t\t(uuid {_q(_u())})\n\t)'
            for a, b in [((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))])
        return (
            f'(kicad_pcb\n\t(version 20260206)\n\t(generator "groundtruth-test")\n\t(generator_version "10.0")\n'
            f'\t(general\n\t\t(thickness {thickness:.4f})\n\t)\n\t(paper "A4")\n\t(layers\n{layers}\n'
            f'\t\t(25 "Edge.Cuts" user)\n\t\t(39 "User.7" user)\n\t\t(41 "User.8" user)\n\t\t(43 "User.9" user)\n\t)\n'
            f'\t(setup\n\t\t(stackup\n' + "\n".join(stack) + '\n\t\t)\n\t)\n'
            + edge + "\n" + "\n".join(self.items) + "\n)\n")

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.write_text(self.text(), encoding="utf-8")
        return path


# ----------------------------------------------------------------------------- scenarios
def microstrip_board(slot: Optional[tuple[float, float, float, float]] = None, layers: int = 2,
                     size: Pt = (40.0, 20.0), plane_layer: Optional[str] = None) -> BoardGen:
    """Straight F.Cu trace from U1 (x=5) to U2 (x=35) at y=10 over a GND plane.

    `slot` = (x0, y0, x1, y1) cuts a rectangular hole into the plane.
    """
    g = BoardGen(layers=layers, size=size)
    y = size[1] / 2
    g.ic("U1", (5.0, y), "SIG", gnd_offset=(0.0, 1.0))
    g.ic("U2", (size[0] - 5.0, y), "SIG", gnd_offset=(0.0, 1.0))
    g.track((5.0, y), (size[0] - 5.0, y), "SIG")
    holes = []
    if slot:
        x0, y0, x1, y1 = slot
        holes.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    layer = plane_layer or g.cu[1]
    g.plane("GND", layer, holes)
    # tie the IC ground pads to the plane
    g.via((5.0, y + 1.6), "GND", (g.cu[0], g.cu[-1]))
    g.track((5.0, y + 1.0), (5.0, y + 1.6), "GND")
    g.via((size[0] - 5.0, y + 1.6), "GND", (g.cu[0], g.cu[-1]))
    g.track((size[0] - 5.0, y + 1.0), (size[0] - 5.0, y + 1.6), "GND")
    return g


def series_path_board(shunt: Optional[tuple] = None) -> BoardGen:
    """A signal path over a GND plane through three series parts, four nets: U1.1 (SIG_A) → R1 → SIG_B → L1 →
    SIG_C → C1 → SIG_D → U2.1, along y = 10 (L1 is drawn as a 0402 resistor footprint: give it an inductor
    model in the settings). shunt = (ref, value, kind "R"/"C", x, net): a part from the path to GND at x, 2 mm
    above the path (a stub to its path pad, a GND via beside its other pad)."""
    g = BoardGen(layers=2, size=(50.0, 20.0))
    y = 10.0
    g.ic("U1", (5.0, y), "SIG_A", gnd_offset=(0.0, 1.0))
    g.ic("U2", (45.0, y), "SIG_D", gnd_offset=(0.0, 1.0))
    for (ref, value, x, a, b, kind) in (("R1", "33", 15.0, "SIG_A", "SIG_B", "R"), ("L1", "1u", 25.0, "SIG_B", "SIG_C", "R"),
                                        ("C1", "100n", 35.0, "SIG_C", "SIG_D", "C")):
        g.two_pin(ref, value, (x, y), a, b, kind=kind)
    for x0, x1, net in ((5.0, 14.5, "SIG_A"), (15.5, 24.5, "SIG_B"), (25.5, 34.5, "SIG_C"), (35.5, 45.0, "SIG_D")):
        g.track((x0, y), (x1, y), net)
    g.plane("GND", g.cu[-1])
    for x in (5.0, 45.0):
        g.via((x, y + 1.6), "GND", (g.cu[0], g.cu[-1]))
        g.track((x, y + 1.0), (x, y + 1.6), "GND")
    if shunt:
        ref, value, kind, x, net = shunt
        g.two_pin(ref, value, (x, y + 2.0), net, "GND", kind=kind)
        if x - 0.5 > 45.0:                    # beyond the receiver: extend the last track to the stub
            g.track((45.0, y), (x - 0.5, y), net)
        g.track((x - 0.5, y), (x - 0.5, y + 2.0), net)
        g.via((x + 0.5, y + 2.6), "GND", (g.cu[0], g.cu[-1]))
        g.track((x + 0.5, y + 2.0), (x + 0.5, y + 2.6), "GND")
    return g


def rc_lowpass_board(r: str = "1k", c: str = "10n") -> BoardGen:
    """An RC low-pass over a GND plane: U1.1 (SIG_A) → R1 (`r`) → SIG_B → U2.1, with C9 (`c`) from SIG_B to GND
    2 mm above the path (a stub to its pad, a GND via at its other pad). With the source's R_s and the receiver's
    C_L: H(s) = 1 / (1 + s·(R_s + R1)·(C9 + C_L)) where the 30 mm of track is short against the wavelength."""
    g = BoardGen(layers=2, size=(40.0, 20.0))
    y = 10.0
    g.ic("U1", (5.0, y), "SIG_A", gnd_offset=(0.0, 1.0))
    g.ic("U2", (35.0, y), "SIG_B", gnd_offset=(0.0, 1.0))
    g.two_pin("R1", r, (15.0, y), "SIG_A", "SIG_B", kind="R")
    g.track((5.0, y), (14.5, y), "SIG_A")
    g.track((15.5, y), (35.0, y), "SIG_B")
    g.two_pin("C9", c, (25.0, y + 2.0), "SIG_B", "GND", kind="C")
    g.track((24.5, y), (24.5, y + 2.0), "SIG_B")
    g.plane("GND", g.cu[-1])
    for x, yy in ((5.0, y + 1.0), (35.0, y + 1.0), (25.5, y + 2.0)):
        g.via((x, yy + 0.6), "GND", (g.cu[0], g.cu[-1]))
        g.track((x, yy), (x, yy + 0.6), "GND")
    return g


def opamp_circuit_board(parts: Sequence[tuple], opamps: Sequence[tuple], out_net: str = "OUT",
                        in_net: str = "SIG_IN") -> BoardGen:
    """Any op-amp circuit, unrouted (the pads carry their nets; analysis/analog_net joins them as the netlist says),
    over a GND plane: the driver U1.1 on `in_net`, the receiver U2.1 on `out_net`, each `parts` entry (ref, value,
    kind "R"/"C", net 1, net 2) a 0402 part on a grid, each `opamps` entry (ref, {pin number: (pin name, net)}) a
    part with those pins (pin types: V+/V- power_in, OUT output, the rest input)."""
    g = BoardGen(layers=2, size=(80.0, 40.0))
    g.ic("U1", (4.0, 20.0), in_net, gnd_offset=(0.0, 1.0))
    g.ic("U2", (76.0, 20.0), out_net, gnd_offset=(0.0, 1.0))
    for k, (ref, value, kind, a, b) in enumerate(parts):
        g.two_pin(ref, value, (12.0 + 6.0 * (k % 8), 8.0 + 5.0 * (k // 8)), a, b, kind=kind)
    for k, (ref, pins) in enumerate(opamps):
        pads = []
        for j, (num, (name, net)) in enumerate(sorted(pins.items())):
            ptype = "power_in" if name.upper() in ("V+", "V-") else "output" if name.upper().startswith("OUT") \
                else "input"
            pads.append({"number": num, "at": (-1.5 + 0.65 * j, 0.0), "net": net, "pintype": ptype,
                         "pinfunction": name})
        g.footprint(ref, "OPAMP", "Package_SO:Fake_OPAMP", (20.0 + 15.0 * k, 30.0), pads)
    g.plane("GND", g.cu[-1])
    return g


def opamp_path_board(r_after: Optional[str] = None, **fields: str) -> BoardGen:
    """A signal path through an op-amp stage over a GND plane: U1.1 (SIG_A) → R1 33 Ω → SIG_B → U5 (+IN pin 3,
    OUT pin 1, GND pin 2 with a via to the plane) → SIG_C → U2.1. `fields`: U5's symbol fields as an HTTP lib would
    give them (Gain="10", GBW="10MHz" ...); a Gain field makes U5 an amplifier. r_after: a resistor R2 of this
    value between U5's output and U2 (SIG_C → R2 → SIG_D → U2.1), as refboard's R303 after the buffer."""
    g = BoardGen(layers=2, size=(50.0, 20.0))
    y = 10.0
    last = "SIG_D" if r_after else "SIG_C"
    g.ic("U1", (5.0, y), "SIG_A", gnd_offset=(0.0, 1.0))
    g.ic("U2", (45.0, y), last, gnd_offset=(0.0, 1.0))
    g.two_pin("R1", "33", (12.0, y), "SIG_A", "SIG_B", kind="R")
    if r_after:
        g.two_pin("R2", r_after, (38.5, y), "SIG_C", "SIG_D", kind="R")
    g.footprint("U5", "OPA376", "Package_TO_SOT_SMD:SOT-23-5", (25.0, y), [
        {"number": 3, "at": (-1.5, 0.0), "net": "SIG_B", "pintype": "input", "pinfunction": "+IN"},
        {"number": 1, "at": (1.5, 0.0), "net": "SIG_C", "pintype": "output", "pinfunction": "OUT"},
        {"number": 2, "at": (0.0, 1.5), "net": "GND", "pintype": "power_in", "pinfunction": "GND"},
    ])
    if fields:
        g.fields("U5", **fields)
    out = ((26.5, 38.0, "SIG_C"), (39.0, 45.0, "SIG_D")) if r_after else ((26.5, 45.0, "SIG_C"),)
    for x0, x1, net in ((5.0, 11.5, "SIG_A"), (12.5, 23.5, "SIG_B")) + out:
        g.track((x0, y), (x1, y), net)
    g.plane("GND", g.cu[-1])
    for x, dy in ((5.0, 1.0), (45.0, 1.0), (25.0, 1.5)):
        g.via((x, y + dy + 0.6), "GND", (g.cu[0], g.cu[-1]))
        g.track((x, y + dy), (x, y + dy + 0.6), "GND")
    return g


HL_X0, HL_Y = 10.0, 12.0     # left end and centre line of the hot-loop boards' VIN strip


def split_strip_board(length: float = 10.0, gap: float = 0.6, joined: bool = False, width: float = 4.0,
                      pad_depth: float = 0.8, metal: Optional[str] = None) -> BoardGen:
    """Two 4 mm wide pours on F.Cu (PWR from x=4, OUT after a `gap`), each `length` long, joined by a part R1 whose
    two pads span the pours' whole width at the gap (a shunt, a FET's drain and source rows); a pad at each far end
    (U1 on PWR, U2 on OUT) to feed the current; GND plane on In1. joined: the same copper as one PWR pour across
    the gap and no R1 (the reference: what the strip alone measures). metal: R1's Terminal Sheet Resistance."""
    g = BoardGen(layers=4, size=(2 * length + gap + 16, 20.0))
    x0, y = 4.0, 10.0
    x_end = x0 + 2 * length + gap
    second = "PWR" if joined else "OUT"
    for ref, x, net in (("U1", x0, "PWR"), ("U2", x_end, second)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, y), [
            {"number": 1, "at": (0, 0), "net": net, "size": (1.0, width - 0.4)},
            {"number": 2, "at": (0, width / 2 + 1.5), "net": "GND", "size": (1.0, 1.0)}])
        g.via((x, y + width / 2 + 1.5), "GND", ("F.Cu", "B.Cu"))
    if joined:
        g.zone("PWR", "F.Cu", [(x0, y - width / 2), (x_end, y - width / 2), (x_end, y + width / 2),
                               (x0, y + width / 2)])
    else:
        xa, xb = x0 + length, x0 + length + gap
        g.zone("PWR", "F.Cu", [(x0, y - width / 2), (xa, y - width / 2), (xa, y + width / 2), (x0, y + width / 2)])
        g.zone("OUT", "F.Cu", [(xb, y - width / 2), (x_end, y - width / 2), (x_end, y + width / 2),
                               (xb, y + width / 2)])
        g.footprint("R1", "1m", "Resistor_SMD:Fake_wide", ((xa + xb) / 2, y), [
            {"number": 1, "at": (-(gap + pad_depth) / 2, 0), "net": "PWR", "size": (pad_depth, width - 0.2)},
            {"number": 2, "at": ((gap + pad_depth) / 2, 0), "net": "OUT", "size": (pad_depth, width - 0.2)}])
        if metal is not None:
            g.fields("R1", **{"Terminal Sheet Resistance": metal})
    g.plane("GND", "In1.Cu")
    return g


def terminal_strip_board(metal: Optional[str] = None, length: float = 30.0, width: float = 4.0,
                         pad_length: float = 10.0) -> BoardGen:
    """One 4 mm wide pour PWR on F.Cu, fed at its ends (U1, U2), with a capacitor C1 whose first pad (pad_length along
    the strip, its whole width) lies on it halfway and whose second pad sits on a small AUX pour beside it: at DC the
    capacitor is open, so its terminal over the strip only conducts alongside the copper. metal: C1's Terminal
    Sheet Resistance field. GND plane on In1."""
    g = BoardGen(layers=4, size=(length + 16, 24.0))
    x0, y = 4.0, 10.0
    x_end = x0 + length
    g.zone("PWR", "F.Cu", [(x0, y - width / 2), (x_end, y - width / 2), (x_end, y + width / 2), (x0, y + width / 2)])
    xc = (x0 + x_end) / 2
    g.zone("AUX", "F.Cu", [(xc - 1.5, y + width / 2 + 1.0), (xc + 1.5, y + width / 2 + 1.0),
                           (xc + 1.5, y + width / 2 + 3.0), (xc - 1.5, y + width / 2 + 3.0)])
    for ref, x in (("U1", x0), ("U2", x_end)):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, y), [
            {"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, width - 0.4)},
            {"number": 2, "at": (0, width / 2 + 1.5), "net": "GND", "size": (1.0, 1.0)}])
        g.via((x, y + width / 2 + 1.5), "GND", ("F.Cu", "B.Cu"))
    g.footprint("C1", "1u", "Capacitor_SMD:Fake_long", (xc, y), [
        {"number": 1, "at": (0, 0), "net": "PWR", "size": (pad_length, width)},
        {"number": 2, "at": (0, width / 2 + 2.0), "net": "AUX", "size": (2.0, 1.2)}])
    if metal is not None:
        g.fields("C1", **{"Terminal Sheet Resistance": metal})
    g.plane("GND", "In1.Cu")
    return g


def fet_tab_board(metal: Optional[str] = None, length: float = 20.0, tab: float = 10.0, width: float = 2.1
                  ) -> BoardGen:
    """A vertical FET Q1 at the end of a PWR strip on F.Cu (`length` long from x=4, `width` wide): its drain tab
    covers the strip's last `tab` mm (its whole width), its three small source pads sit on an OUT pour beyond; U1 feeds
    the strip's start, U2 takes the current out of OUT. metal: Q1's Terminal Sheet Resistance. GND plane on In1."""
    g = BoardGen(layers=4, size=(length + 20, 16.0))
    x0, y = 4.0, 8.0
    x_end = x0 + length
    g.zone("PWR", "F.Cu", [(x0, y - width / 2), (x_end, y - width / 2), (x_end, y + width / 2), (x0, y + width / 2)])
    g.zone("OUT", "F.Cu", [(x_end + 1.0, y - width / 2), (x_end + 6.0, y - width / 2), (x_end + 6.0, y + width / 2),
                           (x_end + 1.0, y + width / 2)])
    for ref, x, net in (("U1", x0, "PWR"), ("U2", x_end + 5.0, "OUT")):
        g.footprint(ref, "IC", "Package_SO:Fake_IC", (x, y), [
            {"number": 1, "at": (0, 0), "net": net, "size": (0.6, width - 0.4)},
            {"number": 2, "at": (0, width / 2 + 1.5), "net": "GND", "size": (1.0, 1.0)}])
        g.via((x, y + width / 2 + 1.5), "GND", ("F.Cu", "B.Cu"))
    xd = x_end - tab / 2
    g.footprint("Q1", "6m5", "Package_TO_SOT_SMD:Fake_tab", (xd, y), [
        {"number": 1, "at": (0, 0), "net": "PWR", "size": (tab, width)}] + [
        {"number": 2, "at": (tab / 2 + 1.8, dy), "net": "OUT", "size": (0.6, 0.45)} for dy in (-0.6, 0.0, 0.6)])
    if metal is not None:
        g.fields("Q1", **{"Terminal Sheet Resistance": metal})
    g.plane("GND", "In1.Cu")
    return g


def hot_loop_board(length: float = 20.0, width: float = 4.0, caps: Sequence[tuple] = (("C1", "10u", "end", 0.0),),
                   connector: bool = False, sw_pin: bool = False, far_series: bool = False,
                   gnd_via_mm: Optional[float] = None, out_pin: bool = False) -> BoardGen:
    """4-layer power stage reduced to its hot loop: a VIN pour strip on F.Cu (x from HL_X0, `length` long, `width`
    wide, centred on y = HL_Y) over a GND plane on In1 (0.2 mm). An integrated converter U1 at the strip's right
    end (VIN pad inside the strip, GND pad beyond its end with a via to the plane): the switching current flows from
    the capacitors along the strip into U1 and back under it in the plane.

    caps: (ref, value, where, x offset from the strip's left end); where = "end" (on the centre line, GND pad
    beyond the strip's left end), "top" or "bottom" (across that edge, GND pad outside with a via).
    connector: J1 left of the strip (pin 1 on VIN by a track, pin 2 GND with a via) for the LISN.
    sw_pin: U1 also gets a SW pin with an inductor L1 to VOUT and an output capacitor (auto-detection).
    out_pin: U1 also has a V_OUT pin on VOUT, the inductor's other side (like the LT3970's), with sw_pin.
    far_series: J1.1 is on VIN_IN instead, which reaches VIN through a 0R (R9) 9.5 mm below the strip, outside the
    region spanned by the switch, the capacitors and the connector (like a surge-stopper FET).
    gnd_via_mm: the "end" capacitors' GND pads sit on an F.Cu GND patch whose only via to the plane is this far to
    the left (instead of a via in the pad): an unstitched capacitor ground."""
    x0, y, w = HL_X0, HL_Y, width
    g = BoardGen(layers=4, size=(x0 + length + 10.0, 2 * y))
    g.zone("VIN", "F.Cu", [(x0, y - w / 2), (x0 + length, y - w / 2), (x0 + length, y + w / 2), (x0, y + w / 2)])
    g.plane("GND", "In1.Cu")
    xe = x0 + length
    pads = [{"number": 1, "at": (-0.6, 0.0), "net": "VIN", "size": (1.0, 2.0), "pinfunction": "VIN",
             "pintype": "power_in"},
            {"number": 2, "at": (1.2, 0.0), "net": "GND", "size": (1.0, 2.0), "pinfunction": "GND",
             "pintype": "power_in"}]
    if sw_pin:
        pads.append({"number": 3, "at": (0.3, w / 2 + 1.6), "net": "SW", "size": (0.8, 0.8), "pinfunction": "SW",
                     "pintype": "power_out"})
    if sw_pin and out_pin:
        pads.append({"number": 4, "at": (-0.6, -w / 2 - 1.6), "net": "VOUT", "size": (0.8, 0.8),
                     "pinfunction": "V_{OUT}", "pintype": "input"})
    g.footprint("U1", "BUCK", "Package_SO:Fake_PowerIC", (xe, y), pads)
    g.via((xe + 1.2, y), "GND", ("F.Cu", "B.Cu"))
    if sw_pin:
        g.footprint("L1", "4u7", "Inductor_SMD:L_1210_3225Metric", (xe + 0.3, y + w / 2 + 4.0), [
            {"number": 1, "at": (0.0, -1.4), "net": "SW", "size": (1.2, 1.0)},
            {"number": 2, "at": (0.0, 1.4), "net": "VOUT", "size": (1.2, 1.0)}])
        g.track((xe + 0.3, y + w / 2 + 1.6), (xe + 0.3, y + w / 2 + 2.6), "SW", width=0.6)
        g.two_pin("C9", "22u", (xe + 4.0, y + w / 2 + 5.4), "VOUT", "GND", package="1206", kind="C")
        g.track((xe + 0.3, y + w / 2 + 5.4), (xe + 3.5, y + w / 2 + 5.4), "VOUT", width=0.6)
        g.via((xe + 4.5, y + w / 2 + 6.2), "GND")
        g.track((xe + 4.5, y + w / 2 + 5.4), (xe + 4.5, y + w / 2 + 6.2), "GND", width=0.4)
    for ref, value, where, dx in caps:
        package = "1206" if value.endswith("u") else "0603"
        lib = f"Capacitor_SMD:C_{package}_3216Metric"
        if where == "end":
            g.footprint(ref, value, lib, (x0 + dx, y), [
                {"number": 1, "at": (0.6, 0.0), "net": "VIN", "size": (1.0, 2.0)},
                {"number": 2, "at": (-1.2, 0.0), "net": "GND", "size": (1.0, 2.0)}])
            gx = x0 + dx - 1.2
            if gnd_via_mm is None:
                g.via((gx, y), "GND", ("F.Cu", "B.Cu"))
            else:
                g.zone("GND", "F.Cu", [(gx - gnd_via_mm - 1.0, y - 1.5), (gx + 0.5, y - 1.5), (gx + 0.5, y + 1.5),
                                       (gx - gnd_via_mm - 1.0, y + 1.5)])
                g.via((gx - gnd_via_mm, y), "GND", ("F.Cu", "B.Cu"))
        else:
            s = -1.0 if where == "top" else 1.0
            edge = y + s * w / 2
            g.footprint(ref, value, lib, (x0 + dx, edge), [
                {"number": 1, "at": (0.0, -s * 0.5), "net": "VIN", "size": (1.0, 1.2)},
                {"number": 2, "at": (0.0, s * 1.5), "net": "GND", "size": (1.0, 1.2)}])
            g.via((x0 + dx, edge + s * 1.5), "GND", ("F.Cu", "B.Cu"))
    if connector:
        jx = x0 - 6.0
        plus = "VIN_IN" if far_series else "VIN"
        g.footprint("J1", "Conn", "Connector:Fake_2pin", (jx, y), [
            {"number": 1, "at": (0.0, -1.7), "net": plus, "size": (1.5, 1.5)},
            {"number": 2, "at": (0.0, 1.7), "net": "GND", "size": (1.5, 1.5)}])
        g.via((jx, y + 1.7), "GND", ("F.Cu", "B.Cu"))
        if far_series:
            ry = y + 9.5
            g.polyline([(jx, y - 1.7), (x0 - 8.0, y - 1.7), (x0 - 8.0, ry), (x0 - 4.5, ry)], "VIN_IN", width=0.5)
            g.two_pin("R9", "0R", (x0 - 4.0, ry), "VIN_IN", "VIN")
            g.polyline([(x0 - 3.5, ry), (x0 + 0.5, ry), (x0 + 0.5, y + w / 2 - 0.5)], "VIN", width=0.5)
        else:
            g.track((jx, y - 1.7), (x0 + 0.5, y - 1.7), "VIN", width=0.5)
    return g


def discrete_buck_board(out_connector: bool | str = False, shunt: bool = False, surge_fet: bool = False,
                        post_stage: bool = False, victim: Optional[tuple] = None, bootstrap: bool = False,
                        loop: bool = False) -> BoardGen:
    """A synchronous buck with discrete FETs: VIN pour, Q1 (high side) and Q2 (low side) joined by a SW pour,
    inductor L1 to VOUT, input capacitor C1 (VIN-GND), output capacitor C2, input connector J1; GND plane on In1.
    out_connector: an output connector J2 at the right edge, its VOUT pin fed from L1 by a 1 mm track on F.Cu
    (27.4 → 28.5 along y = 14, down to y = 10, on to x = 36), its GND pin with a via in the pad ("tht": through-hole
    pins, 1 mm holes, and no via: the GND pin's hole reaches the plane).
    shunt: a 10 mΩ current-sense resistor R10 in that track at x = 33 (J2.1 on VOUT_S behind it).
    surge_fet: J1.1 is on VBATT, which reaches VIN only through a surge stopper's series FET Q9 (D on VBATT, S on
    VIN, its gate on Q9_G), like Q601 between J101 and +VDC on refboard.
    post_stage: a regulator fed from the buck's output (U9: VIN on VOUT, SW on SW9 to L9 and V3, GND with a via),
    like refboard's LT3970 on the LM5008's +12 V.
    victim (x0, x1, y): a reset line NRST on F.Cu (0.2 mm) from a 10k pull-up R5 at x0 to a microcontroller U5
    (STM32F030: NRST an input pin, GND with a via to the plane) at x1, along y (analysis/victims.py).
    bootstrap: a gate driver U7 (BOOST on BST, TG on TGU, GND) at (30, 26), its bootstrap capacitor C7 from BST to
    SW and a 2R gate resistor R7 from TGU to Q1's gate HG (analysis/safety.py: nets riding on the switch node).
    loop: a controller U8 (pins SW1 on SW, ITH1, V_FB1, SENSE1+ / SENSE1-, GND) like refboard's LTC7890
    channel 1 (analysis/loop.py): L1's far side on VL, a 10 mΩ sense shunt RS from VL to VOUT with 10R filters RF1 /
    RF2 to the SENSE pins; on ITH1 a 2.2k (RC1) to a 22n (CC1) to GND and 22p (CH1) to GND; on V_FB1 100k (RT1) from
    VOUT and 25k (RB1) to GND."""
    g = BoardGen(layers=4, size=(40.0, 30.0))
    g.plane("GND", "In1.Cu")
    g.zone("VIN", "F.Cu", [(6.0, 6.0), (16.0, 6.0), (16.0, 12.0), (6.0, 12.0)])
    g.zone("SW", "F.Cu", [(17.0, 6.0), (22.0, 6.0), (22.0, 20.0), (17.0, 20.0)])
    for ref, at, d_net, s_net, g_net in (("Q1", (16.5, 9.0), "VIN", "SW", "HG"), ("Q2", (19.5, 21.5), "SW", "GND", "LG")):
        horizontal = ref == "Q1"
        d_at, s_at = ((-1.0, 0.0), (1.0, 0.0)) if horizontal else ((0.0, -1.0), (0.0, 1.0))
        g.footprint(ref, "SiR500", "Package_SO:Fake_PowerPAK", at, [
            {"number": 5, "at": d_at, "net": d_net, "size": (1.2, 1.6) if horizontal else (1.6, 1.2), "pinfunction": "D"},
            {"number": 1, "at": s_at, "net": s_net, "size": (1.2, 1.6) if horizontal else (1.6, 1.2), "pinfunction": "S"},
            {"number": 4, "at": (0.0, 2.2) if horizontal else (2.2, 0.0), "net": g_net, "size": (0.6, 0.6),
             "pinfunction": "G"}])
    g.zone("GND", "F.Cu", [(14.0, 22.5), (24.0, 22.5), (24.0, 26.0), (14.0, 26.0)])
    for x in (16.0, 19.5, 23.0):
        g.via((x, 24.5), "GND")
    g.footprint("C1", "10u", "Capacitor_SMD:C_1206_3216Metric", (11.0, 12.3), [
        {"number": 1, "at": (0.0, -0.9), "net": "VIN", "size": (1.6, 1.0)},
        {"number": 2, "at": (0.0, 0.9), "net": "GND", "size": (1.6, 1.0)}])
    g.via((11.0, 14.0), "GND")
    g.track((11.0, 13.2), (11.0, 14.0), "GND", width=0.6)
    g.footprint("L1", "4u7", "Inductor_SMD:L_1210_3225Metric", (26.0, 14.0), [
        {"number": 1, "at": (-1.4, 0.0), "net": "SW", "size": (1.0, 1.2)},
        {"number": 2, "at": (1.4, 0.0), "net": "VL" if loop else "VOUT", "size": (1.0, 1.2)}])
    g.track((22.0, 14.0), (24.6, 14.0), "SW", width=1.0)
    g.two_pin("C2", "22u", (31.0, 14.0), "VOUT", "GND", package="1206", kind="C")
    if loop:                                    # L1 -> VL -> the 10 mOhm sense shunt RS -> VOUT
        g.track((27.4, 14.0), (28.75, 14.0), "VL", width=1.0)
        g.two_pin("RS", "10m", (29.25, 14.0), "VL", "VOUT", package="1206")
        g.track((29.75, 14.0), (30.5, 14.0), "VOUT", width=1.0)
    else:
        g.track((27.4, 14.0), (30.5, 14.0), "VOUT", width=1.0)
    g.via((31.5, 15.0), "GND")
    g.track((31.5, 14.0), (31.5, 15.0), "GND", width=0.6)
    g.footprint("J1", "Conn", "Connector:Fake_2pin", (3.0, 9.0), [
        {"number": 1, "at": (0.0, -1.5), "net": "VBATT" if surge_fet else "VIN", "size": (1.5, 1.5)},
        {"number": 2, "at": (0.0, 1.5), "net": "GND", "size": (1.5, 1.5)}])
    if surge_fet:
        g.footprint("Q9", "FDMT800150DC", "Package_TO_SOT_SMD:Fake_PQFN", (4.8, 7.5), [
            {"number": 3, "at": (-0.8, 0.0), "net": "VBATT", "size": (0.6, 0.8), "pinfunction": "D"},
            {"number": 2, "at": (0.8, 0.0), "net": "VIN", "size": (0.6, 0.8), "pinfunction": "S"},
            {"number": 1, "at": (0.0, 1.2), "net": "Q9_G", "size": (0.5, 0.5), "pinfunction": "G"}])
        g.track((3.0, 7.5), (4.0, 7.5), "VBATT", width=0.8)
        g.track((5.6, 7.5), (6.5, 7.5), "VIN", width=0.8)
    else:
        g.track((3.0, 7.5), (6.5, 7.5), "VIN", width=0.8)
    g.via((3.0, 10.5), "GND")
    if out_connector:
        out_net = "VOUT_S" if shunt else "VOUT"
        hole = {"type": "thru_hole", "drill": 1.0} if out_connector == "tht" else {}
        g.footprint("J2", "Conn", "Connector:Fake_2pin", (36.0, 11.5), [
            {"number": 1, "at": (0.0, -1.5), "net": out_net, "size": (1.5, 1.5), **hole},
            {"number": 2, "at": (0.0, 1.5), "net": "GND", "size": (1.5, 1.5), **hole}])
        g.track((27.4, 14.0), (28.5, 14.0), "VOUT", width=1.0)
        g.track((28.5, 14.0), (28.5, 10.0), "VOUT", width=1.0)
        if shunt:
            g.track((28.5, 10.0), (32.5, 10.0), "VOUT", width=1.0)
            g.two_pin("R10", "10m", (33.0, 10.0), "VOUT", "VOUT_S", package="1206")
            g.track((33.5, 10.0), (36.0, 10.0), "VOUT_S", width=1.0)
        else:
            g.track((28.5, 10.0), (36.0, 10.0), "VOUT", width=1.0)
        if out_connector != "tht":
            g.via((36.0, 13.0), "GND")
    if post_stage:
        g.track((30.5, 14.0), (30.5, 21.5), "VOUT", width=0.6)
        g.footprint("U9", "LT3970", "Package_SO:Fake_MSOP", (31.5, 22.0), [
            {"number": 1, "at": (-1.0, -0.5), "net": "VOUT", "size": (0.6, 0.6), "pinfunction": "VIN"},
            {"number": 2, "at": (1.0, -0.5), "net": "SW9", "size": (0.6, 0.6), "pinfunction": "SW"},
            {"number": 3, "at": (0.0, 0.5), "net": "GND", "size": (0.6, 0.6), "pinfunction": "GND"}])
        g.via((31.5, 23.5), "GND")
        g.track((31.5, 22.5), (31.5, 23.5), "GND", width=0.4)
        g.footprint("L9", "10u", "Inductor_SMD:L_1210_3225Metric", (35.4, 21.5), [
            {"number": 1, "at": (-1.4, 0.0), "net": "SW9", "size": (1.0, 1.2)},
            {"number": 2, "at": (1.4, 0.0), "net": "V3", "size": (1.0, 1.2)}])
        g.track((32.5, 21.5), (34.0, 21.5), "SW9", width=0.6)
    if victim:
        x0, x1, y = victim
        g.footprint("R5", "10k", "Resistor_SMD:R_0402_1005Metric", (x0, y), [
            {"number": 1, "at": (0.5, 0.0), "net": "NRST", "size": (0.5, 0.6)},
            {"number": 2, "at": (-0.5, 0.0), "net": "V33", "size": (0.5, 0.6)}])
        g.footprint("U5", "STM32F030", "Package_SO:Fake_TSSOP", (x1, y), [
            {"number": 4, "at": (-0.6, 0.0), "net": "NRST", "size": (0.4, 0.6), "pintype": "input",
             "pinfunction": "NRST"},
            {"number": 8, "at": (0.6, 0.0), "net": "GND", "size": (0.4, 0.6), "pintype": "power_in",
             "pinfunction": "VSS"}])
        g.via((x1 + 0.6, y + 0.8), "GND")
        g.track((x1 + 0.6, y), (x1 + 0.6, y + 0.8), "GND", width=0.3)
        g.track((x0 + 0.5, y), (x1 - 0.6, y), "NRST", width=0.2)
    if bootstrap:
        g.footprint("U7", "LTC7890", "Package_SO:Fake_TSSOP", (30.0, 26.0), [
            {"number": 1, "at": (-1.0, 0.0), "net": "BST", "size": (0.4, 0.6), "pinfunction": "BOOST"},
            {"number": 2, "at": (0.0, 0.0), "net": "TGU", "size": (0.4, 0.6), "pinfunction": "TGUP"},
            {"number": 3, "at": (1.0, 0.0), "net": "GND", "size": (0.4, 0.6), "pinfunction": "GND"}])
        g.two_pin("C7", "100n", (26.0, 26.0), "BST", "SW", kind="C")
        g.two_pin("R7", "2R", (22.0, 26.0), "TGU", "HG")
    if loop:
        pins = [("SW1", "SW"), ("ITH1", "ITH"), ("V_{FB1}", "FB"), ("SENSE1+", "SP"), ("SENSE1-", "SN"), ("GND", "GND")]
        g.footprint("U8", "LTC7890", "Package_DFN_QFN:Fake_QFN", (8.0, 26.0), [
            {"number": k + 1, "at": (-2.5 + k, 0.0), "net": net, "size": (0.4, 0.6), "pinfunction": fn}
            for k, (fn, net) in enumerate(pins)])
        g.two_pin("RC1", "2.2k", (4.0, 22.0), "ITH", "ITHC")
        g.two_pin("CC1", "22n", (6.0, 22.0), "ITHC", "GND", kind="C")
        g.two_pin("CH1", "22p", (8.0, 22.0), "ITH", "GND", kind="C")
        g.two_pin("RT1", "100k", (12.0, 22.0), "VOUT", "FB")
        g.two_pin("RB1", "25k", (14.0, 22.0), "FB", "GND")
        g.two_pin("RF1", "10R", (34.0, 18.0), "SP", "VL")
        g.two_pin("RF2", "10R", (36.0, 18.0), "SN", "VOUT")
    return g


BR_X = (18.0, 30.0, 42.0)    # the bridge board's leg centres (mirror symmetric about x = 30)


def bridge_board(legs: int = 3, pinfunctions: str = "EPC", sense_ic: bool = True, snubber: bool = True,
                 inductor_on_a: bool = False, load_switch: bool = False, caps_per_leg: int = 1,
                 esc: bool = False, header_gnd_pins: int = 0, unstitched: str = "", phase_shunts: str = "",
                 shunt_net: str = "PH{}_GND", shunt_r: str = "2m", kelvin: bool | str = False) -> BoardGen:
    """A motor bridge, 4 layers, 60 x 34 mm, GND plane on In1: a VBUS strip on F.Cu (x 10-50, y 4-9.6), and per
    leg k (centre x = BR_X[k], phase A / B / C) a high-side FET Q<2k+1> (drain in the strip at y 9, source on the
    PH<phase> pour at y 12), a low-side FET Q<2k+2> (drain on PH<k> at y 18.5, source on an F.Cu GND strip with vias at y 21.5), the PH<k>
    pour between them (x - 2 to x + 1.2, y 11.4-18.6), a driver U<k> with SW / HO / LO / HIN / LIN / GND pins,
    `caps_per_leg` bus capacitors C<10(k+1)+1>.. across the strip's top edge (VBUS pad in the strip, GND pad above with a
    via), an RC snubber C<41+k> + R<41+k> (PH -> SNB -> VBUS, on B.Cu, no copper), and the phase output: a via in the
    pour and a B.Cu track to the motor connector J1, pads A / B / C on B.Cu at y 30.
    pinfunctions: "EPC" (GN / DN / SN, EPC's GaN FETs) or "DSG".
    sense_ic: the bus input J2 (VIN_RAW, GND) at the left reaches VBUS through U9, a current-sense IC (IP+ on
    VBUS, IP- on VIN_RAW, like an ACS71240); else J2.1 is on VBUS.
    inductor_on_a: leg A's output goes through a 0R (R90) and an inductor L90 instead of J1.A (a converter's
    switch node, not a motor leg). load_switch: also a back-to-back FET pair Q90 / Q91 between VBUS and LOAD
    (no FET to ground: not a leg).
    esc: laid out like an ESC: one three-phase driver U1 (SHA / INHA / INLA ...) for all legs, a
    single-pad wire tab per phase (J4 / J5 / J6, pad 1) instead of J1, and the battery on two tabs: J2 on VBUS and
    J3 on IS-, which reaches GND through a 1 mOhm shunt R9 (no sensor IC).
    unstitched: the phases whose low side's GND strip gets no vias (its return reaches the plane through the
    other legs' vias).
    phase_shunts: the phases whose low side is on a net of its own (shunt_net, "PH{}_GND": PHA_GND ...): an F.Cu
    island x - 2 to x + 1.5 under the source pad, with a shunt R<51+k> (shunt_r) at (x + 2, 22.25) to GND and a GND via
    beside it; the strip is then an island per phase (the others on GND with their vias). kelvin: each shunt a
    4-terminal resistor (force pads 1 / 2 as the 2-pad one's, larger; sense pads 3 / 4, small, on PH<k>_SP / PH<k>_SN),
    like a 2512 Kelvin shunt; kelvin="custom": the force pads custom pads whose anchor (0.2 mm) is smaller than the
    sense pads, their copper in a primitive (as a battery shunt's footprint)."""
    g = BoardGen(layers=4, size=(60.0, 34.0))
    g.plane("GND", "In1.Cu")
    g.zone("VBUS", "F.Cu", [(10.0, 4.0), (50.0, 4.0), (50.0, 9.6), (10.0, 9.6)])
    if not phase_shunts:
        g.zone("GND", "F.Cu", [(14.0, 21.0), (46.0, 21.0), (46.0, 23.5), (14.0, 23.5)])
    gn, dn, sn = ("GN", "DN", "SN") if pinfunctions == "EPC" else ("G", "D", "S")
    phases = "ABC"[:legs]
    esc_pads: list[dict] = []
    for k, ph in enumerate(phases):
        x = BR_X[k]
        node = f"PH{ph}"
        g.zone(node, "F.Cu", [(x - 2.0, 11.4), (x + 1.2, 11.4), (x + 1.2, 18.6), (x - 2.0, 18.6)])
        low = shunt_net.format(ph) if ph in phase_shunts else "GND"
        if phase_shunts:                    # an island per phase: the low side's own net, or GND
            g.zone(low, "F.Cu", [(x - 2.0, 21.0), (x + (1.5 if low != "GND" else 2.0), 21.0),
                                 (x + (1.5 if low != "GND" else 2.0), 23.5), (x - 2.0, 23.5)])
        if low != "GND":                    # the phase's shunt to GND, and GND's via to the plane beside it
            if kelvin == "custom":          # force pads as custom pads: a small anchor, the copper in a primitive
                big = [[(-0.3, -0.5), (0.3, -0.5), (0.3, 0.5), (-0.3, 0.5)]]
                g.footprint(f"R{51 + k}", shunt_r, "Resistor_SMD:R_Shunt_Kelvin_Custom", (x + 2.0, 22.25), [
                    {"number": 1, "at": (-0.5, 0.0), "net": low, "size": (0.2, 0.2), "shape": "custom",
                     "primitives": big},
                    {"number": 4, "at": (0.5, 0.0), "net": "GND", "size": (0.2, 0.2), "shape": "custom",
                     "primitives": big},
                    {"number": 2, "at": (-0.5, 0.9), "net": f"PH{ph}_SP", "size": (0.25, 0.4)},
                    {"number": 3, "at": (0.5, 0.9), "net": f"PH{ph}_SN", "size": (0.25, 0.4)}])
            elif kelvin:
                g.footprint(f"R{51 + k}", shunt_r, "Resistor_SMD:R_Shunt_Kelvin", (x + 2.0, 22.25), [
                    {"number": 1, "at": (-0.5, 0.0), "net": low, "size": (0.6, 1.0)},
                    {"number": 2, "at": (0.5, 0.0), "net": "GND", "size": (0.6, 1.0)},
                    {"number": 3, "at": (-0.5, 0.9), "net": f"PH{ph}_SP", "size": (0.3, 0.3)},
                    {"number": 4, "at": (0.5, 0.9), "net": f"PH{ph}_SN", "size": (0.3, 0.3)}])
            else:
                g.two_pin(f"R{51 + k}", shunt_r, (x + 2.0, 22.25), low, "GND", package="1206")
            g.via((x + 2.9, 22.25), "GND")
        for ref, y, d_net, s_net, gate in ((f"Q{2 * k + 1}", 10.5, "VBUS", node, f"G_H{ph}"),
                                           (f"Q{2 * k + 2}", 20.0, node, low, f"G_L{ph}")):
            g.footprint(ref, "EPC2302" if pinfunctions == "EPC" else "SiR500", "Package_GAN:Fake_EPC", (x, y), [
                {"number": 3, "at": (0.0, -1.5), "net": d_net, "size": (2.4, 1.2), "pinfunction": dn},
                {"number": 2, "at": (0.0, 1.5), "net": s_net, "size": (2.4, 1.2), "pinfunction": sn},
                {"number": 1, "at": (2.0, 0.0), "net": gate, "size": (0.6, 0.6), "pinfunction": gn}])
        for vx in (x - 0.8, x + 0.8) if ph not in unstitched and low == "GND" else ():
            g.via((vx, 22.8), "GND")
        drv_pads = [
            {"number": 4, "at": (0.0, 0.0), "net": node, "size": (0.6, 0.6), "pinfunction": f"SH{ph}" if esc else "SW"},
            {"number": 2, "at": (0.0, -1.5), "net": f"G_H{ph}", "size": (0.6, 0.6), "pinfunction": f"GH{ph}" if esc else "HO"},
            {"number": 6, "at": (0.0, 1.5), "net": f"G_L{ph}", "size": (0.6, 0.6), "pinfunction": f"GL{ph}" if esc else "LO"},
            {"number": 12, "at": (1.5, -0.8), "net": f"PWM_H{ph}", "size": (0.4, 0.4),
             "pinfunction": f"INH{ph}" if esc else "HIN", "pintype": "input"},
            {"number": 11, "at": (1.5, 0.8), "net": f"PWM_L{ph}", "size": (0.4, 0.4),
             "pinfunction": f"INL{ph}" if esc else "LIN", "pintype": "input"},
            {"number": 8, "at": (1.5, 0.0), "net": "GND", "size": (0.4, 0.4), "pinfunction": "PGND",
             "pintype": "power_in"}]
        if esc:                     # one driver for all legs: its pads at the same places, numbered per phase
            for pad in drv_pads:
                pad["number"] = pad["number"] + 20 * k
                pad["at"] = (pad["at"][0] + x + 5.0 - 30.0, pad["at"][1])
            esc_pads += drv_pads
        else:
            g.footprint(f"U{k + 1}", "NCP51810", "Package_DFN_QFN:Fake_Driver", (x + 5.0, 15.0), drv_pads)
        g.track((x + 1.2, 15.0), (x + 5.0, 15.0), node, width=0.4)
        g.polyline([(x + 5.0, 13.5), (x + 3.0, 13.5), (x + 3.0, 10.5), (x + 2.0, 10.5)], f"G_H{ph}", width=0.25)
        g.polyline([(x + 5.0, 16.5), (x + 3.0, 16.5), (x + 3.0, 20.0), (x + 2.0, 20.0)], f"G_L{ph}", width=0.25)
        for c in range(caps_per_leg):
            cx = x - 1.0 + 2.0 * c
            g.footprint(f"C{10 * (k + 1) + c + 1}", "15u", "Capacitor_SMD:C_1206_3216Metric", (cx, 3.6), [
                {"number": 1, "at": (0.0, 1.0), "net": "VBUS", "size": (1.0, 1.2)},
                {"number": 2, "at": (0.0, -1.0), "net": "GND", "size": (1.0, 1.2)}])
            g.via((cx, 2.6), "GND")
        if snubber:
            g.two_pin(f"C{41 + k}", "1n", (x + 3.0, 26.0), node, f"SNB{ph}", kind="C", layer="B.Cu")
            g.two_pin(f"R{41 + k}", "1R", (x + 5.0, 26.0), f"SNB{ph}", "VBUS", layer="B.Cu")
        if inductor_on_a and ph == "A":
            g.two_pin("R90", "0R", (x - 1.0, 26.0), node, "PHA_L", layer="B.Cu")
            g.footprint("L90", "4u7", "Inductor_SMD:L_1210_3225Metric", (x - 1.0, 29.0), [
                {"number": 1, "at": (0.0, -1.0), "net": "PHA_L", "size": (1.0, 1.0)},
                {"number": 2, "at": (0.0, 1.0), "net": "VOUT", "size": (1.0, 1.0)}], layer="B.Cu")
        else:
            g.via((x - 1.0, 17.8), node)
            g.track((x - 1.0, 17.8), (x - 1.0, 30.0), node, layer="B.Cu", width=1.0)
            if esc:                 # a wire tab per phase
                g.footprint(f"J{4 + k}", f"PHASE_{ph}", "Connector:Fake_WireTab", (x - 1.0, 30.0), [
                    {"number": 1, "at": (0.0, 0.0), "net": node, "size": (2.0, 2.0)}], layer="B.Cu")
    if esc_pads:
        g.footprint("U1", "DRV8300", "Package_DFN_QFN:Fake_Driver3", (30.0, 15.0), esc_pads)
    j1 = [{"number": ph, "at": (BR_X[k] - 1.0 - 30.0, 0.0), "net": f"PH{ph}", "size": (1.5, 1.5)}
          for k, ph in enumerate(phases) if not (inductor_on_a and ph == "A")]
    if j1 and not esc:
        g.footprint("J1", "Motor", "Connector:Fake_Motor", (30.0, 30.0), j1, layer="B.Cu")
    if esc:                         # battery tabs: + on VBUS, - on IS- through the shunt R9 to GND
        g.footprint("J2", "BAT", "Connector:Fake_WireTab", (4.0, 5.0), [
            {"number": 1, "at": (0.0, 0.0), "net": "VBUS", "size": (1.5, 1.5)}])
        g.polyline([(4.0, 5.0), (10.5, 5.0)], "VBUS", width=1.0)
        g.footprint("J3", "GND", "Connector:Fake_WireTab", (4.0, 9.0), [
            {"number": 1, "at": (0.0, 0.0), "net": "IS-", "size": (1.5, 1.5)}])
        g.track((4.0, 9.0), (5.5, 9.0), "IS-", width=1.0)
        g.two_pin("R9", "1m", (6.0, 9.0), "IS-", "GND", package="1206")
        g.via((6.5, 9.0), "GND")
        sense_ic = None
    plus = "VIN_RAW" if sense_ic else "VBUS"
    if sense_ic is not None:
        g.footprint("J2", "Conn", "Connector:Fake_2pin", (4.0, 6.5), [
            {"number": 1, "at": (0.0, -1.5), "net": plus, "size": (1.5, 1.5)},
            {"number": 2, "at": (0.0, 1.5), "net": "GND", "size": (1.5, 1.5)}])
        g.via((4.0, 8.0), "GND")
    if sense_ic:
        g.footprint("U9", "ACS71240", "Package_DFN_QFN:Fake_Sense", (8.5, 6.8), [
            {"number": 1, "at": (1.3, 0.0), "net": "VBUS", "size": (1.0, 1.0), "pinfunction": "IP+"},
            {"number": 2, "at": (-1.3, 0.0), "net": "VIN_RAW", "size": (1.0, 1.0), "pinfunction": "IP-"},
            {"number": 3, "at": (0.0, 1.4), "net": "GND", "size": (0.4, 0.4), "pinfunction": "GND"},
            {"number": 10, "at": (0.0, -1.4), "net": "+3V3", "size": (0.4, 0.4), "pinfunction": "VCC"},
            {"number": 9, "at": (0.6, -1.4), "net": "ISENSE", "size": (0.4, 0.4), "pinfunction": "VIOUT"}])
        g.polyline([(4.0, 5.0), (7.2, 5.0), (7.2, 6.8)], "VIN_RAW", width=1.0)
        g.via((8.5, 8.2), "GND")
    elif sense_ic is not None:
        g.polyline([(4.0, 5.0), (10.5, 5.0)], "VBUS", width=1.0)
    if load_switch:
        for ref, d_net, s_net, at in (("Q90", "VBUS", "LOAD", (54.0, 6.0)), ("Q91", "OUT", "LOAD", (54.0, 12.0))):
            g.footprint(ref, "SiR500", "Package_SO:Fake_PowerPAK", at, [
                {"number": 5, "at": (0.0, -1.0), "net": d_net, "size": (1.2, 0.8), "pinfunction": "D"},
                {"number": 1, "at": (0.0, 1.0), "net": s_net, "size": (1.2, 0.8), "pinfunction": "S"},
                {"number": 4, "at": (1.5, 0.0), "net": f"{ref}_G", "size": (0.5, 0.5), "pinfunction": "G"}])
    if header_gnd_pins:
        _header(g, "J7", (55.0, 14.0), header_gnd_pins)
    return g


HEADER_PITCH = 2.54


def _header(g: BoardGen, ref: str, at: Pt, gnd_pins: int):
    """A board-to-board header: pins 1..gnd_pins on GND, the next one on +3V3, 2.54 mm apart along y."""
    g.footprint(ref, "Header", "Connector_PinHeader_2.54mm:Fake_1xN", at, [
        {"number": k + 1, "at": (0.0, k * HEADER_PITCH), "net": "GND" if k < gnd_pins else "+3V3",
         "type": "thru_hole", "drill": 1.0, "size": (1.7, 1.7)} for k in range(gnd_pins + 1)])


def distribution_board(pour_w: float = 3.0) -> BoardGen:
    """A passive distribution board: a 2-pin power connector J1 (pin 2 VDD, pin 1 GND) and
    four 4-pin connectors J2-J5 (GND, VDD, CANH, CANL) along a `pour_w` mm VDD pour on F.Cu, a GND plane on B.Cu, a
    100 nF capacitor C1-C4 from VDD to GND by each connector. Every pin is through-hole (1.0 mm drill)."""
    g = BoardGen(layers=2, size=(70.0, 16.0))
    y = 8.0
    tht = {"type": "thru_hole", "drill": 1.0, "size": (1.7, 1.7), "shape": "circle"}
    g.footprint("J1", "Conn_01x02", "Connector:Fake_2pin", (5.0, y), [
        {"number": 1, "at": (0.0, 3.0), "net": "GND", **tht}, {"number": 2, "at": (0.0, 0.0), "net": "VDD", **tht}],
        attrs="through_hole")
    for k, x in enumerate((20.0, 33.0, 46.0, 59.0)):
        g.footprint(f"J{k + 2}", "Conn_01x04", "Connector:Fake_4pin", (x, y), [
            {"number": 1, "at": (-2.54, 0.0), "net": "GND", **tht}, {"number": 2, "at": (0.0, 0.0), "net": "VDD", **tht},
            {"number": 3, "at": (2.54, 0.0), "net": "CANH", **tht},
            {"number": 4, "at": (5.08, 0.0), "net": "CANL", **tht}], attrs="through_hole")
        g.two_pin(f"C{k + 1}", "100n", (x, y - 3.0), "VDD", "GND", kind="C")
    g.zone("VDD", "F.Cu", [(4.0, y - pour_w / 2), (61.0, y - pour_w / 2), (61.0, y + pour_w / 2),
                           (4.0, y + pour_w / 2)])
    g.plane("GND", "B.Cu")
    return g


def logic_board(gnd_pins: int = 1) -> BoardGen:
    """A logic board for stacking above bridge_board(header_gnd_pins=...): 4 layers, 50 x 30 mm, GND plane on In1.
    Placed with its header J1 (at (45, 8)) on the power board's J7 (at (55, 14)): its (x, y) sits at (x + 10, y + 6)
    there, over the bridge. U1 (an STM32) at (15, 15) on B.Cu, its GND pad joined to the plane by a via; its reset
    NRST runs on B.Cu (the side facing the power board) to the pull-up R1 at (7.5, 3), across leg A's phase pour
    below (power x 16-19.2, y 11.4-18.6 is x 6-9.2, y 5.4-12.6 here); HALL_U runs on F.Cu (behind the GND plane)
    from U1 to J2 at (3, 20)."""
    g = BoardGen(layers=4, size=(50.0, 30.0))
    g.plane("GND", "In1.Cu")
    _header(g, "J1", (45.0, 8.0), gnd_pins)
    g.footprint("U1", "STM32G474CET6", "Package_QFP:Fake_LQFP", (15.0, 15.0), [
        {"number": 7, "at": (-1.0, 0.0), "net": "NRST", "size": (0.6, 0.6), "pinfunction": "NRST"},
        {"number": 8, "at": (0.0, 1.0), "net": "GND", "size": (0.6, 0.6), "pinfunction": "VSS"},
        {"number": 9, "at": (1.0, 0.0), "net": "+3V3", "size": (0.6, 0.6), "pinfunction": "VDD"},
        {"number": 10, "at": (0.0, -1.0), "net": "HALL_U", "size": (0.6, 0.6), "pinfunction": "PA0"}],
        layer="B.Cu")
    g.via((15.0, 16.8), "GND")
    g.track((15.0, 16.0), (15.0, 16.8), "GND", layer="B.Cu", width=0.4)
    g.two_pin("R1", "10k", (7.5, 3.0), "NRST", "+3V3", layer="B.Cu", angle=90.0)
    g.polyline([(14.0, 15.0), (7.5, 15.0), (7.5, 3.5)], "NRST", layer="B.Cu", width=0.25)
    g.via((15.0, 13.0), "HALL_U")
    g.track((15.0, 14.0), (15.0, 13.0), "HALL_U", layer="B.Cu", width=0.25)
    g.polyline([(15.0, 13.0), (8.5, 13.0), (8.5, 20.0), (3.0, 20.0)], "HALL_U", layer="F.Cu", width=0.25)
    g.footprint("J2", "Hall", "Connector:Fake_1pin", (3.0, 20.0), [
        {"number": 1, "at": (0.0, 0.0), "net": "HALL_U", "size": (1.2, 1.2)}])
    return g


def boost_board(cin: str = "1u", out_connector: bool = False, loop: bool = False) -> BoardGen:
    """A boost: input connector J1 and input capacitor C1 on a VIN pour, inductor L1 from VIN to the SW pour,
    low-side FET Q1 (SW to GND), boost diode D1 (anode on SW, cathode on VOUT), output capacitor C2 on a VOUT
    pour; GND plane on In1. out_connector: an output connector J2 at the right edge (pin 1 VOUT by a track from
    the VOUT pour, pin 2 GND through a via to the plane).
    loop: a boost controller U8 (pins SW1 on SW, ITH1, V_FB1, SENSE1+ / SENSE1-, GND; analysis/loop.py): J1.1 on
    VB, a 10 mΩ input sense shunt RS from VB to VIN (in series with the inductor) with 10R filters RF1 / RF2 to the
    SENSE pins; on ITH1 10k (RC1) to 10n (CC1) to GND and 100p (CH1) to GND; on V_FB1 290k (RT1) from VOUT and 10k
    (RB1) to GND."""
    g = BoardGen(layers=4, size=(36.0, 24.0))
    g.plane("GND", "In1.Cu")
    g.zone("VIN", "F.Cu", [(6.0, 5.0), (14.0, 5.0), (14.0, 11.0), (6.0, 11.0)])
    g.zone("SW", "F.Cu", [(17.0, 5.0), (22.0, 5.0), (22.0, 14.0), (17.0, 14.0)])
    g.zone("VOUT", "F.Cu", [(25.0, 5.0), (32.0, 5.0), (32.0, 11.0), (25.0, 11.0)])
    g.footprint("J1", "Conn", "Connector:Fake_2pin", (3.0, 8.0), [
        {"number": 1, "at": (0.0, -1.5), "net": "VB" if loop else "VIN", "size": (1.5, 1.5)},
        {"number": 2, "at": (0.0, 1.5), "net": "GND", "size": (1.5, 1.5)}])
    if loop:                                    # J1 -> VB -> the 10 mOhm sense shunt RS -> VIN
        g.track((3.0, 6.5), (4.0, 6.5), "VB", width=0.8)
        g.two_pin("RS", "10m", (4.5, 6.5), "VB", "VIN", package="1206")
        g.track((5.0, 6.5), (6.5, 6.5), "VIN", width=0.8)
    else:
        g.track((3.0, 6.5), (6.5, 6.5), "VIN", width=0.8)
    g.via((3.0, 9.5), "GND")
    g.footprint("C1", cin, "Capacitor_SMD:C_1206_3216Metric", (10.0, 11.3), [
        {"number": 1, "at": (0.0, -0.9), "net": "VIN", "size": (1.6, 1.0)},
        {"number": 2, "at": (0.0, 0.9), "net": "GND", "size": (1.6, 1.0)}])
    g.via((10.0, 13.0), "GND")
    g.track((10.0, 12.2), (10.0, 13.0), "GND", width=0.6)
    g.footprint("L1", "4u7", "Inductor_SMD:L_1210_3225Metric", (15.5, 8.0), [
        {"number": 1, "at": (-1.2, 0.0), "net": "VIN", "size": (1.0, 2.0)},
        {"number": 2, "at": (1.8, 0.0), "net": "SW", "size": (1.0, 2.0)}])
    g.footprint("Q1", "SiR500", "Package_SO:Fake_PowerPAK", (19.5, 15.5), [
        {"number": 5, "at": (0.0, -1.0), "net": "SW", "size": (1.6, 1.2), "pinfunction": "D"},
        {"number": 1, "at": (0.0, 1.0), "net": "GND", "size": (1.6, 1.2), "pinfunction": "S"},
        {"number": 4, "at": (2.2, 0.0), "net": "LG", "size": (0.6, 0.6), "pinfunction": "G"}])
    g.via((19.5, 17.5), "GND")
    g.track((19.5, 16.5), (19.5, 17.5), "GND", width=0.8)
    g.footprint("D1", "SS34", "Diode_SMD:D_SMA", (23.5, 8.0), [
        {"number": 1, "at": (1.5, 0.0), "net": "VOUT", "size": (1.2, 1.6), "pinfunction": "K"},
        {"number": 2, "at": (-1.5, 0.0), "net": "SW", "size": (1.2, 1.6), "pinfunction": "A"}])
    g.footprint("C2", "22u", "Capacitor_SMD:C_1206_3216Metric", (28.5, 11.3), [
        {"number": 1, "at": (0.0, -0.9), "net": "VOUT", "size": (1.6, 1.0)},
        {"number": 2, "at": (0.0, 0.9), "net": "GND", "size": (1.6, 1.0)}])
    g.via((28.5, 13.0), "GND")
    g.track((28.5, 12.2), (28.5, 13.0), "GND", width=0.6)
    if out_connector:
        g.footprint("J2", "Conn", "Connector:Fake_2pin", (34.5, 8.0), [
            {"number": 1, "at": (0.0, -1.5), "net": "VOUT", "size": (1.5, 1.5)},
            {"number": 2, "at": (0.0, 1.5), "net": "GND", "size": (1.5, 1.5)}])
        g.track((31.5, 6.5), (34.5, 6.5), "VOUT", width=0.8)
        g.via((34.5, 9.5), "GND")
    if loop:
        pins = [("SW1", "SW"), ("ITH1", "ITH"), ("V_{FB1}", "FB"), ("SENSE1+", "SP"), ("SENSE1-", "SN"), ("GND", "GND")]
        g.footprint("U8", "LTC3786", "Package_DFN_QFN:Fake_QFN", (8.0, 20.0), [
            {"number": k + 1, "at": (-2.5 + k, 0.0), "net": net, "size": (0.4, 0.6), "pinfunction": fn}
            for k, (fn, net) in enumerate(pins)])
        g.two_pin("RC1", "10k", (4.0, 17.0), "ITH", "ITHC")
        g.two_pin("CC1", "10n", (6.0, 17.0), "ITHC", "GND", kind="C")
        g.two_pin("CH1", "100p", (8.0, 17.0), "ITH", "GND", kind="C")
        g.two_pin("RT1", "290k", (12.0, 17.0), "VOUT", "FB")
        g.two_pin("RB1", "10k", (14.0, 17.0), "FB", "GND")
        g.two_pin("RF1", "10R", (4.0, 3.0), "SP", "VB")
        g.two_pin("RF2", "10R", (6.0, 3.0), "SN", "VIN")
    return g


def layer_change_board(stitch_distance: Optional[float], second_plane_net: str = "GND",
                       decap_distance: Optional[float] = None) -> BoardGen:
    """4-layer: F.Cu trace -> via at x=20 -> B.Cu trace. F.Cu refs In1 (GND), B.Cu refs In2."""
    g = BoardGen(layers=4, size=(40.0, 20.0))
    y = 10.0
    g.ic("U1", (5.0, y), "SIG")
    g.ic("U2", (35.0, y), "SIG", layer="B.Cu")
    g.track((5.0, y), (20.0, y), "SIG", "F.Cu")
    g.via((20.0, y), "SIG")
    g.track((20.0, y), (35.0, y), "SIG", "B.Cu")
    g.plane("GND", "In1.Cu")
    g.plane(second_plane_net, "In2.Cu")
    if stitch_distance is not None:
        g.via((20.0, y + stitch_distance), "GND")
    if decap_distance is not None:
        decaps = [("C1", 20.0 + decap_distance)]
        if second_plane_net != "GND":   # every IC has its own decap, like on a real board
            decaps += [("C2", 7.0), ("C3", 33.0)]
        for ref, x in decaps:
            g.two_pin(ref, "100n", (x, y + 3.0), "GND", second_plane_net, kind="C")
            g.via((x - 0.5, y + 3.0), "GND")
            g.via((x + 0.5, y + 3.0), second_plane_net)
    for x, lay in ((5.0, ("F.Cu", "B.Cu")), (35.0, ("F.Cu", "B.Cu"))):
        g.via((x, y + 1.6), "GND", lay)
    return g


PDN_IC = (20.0, 15.0)


def pdn_board(caps: Sequence[tuple] = (), source: bool = False, size: Pt = (40.0, 30.0),
              cut_ic: bool = False) -> BoardGen:
    """A power-distribution test board: 4 layers 0.2 mm apart, a GND plane on In1.Cu and a VDD plane on In2.Cu over
    the whole board (0.3 mm inset), an IC U1 at PDN_IC with one supply pin (VDD, power_in) and one ground pin, each
    with a via in its pad to its plane. caps: (ref, value, (x, y)) 0402 capacitors VDD | GND with a via in each pad.
    source: a 2-pin connector J1 (VDD, GND) at the left edge, vias in its pads (the rail's supply).
    cut_ic: a second IC U2 whose supply pad has no via (cut off from the VDD plane; its ground pad has one)."""
    g = BoardGen(layers=4, size=size, dielectrics=[0.2, 0.2, 0.2])
    g.plane("GND", "In1.Cu")
    g.plane("VDD", "In2.Cu")
    x, y = PDN_IC
    g.footprint("U1", "MCU", "Package_SO:Fake_IC", (x, y), [
        {"number": 1, "at": (-0.6, 0.0), "net": "VDD", "size": (0.6, 0.8), "pinfunction": "VDD", "pintype": "power_in"},
        {"number": 2, "at": (0.6, 0.0), "net": "GND", "size": (0.6, 0.8), "pinfunction": "GND", "pintype": "power_in"}])
    g.via((x - 0.6, y), "VDD")
    g.via((x + 0.6, y), "GND")
    for ref, value, (cx, cy) in caps:
        g.two_pin(ref, value, (cx, cy), "VDD", "GND", kind="C")
        g.via((cx - 0.5, cy), "VDD")
        g.via((cx + 0.5, cy), "GND")
    if cut_ic:
        g.footprint("U2", "SENSOR", "Package_SO:Fake_IC", (30.0, 22.0), [
            {"number": 1, "at": (-0.6, 0.0), "net": "VDD", "size": (0.6, 0.8), "pinfunction": "VDD",
             "pintype": "power_in"},
            {"number": 2, "at": (0.6, 0.0), "net": "GND", "size": (0.6, 0.8), "pinfunction": "GND",
             "pintype": "power_in"}])
        g.via((30.6, 22.0), "GND")
    if source:
        g.footprint("J1", "Conn", "Connector:Fake_2pin", (3.0, y), [
            {"number": 1, "at": (0.0, -1.3), "net": "VDD", "size": (1.2, 1.2)},
            {"number": 2, "at": (0.0, 1.3), "net": "GND", "size": (1.2, 1.2)}])
        g.via((3.0, y - 1.3), "VDD")
        g.via((3.0, y + 1.3), "GND")
    return g


def safety_board(gap: float = 0.4, layer: str = "F.Cu", cap_value: str = "100n 50V", tvs: bool = True,
                 other_net: str = "GND", mask_open: bool = False, **fields: dict) -> BoardGen:
    """Voltage-aware checks (analysis/safety.py), 4 layers: a +48V track (0.5 mm wide, y = 10, x 5-35) on `layer`
    with a 0.5 mm `other_net` track beside it `gap` mm away edge to edge; a 1206 capacitor C1 (`cap_value`, pads 2 mm
    apart) from +48V to GND; a 0805 0R R1 from +48V to AUX (a net that takes +48V's voltage through it); a connector
    J1 (SIG, GND, an unconnected pin 3), SIG on to R2, with an ESD diode D1 (PESD5V0S1BL) on SIG 2 mm from J1.1
    (`tvs`). mask_open: the two tracks in an opening of the layer's solder mask (bare copper), else under it.
    fields: {ref: {name: value}} symbol fields for the parts."""
    g = BoardGen(layers=4, size=(40.0, 30.0))
    g.track((5.0, 10.0), (35.0, 10.0), "+48V", layer, width=0.5)
    y2 = 10.0 + 0.5 + gap
    g.track((5.0, y2), (35.0, y2), other_net, layer, width=0.5)
    if mask_open and layer in ("F.Cu", "B.Cu"):
        g.mask_rect(f"{layer[0]}.Mask", 4.0, 9.0, 36.0, y2 + 1.0)
    g.footprint("C1", cap_value, "Capacitor_SMD:C_1206_3216Metric", (20.0, 22.0), [
        {"number": 1, "at": (-1.5, 0.0), "net": "+48V", "size": (1.0, 1.6)},
        {"number": 2, "at": (1.5, 0.0), "net": "GND", "size": (1.0, 1.6)}])
    g.two_pin("R1", "0R", (30.0, 22.0), "+48V", "AUX", package="0805")
    g.footprint("J1", "Conn", "Connector:Fake_3pin", (5.0, 25.0), [
        {"number": 1, "at": (0.0, -1.3), "net": "SIG", "size": (1.2, 1.2)},
        {"number": 2, "at": (0.0, 1.3), "net": "GND", "size": (1.2, 1.2)},
        {"number": 3, "at": (0.0, 2.8), "net": "unconnected-(J1-Pad3)", "size": (1.0, 1.0)}])
    g.two_pin("R2", "1k", (15.0, 25.0), "SIG", "GND")
    if tvs:
        g.footprint("D1", "PESD5V0S1BL", "Diode_SMD:D_SOD-323", (7.0, 25.0), [
            {"number": 1, "at": (0.0, -1.3), "net": "SIG", "size": (0.6, 0.6)},
            {"number": 2, "at": (0.0, 1.3), "net": "GND", "size": (0.6, 0.6)}])
    for ref, values in fields.items():
        g.fields(ref, **values)
    return g


def line_board(width: float = 0.3, length: float = 20.0, mask: Optional[float] = 0.0, under: bool = False,
               pair_gap: Optional[float] = None, n_extra: float = 0.0) -> BoardGen:
    """Signal-integrity lines (analysis/si.py), 4 layers (0.2 / 1.0 / 0.2 mm, εr 4.5), a GND plane on In1.Cu over the
    whole board: SIG (or with pair_gap SIG_P) on F.Cu from x = 5 to 5 + length at y = 10, `width` wide. under: SIG's
    own track on B.Cu right under it (the same net elsewhere on its route). pair_gap: SIG_N beside it that far apart
    edge to edge, n_extra mm longer (the extra part runs on straight, uncoupled). mask: the solder mask's thickness
    (0: none; None: not in the stackup)."""
    g = BoardGen(layers=4, size=(10.0 + length + n_extra, 20.0), mask=mask)
    g.plane("GND", "In1.Cu")
    p = "SIG" if pair_gap is None else "SIG_P"
    g.track((5.0, 10.0), (5.0 + length, 10.0), p, "F.Cu", width=width)
    if under:
        g.track((5.0, 10.0), (5.0 + length, 10.0), p, "B.Cu", width=width)
    if pair_gap is not None:
        y = 10.0 + width + pair_gap
        g.track((5.0, y), (5.0 + length + n_extra, y), "SIG_N", "F.Cu", width=width)
    return g


def stub_board() -> BoardGen:
    """A via stub (analysis/si.py): SIG on F.Cu from (5, 10) to a through via at (10, 10), on to (15, 10) on In1.Cu;
    the via's barrel from In1.Cu to B.Cu is unused (1.27 mm with the default stackup); a GND plane on In2.Cu."""
    g = BoardGen(layers=4, size=(20.0, 20.0), mask=0.0)
    g.plane("GND", "In2.Cu")
    g.track((5.0, 10.0), (10.0, 10.0), "SIG", "F.Cu", width=0.2)
    g.via((10.0, 10.0), "SIG")
    g.track((10.0, 10.0), (15.0, 10.0), "SIG", "In1.Cu", width=0.2)
    return g
