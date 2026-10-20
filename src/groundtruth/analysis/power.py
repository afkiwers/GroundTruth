"""Switching power stages (DC-DC converters): hot loop, input-capacitor current sharing, radiated and conducted
emission.

The switches commutate the load current within the edge time tr. The current that changes that fast flows around
the hot loop, rail capacitors -> high-side switch -> switch node -> low-side switch -> ground -> rail capacitors,
as a trapezoidal pulse train (amplitude I, duty D, edges tr). The inductor current is continuous and stays out of
it. A buck's hot loop sits on its input, a boost's on its output [Erickson 2001]; its spectrum is the trapezoid's
[Paul 2006]. Source keys in square brackets: docs/REFERENCES.md.

Model (one per converter, over the converter's region of the board):
* every net the loop touches is solved copper, pours and tracks alike: the rail, the switch node, ground and the
  input path up to the connector, plus every plane in the region;
* capacitors (C, ESR, ESL), inductors, ferrites and 0R are branches with their settings models, so the capacitors
  share the loop current according to their impedance and the copper between them;
* the switches are known currents: filaments through each package (up to PACKAGE_H_MM above the board), from its
  pads on one net to its pads on the other, injected into the copper at every pad (shared by pad area);
* with an input connector, one LISN per line (two in series for the differential-mode loop) closes the input:
  the conducted-emission measurement.

Solved at:
* LOOP_F_LO and LOOP_F: the hot-loop inductance, well above the capacitors' self-resonance; fitting a series LC
  through both points removes what is left of their reactance;
* the switching frequency (ripple-current sharing of the capacitors) and the conducted band, log-spaced and refined
  where the share of the loop current reaching the input changes quickly;
* the harmonics above 30 MHz for the radiated estimate, on the board-wide model with plane capacitance, as for nets.

With the switches' output capacitance c_oss, the loop rings at 1/(2π·sqrt(L·C_oss)) (a series RLC [Desoer 1969]):
the pulse spectrum is shaped by that series resonance (damping r_damp plus the loop's own resistance) and the
switch-node overshoot is estimated. The edges also charge the node (C_oss·dv/dt, Q_oss per edge where known): that
current adds to the load pulse (edge_charge_factor) and dominates near the ringing when V/I is large (a light load on a
high input voltage).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Optional

import numpy as np
from shapely.geometry import box

from ..components.defaults import is_electrolytic
from ..components.models import model_nets
from ..components.values import format_si
from ..config.settings import Config, ConverterSpec
from ..core.board import Board, Footprint, Pad
from ..core.issues import Issue, resolve_severity
from ..emi.conducted import conducted_table, describe, frequency_range, lisn_loop_impedance
from ..emi.radiation import emission_table, limit_range
from ..emi.spectrum import analysis_band, trapezoid_harmonics
from ..solver.peec import Branch, RouteExcitation, Solution, SolverError, TraceSegment
from .pins import feed_layers
from .simulate import (BoardModel, ModelRun, RouteResult, StackBuild, _choose_pitch, _interp_loglog,
                       _solve_row, _top_plane_pair, band_moments, initial_frequencies, transfers, unsolved_band,
                       unsolved_issue)

LOOP_F = 100e6              # hot-loop inductance: far above the input capacitors' self-resonance
LOOP_F_LO = 30e6            # second point of the series-LC fit
PACKAGE_H_MM = 0.25         # current path inside a switch package, above its pads [A30]
CAP_SEARCH_MM = 20.0        # rail capacitors this close to the switching cell are part of the model
POWER_MARGINS_MM = (5.0, 10.0, 20.0)   # model region around the cell, its capacitors and the input connector;
                                       # widened when the hot loop doesn't close within it
DEFAULT_R_DAMP = 0.1        # ohm: ringing that decays within a few cycles (no snubber), on top of the loop's own R
                            # [A31]
COND_PER_DECADE = 2.5       # initial conducted-band solves per decade
COND_MAX_SOLVES = 12
COND_REFINE_DB = 1.0        # split a conducted gap whose log-log interpolation may be off by more than this (dB)
COND_REFINE_MARGIN_DB = 20.0
CE101_PER_DECADE = 4         # CE101 solves per decade, 30 Hz-9.9 kHz (11): the lead against the bus capacitors
                             # can resonate in band (100 µH of lead with 300 µF: ~900 Hz) ...
CE101_MAX_SOLVES = 20        # ... so up to this many with the refinement (next_ce101)
CE101_REFINE_DB = 0.5        # split a CE101 gap whose log-log interpolation may be off by more than this (dB)
RING_F_MAX = 6e9
REPORT_PER_DECADE = 100     # emission rows kept per decade in reports (thousands of bins at kHz switching)
INPUT_PATH_MAX_OHMS = 10.0  # resistors up to this are series elements of the input path (sense, inrush)
DC_F = 10.0                 # the DC solves: resistive (skin depth >> copper), inductance negligible
SUSTAINED_MM = 1.0          # current density averaged over this window: what heats copper (corner peaks don't)
PEAK_MM = 0.25              # the peak density is over this window (or the cell, if larger): a node's own value grows
                            # without bound at a re-entrant corner as the grid is refined [A45]
FILTER_CAPS = (1e-6, 2.2e-6, 4.7e-6, 10e-6, 22e-6, 47e-6, 100e-6)   # input-filter capacitor choices
FILTER_L_MAX = 4.7e-6       # largest filter inductor proposed before a bigger capacitor is preferred


class ConverterError(ValueError):
    """A power stage's settings don't fit the board (a part missing, a switch without pads on its nets): reported
    as power_loop_broken."""
    pass


class _NoModel:
    """Stands in for a missing part model (kind "open"), so lookups need no None checks."""
    kind = "open"


_NO_MODEL = _NoModel()


@dataclass
class ConverterResult(RouteResult):
    """One power stage. Shares RouteResult's fields (heatmap, spectrum and report code): net = the converter's name,
    driver/receiver = its high-side and low-side parts (or the integrated stage); sheet_current, layer_share and
    transfers describe the loop current at LOOP_F."""
    kind: str = "converter"
    topology: str = "buck"
    parts: list = field(default_factory=list)
    f_sw: float = 0.0
    pulse_a: float = 0.0
    duty: float = 0.5
    rise_time: float = 0.0
    v_in: Optional[float] = None
    loop_nh: Optional[float] = None          # hot-loop inductance (series LC fit at LOOP_F_LO / LOOP_F)
    loop_mohm: Optional[float] = None        # its resistance at LOOP_F
    loop_f: float = LOOP_F
    overshoot_v: Optional[float] = None      # L·I/tr
    ringing: Optional[dict] = None           # f, q, r_ohm, overshoot_v
    caps: list = field(default_factory=list)        # rail capacitors: share of the loop current at LOOP_F and f_sw
    conducted: list = field(default_factory=list)   # receiver readings (rows)
    conducted_worst: Optional[dict] = None
    conducted_standard: str = ""
    ce101: list = field(default_factory=list)       # MIL-STD-461 CE101 readings (rows: f, i_dbua, limit_dbua, margin)
    ce101_worst: Optional[dict] = None
    ce101_limit: str = ""                           # the CE101 limit in words (Ce101.describe)
    input_transfer: list = field(default_factory=list)  # [{f, ratio}]: input line current / loop current
    ce101_transfer: list = field(default_factory=list)  # the same at the CE101 solves (the CE101 lead at the input)
    peak_density: dict = field(default_factory=dict)     # layer -> peak sheet current at LOOP_F, A/mm at pulse_a
    dc: list = field(default_factory=list)              # DC power paths: resistance, drop, loss, current density
    dc_unsolved: list = field(default_factory=list)     # DC paths whose solve failed: missing from `dc`, the input
                                                        # estimate's copper and Thermal's heat (each says so)
    input: Optional[dict] = None                        # DC input current from output power + losses (input_estimate)
    dc_branch: dict = field(default_factory=dict)       # path name -> (build, signed DC current per branch, A):
                                                        # the paths of all stages add up on the shared model (combine_dc)
    cm: Optional[dict] = None                           # common mode: switch-node capacitance budget (and estimate)
    input_filter: Optional[dict] = None                 # first-order LC input-filter guidance for the conducted margin
    fixes: list = field(default_factory=list)           # the fix advisor's design changes (analysis/fix_advisor.py)
    ripple: Optional[dict] = None                       # boost: the inductor ripple driving the conducted estimate
    dc_maps: dict = field(default_factory=dict)         # path name -> {layer: A/mm² map} (not in the summary)
    dc_grids: dict = field(default_factory=dict)        # path name -> {layer: Grid} of the model it was solved on
    dc_vectors: dict = field(default_factory=dict)      # path name -> {layer: (Jx, Jy) A/mm²} (not in the summary)
    # the switch node's time variance (dc_runs): it carries the current from the high side for D and from the low
    # side for 1 - D, so besides the mean (dc_vectors) its heat has D(1-D)·|J_hs - J_ls|² (A²/mm⁴ per layer), and
    # the per-branch amplitude √(D(1-D))·I·I_hs-ls (A, with its build: combine_dc adds it in quadrature)
    dc_ripple_sq: dict = field(default_factory=dict)    # path name -> {layer: map} (not in the summary)
    dc_ripple_branch: dict = field(default_factory=dict)  # path name -> (build, amplitude per branch, A)
    # the same currents at the vias only, kept after Simulate (dc_branch goes: it holds the whole-board model): the
    # via barrels' Joule heat for Thermal [A43]; path name -> (DcBoard, A at its vias), signed / RMS amplitude
    dc_via: dict = field(default_factory=dict)
    dc_ripple_via: dict = field(default_factory=dict)
    switch_pins: list = field(default_factory=list)     # the switches' D/S (A/K, VIN/SW/GND) pads, for the heatmaps
    location: tuple = (0.0, 0.0)
    layer: str = ""
    bridge: Optional[str] = None                        # a motor bridge's leg: the bridge's name and the phase
    phase: str = ""
    loop: Optional[dict] = None                         # the control loop (analysis/loop.py): fc, pm, gm_db, notes
    victims: list = field(default_factory=list)         # what the switching does to victim nets and parts (a stage of
                                                        # its own; a bridge's are on the bridge): victims.stage_rows
    # an interleaved stage (analysis/interleave.py): its phasor record until the group is combined (not in the
    # summary), then its group's figures with the stages together (the same dict on every member)
    phasors: Optional[dict] = None
    interleave: Optional[dict] = None

    def summary(self) -> dict:
        """The result as plain data for the JSON report: without the DC maps, grids and arrow fields (large
        arrays), the switch pins (only the maps use them) and an interleaved stage's phasors."""
        d = super().summary()
        d.pop("phasors", None)
        d.pop("dc_maps", None)
        d.pop("dc_grids", None)
        d.pop("dc_vectors", None)
        d.pop("dc_branch", None)
        d.pop("dc_ripple_sq", None)
        d.pop("dc_ripple_branch", None)
        d.pop("dc_via", None)
        d.pop("dc_ripple_via", None)
        d.pop("switch_pins", None)
        d["kind"] = "converter"
        return d


@dataclass
class Element:
    """A switch (or integrated stage) in the hot loop: the loop current enters at its `a` pads and leaves at its
    `b` pads, `weight` of it when several parts are in parallel."""
    ref: str
    fp: Footprint
    a: list[Pad]
    b: list[Pad]
    weight: float = 1.0


@dataclass
class ConverterSetup:
    """Everything needed to solve one power stage (duck-types RouteSetup for the shared scheduler)."""
    spec: ConverterSpec
    res: ConverterResult
    fine: ModelRun                       # the converter's region: hot-loop L, maps, radiated without cavity
    cavity: Optional[ModelRun]           # board-wide with plane capacitance (radiated band)
    cond: ModelRun                       # same model as fine: switching frequency and conducted band
    harmonics: np.ndarray
    pulse: np.ndarray                    # trapezoid harmonics of the loop current (peak A)
    currents: np.ndarray                 # the same with the ringing resonance applied (once L is known)
    elements: list[Element]
    f_ref: float = LOOP_F
    lisn: Optional[int] = None           # index of the LISN branch in the fine stack
    standard: str = "none"
    standard_class: str = "B"
    issues: list = field(default_factory=list)
    t_start: float = 0.0
    model_label: str = "power-stage"
    config: Optional[Config] = None
    dc_runs: list = field(default_factory=list)      # [(name, current A, ModelRun planned at DC_F)]
    dc_ripple: dict = field(default_factory=dict)    # path name -> (√(D(1-D)), ModelRun at DC_F of 1 A in at the
                                                     # high side's pads and out at the low side's: J_hs - J_ls)
    dc_hot: Optional[tuple] = None                   # (name, I_L·√(D(1-D)) A, ModelRun at DC_F): the hot loop's
                                                     # switching current from its capacitors (dc_runs, [A43])
    dc_extra: dict = field(default_factory=dict)     # path name -> A²: the inductor's ripple ΔI²/12 along the switch
                                                     # node path's own pattern (dc_summary's extra_var, [A43])
    cond_freqs: Optional[np.ndarray] = None           # harmonics driving the conducted estimate, when they are not
    cond_currents: Optional[np.ndarray] = None        # the hot loop's (a boost: the inductor's ripple current)
    victim_run: Optional[ModelRun] = None             # the hot loop on a model that reaches the victims (victims.py)
    victim_set: Optional[object] = None               # a stage's own victims (victims.VictimSet; a bridge: its own)
    ringing_model: Optional[tuple] = None             # (L, r, C_oss, snubber) of the ringing (stage_responses)
    probes: list = field(default_factory=list)        # their receivers' inputs in victim_run (victims.Probe)
    ce101_freqs: list = field(default_factory=list)   # the conducted run's CE101 solves (30 Hz-9.9 kHz, the CE101
                                                      # lead as the LISN branch): kept out of the CE102 transfer

    @property
    def c_freqs(self) -> np.ndarray:
        """The harmonics that drive the conducted estimate (the hot loop's, unless set apart: a boost's ripple)."""
        return self.harmonics if self.cond_freqs is None else self.cond_freqs

    @property
    def c_currents(self) -> np.ndarray:
        """The currents at c_freqs (A peak per harmonic)."""
        return self.currents if self.cond_currents is None else self.cond_currents

    @property
    def spectrum_run(self) -> ModelRun:
        """The run whose solves give the radiated estimate: the board-wide model when there is one."""
        return self.cavity if self.cavity is not None else self.fine

    @property
    def extra_runs(self) -> list[ModelRun]:
        """The stage's further runs for the scheduler (solve_setups): the conducted band, the DC paths and the
        victim model (a stage's own, or a bridge leg's)."""
        return [self.cond] + [r for _, _, r in self.dc_runs] + [r for _, r in self.dc_ripple.values()] + \
            ([self.dc_hot[2]] if self.dc_hot else []) + ([self.victim_run] if self.victim_run else [])

    def refine(self, run: ModelRun, config: Config) -> list[float]:
        """The next frequency to solve for one of the extra runs: only the conducted band is refined."""
        return (next_conducted(self, config) or next_ce101(self)) if run is self.cond else []

    def planned_solved(self):
        """After the first round: the loop inductance is known, so the ringing can shape the spectrum the
        radiated refinement weighs."""
        apply_ringing(self)

    def issue(self, rules, check, message, loc, severity=None, **kw):
        """Add an issue for this stage (unless its check is switched off)."""
        sev = resolve_severity(check, rules.severity, severity)
        if sev != "off":
            self.issues.append(Issue(check, sev, self.spec.name, message, (round(loc[0], 4), round(loc[1], 4)), **kw))


# --------------------------------------------------------------------------- the stage on the board
def elements_of(board: Board, spec: ConverterSpec) -> list[Element]:
    """The switches in loop order: high side (rail -> switch node), low side (switch node -> ground), or the
    integrated stage (rail -> ground)."""
    groups = [(spec.switch, spec.rail, spec.ground)] if spec.switch else \
        [(spec.high_side, spec.rail, spec.switch_node), (spec.low_side, spec.switch_node, spec.ground)]
    out = []
    for refs, na, nb in groups:
        for ref in refs:
            fp = board.footprint(ref)
            if fp is None:
                raise ConverterError(f"part {ref} not found")
            pa = [p for p in fp.pads if p.net == na]
            pb = [p for p in fp.pads if p.net == nb]
            if not pa or not pb:
                raise ConverterError(f"{ref} needs pads on {na} and {nb}")
            out.append(Element(ref, fp, pa, pb, 1.0 / len(refs)))
    return out


def _cap_model(config: Config, ref: str):
    """A part's capacitor model, or None when it is not modelled as a capacitor."""
    m = config.model_for(ref)
    return m if m is not None and m.kind == "cap" else None


def rail_caps(board: Board, config: Config, spec: ConverterSpec, centre, radius: float = CAP_SEARCH_MM
              ) -> list[Footprint]:
    """Capacitors between the rail and ground within `radius` of the switching cell, or, when the nearest one is
    farther, within 5 mm beyond it (the loop closes through the nearest capacitors, however far they are)."""
    found = []
    for fp in board.footprints:
        if _cap_model(config, fp.reference) is None or fp.nets() != {spec.rail, spec.ground}:
            continue
        found.append((min(math.dist(p.position, centre) for p in fp.pads), fp))
    if not found:
        return []
    reach = max(radius, min(d for d, _ in found) + 5.0)
    return [fp for d, fp in found if d <= reach]


def _passes(config: Config, fp: Footprint) -> bool:
    """A part a power path runs through: a short, an inductor or ferrite, or a resistor up to INPUT_PATH_MAX_OHMS
    (a current-sense shunt, not a bleeder)."""
    m = config.model_for(fp.reference)
    if m is None or m.kind not in ("short", "resistor", "inductor", "ferrite"):
        return False
    return m.kind != "resistor" or m.get("R") <= INPUT_PATH_MAX_OHMS


def input_path(board: Board, config: Config, start: str, goal: str, stop: set[str], max_depth: int = 6
               ) -> Optional[list[str]]:
    """Nets from `start` to `goal` through series parts that pass current (0R, fuses, ferrites, filter inductors,
    low-ohm resistors, and switches the user gave a short or resistor model, e.g. a surge stopper), never through
    `stop`. None when there is no such path."""
    if start == goal:
        return [start]
    by_net = board.parts_by_net()
    parent = {start: None}
    frontier = [start]
    for _ in range(max_depth):
        nxt = []
        for n in frontier:
            for fp in by_net.get(n, []):
                if not _passes(config, fp):
                    continue
                joined = model_nets(fp, config.model_for(fp.reference))
                if n not in joined:             # a current-sense IC joins only its IP+ / IP- nets
                    continue
                for other in joined:
                    if other and other not in parent and other not in stop:
                        parent[other] = n
                        if other == goal:
                            path = [other]
                            while parent[path[-1]] is not None:
                                path.append(parent[path[-1]])
                            return path[::-1]
                        nxt.append(other)
        frontier = nxt
    return None


_KEY_UNITS = {"rds_on": "Ω", "rds_on_low": "Ω", "dcr": "Ω", "c_oss": "F", "q_oss": "C", "v_sd": "V", "diode_vf": "V"}


def schematic_note(spec) -> str:
    """The note naming a stage's parameters that come from its parts' schematic fields."""
    return "from the schematic fields: " + ", ".join(f"{k} {format_si(getattr(spec, k), _KEY_UNITS.get(k, ''))}"
                                                      for k in spec.from_schematic if getattr(spec, k) is not None)


def feeding_stage(board: Board, config: Config, spec: ConverterSpec) -> Optional[str]:
    """The name of the buck whose output (its inductor's far side, or a net reached from it through series parts)
    is this stage's rail: refboard's LT3970 runs from the +12 V of the LM5008 buck."""
    for other in config.converters:
        if other is spec or other.name == spec.name or other.topology != "buck":
            continue
        sw, ind = switch_inductor(board, config, other)
        out = next((n for n in (ind.nets() if ind else ()) if n != sw), None)
        if out is not None and (out == spec.rail or
                                input_path(board, config, spec.rail, out, {spec.ground, other.rail}) is not None):
            return other.name
    return None


def switch_inductor(board: Board, config: Config, spec: ConverterSpec) -> tuple[Optional[str], Optional[Footprint]]:
    """(switch-node net, power inductor on it). For an integrated stage the switch node is the IC's net that
    an inductor shares."""
    def inductors_on(net):
        """The two-terminal parts modelled as inductors with a pad on `net`."""
        return [fp for fp in board.parts_by_net().get(net, []) if len(fp.nets()) == 2
                and (config.model_for(fp.reference) or _NO_MODEL).kind == "inductor"]
    if spec.switch_node:
        found = inductors_on(spec.switch_node)
        return spec.switch_node, (found[0] if found else None)
    from .detect import SW_PIN
    for ref in spec.switch:
        fp = board.footprint(ref)
        # the IC's SW/LX pin first: its output pin (V_OUT, BD) shares the inductor too, from the other side
        sw_pins = {p.net for p in (fp.pads if fp else ()) if p.net and SW_PIN.search(p.pin_function or "")}
        for net in sorted(fp.nets() if fp else (), key=lambda n: (n not in sw_pins, n)):
            if net in (spec.rail, spec.ground):
                continue
            found = inductors_on(net)
            if found:
                return net, found[0]
    return None, None


def path_parts(board: Board, config: Config, path: list[str]) -> list[Footprint]:
    """The series parts joining consecutive nets of an input path."""
    out: dict[str, Footprint] = {}
    for a_, b_ in pairwise(path):
        for fp in board.parts_by_net().get(a_, []):
            if _passes(config, fp) and {a_, b_} <= model_nets(fp, config.model_for(fp.reference)):
                out.setdefault(fp.reference, fp)
    return list(out.values())


def _side(fp: Footprint) -> str:
    """The outer layer a part sits on (B.Cu for a bottom-side part, else F.Cu)."""
    return "B.Cu" if fp.layer == "B.Cu" else "F.Cu"


def _pad_node(st: StackBuild, pad: Pad, side: str):
    """The model node a pad connects to: the nearest node of its net within the pad's reach (its half-size plus a
    cell), on the part's own side first, then the pad's other layers. None when no copper of its net is there."""
    r = max(1.5 * st.d, max(pad.size) / 2 + st.d)
    # a through-hole pin soldered only part of the way up (its `fill`, analysis/pins.py): its current enters the
    # board where the solder ends, not at the part's side
    side = getattr(st, "pin_feed", {}).get(pad.pin, side)
    order = [side] + [l for l in pad.layers if l != side] if side in pad.layers else list(pad.layers)
    for layer in order:
        n = st.node(layer, *pad.position, pad.net, r)
        if n is not None:
            return n
    return None


def switch_filaments(st: StackBuild, z_phys: dict, elements: list[Element]
                     ) -> tuple[list[TraceSegment], list[tuple]]:
    """Known currents through the switch packages, and the injections into the copper at their pads.

    Per element: from every `a` pad's grid node up to PACKAGE_H_MM, across to the package centre, across to above
    every `b` pad and down to its node; each pad carries a share of the element's current by pad area. The
    filaments start and end exactly at the injection nodes, so the loop (filaments + copper) is closed."""
    g0 = st.grids[0]

    def P(x, y, z):
        """A board point (mm) as a solver point (m, relative to the grid's node (0, 0))."""
        return np.array([(x - g0.x0) * 1e-3, (y - g0.y0) * 1e-3, z])

    trace: list[TraceSegment] = []
    inj: list[tuple] = []
    for el in elements:
        side = _side(el.fp)
        up = -1.0 if side == "B.Cu" else 1.0
        z_top = z_phys[side] + up * PACKAGE_H_MM * 1e-3
        pads = el.a + el.b
        cx = float(np.mean([p.position[0] for p in pads]))
        cy = float(np.mean([p.position[1] for p in pads]))
        centre = P(cx, cy, z_top)
        for group, sign in ((el.a, -1.0), (el.b, 1.0)):
            areas = np.array([max(p.size[0] * p.size[1], 1e-3) for p in group])
            for p, area in zip(group, areas):
                n = _pad_node(st, p, side)
                if n is None:
                    raise SolverError(f"{p.pin} ({p.net}): no {p.net} copper reaches the pad in the model (an unrouted "
                                      "net or unfilled zones?)")
                share = el.weight * float(area / areas.sum())
                inj.append((n, sign * share))     # the current leaves the copper at a pads, enters at b pads
                x, y = g0.xy(*n[1:])
                base, top = P(x, y, st.z[n[0]]), P(x, y, z_top)
                w = max(min(p.size), 0.3) * 1e-3
                legs = [(base, top), (top, centre)] if sign < 0 else [(centre, top), (top, base)]
                for a_, b_ in legs:
                    if np.linalg.norm(b_ - a_) > 1e-12:
                        trace.append(TraceSegment(a_, b_, w, share))
    merged: dict[tuple, float] = {}
    for n, amps in inj:
        merged[n] = merged.get(n, 0.0) + amps
    return trace, [(n, a) for n, a in merged.items() if abs(a) > 1e-12]


def _lisn_branch(st: StackBuild, board: Board, spec: ConverterSpec, standard: str) -> Branch:
    """The LISNs as one branch between the input connector's + and - pins: the differential-mode loop's
    impedance through both lines' LISNs (lisn_loop_impedance)."""
    plus, minus = (board.find_pin(p) for p in spec.input_connector)
    n_plus = _pad_node(st, plus, _side(board.footprint(plus.footprint)))
    n_minus = _pad_node(st, minus, _side(board.footprint(minus.footprint)))
    if n_plus is None or n_minus is None:
        raise SolverError("no copper at the input connector pins")
    return Branch(n_plus, n_minus, lisn_loop_impedance(standard), "LISN", "part")


def stage_power_nets(board: Board, config: Config, specs) -> dict[str, str]:
    """Every net that carries a power stage's current, with what it is for the stage ('the rail', 'the inductor
    L301's output side', ...): rail, ground, switch node, the input path, the inductor's far side and the output
    path from there to the output connector (through shunts, ferrites, 0R). None of them is a signal, even where a
    sense amplifier's input pin sits on one (Net-(U302-Vs+) between L301 and its 8 mOhm shunts)."""
    out: dict[str, str] = {}
    for spec in specs:
        owner = (f"leg {spec.phase} of the motor bridge {spec.bridge}" if spec.bridge
                 else f"the power stage {spec.name}")

        def add(net, what):
            """Record a net as this stage's (the first stage to claim a net keeps it)."""
            if net and net not in out:
                out[net] = f"{what} of {owner}"
        for net in sorted(converter_nets(board, config, spec) - {spec.rail, spec.ground, spec.switch_node}):
            add(net, "the input path")
        add(spec.rail, "the bus" if spec.bridge else "the rail")
        sw, inductor = switch_inductor(board, config, spec)
        add(sw, "the phase node" if spec.bridge else "the switch node")
        out_pad = board.find_pin(spec.phase_output) if spec.phase_output else None
        if sw and out_pad is not None and out_pad.net:           # to the motor (through shunts or a sensor)
            for net in input_path(board, config, sw, out_pad.net, {spec.rail, spec.ground}) or [out_pad.net]:
                add(net, "the phase output")
        if spec.bridge:                          # the legs' gate drives: part of the leg, not signals to follow
            from .detect import fet_terms
            for ref in spec.parts:
                fp = board.footprint(ref)
                terms = fet_terms(fp) if fp is not None else None
                if terms:
                    add(terms[0], "the gate drive")
        far = [n for n in (inductor.nets() if inductor is not None else ()) if n and n != sw]
        for net in far:
            add(net, f"the inductor {inductor.reference}'s output side")
        plus = board.find_pin(spec.output_connector[0]) if spec.output_connector else None
        if far and plus is not None and plus.net:
            path = input_path(board, config, far[0], plus.net, {spec.ground, sw} - {None}) or []
            for net in path:
                add(net, "the output path")
            if len(spec.output_connector) > 1:
                minus = board.find_pin(spec.output_connector[1])
                add(minus.net if minus is not None else None, "the output return")
        add(spec.ground, "the ground")
    return out


def converter_nets(board: Board, config: Config, spec: ConverterSpec, with_input: bool = True) -> set[str]:
    """Nets the converter's model must solve (pours or tracks): rail, switch node, ground, the input path."""
    nets = {spec.rail, spec.ground} | ({spec.switch_node} if spec.switch_node else set())
    if not with_input:
        return {n for n in nets if n}
    for pin in spec.input_connector:
        pad = board.find_pin(pin)
        if pad is not None and pad.net:
            nets.add(pad.net)
    if len(spec.input_connector) == 2:
        plus = board.find_pin(spec.input_connector[0])
        if plus is not None and plus.net:
            path = input_path(board, config, spec.rail, plus.net, {spec.ground} | nets - {spec.rail, plus.net})
            nets |= set(path or [])
    return {n for n in nets if n}


def switch_pins(board: Board, config: Config, spec: ConverterSpec) -> list[dict]:
    """The switches' power pads for the heatmaps (the user: "display the drain and source locations better"):
    [{"ref", "role", "net", "x", "y", "w", "h", "angle", "polygon", "layers"}] with role D / S (a FET's drain and
    source), K / A (a diode's cathode and anode) or VIN / SW / GND (an integrated stage). Told from the pads' nets
    (rail, switch node, ground), so it needs no schematic pin names: a buck's high side has its drain on the rail
    and its source on the switch node, the low side its drain on the switch node; a boost's diode its anode on
    the switch node. Gate and other pins are left out."""
    sw = spec.switch_node or switch_inductor(board, config, spec)[0]
    diode = lambda ref: ref.upper().startswith("D")      # noqa: E731
    roles: dict[str, dict[str, str]] = {}
    for ref in spec.high_side:                 # between rail and switch node
        roles[ref] = {spec.rail: "K" if diode(ref) else "D", sw: "A" if diode(ref) else "S"}
    for ref in spec.low_side:                  # between switch node and ground
        roles[ref] = {sw: "K" if diode(ref) else "D", spec.ground: "A" if diode(ref) else "S"}
    for ref in spec.switch:                    # an IC with both switches
        roles[ref] = {spec.rail: "VIN", sw: "SW", spec.ground: "GND"}
    out = []
    for ref, by_net in roles.items():
        fp = board.footprint(ref)
        for pad in (fp.pads if fp is not None else ()):
            role = by_net.get(pad.net) if pad.net else None
            if role is None or not pad.layers:
                continue
            out.append({"ref": ref, "role": role, "net": pad.net, "x": pad.position[0], "y": pad.position[1],
                        "w": pad.size[0], "h": pad.size[1], "angle": pad.angle,
                        "polygon": pad.polygon[0] if pad.polygon else None, "layers": list(pad.layers)})
    return out


# --------------------------------------------------------------------------- setup
def prepare_converter(model: BoardModel, spec: ConverterSpec, region: Optional[tuple] = None
                      ) -> tuple[Optional[ConverterSetup], ConverterResult, list[Issue]]:
    """region: (points, layers, nets) of a model shared by several stages (a motor bridge's legs: one model, so each
    frequency is factorised once and their input currents add on one grid) instead of the stage's own."""
    t_start = time.time()
    board, config = model.board, model.config
    a = config.analysis
    i_pulse, duty = spec.pulse()
    res = ConverterResult(spec.name, "+".join(spec.high_side or spec.switch),
                          "+".join(spec.low_side) if spec.low_side else "(integrated)",
                          topology=spec.topology, parts=spec.parts, f_sw=spec.f_sw, pulse_a=i_pulse, duty=duty,
                          rise_time=spec.rise_time, v_in=spec.v_in if spec.topology != "boost" else spec.v_out,
                          plane_capacitance=model.capacitance, bridge=spec.bridge, phase=spec.phase)
    res.switch_pins = switch_pins(board, config, spec)
    if spec.from_schematic:
        res.notes.append(schematic_note(spec))
    issues: list[Issue] = []

    def fail(msg: str, loc, key: str):
        """Give up on the stage: its result carries the error, and a power_loop_broken issue says why."""
        res.error = msg
        sev = resolve_severity("power_loop_broken", config.rules.severity)
        if sev != "off":
            issues.append(Issue("power_loop_broken", sev, spec.name, f"{spec.name}: {msg}",
                                (round(loc[0], 4), round(loc[1], 4)), key=f"power:{spec.name}:{key}"))
        return None, res, issues

    try:
        elements = elements_of(board, spec)
    except ConverterError as e:
        return fail(str(e), (0.0, 0.0), "parts")
    cell_pads = [p for el in elements for p in el.a + el.b]
    centre = (float(np.mean([p.position[0] for p in cell_pads])), float(np.mean([p.position[1] for p in cell_pads])))
    side = _side(elements[0].fp)
    res.location, res.layer = centre, side
    res.route_xy = [(_side(el.fp), [_centroid(el.a), _centroid(el.b)]) for el in elements]

    caps = rail_caps(board, config, spec, centre)
    if not caps:
        return fail(f"no capacitor between {spec.rail} and {spec.ground} within {CAP_SEARCH_MM:g} mm of the "
                    "switches (check the capacitor models in the settings)", centre, "caps")

    # Conducted measurement: a LISN at the input connector, if the input reaches the rail through series parts
    standard = (spec.conducted or a.conducted or "none").lower()
    requested = standard
    connector: list[Pad] = []
    boost_conn: list[Pad] = []
    series: list[Footprint] = []
    if spec.input_connector and standard != "none":
        connector = [board.find_pin(p) for p in spec.input_connector]
        stop = {spec.ground} | ({spec.switch_node} if spec.switch_node else set())
        path = None
        if len(connector) != 2 or any(p is None or not p.net for p in connector):
            res.notes.append(f"input connector {' / '.join(spec.input_connector)}: pins not found or not connected; "
                             "conducted emission not estimated")
            connector = []
        elif spec.topology == "boost":
            boost_conn = connector      # a boost draws its inductor current: modelled with the ripple below
            connector = []
        else:
            path = input_path(board, config, spec.rail, connector[0].net, stop)
            upstream = feeding_stage(board, config, spec) if path is None else None
            if upstream is not None:
                res.notes.append(f"{spec.rail} is the output of {upstream}: a cascaded stage, its input current reaches "
                                 f"{connector[0].pin} only through {upstream} (not modelled); conducted emission not "
                                 "estimated")
            elif path is None:
                res.notes.append(f"input connector {connector[0].pin} ({connector[0].net}) does not reach "
                                 f"{spec.rail} through parts with a series model: give series switches "
                                 "(reverse-polarity or surge-stopper FETs) a short or resistor model; conducted "
                                 "emission not estimated")
            if path is None:
                connector = []
            else:
                series = path_parts(board, config, path)
    if not connector:
        standard = "none"

    # Regions: the hot loop (cell, rail capacitors, the inductor's switch-node pad) and, for the conducted
    # estimate, the input path (plus the connector and the series parts on the way to it: they are branches only
    # if the model holds them; a surge stopper beyond the connector once cut the input path). When the input
    # path reaches far, it gets a model of its own, so the hot loop keeps a fine grid.
    sw_net, inductor = switch_inductor(board, config, spec)
    core_pts = [p.position for p in cell_pads] + [p.position for fp in caps for p in fp.pads] + \
        [p.position for p in (inductor.pads if inductor else []) if p.net == sw_net]
    input_pts = core_pts + [p.position for p in connector] + [p.position for fp in series for p in fp.pads]
    sw = {sw_net} if sw_net else set()
    core_nets = converter_nets(board, config, spec, with_input=False) | sw
    input_nets = converter_nets(board, config, spec, with_input=True) | sw
    core_layers = {_side(el.fp) for el in elements} | {_side(fp) for fp in caps}
    input_layers = core_layers | {_side(fp) for fp in series} | {l for p in connector for l in p.layers[:1]}
    if region is not None:                   # one model for every leg of a bridge
        core_pts = input_pts = list(region[0])
        core_layers = input_layers = set(region[1])
        core_nets = input_nets = set(region[2])
    split = bool(connector) and region is None and \
        _pitch(model, side, input_pts, input_layers) > 1.05 * _pitch(model, side, core_pts, core_layers)
    try:
        if connector and not split:
            st, exc, lisn, h_mm, margin = _loop_model(model, spec, elements, input_pts, input_layers, input_nets,
                                                      side, POWER_MARGINS_MM, standard)
            cond_st, cond_exc = st, exc
        else:
            st, exc, _, h_mm, margin = _loop_model(model, spec, elements, core_pts, core_layers, core_nets, side,
                                                   POWER_MARGINS_MM)
            cond_st, cond_exc, lisn = st, exc, None
            if split:
                cond_st, cond_exc, lisn, _, _ = _loop_model(
                    model, spec, elements, input_pts, input_layers, input_nets, side,
                    tuple(m for m in POWER_MARGINS_MM if m >= margin), standard)
                res.notes.append(f"input path modelled separately: {cond_st.nodes} nodes at {cond_st.d:.2f} mm "
                                 f"(hot loop {st.nodes} nodes at {st.d:.2f} mm)")
    except SolverError as e:
        msg = str(e)
        if "not closed" in msg or "balance" in msg:
            return fail(msg, centre, "loop")
        return fail(msg, centre, "plane")
    if margin > POWER_MARGINS_MM[0]:
        smaller = max(m for m in POWER_MARGINS_MM if m < margin)
        res.notes.append(f"the hot loop closes only through copper more than {smaller:g} mm from the switches and "
                         f"capacitors: model region widened to {margin:g} mm")
    res.plane_layers = st.names
    res.grid = st.grids[0]
    res.layer_grids = dict(zip(st.names, st.grids))
    res.height_mm = h_mm
    if lisn is None and connector:
        res.notes.append("LISN not connected (no copper at the input connector pins in the model)")
        standard = "none"
    res.conducted_standard = describe(standard, a.conducted_class) if standard != "none" else ""

    # Board-wide model with plane capacitance (radiated band)
    cavity = None
    if model.capacitance:
        cst = model.cavity_build(h_mm)
        if cst is not None:
            try:
                ctrace, cinj = switch_filaments(cst, model.z_phys, elements)
                cplane = cst.plane
                if lisn is not None:
                    try:
                        cplane = cplane.with_branches([_lisn_branch(cst, board, spec, standard)])
                    except SolverError:
                        pass
                cavity = ModelRun(cst, RouteExcitation(cplane, ctrace, injections=cinj))
                res.cavity = {"d_mm": cst.d, "nodes": cst.nodes, "layers": cst.names}
            except SolverError as e:
                res.notes.append(f"board-wide model: {e}")
        if cavity is None:
            res.notes.append("plane capacitance not modelled for this converter")
    res.dc_connected = exc.dc_connected

    f_max = max(analysis_band(spec.f_sw, spec.rise_time), 1e9 if spec.c_oss else 0.0)
    freqs, amps = trapezoid_harmonics(spec.f_sw, spec.rise_time, i_pulse, duty, min(f_max, RING_F_MAX))
    fine, cond = ModelRun(st, exc), ModelRun(cond_st, cond_exc)
    setup = ConverterSetup(spec, res, fine, cavity, cond, freqs, amps, amps.copy(), elements, LOOP_F, lisn,
                           standard, a.conducted_class, issues, t_start, config=config)
    fine.planned = [LOOP_F_LO, LOOP_F] + ([float(spec.f_sw)] if split else [])
    if cavity is not None:
        cavity.planned = initial_frequencies(setup, a.solve_points, limit_range(a.limit_spec()))
    # Without the board-wide model the hot loop's model is inductive: its moment is flat above a few MHz, so the
    # solves at 30 and 100 MHz carry it across the radiated band (interpolated, then held); more harmonics would
    # only repeat it (they cost ~15 s of CPU on the real board).
    cond.planned = _conducted_start(setup, with_fsw=not split)
    if boost_conn:
        res.notes += boost_conducted(model, setup, boost_conn, requested)
    if spec.bridge:                          # the bridge's own finish combines the legs at the input
        setup.dc_runs = []
    else:
        setup.dc_runs, notes = dc_runs(model, setup)
        res.notes += notes
    return setup, res, issues


def ripple_harmonics(f_sw: float, delta_i: float, duty: float, f_max: float) -> tuple[np.ndarray, np.ndarray]:
    """Harmonics of an asymmetric triangle (rising for duty·T, falling for the rest), peak-to-peak delta_i:
    c_n = delta_i·|sin(nπD)| / (π²·n²·D·(1−D)) (peak amplitudes)."""
    n = np.arange(1, max(int(f_max // f_sw), 1) + 1)
    c = delta_i * np.abs(np.sin(n * np.pi * duty)) / (np.pi ** 2 * n ** 2 * duty * (1 - duty))
    keep = c > delta_i * 1e-9
    return n[keep] * f_sw, c[keep]


def boost_conducted(model: BoardModel, setup: "ConverterSetup", connector: list[Pad], standard: str) -> list[str]:
    """A boost's input current is its inductor current: DC plus a triangular ripple ΔI = V_in·D/(L·f_sw),
    D = 1 − V_in/V_out (the low side's on-time). The ripple is drawn from the input net at the inductor and returns
    through the low side to ground; the input capacitors and the LISN share it. Its own input-side model (the
    inductor's input pad, the low side's ground pads, the input capacitors, the connector and the series parts on
    the way) gives the transfer to the input lines. Returns notes."""
    from dataclasses import replace
    board, config, spec, res = model.board, model.config, setup.spec, setup.res
    a = config.analysis
    sw_net, inductor = switch_inductor(board, config, spec)
    if inductor is None:
        return ["boost: no inductor on the switch node; conducted emission not estimated"]
    vin_net = next(n for n in inductor.nets() if n != sw_net)
    m = config.model_for(inductor.reference)
    L = m.get("L") if m is not None else 0.0
    if not (L > 0 and spec.v_in and spec.v_out and spec.v_out > spec.v_in):
        return ["boost: conducted emission needs v_in, v_out above it and the inductor's L (its model)"]
    if standard == "none":
        return []
    plus = connector[0]
    stop = {spec.ground, sw_net, spec.rail}
    path = input_path(board, config, vin_net, plus.net, stop)
    if path is None:
        return [f"input connector {plus.pin} ({plus.net}) does not reach {vin_net} through parts with a series "
                "model; conducted emission not estimated"]
    series = path_parts(board, config, path)
    vin_pads = [p for p in inductor.pads if p.net == vin_net]
    gnd_pads = [p for el in setup.elements if el.ref in spec.low_side for p in el.b]
    if not gnd_pads:
        return ["boost: the low side has no ground pads; conducted emission not estimated"]
    el = Element(inductor.reference, inductor, vin_pads, gnd_pads, 1.0)
    in_spec = replace(spec, rail=vin_net)
    caps = rail_caps(board, config, in_spec, _centroid(vin_pads))
    pts = [p.position for p in vin_pads + gnd_pads + connector] + [p.position for fp in caps + series for p in fp.pads]
    side = _side(inductor)
    layers = {side, _side(setup.elements[0].fp)} | {_side(fp) for fp in caps + series} | \
        {l for p in connector for l in p.layers[:1]}
    nets = {vin_net, spec.ground, sw_net} | {p.net for p in connector} | set(path)
    try:
        st, exc, lisn, _, _ = _loop_model(model, spec, [el], pts, layers, nets, side, POWER_MARGINS_MM, standard)
    except SolverError as e:
        return [f"boost input model: {e}"]
    if lisn is None:
        return ["LISN not connected (no copper at the input connector pins in the model)"]
    duty = 1 - spec.v_in / spec.v_out
    delta_i = spec.v_in * duty / (L * spec.f_sw)
    rng = frequency_range(standard, a.conducted_class)
    setup.cond = ModelRun(st, exc)
    setup.lisn, setup.standard = lisn, standard
    setup.fine.planned = sorted(set(setup.fine.planned) | {float(spec.f_sw)})   # ripple sharing on the hot loop
    setup.cond_freqs, setup.cond_currents = ripple_harmonics(spec.f_sw, delta_i, duty, rng[1] * 1.01)
    setup.cond.planned = _conducted_start(setup, with_fsw=False)
    res.conducted_standard = describe(standard, a.conducted_class)
    res.ripple = {"delta_i_pp": delta_i, "duty": duty, "L": L, "input_net": vin_net}
    return [f"boost: conducted emission from the inductor ripple ({delta_i:.2f} A p-p at D = {duty:.2f}, "
            f"L = {L * 1e6:g} µH), drawn from {vin_net}"]


def _pitch(model: BoardModel, side: str, pts, extra_layers: set) -> float:
    """Grid pitch a model around `pts` would get (its plane layers, the default margin): the input path gets a
    model of its own only when including it would coarsen the hot loop's grid."""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    m = POWER_MARGINS_MM[0]
    bbox = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
    layers = model.plane_layers(box(*bbox), extra_layers, set())
    stk = model.board.stackup
    h = min((stk.distance(side, l) for l in layers if l != side), default=0.2)
    a = model.config.analysis
    return _choose_pitch(h, bbox, max(len(layers), 1), a.max_nodes, a.grid_mm, a.resolution)


def _region_build(model: BoardModel, pts, extra_layers: set, nets: set, side: str, margin: float
                  ) -> tuple[StackBuild, float]:
    """(build, h): the inductive copper model (no plane capacitance) of the box around `pts` plus `margin`, clipped
    to the board outline: its plane layers and `nets`, at the pitch the node budget allows for the dielectric height
    h (mm) below `side`."""
    a = model.config.analysis
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    bbox = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    if not model.copper.outline.is_empty:
        ob = model.copper.outline.bounds
        bbox = (max(bbox[0], ob[0] - 0.5), max(bbox[1], ob[1] - 0.5), min(bbox[2], ob[2] + 0.5),
                min(bbox[3], ob[3] + 0.5))
    region = box(*bbox)
    layers = model.plane_layers(region, extra_layers, set())
    plane_nets = model.plane_nets(region, layers, set()) | nets
    h_mm = min((model.board.stackup.distance(side, l) for l in layers if l != side), default=0.2)
    d = _choose_pitch(h_mm, bbox, len(layers), a.max_nodes, a.grid_mm, a.resolution)
    return model.build(bbox, layers, plane_nets, d, False), h_mm


def _loop_model(model: BoardModel, spec: ConverterSpec, elements: list[Element], pts, extra_layers: set,
                nets: set, side: str, margins, lisn_standard: Optional[str] = None):
    """(build, excitation, LISN branch index, h, margin used): the copper model of the region around `pts`, the hot
    loop's filaments and injections on it and, with a standard, the LISN at the input connector. The margin widens
    while the loop doesn't close (the injections don't balance). Raises SolverError."""
    board = model.board
    for attempt, m in enumerate(margins):
        st, h_mm = _region_build(model, pts, extra_layers, nets, side, m)
        try:
            trace, inj = switch_filaments(st, model.z_phys, elements)
            plane = st.plane
            lisn = None
            if lisn_standard:
                plane = plane.with_branches([_lisn_branch(st, board, spec, lisn_standard)])
                if plane.branches and plane.branches[-1].name == "LISN":
                    lisn = len(plane.branches) - 1
            return st, RouteExcitation(plane, trace, injections=inj), lisn, h_mm, m
        except SolverError as e:
            if "balance" in str(e) and attempt + 1 < len(margins):
                continue
            if "balance" in str(e):
                raise SolverError(f"the hot loop is not closed in the copper model: {_pieces(st, elements)}") from e
            raise
    raise SolverError("no model region")


def _pieces(st: StackBuild, elements: list[Element]) -> str:
    """Which switch pads and capacitors sit on separate pieces of copper (and part branches): what keeps the hot
    loop from closing, e.g. an unrouted rail."""
    plane = st.plane
    groups: dict[int, dict[str, int]] = {}
    for el in elements:
        for p in el.a + el.b:
            n = _pad_node(st, p, _side(el.fp))
            if n is not None:
                g = groups.setdefault(int(plane.full_label[plane.nid[n]]), {})
                label = f"{p.pin} ({p.net})"
                g[label] = g.get(label, 0) + 1          # footprints may repeat a pad number (EPC: 5 × pad 7)
    for b in plane.branches:
        if b.kind == "part":
            k = int(plane.full_label[plane.nid[b.a]])
            if k in groups:
                groups[k].setdefault(b.name, 1)
    if len(groups) < 2:
        return "the switches' pads and the capacitors are not joined by copper and parts"
    texts = [", ".join(lab + (f" ×{n}" if n > 1 else "") for lab, n in g.items()) for g in groups.values()]
    more = f" | … {len(texts) - 6} more" if len(texts) > 6 else ""
    return (f"the copper falls apart into {len(texts)} separate pieces: {' | '.join(texts[:6])}{more}. Join them (route "
            "the rail and ground between the switches and the input capacitors: an unrouted net or unfilled zones?)")


def _centroid(pads: list[Pad]) -> tuple[float, float]:
    """The mean position of the pads."""
    return (float(np.mean([p.position[0] for p in pads])), float(np.mean([p.position[1] for p in pads])))


def _conducted_start(setup: ConverterSetup, with_fsw: bool = True) -> list[float]:
    """The switching frequency (capacitor ripple sharing, when the conducted run shares the hot loop's model)
    plus log-spaced points across the conducted band, and the CE101 band's (ce101_points) when CE101 is on."""
    picks = {float(setup.spec.f_sw)} if with_fsw else set()
    setup.ce101_freqs = ce101_points(setup)
    picks |= set(setup.ce101_freqs)
    cfg = setup.config
    if setup.ce101_freqs and cfg is not None and cfg.analysis.ce101 == "ships" and cfg.analysis.mil461 == "H":
        # MIL-STD-461H takes ships' CE101 to 20 kHz: that part reads the CE102 band's solves (ce101_transfer), which
        # start at 10 kHz and step to ~24 kHz; two more in between, with the LISN at the input like the rest
        picks |= {float(f) for f in np.geomspace(10e3, 20e3, 3)[1:]}
    rng = frequency_range(setup.standard, setup.standard_class) if setup.standard != "none" else None
    if rng:
        lo, hi = rng
        n = max(int(math.ceil(math.log10(hi / lo) * COND_PER_DECADE)), 2)
        for f in np.geomspace(lo, hi, n + 1):
            if all(abs(math.log(f / g)) > 0.05 for g in (LOOP_F_LO, LOOP_F)):
                picks.add(float(f))
    return sorted(picks)


def ce101_points(setup: ConverterSetup) -> list[float]:
    """The CE101 band's solves (log-spaced from 30 Hz to CE101_TOP_SOLVE, CE101_PER_DECADE per decade) when
    CE101 is on and the stage has content there: a bridge leg (the drive's lines every electrical frequency) or a
    converter with switching harmonics within the limit's range. Above 10 kHz the CE101 lead is not in the model
    (CE102's LISN is): MIL-STD-461H's ships range up to 20 kHz reads those solves (ce101_transfer)."""
    from ..emi.conducted import CE101_RANGE, CE101_TOP_SOLVE
    cfg = setup.config
    if cfg is None or cfg.analysis.ce101 == "none" or setup.standard == "none":
        return []
    cf = setup.c_freqs
    top = cfg.analysis.ce101_spec(0.0, 0.0).range[1]
    if not setup.spec.bridge and not np.any((cf >= CE101_RANGE[0]) & (cf <= top)):
        return []
    lo, hi = CE101_RANGE[0], CE101_TOP_SOLVE
    n = max(int(math.ceil(math.log10(hi / lo) * CE101_PER_DECADE)), 2)
    return [float(f) for f in np.geomspace(lo, hi, n + 1)]


# --------------------------------------------------------------------------- refinement of the conducted band
def _transfer(setup: ConverterSetup, ce101: bool = False) -> tuple[list[float], np.ndarray]:
    """Input line current per A of loop current at every solved frequency of the converter's own model: the
    CE102 / CISPR band's (default) or the CE101 solves' (ce101=True), whose LISN branch is the CE101 lead."""
    if setup.lisn is None:
        return [], np.zeros(0)
    same = setup.cond.exc is setup.fine.exc            # the LISN is only in the conducted run's model
    sols = {**(setup.fine.ok() if same else {}), **setup.cond.ok()}
    low = set(setup.ce101_freqs)
    fs = sorted(f for f in sols if (f in low) == ce101)
    return fs, np.array([abs(sols[f].I_branch[setup.lisn]) for f in fs])


def ce101_transfer(setup: ConverterSetup, top: float) -> tuple[list[float], np.ndarray]:
    """The input lead's current per A of loop current over a CE101 limit's range up to `top` (Hz): the CE101 solves
    (the CE101 lead at the input) below 10 kHz, and where the range goes on (MIL-STD-461H, ships: to 20 kHz) the
    CE102 band's solves from 10 kHz, whose input is the LISN as specified: the test's setup there [A34]."""
    from ..emi.conducted import CE101_RANGE
    fs, hs = _transfer(setup, ce101=True)
    if top <= CE101_RANGE[1]:
        return fs, hs
    up, hu = _transfer(setup)
    pairs = sorted([*zip(fs, hs), *((f, h) for f, h in zip(up, hu) if CE101_RANGE[1] <= f <= top * 1.5)])
    return [float(f) for f, _ in pairs], np.array([h for _, h in pairs])


def spread_of(spec: ConverterSpec):
    """(f_sw, Spread) when the stage dithers its switching frequency (`spread` in the settings), else None."""
    if not spec.spread:
        return None
    from ..emi.spread import Spread
    return spec.f_sw, Spread(spec.spread, spec.spread_mode, spec.spread_rate)


def _conducted_margin(setup: ConverterSetup, fs, hs) -> tuple[np.ndarray, np.ndarray, Optional[tuple]]:
    """(harmonics in the standard's range, their conducted margins, the range) with the input transfer hs at fs
    interpolated to the harmonics: the line current is the loop current times the share reaching the input."""
    from ..emi.conducted import conducted_arrays
    rng = frequency_range(setup.standard, setup.standard_class)
    H = setup.c_freqs
    sel = (rng[0] <= H) & (rng[1] >= H)
    f = H[sel]
    line = setup.c_currents[sel] * _interp_loglog(f, fs, hs)
    arr = conducted_arrays(f, line, setup.standard, setup.standard_class, spread_of(setup.spec))
    return f, arr["margin_db"], rng


def next_conducted(setup: ConverterSetup, config: Config) -> list[float]:
    """One frequency to solve next in the conducted band, or [] when it is resolved well enough.

    The transfer is interpolated linearly in log-log, which is exact on a power law (a capacitor against the LISN
    falls 20 dB/decade). What it misses is curvature: a self-resonance notch or a filter peak. So a gap is split
    when its interpolation error, estimated from the slope change against the neighbouring gaps
    (|Δslope|·width/8, the midpoint error of a chord on a parabola), exceeds COND_REFINE_DB, and its estimate
    comes within COND_REFINE_MARGIN_DB of the warning level. The gap closest to the limit goes first."""
    if setup.lisn is None or len(set(setup.cond.sols) - set(setup.ce101_freqs)) >= COND_MAX_SOLVES:
        return []
    fs, hs = _transfer(setup)
    rng = frequency_range(setup.standard, setup.standard_class)
    if not rng or len(fs) < 2:
        return []
    f_h, margins, _ = _conducted_margin(setup, fs, hs)
    floor = config.rules.emi_margin_db + COND_REFINE_MARGIN_DB
    best = None
    tried = set(np.round(list(setup.cond.sols) + (list(setup.fine.sols) if setup.cond.exc is setup.fine.exc
                                                    else []), 3))
    lx = np.log10(np.asarray(fs, float))
    ly = 20 * np.log10(np.maximum(np.asarray(hs, float), 1e-30))
    slope = np.diff(ly) / np.maximum(np.diff(lx), 1e-12)          # dB per decade, per gap
    for g in range(len(fs) - 1):
        fa, fb = fs[g], fs[g + 1]
        if fb < rng[0] or fa > rng[1] or fb / fa < 1.05 or hs[g] <= 0 or hs[g + 1] <= 0:
            continue
        bends = [abs(slope[g] - slope[k]) for k in (g - 1, g + 1) if 0 <= k < len(slope)]
        unc = max(bends, default=abs(slope[g])) * (lx[g + 1] - lx[g]) / 8
        if unc < COND_REFINE_DB:
            continue
        seg = margins[(f_h >= fa) & (f_h <= fb)]
        seg = seg[np.isfinite(seg)]
        if not len(seg) or seg.min() > floor:
            continue
        mid = math.sqrt(fa * fb)
        if round(mid, 3) in tried:
            continue
        score = unc - float(seg.min())
        if best is None or score > best[0]:
            best = (score, mid)
    return [best[1]] if best else []


def next_ce101(setup: ConverterSetup) -> list[float]:
    """One more CE101 solve where its transfer bends, or [] when it is resolved well enough (or CE101_MAX_SOLVES
    were made). The leads' inductance and the bus capacitors make a series resonance in the band; between solves 4
    per decade apart its peak would be cut off by the log-log interpolation. A gap is split (at its log midpoint)
    when its interpolation error, estimated as in next_conducted from the slope change against its neighbours,
    exceeds CE101_REFINE_DB; the worst gap first. The new frequency joins ce101_freqs (the CE101 lead there)."""
    if setup.lisn is None or not setup.ce101_freqs or len(setup.ce101_freqs) >= CE101_MAX_SOLVES:
        return []
    fs, hs = _transfer(setup, ce101=True)
    if len(fs) < 3:
        return []
    lx = np.log10(np.asarray(fs, float))
    ly = 20 * np.log10(np.maximum(np.asarray(hs, float), 1e-30))
    slope = np.diff(ly) / np.maximum(np.diff(lx), 1e-12)          # dB per decade, per gap
    planned = set(np.round(setup.ce101_freqs, 3))
    best = None
    for g in range(len(fs) - 1):
        if fs[g + 1] / fs[g] < 1.02:
            continue
        bends = [abs(slope[g] - slope[k]) for k in (g - 1, g + 1) if 0 <= k < len(slope)]
        unc = max(bends) * (lx[g + 1] - lx[g]) / 8
        mid = math.sqrt(fs[g] * fs[g + 1])
        if unc >= CE101_REFINE_DB and round(mid, 3) not in planned and (best is None or unc > best[0]):
            best = (unc, mid)
    if best is None:
        return []
    setup.ce101_freqs.append(float(best[1]))
    return [float(best[1])]


# --------------------------------------------------------------------------- results
def loop_inductance(s_lo: Optional[Solution], s_hi: Solution) -> tuple[float, Optional[float]]:
    """(L, C_eff): a series LC through the loop reactance at two frequencies, X = ωL - 1/(ωC), removes what
    is left of the capacitors' reactance. One frequency, or a non-physical fit: L = X/ω at the higher one."""
    w2 = 2 * math.pi * s_hi.f
    L_hi = s_hi.Z_loop.imag / w2
    if s_lo is None:
        return L_hi, None
    w1 = 2 * math.pi * s_lo.f
    x1, x2 = s_lo.Z_loop.imag, s_hi.Z_loop.imag
    L = (w2 * x2 - w1 * x1) / (w2 ** 2 - w1 ** 2)
    inv_c = w1 * w1 * L - w1 * x1
    if L <= 0 or inv_c <= 0 or 1.5 * L_hi < L:
        return L_hi, None
    return L, 1.0 / inv_c


def ringing_response(f, f_ring: float, q: float) -> np.ndarray:
    """|current / pulse current| of the series resonance (L_loop, C_oss) excited by the switching edges."""
    x = np.asarray(f, float) / f_ring
    return 1.0 / np.abs(1.0 - x * x + 1j * x / q)


def snubbed_response(f, L: float, r: float, c_oss: float, c_s: float, r_s: float) -> np.ndarray:
    """|node current / pulse current| of the loop (L, r) into the switch node's C_oss in parallel with an RC
    snubber (r_s + c_s): |Z_node / (jωL + r + Z_node)|. With r_s → ∞ it is ringing_response's series LC."""
    w = 2 * np.pi * np.asarray(f, float)
    z_c = 1.0 / (1j * w * c_oss)
    z_s = r_s + 1.0 / (1j * w * c_s)
    z_n = z_c * z_s / (z_c + z_s)
    return np.abs(z_n / (1j * w * L + r + z_n))


def ringing_rational(L: float, r: float, c_oss: float, snub: Optional[tuple[float, float]] = None
                     ) -> tuple[np.ndarray, np.ndarray]:
    """(numerator, denominator) in s of the node current / pulse current that snubbed_response (ringing_response
    without a snubber) evaluates: Z_n / (sL + r + Z_n) = 1 / (1 + (sL + r)·Y_n) with Y_n = sC_oss (+ the snubber's
    s·c_s / (s·r_s·c_s + 1)). Without a snubber 1 / (s²LC + s·rC + 1); with one
    (s·r_s·c_s + 1) / ((s·r_s·c_s + 1) + (sL + r)·(sC·(s·r_s·c_s + 1) + s·c_s))."""
    if snub is None:
        return np.array([1.0]), np.array([L * c_oss, r * c_oss, 1.0])
    c_s, r_s = snub
    p = np.array([r_s * c_s, 1.0])
    y_num = np.polyadd(np.polymul([c_oss, 0.0], p), [c_s, 0.0])
    return p, np.polyadd(p, np.polymul([L, r], y_num))


def edge_charge_factor(f, i_pulse: float, v: float, c_node: float, snub: Optional[tuple[float, float]] = None
                       ) -> np.ndarray:
    """|1 + Y_node·V/I|: the switch node's charge current on top of the load-current pulse.

    The edges move the node by V in tr, so besides the pulse I the hot loop carries Y_node·V_sw (C_oss·dv/dt at a
    bare node; with an RC snubber also its current). KCL at the node with the loop equation gives
    i = H·(I_pulse + Y_node·V_sw); both are trapezoids with the same duty and edges, so the sum is the pulse times
    |1 + Y_node·V/I| (with Y = jωC: sqrt(1 + (ωCV/I)²), the two in quadrature). c_node: the charge per volt the
    edge moves (C_oss, or Q_oss/V where the output charge is known: C_oss is larger at low voltage)."""
    w = 2 * np.pi * np.asarray(f, float)
    y = 1j * w * c_node
    if snub is not None:
        c_s, r_s = snub
        y = y + 1.0 / (r_s + 1.0 / (1j * w * c_s))
    return np.abs(1.0 + y * v / i_pulse)


NOMINAL_BIAS_V = 10.0       # from this DC bias on, a ceramic's nominal C is worth a note (class II loses ≥ 20 %)


def nominal_ceramics(board: Board, config: Config, refs: list[str], v: Optional[float]) -> list[str]:
    """The ceramic capacitors among refs (1 µF and up: class II) still at their nominal C where they see v."""
    if not v or v < NOMINAL_BIAS_V:
        return []
    out = []
    for ref in refs:
        m, fp = config.model_for(ref), board.footprint(ref)
        if m is None or m.kind != "cap" or fp is None or m.params.get("C_bias") or m.get("C") < 1e-6:
            continue
        if not is_electrolytic(fp):
            out.append(ref)
    return out


def _snubber_rc(config: Optional[Config], spec: ConverterSpec) -> Optional[tuple[float, float]]:
    """(C, R) of a stage's RC snubber from its parts' models, when both are known, or its proposed values
    (snubber_c, snubber_r: a what-if of the fix advisor)."""
    if not spec.snubber and spec.snubber_c and spec.snubber_r:
        return spec.snubber_c, spec.snubber_r
    if config is None or len(spec.snubber) != 2:
        return None
    mc, mr = (config.model_for(r) for r in spec.snubber)
    if mc is None or mr is None or mc.kind != "cap" or mr.kind not in ("resistor", "short"):
        return None
    c, r = mc.capacitance(), (mr.get("R") if mr.kind == "resistor" else 1e-3)
    return (c, r) if c > 0 else None


def apply_ringing(setup: ConverterSetup):
    """With c_oss: the ringing frequency, its Q (r_damp + the loop resistance), the spectrum shaped by it and the
    overshoot estimate. Needs the solve at LOOP_F."""
    spec, res = setup.spec, setup.res
    fine = setup.fine.ok()
    s_hi = fine.get(LOOP_F)
    if s_hi is None:
        return
    L, _ = loop_inductance(fine.get(LOOP_F_LO), s_hi)
    res.loop_nh = L * 1e9
    res.loop_mohm = s_hi.Z_loop.real * 1e3
    res.overshoot_v = L * res.pulse_a / spec.rise_time        # v = L di/dt over the edge [A14]
    snub = _snubber_rc(setup.config, spec)
    setup.currents, res.ringing = stage_spectrum(spec, setup.harmonics, setup.pulse, res.v_in, res.pulse_a, L,
                                                 s_hi.Z_loop.real, spec.rise_time, snub)
    if res.ringing is not None and snub is not None:
        res.ringing["snubber"] = {"c": snub[0], "r": snub[1], "parts": list(spec.snubber)}
    if res.ringing is not None:
        setup.ringing_model = (L, res.ringing["r_ohm"], spec.c_oss, snub)


def stage_responses(setup: ConverterSetup) -> list:
    """A power stage's Bode responses (analysis/transfer.py): the hot loop's ringing (the node current per A of the
    load pulse: an exact rational of L, the damping, C_oss and an RC snubber) and the input transfer (the current
    at the LISN per A of hot-loop current, the input filter's effect, at the conducted band's solves: complex, fitted).
    """
    from .transfer import exact_response, fitted_response
    spec, res, out = setup.spec, setup.res, []
    model = getattr(setup, "ringing_model", None)
    if model is not None:
        L, r, c_oss, snub = model
        num, den = ringing_rational(L, r, c_oss, snub)
        f_ring = res.ringing["f"]
        out.append(exact_response(f"{spec.name}: hot-loop ringing (node current / pulse)", spec.name,
                                  "current ratio", np.geomspace(f_ring / 100, f_ring * 100, 400), num, den))
    if setup.lisn is not None:
        # the LISN is a branch of the conducted run's model; the hot loop's solves count only when it is the same
        # model (an input path far away gets a model of its own: there the hot loop's model has no LISN)
        sols = {**(setup.fine.ok() if setup.fine.build is setup.cond.build else {}), **setup.cond.ok()}
        low = set(setup.ce101_freqs)
        fs = np.array(sorted(f for f in sols if f not in low))
        if len(fs) >= 6:
            ts = np.array([complex(sols[f].I_branch[setup.lisn]) for f in fs])
            resp = fitted_response(f"{spec.name}: input transfer (LISN current / loop current)", spec.name,
                                   "current ratio", fs, ts, max_order=10)
            resp.notes.append(f"{len(fs)} solved frequencies: the curve between them is the fit's")
            out.append(resp)
    return out


def pulse_harmonics(freqs: np.ndarray, f_sw: float, amplitude: float, duty: float, rise_time: float) -> np.ndarray:
    """The trapezoid's harmonic amplitudes (trapezoid_harmonics) on given harmonics of f_sw: another edge time on
    a stage's own frequency grid (the fix advisor)."""
    n = np.rint(np.asarray(freqs, float) / f_sw)
    return 2 * amplitude * duty * np.abs(np.sinc(n * duty)) * np.abs(np.sinc(n * f_sw * rise_time))


def stage_spectrum(spec: ConverterSpec, freqs: np.ndarray, pulse: np.ndarray, v: Optional[float], i_pulse: float,
                   L: float, r_loop: float, rise_time: float, snub: Optional[tuple[float, float]] = None
                   ) -> tuple[np.ndarray, Optional[dict]]:
    """(loop current per harmonic, the ringing) of a switching cell: the load pulse, shaped by the series resonance
    of the loop L with C_oss (damped by r_damp + the loop's resistance; with an RC snubber (C, R) across the node its
    own response, peak and Q), carrying the node's charge current (edge_charge_factor), and the overshoot of the
    switch node. Without c_oss: the pulse and no ringing. Shared by Simulate (apply_ringing) and the fix advisor."""
    if not (spec.c_oss and L > 0):
        return pulse.copy(), None
    f_ring = 1.0 / (2 * math.pi * math.sqrt(L * spec.c_oss))
    r = (spec.r_damp if spec.r_damp is not None else DEFAULT_R_DAMP) + max(r_loop, 0.0)
    q = math.sqrt(L / spec.c_oss) / max(r, 1e-6)
    if snub is None:
        currents = pulse * ringing_response(freqs, f_ring, q)
    else:                    # the RC snubber across the node damps it: its own response, peak and Q
        currents = pulse * snubbed_response(freqs, L, r, spec.c_oss, *snub)
        fg = f_ring * np.geomspace(0.2, 2.0, 2001)
        h = snubbed_response(fg, L, r, spec.c_oss, *snub)
        k = int(np.argmax(h))
        f_ring, q = float(fg[k]), float(h[k])
    ring = {"f": f_ring, "q": q, "r_ohm": r}
    if v and i_pulse > 0:
        c_node = spec.q_oss / v if spec.q_oss else spec.c_oss
        currents = currents * edge_charge_factor(freqs, i_pulse, v, c_node, snub)
        ring["charge_c"] = c_node
    if v and q > 0.5:
        zeta = 1 / (2 * q)
        # overshoot of a lightly damped series LC after a ramp of length tr (the edge): the second-order step
        # response's overshoot exp(-πζ/sqrt(1-ζ²)) [Ogata 2010], reduced by |sinc(f_ring·tr)| (a ramp is a step
        # averaged over tr: its ringing amplitude scales with the ramp's spectrum at f_ring)
        ring["overshoot_v"] = v * math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta)) * \
            abs(float(np.sinc(f_ring * rise_time)))
    return currents, ring


def _stage_row(f: float, s: Solution, model: str) -> dict:
    """A solve row of a power stage, with its reactance: below the capacitors' self-resonance the loop is
    capacitive, and Im(Z)/ω is no inductance (it read "-1829 nH" at 10 kHz), so L_nH is None there."""
    row = dict(_solve_row(f, s), model=model, X_ohm=s.Z_loop.imag)
    if s.Z_loop.imag < 0:
        row["L_nH"] = None
    return row


def finish_converter(model: BoardModel, setup: ConverterSetup) -> ConverterResult:
    """A power stage's result from its solves: the hot-loop inductance and ringing, the radiated estimate (loop
    current per harmonic times the loop moment per ampere), a plane resonance, the maps and the capacitors'
    current shares at LOOP_F, the conducted estimate (loop current times the input transfer through the LISN),
    the DC paths, the common-mode budget, the input-filter advice, the issues and the fix advisor's options."""
    config = model.config
    rules, a = config.rules, config.analysis
    res, spec = setup.res, setup.spec
    fine = setup.fine.ok()
    if LOOP_F not in fine:
        res.error = "solver did not converge at the hot-loop frequency"
        return res
    apply_ringing(setup)
    if setup.cond.exc is setup.fine.exc:
        res.solves = [_stage_row(f, s, "power stage") for f, s in sorted({**fine, **setup.cond.ok()}.items())]
    else:                       # separate input-path model: label the rows (its L includes the input path)
        res.solves = sorted([_stage_row(f, s, "hot loop") for f, s in fine.items()] +
                            [_stage_row(f, s, "input path") for f, s in setup.cond.ok().items()],
                            key=lambda r: r["f"])
    spectral = setup.spectrum_run.ok()
    if setup.cavity is not None:
        res.cavity_solves = [_solve_row(f, s) for f, s in sorted(spectral.items())]

    # Radiated: loop current per harmonic × loop moment per ampere
    fs, ms = band_moments(setup)
    freqs = setup.harmonics
    if fs:
        rad = freqs >= limit_range(a.limit_spec())[0] * 0.5
        m_curve = _interp_loglog(freqs[rad], fs, ms)
        # spread spectrum read by the receiver as for the conducted estimate (emi/spread.py)
        res.emissions = emission_table(freqs[rad], setup.currents[rad], m_curve, a.limit_spec(), a.distance_m,
                                       a.ground_reflection, per_decade=REPORT_PER_DECADE, spread=spread_of(setup.spec))
        compared = [r for r in res.emissions if r["margin_db"] is not None]
        if compared:
            res.worst = min(compared, key=lambda r: r["margin_db"])
            res.unsolved = unsolved_band(setup.spectrum_run, max(r["f"] for r in compared))
    # a plane resonance: the moment per ampere rising above its value at LOOP_F (a loop's moment is flat in
    # frequency until the board-wide cavity resonates)
    if setup.cavity is not None and len(fs) > 1:
        hi = [k for k, f in enumerate(fs) if f >= LOOP_F * 0.99]
        if hi:
            k0 = hi[0]
            k = k0 + int(np.argmax(ms[k0:]))
            rise = 20 * math.log10(ms[k] / ms[k0]) if ms[k0] > 0 else 0.0
            if rise > 1.0:
                res.resonance = {"f": fs[k], "rise_db": rise, "pair": _top_plane_pair(setup.cavity.build, spectral[fs[k]])}

    # Maps, current paths and capacitor sharing at LOOP_F
    rep = fine[LOOP_F]
    st = setup.fine.build
    res.map_frequency = rep.f
    share = rep.layer_share()
    res.layer_share = {n: float(s) for n, s in zip(st.names, share)}
    res.sheet_current = {n: rep.sheet_current(k, st.d) for k, n in enumerate(st.names)}
    res.sheet_vectors = {n: tuple(v.astype(np.float32) for v in rep.sheet_vectors(k, st.d))
                         for k, n in enumerate(st.names)}
    res.reference_layer = st.names[int(np.argmax(share))]
    res.peak_density = {n: float(K.max()) * res.pulse_a * 1e-3 for n, K in res.sheet_current.items() if K.size}
    plane = setup.fine.exc.stack
    res.transfers = [dict(t, distance_mm=math.dist((t["x"], t["y"]), res.location))
                     for t in transfers(st, plane, rep, None, [])]
    same = setup.cond.exc is setup.fine.exc
    s_sw = {**(setup.cond.ok() if same else {}), **fine}.get(float(spec.f_sw))
    res.caps = _cap_shares(config, spec, plane, rep, s_sw)
    nominal = nominal_ceramics(model.board, config, [c["name"] for c in res.caps if not c["lisn"]], res.v_in)
    if nominal:
        res.notes.append(f"{', '.join(nominal)}: ceramic, nominal capacitance at {res.v_in:g} V (an X7R keeps "
                         "roughly 30-60 % at half its rating): give their C at bias (Parts tab) from the maker's "
                         "DC-bias curve; with less capacitance the ripple and conducted emission rise")

    # Conducted: line current per harmonic = loop current × the share that reaches the input
    fs_c, hs_c = _transfer(setup)
    res.input_transfer = [{"f": f, "ratio": float(h)} for f, h in zip(fs_c, hs_c)]
    res.ce101_transfer = [{"f": f, "ratio": float(h)} for f, h in zip(*_transfer(setup, ce101=True))]
    if setup.standard != "none" and len(fs_c) and not spec.bridge:
        rng = frequency_range(setup.standard, setup.standard_class)
        cf = setup.c_freqs
        sel = (cf >= rng[0]) & (cf <= rng[1])
        line = setup.c_currents[sel] * _interp_loglog(cf[sel], fs_c, hs_c)
        res.conducted = conducted_table(cf[sel], line, setup.standard, setup.standard_class,
                                        per_decade=REPORT_PER_DECADE, spread=spread_of(setup.spec))
        if setup.spec.spread:
            sp = setup.spec
            res.notes.append(f"spread spectrum: f_sw dithered 0 to +{sp.spread * 100:g} % ({sp.spread_mode}"
                             + (f" at {format_si(sp.spread_rate, 'Hz')}" if sp.spread_rate else ", every cycle")
                             + "); conducted readings from a simulated receiver")
        compared = [r for r in res.conducted if r["margin_db"] is not None]
        if compared:
            res.conducted_worst = min(compared, key=lambda r: r["margin_db"])
        if len(hs_c) and float(np.max(hs_c)) < 1e-6:
            res.notes.append("no loop current reaches the input connector: is the input path modelled?")

    if not spec.bridge:                      # a bridge's leg: these come from the bridge (finish_bridge)
        res.dc, res.dc_maps, res.dc_grids, dc_notes = dc_analysis(model, setup)
        # the input current again, now with the DC paths' copper losses; the input path was solved at the estimate
        # without them, so its figures are scaled to the final current (a DC solve is linear in its current)
        res.input = input_estimate(model.board, config, spec, res, res.dc, res.dc_unsolved)
        if res.input is not None:
            for p in res.dc:
                if p["name"].startswith("input ") and p["current_a"] > 0:
                    _rescale_path(res, p, res.input["i_in_a"] / p["current_a"])
            res.notes.append(input_note(res.input, spec.v_in or res.v_in))
        res.cm = cm_estimate(spec, res, (spec.conducted or a.conducted or "none").lower(), a.conducted_class)
        res.input_filter = input_filter_advice(res.conducted, rules.emi_margin_db)
        res.notes += dc_notes
        _ce101_stage(setup, res, a)
        _stage_victims(model, setup)
        res.responses = stage_responses(setup)
        _stage_loop(model, setup)
    _converter_issues(setup, rules, a)
    if not spec.bridge:
        from .fix_advisor import stage_options
        res.fixes = stage_options(setup, model.board)
        if spec.interleave:                  # its group adds the stages' phasors at the end of the Simulate
            from .interleave import stage_phasors
            res.phasors = stage_phasors(setup, config)
    res.seconds = time.time() - setup.t_start
    return res


def _ce101_stage(setup: ConverterSetup, res: ConverterResult, a):
    """MIL-STD-461 CE101 of a converter (analysis.ce101): its harmonics within the limit's range (30 Hz to 10 kHz;
    461H ships to 20 kHz) times the transfer to the input lead (ce101_transfer), read by the current probe
    (emi/conducted.ce101_table). A converter has harmonics there only when it switches below the range's top;
    otherwise a note says what CE101 would see instead. Spread spectrum is not applied (conservative)."""
    from ..emi.conducted import ce101_table
    spec = setup.spec
    ce = a.ce101_spec(spec.v_in or res.v_in, (res.input or {}).get("i_in_a"))
    if ce is None:
        return
    res.ce101_limit = ce.describe()
    cf = setup.c_freqs
    lo, hi = ce.range
    sel = (cf >= lo) & (cf <= hi)
    if not sel.any():
        res.notes.append(f"CE101: no switching harmonic below {hi / 1e3:g} kHz (f_sw "
                         f"{format_si(spec.f_sw, 'Hz')}); the input lead's current there comes from load changes, "
                         "burst mode or a motor drive behind the stage, which are not modelled for a converter")
        return
    fs, hs = ce101_transfer(setup, hi)
    if not len(fs):
        res.notes.append("CE101 not estimated: no LISN at the input connector (set the stage's input_connector)")
        return
    line = setup.c_currents[sel] * _interp_loglog(cf[sel], fs, hs)
    res.ce101 = ce101_table(cf[sel], line, ce, per_decade=REPORT_PER_DECADE)
    compared = [r for r in res.ce101 if r["margin_db"] is not None]
    if compared:
        res.ce101_worst = min(compared, key=lambda r: r["margin_db"])


def _pad_nodes(st: StackBuild, pad: Pad, side: str) -> list[tuple]:
    """Every grid node of the pad's net inside its outline, on the layer _pad_node picks; at least the nearest.
    A DC current fed into one node would add a spreading resistance (a log singularity) of the order of
    0.1 mΩ per pad: as much as a whole power path."""
    import shapely
    from ..geometry.shapes import pad_shape
    n0 = _pad_node(st, pad, side)
    if n0 is None:
        return []
    k = n0[0]
    g = st.grids[k]
    poly = pad_shape(pad).buffer(st.d / 2)
    x0, y0, x1, y1 = poly.bounds
    i0, i1 = max(int((x0 - g.x0) / g.d) - 1, 0), min(int((x1 - g.x0) / g.d) + 2, g.nx)
    j0, j1 = max(int((y0 - g.y0) / g.d) - 1, 0), min(int((y1 - g.y0) / g.d) + 2, g.ny)
    ii, jj = np.meshgrid(np.arange(i0, i1), np.arange(j0, j1), indexing="ij")
    ii, jj = ii.ravel(), jj.ravel()
    xs, ys = g.xy(ii, jj)
    inside = shapely.contains_xy(poly, xs, ys) & g.node[ii, jj]
    if g.label is not None and g.nets:
        net_id = g.nets.index(pad.net) if pad.net in g.nets else -1
        inside &= g.label[ii, jj] == net_id
    nodes = [(k, int(i), int(j)) for i, j in zip(ii[inside], jj[inside])]
    return nodes or [n0]


def _area_shares(st: StackBuild, pads: list[Pad], side: str, total: float) -> list[tuple]:
    """Injections spreading `total` A over pads by area, and over each pad's own grid nodes evenly (a pad is fed
    over its whole area, like a lead frame)."""
    areas = [max(p.size[0] * p.size[1], 1e-3) for p in pads]
    out = []
    for p, a_ in zip(pads, areas):
        nodes = _pad_nodes(st, p, side)
        if not nodes:
            raise SolverError(f"{p.pin}: no {p.net} copper at the pad")
        share = total * a_ / sum(areas) / len(nodes)
        out += [(n, share) for n in nodes]
    merged: dict[tuple, float] = {}
    for n, amps in out:
        merged[n] = merged.get(n, 0.0) + amps
    return list(merged.items())


def sustained(density: np.ndarray, copper: np.ndarray, d_mm: float, grid=None) -> np.ndarray:
    """Current density averaged over the copper within a SUSTAINED_MM window: what heats the copper. A corner
    of a pad or via antipad shows a local peak that a grid cell resolves but that does not run hot.

    With the density's `grid` (peec.Grid): over each node's own net only, as the RMS weighted by the copper's
    area (Grid.copper_area), sqrt(Σ J²·A / Σ A): the density whose heat ρ·J²·t·A is the window's [A40]. Another
    net's copper in the window carries none of this current, and the plain mean over all copper counted it as
    copper at 0 A/mm²: a 0.1 mm track 0.2 mm beside a GND pour on its layer read a third less than alone
    (VALIDATION_AUDIT.md round 5, ISSUE-13). Without a grid, the plain mean over `copper` as before."""
    from scipy.ndimage import uniform_filter
    k = max(1, int(round(SUSTAINED_MM / d_mm)))
    if k <= 1:
        return density
    if grid is None or grid.label is None:
        num = uniform_filter(np.where(copper, density, 0.0), size=k, mode="constant")
        den = uniform_filter(copper.astype(float), size=k, mode="constant")
        return np.where(copper, num / np.maximum(den, 1e-12), 0.0)
    area = grid.copper_area()
    lab = np.where(copper, grid.label, -1)
    out = np.zeros(density.shape)
    for net in np.unique(lab[(lab >= 0) & (density > 0)]):
        on = lab == net
        w = np.where(on, area, 0.0)
        num = uniform_filter(w * density * density, size=k, mode="constant")
        den = uniform_filter(w, size=k, mode="constant")
        # (the filter's running sums can leave a rounding error just below zero where the window has no current)
        out = np.where(on, np.sqrt(np.maximum(num, 0.0) / np.maximum(den, 1e-12)), out)
    return out


def _box(width: float) -> np.ndarray:
    """The weights of a centred box `width` cells wide over the cells it covers (each cell [j - ½, j + ½] weighted
    by its overlap with [-width/2, width/2]): a window of the same physical size at any cell size, where a whole
    number of cells would round it (0.25 mm is 1 cell at 0.25 and 0.177 mm, 2 at 0.125 mm). At most a cell wide:
    the cell alone."""
    h = max(width, 1.0) / 2
    m = int(math.ceil(h - 0.5))
    j = np.arange(-m, m + 1, dtype=float)
    return np.clip(np.minimum(j + 0.5, h) - np.maximum(j - 0.5, -h), 0.0, 1.0)


def peak_density(density: np.ndarray, copper: np.ndarray, d_mm: float, grid=None) -> np.ndarray:
    """The current density as the peak figure reports it: the RMS over a PEAK_MM window of the node's own net's
    copper, √(Σ J²·A / Σ A) as `sustained` but over a quarter millimetre (the cell itself when it is larger).

    A node's own density is the grid's: at a re-entrant corner the 2D current density is singular, J ∝ r^(-1/3)
    [Jackson 1999, §2.11], so the node at the corner reads more on every finer grid (a 2 mm L at 10 A: 290 / 348 /
    366 A/mm² at 1x / 2x / 4x, as d^(-1/3)) and the "peak" was a property of the grid. Over a fixed window the
    singularity integrates to a finite value, so the figure converges with the grid (VALIDATION_AUDIT.md round 6,
    ISSUE-27) [A45]."""
    from scipy.ndimage import convolve1d
    k = _box(PEAK_MM / d_mm)
    if len(k) == 1:
        return density

    def window(a):
        """The box sum of a over both axes (zero outside the grid)."""
        return convolve1d(convolve1d(a, k, axis=0, mode="constant"), k, axis=1, mode="constant")

    if grid is None or grid.label is None:
        num = window(np.where(copper, density * density, 0.0))
        den = window(copper.astype(float))
        return np.where(copper, np.sqrt(np.maximum(num, 0.0) / np.maximum(den, 1e-12)), 0.0)
    area = grid.copper_area()
    lab = np.where(copper, grid.label, -1)
    out = np.zeros(density.shape)
    for net in np.unique(lab[(lab >= 0) & (density > 0)]):
        on = lab == net
        w = np.where(on, area, 0.0)
        num = window(w * density * density)
        den = window(w)
        out = np.where(on, np.sqrt(np.maximum(num, 0.0) / np.maximum(den, 1e-12)), out)
    return out


def dc_path(st: StackBuild, injections: list[tuple], current: float, name: str) -> tuple[dict, dict]:
    """DC (resistive) solve of `current` A entering the copper at the + injections and leaving at the - ones
    (shares of +1 and -1 A). Returns (summary, {layer: A/mm² map})."""
    sol = RouteExcitation(st.plane, [], injections=injections).solve(DC_F)
    return dc_summary(st, sol, current, name)


def dc_summary(st: StackBuild, sol: Solution, current: float, name: str, plating_um: float = 25.0,
               ripple: Optional[tuple] = None, extra_var: float = 0.0) -> tuple[dict, dict]:
    """Resistance, drop, loss, peak and sustained current density and via currents of a DC solution; each via
    with its IPC-2221 capacity for a 10 K rise (analysis.ipc) and its load in % of it. A part's plated hole is
    not a via here: its soldered pin carries the current between the layers (a connector pin's 6 A read as
    187 % of the plating's capacity on refboard).

    ripple = (k, sol_d): the path's current switches between two patterns (the switch node: from the high side for
    D, from the low side for 1 - D) whose difference per A is sol_d, k = √(D(1-D)). `sol` is their time average;
    the two-state variance adds in quadrature, exactly: the densities, via and part currents are RMS
    (√(mean² + k²·diff²)), R and the loss gain k²·R_d (Tellegen on sol_d [Tellegen 1952]), so loss = r·I² is the
    time-averaged Joule loss. (Before 2026-10-17 the switch node's mean alone: 6-17 % less copper loss on the test buck,
    VALIDATION_AUDIT.md ISSUE-7.)

    extra_var (A²): the variance of the current itself (an inductor's triangular ripple, ΔI²/12 [Erickson 2001]
    [A43]), which rides on whichever pattern carries it: every quadratic figure (densities, via and part currents,
    the loss, the switching share) takes the RMS current e = √(I² + extra_var) where it took I, the mean's
    pattern's variance (extra_var·|J|²) then included; the drop stays the mean's."""
    from .ipc import via_capacity
    k_r, sol_d = ripple if ripple is not None else (0.0, None)
    r_mean = float(sol.Z_loop.real)                  # V per A between the + and - nodes
    r_rip = k_r * k_r * float(sol_d.Z_loop.real) if sol_d is not None else 0.0
    e = math.sqrt(current * current + max(extra_var, 0.0))     # the mean pattern's RMS current
    gain = (e / current) ** 2 if current > 0 else 1.0
    r = (r_mean + r_rip) * gain                      # loss = r·I² = (r_mean + r_rip)·e²
    i_d = np.asarray(sol_d.I_branch) if sol_d is not None else np.zeros(len(sol.I_branch))
    maps, best, best_s = {}, (0.0, "", (0.0, 0.0)), (0.0, "", (0.0, 0.0))
    for k, layer in enumerate(st.names):
        g = st.grids[k]
        dens = sol.sheet_current(k, st.d, True) * e / st.t[k] * 1e-6          # A/mm² in the copper
        if sol_d is not None:
            dd = sol_d.sheet_current(k, st.d, True) * (k_r * e) / st.t[k] * 1e-6
            dens = np.sqrt(dens * dens + dd * dd)                               # RMS over the two states
        maps[layer] = dens
        if not dens.size or dens.max() <= 0:
            continue
        pk = peak_density(dens, g.node, st.d, g)
        i, j = np.unravel_index(int(np.argmax(pk)), pk.shape)
        if pk[i, j] > best[0]:
            best = (float(pk[i, j]), layer, tuple(float(v) for v in g.xy(i, j)))
        sus = sustained(dens, g.node, st.d, g)
        i, j = np.unravel_index(int(np.argmax(sus)), sus.shape)
        if sus[i, j] > best_s[0]:
            best_s = (float(sus[i, j]), layer, tuple(float(v) for v in g.xy(i, j)))
    vias: dict[str, dict] = {}
    for b, cur, cd in zip(st.plane.branches, sol.I_branch, i_d):
        if b.kind != "via" or b.name.startswith("pad "):    # a part's hole carries its pin, not only plating
            continue
        a_ = float(math.hypot(abs(cur), k_r * abs(cd))) * e             # RMS (the mean alone without ripple)
        if a_ < 0.01 * current:                          # vias of other nets, or idle ones
            continue
        dens = a_ / (math.pi * 2 * b.radius * plating_um * 1e-6) * 1e-6
        old = vias.get(b.name)
        if old is None or a_ > old["current_a"]:
            x, y = st.xy(b.a)
            cap = via_capacity(2 * b.radius * 1e3, plating_um)
            vias[b.name] = {"name": b.name, "current_a": a_, "a_mm2": dens, "x": x, "y": y,
                            "capacity_a": cap, "load_pct": 100.0 * a_ / cap if cap > 0 else None}
    top = sorted(vias.values(), key=lambda v: -v["current_a"])      # every via with ≥ 1 % of the current
    # Series parts on the path (a current-sense shunt, a fuse, a surge stopper's model) are in R: their share is
    # |I_b|²·Re(Z_b) per A² (Tellegen [Tellegen 1952]: the path's R is the sum over all branches), the rest is copper
    # and vias
    parts: dict[str, dict] = {}
    flow: dict[str, complex] = {}           # a part's parallel branches (simulate.part_pairs): their currents add
    flow_d: dict[str, complex] = {}
    for b, cur, cd in zip(st.plane.branches, sol.I_branch, i_d):
        if b.kind != "part":
            continue
        flow[b.name] = flow.get(b.name, 0j) + complex(cur)
        flow_d[b.name] = flow_d.get(b.name, 0j) + complex(cd)
        p = parts.setdefault(b.name, {"name": b.name, "current_a": 0.0, "r_mohm": 0.0})
        p["current_a"] = float(math.hypot(abs(flow[b.name]), k_r * abs(flow_d[b.name]))) * e
        p["r_mohm"] += (float(abs(cur)) ** 2 + (k_r * float(abs(cd))) ** 2) * gain * complex(b.z(DC_F)).real * 1e3
    parts = {n: p for n, p in parts.items() if math.hypot(abs(flow[n]), k_r * abs(flow_d[n])) >= 0.01}
    r_parts = sum(p["r_mohm"] for p in parts.values()) * 1e-3
    out = {"name": name, "current_a": current, "r_mohm": r * 1e3,
           "drop_mv": (r_mean + r_rip) * current * 1e3,
           "loss_w": r * current * current, "r_copper_mohm": (r - r_parts) * 1e3,
           "parts": sorted(parts.values(), key=lambda p: -p["r_mohm"]),
           "peak_a_mm2": best[0], "peak_layer": best[1], "peak_at": best[2],
           "sustained_a_mm2": best_s[0], "sustained_layer": best_s[1], "sustained_at": best_s[2],
           "vias": top}
    if sol_d is not None or extra_var > 0:      # RMS figures: the switching share of the loss, for the reports
        out["rms"] = True
        out["ripple_loss_w"] = r_rip * e * e + r_mean * max(extra_var, 0.0)
    return out, maps


DC_BUDGET = 3.0               # the whole-board DC model's node budget, times analysis.max_nodes (and the resolution)
COMBINED_VIEW = "currents together"   # the combined DC view's path name (its view_of): "DC currents together"
COMBINED_NET = "all power stages"


@dataclass
class DcBoard:
    """What combine_dc needs of the whole-board DC model (dc_board_build), without the model: its layers, grids,
    copper thicknesses and pitch, and its vias' branches (index, name, radius, position). A Simulate split over
    several compute servers (remote/split.py) solves each part's stages in a process of its own, on a model of its
    own; the parts send this instead of the model (hundreds of MB, and its branches' impedance closures can't be
    pickled), with each path's branch currents at the vias only. `fingerprint` tells whether two parts' models
    are the same model (built from the same board and settings, they are)."""
    names: list
    grids: list
    t: list
    d: float
    vias: list                      # [(branch index, name, radius m, x mm, y mm)]
    branches: int                   # the model's branch count (all kinds)
    fingerprint: str = ""
    via_ends: list = field(default_factory=list)   # [(layer a, layer b, R ohm at DC)] with `vias`: each barrel
                                                   # segment's two layers and resistance (its heat, thermal [A43])

    def via_currents(self, i_branch) -> np.ndarray:
        """A path's branch currents (the full model's) at this model's vias, in the order of `vias`."""
        return np.asarray(i_branch)[[v[0] for v in self.vias]] if self.vias else np.zeros(0)


def dc_board_of(st: StackBuild) -> DcBoard:
    """The DcBoard of a whole-board DC model (cached on it: every stage's paths share the one model)."""
    got = getattr(st, "_dc_board", None)
    if got is not None:
        return got
    import hashlib
    vias, ends = [], []
    for k, b in enumerate(st.plane.branches):
        if b.kind == "via" and not b.name.startswith("pad "):
            x, y = st.xy(b.a)
            vias.append((k, b.name, float(b.radius), x, y))
            ends.append((st.names[b.a[0]], st.names[b.b[0]], float(complex(b.z(DC_F)).real)))
    # rounded figures only: the same board and settings give the same text in every process (no hash() salts)
    h = hashlib.sha256()
    h.update(repr((list(st.names), [round(float(t), 12) for t in st.t], round(float(st.d), 9),
                   len(st.plane.branches))).encode())
    for g in st.grids:
        h.update(repr((round(g.x0, 6), round(g.y0, 6), round(g.d, 9), g.nx, g.ny, int(g.node.sum()))).encode())
    for k, name, r, x, y in vias:
        h.update(f"{k}|{name}|{r:.9g}|{x:.6f}|{y:.6f};".encode())
    out = DcBoard(list(st.names), list(st.grids), list(st.t), float(st.d), vias, len(st.plane.branches),
                  h.hexdigest()[:16], ends)
    try:
        st._dc_board = out
    except AttributeError:                          # a build without a __dict__ slot for it: not cached
        pass
    return out


def compact_dc_branch(res) -> None:
    """A stage result's dc_branch made portable (a split Simulate's part): {path: (DcBoard, currents at its vias)}
    in place of {path: (model, currents at every branch)}."""
    res.dc_branch = {name: (dc_board_of(st) if isinstance(st, StackBuild) else st,
                            dc_board_of(st).via_currents(cur) if isinstance(st, StackBuild) else cur)
                     for name, (st, cur) in (res.dc_branch or {}).items()}
    res.dc_ripple_branch = {name: (dc_board_of(st) if isinstance(st, StackBuild) else st,
                                   dc_board_of(st).via_currents(cur) if isinstance(st, StackBuild) else cur)
                            for name, (st, cur) in (getattr(res, "dc_ripple_branch", None) or {}).items()}


def dc_nets(board: Board, config: Config, spec: ConverterSpec) -> set[str]:
    """The nets a stage's DC paths run on: rail, switch node, ground and the input path (converter_nets), the
    inductor's far side, the output path through its series parts to the output connector, and both connectors'
    pins' nets."""
    nets = set(converter_nets(board, config, spec))
    sw_net, inductor = switch_inductor(board, config, spec)
    out_net = next((p.net for p in inductor.pads if p.net and p.net != sw_net), None) if inductor is not None else None
    if out_net:
        nets.add(out_net)
    pins = [board.find_pin(p) for p in list(spec.output_connector) + list(spec.input_connector)]
    nets |= {p.net for p in pins if p is not None and p.net}
    if out_net and spec.output_connector:
        plus = board.find_pin(spec.output_connector[0])
        if plus is not None and plus.net:
            path = input_path(board, config, out_net, plus.net, {spec.ground, sw_net, spec.rail} - {None})
            nets |= set(path or [])
    return {n for n in nets if n}


def dc_board_build(model: BoardModel):
    """The whole-board copper model every power stage's DC paths are solved on (built once per model, None
    without a board outline): all plane nets and every stage's DC nets on every layer with plane copper, at the
    pitch DC_BUDGET times the node budget allows. One model for all stages and paths: nothing is cut off at a
    region's edge (a 5 mm box around the parts squeezed refboard's ground return at its right edge, +27 %
    on its hotspot), its factorisation is shared by all the DC solves, and the stages' currents add up on its one
    grid (combine_dc)."""
    with model._lock:
        if hasattr(model, "_dc_board"):
            return model._dc_board
        model._dc_board = None
        bbox = model.board_region()
        if bbox is None:
            return None
        a = model.config.analysis
        nets, comp_layers = set(), set()
        for spec in model.config.converters:
            nets |= dc_nets(model.board, model.config, spec)
            for ref in spec.parts:
                fp = model.board.footprint(ref)
                if fp is not None:
                    comp_layers.add(_side(fp))
        # DC rails with loads (analysis/dcload.py): their pins' nets, the nets between the source and each load
        # through series parts (a fuse, a ferrite), and their parts' sides
        for d in getattr(model.config, "dc_loads", ()):
            pads = [p for p in (model.board.find_pin(x) for x in d.pins) if p is not None]
            nets |= {p.net for p in pads if p.net}
            src = model.board.find_pin(d.source)
            for ld in d.loads:
                pad = model.board.find_pin(ld.pin)
                if src is not None and pad is not None and src.net and pad.net and pad.net != src.net:
                    nets |= set(input_path(model.board, model.config, src.net, pad.net, set()) or [])
            for x in d.pins:
                fp = model.board.footprint(x.split(".")[0])
                if fp is not None:
                    comp_layers.add(_side(fp))
        # a fed pin soldered only part of the way up (analysis/pins.py `fill`) feeds the board where its solder
        # ends: that layer is modelled even when it holds nothing else of the net (the current climbs the hole)
        fed = [x for d in getattr(model.config, "dc_loads", ()) for x in d.pins] + \
            [x for spec in model.config.converters for x in (*spec.input_connector, *spec.output_connector)]
        comp_layers |= feed_layers(model.board, model.config, fed, model.z_phys)
        nets &= set(model.board.nets)
        region = box(*bbox)
        layers = model.plane_layers(region, comp_layers & set(model.board.copper_layers), set())
        plane_nets = model.plane_nets(region, layers, set()) | nets
        # the same starting pitch as a stage's region model (the dielectric height below the parts' side): the DC
        # solve has no inductance, but a coarser cell widens narrow tracks (a 1 mm track on 0.25 mm cells read
        # 16 % low); on a large board the node budget binds first anyway
        stk = model.board.stackup
        h_mm = min((stk.distance(s, l) for s in comp_layers for l in layers if l != s), default=0.25)
        d = _choose_pitch(h_mm, bbox, len(layers), int(a.max_nodes * DC_BUDGET), a.grid_mm, a.resolution)
        model._dc_board = model.build(bbox, layers, plane_nets, d, False)
        return model._dc_board


def input_estimate(board: Board, config: Config, spec, res, dc: list = (), unsolved: list = ()) -> Optional[dict]:
    """The stage's DC input current from an energy balance, V_in·I_in = V_out·I_out + P_loss, instead of the
    lossless I_out·D (a buck) or I_out/(1-D) (a boost).

    The losses are the ones Thermal heats the parts with (thermal.part_losses: the switches' conduction, the hard
    edge's overlap and output charge, a catch or boost diode, the inductor's DCR, series parts on the DC paths)
    and, once the DC paths are solved (`dc`), their copper (r_copper·I²). Gate drive and the controller's own
    supply are not included. A part without its value (no rds_on, no dcr) counts as lossless: `missing` says so,
    as it does for a DC path in `unsolved` (its solve failed: its copper and series parts are not in the loss).
    Returns {i_in_a, i_in_ideal_a, p_out_w, p_loss_w, copper_w, efficiency, losses: [{ref, power_w, what}],
    missing: [notes]}, or None without an input voltage, an output voltage (or duty) and an output current."""
    from dataclasses import replace
    from types import SimpleNamespace
    from .thermal import part_losses
    v_in = spec.v_in or res.v_in
    if not v_in or not spec.i_out:
        return None
    boost = spec.topology == "boost"
    v_out = spec.v_out or (None if boost else res.duty * v_in)
    if not v_out:
        return None
    p_out = v_out * spec.i_out
    # part_losses reads the stage from the settings by name: give it this spec (a what-if or test spec may differ)
    one = replace(config, converters=[replace(spec, name=res.net)])
    probe = SimpleNamespace(net=res.net, duty=res.duty, v_in=v_in, dc=list(dc))
    rows, missing = part_losses(board, one, [probe])
    missing = list(missing) + [f"DC {name} not solved: its copper and series parts are not in the loss"
                               for name in unsolved]
    copper = sum(p.get("r_copper_mohm", 0.0) * 1e-3 * p["current_a"] ** 2 for p in dc)
    p_loss = sum(r["power_w"] for r in rows) + copper
    return {"i_in_a": (p_out + p_loss) / v_in, "i_in_ideal_a": p_out / v_in, "p_out_w": p_out, "p_loss_w": p_loss,
            "copper_w": copper, "efficiency": p_out / (p_out + p_loss), "losses": rows, "missing": missing}


def _rescale_path(res, path: dict, k: float):
    """A solved DC path at k times its current (a DC solve is linear): currents, drop, densities and the maps by
    k, losses by k²; the resistances stay."""
    if abs(k - 1.0) < 1e-12:
        return
    for key in ("current_a", "drop_mv", "peak_a_mm2", "sustained_a_mm2"):
        if isinstance(path.get(key), (int, float)):
            path[key] *= k
    path["loss_w"] = path.get("loss_w", 0.0) * k * k
    for v in path.get("vias", []):
        for key in ("current_a", "a_mm2", "load_pct"):
            if isinstance(v.get(key), (int, float)):
                v[key] *= k
    for p in path.get("parts", []):
        if isinstance(p.get("current_a"), (int, float)):
            p["current_a"] *= k
    name = path["name"]
    if name in res.dc_maps:
        res.dc_maps[name] = {l: m * k for l, m in res.dc_maps[name].items()}
    if name in res.dc_vectors:
        res.dc_vectors[name] = {l: tuple(c * k for c in v) for l, v in res.dc_vectors[name].items()}
    if name in getattr(res, "dc_branch", {}):
        st, cur = res.dc_branch[name]
        res.dc_branch[name] = (st, cur * k)
    if name in getattr(res, "dc_ripple_sq", {}):
        res.dc_ripple_sq[name] = {l: m * (k * k) for l, m in res.dc_ripple_sq[name].items()}
    if name in getattr(res, "dc_ripple_branch", {}):
        st, cur = res.dc_ripple_branch[name]
        res.dc_ripple_branch[name] = (st, cur * k)


def input_note(est: dict, v_in: float) -> str:
    """input_estimate as a sentence for the stage's notes."""
    top = ", ".join(f"{r['ref']} {r['power_w']:.2g} W" for r in est["losses"][:4])
    return (f"input current {est['i_in_a']:.3g} A at {v_in:g} V: {est['p_out_w']:.3g} W out + {est['p_loss_w']:.2g} W "
            f"lost (efficiency {est['efficiency'] * 100:.1f} %; lossless {est['i_in_ideal_a']:.3g} A)"
            + (f"; losses {top}" + (f", copper {est['copper_w']:.2g} W" if est["copper_w"] else "") if top else "")
            + ("; " + "; ".join(est["missing"]) if est["missing"] else ""))


def dc_runs(model: BoardModel, setup: "ConverterSetup") -> tuple[list, list[str]]:
    """The DC power paths as runs of their own (solved with everything else in the parallel phase): the load
    current through the switch node to the inductor (on the hot loop's model), and the input current from the
    connector to the high-side switch (on the input path's model, without the LISN). Returns (runs, notes).
    The input current is input_estimate's (output power plus the parts' losses; the copper's are known only after
    these solves, a fraction of a per cent of it on refboard), else the lossless one."""
    board, config, spec, res = model.board, model.config, setup.spec, setup.res
    i_out, duty = spec.i_out, res.duty
    out, notes = [], []
    res.input = input_estimate(board, config, spec, res)
    try:                                    # every DC path of every stage on one whole-board model
        dst = dc_board_build(model)
    except SolverError as e:
        dst = None
        notes.append(f"DC paths on the stage's own models: the whole-board DC model failed ({e})")
    sw_net, inductor = switch_inductor(board, config, spec)
    if sw_net and inductor is not None:
        st = dst or setup.fine.build
        try:
            inj = []
            if spec.switch:
                ic = setup.elements[0]
                inj += _area_shares(st, [p for p in ic.fp.pads if p.net == sw_net], _side(ic.fp), 1.0)
            else:
                hs = [el for el in setup.elements if el.ref in spec.high_side]
                ls = [el for el in setup.elements if el.ref in spec.low_side]
                for els, part in ((hs, duty), (ls, 1.0 - duty)):
                    for el in els:
                        pads = el.b if el in hs else el.a
                        inj += _area_shares(st, pads, _side(el.fp), part * el.weight)
            inj += _area_shares(st, [p for p in inductor.pads if p.net == sw_net], _side(inductor), -1.0)
            run = ModelRun(st, RouteExcitation(st.plane, [], injections=inj), planned=[DC_F])
            # the inductor current: the load current for a buck, the input current for a boost
            i_sw = res.pulse_a if spec.topology == "boost" else i_out
            name = f"switch node → {inductor.reference}"
            out.append((name, i_sw, run))
            # the inductor current's triangular ripple ΔI p-p rides on this path: a variance ΔI²/12 along its own
            # pattern [Erickson 2001] [A43]. ΔI = V_in·D(1−D)/(L·f_sw) for a buck (D its high side's on-time), V_in·D_sw
            # /(L·f_sw) for a boost (D_sw = 1 − D, its switch's)
            lm = config.model_for(inductor.reference)
            l_h = lm.get("L") if lm is not None and lm.kind == "inductor" else 0.0
            v_in = spec.v_in or res.v_in
            if l_h > 0 and spec.f_sw and v_in and 0.0 < duty < 1.0:
                d_on = (1.0 - duty) if spec.topology == "boost" else duty * (1.0 - duty)
                di = v_in * d_on / (l_h * spec.f_sw)
                setup.dc_extra[name] = di * di / 12.0
            if not spec.switch and hs and ls and 0.0 < duty < 1.0:
                # the injection above is the time average; the node carries the whole current from the high side
                # for D and from the low side for 1 - D. Its variance is D(1-D)·(J_hs - J_ls)²: one more solve,
                # 1 A in at the high side's pads and out at the low side's (dc_summary takes it in quadrature)
                rip = [x for el in hs for x in _area_shares(st, el.b, _side(el.fp), el.weight)] + \
                    [x for el in ls for x in _area_shares(st, el.a, _side(el.fp), -el.weight)]
                setup.dc_ripple[name] = (math.sqrt(duty * (1.0 - duty)),
                                         ModelRun(st, RouteExcitation(st.plane, [], injections=rip), planned=[DC_F]))
        except SolverError as e:
            notes.append(f"DC switch-node path not modelled: {e}")
        if 0.0 < duty < 1.0:
            try:
                hot = _hot_loop_injections(board, config, spec, setup, st)
            except SolverError as e:
                hot, more = None, f"the hot loop's switching current not modelled: {e}"
                notes.append(more)
            if hot is not None:
                inj, name = hot
                i_l = res.pulse_a if spec.topology == "boost" else i_out
                setup.dc_hot = (name, i_l * math.sqrt(duty * (1.0 - duty)),
                                ModelRun(st, RouteExcitation(st.plane, [], injections=inj), planned=[DC_F]))
    if (dst is not None or setup.standard != "none") and spec.input_connector:
        plus = board.find_pin(spec.input_connector[0])
        ist = dst or setup.cond.build                   # the input path's model (the hot loop's when not split)
        try:
            inj = _area_shares(ist, [plus], _side(board.footprint(plus.footprint)), 1.0)
            if spec.topology == "boost" and res.ripple and inductor is not None:
                vin_pads = [p for p in inductor.pads if p.net == res.ripple["input_net"]]
                inj += _area_shares(ist, vin_pads, _side(inductor), -1.0)
                i_in, end = res.pulse_a, inductor.reference
            else:
                for el in setup.elements:
                    if el.ref in spec.high_side or el.ref in spec.switch:
                        inj += _area_shares(ist, el.a, _side(el.fp), -el.weight)
                i_in, end = i_out * duty, "+".join(spec.high_side or spec.switch)
            if res.input is not None:
                i_in = res.input["i_in_a"]
            run = ModelRun(ist, RouteExcitation(ist.plane, [], injections=inj), planned=[DC_F])
            out.append((f"input {plus.pin} → {end}", i_in, run))
        except SolverError as e:
            notes.append(f"DC input path not modelled: {e}")
    if spec.output_connector:
        runs, more = _output_runs(model, setup, sw_net, inductor, res.input, dst)
        out += runs
        notes += more
    return out, notes


def _hot_loop_injections(board: Board, config: Config, spec: ConverterSpec, setup: "ConverterSetup",
                         st: StackBuild) -> Optional[tuple[list, str]]:
    """The hot loop's switching current as one unit DC solve: (injections, path name), None when the stage has no
    capacitors on its rail near the switches or no pads to feed.

    The high side carries the inductor current I_L for D and nothing for 1 − D; its mean is the rail's DC current
    (a path of its own, from the connector), and the rest, of zero mean and variance I_L²·D(1−D), is the rail
    capacitors' (a buck's input capacitors, a boost's output ones): it flows from them through the rail's copper into
    the high side, and back from the low side through the ground's copper into them [Erickson 2001]. So: 1 A out of
    the capacitors' rail pads into the high side's rail pads (an integrated stage's rail pins), and 1 A out of the
    low side's ground pads (an integrated stage's ground pins when it has no low side of its own) into the
    capacitors' ground pads; the capacitors share it by their admittance at f_sw (their own models, the copper
    between them left out). Scaled by I_L·√(D(1−D)) it is the current's RMS everywhere (its pattern is fixed), so its
    densities, via currents and loss are RMS, and its heat adds to the means' [A43]. The switch node's own variance
    is dc_ripple's run."""
    els = setup.elements
    hs = [el for el in els if el.ref in spec.high_side]
    ls = [el for el in els if el.ref in spec.low_side]
    if spec.switch:
        ic = els[0]
        rail = [([p for p in ic.fp.pads if p.net == spec.rail], _side(ic.fp), 1.0)]
        ground = ([(el.b, _side(el.fp), el.weight) for el in ls] if ls else
                  [([p for p in ic.fp.pads if p.net == spec.ground], _side(ic.fp), 1.0)])
    else:
        rail = [(el.a, _side(el.fp), el.weight) for el in hs]
        ground = [(el.b, _side(el.fp), el.weight) for el in ls]
    rail = [(pads, side, w) for pads, side, w in rail if pads]
    ground = [(pads, side, w) for pads, side, w in ground if pads]
    if not rail or not ground or not spec.f_sw:
        return None
    caps = rail_caps(board, config, spec, _centroid([p for pads, _, _ in rail for p in pads]))
    usable = []                             # (footprint, admittance at f_sw, its rail pads, its ground pads)
    for fp in caps:
        z = _cap_model(config, fp.reference).impedance(float(spec.f_sw))
        plus = [p for p in fp.pads if p.net == spec.rail]
        minus = [p for p in fp.pads if p.net == spec.ground]
        if z and plus and minus:
            usable.append((fp, 1.0 / abs(z), plus, minus))
    total = sum(y for _, y, _, _ in usable)
    usable = [u for u in usable if total > 0 and u[1] / total >= 1e-3]     # parts carrying almost none left out
    total = sum(y for _, y, _, _ in usable)
    if total <= 0:
        return None
    inj, used = [], []
    for fp, y, plus, minus in usable:
        inj += _area_shares(st, plus, _side(fp), y / total)
        inj += _area_shares(st, minus, _side(fp), -y / total)
        used.append(fp.reference)
    for pads, side, w in rail:
        inj += _area_shares(st, pads, side, -w)
    for pads, side, w in ground:
        inj += _area_shares(st, pads, side, w)
    switches = "/".join(dict.fromkeys(list(spec.switch or []) + list(spec.high_side) + list(spec.low_side)))
    shown = "+".join(used[:3]) + ("…" if len(used) > 3 else "")
    return inj, f"hot loop {shown} ↔ {switches} (switching current, RMS)"


def _output_runs(model: BoardModel, setup: "ConverterSetup", sw_net: Optional[str], inductor: Optional[Footprint],
                 est: Optional[dict] = None, dst: Optional[StackBuild] = None) -> tuple[list, list[str]]:
    """The load current from the stage to the output connector's + pin, and the ground return from its - pin, on a
    model of their own around the stage, the connectors and the series parts on the way (the hot loop's region
    rarely holds the load). The region widens like the hot loop's while a path doesn't close.

    DC currents in ground, by Kirchhoff at the ground node: whatever the input connector's - pin takes back is the
    input current I_in, and the low side carries the difference. A buck's load current I_out returns from the load
    and leaves as I_in through the input connector (D·I_out without losses) and I_out - I_in through the low side.
    A boost draws its inductor current I_in from the input connector and gets back I_out from the load ((1-D)·I_in
    without losses) and the rest from the low side, which conducts during the on-time D. I_in is input_estimate's
    (`est`), else the lossless one. Without an input connector, a buck's whole return goes to the low side and a
    boost's is not modelled. `dst`: the whole-board DC model (dc_board_build): solved there, without a region of
    their own."""
    board, config, spec, res = model.board, model.config, setup.spec, setup.res
    plus, minus = (board.find_pin(p) for p in spec.output_connector)
    if plus is None or minus is None or not plus.net or not minus.net:
        return [], [f"output connector {' / '.join(spec.output_connector)}: pins not found or not connected; DC "
                    "output path not modelled"]
    boost, d = spec.topology == "boost", res.duty
    notes: list[str] = []
    high = [el for el in setup.elements if el.ref in spec.high_side or el.ref in spec.switch]
    low = [el for el in setup.elements if el.ref in spec.low_side or el.ref in spec.switch]
    # where the load current leaves the stage: a boost's high side (rail = VOUT), a buck's inductor
    if boost:
        src = [(el.a, _side(el.fp), el.weight) for el in high]
        out_net, start, stop = spec.rail, "+".join(el.ref for el in high), {spec.ground, sw_net}
        if inductor is not None:
            stop |= inductor.nets()
    elif inductor is not None and any(p.net and p.net != sw_net for p in inductor.pads):
        pads = [p for p in inductor.pads if p.net and p.net != sw_net]
        src = [(pads, _side(inductor), 1.0)]
        out_net, start, stop = pads[0].net, inductor.reference, {spec.ground, sw_net, spec.rail}
    else:
        src, out_net, start, stop = [], None, "", set()
        notes.append("DC output path not modelled: no inductor on the switch node")
    path = input_path(board, config, out_net, plus.net, stop - {None}) if out_net else None
    if out_net and path is None:
        notes.append(f"output connector {plus.pin} ({plus.net}) does not reach {out_net} through parts with a "
                     "series model; DC output path not modelled")
    inp = board.find_pin(spec.input_connector[1]) if len(spec.input_connector) == 2 else None
    if inp is not None and not inp.net:
        inp = None
    if boost and inp is None:
        notes.append("DC ground return of a boost needs the input connector (its current returns there)")

    def output_injections(st):
        """The output path's DC injections: the load current enters at the inductor's (or the rectifier's) far
        side, leaves at the output connector's + pin (1 A in all)."""
        inj = [x for pads, side, w in src for x in _area_shares(st, pads, side, w)]
        return inj + _area_shares(st, [plus], _side(board.footprint(plus.footprint)), -1.0)

    # the input current per ampere of output current (a buck: d = D without losses), or the output current per
    # ampere of input current (a boost: its rectifier's share d = 1 - D = V_in/V_out without losses; d is the high
    # side's conduction time, ConverterSpec.pulse, in both topologies); see the docstring
    ratio = min(est["i_in_a"] / spec.i_out, 1.0) if est and not boost else d
    out_per_in = min(spec.i_out / est["i_in_a"], 1.0) if est and boost else d

    def ground_injections(st):
        """The ground return's DC injections: in at the output connector's - pin, out through the input connector's
        - pin (the input current) and through the low side (the rest), per ampere of output current (a boost: the
        load's and the low side's in, the input connector's out, per ampere of input current)."""
        side_of = _side(board.footprint(minus.footprint))
        if boost:          # normalised to the input current
            inj = _area_shares(st, [minus], side_of, out_per_in)
            inj += [x for el in low for x in _area_shares(st, el.b, _side(el.fp), (1 - out_per_in) * el.weight)]
            return inj + _area_shares(st, [inp], _side(board.footprint(inp.footprint)), -1.0)
        share = 1 - ratio if inp is not None else 1.0
        inj = _area_shares(st, [minus], side_of, 1.0)
        inj += [x for el in low for x in _area_shares(st, el.b, _side(el.fp), -share * el.weight)]
        if inp is not None:
            inj += _area_shares(st, [inp], _side(board.footprint(inp.footprint)), -ratio)
        return inj

    wanted = []
    low_refs = "+".join(el.ref for el in low)
    if path is not None and src:
        wanted.append((f"output {start} → {plus.pin}", spec.i_out, output_injections))
    if not boost:
        wanted.append((f"ground {minus.pin} → {low_refs}" + (f" + {inp.pin}" if inp is not None else ""),
                       spec.i_out, ground_injections))
    elif inp is not None:
        wanted.append((f"ground {minus.pin} + {low_refs} → {inp.pin}", est["i_in_a"] if est else res.pulse_a,
                       ground_injections))
    if not wanted:
        return [], notes
    parts = path_parts(board, config, path) if path else []
    pads = [p for pads_, _, _ in src for p in pads_] + [plus, minus] + [p for el in low for p in el.b] + \
        ([inp] if inp is not None else []) + [p for fp in parts for p in fp.pads]
    nets = {spec.ground, minus.net} | set(path or []) | ({inp.net} if inp is not None else set())
    layers = {_side(fp) for fp in [el.fp for el in high + low] + parts + ([inductor] if inductor else [])} | \
        {p.layers[0] for p in (plus, minus, inp) if p is not None and p.layers}
    side = _side(setup.elements[0].fp)
    runs, failed = [], {}
    for _attempt, m in enumerate((None,) if dst is not None else POWER_MARGINS_MM):
        st = dst if dst is not None else _region_build(model, [p.position for p in pads], layers, nets, side, m)[0]
        runs, failed = [], {}
        for name, current, injections in wanted:
            try:
                exc = RouteExcitation(st.plane, [], injections=injections(st))
                runs.append((name, current, ModelRun(st, exc, planned=[DC_F])))
            except SolverError as e:
                failed[name] = str(e)
        if not any("balance" in msg for msg in failed.values()):
            break
    if runs:
        notes.append(f"DC paths modelled on the whole board: {st.nodes} nodes at {st.d:.2f} mm" if dst is not None
                     else f"DC output and ground return modelled on {st.nodes} nodes at {st.d:.2f} mm "
                     f"(region margin {m:g} mm)")
    for name, msg in failed.items():
        notes.append(f"DC {name} not modelled: " + ("its copper is not connected within the model region"
                                                    if "balance" in msg else msg))
    return runs, notes


def _density_peaks(st: StackBuild, maps: dict) -> tuple[tuple, tuple]:
    """(peak, sustained) of current-density maps {layer: A/mm²} on a build: each (A/mm², layer, (x, y)); the
    sustained one averaged over SUSTAINED_MM (what heats the copper, see sustained), the peak over PEAK_MM
    (peak_density: a figure that converges with the grid)."""
    best, best_s = (0.0, "", (0.0, 0.0)), (0.0, "", (0.0, 0.0))
    for k, layer in enumerate(st.names):
        dens, g = maps.get(layer), st.grids[k]
        if dens is None or not dens.size or dens.max() <= 0:
            continue
        pk = peak_density(dens, g.node, st.d, g)
        i, j = np.unravel_index(int(np.argmax(pk)), pk.shape)
        if pk[i, j] > best[0]:
            best = (float(pk[i, j]), layer, tuple(float(v) for v in g.xy(i, j)))
        sus = sustained(dens, g.node, st.d, g)
        i, j = np.unravel_index(int(np.argmax(sus)), sus.shape)
        if sus[i, j] > best_s[0]:
            best_s = (float(sus[i, j]), layer, tuple(float(v) for v in g.xy(i, j)))
    return best, best_s


def combine_dc(results, rules, plating_um: float = 25.0):
    """All power stages' DC paths together: (a result to show as a view, issues, notes), or (None, [], notes).

    Every stage's paths are solved on the one whole-board model (dc_board_build), each at its own current, so on
    that grid their current densities add as vectors and their via currents as signed branch currents: the
    current the copper really carries with every load on at once (the user: "the currents are not added together.
    So one plot shows 5.15 A the other says 1 A"; refboard's two bucks share J101, Q601, the +VDC pours
    and the ground). The current-density and via-load checks run on this total, as issues of COMBINED_NET (the
    stages' own DC-path issues are then dropped by the caller). Only with two or more stages solved on one model."""
    import copy
    from .ipc import via_capacity
    stages = [r for r in results if not getattr(r, "bridge", None) and getattr(r, "dc_branch", None)]
    if len(stages) < 2:
        return None, [], []
    # each path's model as a DcBoard and its currents at the model's vias: a whole-board model (one Simulate) or
    # the DcBoard a split Simulate's part sent (compact_dc_branch); the same model = the same fingerprint
    paths = []
    for r in stages:
        for name, (st, i_b) in r.dc_branch.items():
            if isinstance(st, DcBoard):
                paths.append((r, name, st, np.asarray(i_b)))
            else:
                db = dc_board_of(st)
                paths.append((r, name, db, db.via_currents(i_b)))
    if len({db.fingerprint for _, _, db, _ in paths}) != 1:
        return None, [], ["DC paths of the stages not combined: they were solved on different models"]
    st = paths[0][2]
    # vectors and via currents add up (the paths share the grid and the branches)
    vec = {layer: (np.zeros(st.grids[k].node.shape), np.zeros(st.grids[k].node.shape))
           for k, layer in enumerate(st.names)}
    cur = np.zeros(len(st.vias))
    for r, name, _, i_v in paths:
        cur += i_v
        for layer, (jx, jy) in r.dc_vectors.get(name, {}).items():
            vec[layer] = (vec[layer][0] + jx, vec[layer][1] + jy)
    # each stage's switch-node variance (dc_ripple_sq / dc_ripple_branch, see dc_summary) adds in quadrature: the
    # mean currents superpose, the stages' switching is taken as independent (their phases not locked), so the
    # densities and via currents are RMS and the loss below the time-averaged one
    rip_sq = {layer: np.zeros(st.grids[k].node.shape) for k, layer in enumerate(st.names)}
    rip_v = np.zeros(len(st.vias))
    for r in stages:
        for name, layers in (getattr(r, "dc_ripple_sq", None) or {}).items():
            for layer, m in layers.items():
                if layer in rip_sq and m.shape == rip_sq[layer].shape:
                    rip_sq[layer] += m
        for name, (rst, i_b) in (getattr(r, "dc_ripple_branch", None) or {}).items():
            i_v = np.asarray(i_b) if isinstance(rst, DcBoard) else dc_board_of(rst).via_currents(i_b)
            if i_v.shape == rip_v.shape:
                rip_v += i_v ** 2
    maps = {layer: np.sqrt(jx * jx + jy * jy + rip_sq[layer]) for layer, (jx, jy) in vec.items()}
    best, best_s = _density_peaks(st, maps)
    vias: dict[str, dict] = {}
    for (_, vname, radius, x, y), a_ in zip(st.vias, np.sqrt(cur ** 2 + rip_v)):
        if a_ < 0.01:
            continue
        old = vias.get(vname)
        if old is None or a_ > old["current_a"]:
            cap = via_capacity(2 * radius * 1e3, plating_um)
            vias[vname] = {"name": vname, "current_a": float(a_),
                           "a_mm2": float(a_) / (math.pi * 2 * radius * plating_um * 1e-6) * 1e-6, "x": x, "y": y,
                           "capacity_a": cap, "load_pct": 100.0 * a_ / cap if cap > 0 else None}
    from ..solver.peec import SIGMA_CU
    # the copper's Joule loss of the total current: rho |J|^2 over each node's copper (Grid.copper_area: the area its
    # density is defined over; the whole cell counted a narrow track's node several times over until 2026-10-19),
    # |J| in A/mm² -> A/m², and the via barrels' R·I² (the signed sums and the variances above) [A43]
    loss = sum(float((maps[layer] ** 2 * st.grids[k].copper_area()).sum()) * 1e12 / SIGMA_CU * st.t[k]
               * (st.d * 1e-3) ** 2 for k, layer in enumerate(st.names))
    ends = getattr(st, "via_ends", None) or []
    if len(ends) == len(st.vias):
        loss += float(sum(r * (c * c + v) for (_, _, r), c, v in zip(ends, cur, rip_v)))
    loads = ", ".join(f"{r.net} {r.dc[0]['current_a']:.3g} A" if r.dc else r.net for r in stages)
    i_in = sum(p["current_a"] for r in stages for p in r.dc if p["name"].startswith("input "))
    path = {"name": COMBINED_VIEW, "combined": True, "stages": [r.net for r in stages], "current_a": i_in,
            "loads": loads, "r_mohm": 0.0, "r_copper_mohm": 0.0, "drop_mv": 0.0, "loss_w": loss, "parts": [],
            "peak_a_mm2": best[0], "peak_layer": best[1], "peak_at": best[2],
            "sustained_a_mm2": best_s[0], "sustained_layer": best_s[1], "sustained_at": best_s[2],
            "vias": sorted(vias.values(), key=lambda v: -v["current_a"])}
    # a copy of a stage's result (a power stage's when there is one): always a plain DC view, never a DC load's
    # result (thermal would take that as a heat source of its own: the copper heated twice)
    view = copy.copy(next((r for r in stages if r.kind != "dcload"), stages[0]))
    view.kind = "converter"
    view.net = COMBINED_NET
    view.notes = [f"every power stage's DC paths at once ({loads}; {i_in:.3g} A in at the input connectors): "
                  f"their currents added on one whole-board model; copper loss {loss:.2f} W"]
    view.dc = [path]
    view.dc_maps = {COMBINED_VIEW: {l: m.astype(np.float32) for l, m in maps.items()}}
    view.dc_vectors = {COMBINED_VIEW: {l: (jx.astype(np.float32), jy.astype(np.float32)) for l, (jx, jy) in vec.items()}}
    view.dc_grids = {COMBINED_VIEW: dict(zip(st.names, st.grids))}
    view.dc_branch = {}
    view.dc_ripple_sq, view.dc_ripple_branch = {}, {}     # the view is never a heat source (thermal._stages)
    view.switch_pins = [p for r in stages for p in (r.switch_pins or [])]
    view.fixes, view.input = [], None
    # the checks on the total current (as _converter_issues does per path)
    issues = []
    key = f"power:{COMBINED_NET}"
    worst_via = max(path["vias"], key=lambda v: v["a_mm2"], default=None)
    level = max(path["sustained_a_mm2"], worst_via["a_mm2"] if worst_via else 0.0)
    if level >= rules.current_density_warning:
        sev = resolve_severity("current_density", rules.severity,
                               "error" if level >= rules.current_density_error else "warning")
        if sev != "off":
            at = path["sustained_at"] if path["sustained_a_mm2"] >= (worst_via or {}).get("a_mm2", 0.0) or \
                not worst_via else (worst_via["x"], worst_via["y"])
            via_txt = (f"; {worst_via['name']} carries {worst_via['current_a']:.2f} A ({worst_via['a_mm2']:.0f} A/mm² "
                       f"in its plating)" if worst_via else "")
            issues.append(Issue("current_density", sev, COMBINED_NET,
                                f"all power stages together ({loads}): {path['sustained_a_mm2']:.0f} A/mm² sustained "
                                f"over {SUSTAINED_MM:g} mm on {path['sustained_layer']} at "
                                f"({path['sustained_at'][0]:.2f}, {path['sustained_at'][1]:.2f}){via_txt}",
                                (round(at[0], 4), round(at[1], 4)), layer=path["sustained_layer"],
                                key=f"{key}:dc:{COMBINED_VIEW}",
                                details={"sustained_a_mm2": path["sustained_a_mm2"], "peak_a_mm2": path["peak_a_mm2"]}))
    over = [v for v in path["vias"] if (v.get("load_pct") or 0.0) >= rules.via_load_warning]
    if over:
        v = max(over, key=lambda x: x["load_pct"])
        sev = resolve_severity("via_overload", rules.severity,
                               "error" if v["load_pct"] >= rules.via_load_error else "warning")
        if sev != "off":
            issues.append(Issue("via_overload", sev, COMBINED_NET,
                                f"all power stages together ({loads}): {v['name']} carries {v['current_a']:.2f} A, "
                                f"{v['load_pct']:.0f} % of its IPC-2221 capacity ({v['capacity_a']:.2f} A for a 10 K "
                                f"rise)" + (f"; {len(over) - 1} more via(s) over {rules.via_load_warning:g} %"
                                            if len(over) > 1 else ""),
                                (round(v["x"], 4), round(v["y"], 4)), key=f"{key}:via:{COMBINED_VIEW}",
                                details={"current_a": v["current_a"], "capacity_a": v["capacity_a"],
                                         "load_pct": v["load_pct"]}))
    return view, issues, []


def dc_vector_maps(st: StackBuild, sol: Solution, current: float) -> dict:
    """{layer: (Jx, Jy)} current density vectors of a DC solution in A/mm² (as the density maps of dc_summary)."""
    out = {}
    for k, layer in enumerate(st.names):
        kx, ky = sol.sheet_vectors(k, st.d, True)
        scale = current / st.t[k] * 1e-6
        out[layer] = ((kx * scale).astype(np.float32), (ky * scale).astype(np.float32))
    return out


def dc_analysis(model: BoardModel, setup: "ConverterSetup") -> tuple[list[dict], dict, dict, list[str]]:
    """Summaries of the solved DC power paths. Returns (paths, maps, grids, notes); the vector maps go to
    setup.res.dc_vectors. A path whose solve failed (GMRES did not converge, a factorisation too large) is not in
    `paths`: it goes to setup.res.dc_unsolved, so the input estimate, Thermal and the issues say what is missing
    instead of presenting the stage's figures as complete (VALIDATION_AUDIT.md round 6, ISSUE-25)."""
    paths, maps, grids, notes = [], {}, {}, []
    unsolved = setup.res.dc_unsolved = []
    for name, current, run in setup.dc_runs:
        sol = run.ok().get(DC_F)
        if sol is None:
            notes.append(f"DC {name} not solved")
            unsolved.append(name)
            continue
        ripple = None
        if name in setup.dc_ripple:
            k_r, rrun = setup.dc_ripple[name]
            sol_d = rrun.ok().get(DC_F)
            if sol_d is None:
                notes.append(f"DC {name}: its switching share not solved (the mean current only)")
                unsolved.append(f"{name} (its switching share)")
            else:
                ripple = (k_r, sol_d)
        extra = setup.dc_extra.get(name, 0.0)          # the inductor's ripple on the switch node path
        summary, m = dc_summary(run.build, sol, current, name, model.config.analysis.via_plating_um, ripple, extra)
        paths.append(summary)
        maps[name] = m
        grids[name] = dict(zip(run.build.names, run.build.grids))
        setup.res.dc_vectors[name] = dc_vector_maps(run.build, sol, current)          # the mean flow (arrows)
        setup.res.dc_branch[name] = (run.build, np.asarray(sol.I_branch).real * current)
        db = dc_board_of(run.build) if isinstance(run.build, StackBuild) else None
        if db is not None:              # the vias' share, kept for Thermal after the branches go [A43]
            setup.res.dc_via[name] = (db, db.via_currents(np.asarray(sol.I_branch).real * current))
        e = math.sqrt(current * current + extra)     # the RMS current the switching share scales with
        if ripple is not None:
            k_r, sol_d = ripple
            st = run.build
            setup.res.dc_ripple_sq[name] = {
                layer: ((sol_d.sheet_current(k, st.d, True) * (k_r * e) / st.t[k] * 1e-6) ** 2
                        ).astype(np.float32)
                for k, layer in enumerate(st.names)}
            setup.res.dc_ripple_branch[name] = (st, np.asarray(sol_d.I_branch).real * (k_r * e))
            if db is not None:
                setup.res.dc_ripple_via[name] = (db, db.via_currents(np.asarray(sol_d.I_branch).real * (k_r * e)))
            notes.append(f"DC {name}: the high side carries it for D = {setup.res.duty:.2f}, the low side "
                         f"for the rest: densities, via currents and the {summary['ripple_loss_w'] * 1e3:.1f} mW of "
                         "switching share in its loss are RMS")
        if extra > 0:
            # the ripple's variance along the mean's own pattern: into the heat and the vias' quadrature sums
            st = run.build
            add = {layer: ((sol.sheet_current(k, st.d, True) / st.t[k] * 1e-6) ** 2 * extra).astype(np.float32)
                   for k, layer in enumerate(st.names)}
            old = setup.res.dc_ripple_sq.get(name)
            setup.res.dc_ripple_sq[name] = {l: (old[l] + v if old else v) for l, v in add.items()}
            unit = np.asarray(sol.I_branch).real
            prev = setup.res.dc_ripple_branch.get(name)
            amp = np.sqrt((prev[1] ** 2 if prev else 0.0) + unit ** 2 * extra)
            setup.res.dc_ripple_branch[name] = (st, amp)
            if db is not None:
                setup.res.dc_ripple_via[name] = (db, db.via_currents(amp))
            notes.append(f"DC {name}: the inductor's ripple ({math.sqrt(12 * extra):.2g} A p-p) adds "
                         f"{summary['loss_w'] * extra / (e * e) * 1e3:.2f} mW to its loss")
    if setup.dc_hot is not None:
        # the hot loop's switching current (_hot_loop_injections): no mean, so it adds heat and via current as a
        # variance (its RMS everywhere is the unit solve's pattern times I_L·√(D(1−D))); listed as a path of its own
        name, i_rms, run = setup.dc_hot
        sol = run.ok().get(DC_F)
        if sol is None:
            notes.append(f"{name}: not solved")
            unsolved.append(name)
        else:
            st = run.build
            summary, m = dc_summary(st, sol, i_rms, name, model.config.analysis.via_plating_um)
            summary["rms"] = summary["ac"] = True
            paths.append(summary)
            maps[name] = m
            grids[name] = dict(zip(st.names, st.grids))
            setup.res.dc_ripple_sq[name] = {layer: (mm.astype(np.float64) ** 2).astype(np.float32)
                                            for layer, mm in m.items()}
            setup.res.dc_ripple_branch[name] = (st, np.asarray(sol.I_branch).real * i_rms)
            db = dc_board_of(st) if isinstance(st, StackBuild) else None
            if db is not None:
                setup.res.dc_ripple_via[name] = (db, db.via_currents(np.asarray(sol.I_branch).real * i_rms))
            notes.append(f"{name}: {i_rms:.3g} A RMS from the rail capacitors through the switches ({setup.res.duty:.2f}"
                         f" duty), {summary['loss_w'] * 1e3:.1f} mW in the copper: heats it with the DC paths")
    return paths, maps, grids, notes


def input_filter_advice(rows: list[dict], margin_db: float) -> Optional[dict]:
    """First-order input-filter guidance from the conducted estimate. A series L and a capacitor C at the
    connector form, with the stage's input capacitors, a π-filter whose attenuation grows 40 dB/decade above
    f_c = 1/(2π·sqrt(L·C)). f_c is chosen so every harmonic gains what it lacks to `margin_db`: f_c ≤ f·10^(-A/40).
    C is the smallest standard value that keeps L ≤ FILTER_L_MAX. The damping network is the usual R = sqrt(L/C)
    in series with 4·C across C (the filter's output impedance must stay below the converter's input impedance,
    [Middlebrook 1976] [Erickson 2001]). None when the estimate already meets the margin."""
    need = [(r["f"], margin_db - r["margin_db"]) for r in rows
            if r.get("margin_db") is not None and r["margin_db"] < margin_db]
    if not need:
        return None
    f_c = min(f * 10 ** (-a / 40) for f, a in need)
    w2 = (2 * math.pi * f_c) ** 2
    C = next((c for c in FILTER_CAPS if 1 / (w2 * c) <= FILTER_L_MAX), FILTER_CAPS[-1])
    L = 1 / (w2 * C)
    f_worst, a_worst = max(need, key=lambda t: t[1])
    return {"f_c": f_c, "L": L, "C": C, "R_damp": math.sqrt(L / C), "C_damp": 4 * C, "need_db": a_worst,
            "f_worst": f_worst, "margin_db": margin_db}


def cm_estimate(spec: ConverterSpec, res: "ConverterResult", standard: str, cls: str) -> Optional[dict]:
    """Common-mode conducted emission from the switch node's dV/dt. The switch node swings the rail voltage with the
    switching edges; through its stray capacitance C to the chassis or heatsink that drives a current back through
    both lines' LISNs in parallel (each carries half and reads it). The emission is proportional to C, so the
    result is a budget: the largest C that keeps the reading under the limit (binding at one frequency). With
    `c_stray` the estimate itself. The capacitance depends on the mechanics (heatsink, housing), not the board."""
    from ..emi.conducted import conducted_arrays
    v = res.v_in
    if not v or standard == "none":
        return None
    rng = frequency_range(standard, cls)
    if not rng:
        return None
    freqs, v_sw = trapezoid_harmonics(spec.f_sw, spec.rise_time, v, res.duty, rng[1] * 1.01)
    sel = (freqs >= rng[0]) & (freqs <= rng[1])
    f, vs = freqs[sel], v_sw[sel]
    if not len(f):
        return None
    i_1pf = 2 * np.pi * f * 1e-12 * vs                  # CM current through 1 pF (A peak)
    arr = conducted_arrays(f, i_1pf / 2, standard, cls, spread_of(spec))
    margins = arr["margin_db"]
    if not np.isfinite(margins).any():
        return None
    k = int(np.nanargmin(margins))
    out = {"budget_pf": float(10 ** (margins[k] / 20)), "f": float(f[k]), "standard": describe(standard, cls),
           "v_sw": float(v), "c_stray_pf": None, "worst": None}
    if spec.c_stray:
        c_pf = spec.c_stray * 1e12
        rows = conducted_table(f, i_1pf / 2 * c_pf, standard, cls, per_decade=REPORT_PER_DECADE,
                               spread=spread_of(spec))
        compared = [r for r in rows if r["margin_db"] is not None]
        out["c_stray_pf"] = c_pf
        out["worst"] = min(compared, key=lambda r: r["margin_db"]) if compared else None
    return out


def _r_text(path: dict) -> str:
    """'R mΩ', and the copper's share when series parts (a shunt, a fuse) are on the path."""
    return f"{path['r_mohm']:.2f} mΩ" + (f" ({path['r_copper_mohm']:.2f} in copper)" if path.get("parts") else "")


def dc_view(c: "ConverterResult", name: str) -> "ConverterResult":
    """A copy of the result whose heatmap shows a DC path's current density (for the plugin and the HTML)."""
    import copy
    v = copy.copy(c)
    maps = c.dc_maps[name]
    path = next(p for p in c.dc if p["name"] == name)
    v.sheet_current = maps
    v.sheet_vectors = c.dc_vectors.get(name, {})
    v.layer_grids = c.dc_grids.get(name, c.layer_grids)
    v.grid = next(iter(v.layer_grids.values()), c.grid)
    tot = sum(float(m.sum()) for m in maps.values()) or 1.0
    v.layer_share = {l: float(m.sum()) / tot for l, m in maps.items()}
    v.map_frequency = 0.0
    v.transfers = [{"name": x["name"], "kind": "via", "layers": list(maps), "x": x["x"], "y": x["y"],
                    "fraction": x["current_a"] / max(path["current_a"], 1e-12), "distance_mm": 0.0}
                   for x in path["vias"]]
    v.heat_label = f"current density (A/mm²) at {path['current_a']:.3g} A DC"
    # the current beside the resistance and the drop; on the input path it is the expected input current (output
    # power plus losses, input_estimate), with the efficiency it comes from
    amps = f"{path['current_a']:.3g} A"
    if path.get("combined"):
        v.heat_label = "current density (A/mm²), all power stages at their loads"
        v.heat_title = (f"All power stages: DC currents together ({path['loads']}), {path['current_a']:.3g} A in, "
                        f"copper loss {path['loss_w']:.2f} W")
        v.view_of = name
        return v
    if name.startswith("input ") and getattr(c, "input", None):
        amps = f"expected input current {amps} (efficiency {c.input['efficiency'] * 100:.1f} %)"
    if path.get("ac"):              # the hot loop's switching current (_hot_loop_injections): an RMS, no mean
        v.heat_label = f"current density (A/mm², RMS) of {path['current_a']:.3g} A switching current"
        v.heat_title = f"{c.net}: {name}, {amps} RMS, {_r_text(path)}, {path['loss_w'] * 1e3:.1f} mW"
        v.view_of = name
        return v
    v.heat_title = f"{c.net}: DC {name}, {amps}, {_r_text(path)}, {path['drop_mv']:.1f} mV"
    v.view_of = name
    return v


def _cap_shares(config: Config, spec: ConverterSpec, plane, s_hf: Solution, s_sw: Optional[Solution]) -> list[dict]:
    """Rail capacitors (and the LISN) with their share of the loop current at LOOP_F and at f_sw."""
    sums: dict[str, list] = {}              # a capacitor's parallel branches (simulate.part_pairs): currents add
    for k, b in enumerate(plane.branches):
        if b.kind != "part":
            continue
        is_cap = _cap_model(config, b.name) is not None
        if not is_cap and b.name != "LISN":
            continue
        acc = sums.setdefault(b.name, [0j, 0j])
        acc[0] += complex(s_hf.I_branch[k])
        if s_sw is not None:
            acc[1] += complex(s_sw.I_branch[k])
    out = []
    for name, (i_hf, i_sw) in sums.items():
        hf = float(abs(i_hf))
        sw = float(abs(i_sw)) if s_sw is not None else None
        if max(hf, sw or 0.0) < 0.01:
            continue
        out.append({"name": name, "share_hf": hf, "share_sw": sw, "lisn": name == "LISN"})
    out.sort(key=lambda c: -c["share_hf"])
    return out


def _converter_issues(setup: ConverterSetup, rules, a):
    """A power stage's issues: the overshoot (the larger of L di/dt and the ringing's) against the rules' share of
    V_in, the radiated and conducted margins, high current density and overloaded vias on the DC paths, a plane
    resonance, and an unsolved part of the radiated band."""
    res, spec = setup.res, setup.spec
    loc = res.location
    key = f"power:{spec.name}"
    if res.loop_nh is not None and res.v_in:
        v_l = res.overshoot_v or 0.0
        v_r = (res.ringing or {}).get("overshoot_v") or 0.0
        worst = max(v_l, v_r)
        frac = worst / res.v_in
        if frac >= rules.overshoot_warning:
            sev = "error" if frac >= rules.overshoot_error else "warning"
            ring = res.ringing
            ring_txt = (f"; with C_oss {spec.c_oss * 1e12:.0f} pF it rings at {ring['f'] / 1e6:.0f} MHz (Q {ring['q']:.1f})"
                        f", ≈{v_r:.1f} V" if ring else "")
            top = ", ".join(c["name"] for c in res.caps[:2] if not c["lisn"])
            setup.issue(rules, "hot_loop",
                        f"{spec.name}: hot-loop inductance {res.loop_nh:.2f} nH ({top} closest); switching "
                        f"{res.pulse_a:g} A in {spec.rise_time * 1e9:g} ns gives ≈{v_l:.1f} V of overshoot{ring_txt}: "
                        f"{frac * 100:.0f} % of {res.v_in:g} V", loc, severity=sev, layer=res.layer, key=f"{key}:loop",
                        details={"loop_nh": res.loop_nh, "overshoot_v": worst, "fraction": frac})
    for path in res.dc:
        worst_via = max(path["vias"], key=lambda v: v["a_mm2"], default=None)
        level = max(path["sustained_a_mm2"], worst_via["a_mm2"] if worst_via else 0.0)
        if level >= rules.current_density_warning:
            sev = "error" if level >= rules.current_density_error else "warning"
            where = (f"{path['sustained_a_mm2']:.0f} A/mm² sustained over {SUSTAINED_MM:g} mm on "
                     f"{path['sustained_layer']} at ({path['sustained_at'][0]:.2f}, {path['sustained_at'][1]:.2f})")
            via_txt = (f"; {worst_via['name']} carries {worst_via['current_a']:.2f} A ({worst_via['a_mm2']:.0f} A/mm² "
                       f"in its plating)" if worst_via else "")
            at = path["sustained_at"] if path["sustained_a_mm2"] >= (worst_via or {}).get("a_mm2", 0.0) or not worst_via \
                else (worst_via["x"], worst_via["y"])
            setup.issue(rules, "current_density",
                        f"{spec.name}: DC {path['name']} at {path['current_a']:.3g} A: {where}{via_txt}; "
                        f"{_r_text(path)}, {path['drop_mv']:.0f} mV, {path['loss_w'] * 1e3:.0f} mW",
                        at, severity=sev, layer=path["sustained_layer"], key=f"{key}:dc:{path['name']}",
                        details={k: path[k] for k in ("current_a", "r_mohm", "sustained_a_mm2", "peak_a_mm2")})
        over = [v for v in path["vias"] if (v.get("load_pct") or 0.0) >= rules.via_load_warning]
        if over:
            v = max(over, key=lambda x: x["load_pct"])
            sev = "error" if v["load_pct"] >= rules.via_load_error else "warning"
            setup.issue(rules, "via_overload",
                        f"{spec.name}: DC {path['name']}: {v['name']} carries {v['current_a']:.2f} A, "
                        f"{v['load_pct']:.0f} % of its IPC-2221 capacity ({v['capacity_a']:.2f} A for a 10 K rise)"
                        + (f"; {len(over) - 1} more via(s) over {rules.via_load_warning:g} %" if len(over) > 1 else ""),
                        (v["x"], v["y"]), severity=sev, key=f"{key}:via:{path['name']}",
                        details={"current_a": v["current_a"], "capacity_a": v["capacity_a"],
                                 "load_pct": v["load_pct"]})
    cm = res.cm
    if cm and cm.get("worst") and cm["worst"]["margin_db"] < rules.emi_margin_db:
        w = cm["worst"]
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "conducted_emission",
                    f"{spec.name}: common mode through {cm['c_stray_pf']:g} pF from the switch node: estimated "
                    f"{w['v_dbuv']:.1f} dBµV at {w['f'] / 1e6:.4g} MHz vs {cm['standard']} {w['limit_dbuv']:.1f} dBµV "
                    f"(margin {w['margin_db']:+.1f} dB); the budget is {cm['budget_pf']:.2g} pF", loc, severity=sev,
                    layer=res.layer, key=f"{key}:cm", details={"budget_pf": cm["budget_pf"], **w})
    w = res.conducted_worst
    if w is not None and w["margin_db"] < rules.emi_margin_db:
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "conducted_emission",
                    f"{spec.name}: estimated {w['v_dbuv']:.1f} dBµV at {w['f'] / 1e6:.4g} MHz on the input vs "
                    f"{res.conducted_standard} {w['limit_dbuv']:.1f} dBµV (margin {w['margin_db']:+.1f} dB)", loc,
                    severity=sev, layer=res.layer, key=f"{key}:conducted",
                    details={k: w[k] for k in ("f", "v_dbuv", "limit_dbuv", "margin_db")})
    w = res.ce101_worst
    if w is not None and w["margin_db"] < rules.emi_margin_db:
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "conducted_emission",
                    f"{spec.name}: input lead current {w['i_dbua']:.1f} dBµA at {format_si(w['f'], 'Hz')} vs "
                    f"{res.ce101_limit} {w['limit_dbua']:.1f} dBµA (margin {w['margin_db']:+.1f} dB)", loc,
                    severity=sev, layer=res.layer, key=f"{key}:ce101",
                    details={k: w[k] for k in ("f", "i_dbua", "limit_dbua", "margin_db")})
    w = res.worst
    if w is not None and w["margin_db"] < rules.emi_margin_db:
        sev = "error" if w["margin_db"] < 0 else "warning"
        setup.issue(rules, "emi_margin",
                    f"{spec.name}: hot loop radiates an estimated {w['e_dbuv']:.1f} dBµV/m at {w['f'] / 1e6:.4g} MHz "
                    f"({w['distance_m']:g} m) vs {a.limit_text()} limit {w['limit_dbuv']:.1f} dBµV/m "
                    f"(margin {w['margin_db']:+.1f} dB)", loc, severity=sev, layer=res.layer, key=f"{key}:emi",
                    details={k: w[k] for k in ("f", "e_dbuv", "limit_dbuv", "margin_db")})
    r = res.resonance
    if r and r["rise_db"] >= rules.resonance_db:
        pair = f" between {r['pair'][0]} and {r['pair'][1]}" if r.get("pair") else ""
        setup.issue(rules, "plane_resonance",
                    f"{spec.name}: the hot loop's moment rises {r['rise_db']:+.1f} dB at {r['f'] / 1e6:.4g} MHz over "
                    f"its value at {LOOP_F / 1e6:.0f} MHz: a plane-pair resonance{pair} is excited", loc,
                    layer=r["pair"][0] if r.get("pair") else res.layer, key=f"{key}:resonance", details=dict(r))
    unsolved_issue(setup, rules, spec.name, loc, res.layer, f"{key}:unsolved")
    if res.dc_unsolved:
        setup.issue(rules, "dc_unsolved",
                    f"{spec.name}: DC {', '.join(res.dc_unsolved)} could not be solved: the stage's DC table, its "
                    "input current and efficiency, and Thermal's heat leave it out; simulate again (another resolution "
                    "in Preferences)", loc, layer=res.layer, key=f"{key}:dc_unsolved",
                    details={"paths": list(res.dc_unsolved)})


def prepare_converters(model: BoardModel, specs: list[ConverterSpec]):
    """Every stage's model (prepare_converter): (setups to solve, every stage's result so far, the issues of the
    stages that could not be modelled)."""
    setups, results, issues = [], [], []
    for spec in specs:
        setup, res, iss = prepare_converter(model, spec)
        results.append(res)
        if setup is None:
            issues.extend(iss)
        else:
            setups.append(setup)
            if not spec.bridge and model.config.victims.enabled:
                _stage_victim_run(model, setup)
    return setups, results, issues


def _stage_victim_run(model: BoardModel, setup: ConverterSetup):
    """A power stage's hot loop once more, on a model that also reaches its victims (victims.py: the board's
    microcontrollers and reset supervisors, its reset, crystal, Hall, debug and PWM nets, or the settings' lists), at
    the two hot-loop frequencies; with them the switching's noise at the victims' receivers, their grounds' shift and
    the current the switch node pushes into them (finish_converter). A stage with no victim adds nothing."""
    from .victims import find_victims, region_points, terminations
    spec, res = setup.spec, setup.res
    board, config = model.board, model.config
    vs = find_victims(board, config, [spec], spec.rail, spec.ground, spec.input_connector)
    res.notes += vs.notes
    if not (vs.parts or vs.nets):
        return
    caps = rail_caps(board, config, spec, res.location)
    sw_net, inductor = switch_inductor(board, config, spec)
    pts = ([p.position for el in setup.elements for p in el.a + el.b] + [p.position for fp in caps for p in fp.pads]
           + [p.position for p in (inductor.pads if inductor else []) if p.net == sw_net])
    layers = {_side(el.fp) for el in setup.elements} | {_side(fp) for fp in caps}
    nets = converter_nets(board, config, spec, with_input=False) | ({sw_net} if sw_net else set())
    v_pts, v_layers = region_points(board, vs, spec.ground)
    try:
        st, exc, _, _, _ = _loop_model(model, spec, setup.elements, pts + v_pts, layers | v_layers,
                                       nets | {spec.ground} | set(vs.nets), res.layer, POWER_MARGINS_MM[:1])
        branches, probes, notes = terminations(st, board, config, vs, spec.ground)
        exc = RouteExcitation(exc.stack.with_branches(branches), exc.trace, injections=exc.extra)
    except SolverError as e:
        res.notes.append(f"victims: {e}")
        return
    res.notes += notes
    setup.victim_set, setup.probes = vs, probes
    setup.victim_run = ModelRun(st, exc, planned=[LOOP_F_LO, LOOP_F])


LOOP_PM_WARN, LOOP_PM_ERROR = 45.0, 30.0     # degrees of phase margin
LOOP_GM_WARN = 6.0                           # dB of gain margin
LOOP_FC_MAX = 0.2                            # the crossover above this share of f_sw: the sampling / ripple limit
LOOP_RHP_MAX = 0.2                           # a boost's crossover above this share of its right-half-plane zero


def _stage_loop(model: BoardModel, setup: ConverterSetup):
    """The stage's control loop (analysis/loop.py): its figures on the result, the loop gain among its responses,
    and the issues: loop_phase_margin (under LOOP_PM_WARN / LOOP_PM_ERROR degrees, or a gain margin under
    LOOP_GM_WARN dB), loop_crossover (above LOOP_FC_MAX of f_sw, or a boost's above LOOP_RHP_MAX of its
    right-half-plane zero)."""
    from .loop import analyse_loop
    spec, res = setup.spec, setup.res
    try:
        lr = analyse_loop(model.board, model.config, spec)
    except (ValueError, ZeroDivisionError, np.linalg.LinAlgError) as e:
        res.notes.append(f"control loop: {e}")
        return
    if lr is None:
        return
    res.loop = lr.summary()
    if lr.error:
        # said only where a loop was asked for (the controller has loop fields, or the stage a loop: override): a
        # controller without any stays quiet (its stage's summary still says why there is no loop)
        p = lr.parts
        if (p is not None and (p.params or p.mode)) or getattr(spec, "loop", None):
            res.notes.append(f"control loop: {lr.error}")
        return
    res.notes += [f"control loop: {n}" for n in lr.notes]
    res.responses.append(lr.response)
    rules = model.config.rules
    if lr.pm is not None and (lr.pm < LOOP_PM_WARN or (lr.gm_db is not None and lr.gm_db < LOOP_GM_WARN)):
        sev = "error" if lr.pm < LOOP_PM_ERROR else "warning"
        setup.issue(rules, "loop_phase_margin",
                    f"{spec.name}: the control loop crosses over at {format_si(lr.fc, 'Hz')} with {lr.pm:.0f}° of "
                    "phase margin" + (f" and {lr.gm_db:.1f} dB of gain margin" if lr.gm_db is not None else "")
                    + f" (aim for ≥ {LOOP_PM_WARN:g}° and ≥ {LOOP_GM_WARN:g} dB): change the compensation on "
                    f"{lr.parts.controller}'s COMP pin", res.location, severity=sev, layer=res.layer,
                    key=f"loop:{spec.name}:pm", details=res.loop)
    f_rhp = lr.figures.get("f_rhp")
    if lr.fc is not None and lr.fc > LOOP_FC_MAX * spec.f_sw:
        setup.issue(rules, "loop_crossover",
                    f"{spec.name}: the control loop crosses over at {format_si(lr.fc, 'Hz')}, above "
                    f"{LOOP_FC_MAX * 100:g} % of f_sw ({format_si(spec.f_sw, 'Hz')}): the sampling and the ripple "
                    "limit the loop there", res.location, layer=res.layer, key=f"loop:{spec.name}:fc",
                    details=res.loop)
    elif lr.fc is not None and f_rhp and lr.fc > LOOP_RHP_MAX * f_rhp:
        # a boost's right-half-plane zero adds phase lag like a pole while it raises the gain: keep well below it
        setup.issue(rules, "loop_crossover",
                    f"{spec.name}: the control loop crosses over at {format_si(lr.fc, 'Hz')}, above "
                    f"{LOOP_RHP_MAX * 100:g} % of the boost's right-half-plane zero ({format_si(f_rhp, 'Hz')} at "
                    f"{format_si(spec.i_out, 'A')}): lower the crossover (it moves down as the load rises)",
                    res.location, layer=res.layer, key=f"loop:{spec.name}:fc", details=res.loop)


def _stage_victims(model: BoardModel, setup: ConverterSetup):
    """A power stage's victim rows (victims.stage_rows: its switched current I over its edge, the switch node
    swinging to its rail) and a victim_coupling issue for each over its limit."""
    from .victims import finish_rows, stage_rows, victim_text
    spec, res, vs = setup.spec, setup.res, setup.victim_set
    if vs is None or setup.victim_run is None:
        return
    sw_net, _ = switch_inductor(model.board, model.config, spec)
    rows = stage_rows(model.board, model.copper, vs, setup.victim_run, setup.probes, spec.ground, spec.name,
                      res.pulse_a, spec.rise_time, res.v_in or 0.0, sw_net)
    res.victims = finish_rows(rows, model.config.victims, vs.reference_set)
    for r in res.victims:
        if r["checked"] and r["peak"] > r["limit"]:
            setup.issue(model.config.rules, "victim_coupling", f"{spec.name}: {victim_text(r)}", res.location,
                        layer=res.layer, key=f"victim:{spec.name}:{r['kind']}:{r['name']}", details=dict(r))
