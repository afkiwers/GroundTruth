"""A motor bridge on the board: its six-step drive (analysis/motor.py) and its legs' hot loops (analysis/power.py),
combined where they meet: the bus capacitors and the input.

Every leg is a power stage of its own for the copper solver (its commutation loop: bus capacitors, high side, phase
node, low side, ground), solved on one model region that holds all legs, their capacitors and the input path, so a
frequency is factorised once for all of them. Its hot-loop current is the leg's high-side switch current from the
drive model, at its largest hard edge (overshoot, ringing, radiated estimate). At the input the legs add: the line
current is the sum over the legs of each one's transfer to the LISN (complex, per A of its loop current) times the
spectrum of its high-side current, which the receiver then reads (emi/pwm_lines.py). Each bus capacitor's ripple
current is its share of every leg's switching current (the legs chop one at a time, so their RMS values add in
power).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np

from ..config.settings import BridgeSpec, simulation_blockers
from ..core.issues import Issue, resolve_severity
from ..emi.conducted import BANDS, LISNS, describe, detector, frequency_range, limit_line
from ..emi.pwm_lines import line_spectrum, receiver, tunings_for
from ..emi.radiation import receiver_bandwidth, thin_rows
from ..solver.peec import RouteExcitation, SolverError
from .motor import DriveError, DriveWaveform, six_step
from .bridge_rms import RmsBasis, drive_spectra, rms_runs
from .power import (LOOP_F, LOOP_F_LO, POWER_MARGINS_MM, REPORT_PER_DECADE, SUSTAINED_MM, ConverterSetup, _loop_model,
                    _side, converter_nets, elements_of, input_path, path_parts, prepare_converter, rail_caps,
                    schematic_note)
from .simulate import BoardModel, ModelRun, RouteResult, _interp_loglog
from .victims import (VictimSet, find_victims, finish_rows, region_points, stage_rows,  # noqa: F401
                      terminations, victim_text)          # victim_text: also bridge.victim_text for the reports


@dataclass
class BridgeResult(RouteResult):
    """A motor bridge's drive and what its legs do together. net = the bridge's name; its legs are ConverterResults
    with `bridge` set."""
    kind: str = "bridge"
    legs: list = field(default_factory=list)             # the legs' stage names
    operating: dict = field(default_factory=dict)        # v_bus, f_pwm, speed, f_e, mode, duty, current asked/reached
    drive: dict = field(default_factory=dict)            # DriveWaveform.stats(), edge currents
    caps: list = field(default_factory=list)             # [{name, rms_a, legs: {phase: share}}] at f_pwm, and the LISN
    conducted: list = field(default_factory=list)
    conducted_worst: Optional[dict] = None
    conducted_standard: str = ""
    ce101: list = field(default_factory=list)            # MIL-STD-461 CE101 lead current (rows: f, i_dbua, limit, margin)
    ce101_worst: Optional[dict] = None
    ce101_limit: str = ""
    worst_leg: dict = field(default_factory=dict)        # overshoot, radiated margin: the worst leg's
    losses: list = field(default_factory=list)           # [{ref, power_w, what}] from the drive (bridge_losses)
    rms: dict = field(default_factory=dict)              # RMS current density, vias, series parts (bridge_rms.py)
    rms_missing: list = field(default_factory=list)      # solves the RMS currents lack (failed): RmsBasis.missing
    sweep: list = field(default_factory=list)            # [{i_phase, speed_rpm, duty, losses, margins, ...}] (sweep)
    victims: list = field(default_factory=list)          # what the switching does to other parts and nets (victims.py)
    waveform: Optional[DriveWaveform] = None             # not in the summary
    location: tuple = (0.0, 0.0)
    view: str = ""                                       # "sweep": the sweep's view of it (sweep_view)

    def label(self) -> str:
        """The bridge's name in the Results tab's view list."""
        if self.view == "sweep":
            return f"{self.net}  sweep (phase current × speed)"
        return f"{self.net}  (drive and bus)"

    def summary(self) -> dict:
        """The result as plain data for the JSON report: without the drive waveform and the RMS maps (large
        arrays the report does not need)."""
        d = super().summary()
        d.pop("waveform", None)
        d["rms"] = {k: v for k, v in self.rms.items() if k not in ("maps", "grids")}
        d["kind"] = "bridge"
        return d


@dataclass
class BridgeSetup:
    """A bridge while Simulate runs: its settings, its result being filled in, the legs' power-stage setups, its
    issues, and the victims (parts and nets) and their probes in the legs' models."""
    spec: BridgeSpec
    res: BridgeResult
    legs: list = field(default_factory=list)             # ConverterSetup per leg
    issues: list = field(default_factory=list)
    t_start: float = 0.0
    victims: Optional[VictimSet] = None
    probes: dict = field(default_factory=dict)           # leg name -> [Probe]: the victim nets' receiver inputs
    rms_runs: list = field(default_factory=list)         # [(source, leg index, ModelRun)]: bridge_rms.rms_runs


def _region(model: BoardModel, b: BridgeSpec, stages) -> tuple[list, set, set]:
    """Points, layers and nets of the model all legs share: every leg's switches and rail capacitors, the input
    connector and the series parts on its way to the bus."""
    board, config = model.board, model.config
    pts, layers, nets = [], set(), set()
    for s in stages:
        els = elements_of(board, s)
        pads = [p for el in els for p in el.a + el.b]
        centre = (float(np.mean([p.position[0] for p in pads])), float(np.mean([p.position[1] for p in pads])))
        pts += [p.position for p in pads]
        layers |= {_side(el.fp) for el in els}
        for fp in rail_caps(board, config, s, centre):
            pts += [p.position for p in fp.pads]
            layers.add(_side(fp))
        nets |= converter_nets(board, config, s, with_input=True)
    if len(b.input_connector) == 2:
        conn = [board.find_pin(p) for p in b.input_connector]
        if all(p is not None and p.net for p in conn):
            pts += [p.position for p in conn]
            layers |= {l for p in conn for l in p.layers[:1]}
            path = input_path(board, config, b.rail, conn[0].net, {b.ground} | {s.switch_node for s in stages})
            for fp in path_parts(board, config, path or []):
                pts += [p.position for p in fp.pads]
                layers.add(_side(fp))
    return pts, layers, nets


def prepare_bridges(model: BoardModel, bridges: list[BridgeSpec]):
    """(bridge setups, leg setups, leg results, bridge results, issues): the drive of each bridge and its legs
    prepared for the solver on one shared region."""
    config = model.config
    bsetups, leg_setups, leg_results, results, issues = [], [], [], [], []
    for b in bridges:
        res = BridgeResult(b.name, "bridge", f"{len(b.legs)} legs", legs=[b.leg_name(leg) for leg in b.legs],
                           plane_capacitance=model.capacitance)
        if b.from_schematic:
            res.notes.append(schematic_note(b))
        results.append(res)
        blockers = simulation_blockers(b)
        if blockers:
            res.error = "not simulated: " + "; ".join(blockers)
            continue
        try:
            w = six_step(b)
        except DriveError as e:
            res.error = str(e)
            continue
        res.waveform = w
        res.notes += w.notes
        stats = w.stats()
        res.drive = dict(stats, edges=[float(e) for e in w.edges])
        res.operating = {"v_bus": b.v_bus, "f_pwm": b.f_pwm, "speed_rpm": b.speed_rpm, "f_e": w.f_e, "mode": b.mode,
                         "pwm": b.pwm, "duty": w.duty, "i_target": abs(w.target), "i_reached": abs(w.mean),
                         "reached": w.reached}
        if not w.reached:
            sev = resolve_severity("bridge_operating_point", config.rules.severity)
            if sev != "off":
                issues.append(Issue("bridge_operating_point", sev, b.name, f"{b.name}: {w.notes[-1]}", (0.0, 0.0),
                                    key=f"bridge:{b.name}:op"))
        on = w.duty - (b.dead_time / w.T if b.pwm != "bipolar" else 0.0)
        stages = [replace(s, i_out=max(float(w.edges[k]), 1e-3), duty=min(max(on, 0.01), 0.99))
                  for k, s in enumerate(b.stages())]
        region = _region(model, b, stages)
        bs = BridgeSetup(b, res, t_start=time.time())
        for s in stages:
            setup, lres, li = prepare_converter(model, s, region)
            leg_results.append(lres)
            issues.extend(li)
            if setup is not None:
                bs.legs.append(setup)
                leg_setups.append(setup)
        if bs.legs:
            res.location = bs.legs[0].res.location
            bs.rms_runs, notes = rms_runs(model, bs)
            res.notes += notes
            bs.legs[0].dc_runs += [(f"RMS {src} {k}", 1.0, run) for src, k, run in bs.rms_runs]   # solved with it
            _victim_runs(model, bs, region)
            bsetups.append(bs)
        else:
            res.error = "no leg could be modelled (see the legs' errors)"
    return bsetups, leg_setups, leg_results, results, issues


def _victim_runs(model: BoardModel, bs: BridgeSetup, region: tuple):
    """Each leg's hot loop once more, on a model that also reaches the victims (their ground pads and nets, the
    reference pin, a stack's connector pins), at the two hot-loop frequencies."""
    b = bs.spec
    if not model.config.victims.enabled:
        return
    bs.victims = victims = find_victims(model.board, model.config, b.stages(), b.rail, b.ground,
                                        b.input_connector)
    bs.res.notes += victims.notes
    if not (victims.parts or victims.nets):
        bs.res.notes.append("victims: no microcontroller, reset supervisor or sensitive net found (list them in the "
                            "settings' victims: section)")
        return
    pts, layers = region_points(model.board, victims, b.ground)
    pts, layers = list(region[0]) + pts, set(region[1]) | layers
    nets = set(region[2]) | {b.ground} | set(victims.nets)
    for k, s in enumerate(bs.legs):
        try:
            st, exc, _, _, _ = _loop_model(model, s.spec, s.elements, pts, layers, nets, _side(s.elements[0].fp),
                                           POWER_MARGINS_MM[:1])
            branches, probes, notes = terminations(st, model.board, model.config, victims, b.ground)
            exc = RouteExcitation(exc.stack.with_branches(branches), exc.trace, injections=exc.extra)
        except SolverError as e:
            bs.res.notes.append(f"victims: {s.spec.name}: {e}")
            continue
        if k == 0:
            bs.res.notes += notes
        bs.probes[s.spec.name] = probes
        s.victim_run = ModelRun(st, exc, planned=[LOOP_F_LO, LOOP_F])


def rms_view(b: BridgeResult, leg):
    """A copy of one of the bridge's leg results whose heatmap shows the bridge's RMS current density (for the
    plugin and the HTML), as power.dc_view does for a DC path. None without RMS maps."""
    import copy
    r = b.rms
    if not r.get("maps"):
        return None
    v = copy.copy(leg)
    v.net = b.net
    v.sheet_current = r["maps"]
    v.sheet_vectors = {}
    v.layer_grids = r["grids"]
    v.grid = next(iter(r["grids"].values()))
    tot = sum(float(m.sum()) for m in r["maps"].values()) or 1.0
    v.layer_share = {l: float(m.sum()) / tot for l, m in r["maps"].items()}
    v.map_frequency = 0.0
    top = max(b.drive.get("phase_rms") or [1.0])
    v.transfers = [{"name": x["name"], "kind": "via", "layers": list(r["maps"]), "x": x["x"], "y": x["y"],
                    "fraction": x["current_a"] / max(top, 1e-12), "distance_mm": 0.0,
                    "current_a": x["current_a"], "rms": True} for x in r["vias"]]
    v.heat_label = f"RMS current density (A/mm²), six-step at {b.operating.get('i_target', 0):g} A"
    v.heat_title = f"{b.net}: RMS currents, battery to motor ({r['copper_w']:.2f} W in the copper)"
    v.view_of = "RMS"
    v.rms_of = b
    return v


def _complex_transfer(setup: ConverterSetup, ce101: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """(frequencies, LISN current per A of loop current, complex) of a leg's solves: the conducted band's, or
    with ce101 the CE101 solves' (power.ce101_points: the LISN branch is the CE101 lead there)."""
    if setup.lisn is None:
        return np.zeros(0), np.zeros(0, complex)
    sols = {**setup.fine.ok(), **setup.cond.ok()}
    low = set(setup.ce101_freqs)
    fs = np.array(sorted(f for f in sols if (f in low) == ce101))
    return fs, np.array([complex(sols[f].I_branch[setup.lisn]) for f in fs])


def _ce101_complex_transfer(setup: ConverterSetup, top: float) -> tuple[np.ndarray, np.ndarray]:
    """A leg's complex CE101 transfer up to `top` (Hz): its CE101 solves below 10 kHz and, where the limit goes on
    (MIL-STD-461H, ships: to 20 kHz), the conducted band's from 10 kHz (as power.ce101_transfer) [A34]."""
    from ..emi.conducted import CE101_RANGE
    fs, ts = _complex_transfer(setup, ce101=True)
    if top <= CE101_RANGE[1]:
        return fs, ts
    up, tu = _complex_transfer(setup)
    keep = (up >= CE101_RANGE[1]) & (up <= top * 1.5)
    f = np.concatenate([fs, up[keep]])
    order = np.argsort(f)
    return f[order], np.concatenate([ts, tu[keep]])[order]


def _interp_complex(f: np.ndarray, fs: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """A complex transfer at frequencies f from its solved values ts at fs: the magnitude interpolated in log-log
    (straight for a power law), the phase (unwrapped, so it does not jump by 2 pi) in log frequency."""
    mag = _interp_loglog(f, list(fs), np.abs(ts))
    ph = np.interp(np.log(f), np.log(fs), np.unwrap(np.angle(ts)))
    return mag * np.exp(1j * ph)


def _column(s: ConverterSetup, legs: list) -> int:
    """The leg's column in the drive waveform (A, B, C)."""
    return "ABC".index(s.spec.phase) if s.spec.phase in "ABC" else min(legs.index(s), 2)


def finish_bridge(model: BoardModel, bs: BridgeSetup) -> BridgeResult:
    """The legs (finished) combined: conducted emission at the input, the capacitors' ripple currents, the worst
    leg's overshoot and radiated margin."""
    res, b, w = bs.res, bs.spec, bs.res.waveform
    legs = [s for s in bs.legs if s.res.error == ""]
    if not legs:
        res.error = "no leg was solved"
        return res
    # capacitors: each leg's switching current (its high side's AC part) shared at f_pwm; the legs switch one at a
    # time, so their contributions add in power
    hs_ac = w.hs_ac_rms()
    caps: dict[str, dict] = {}
    for s in legs:
        for c in s.res.caps:
            e = caps.setdefault(c["name"], {"name": c["name"], "lisn": c.get("lisn", False), "legs": {}, "p": 0.0})
            share = c.get("share_sw")
            if share is not None:
                e["legs"][s.spec.phase] = share
                e["p"] += (share * hs_ac[_column(s, legs)]) ** 2
    res.caps = sorted(({"name": e["name"], "lisn": e["lisn"], "rms_a": math.sqrt(e["p"]), "legs": e["legs"]}
                       for e in caps.values()), key=lambda e: -e["rms_a"])
    # a leg without a switching edge at this operating point (a locked rotor drives one commutation step: one leg
    # held low, one off) has no hot-loop event to check; its solves stay for the sweep's other speeds
    top = max(max(w.edges), 1e-12)
    idle = [s for s in legs if w.edges[_column(s, legs)] < 0.01 * top]
    for s in idle:
        s.issues = [i for i in s.issues if i.check not in ("hot_loop", "emi_margin")]
    if idle:
        res.notes.append(f"leg(s) {', '.join(s.spec.phase for s in idle)} do not switch at this operating point: "
                         "their hot-loop checks are left out")
    # the worst leg
    over = max(legs, key=lambda s: s.res.overshoot_v or 0.0)
    res.worst_leg = {"overshoot_v": over.res.overshoot_v, "overshoot_leg": over.spec.name,
                     "loop_nh": max((s.res.loop_nh or 0.0) for s in legs)}
    rad = [s for s in legs if s.res.worst]
    if rad:
        wr = min(rad, key=lambda s: s.res.worst["margin_db"])
        res.worst_leg.update(radiated=wr.res.worst, radiated_leg=wr.spec.name)
        res.worst = wr.res.worst
    _conducted(model, bs, legs)
    _ce101(model, bs, legs)
    _victims(model, bs, legs)
    basis = RmsBasis(model, bs, legs, lambda s: _column(s, bs.legs))
    res.rms = basis.evaluate(*drive_spectra(w, b.f_pwm))
    res.rms_missing = list(basis.missing)
    res.notes += basis.notes
    _rms_issues(model, bs)
    res.losses, notes = bridge_losses(model.board, model.config, b, w, res.caps, res.rms["parts"])
    res.notes += notes
    res.notes.append(f"six-step {b.pwm}{' synchronous' if b.synchronous else ''}, duty {w.duty:.3f}, "
                     f"{w.n} PWM periods per electrical period (f_e {w.f_e:.4g} Hz)")
    if b.sweep_i_phase or b.sweep_speed_rpm:
        res.sweep = sweep(model, bs, legs, basis)
        res.notes.append(f"sweep: {len(res.sweep)} operating points from this operating point's solves (the copper "
                         "is linear); L·di/dt and the radiated margin scaled with the largest switched current")
    res.seconds = time.time() - bs.t_start
    return res


def sweep_view(b: BridgeResult) -> Optional[BridgeResult]:
    """The bridge's sweep as a view of its own for the Results tab (None without a sweep)."""
    return replace(b, view="sweep") if b.sweep and not b.error else None


def caps_at(b: BridgeSpec, caps: list, w: DriveWaveform) -> list[dict]:
    """The capacitors' ripple currents for another drive waveform: each one's shares of the legs' switching
    currents at f_pwm, added in power as finish_bridge does."""
    phases = [leg.phase for leg in b.legs]
    hs_ac = w.hs_ac_rms()

    def col(ph):
        """A leg's column in the drive waveform, by its phase (A, B, C), else its position."""
        return "ABC".index(ph) if ph in ("A", "B", "C") else min(phases.index(ph), 2)
    return [dict(c, rms_a=math.sqrt(sum((share * hs_ac[col(ph)]) ** 2 for ph, share in c["legs"].items())))
            for c in caps]


def sweep(model: BoardModel, bs: BridgeSetup, legs: list, basis: RmsBasis) -> list[dict]:
    """The bridge at every phase current × speed of its sweep, from the solves of its operating point: the drive
    again, the RMS currents re-weighted (basis), the capacitors' shares, the losses, the conducted reading; the
    hot loop's L·di/dt and radiated margin scaled with the largest switched current (both ∝ the edge current)."""
    b, res, w0 = bs.spec, bs.res, bs.res.waveform
    board, config = model.board, model.config
    std = legs[0].standard
    conducted = std != "none" and frequency_range(std, config.analysis.conducted_class) is not None
    edge0 = max(max(w0.edges), 1e-12)
    wl = res.worst_leg
    rows = []
    for rpm in b.sweep_speed_rpm or [b.speed_rpm or 0.0]:
        for i in b.sweep_i_phase or [b.i_phase]:
            row: dict = {"i_phase": float(i), "speed_rpm": float(rpm)}
            rows.append(row)
            spec = replace(b, i_phase=i, speed_rpm=rpm)
            try:
                w = six_step(spec)
            except DriveError as e:
                row["error"] = str(e)
                continue
            r = basis.evaluate(*drive_spectra(w, b.f_pwm))
            caps = caps_at(b, res.caps, w)
            losses, _ = bridge_losses(board, config, spec, w, caps, r["parts"])
            stats = w.stats()
            ratio = max(w.edges) / edge0
            top = max(losses, key=lambda x: x["power_w"], default=None)
            row.update(duty=w.duty, reached=w.reached, i_reached=abs(w.mean), phase_rms=max(stats["phase_rms"]),
                       bus_ac_rms=stats["bus_ac_rms"], loss_w=sum(x["power_w"] for x in losses),
                       worst_part=top["ref"] if top else "", worst_part_w=top["power_w"] if top else 0.0,
                       cap_rms=max((c["rms_a"] for c in caps if not c.get("lisn")), default=0.0),
                       copper_w=r["copper_w"], sustained_a_mm2=r["sustained_a_mm2"],
                       via_a=r["vias"][0]["current_a"] if r["vias"] else 0.0, edge_a=max(w.edges))
            if wl.get("overshoot_v") is not None:
                row["overshoot_v"] = wl["overshoot_v"] * ratio
            if wl.get("radiated"):
                row["radiated_margin_db"] = wl["radiated"]["margin_db"] - 20 * math.log10(max(ratio, 1e-12))
            if conducted:
                worst = min((x for x in conducted_rows(model, legs, w, std) if x["margin_db"] is not None),
                            key=lambda x: x["margin_db"], default=None)
                if worst is not None:
                    row.update(conducted_margin_db=worst["margin_db"], conducted_f=worst["f"])
            ce = ce101_spec(model, w) if any(s.ce101_freqs for s in legs) else None
            if ce is not None:          # CE101: the commutation lines move with the speed
                worst = min((x for x in ce101_bridge_rows(model, legs, w, ce) if x["margin_db"] is not None),
                            key=lambda x: x["margin_db"], default=None)
                if worst is not None:
                    row.update(ce101_margin_db=worst["margin_db"], ce101_f=worst["f"])
    return rows


def bridge_losses(board, config, b: BridgeSpec, w: DriveWaveform, caps: list, parts: list = ()
                  ) -> tuple[list[dict], list[str]]:
    """[{"ref", "power_w", "what"}] of a bridge's parts from its drive waveform, and notes on what is missing:
    per FET (parallel FETs share the current) the channel's R_DS(on)·(i/n)², reverse conduction v_sd·|i|/n, and
    at a hard turn-on ½·Δv·I·t_r and the output charge E_oss·(Δv/V)² (E_oss = Q_oss·V, or C_oss·V², with Q_oss and
    C_oss of one side's parallel switches together, as the form and the ringing take them; shared by the
    switches turning on); a snubber's resistor ½·C·Δv² per node swing; a capacitor's ESR·I_rms²; the series
    parts on the battery and motor paths R·I_rms² (parts: bridge_rms's [{name, rms_a, r_ohm}])."""
    from ..components.defaults import suggest_model
    out: dict[str, dict] = {}
    notes = []

    def add(ref: str, watts: float, what: str):
        """Add a loss to a part, with what causes it (several causes per part add up)."""
        if watts <= 0 or board.footprint(ref) is None:
            return
        e = out.setdefault(ref, {"ref": ref, "power_w": 0.0, "what": []})
        e["power_w"] += float(watts)
        e["what"].append(what)

    def model(ref: str):
        """A part's model: the settings', else the suggested one."""
        fp = board.footprint(ref)
        return config.model_for(ref) or (suggest_model(fp) if fp is not None else None)

    V, p = w.params.v, w.params
    cond, sw = w.conduction(), w.switching()
    r_hs = b.rds_on
    r_ls = b.rds_on_low if b.rds_on_low is not None else b.rds_on
    if r_hs is None:
        notes.append(f"{b.name}: no rds_on, so the switches' conduction loss is left out")
    if b.q_oss is not None:
        e_oss, how = b.q_oss * V, "Q_oss·V"
    elif b.c_oss is not None:
        e_oss, how = b.c_oss * V * V, "C_oss·V²"
    else:
        e_oss, how = 0.0, ""
        notes.append(f"{b.name}: no q_oss or c_oss, so the output-charge loss is left out")
    for j, leg in enumerate(b.legs[:3]):
        k = "ABC".index(leg.phase) if leg.phase in ("A", "B", "C") else j
        for side, refs, r in (("hs", leg.high_side, r_hs), ("ls", leg.low_side, r_ls)):
            n = max(len(refs), 1)
            for ref in refs:
                if r is not None:
                    add(ref, r * cond[side + "_sq"][k] / n ** 2,
                        f"conduction {math.sqrt(cond[side + '_sq'][k]):.3g} A RMS")
                add(ref, p.v_sd * cond[side + "_rev"][k] / n, f"reverse conduction {p.v_sd:g} V")
                add(ref, 0.5 * p.rise_time * sw[side + "_vi"][k] / n, "switching, hard turn-on")
                if e_oss:
                    add(ref, e_oss * sw[side + "_dv2"][k] / (V * V) / n, f"output charge {how}")
        if len(leg.snubber) == 2:
            m = model(leg.snubber[0])
            c = m.capacitance() if m is not None and m.kind == "cap" else 0.0
            if c > 0:
                add(leg.snubber[1], 0.5 * c * sw["node_dv2"][k], f"snubber ½·C·ΔV², {c * 1e9:.3g} nF")
            else:
                notes.append(f"{leg.snubber[0]}: no capacitance known, so the snubber loss is left out")
    for c in caps:
        if c.get("lisn"):
            continue
        m = model(c["name"])
        esr = m.get("ESR") if m is not None and m.kind == "cap" else 0.0
        add(c["name"], esr * c["rms_a"] ** 2, f"ESR {esr * 1e3:.3g} mΩ at {c['rms_a']:.3g} A RMS")
    for part in parts:
        m = model(part["name"])
        if m is not None and m.kind == "short":     # its resistance is a numerical stand-in
            notes.append(f"{part['name']} carries {part['rms_a']:.3g} A RMS but is modelled as a short: give it a "
                         "resistor model (its R_DS(on), shunt or sensor resistance) for its loss")
        else:
            add(part["name"], part["r_ohm"] * part["rms_a"] ** 2, f"series part at {part['rms_a']:.3g} A RMS")
    if out:
        notes.append(f"{b.name}: turn-off counted soft (GaN), the gate drive's loss is left out")
    rows = [dict(e, what=", ".join(e["what"])) for e in out.values()]
    return sorted(rows, key=lambda r: -r["power_w"]), notes


def _rms_issues(model: BoardModel, bs: BridgeSetup):
    """current_density and via_overload from the RMS currents, as a power stage's DC paths raise them."""
    rules, res, b = model.config.rules, bs.res, bs.spec
    r = res.rms
    sev = resolve_severity("dc_unsolved", rules.severity, "warning")
    if res.rms_missing and sev != "off":
        bs.issues.append(Issue(
            "dc_unsolved", sev, b.name,
            f"{b.name}: {', '.join(res.rms_missing)} could not be solved: the RMS currents, the vias' load and "
            "Thermal's heat leave it out; simulate again (another resolution in Preferences)", (0.0, 0.0),
            key=f"power:{b.name}:rms_unsolved", details={"missing": list(res.rms_missing)}))
    if not r.get("maps"):
        return
    worst_via = max(r["vias"], key=lambda v: v["a_mm2"], default=None)
    level = max(r["sustained_a_mm2"], worst_via["a_mm2"] if worst_via else 0.0)
    if level >= rules.current_density_warning:
        sev = resolve_severity("current_density", rules.severity,
                               "error" if level >= rules.current_density_error else "warning")
        on_copper = not worst_via or r["sustained_a_mm2"] >= worst_via["a_mm2"]
        at = r["sustained_at"] if on_copper else (worst_via["x"], worst_via["y"])
        via_txt = (f"; {worst_via['name']} carries {worst_via['current_a']:.2f} A RMS ({worst_via['a_mm2']:.0f} "
                   "A/mm² in its plating)" if worst_via else "")
        if sev != "off":
            bs.issues.append(Issue(
                "current_density", sev, b.name,
                f"{b.name}: RMS current {r['sustained_a_mm2']:.0f} A/mm² sustained over {SUSTAINED_MM:g} mm on "
                f"{r['sustained_layer']} at ({r['sustained_at'][0]:.2f}, {r['sustained_at'][1]:.2f}){via_txt} "
                f"(six-step drive at {b.i_phase:g} A)", (round(at[0], 4), round(at[1], 4)),
                layer=r["sustained_layer"] if on_copper else "", key=f"power:{b.name}:rms",
                details={"sustained_a_mm2": r["sustained_a_mm2"], "peak_a_mm2": r["peak_a_mm2"]}))
    over = [v for v in r["vias"] if (v.get("load_pct") or 0.0) >= rules.via_load_warning]
    if over:
        v = max(over, key=lambda x: x["load_pct"])
        sev = resolve_severity("via_overload", rules.severity,
                               "error" if v["load_pct"] >= rules.via_load_error else "warning")
        if sev != "off":
            bs.issues.append(Issue(
                "via_overload", sev, b.name,
                f"{b.name}: {v['name']} carries {v['current_a']:.2f} A RMS, {v['load_pct']:.0f} % of its IPC-2221 "
                f"capacity ({v['capacity_a']:.2f} A for a 10 K rise)"
                + (f"; {len(over) - 1} more via(s) over {rules.via_load_warning:g} %" if len(over) > 1 else ""),
                (round(v["x"], 4), round(v["y"], 4)), key=f"power:{b.name}:via",
                details={"current_a": v["current_a"], "capacity_a": v["capacity_a"], "load_pct": v["load_pct"]}))


def conducted_rows(model: BoardModel, legs: list, w: DriveWaveform, std: str) -> list[dict]:
    """The receiver's readings [{f, v_dbuv, limit_dbuv, margin_db}] of the legs' line currents for a drive
    waveform: each leg's high-side spectrum times its transfer to the LISN, added."""
    a = model.config.analysis
    rng = frequency_range(std, a.conducted_class)
    contributions, freqs = [], None
    for s in legs:
        fs, ts = _complex_transfer(s)
        if len(fs) < 2:
            continue
        f, ph, _ = line_spectrum(w.t, w.hs[:, _column(s, legs)], w.period, w.T, w.params.rise_time, rng[1] * 1.05)
        sel = f >= rng[0] / 1.1
        freqs = f[sel]                   # the same line grid for every leg (one drive waveform)
        contributions.append(ph[sel] * _interp_complex(freqs, fs, ts))
    if not contributions:
        return []
    z, frac = LISNS[std].impedance(freqs)
    v = sum(contributions) * z * frac
    bands = BANDS[std]

    def rbw(x):
        """The receiver's bandwidth at frequency x."""
        return float(receiver_bandwidth([x], bands)[0])
    tun = tunings_for(rng[0], rng[1], rbw, w.params.f_pwm)
    reading = receiver(freqs, v, 1.0 / w.period, tun, rbw, detector(std))
    v_db = 20 * np.log10(np.maximum(reading / math.sqrt(2), 1e-30) / 1e-6)
    lim = limit_line(tun, std, a.conducted_class)
    return [{"f": float(tun[j]), "v_dbuv": float(v_db[j]), "limit_dbuv": None if np.isnan(lim[j]) else float(lim[j]),
             "margin_db": None if np.isnan(lim[j]) else float(lim[j] - v_db[j])}
            for j in thin_rows(tun, v_db, np.arange(len(tun)), bands, REPORT_PER_DECADE)]


def ce101_spec(model: BoardModel, w: DriveWaveform):
    """The CE101 limit for a bridge at a drive (analysis.ce101_spec with the bus voltage and the mean bus
    current), or None when CE101 is off."""
    return model.config.analysis.ce101_spec(w.params.v, abs(w.stats()["bus_mean"]))


def ce101_bridge_rows(model: BoardModel, legs: list, w: DriveWaveform, ce) -> list[dict]:
    """CE101 rows [{f, current_a, i_dbua, limit_dbua, margin_db}] of the input lead for a drive waveform: each
    leg's high-side line spectrum within the limit's range (10 kHz; 461H ships 20 kHz: lines every electrical
    frequency, the six-step commutation at 6·f_e and its neighbours) times the leg's CE101 transfer, added as
    phasors, read by the current probe's receiver (Table II's 10 Hz / 100 Hz / 1 kHz bandwidths, peak detector)."""
    from ..emi.conducted import CE101_BANDS, ce101_rows
    lo, hi = ce.range
    contributions, freqs = [], None
    for s in legs:
        fs, ts = _ce101_complex_transfer(s, hi)
        if len(fs) < 2:
            continue
        f, ph, _ = line_spectrum(w.t, w.hs[:, _column(s, legs)], w.period, w.T, w.params.rise_time, hi * 1.05)
        sel = f >= lo / 1.1
        freqs = f[sel]                   # the same line grid for every leg (one drive waveform)
        contributions.append(ph[sel] * _interp_complex(freqs, fs, ts))
    if not contributions or not len(freqs):
        return []
    line = sum(contributions)

    def rbw(x):
        """The current probe receiver's bandwidth at frequency x."""
        return float(receiver_bandwidth([x], CE101_BANDS)[0])
    tun = tunings_for(lo, hi, rbw)
    reading = receiver(freqs, line, 1.0 / w.period, tun, rbw, "peak")
    i_db = 20 * np.log10(np.maximum(reading / math.sqrt(2), 1e-30) / 1e-6)
    return ce101_rows(tun, i_db, reading, ce, REPORT_PER_DECADE)


def _ce101(model: BoardModel, bs: BridgeSetup, legs: list):
    """MIL-STD-461 CE101 of the bridge's input lead (analysis.ce101): the rows, the worst, an issue below the
    margin."""
    rules = model.config.rules
    res, b, w = bs.res, bs.spec, bs.res.waveform
    ce = ce101_spec(model, w)
    if ce is None:
        return
    res.ce101_limit = ce.describe()
    if not any(s.ce101_freqs for s in legs) or all(s.lisn is None for s in legs):
        res.notes.append("CE101 not estimated (set the bridge's input_connector and a conducted standard)")
        return
    res.ce101 = ce101_bridge_rows(model, legs, w, ce)
    if not res.ce101:
        res.notes.append(f"CE101: no drive lines below {ce.range[1] / 1e3:g} kHz at this operating point "
                         f"(f_e {w.f_e:.4g} Hz)")
        return
    compared = [r for r in res.ce101 if r["margin_db"] is not None]
    if not compared:
        return
    wc = res.ce101_worst = min(compared, key=lambda r: r["margin_db"])
    if wc["margin_db"] >= rules.emi_margin_db:
        return
    sev = resolve_severity("conducted_emission", rules.severity, "error" if wc["margin_db"] < 0 else "warning")
    if sev != "off":
        bs.issues.append(Issue(
            "conducted_emission", sev, b.name,
            f"{b.name}: input lead current {wc['i_dbua']:.1f} dBµA at {wc['f']:.4g} Hz, margin "
            f"{wc['margin_db']:+.1f} dB to {res.ce101_limit} (six-step drive, all legs)",
            (round(res.location[0], 4), round(res.location[1], 4)), key=f"power:{b.name}:ce101"))


def _conducted(model: BoardModel, bs: BridgeSetup, legs: list):
    """The legs' line currents added at the LISN (each leg's high-side spectrum times its transfer to the LISN),
    read by the receiver against the limit; an issue below the margin."""
    a, rules = model.config.analysis, model.config.rules
    res, b, w = bs.res, bs.spec, bs.res.waveform
    std = legs[0].standard
    rng = frequency_range(std, a.conducted_class) if std != "none" else None
    if rng is None:
        res.notes.append("conducted emission not estimated (set the bridge's input_connector and a conducted "
                         "standard)")
        return
    res.conducted_standard = describe(std, a.conducted_class)
    res.conducted = conducted_rows(model, legs, w, std)
    compared = [r for r in res.conducted if r["margin_db"] is not None]
    if not compared:
        return
    wc = res.conducted_worst = min(compared, key=lambda r: r["margin_db"])
    if wc["margin_db"] >= rules.emi_margin_db:
        return
    sev = resolve_severity("conducted_emission", rules.severity, "error" if wc["margin_db"] < 0 else "warning")
    if sev != "off":
        bs.issues.append(Issue(
            "conducted_emission", sev, b.name,
            f"{b.name}: conducted {wc['v_dbuv']:.1f} dBµV at {wc['f'] / 1e6:.4g} MHz, margin "
            f"{wc['margin_db']:+.1f} dB to {res.conducted_standard} (six-step drive, all legs)",
            (round(res.location[0], 4), round(res.location[1], 4)), key=f"power:{b.name}:conducted"))


def _victims(model: BoardModel, bs: BridgeSetup, legs: list):
    """Per victim, the worst leg: the noise at its receivers, the current its phase node injects, its ground's
    shift (victims.stage_rows)."""
    vs = bs.victims
    if vs is None or not (vs.parts or vs.nets):
        return
    board, b, config = model.board, bs.spec, model.config
    worst: dict[tuple, dict] = {}
    for s in legs:
        for row in stage_rows(board, model.copper, vs, s.victim_run, bs.probes.get(s.spec.name, []), b.ground,
                              s.spec.name, s.spec.i_out, b.rise_time, b.v_bus or 0.0, s.spec.switch_node):
            key = (row["kind"], row["name"])
            if key not in worst or row["peak"] > worst[key]["peak"]:        # the worst leg per victim
                worst[key] = row
    rows = finish_rows(list(worst.values()), config.victims, vs.reference_set)
    bs.res.victims = rows
    sev = resolve_severity("victim_coupling", config.rules.severity)
    for r in rows:
        if r["checked"] and sev != "off" and r["peak"] > r["limit"]:
            bs.issues.append(Issue("victim_coupling", sev, b.name, f"{b.name}: {victim_text(r)}",
                                   (round(bs.res.location[0], 4), round(bs.res.location[1], 4)),
                                   key=f"victim:{b.name}:{r['kind']}:{r['name']}"))
