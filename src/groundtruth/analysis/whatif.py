"""What-if edits: virtual stitching vias, capacitors and copper fills applied to a copy of the board.

They live in the settings under `modifications:` and are only applied by `groundtruth compare`,
`check/simulate --what-if` and the plugin's what-if switch, never by a plain CI run (CI must judge the real
board). Because they become ordinary board items (Via, Footprint + model, Zone), Tier 1 and Tier 2 need no
special cases.
"""
from __future__ import annotations

import copy
import math

from ..components.models import ComponentModel
from ..components.values import format_si
from ..config.modifications import Modification, ModificationError
from ..config.settings import ComponentEntry, Config
from ..core.board import Board, Footprint, Pad, Via, Zone

WHATIF_PREFIX = "WHATIF"
CAP_PAD_PITCH = 1.0          # mm between the virtual capacitor's pads (0402-like)


def validate_modifications(board: Board, mods: list[Modification]) -> list[str]:
    """What keeps the edits from being applied to this board: nets and layers it does not have (one message
    each, numbered as the edits are)."""
    errors = []
    nets = set(board.nets)
    layers = set(board.copper_layers)
    for k, m in enumerate(mods, 1):
        if m.type in ("via", "fill") and m.net not in nets:
            errors.append(f"modification {k} ({m.type}): net {m.net!r} not on the board")
        if m.type == "cap":
            for n in m.nets:
                if n not in nets:
                    errors.append(f"modification {k} (cap): net {n!r} not on the board")
        if m.type in ("cap", "fill") and m.layer not in layers:
            errors.append(f"modification {k} ({m.type}): layer {m.layer!r} is not a copper layer")
        for l in m.layers:
            if l not in layers:
                errors.append(f"modification {k} (via): layer {l!r} is not a copper layer")
    return errors


def via_span(board: Board, layers: list[str]) -> list[str]:
    """The copper layers a via from layers[0] to layers[-1] passes (in stackup order); all of them when no span is
    given (a through via)."""
    order = board.copper_layers
    if len(layers) >= 2:
        i0, i1 = sorted(order.index(l) for l in (layers[0], layers[-1]))
        return order[i0:i1 + 1]
    return list(order)


def cap_pads(m: Modification) -> list[tuple[float, float]]:
    """Pad centres of a virtual capacitor (pad 1 = nets[0])."""
    a = math.radians(m.angle)
    # half the pad pitch along the part's axis; -sin because KiCad's y grows downwards (angles turn on screen)
    dx, dy = CAP_PAD_PITCH / 2 * math.cos(a), -CAP_PAD_PITCH / 2 * math.sin(a)
    return [(m.at[0] - dx, m.at[1] - dy), (m.at[0] + dx, m.at[1] + dy)]


def apply_modifications(board: Board, config: Config, mods: list[Modification]) -> tuple[Board, Config]:
    """A copy of board and config with every enabled modification added."""
    active = [m for m in mods if m.enabled]
    if not active:
        return board, config
    errors = validate_modifications(board, active)
    if errors:
        raise ModificationError("; ".join(errors))
    # shallow copies with new lists: the real board and settings are never changed, and the lists' new identity
    # makes the board's lookup index rebuild itself (Board._indexes)
    b = copy.copy(board)
    b.vias = list(board.vias)
    b.footprints = list(board.footprints)
    b.zones = list(board.zones)
    b.warnings = list(board.warnings)
    cfg = copy.copy(config)
    cfg.components = dict(config.components)
    for k, m in enumerate(active, 1):
        tag = f"whatif-{k}"
        if m.type == "via":
            b.vias.append(Via(m.at, m.size, m.drill, via_span(b, m.layers), m.net, "through", tag))
        elif m.type == "cap":
            ref = f"{WHATIF_PREFIX}{k}"
            fp = Footprint(ref, format_si(m.C, "F"), "groundtruth:WhatIfCap", m.at, m.angle, m.layer, uuid=tag)
            for n, (pos, net) in enumerate(zip(cap_pads(m), m.nets), 1):
                fp.pads.append(Pad(ref, str(n), pos, m.angle, (0.5, 0.6), "rect", [m.layer], net, "smd",
                                   uuid=f"{tag}-{n}"))
                # each pad gets a via down through the board, as a real decap would have: without it the pad
                # would reach only the planes of its own layer
                b.vias.append(Via(pos, 0.5, 0.25, list(b.copper_layers), net, "through", f"{tag}-via{n}"))
            b.footprints.append(fp)
            cfg.components[ref] = ComponentEntry(ref, fp.value, "WhatIfCap",
                                                 ComponentModel("cap", {"C": m.C, "ESR": m.ESR, "ESL": m.ESL}))
        else:
            b.zones.append(Zone(m.net, [m.layer], {m.layer: [(list(m.polygon), [])]}, (list(m.polygon), []),
                                name=f"what-if {k}", uuid=tag))
    return b, cfg
