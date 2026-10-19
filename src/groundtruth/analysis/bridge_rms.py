"""A motor bridge's RMS currents in the copper: what heats it, from the battery to the motor connector.

The copper is linear, so the current density anywhere is a sum of unit solutions weighted by the drive's currents
(analysis/motor.py). Two kinds of current flow on the board:

- Slow (below f_pwm/2: the electrical frequency and its sidebands): each leg's high-side current hs_k comes from
  the battery's + pin, crosses the leg and leaves at the leg's motor pin; its low-side current ls_k comes from the
  battery's - pin the same way. Six DC solves (hs and ls of three legs) give their unit fields J_c.
- Fast (the PWM harmonics): a leg's switching current circulates in its hot loop (bus capacitors, high side,
  phase node, low side, ground), whose solves at n·f_pwm (n = 1..3) the leg already has (analysis/bridge.py).

The mean square density is then exact for the waveform's spectrum (Parseval's relation [Oppenheim 1997]), without
stepping through time:
    <|J|²> = Σ_cd G_cd J_c·J_d  +  Σ_n ½·Re Σ_kl C_n,kl F_n,k·conj(F_n,l)
with G the slow sources' correlation (mean·mean + the slow lines) and C_n the legs' cross spectrum in band n (the
lines nearest n·f_pwm; what lies above the third band is put in it). The motor's ripple current (the PWM part of
the phase current, out through the motor pin) is left out [A23]: it is small next to the switching current.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from ..emi.pwm_lines import line_spectrum
from ..solver.peec import RouteExcitation, SolverError
from .power import (DC_F, POWER_MARGINS_MM, _area_shares, _region_build, _side, input_path, path_parts, rail_caps,
                    peak_density, sustained)
from .simulate import ModelRun

BANDS = 3                       # PWM harmonics with a field of their own (the leg's solves at n·f_pwm)
SOURCES = ("hs", "ls")


def drive_spectra(w, f_pwm: float, bands: int = BANDS) -> tuple[np.ndarray, dict]:
    """(G, {n: C_n}): the slow sources' correlation matrix over (hs_A, hs_B, hs_C, ls_A, ls_B, ls_C) in A² (ls as
    the unit solves take it: from ground into the leg, -w.ls), and per PWM band n the high sides' cross spectrum
    Σ X_k·conj(X_l) over the band's lines (peak phasors, complex 3×3)."""
    sig = np.hstack([w.hs, -w.ls])
    X, means = [], []
    for c in range(6):
        f, x, m = line_spectrum(w.t, sig[:, c], w.period, w.T, w.params.rise_time, (bands + 0.5) * f_pwm)
        X.append(x)
        means.append(m)
    X, means = np.array(X), np.array(means)
    band = np.rint(f / f_pwm).astype(int) if len(f) else np.zeros(0, int)
    slow = band == 0
    G = np.outer(means, means) + 0.5 * (X[:, slow] @ X[:, slow].conj().T).real
    C = {n: X[:3, band == n] @ X[:3, band == n].conj().T for n in range(1, bands + 1)}
    rest = w._avg(w.hs, True) - means[:3] ** 2 - 0.5 * (np.abs(X[:3]) ** 2).sum(axis=1)
    C[bands] = C[bands] + np.diag(2 * np.maximum(rest, 0.0))
    return G, C


def rms_runs(model, bs) -> tuple[list, list[str]]:
    """The six unit DC solves of a bridge (hs and ls per leg) on a model of their own around the legs, the battery
    connector, the motor pins and the series parts on the way: [(source, leg index, ModelRun)], notes. The region
    widens while a path doesn't close."""
    board, config, b = model.board, model.config, bs.spec
    conn = [board.find_pin(p) for p in b.input_connector] if len(b.input_connector) == 2 else [None, None]
    conn = [p if p is not None and p.net else None for p in conn]
    notes = []
    if conn[0] is None:
        notes.append("RMS currents: no input connector, so the battery's side of the bus is not modelled")
    pads, nets, layers, parts = [], {b.rail, b.ground}, set(), []
    if conn[0] is not None and conn[1] is not None:
        path = input_path(board, config, b.rail, conn[0].net, {b.ground} | {leg.switch_node for leg in b.legs})
        nets |= set(path or []) | {conn[1].net}
        parts += path_parts(board, config, path or [])
    legs = []
    for s in bs.legs:
        out = board.find_pin(s.spec.phase_output) if s.spec.phase_output else None
        if out is None or not out.net:
            notes.append(f"RMS currents: {s.spec.name} has no motor pin, so its phase path is not modelled")
            out = None
        else:
            path = input_path(board, config, s.spec.switch_node, out.net, {b.rail, b.ground})
            nets |= set(path or [s.spec.switch_node])
            parts += path_parts(board, config, path or [])
        nets.add(s.spec.switch_node)
        own = [p for el in s.elements for p in el.a + el.b]
        pads += own + ([out] if out is not None else [])
        layers |= {_side(el.fp) for el in s.elements}
        centre = (float(np.mean([p.position[0] for p in own])), float(np.mean([p.position[1] for p in own])))
        pads += [p for fp in rail_caps(board, config, s.spec, centre) for p in fp.pads]
        legs.append((s, out))
    pads += [p for p in conn if p is not None] + [p for fp in parts for p in fp.pads]
    layers |= {_side(fp) for fp in parts} | {p.layers[0] for p in pads if p.layers}

    def injections(st, s, out, source):
        """The DC unit-current injections for one source of one leg: the high side (battery + to the motor pin
        through the high-side switches) or the low side (the motor pin back to battery - through the low side),
        each pad's share by its copper area (_area_shares), +1 A in, -1 A out in total."""
        hs = source == "hs"
        refs = s.spec.high_side if hs else s.spec.low_side
        els = [el for el in s.elements if el.ref in refs]
        battery = conn[0 if hs else 1]
        inj = []
        if battery is not None:     # battery pin -> the side's bus or ground pads
            inj += _area_shares(st, [battery], _side(board.footprint(battery.footprint)), 1.0)
            inj += [x for el in els for x in _area_shares(st, el.a if hs else el.b, _side(el.fp), -el.weight)]
        if out is not None:         # the side's node pads -> the motor pin
            inj += [x for el in els for x in _area_shares(st, el.b if hs else el.a, _side(el.fp), el.weight)]
            inj += _area_shares(st, [out], _side(board.footprint(out.footprint)), -1.0)
        return inj

    side = _side(bs.legs[0].elements[0].fp)
    runs, failed = [], {}
    for m in POWER_MARGINS_MM:
        st, _ = _region_build(model, [p.position for p in pads], layers, nets, side, m)
        runs, failed = [], {}
        for k, (s, out) in enumerate(legs):
            for source in SOURCES:
                name = f"{source} {s.spec.phase}"
                try:
                    inj = injections(st, s, out, source)
                    if inj:
                        runs.append((source, k, ModelRun(st, RouteExcitation(st.plane, [], injections=inj),
                                                         planned=[DC_F])))
                except SolverError as e:
                    failed[name] = str(e)
        if not any("balance" in msg for msg in failed.values()):
            break
    if runs:
        notes.append(f"RMS currents: the battery-to-motor paths on {st.nodes} nodes at {st.d:.2f} mm (region "
                     f"margin {m:g} mm)")
    for name, msg in failed.items():
        notes.append(f"RMS currents: {name} not modelled: " + ("its copper is not connected within the model "
                                                               "region" if "balance" in msg else msg))
    return runs, notes


def _nearest(sol_freqs, f: float) -> Optional[float]:
    """The solved frequency closest to f on a log scale (None without solves, or for f = 0)."""
    return min(sol_freqs, key=lambda x: abs(math.log(x / f)), default=None) if f > 0 else None


def _resample(grid_to, grid_from, values: np.ndarray) -> np.ndarray:
    """Node values of one grid at the nodes of another (nearest node; 0 outside or off copper)."""
    X, Y = np.meshgrid(grid_to.x0 + np.arange(grid_to.nx) * grid_to.d, grid_to.y0 + np.arange(grid_to.ny) * grid_to.d,
                       indexing="ij")
    i = np.rint((X - grid_from.x0) / grid_from.d).astype(np.int64)
    j = np.rint((Y - grid_from.y0) / grid_from.d).astype(np.int64)
    inside = (i >= 0) & (i < grid_from.nx) & (j >= 0) & (j < grid_from.ny)
    ii, jj = np.where(inside, i, 0), np.where(inside, j, 0)
    return np.where(inside & grid_from.node[ii, jj] & grid_to.node, values[ii, jj], 0.0)


class RmsBasis:
    """A bridge's unit fields, kept so that any drive waveform can weight them: the operating point and every point
    of the sweep (no new solves). legs: the solved leg setups; column(setup): a leg's column in the waveform."""

    def __init__(self, model, bs, legs, column):
        """Collect the unit fields: the slow part from the DC battery-to-motor solves (one per leg and side), the
        fast part per PWM harmonic band from the legs' conducted-band solves near n x f_pwm. Fields are stored as
        current density (A/mm^2 per A: the sheet current over the copper thickness)."""
        self.model, self.f_pwm, self.notes = model, bs.spec.f_pwm, []
        # what failed to solve and is therefore missing from the maps, vias and heat (said, not left out silently:
        # VALIDATION_AUDIT.md round 6, ISSUE-25): a leg's battery-to-motor DC path, a leg's PWM band
        self.missing: list[str] = []
        runs = [(src, column(bs.legs[k]), run) for src, k, run in bs.rms_runs if run.ok().get(DC_F) is not None]
        failed = [f"the {src} battery-to-motor path of {bs.legs[k].spec.name}" for src, k, run in bs.rms_runs
                  if run.ok().get(DC_F) is None]
        if runs and failed:
            self.missing += failed
            self.notes.append(f"RMS currents: {', '.join(failed)} not solved: its current is missing from the maps, "
                              "the vias and the heat")
        self.slow = None                 # (build, source indices into G, {layer: [(Jx, Jy)]}, branch currents)
        if runs:
            st = runs[0][2].build
            sols = [run.ok()[DC_F] for _, _, run in runs]
            fields = {layer: [tuple(f.real * 1e-6 / st.t[k] for f in sol.node_field(k, st.d, True)) for sol in sols]
                      for k, layer in enumerate(st.names)}
            self.slow = (st, [(0 if src == "hs" else 3) + c for src, c, _ in runs], fields,
                         np.array([sol.I_branch.real for sol in sols]))
        else:
            self.notes.append("RMS currents: no battery-to-motor path solved; only the switching currents are shown")
        self.fast = {}                   # band n: (build, columns, {layer: [(Fx, Fy)]}, branch currents)
        for n in range(1, BANDS + 1):
            got, st, lost = [], None, []
            for s in legs:
                sols = s.cond.ok()
                f = _nearest(list(sols), n * self.f_pwm)
                if f is not None and abs(math.log(f / (n * self.f_pwm))) <= 0.05:
                    got.append((column(s), sols[f]))
                    st = s.cond.build
                else:
                    # planned near the band but its solve failed: missing, not merely never asked for
                    tried = _nearest([g for g, v in s.cond.sols.items() if v is None], n * self.f_pwm)
                    if tried is not None and abs(math.log(tried / (n * self.f_pwm))) <= 0.05:
                        lost.append(s.spec.name)
            if lost:
                self.missing += [f"PWM band {n} of {name}" for name in lost]
                self.notes.append(f"RMS currents: PWM band {n} of {', '.join(lost)} not solved: its switching current "
                                  "is missing from the maps, the vias and the heat")
            if got:
                fields = {layer: [tuple(x * 1e-6 / st.t[k] for x in sol.node_field(k, st.d, True))
                                  for _, sol in got]
                          for k, layer in enumerate(st.names)}
                self.fast[n] = (st, [c for c, _ in got], fields, np.array([sol.I_branch for _, sol in got]))

    def evaluate(self, G: np.ndarray, C: dict) -> dict:
        """The RMS current density per layer (A/mm²) for the sources' correlation G and cross spectra C
        (drive_spectra), its peak and sustained values, the vias' and the series parts' RMS currents, the copper
        loss."""
        from ..thermal.materials import COPPER
        from .ipc import via_capacity
        plating = self.model.config.analysis.via_plating_um
        out = {"maps": {}, "grids": {}, "vias": [], "parts": []}
        sq, grids, via_sq = {}, {}, {}
        seg_sq: dict = {}               # every via segment's mean square, per model: its heat for Thermal [A43]
        if self.slow is not None:
            st, idx, fields, I = self.slow
            g = G[np.ix_(idx, idx)]
            for k, layer in enumerate(st.names):
                J = fields[layer]
                sq[layer] = sum(g[a, c] * (J[a][0] * J[c][0] + J[a][1] * J[c][1])
                                for a in range(len(J)) for c in range(len(J)))
                grids[layer] = st.grids[k]
            branch_sq = np.einsum("sb,st,tb->b", I, g, I)
            part_rms: dict[str, list] = {}      # a part's parallel branches (simulate.part_pairs): RMS currents add
            for bi, br in enumerate(st.plane.branches):
                if br.kind == "via":
                    via_sq[br.name] = max(via_sq.get(br.name, (0.0, None))[0], branch_sq[bi]), (br, st)
                    seg_sq[(id(st), bi)] = (seg_sq.get((id(st), bi), (0.0,))[0] + float(branch_sq[bi]), br, st)
                elif br.kind == "part" and branch_sq[bi] > 1e-12:     # a series part carrying current (caps: open)
                    acc = part_rms.setdefault(br.name, [0.0, 0.0])
                    acc[0] += math.sqrt(branch_sq[bi])               # in phase, side by side: the RMS values add
                    acc[1] += 1.0 / max(complex(br.z(DC_F)).real, 1e-12)   # parallel: conductances add
            for name, (rms, g_sum) in part_rms.items():
                if rms ** 2 > 1e-6:
                    out["parts"].append({"name": name, "rms_a": rms, "r_ohm": 1.0 / g_sum})
        for n, (st, cols, fields, I) in self.fast.items():
            cc = C[n][np.ix_(cols, cols)]
            for k, layer in enumerate(st.names):
                F = fields[layer]
                acc = sum(0.5 * (cc[a, c] * (F[a][0] * F[c][0].conj() + F[a][1] * F[c][1].conj())).real
                          for a in range(len(F)) for c in range(len(F)))
                if layer in grids:
                    sq[layer] = sq[layer] + _resample(grids[layer], st.grids[k], acc)
                else:
                    sq[layer], grids[layer] = acc, st.grids[k]
            branch_sq = 0.5 * np.einsum("sb,st,tb->b", I, cc, I.conj()).real
            for bi, br in enumerate(st.plane.branches):
                if br.kind == "via":
                    old, where = via_sq.get(br.name, (0.0, (br, st)))
                    via_sq[br.name] = old + branch_sq[bi], where
                    seg_sq[(id(st), bi)] = (seg_sq.get((id(st), bi), (0.0,))[0] + float(branch_sq[bi]), br, st)
        rho = COPPER.resistivity_20c * 1e3                                 # Ω·mm
        best, best_s = (0.0, "", (0.0, 0.0)), (0.0, "", (0.0, 0.0))
        copper_w = 0.0
        for layer, v in sq.items():
            g = grids[layer]
            dens = np.sqrt(np.maximum(v, 0.0))
            out["maps"][layer] = dens
            out["grids"][layer] = g
            sl = self.model.board.stackup.layer(layer)
            # densities in the copper: each cell's heat over its copper (Grid.copper_area)
            copper_w += float(rho * (np.maximum(v, 0.0) * g.copper_area()).sum() * (sl.thickness if sl else 0.035)
                              * g.d * g.d)
            if not dens.size or dens.max() <= 0:
                continue
            pk = peak_density(dens, g.node, g.d, g)          # over PEAK_MM: converges with the grid [A45]
            i, j = np.unravel_index(int(np.argmax(pk)), pk.shape)
            if pk[i, j] > best[0]:
                best = (float(pk[i, j]), layer, tuple(float(x) for x in g.xy(i, j)))
            sus = sustained(dens, g.node, g.d, g)
            i, j = np.unravel_index(int(np.argmax(sus)), sus.shape)
            if sus[i, j] > best_s[0]:
                best_s = (float(sus[i, j]), layer, tuple(float(x) for x in g.xy(i, j)))
        vias = []
        for name, (v, (br, st)) in via_sq.items():
            a = math.sqrt(max(v, 0.0))
            if a < 0.01 or name.startswith("pad "):         # a part's plated hole carries its pin, not only plating
                continue
            x, y = st.xy(br.a)
            cap = via_capacity(2 * br.radius * 1e3, plating)
            vias.append({"name": name, "current_a": a, "a_mm2": a / (math.pi * 2 * br.radius * plating * 1e-6) * 1e-6,
                         "x": x, "y": y, "capacity_a": cap, "load_pct": 100.0 * a / cap if cap > 0 else None})
        top = max((v["current_a"] for v in vias), default=0.0)
        out["vias"] = sorted((v for v in vias if v["current_a"] >= 0.01 * top), key=lambda v: -v["current_a"])
        # (x, y, layer a, layer b, R ohm, mean square A²) of every via segment that carries current: Thermal puts its
        # I²R into the copper at both ends (the slow and the fast parts on their own models: mean squares add)
        out["via_heat"] = [(*st.xy(br.a), st.names[br.a[0]], st.names[br.b[0]], float(complex(br.z(DC_F)).real), ms)
                           for ms, br, st in seg_sq.values() if ms > 1e-12 and not br.name.startswith("pad ")]
        # the copper's loss counts the barrels too (on the test bridge 16 % of it: large currents through few vias)
        copper_w += sum(r * ms for *_, r, ms in out["via_heat"])
        out["parts"].sort(key=lambda p: -p["rms_a"])
        out.update(peak_a_mm2=best[0], peak_layer=best[1], peak_at=best[2], sustained_a_mm2=best_s[0],
                   sustained_layer=best_s[1], sustained_at=best_s[2], copper_w=copper_w)
        return out
