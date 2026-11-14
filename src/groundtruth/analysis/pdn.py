"""Power-distribution network (PDN) impedance of a supply rail: what its ICs see between their supply and ground pins.

An engineer checks a rail by its impedance against frequency at the IC: below a target Z_t = V·ripple / I_step
[Smith 1999] the
rail holds its voltage within the ripple when the IC's current steps by I_step, whatever the frequency of the step.
Above the target somewhere, a load step there rings the rail: the decaps, planes, vias and the regulator decide
where. (settings `pdn:`, config.PdnSpec; the plan's milestone M1.)

The model: the whole board's copper of the rail and its ground on every layer (the planes, pours, tracks and vias),
with the capacitance between their facing copper (plane pairs, from the stackup), the decaps as branches with their
C, ESR, ESL and mounting inductance (simulate.part_impedance: every part with a model joining the two nets), and the
source (a regulator's output, a connector) as a branch of vrm_r + jω·vrm_l between its rail and ground pads. Each
IC is a port: +1 A spread over its supply pads on the rail (by area, power._area_shares), -1 A over its pads on the
ground; the solve's Σ J·V is then V_rail - V_ground per ampere: the impedance Z(f) the IC sees, with every decap's
share of the current (which part holds the rail at which frequency).

All ports of a rail are solved together at each frequency (PortSet: one factorisation, a few right-hand sides), and
only what is needed is kept (PortSolution: Z per port, the parts' currents), not the field maps. The frequencies:
CE-style log spacing from 10 kHz to 1 GHz, then refined where |Z| bends (a decap's series resonance, an
anti-resonance between two decaps or a decap and the plane), see PdnSetup.refine.

Checks (issues):
- pdn_impedance: |Z| above the target below `check_to` (default 100 MHz [A19]: above it the package and the die's own
  capacitance decouple), with the frequency, the margin and the part holding the rail there;
- decap_missing: a supply pin with no decap pad on the rail within `decap_mm` (default 3 mm).

Validated in tests/test_pdn.py against analytic results: the plane pair's capacitance at the port, one and N equal
decaps (1/(ωNC) below resonance, ESR/N at it), the source's resistance at low frequency.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

import numpy as np

from ..config.settings import PdnSpec
from ..core.board import Board, Footprint, Pad
from ..core.issues import Issue, resolve_severity
from ..solver.peec import Branch, RouteExcitation, SolverError
from .simulate import BoardModel, ModelRun, _choose_pitch, part_pairs

F_MIN, F_MAX = 10e3, 1e9          # the sweep
PER_DECADE = 4                   # initial solves per decade (21 over the sweep)
MAX_SOLVES = 40                  # with the refinement
REFINE_DB = 1.0                  # split a gap whose log-log interpolation may be off by more than this (dB)
WARN_DB = 3.0                    # a margin to the target under this is a warning (under 0: an error)
SHARE_MIN = 0.05                 # a part's share of the port current listed from this fraction up
SUPPLY_TYPES = ("power_in",)     # schematic pin types of an IC's supply pins
IC_PREFIXES = ("U", "IC")        # without pin types on the board: parts with these prefixes and rail + ground pads
CONNECTOR_PREFIXES = ("J", "P", "X", "CN", "CON")
VRM_R_DEFAULT, VRM_L_DEFAULT = 5e-3, 20e-9       # PdnSpec's defaults: a regulator's output
HARNESS_R, HARNESS_L = 20e-3, 500e-9             # a connector's source with those defaults: ~0.5 m of lead [A110]


def rail_voltage(name: str) -> Optional[float]:
    """A rail's voltage from its name: "+3V3" 3.3, "+12V" 12, "VDD_1V8" 1.8, "/MCU/+5V_ISO" 5, and with a decimal
    point as KiCad's power symbols also write it, "+3.3V" 3.3, "+1.8V" 1.8, "+5.0V" 5; None without one. (Before
    2026-11-12 the decimal form read its digits after the point: "+1.8V" as 8 V, "+5.0V" as 0 V: audit round 12,
    C-1.)"""
    last = name.upper().rsplit("/", 1)[-1]
    m = re.search(r"(?<![\d.])(\d+\.\d+)V(?!\d)", last)          # "3.3V": the point is the decimal point
    if m:
        return float(m.group(1))
    m = re.search(r"(?<![\d.])(\d+)V(\d*)", last)                 # "3V3", "12V": the V is
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def _prefix(ref: str) -> str:
    """A reference's letters ("U53" -> "U")."""
    return re.match(r"[A-Za-z_]*", ref).group(0).upper()


def supply_parts(board: Board, rail: str, ground: str) -> list[tuple[Footprint, list[Pad], list[Pad]]]:
    """The ICs on the rail: (part, its supply pads on the rail, its pads on the ground). A part counts with a pad of
    pin type power_in on the rail; a board without pin types: an IC-like reference (U, IC) with pads on both. Parts
    without a ground pad are left out (their return is another part's)."""
    typed = any(p.pin_type for fp in board.footprints for p in fp.pads)
    out = []
    for fp in board.footprints:
        # connectors are not ICs to decouple, even with a power_in pin (refboard's Tag-Connect J41)
        if fp.dnp or _prefix(fp.reference) in CONNECTOR_PREFIXES:
            continue
        on_rail = [p for p in fp.pads if p.net == rail]
        if typed:
            on_rail = [p for p in on_rail if p.pin_type in SUPPLY_TYPES]
        elif _prefix(fp.reference) not in IC_PREFIXES:
            on_rail = []
        gnd = [p for p in fp.pads if p.net == ground]
        if on_rail and gnd:
            out.append((fp, on_rail, gnd))
    return out


def rails_with_ics(board: Board, ground: str) -> dict[str, list[str]]:
    """Every net that ICs draw supply from (a power_in pad on it and a pad on `ground`; connectors left out), with
    those ICs: {net: [refs]}, in one pass over the pads (fast enough for the window's thread). The Power integrity
    tab's Find rails."""
    out: dict[str, list[str]] = {}
    for fp in board.footprints:
        if fp.dnp or _prefix(fp.reference) in CONNECTOR_PREFIXES or not any(p.net == ground for p in fp.pads):
            continue
        for net in {p.net for p in fp.pads if p.pin_type in SUPPLY_TYPES and p.net and p.net != ground}:
            out.setdefault(net, []).append(fp.reference)
    return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))


STAGE = "stage:"                 # a PdnSpec source naming a power stage: "stage:conv1"
LATE_TAUS = 10.0                 # a port step's "late" dip: after this many time constants of the check_to low-pass
STAGE_F_LOW = 30.0               # with a power stage as the source the sweep starts at its crossover / this ...
F_MIN_STAGE = 100.0              # ... but not below this


def stage_outputs(board: Board, config, spec) -> tuple[set, str, str]:
    """A power stage's output nets (loop.output_nets: the inductor's far side and nets joined to it through shunts),
    the net its load hangs on and that connector's pins as a PDN port ("J2.2/J2.1", "" without one). The load's
    net: the output connector's + pin's; else the one the feedback divider senses (what the loop holds); else the
    output net with the most capacitors to ground; else the inductor's far side."""
    from .loop import find_loop, output_nets
    from .power import switch_inductor
    from .safety import is_ground
    outs = output_nets(board, config, spec)
    if not outs:
        return set(), "", ""
    port, rail = "", ""
    oc = list(spec.output_connector)
    if len(oc) == 2 and all("." in p for p in oc):
        ref, pin = oc[0].rsplit(".", 1)
        fp = board.footprint(ref)
        pad = next((p for p in fp.pads if p.number == pin), None) if fp is not None else None
        if pad is not None and pad.net in outs:
            rail, port = pad.net, f"{oc[0]}/{oc[1]}"
    if not rail:
        parts = find_loop(board, config, spec)
        for ref, *_ in (parts.top if parts is not None else []):
            fp = board.footprint(ref)
            rail = next((p.net for p in (fp.pads if fp is not None else ()) if p.net in outs), "")
            if rail:
                break
    if not rail:
        by_net = board.parts_by_net()
        count = {n: sum(1 for fp in by_net.get(n, ()) if (m := config.model_for(fp.reference)) is not None
                        and m.kind == "cap" and any(is_ground(p.net) for p in fp.pads)) for n in outs}
        if max(count.values(), default=0):
            rail = max(sorted(count), key=count.get)
    if not rail:
        node, ind = switch_inductor(board, config, spec)
        far = [p.net for p in ind.pads if p.net and p.net != node] if ind is not None else []
        rail = far[0] if far and far[0] in outs else sorted(outs)[0]
    return outs, rail, port


def converter_rails(board: Board, config) -> dict[str, tuple[str, str]]:
    """The rails the board's power stages feed: {net: (stage name, its output connector as a port or "")} (enabled
    stages, not a motor bridge's legs; a stage whose output can't be found left out). The Power integrity tab's
    Find rails lists them beside the rails ICs draw from (the user: "Why are converters not listed under Power
    integrity"); each stage's own output nets also count as its rail for the source (find_stage)."""
    out: dict[str, tuple[str, str]] = {}
    for spec in config.converters:
        if not spec.enabled or spec.bridge or spec.topology not in ("buck", "boost"):
            continue
        _, rail, port = stage_outputs(board, config, spec)
        if rail and rail not in out:
            out[rail] = (spec.name, port)
    return out


def find_stage(board: Board, config, rail: str, name: str = ""):
    """The power stage feeding a rail: the one named (a "stage:NAME" source), else the first enabled stage whose
    output nets hold the rail; None without one."""
    for spec in config.converters:
        if name and spec.name != name:
            continue
        if not spec.enabled or spec.bridge:
            continue
        if name or rail in stage_outputs(board, config, spec)[0]:
            return spec
    return None


def find_source(board: Board, rail: str, ground: str) -> Optional[Footprint]:
    """The part feeding the rail: one with a power_out pin on it (a regulator's output), else a connector with pads on
    the rail and the ground; None when neither is there."""
    for fp in board.footprints:
        if not fp.dnp and any(p.net == rail and p.pin_type == "power_out" for p in fp.pads):
            return fp
    for fp in board.footprints:
        nets = {p.net for p in fp.pads}
        if not fp.dnp and _prefix(fp.reference) in CONNECTOR_PREFIXES and rail in nets and ground in nets:
            return fp
    return None


def decaps(board: Board, config, rail: str, ground: str) -> list[Footprint]:
    """The capacitors between the rail and the ground (parts with a cap model and pads on both nets)."""
    out = []
    for fp in board.footprints:
        model = config.model_for(fp.reference)
        if model is None or model.kind != "cap" or fp.dnp:
            continue
        nets = {p.net for p in fp.pads}
        if rail in nets and ground in nets:
            out.append(fp)
    return out


@dataclass
class PortSolution:
    """What a PDN solve keeps at one frequency: per port the impedance (complex, Ω) and the branch currents (per A of
    the port's current; the stack's branch order)."""
    f: float
    Z: np.ndarray
    I_branch: np.ndarray


class PortSet:
    """Several ports on one model, solved together at each frequency (the model's factorisation is shared; one
    right-hand side each). Duck-types the excitation a ModelRun holds (`stack`, `solve`)."""

    def __init__(self, stack, ports: list[list[tuple]]):
        """`ports`: each port's injections [(node, A)], +1 A in at the supply pads, -1 A out at the ground pads."""
        self.stack = stack
        self.excs = [RouteExcitation(stack, [], injections=inj) for inj in ports]

    def solve(self, f: float) -> PortSolution:
        """Every port at f (SolverError when one fails)."""
        sols = [e.solve(f) for e in self.excs]
        return PortSolution(f, np.array([s.Z_loop for s in sols], complex),
                            np.array([s.I_branch for s in sols], complex))


@dataclass
class PdnResult:
    """A rail's PDN (see the module): per IC port its Z(f) and what holds it, the decaps, the target and the source."""
    name: str
    rail: str
    ground: str
    kind: str = "pdn"
    voltage: Optional[float] = None
    target_ohm: Optional[float] = None
    check_to: float = 100e6
    source: str = ""                    # the source part and its model, in words
    stage: str = ""                     # a power stage as the source (its closed loop: analysis/load_step.py)
    f_min: float = F_MIN                # the sweep's lowest frequency (lower with a power stage's loop)
    ports: list = field(default_factory=list)       # [{part, pins, f, z_ohm, phase_deg, worst, peaks, l_nh, holds}]
    responses: list = field(default_factory=list)   # each IC's Z(f) with its poles and zeros (analysis/transfer.py)
    decaps: list = field(default_factory=list)      # [{ref, value, C, near: [{part, mm}]}]
    worst: Optional[dict] = None        # the worst port's {part, f, z_ohm, margin_db, holds}
    notes: list = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    nodes: int = 0
    pitch_mm: float = 0.0

    @property
    def net(self) -> str:
        """Its name where the results list views by net."""
        return self.name

    def label(self) -> str:
        """Its name in lists."""
        return f"{self.name}  PDN"

    def summary(self) -> dict:
        """As plain data for the JSON report (the responses as their own summaries)."""
        d = asdict(replace(self, responses=[]))
        d["responses"] = [r.summary() for r in self.responses]
        return d


class PdnSetup:
    """A rail's run for the solver's scheduler (simulate.solve_setups): the ports' run as an extra run, refined by
    `refine`; `fine` is the same model with nothing planned (the scheduler refines only extra runs: the pattern of
    a power stage's conducted run). Duck-types what solve_setups and the memory bookkeeping read of a setup."""
    model_label = "PDN"

    def __init__(self, spec: PdnSpec, res: PdnResult, run: ModelRun, ports: list[dict], branches: list[Branch],
                 build):
        """`ports`: [{part, pins, xy, pads}] in the run's order; `branches`: the model's branches (names: parts)."""
        self.spec, self.res, self.ports, self.branches, self.build = spec, res, ports, branches, build
        self.fine = ModelRun(run.build, run.exc, planned=[])
        # the ports' run (refined), and each IC's two regional models at LOCAL_F (not refined)
        self.extra_runs = [run] + [r for m in ports if m.get("local") for r in m["local"]]
        self.run = run
        self.cavity, self.spectrum_run = None, None
        self.issues: list[Issue] = []
        self.t_start = time.time()

    def refine(self, run, config) -> list:
        """One more frequency where the worst port's |Z| bends most (a resonance between solves), or [] when every
        gap is resolved to REFINE_DB or MAX_SOLVES were made."""
        if run is not self.run:
            return []                       # a regional model: its LOCAL_F only
        sols = run.ok()
        if len(run.sols) >= MAX_SOLVES or len(sols) < 3:
            return []
        fs = np.array(sorted(sols))
        z = np.max(np.abs(np.array([sols[f].Z for f in fs])), axis=1)      # the worst port at each frequency
        lx, ly = np.log10(fs), 20 * np.log10(np.maximum(z, 1e-12))
        slope = np.diff(ly) / np.maximum(np.diff(lx), 1e-12)
        best = None
        for g in range(len(fs) - 1):
            if fs[g + 1] / fs[g] < 1.03:
                continue
            bends = [abs(slope[g] - slope[k]) for k in (g - 1, g + 1) if 0 <= k < len(slope)]
            unc = max(bends) * (lx[g + 1] - lx[g]) / 8
            if unc >= REFINE_DB and (best is None or unc > best[0]):
                best = (unc, math.sqrt(fs[g] * fs[g + 1]))
        return [float(best[1])] if best else []

    def solve_limit(self, run) -> int:
        """The most solves `run` takes with refine (the progress count, simulate.solve_limit): the ports' run up to
        MAX_SOLVES, a regional model its LOCAL_F."""
        return max(len(run.planned), MAX_SOLVES) if run is self.run else len(run.planned)

    def issue(self, rules, check, message, loc, severity=None, **kw):
        """Add an issue for this rail (unless its check is switched off)."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            self.issues.append(Issue(check, sev, self.spec.label, message, (round(loc[0], 4), round(loc[1], 4)),
                                     **kw))


def _source_branch(st, src: Footprint, rail: str, ground: str, spec: PdnSpec) -> tuple[list[Branch], str]:
    """The source as branches of vrm_r + jω·vrm_l between its rail and ground pads (spread over them like a part),
    and its description; a source without a ground pad joins the nearest ground copper."""
    rail_pads = [p for p in src.pads if p.net == rail]
    gnd_pads = [p for p in src.pads if p.net == ground]
    r, l_ = spec.vrm_r, spec.vrm_l
    # a connector (no power_out pin on the rail) has a harness and the supply behind it, not a regulator's output:
    # with the regulator's defaults left as they are, a lead's instead [A110] (before 2026-11-12 the regulator's
    # 20 nH: the impedance below the decaps' range up to ~30 dB low: audit round 12, C-5)
    harness = (not any(p.net == rail and p.pin_type == "power_out" for p in src.pads)
               and r == VRM_R_DEFAULT and l_ == VRM_L_DEFAULT)
    if harness:
        r, l_ = HARNESS_R, HARNESS_L

    def z(f, r=r, l_=l_):
        """The source's output impedance."""
        return complex(r, 2 * math.pi * f * l_)
    pairs = part_pairs(st, rail_pads, gnd_pads) if gnd_pads else []
    if not pairs:
        p = rail_pads[0]
        radius = max(3 * st.d, 2.0)
        na = next((n for n in (st.node(l, *p.position, rail, radius) for l in p.layers) if n), None)
        nb = next((n for n in (st.node(l, *p.position, ground, radius) for l in st.names) if n), None)
        pairs = [(na, nb)] if na and nb and na != nb else []
    if not pairs:
        raise SolverError(f"the source {src.reference}: no {rail} / {ground} copper at its pads")
    n = len(pairs)
    out = [Branch(a, b, z if n == 1 else (lambda f, z=z, n=n: n * z(f)), f"source {src.reference}", "part")
           for a, b in pairs]
    return out, (f"{src.reference} ({src.value}) as {r * 1e3:g} mΩ + {l_ * 1e9:g} nH (vrm_r, vrm_l"
                 + ("; a connector: a harness's defaults, give the supply's own values" if harness else "") + ")")


def _stage_branch(st, board: Board, config, stage, rail: str, ground: str) -> tuple[list[Branch], str, float]:
    """A power stage as the rail's source: its closed loop as the admittance Y_conv(s) (loop.LoopModel, or the
    approximate loop of its bandwidth and phase margin: load_step.converter_model) from where its feedback divider
    senses the output (the divider's top resistor's pad on an output net: the loop holds that node; else the
    inductor's output pad) to the nearest ground copper, the branch's impedance 1/Y_conv(j2πf) [A117]. Its output
    capacitors are the model's own part branches (decaps), so they are not in it. A sense shunt between the
    inductor and the divider is inside the loop: at the inductor's pad its resistance read as a droop the loop
    takes out (10 mΩ: 10 mV per A on the test board). (branches, its description, the loop's crossover in Hz or 0);
    SolverError without a model or without copper there."""
    from .load_step import _crossover, converter_model
    from .loop import find_loop
    from .power import switch_inductor
    model, _, err = converter_model(board, config, stage, stage.i_out)
    if model is None:
        raise SolverError(f"{stage.name}: {err}")
    outs = stage_outputs(board, config, stage)[0]
    # where to attach, best first: the divider's top pad (the sensed node), the output capacitors' pads on the
    # sensed net, the inductor's output pad; the first whose grid node lies on its net's main copper (a divider's
    # 0402 pad on a thin track was its own piece on a 0.37 mm grid: the source read 7 Ω at 1 kHz)
    cands = []
    parts = find_loop(board, config, stage)
    sensed = set()
    for ref, *_ in (parts.top if parts is not None else []):
        fp = board.footprint(ref)
        for p in (fp.pads if fp is not None else ()):
            if p.net in outs:
                cands.append((p, f"{ref} (its feedback divider)"))
                sensed.add(p.net)
    caps = [fp for fp in decaps(board, config, next(iter(sensed)), ground)] if sensed else []
    cands += [(p, f"{fp.reference} (an output capacitor)") for fp in caps for p in fp.pads if p.net in sensed]
    node, ind = switch_inductor(board, config, stage)
    if ind is not None:
        cands += [(p, ind.reference) for p in ind.pads if p.net and p.net != node]
    piece, main = copper_pieces(st, st.plane)
    radius = max(3 * st.d, 2.0)
    na = nb = None
    where = ""
    for pad, where in cands:
        na = next((n for n in (st.node(l, *pad.position, pad.net, radius) for l in pad.layers) if n), None)
        nb = next((n for n in (st.node(l, *pad.position, ground, radius) for l in st.names) if n), None)
        if na and nb and na != nb and piece[st.plane.nid[na]] == main.get(pad.net) \
                and piece[st.plane.nid[nb]] == main.get(ground):
            break
    else:
        raise SolverError(f"{stage.name}: no {'/'.join(sorted(outs))} and {ground} copper at its feedback divider, "
                          "output capacitors or inductor on this model's grid")
    y_conv = model.y_conv

    def z(f, y=y_conv):
        """The stage's closed-loop source impedance 1/Y_conv at f."""
        return complex(1.0 / y(2j * math.pi * f))
    fc = _crossover(model.t, stage.f_sw) or 0.0
    text = (f"{stage.name} in closed loop at {where} ({'approximate loop, ' if model.approximate else ''}"
            f"crossover {fc / 1e3:.3g} kHz)" if fc else f"{stage.name} in closed loop at {where}")
    return [Branch(na, nb, z, f"source {stage.name}", "part")], text, fc


def _port_pads(board: Board, port: str, rail: str, ground: str):
    """A port "J2.2/J2.1" as (footprint, [its pad on the rail], [its pad on the ground]), or a reason it can't be."""
    try:
        a, b = (p.strip() for p in port.split("/"))
        ref, pa = a.rsplit(".", 1)
        ref_b, pb = b.rsplit(".", 1)
    except ValueError:
        return f"port {port!r}: write it as supply / return pins, J2.2/J2.1"
    fp, fb = board.footprint(ref), board.footprint(ref_b)
    if fp is None or fb is None or fp is not fb:
        return f"port {port!r}: both pins on one part on the board"
    rp = [p for p in fp.pads if p.number == pa and p.net == rail]
    gp = [p for p in fp.pads if p.number == pb and p.net == ground]
    if not rp or not gp:
        return f"port {port!r}: {a} must be on {rail} and {b} on {ground}"
    return fp, rp, gp


def copper_pieces(st, plane) -> tuple[np.ndarray, dict]:
    """The model's pieces of copper joined by copper (grid strips, vias, tracks bridged, pins; not parts and not
    the planes' capacitance): a piece number per node, and per net the piece holding most of its nodes (its main
    copper). A pad landing elsewhere is cut off from its net in this model: a pad smaller than a cell whose thin
    track the grid did not join (a QFN's 0.25 mm pads on a 0.86 mm whole-board grid: refboard's U55 read
    1.7 MΩ at 1 MHz, the stray capacitance of its pads), or a real break in the copper."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = plane.n_nodes
    keep = np.ones(plane.nb, bool)
    keep[plane.ns: plane.ns + plane.ncap] = False                     # the plane pairs' capacitance
    for k, b in enumerate(plane.branches):
        if b.kind == "part":                                          # decaps, the source
            keep[plane.ns + plane.ncap + k] = False
    g = coo_matrix((np.ones(int(keep.sum())), (plane.a_nodes[keep], plane.b_nodes[keep])), shape=(n, n))
    _, label = connected_components(g, directed=False)
    main: dict[str, int] = {}
    for k, grid in enumerate(st.grids):
        if grid.label is None:
            continue
        for ni, net in enumerate(grid.nets):
            ids = plane.nid[k][grid.label == ni]
            ids = ids[ids >= 0]
            if len(ids):
                counts = main.setdefault(net, {})
                for comp, c in zip(*np.unique(label[ids], return_counts=True)):
                    counts[int(comp)] = counts.get(int(comp), 0) + int(c)
    return label, {net: max(c, key=c.get) for net, c in main.items() if c}


LOCAL_F = (30e6, 100e6, 300e6)  # the per-IC correction's frequencies (inductive: above the decaps' resonances)
LOCAL_NEAR_MM = 9.0              # decaps within this of an IC's supply pads are in its regional model ...
LOCAL_MARGIN_MM = 3.0            # ... with this much copper around them


def _local_runs(model: BoardModel, st, spec: PdnSpec, ground: str, fp: Footprint, rp: list, gp: list,
                caps: list) -> tuple:
    """Two regional models around one IC (its pads and the decaps within LOCAL_NEAR_MM of its supply pads, plus
    LOCAL_MARGIN_MM), solved at LOCAL_F with the IC's port: one at a fine pitch (the dielectric height, the
    regional node budget analysis.max_nodes, as a power stage's), one at the board-wide model's pitch on the
    board-wide grid's own lattice (the region snapped to it). Their difference is what the coarse grid gets wrong at
    the pins (small pads, vias, escapes: on refboard's +3V3, 0.04-0.9 nH; the test board's coarsest grid read
    -43 % at 100 MHz without it), added to the IC's board-wide curve in finish_pdn. (fine run, coarse run), or (None, None, why)."""
    from .power import _area_shares, _side
    board, a = model.board, model.config.analysis
    pts = [p.position for p in rp + gp]
    near = 0
    for c in caps:
        cp = [p for p in c.pads if p.net in (spec.rail, ground)]
        if any(math.dist(q.position, s.position) <= LOCAL_NEAR_MM for q in cp if q.net == spec.rail for s in rp):
            pts += [q.position for q in cp]
            near += 1
    if not near:
        # without a decap the region is a small plane capacitor: the pitches differ in its capacitance, not in an
        # inductance at the pins (read as one, it moved the bare test board's 10 MHz reading by 2.5 %)
        return None, None, f"no decap within {LOCAL_NEAR_MM:g} mm"
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    g0 = st.grids[0]
    d = st.d

    def snap(v, o, up):
        """v on the board-wide lattice o + k·d (down, or up)."""
        k = (v - o) / d
        return o + (math.ceil(k) if up else math.floor(k)) * d
    region = (snap(min(xs) - LOCAL_MARGIN_MM, g0.x0, False), snap(min(ys) - LOCAL_MARGIN_MM, g0.y0, False),
              snap(max(xs) + LOCAL_MARGIN_MM, g0.x0, True), snap(max(ys) + LOCAL_MARGIN_MM, g0.y0, True))
    layers = list(st.names)
    h = min((board.stackup.dielectric_between(x, y) for x, y in zip(layers, layers[1:])), default=0.2)
    d_fine = _choose_pitch(h, region, len(layers), a.max_nodes, "auto", a.resolution)
    if d_fine >= 0.75 * d:
        return None, None, "the board-wide grid is already about as fine"
    runs = []
    for pitch in (d_fine, d):
        rs = model.build(region, layers, {spec.rail, ground}, pitch, True)
        try:
            inj = _area_shares(rs, rp, _side(fp), 1.0) + _area_shares(rs, gp, _side(fp), -1.0)
        except SolverError as e:
            return None, None, str(e)
        runs.append(ModelRun(rs, PortSet(rs.plane, [inj]), planned=list(LOCAL_F)))
    return runs[0], runs[1], ""


def prepare_pdn(model: BoardModel, specs: list) -> tuple[list, list, list]:
    """Each rail's model and ports: (setups to solve, results, issues). A rail that can't be set up (no outline, no
    IC on it, no copper at a pad) gets a result with its error."""
    from .power import _area_shares, _side
    board, config = model.board, model.config
    setups, results, issues = [], [], []
    bbox = model.board_region()
    a = config.analysis
    for spec in specs:
        ground = spec.ground or (config.reference_nets[0] if config.reference_nets else "GND")
        v = spec.voltage if spec.voltage is not None else rail_voltage(spec.rail)
        res = PdnResult(spec.label, spec.rail, ground, voltage=v, check_to=spec.check_to)
        results.append(res)
        if spec.i_step is not None and v is not None:
            res.target_ohm = v * spec.ripple / spec.i_step
        elif spec.i_step is not None:
            res.notes.append(f"no voltage for {spec.rail} (set voltage): no target impedance")
        if bbox is None:
            res.error = "the board has no outline (Edge.Cuts): the PDN model is the whole board"
            continue
        parts = supply_parts(board, spec.rail, ground)
        if spec.parts:
            wanted = set(spec.parts)
            parts = [t for t in parts if t[0].reference in wanted]
            missing = wanted - {t[0].reference for t in parts}
            if missing:
                res.notes.append(f"{', '.join(sorted(missing))}: no supply pin on {spec.rail} and pad on {ground}: "
                                 "left out")
        for port in spec.ports:             # pin pairs: a connector the load hangs on (a converter's output)
            got = _port_pads(board, port, spec.rail, ground)
            if isinstance(got, str):
                res.notes.append(got + ": left out")
            else:
                parts.append(got)
        if not parts:
            res.error = (f"no IC with a supply pin on {spec.rail} and a pad on {ground} (pin type power_in; set "
                         "`parts` to name them, or `ports` for a connector the load hangs on)")
            continue
        # the source: a power stage (named, or the one whose output this is), else a part (named, or found)
        stage = None
        if spec.source.startswith(STAGE):
            stage = find_stage(board, config, spec.rail, spec.source[len(STAGE):])
            if stage is None:
                res.notes.append(f"no power stage {spec.source[len(STAGE):]!r}: the source is found by itself")
        elif not spec.source:
            stage = find_stage(board, config, spec.rail)
        nets = {spec.rail, ground} | (stage_outputs(board, config, stage)[0] if stage is not None else set())
        try:
            # the rail and its ground on every copper layer; the capacitance between their facing copper
            layers = list(board.copper_layers)
            h = min((board.stackup.dielectric_between(x, y) for x, y in zip(layers, layers[1:])), default=0.2)
            d = _choose_pitch(max(h, 0.25), bbox, len(layers), a.cavity_max_nodes, "auto", a.resolution)
            st = model.build(bbox, layers, nets, d, True)
            branches, src_text = [], ""
            src = None
            if stage is not None:
                try:
                    branches, src_text, fc = _stage_branch(st, board, config, stage, spec.rail, ground)
                    res.source, res.stage = src_text, stage.name
                    if fc:
                        res.f_min = max(min(F_MIN, fc / STAGE_F_LOW), F_MIN_STAGE)
                except SolverError as e:
                    res.notes.append(f"the power stage as the source: {e}; a regulator's vrm_r / vrm_l instead")
                    stage = None
            if stage is None and spec.source.lower() != "none" and not spec.source.startswith(STAGE):
                src = board.footprint(spec.source) if spec.source else find_source(board, spec.rail, ground)
            if src is not None:
                branches, src_text = _source_branch(st, src, spec.rail, ground, spec)
                res.source = src_text
            else:
                res.notes.append(f"no source on {spec.rail} (a regulator's power_out pin, or a connector; `source` "
                                 "names it): below the decaps' range the impedance is theirs alone")
            plane = st.plane.with_branches(branches) if branches else st.plane
            piece, main = copper_pieces(st, plane)
            # the rail's main copper is the piece its source feeds (a coarse grid can split a pour in two, and the
            # piece with most nodes need not be the fed one), else the piece holding most of the ICs' supply pads
            # (a power stage's branch sits on its inductor's net, which may be behind a shunt: not the rail's piece)
            fed = [int(piece[plane.nid[b.a]]) for b in branches if stage is None]
            if fed:
                main[spec.rail] = max(set(fed), key=fed.count)
            else:
                pads_at = []
                for fp, rp, _ in parts:
                    for p in rp:
                        nd = next((x for x in (st.node(l, *p.position, spec.rail, max(st.d, 1.0)) for l in p.layers)
                                   if x), None)
                        if nd is not None:
                            pads_at.append(int(piece[plane.nid[nd]]))
                if pads_at:
                    main[spec.rail] = max(set(pads_at), key=pads_at.count)
            ports, meta, cut = [], [], []
            for fp, rp, gp in parts:
                side = _side(fp)
                try:
                    inj = _area_shares(st, rp, side, 1.0) + _area_shares(st, gp, side, -1.0)
                except SolverError as e:
                    # one IC's pad without copper on this grid (a small pad on a coarse model): that IC is left out,
                    # not the rail
                    res.notes.append(f"{fp.reference} left out: {e} on this model's {st.d:.2f} mm grid")
                    continue
                # its pads on the rail's and the ground's main copper, or it would read only stray capacitance (and
                # slow every solve: a nearly floating port)
                on_rail = all(piece[plane.nid[nd]] == main.get(spec.rail) for nd, a_ in inj if a_ > 0)
                on_gnd = all(piece[plane.nid[nd]] == main.get(ground) for nd, a_ in inj if a_ < 0)
                if not (on_rail and on_gnd):
                    cut.append(fp.reference + ("" if on_rail else f" ({spec.rail})") + ("" if on_gnd else f" ({ground})"))
                    continue
                ports.append(inj)
                meta.append({"part": fp.reference, "value": fp.value, "pins": [p.pin for p in rp],
                             "xy": (float(fp.position[0]), float(fp.position[1])), "pads": rp, "ground_pads": gp,
                             "fp": fp})
            if cut:
                res.notes.append(f"{', '.join(cut)}: the pads do not reach the main copper of that net on this model's "
                                 f"{st.d:.2f} mm grid (pads smaller than a cell on thin tracks, or a break in the "
                                 "copper): left out; a higher resolution (Preferences) resolves small pads")
            if not ports:
                # a board without the pours (zones unfilled, or removed while it is laid out again) has its rail and
                # ground in pieces of tracks: say so rather than blame the pads (refboard saved mid-relayout: no
                # zone at all, every rail failed with "a higher resolution resolves small pads")
                poured = {z.net for z in board.zones if not z.is_rule_area and any(z.filled.values())}
                missing = [n for n in (spec.rail, ground) if n not in poured]
                why = (f"; the board has no filled copper pour (zone) of {' or '.join(missing)}: fill its zones "
                       "(Refill zones), or check that the pours are on the board" if missing else "")
                raise SolverError("no IC's pads reach the rail's copper on this model" + why)
            exc = PortSet(plane, ports)
        except SolverError as e:
            res.error = f"{spec.label}: {e}"
            continue
        # each IC's pins on a fine regional model (the inductance there; _local_runs)
        caps = decaps(board, config, spec.rail, ground)
        why = []
        for m in meta:
            fine, coarse, reason = _local_runs(model, st, spec, ground, m["fp"], m["pads"], m["ground_pads"], caps)
            m["local"] = (fine, coarse) if fine is not None else None
            if fine is None:
                why.append(f"{m['part']} ({reason})")
            else:
                m["local_pitch"] = fine.build.d
        if why:
            res.notes.append("without the fine model at the pins: " + "; ".join(why))
        n0 = int(math.ceil(math.log10(F_MAX / res.f_min) * PER_DECADE))
        run = ModelRun(st, exc, planned=[float(f) for f in np.geomspace(res.f_min, F_MAX, n0 + 1)])
        res.nodes, res.pitch_mm = st.nodes, st.d
        res.notes.append(f"PDN modelled on the whole board: {spec.rail} and {ground} on {len(layers)} layers, "
                         f"{st.nodes} nodes at {st.d:.2f} mm, {len(meta)} IC port(s)")
        # the scheduler's progress names a setup by spec.name: the label (the rail when no name is set)
        setups.append(PdnSetup(replace(spec, name=spec.label), res, run, meta, list(plane.branches), st))
    return setups, results, issues


def _names(branches: list[Branch]) -> list[str]:
    """Each branch's part name ("" for vias and tracks); the source's as "source REF"."""
    return [b.name if b.kind == "part" else "" for b in branches]


def _inductance(fs: np.ndarray, Zc: np.ndarray, z: np.ndarray, peaks: list) -> Optional[float]:
    """The inductance the IC sees above its decaps (nH): Im(Z)/ω in the inductive stretch between the |Z| minimum
    (the decaps' series resonance) and the first anti-resonance above it (with the planes' capacitance), the
    median of the solves there. Near the minimum the decaps' capacitance still lowers Im(Z) and near the peak the
    planes' raises it (one 1 µF decap on the test board read 12.8 nH at the solve below its 305 MHz peak against
    ~2 nH in between); None without an inductive stretch."""
    k0 = int(np.argmin(z))
    above = [pk["f"] for pk in peaks if pk["f"] > fs[k0]]
    f_top = min(above) if above else 300e6
    ks = [k for k in range(k0 + 1, len(fs)) if fs[k] < f_top and Zc[k].imag > 0]
    if not ks:
        return None
    return float(np.median([Zc[k].imag / (2 * math.pi * fs[k]) for k in ks]) * 1e9)


def _local_correction(meta: dict) -> Optional[float]:
    """The inductance (H) the board-wide grid gets wrong at an IC's pins: Im(Z_fine - Z_coarse)/ω of its two
    regional models (_local_runs), the median over LOCAL_F; None without them. Added in series to the IC's curve: it
    is the IC's own current path that the grid resolves differently, and at low frequency (the capacitors' range)
    jω·ΔL is negligible next to them."""
    if not meta.get("local"):
        return None
    fine, coarse = meta["local"]
    sf, sc = fine.ok(), coarse.ok()
    # only where both are inductive (the IC's loop closing through its decaps): below a decap's resonance the
    # difference would be a capacitance's
    common = sorted(f for f in set(sf) & set(sc) if sf[f].Z[0].imag > 0 and sc[f].Z[0].imag > 0)
    if not common:
        return None
    return float(np.median([(sf[f].Z[0] - sc[f].Z[0]).imag / (2 * math.pi * f) for f in common]))


def finish_pdn(model: BoardModel, setup: PdnSetup):
    """The curves, what holds each port, the peaks, the worst margin against the target, the decaps near each IC
    and the issues."""
    res, spec, config = setup.res, setup.spec, model.config
    sols = setup.run.ok()
    if len(sols) < 2:
        res.error = res.error or "the PDN solves failed (see the notes)"
        return res
    failed = sorted(f for f, s in setup.run.sols.items() if s is None)
    if failed:
        # (a rail without decaps at 10 kHz: the planes alone are tens of kΩ against milliohms of copper, a system too
        # ill-conditioned for the iterative solve)
        res.notes = [n for n in res.notes if not n.startswith("GMRES did not converge")]
        res.notes.append(f"{len(failed)} frequenc{'y' if len(failed) == 1 else 'ies'} could not be solved "
                         f"({', '.join(f'{f / 1e3:.4g} kHz' for f in failed[:4])}): left out of the curves")
    fs = np.array(sorted(sols))
    Z = np.array([sols[f].Z for f in fs])                       # (frequencies, ports)
    names = _names(setup.branches)
    part_names = sorted({n for n in names if n})
    idx = {n: [k for k, m in enumerate(names) if m == n] for n in part_names}
    w = 2 * math.pi * fs
    for p, meta in enumerate(setup.ports):
        dl = _local_correction(meta)
        if dl is not None:
            Z[:, p] = Z[:, p] + 1j * w * dl          # the pins' inductance from the fine regional model
        z = np.abs(Z[:, p])
        port = {"part": meta["part"], "value": meta["value"], "pins": meta["pins"], "xy": meta["xy"],
                "f": [float(f) for f in fs], "z_ohm": [float(x) for x in z],
                "phase_deg": [float(np.degrees(np.angle(c))) for c in Z[:, p]]}
        # its Bode response: poles at the anti-resonances, zeros at the series resonances (a decap's SRF); the
        # impedance turns inductive at the top (the s·e term)
        from .transfer import fitted_response
        res.responses.append(fitted_response(f"{meta['part']}: Z at its supply pins", res.name, "impedance", fs,
                                             Z[:, p], use_e=True))
        _port_step(setup, res.responses[-1], meta, fs, Z[:, p])
        # what holds the rail at each frequency: each part's share of the port's current (its branches added)
        holds = []
        for k, f in enumerate(fs):
            I = sols[f].I_branch[p]
            shares = {n: abs(complex(np.sum(I[idx[n]]))) for n in part_names if idx[n] and len(I) > max(idx[n])}
            top = max(shares.items(), key=lambda t: t[1], default=(None, 0.0))
            holds.append({"f": float(f), "part": top[0] if top[1] >= SHARE_MIN else "planes",
                          "share": float(top[1])})
        port["holds"] = holds
        # peaks: local maxima of |Z| (anti-resonances), the highest first
        peaks = [k for k in range(1, len(fs) - 1) if z[k] > z[k - 1] and z[k] >= z[k + 1]]
        port["peaks"] = [{"f": float(fs[k]), "z_ohm": float(z[k])} for k in sorted(peaks, key=lambda k: -z[k])[:4]]
        port["l_nh"] = _inductance(fs, Z[:, p], z, port["peaks"])
        port["l_correction_nh"] = None if dl is None else float(dl * 1e9)
        port["local_pitch_mm"] = meta.get("local_pitch")
        if res.target_ohm:
            band = [k for k in range(len(fs)) if fs[k] <= spec.check_to]
            k = max(band, key=lambda k: z[k])
            port["worst"] = {"f": float(fs[k]), "z_ohm": float(z[k]),
                             "margin_db": float(20 * math.log10(res.target_ohm / max(z[k], 1e-12))),
                             "holds": holds[k]["part"]}
        res.ports.append(port)
        _port_step_figures(res, port)
    if res.target_ohm:
        w = min(res.ports, key=lambda p: p["worst"]["margin_db"])
        res.worst = {"part": w["part"], **w["worst"]}
    _decaps_near(model, setup)
    _issues(setup, config.rules)
    res.seconds = time.time() - setup.t_start
    return res


def _port_step(setup: PdnSetup, resp, meta: dict, fs: np.ndarray, z: np.ndarray):
    """The rail's voltage at a port over time when the IC draws i_step (over the load_step's rise; none: an ideal
    step), or when the load_step's coil is switched on there, through its harness to its load capacitance
    (load_step.sampled_step on the solved impedance up to check_to: the band the target applies to, the package and
    die decoupling above it [A19]): its response's step curve (plots.draw_step) and figures, notes."""
    from ..config.settings import LoadStep
    from .load_step import sampled_step
    res, spec = setup.res, setup.spec
    ls = spec.load_step or LoadStep()
    if res.voltage is None or not (spec.i_step or ls.switched) or np.sum(fs <= spec.check_to) < 6:
        return
    try:
        st = sampled_step(fs, z, ls, spec.i_step or 0.0, res.voltage, ls.rise, f_max=spec.check_to)
    except (ValueError, ZeroDivisionError, np.linalg.LinAlgError) as e:
        res.notes.append(f"{meta['part']}: no step response ({e})")
        return
    for name, t, v, m in st["nodes"]:
        # the dip after the first LATE_TAUS time constants of the low-pass at check_to: the decaps' and the
        # regulator's, apart from the spike of the copper's inductance at the start (L·ΔI·2π·check_to)
        late = t > LATE_TAUS / (2 * math.pi * spec.check_to)
        if np.any(late):
            dv = v[late] - res.voltage
            k = int(np.argmin(dv) if (spec.i_step or ls.switched) else np.argmax(dv))
            m["dip_late"], m["t_dip_late"] = float(dv[k]), float(t[late][k])
            m["peak_late"] = float(np.max(np.abs(dv)))
        if name == "port":                     # (a load node behind a harness: its figures in the notes)
            resp.step = {"t": t, "y": v, "metrics": m, "kind": "load", "v_nom": res.voltage, "max_dev": spec.ripple,
                         "label": meta["part"]}
        else:
            res.notes.append(f"{meta['part']}: at the load behind the harness the rail dips {m['dip'] * 1e3:+.3g} mV "
                             f"({m['dip_pct']:+.2f} %)")
    res.notes += [f"{meta['part']}: {n}" for n in st["notes"]]


def _decaps_near(model: BoardModel, setup: PdnSetup):
    """Every decap with the ICs it is near (its rail pad's distance to their supply pins), and decap_missing issues
    for supply pins with none within decap_mm."""
    board, config, res, spec = model.board, model.config, setup.res, setup.spec
    caps = decaps(board, config, spec.rail, res.ground)
    rows = []
    for fp in caps:
        m = config.model_for(fp.reference)
        cap_pads = [p for p in fp.pads if p.net == spec.rail]
        near = []
        for port in setup.ports:
            mm = min(math.dist(cp.position, pp.position) for cp in cap_pads for pp in port["pads"])
            if mm <= 3 * spec.decap_mm:
                near.append({"part": port["part"], "mm": round(mm, 2)})
        # C: nominal; C_dc: what the solve used, at the capacitor's DC voltage (analysis/bias.py)
        rows.append({"ref": fp.reference, "value": fp.value, "C": m.get("C"), "C_dc": m.capacitance(),
                     "near": sorted(near, key=lambda n: n["mm"])})
    res.decaps = sorted(rows, key=lambda r: r["ref"])
    pads = [(fp, [p for p in fp.pads if p.net == spec.rail]) for fp in caps]
    for port in setup.ports:
        far = []
        for pin in port["pads"]:
            mm = min((math.dist(pin.position, cp.position) for _, cps in pads for cp in cps), default=math.inf)
            if mm > spec.decap_mm:
                far.append((pin, mm))
        if far:
            pin, mm = max(far, key=lambda t: t[1])
            where = "no decap on the rail" if mm == math.inf else f"the nearest decap {mm:.1f} mm away"
            setup.issue(config.rules, "decap_missing",
                        f"{spec.label}: {len(far)} of {port['part']}'s {len(port['pads'])} supply pin(s) on {spec.rail} "
                        f"have no decap within {spec.decap_mm:g} mm ({pin.pin}: {where})",
                        tuple(pin.position), severity="warning", key=f"pdn:{spec.label}:{port['part']}:decap",
                        details={"part": port["part"], "pins": [p.pin for p, _ in far]})


def _port_step_figures(res: PdnResult, port: dict):
    """The port's step figures, taken from its response's step (set by _port_step before the port was listed)."""
    r = res.responses[-1] if res.responses else None
    st = getattr(r, "step", None)
    if st and st.get("label") == port["part"]:
        port["step"] = {"label": port["part"], **st["metrics"]}


def _issues(setup: PdnSetup, rules):
    """pdn_impedance: a port above the target below check_to (an error over it, a warning within WARN_DB);
    pdn_step: the voltage at a port moving by more than the ripple when its IC steps (or the coil is switched on),
    after the copper's spike at the start (peak_late: the decaps' and the source's response). The spike, L·ΔI over
    the edge the check_to low-pass leaves, is the impedance above the decaps that pdn_impedance already judges in
    frequency; judged again in time it flagged an off-board load at a connector port (5 A, 1.3 nH: −3.5 V in 0.5 ns
    on refboard's converter output) whose harness could never draw that edge."""
    res, spec = setup.res, setup.spec
    from ..components.values import format_si as _si
    for port in res.ports:
        m = port.get("step")
        peak = m.get("peak_late", m["peak"]) if m else None
        if m and res.voltage and peak > spec.ripple * res.voltage:
            spike = (f"; before it the copper's spike {m['dip'] * 1e3:+.3g} mV at {_si(m['t_dip'], 's')}"
                     if m.get("dip_late") is not None and abs(m["dip"]) > abs(m["dip_late"]) else "")
            late = m.get("dip_late", m["dip"]), m.get("t_dip_late", m["t_dip"])
            setup.issue(rules, "pdn_step",
                        f"{spec.label}: at {port['part']} the rail moves by {peak * 1e3:.3g} mV "
                        f"({100 * peak / res.voltage:.2f} %) when the load steps, more than the "
                        f"{spec.ripple * 100:g} % allowed (dips {late[0] * 1e3:+.3g} mV after {_si(late[1], 's')}"
                        f"{spike})", port["xy"], key=f"pdn:{spec.label}:{port['part']}:step",
                        details={"dip": late[0], "peak": peak, "t_dip": late[1], "spike": m["dip"]})
    if not res.target_ohm:
        return
    from ..components.values import format_si
    for port in res.ports:
        w = port["worst"]
        if w["margin_db"] >= WARN_DB:
            continue
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "pdn_impedance",
                    f"{spec.label}: {port['part']} sees {format_si(w['z_ohm'], 'Ω')} at {format_si(w['f'], 'Hz')} "
                    f"against the target {format_si(res.target_ohm, 'Ω')} (margin {w['margin_db']:+.1f} dB; held there "
                    f"by {w['holds']})", port["xy"], severity=sev, key=f"pdn:{spec.label}:{port['part']}:z",
                    details={k: w[k] for k in ("f", "z_ohm", "margin_db")})
