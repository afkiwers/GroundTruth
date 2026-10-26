"""The current in the parts' terminal metal, shown with the copper's (the Preferences' "Terminal current").

A part whose terminal is a resistive sheet (its Terminal Sheet Resistance, analysis/simulate.part_lid [A70]) takes
part of a pad's current into its own metal: a FET's lead frame, an inductor's clip. The maps show the board's copper
only, so under such a terminal the copper reads low with a sharp edge where the current moves up into the part (the
user, of Q601's drain tab on F.Cu: "F.CU has still a knive's edge"), and the current seems to vanish there. With the
preference on, a view's maps show the copper's current plus the terminal's above it (simulate.terminal_vectors,
kept per view in `terminal_vectors`), in the copper's units: what flows at that spot, as if the copper carried it.
The figures, checks, tiles and Thermal stay the copper's own; this is the picture only.
"""
from __future__ import annotations

import copy

import numpy as np


def has_terminals(rr) -> bool:
    """The view has terminal sheets carrying current (something to add)."""
    return bool(getattr(rr, "terminal_vectors", None))


def with_terminals(rr):
    """A copy of view `rr` whose maps (sheet_current) and arrows (sheet_vectors) include its terminals' current;
    `rr` itself when it has none. Per node, with the copper's vector V, its magnitude K (|V|, or more where K also
    holds a part that V does not, a switch node's RMS ripple) and the terminal's vector T:
    K' = √(K² + 2·V·T + |T|²), which is |V + T| where K = |V| and keeps the rest of K beside it."""
    terms = getattr(rr, "terminal_vectors", None) or {}
    if not terms:
        return rr
    out = copy.copy(rr)
    current = dict(rr.sheet_current)
    vectors = dict(getattr(rr, "sheet_vectors", None) or {})
    for layer, (ii, jj, tx, ty) in terms.items():
        if layer not in current:
            continue
        K = np.array(current[layer], dtype=float)
        tx, ty = np.asarray(tx, float), np.asarray(ty, float)
        k2 = K[ii, jj] ** 2 + tx * tx + ty * ty
        vec = vectors.get(layer)
        if vec is not None:
            vx, vy = (np.array(c, dtype=np.float32) for c in vec)
            k2 += 2.0 * (vx[ii, jj] * tx + vy[ii, jj] * ty)
            vx[ii, jj] += tx
            vy[ii, jj] += ty
            vectors[layer] = (vx, vy)
        K[ii, jj] = np.sqrt(np.maximum(k2, 0.0))
        current[layer] = K.astype(np.asarray(rr.sheet_current[layer]).dtype, copy=False)
    out.sheet_current = current
    out.sheet_vectors = vectors
    out.terminals_shown = True
    return out
