"""A stack of boards as one board model: a logic board above the power board, joined by board-to-board connectors.

The other board's copper goes where it sits: its layers at their height above (or below) this board, air in the gap
between the facing surfaces. Its parts and nets get its name as prefix (logic:U8, logic:/CPU/NRST), except the nets
a connector pin joins to one of ours: those take our name, since they are one net. Each joined pin becomes a vertical
conductor through the gap, from the other board's pad to ours (its inductance is the pin's), so a ground that crosses
on a few pins shows the voltage across them.

Placement comes from the connectors' pins (by number): the rigid transform (rotation, offset, mirrored or not) that
lays the other board's pins onto ours, in the least-squares sense. A board whose top faces this board is mirrored
(`flip: yes`); `auto` takes whichever fits the pins better (a single row of pins cannot tell, and is taken as not
mirrored).
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np

from ..config.settings import Config, StackBoard
from ..core.board import Board, CopperLayer, DielectricLayer, Footprint, Pad, Stackup, Track, Via, Zone

PIN_DIAMETER_MM = 0.64            # a 2.54 mm header's square pin; a pin through a hole of drill d: d - 0.2 mm


class StackError(ValueError):
    """The stack cannot be built: a board file or connector missing, connectors that share no pin numbers."""
    pass


class _Placement:
    """x' = R·(mirror x) + t, mapping the other board's coordinates (mm) onto ours."""

    def __init__(self, rotation: np.ndarray, offset: np.ndarray, mirrored: bool, residual_mm: float):
        """The transform, and how well it fits (RMS distance of the matched pins, mm)."""
        self.R, self.t, self.mirrored, self.residual_mm = rotation, offset, mirrored, residual_mm
        self.angle = math.degrees(math.atan2(rotation[1, 0], rotation[0, 0]))

    def xy(self, p) -> tuple[float, float]:
        """A point of the other board in our coordinates."""
        x, y = p
        v = self.R @ np.array([-x if self.mirrored else x, y]) + self.t
        return float(v[0]), float(v[1])

    def angle_of(self, a: float) -> float:
        """A part's or pad's angle on the other board, in our coordinates (a mirror reflects it: 180 - a)."""
        return ((180.0 - a) if self.mirrored else a) + self.angle

    def polygon(self, poly):
        """A board polygon (outline, holes) of the other board in our coordinates."""
        outline, holes = poly
        return [self.xy(p) for p in outline], [[self.xy(p) for p in h] for h in holes]


def fit_placement(ours: list[tuple[float, float]], theirs: list[tuple[float, float]], flip: str = "auto"
                  ) -> _Placement:
    """The rigid transform laying `theirs` onto `ours` (matched points), mirrored or not (flip: auto | no | yes)."""
    if len(ours) < 2:
        raise StackError("at least two joined pins are needed to place the other board")
    P, Q = np.asarray(ours, float), np.asarray(theirs, float)
    best = None
    for mirrored in ((False, True) if flip == "auto" else (flip == "yes",)):
        q = Q * np.array([-1.0, 1.0]) if mirrored else Q
        # the Kabsch algorithm: centre both point sets, the SVD of their cross-covariance gives the rotation
        # that best lays one onto the other (least squares), the centroids' difference the offset
        pc, qc = P.mean(axis=0), q.mean(axis=0)
        H = (q - qc).T @ (P - pc)
        U, _, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T
        if np.linalg.det(R) < 0:                  # a proper rotation (the mirror is the explicit option above)
            Vt[1] *= -1
            R = Vt.T @ U.T
        t = pc - R @ qc
        res = float(np.sqrt(np.mean(np.sum((q @ R.T + t - P) ** 2, axis=1))))
        # a tie (one row of pins: both fit) keeps the first, not mirrored
        if best is None or res < best.residual_mm - 1e-6:
            best = _Placement(R, t, mirrored, res)
    return best


def _combined_stackup(ours: Stackup, theirs: Stackup, name: str, side: str, gap: float, mirrored: bool
                      ) -> tuple[Stackup, dict[str, str], str, str]:
    """(the stack's stackup, their layer -> its name in the stack, their layer facing us, our layer facing them).
    z is measured downwards from our top surface (their layers above us have negative z)."""
    t_ours, t_theirs = ours.board_thickness, theirs.board_thickness
    facing_top = (side == "above") == mirrored            # their top (F.Cu) faces us
    rename = {l.name: f"{name}:{l.name}" for l in theirs.copper}
    placed = []
    for l in theirs.copper:
        s = l.z if facing_top else (t_theirs - l.z)       # from their surface facing us, into their board
        z = (-gap - s) if side == "above" else (t_ours + gap + s)
        placed.append((z, CopperLayer(rename[l.name], 0, z, l.thickness), "theirs"))
    placed += [(l.z, CopperLayer(l.name, 0, l.z, l.thickness), "ours") for l in ours.copper]
    placed.sort(key=lambda p: p[0])
    copper = [replace(l, index=k) for k, (_, l, _) in enumerate(placed)]

    def diel(stk: Stackup, a: str, b: str) -> DielectricLayer:
        """The dielectric between two of one board's layers (above the lower index), or a plain one."""
        la, lb = stk.layer(a), stk.layer(b)
        k = min(la.index, lb.index)
        return stk.dielectrics[k] if k < len(stk.dielectrics) else DielectricLayer("dielectric", abs(la.z - lb.z))
    inv = {v: k for k, v in rename.items()}
    dielectrics = []
    for (za, la, wa), (zb, lb, wb) in zip(placed, placed[1:]):
        if wa == wb == "ours":
            dielectrics.append(diel(ours, la.name, lb.name))
        elif wa == wb == "theirs":
            dielectrics.append(diel(theirs, inv[la.name], inv[lb.name]))
        else:
            dielectrics.append(DielectricLayer(f"air gap to {name}", zb - za, 1.0, 0.0))
    facing_theirs = (theirs.copper[0] if facing_top else theirs.copper[-1]).name
    facing_ours = ours.copper[0].name if side == "above" else ours.copper[-1].name
    total = max(l.z for l in copper) - min(l.z for l in copper) + 0.07
    return Stackup(copper, dielectrics, total), rename, facing_theirs, facing_ours


def _pins(board: Board, ref: str) -> dict[str, Pad]:
    """A connector's pads by number. StackError when the board has no such part."""
    fp = board.footprint(ref)
    if fp is None:
        raise StackError(f"connector {ref} not found")
    return {str(p.number): p for p in fp.pads}


def stack_board(board: Board, config: Config) -> tuple[Board, list[str]]:
    """This board with the stack's other boards (config.stack) placed on it: (the stack as one board, notes).
    Without a stack: the board itself."""
    if not config.stack:
        return board, []
    from ..adapters.file_source import load_board
    notes: list[str] = []
    out = board
    for sb in config.stack:
        path = Path(sb.file)
        if not path.is_absolute() and config.base_dir:
            path = Path(config.base_dir) / path
        if not path.exists():
            raise StackError(f"stack board {sb.name}: {path} not found")
        other = load_board(str(path))
        out, more = _merge(out, other, sb)
        notes += more
    return out, notes


def _merge(ours: Board, theirs: Board, sb: StackBoard) -> tuple[Board, list[str]]:
    """`ours` with board `theirs` placed as `sb` says: their pins matched by number on each connector pair, the
    placement fitted to those pins, their nets joined to ours where a pin joins them, everything of theirs
    renamed with the prefix and transformed, and each joined pin added as a via through the air gap."""
    name = sb.name
    pairs, joined = [], {}
    for ref_ours, ref_theirs in sb.connectors:
        a, b = _pins(ours, ref_ours), _pins(theirs, ref_theirs)
        common = sorted(set(a) & set(b), key=lambda n: (len(n), n))
        if not common:
            raise StackError(f"connectors {ref_ours} and {name}:{ref_theirs} have no pin numbers in common")
        for n in common:
            pairs.append((a[n], b[n]))
    place = fit_placement([p.position for p, _ in pairs], [q.position for _, q in pairs], sb.flip)
    notes = [f"stack: {name} placed {sb.side} at a {sb.gap:g} mm gap, turned {place.angle:.1f}°"
             + (", mirrored" if place.mirrored else "") + f", pins within {place.residual_mm:.2f} mm (RMS)"]
    if place.residual_mm > 0.5:
        notes.append(f"stack: {name}'s connector pins do not line up with ours ({place.residual_mm:.2f} mm RMS): "
                     "check the connector pairs and flip")
    for p, q in pairs:
        if q.net and p.net:
            if joined.get(q.net, p.net) != p.net:
                notes.append(f"stack: {name}:{q.net} meets {joined[q.net]} and {p.net} on the connectors: "
                             f"kept {joined[q.net]}")
                continue
            joined[q.net] = p.net
    stackup, layer, facing_theirs, facing_ours = _combined_stackup(ours.stackup, theirs.stackup, name, sb.side,
                                                                   sb.gap, place.mirrored)

    def net(n: str) -> str:
        """Their net's name in the stack: ours where a pin joins it, else prefixed."""
        return "" if not n else joined.get(n, f"{name}:{n}")

    def ref(r: str) -> str:
        """Their reference in the stack, prefixed."""
        return f"{name}:{r}"
    out = replace(ours, stackup=stackup, tracks=list(ours.tracks), vias=list(ours.vias),
                  footprints=list(ours.footprints), zones=list(ours.zones), outline=list(ours.outline),
                  warnings=list(ours.warnings), nets=sorted(set(ours.nets) | {net(n) for n in theirs.nets} - {""}))
    for t in theirs.tracks:
        out.tracks.append(Track(place.xy(t.start), place.xy(t.end), t.width, layer[t.layer], net(t.net),
                                f"{name}:{t.uuid}", t.line, place.xy(t.mid) if t.mid is not None else None))
    for v in theirs.vias:
        out.vias.append(Via(place.xy(v.position), v.size, v.drill, [layer[l] for l in v.layers if l in layer],
                            net(v.net), v.via_type, f"{name}:{v.uuid}", v.line))
    for z in theirs.zones:
        out.zones.append(Zone(net(z.net), [layer[l] for l in z.layers if l in layer],
                              {layer[l]: [place.polygon(p) for p in polys] for l, polys in z.filled.items()
                               if l in layer},
                              place.polygon(z.outline) if z.outline else None, z.is_rule_area, z.name,
                              f"{name}:{z.uuid}", z.line))
    for fp in theirs.footprints:
        nf = Footprint(ref(fp.reference), fp.value, fp.lib_id, place.xy(fp.position), place.angle_of(fp.angle),
                       layer.get(fp.layer, f"{name}:{fp.layer}"), dnp=fp.dnp, exclude_from_sim=fp.exclude_from_sim,
                       uuid=f"{name}:{fp.uuid}", line=fp.line, fields=dict(fp.fields), sheet=fp.sheet,
                       sheet_name=fp.sheet_name, sheet_file=fp.sheet_file)
        for p in fp.pads:
            nf.pads.append(replace(p, footprint=nf.reference, position=place.xy(p.position),
                                   angle=place.angle_of(p.angle), layers=[layer[l] for l in p.layers if l in layer],
                                   net=net(p.net), polygon=place.polygon(p.polygon) if p.polygon else None,
                                   uuid=f"{name}:{p.uuid}"))
        out.footprints.append(nf)
    out.outline += [place.polygon(p) for p in theirs.outline]
    # the pins through the gap: from their pad's facing layer to ours
    for p, q in pairs:
        if not (p.net and q.net) or joined.get(q.net) != p.net:
            continue
        drill = min(p.drill or 0.0, q.drill or 0.0) or 0.0
        d = (drill - 0.2) if drill > 0.4 else PIN_DIAMETER_MM
        top = layer[facing_theirs] if facing_theirs in q.layers else layer[q.layers[0]]
        bottom = facing_ours if facing_ours in p.layers else p.layers[0]
        span = sorted({top, bottom}, key=lambda l: stackup.layer(l).z)
        out.vias.append(Via(p.position, max(d, 0.3), d, span, p.net, "through", f"stack pin {p.pin}-{name}:{q.pin}"))
    return out, notes


def joined_nets(board: Board) -> set[str]:
    """The nets a stack's connector pins join (their pins are vias named 'stack pin ...')."""
    return {v.net for v in board.vias if v.uuid.startswith("stack pin ")}


def facing_layers(stackup: Stackup, name: str) -> Optional[tuple[str, str]]:
    """(their layer, our layer) either side of the air gap to the board `name`."""
    for a, b, d in zip(stackup.copper, stackup.copper[1:], stackup.dielectrics):
        if d.name == f"air gap to {name}":
            return (a.name, b.name) if a.name.startswith(f"{name}:") else (b.name, a.name)
    return None
