"""DC rails with loads (the user, of a passive distribution board, a VDD pour from a connector to four connectors:
"how can I simulate a load across the highlighted trace?", with temperatures and the current capacity).

A rail (config DcLoadSpec) is a supply pin, optionally its return pin, and loads, each a pin drawing a fixed
current and optionally its return pin. It is solved as one DC run on the whole-board copper model
(power.dc_board_build: every plane net and the rail's nets on every layer with copper, the stages' DC paths share
it): +I at the source, -I_k at each load on the supply net; with the returns, +I_k at each load's return and -I at
the source's return on the ground net. The solve is linear, so all loads at once is the superposition of each
[Desoer 1969].

What comes out (DcLoadResult, a ConverterResult so the DC views, the thermal solve and the combined view take it
as they take a stage's DC path):
- the current density map with its flow (the result is its own heatmap view), the busiest vias and series parts,
  the copper loss, the resistance as loss / I² (power.dc_summary);
- per load the voltage it sees: the drop on the supply side (source pin to load pin) and on the return side (load
  return to source return), from the node potentials at the pads' own nodes (each pad's nodes weighted as fed);
- issues: current density and via load (as a stage's DC path), and dc_drop when a load sees more than max_drop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..config.settings import DcLoadSpec
from ..core.issues import Issue, resolve_severity
from ..solver.peec import RouteExcitation, SolverError
from .power import (DC_F, SUSTAINED_MM, ConverterResult, _area_shares, _side, dc_board_build, dc_board_of,
                    dc_summary, dc_vector_maps)
from .simulate import BoardModel, ModelRun, StackBuild, terminal_vectors


@dataclass
class DcLoadResult(ConverterResult):
    """A DC rail's result (see the module): the DC path of all its loads (`dc[0]`, with its maps, grids, vectors and
    branch currents as a stage's path has them), each load's voltage drops (`loads`), and its own heatmap fields
    (sheet_current ... = the DC map), so the result is its own view."""
    kind: str = "dcload"
    loads: list = field(default_factory=list)      # [{pin, ret, current_a, supply_mv, return_mv, drop_mv, v_out}]
    max_drop: Optional[float] = None               # V, the rail's limit (dc_drop)
    source_v: Optional[float] = None               # not modelled: the drops are relative (V at the loads = V_src - drop)

    def label(self) -> str:
        """Its name in the Results list."""
        return f"{self.net}  DC load ({self.dc[0]['current_a']:.3g} A)" if self.dc else f"{self.net}  DC load"

    def summary(self) -> dict:
        """As plain data for the JSON report (without the maps)."""
        d = super().summary()
        d["kind"] = "dcload"
        for k in ("sheet_current", "sheet_vectors", "terminal_vectors", "dc_terminal", "layer_grids"):
            d.pop(k, None)
        return d


class DcLoadSetup:
    """A rail's model run for the solver's scheduler (simulate.solve_setups): one DC run, solved once at DC_F, no
    refinement. Duck-types what solve_setups and the memory bookkeeping read of a setup."""
    model_label = "DC"

    def __init__(self, spec: DcLoadSpec, res: DcLoadResult, run: Optional[ModelRun], feeds: dict):
        """`feeds`: pin -> [(node, amps per A of the rail)], the injections by pin (for the drops)."""
        self.spec, self.res, self.fine, self.feeds = spec, res, run, feeds
        self.cavity, self.spectrum_run, self.extra_runs = None, None, []
        self.issues: list[Issue] = []

    def refine(self, run, config) -> list:
        """No refinement: a DC run is solved at DC_F only."""
        return []

    def issue(self, rules, check, message, loc, severity=None, **kw):
        """Add an issue for this rail (unless its check is switched off)."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            self.issues.append(Issue(check, sev, self.spec.name, message, (round(loc[0], 4), round(loc[1], 4)), **kw))


CONNECTOR_PREFIXES = ("J", "P", "X", "CN", "CON")


def rail_candidates(board, rail: str, ground: str = "") -> list[dict]:
    """Every part with a pad on the `rail` net, for the DC loads tab's "Fill from net…" (the user pressed Add on an
    empty form: typing every connector's pins was the hard part): per part its rail pin (REF.PAD), its pin on `ground`
    (the same part's, "" when it has none) and whether it looks like a connector; connectors first, then by
    reference. A part with several rail pads (a 2-pin power connector's paralleled pins) gives its first one."""
    out = []
    for fp in board.footprints:
        if fp.dnp:
            continue
        on_rail = [p for p in fp.pads if p.net == rail]
        if not on_rail:
            continue
        ret = next((p for p in fp.pads if ground and p.net == ground), None)
        ref = fp.reference.upper()
        out.append({"ref": fp.reference, "value": fp.value, "pin": on_rail[0].pin, "ret": ret.pin if ret else "",
                    "connector": ref.startswith(CONNECTOR_PREFIXES) and ref.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")[:1]
                    .isdigit(), "pads": len(fp.pads)})
    return sorted(out, key=lambda c: (not c["connector"], _natural(c["ref"])))


def guess_source(candidates: list[dict]) -> int:
    """The candidate most likely to feed the rail: the connector with the fewest pads (a 2-pin power input among
    4-pin outputs), else the first; -1 without candidates."""
    if not candidates:
        return -1
    conns = [k for k, c in enumerate(candidates) if c["connector"]]
    return min(conns, key=lambda k: candidates[k]["pads"]) if conns else 0


def _natural(ref: str) -> tuple:
    """A reference's sort key: letters, then the number as a number (J2 before J10)."""
    import re
    m = re.match(r"([A-Za-z]*)(\d*)(.*)", ref)
    return (m.group(1).upper(), int(m.group(2)) if m.group(2) else 0, m.group(3))


def prepare_dc_loads(model: BoardModel, specs: list) -> tuple[list, list, list]:
    """Each rail's setup on the whole-board DC model: (setups to solve, results, issues). A rail that can't be set
    up (no board outline, a pin without copper, injections that don't balance) gets a result with its error."""
    setups, results, issues = [], [], []
    if not specs:
        return setups, results, issues
    try:
        st = dc_board_build(model)
    except SolverError as e:
        st, why = None, str(e)
    else:
        why = "the board has no outline (Edge.Cuts): the whole-board DC model needs one"
    for spec in specs:
        res = DcLoadResult(spec.name, spec.source, ", ".join(ld.pin for ld in spec.loads), max_drop=spec.max_drop)
        results.append(res)
        if st is None:
            res.error = why
            continue
        try:
            inj, feeds = _injections(model, st, spec)
            exc = RouteExcitation(st.plane, [], injections=inj)
        except SolverError as e:
            res.error = f"{spec.name}: {e}"
            if "balance" in str(e):
                res.error += (" (the supply or the return is not one connected piece of copper from the source to "
                              "every load: a series part without a model, or a pin on another net?)")
            continue
        res.notes.append(f"DC load modelled on the whole board: {st.nodes} nodes at {st.d:.2f} mm")
        setups.append(DcLoadSetup(spec, res, ModelRun(st, exc, planned=[DC_F]), feeds))
    return setups, results, issues


def _injections(model: BoardModel, st, spec: DcLoadSpec) -> tuple[list, dict]:
    """The rail's injections per A of its total current (+ enters the copper, - leaves), merged, and by pin."""
    total = spec.current
    feeds: dict[str, list] = {}

    def at(pin: str, amps: float):
        """Feed `amps` (per A of the rail) at a pin, spread over its pad's own nodes. A pin named again (two loads
        on one pin, a return shared by loads) adds to what it is fed already, as pin_currents counts it (before
        2026-10-17 the second replaced the first: the injections did not balance and the rail failed)."""
        pad = model.board.find_pin(pin)
        if pad is None:
            raise SolverError(f"pin {pin} is not on the board")
        fp = model.board.footprint(pin.split(".")[0])
        new = _area_shares(st, [pad], _side(fp) if fp is not None else "F.Cu", amps)
        if pin in feeds:
            # the same pad gives the same nodes with the same weights: add the currents node by node
            old = dict(feeds[pin])
            new = [(n, old.get(n, 0.0) + a) for n, a in new]
        feeds[pin] = new
    at(spec.source, 1.0)
    for ld in spec.loads:
        at(ld.pin, -ld.current / total)
    if spec.source_return:
        at(spec.source_return, -1.0)
        for ld in spec.loads:
            at(ld.ret, ld.current / total)
    merged: dict[tuple, float] = {}
    for nodes in feeds.values():
        for n, amps in nodes:
            merged[n] = merged.get(n, 0.0) + amps
    return [(n, a) for n, a in merged.items() if a != 0.0], feeds


def pin_currents(spec: DcLoadSpec) -> dict[str, float]:
    """The current through each pin a rail feeds, A: the source (and its return) the rail's total, each load (and
    its return) its own. A pin named twice carries both."""
    out: dict[str, float] = {}
    for pin, amps in [(spec.source, spec.current), (spec.source_return, spec.current)] + \
            [(ld.pin, ld.current) for ld in spec.loads] + [(ld.ret, ld.current) for ld in spec.loads]:
        if pin:
            out[pin] = out.get(pin, 0.0) + abs(amps)
    return out


def series_of(model: BoardModel, pin: str, amps: float) -> tuple[float, Optional[str]]:
    """(joint + contact resistance of the pin's part, Ω; a note when a connector pin carries NOTE_A or more
    without a contact value). Pins of surface-mount parts too: their values are 0 unless given."""
    from .pins import params_for, series_note
    ref = pin.rsplit(".", 1)[0]
    pp = params_for(model.board, model.config, ref)
    return pp.series, series_note(ref, pin, amps, pp)


def _potential(sol, nodes: list) -> float:
    """A pad's potential per A of the rail: its nodes' potentials weighted by the current each is fed with."""
    w = np.array([abs(a) for _, a in nodes])
    v = np.array([sol.V[n].real for n, _ in nodes])
    if w.sum() <= 0:            # a pin whose feeds cancel (a load on the source pin itself): its nodes' mean
        return float(v.mean())
    return float((v * w).sum() / w.sum())


def finish_dc_load(model: BoardModel, setup: DcLoadSetup):
    """The solved rail's result: its DC path (dc_summary), maps, vectors and branch currents, each load's drops,
    its own heatmap fields, and its issues. Each load draws its set current whatever the drop, the source pin is
    the reference, and the copper is at 20 °C [A42]: at a fixed current a track's drop, loss and density scale with
    1 / its width (tests/test_track_width.py)."""
    res, spec, run = setup.res, setup.spec, setup.fine
    config = model.config
    sol = run.ok().get(DC_F) if run is not None else None
    if sol is None:
        res.error = res.error or f"{spec.name}: the DC solve failed" + (f" ({res.notes[-1]})" if res.notes else "")
        return
    total = spec.current
    name = f"{spec.source} → {len(spec.loads)} load(s)"
    summary, maps = dc_summary(run.build, sol, total, name, config.analysis.via_plating_um)
    res.dc, res.dc_maps = [summary], {name: maps}
    res.dc_grids = {name: dict(zip(run.build.names, run.build.grids))}
    res.dc_vectors = {name: dc_vector_maps(run.build, sol, total)}
    res.dc_terminal = {name: terminal_vectors(run.build, sol, total, density=True)} \
        if isinstance(run.build, StackBuild) else {}
    res.dc_branch = {name: (run.build, np.asarray(sol.I_branch).real * total)}
    if isinstance(run.build, StackBuild):       # the vias' currents, kept for Thermal's via heat [A43]
        db = dc_board_of(run.build)
        res.dc_via = {name: (db, db.via_currents(np.asarray(sol.I_branch).real * total))}
        # how it is excited, to solve it again with the copper at its temperature (thermal.electrothermal)
        res.dc_plan = {"fingerprint": db.fingerprint, "inj": list(run.exc.extra),
                       "feeds": {k: list(v) for k, v in setup.feeds.items()}}
    # each load's voltage: potentials are per A of the rail (sol.V), so x total; supply side and return side
    v_src = _potential(sol, setup.feeds[spec.source]) * total
    v_ret = _potential(sol, setup.feeds[spec.source_return]) * total if spec.source_return else None
    # each fed pin's solder joint and mating contact (analysis/pins.py) in series: I·R more drop, no change to the
    # copper's currents (the model feeds a current); a connector carrying a lot without a contact value: a note
    series = {pin: series_of(model, pin, amps) for pin, amps in pin_currents(spec).items()}
    res.notes += [n for _, n in series.values() if n]
    r_of = {pin: r for pin, (r, _) in series.items()}
    res.loads = []
    for ld in spec.loads:
        pins_mv = (total * r_of.get(spec.source, 0.0) + ld.current * r_of.get(ld.pin, 0.0)) * 1e3
        sup = (v_src - _potential(sol, setup.feeds[ld.pin]) * total) * 1e3 + pins_mv
        back = 0.0
        if v_ret is not None:
            ret_mv = (total * r_of.get(spec.source_return, 0.0) + ld.current * r_of.get(ld.ret, 0.0)) * 1e3
            back = (_potential(sol, setup.feeds[ld.ret]) * total - v_ret) * 1e3 + ret_mv
            pins_mv += ret_mv
        pad = model.board.find_pin(ld.pin)
        x, y = tuple(pad.position) if pad is not None else (0.0, 0.0)
        res.loads.append({"pin": ld.pin, "ret": ld.ret, "current_a": ld.current, "supply_mv": sup,
                          "return_mv": back, "drop_mv": sup + back, "pins_mv": pins_mv, "x": float(x), "y": float(y)})
    # the result is its own view: the DC map (as power.dc_view makes one of a stage's path)
    res.sheet_current = maps
    res.sheet_vectors = res.dc_vectors[name]
    res.terminal_vectors = res.dc_terminal.get(name, {})
    res.layer_grids = res.dc_grids[name]
    res.grid = next(iter(res.layer_grids.values()), None)
    tot = sum(float(m.sum()) for m in maps.values()) or 1.0
    res.layer_share = {l: float(m.sum()) / tot for l, m in maps.items()}
    res.map_frequency = 0.0
    # marked as a DC path's view (power.dc_view does the same): labels, units, the hottest-spot marker, the KPI tiles
    # and the metrics treat it as one; thermal and capacity take it by its kind (thermal._stages)
    res.view_of = name
    res.transfers = [{"name": x["name"], "kind": "via", "layers": list(maps), "x": x["x"], "y": x["y"],
                      "fraction": x["current_a"] / max(total, 1e-12), "distance_mm": 0.0,
                      "current_a": x["current_a"]} for x in summary["vias"]]
    worst = max(res.loads, key=lambda r: r["drop_mv"], default=None)
    res.heat_label = f"current density (A/mm²) at {total:.3g} A DC"
    res.heat_title = (f"{spec.name}: DC load {total:.3g} A from {spec.source}"
                      + (f", worst drop {worst['drop_mv']:.0f} mV at {worst['pin']}" if worst else "")
                      + f", copper loss {summary['loss_w'] * 1e3:.0f} mW")
    pad = model.board.find_pin(spec.source)
    res.location = tuple(pad.position) if pad is not None else (0.0, 0.0)
    res.layer = summary.get("sustained_layer", "")
    res.switch_pins = []
    _issues(setup, config.rules)


def _issues(setup: DcLoadSetup, rules):
    """The rail's issues: current density and via load (as a stage's DC path, power._converter_issues), and the drop
    to a load above the rail's max_drop."""
    res, spec = setup.res, setup.spec
    key = f"dcload:{spec.name}"
    path = res.dc[0]
    worst_via = max(path["vias"], key=lambda v: v["a_mm2"], default=None)
    level = max(path["sustained_a_mm2"], worst_via["a_mm2"] if worst_via else 0.0)
    if level >= rules.current_density_warning:
        sev = "error" if level >= rules.current_density_error else "warning"
        at = path["sustained_at"] if path["sustained_a_mm2"] >= (worst_via or {}).get("a_mm2", 0.0) or not worst_via \
            else (worst_via["x"], worst_via["y"])
        via_txt = (f"; {worst_via['name']} carries {worst_via['current_a']:.2f} A ({worst_via['a_mm2']:.0f} A/mm² in "
                   f"its plating)" if worst_via else "")
        setup.issue(rules, "current_density",
                    f"{spec.name}: DC load {path['current_a']:.3g} A: {path['sustained_a_mm2']:.0f} A/mm² sustained "
                    f"over {SUSTAINED_MM:g} mm on {path['sustained_layer']} at ({path['sustained_at'][0]:.2f}, "
                    f"{path['sustained_at'][1]:.2f}){via_txt}; copper loss {path['loss_w'] * 1e3:.0f} mW",
                    at, severity=sev, layer=path["sustained_layer"], key=f"{key}:dc:{path['name']}",
                    details={k: path[k] for k in ("current_a", "r_mohm", "sustained_a_mm2", "peak_a_mm2")})
    over = [v for v in path["vias"] if (v.get("load_pct") or 0.0) >= rules.via_load_warning]
    if over:
        v = max(over, key=lambda x: x["load_pct"])
        sev = "error" if v["load_pct"] >= rules.via_load_error else "warning"
        setup.issue(rules, "via_overload",
                    f"{spec.name}: DC load: {v['name']} carries {v['current_a']:.2f} A, {v['load_pct']:.0f} % of its "
                    f"IPC-2221 capacity ({v['capacity_a']:.2f} A for a 10 K rise)"
                    + (f"; {len(over) - 1} more via(s) over {rules.via_load_warning:g} %" if len(over) > 1 else ""),
                    (v["x"], v["y"]), severity=sev, key=f"{key}:via:{path['name']}",
                    details={"current_a": v["current_a"], "capacity_a": v["capacity_a"], "load_pct": v["load_pct"]})
    if spec.max_drop is not None:
        bad = [r for r in res.loads if r["drop_mv"] > spec.max_drop * 1e3]
        if bad:
            w = max(bad, key=lambda r: r["drop_mv"])
            setup.issue(rules, "dc_drop",
                        f"{spec.name}: {w['pin']} sees {w['drop_mv']:.0f} mV of drop ({w['supply_mv']:.0f} mV supply, "
                        f"{w['return_mv']:.0f} mV return) at {w['current_a']:.3g} A, above the rail's "
                        f"{spec.max_drop * 1e3:.0f} mV" + (f"; {len(bad) - 1} more load(s) above it" if len(bad) > 1
                                                           else ""),
                        (w["x"], w["y"]), key=f"{key}:drop",
                        details={"drop_mv": w["drop_mv"], "max_drop_mv": spec.max_drop * 1e3, "pin": w["pin"]})
