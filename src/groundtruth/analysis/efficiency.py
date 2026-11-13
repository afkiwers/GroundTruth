"""A power stage's efficiency and losses against its load current (the user, 2026-11-02, with a report's efficiency
and power-loss plots per output rail: "can we sweep and create these kind of graphs?").

No new field solve: at each load current the losses Thermal uses (thermal.part_losses: the switches' conduction with
the inductor's ripple, the hard edge's overlap, the output charge, reverse recovery, the dead times, a diode, the
inductor's DCR, series parts and connector pins) are worked out again for that current, the DC paths' copper scaled
as I² (the DC model is linear: an output path carries I_out, an input path I_in), plus the fixed losses the
operating point leaves out [A80]: each switching FET's gate charge driven every cycle, Q_g·V_gate·f_sw, and the
controller's own supply current from the input, I_q·V_in. I_in follows from the energy balance
V_in·I_in = V_out·I_out + P_loss (a few rounds: the input path's copper depends on it).

The model assumes continuous conduction: below I_L = ΔI/2 a real controller may skip pulses, burst or run
discontinuous, and its light-load efficiency differs (`ccm_from` marks that current). The inductor's core loss is in
it when the stage gives a point of the maker's loss data (`core_loss`, thermal.core_loss [A98]). A measured curve (a CSV the settings name, `measured`) can be drawn over
the model's: it is read here on the computer that draws, never on a compute server. No wx here.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

EFF_V_GATE = 5.0             # V, the gate-drive voltage when the stage gives none (a typical driver's) [A80]
EFF_POINTS = 24              # load currents of the default sweep
EFF_RANGE = (0.05, 1.2)      # the default sweep, a share of the stage's i_out
EFF_ROUNDS = 4               # energy-balance rounds for the input current (it changes the input path's copper)
CATEGORIES = ("conduction", "switching", "output charge", "reverse recovery", "dead time", "diode", "inductor",
              "inductor core", "copper", "series parts", "connectors", "gate drive", "controller", "other")


@dataclass
class EfficiencyResult:
    """A power stage's efficiency sweep as a Results view of its own (kind "efficiency"): per load current the
    output power, the loss (by category), the efficiency; the band below `ccm_from` (discontinuous conduction
    possible), and notes on what the model leaves out. `measured`: points read from the stage's CSV when drawn
    (current A, efficiency 0-1, loss W or None)."""
    net: str
    v_in: float
    v_out: float
    i_ref: float
    currents: list = field(default_factory=list)
    p_out: list = field(default_factory=list)
    p_loss: list = field(default_factory=list)
    efficiency: list = field(default_factory=list)
    by: dict = field(default_factory=dict)          # {category: [W per current]}
    ccm_from: Optional[float] = None                # A: below it the inductor current may reach zero
    notes: list = field(default_factory=list)
    measured: list = field(default_factory=list)
    measured_note: str = ""
    error: str = ""
    kind: str = "efficiency"

    def label(self) -> str:
        """The view's name in the Results list."""
        return f"{self.net}  efficiency"

    def at(self, current: float) -> Optional[int]:
        """The sweep point nearest a load current."""
        if not self.currents:
            return None
        return min(range(len(self.currents)), key=lambda k: abs(self.currents[k] - current))

    def summary(self) -> dict:
        """Plain data for the JSON report."""
        return {k: v for k, v in self.__dict__.items() if k != "measured"} | {"kind": "efficiency"}


def default_currents(i_out: float) -> list[float]:
    """The sweep's load currents when the stage gives none: EFF_POINTS from 5 % to 120 % of its i_out."""
    lo, hi = EFF_RANGE
    return [i_out * (lo + (hi - lo) * k / (EFF_POINTS - 1)) for k in range(EFF_POINTS)]


def _fets(spec) -> list[str]:
    """The stage's switching FETs (not diodes): their gates are driven every cycle."""
    refs = (spec.high_side or []) + (spec.low_side or []) + (spec.switch or [])
    return [r for r in refs if not r.upper().startswith("D")]


def fixed_losses(spec, v_in: float) -> tuple[float, float]:
    """(gate drive, controller) in W, the same at every load current [A80]: each switching FET's gate charge driven
    every cycle from v_gate, n·Q_g·V_gate·f_sw (EFF_V_GATE without a v_gate; none without qg), and the controller's
    supply current from the input, I_q·V_in (none without iq). Shared by the sweep and the stage's input estimate
    (power.input_estimate), so one operating point has one efficiency."""
    v_gate = spec.v_gate if spec.v_gate is not None else EFF_V_GATE
    p_gate = len(_fets(spec)) * spec.qg * v_gate * spec.f_sw if spec.qg and spec.f_sw else 0.0
    return p_gate, (spec.iq * v_in if spec.iq else 0.0)


def scaled_paths(paths: list, i_out: float, i_in: float, i_ref: float) -> list:
    """The stage's solved DC paths at another operating point (a DC solve is linear in its current): the input
    paths at I_in, the output paths at I_out, any other path with the load (ref·I_out/I_ref)."""
    out = []
    for p in paths:
        ref = p.get("current_a") or 0.0
        if p["name"].startswith("input"):
            amps = i_in
        elif p["name"].startswith("output"):
            amps = i_out
        else:                                   # another path: with the load
            amps = ref * i_out / i_ref if i_ref else ref
        out.append(dict(p, current_a=amps))
    return out


def losses_at(board, config, spec, res, i_out: float, v_in: float, duty: float, i_ref: Optional[float] = None
              ) -> dict:
    """The stage's losses at the load current i_out, input voltage v_in and duty (its own V_out and f_sw), on its
    solved DC paths scaled to the point (scaled_paths): {p_out, p_loss, efficiency, i_in, by {category: W},
    rows (part_losses' rows), copper, missing}. i_ref: the load current the paths were solved at (default
    spec.i_out: the stage's own; a sweep passes its nominal one). The input current from the energy balance,
    EFF_ROUNDS rounds
    (V_in·I_in = P_out + losses). Shared by the efficiency sweep and the operating-point sweep
    (analysis/converter_sweep.py)."""
    from .thermal import part_losses
    boost = spec.topology == "boost"
    v_out = spec.v_out or (None if boost or duty is None else duty * v_in)
    p_out = (v_out or 0.0) * i_out
    s_i = replace(spec, i_out=i_out, v_in=v_in, name=res.net)
    one_cfg = replace(config, converters=[s_i])
    p_gate, p_ctrl = fixed_losses(s_i, v_in)
    paths = list(getattr(res, "dc", []) or [])
    i_in = p_out / v_in                         # lossless, then the energy balance's rounds
    rows, missing, copper, p_loss, scaled = [], [], 0.0, 0.0, []
    for _ in range(EFF_ROUNDS):
        scaled = scaled_paths(paths, i_out, i_in, spec.i_out if i_ref is None else i_ref)
        probe = SimpleNamespace(net=res.net, duty=duty, v_in=v_in, dc=scaled, input={"i_in_a": i_in},
                                kind="converter")
        rows, missing = part_losses(board, one_cfg, [probe])
        copper = sum(p.get("r_copper_mohm", 0.0) * 1e-3 * p["current_a"] ** 2 for p in scaled)
        p_loss = sum(r["power_w"] for r in rows) + copper + p_gate + p_ctrl
        i_in = (p_out + p_loss) / v_in
    cats = {c: 0.0 for c in CATEGORIES}
    for r in rows:
        for c, w in (r.get("by") or {"other": r["power_w"]}).items():
            cats[c if c in cats else "other"] += w
    cats["copper"] += copper
    cats["gate drive"] += p_gate
    cats["controller"] += p_ctrl
    return {"p_out": p_out, "p_loss": p_loss, "efficiency": p_out / (p_out + p_loss) if p_out + p_loss > 0 else 0.0,
            "i_in": i_in, "by": cats, "rows": rows, "copper": copper, "missing": missing, "paths": scaled,
            "config": one_cfg}


def sweep(board, config, spec, res, currents: Optional[list] = None) -> Optional[EfficiencyResult]:
    """The stage's efficiency over load current (see the module), or None without an input voltage, an output
    voltage (or duty) or an output current. res: its ConverterResult (duty, v_in, its solved DC paths)."""
    from .thermal import ripple_pp
    v_in = spec.v_in or res.v_in
    if not v_in or not spec.i_out:
        return None
    boost = spec.topology == "boost"
    v_out = spec.v_out or (None if boost or res.duty is None else res.duty * v_in)    # a buck: D·V_in
    if not v_out:
        return None
    currents = sorted(currents or spec.efficiency_currents or default_currents(spec.i_out))
    out = EfficiencyResult(res.net, v_in, v_out, spec.i_out, currents)
    by = {c: [] for c in CATEGORIES}
    one_cfg, missing = None, []
    for i in currents:
        at = losses_at(board, config, spec, res, i, v_in, res.duty)
        one_cfg, missing = at["config"], at["missing"]
        for c in CATEGORIES:
            by[c].append(at["by"][c])
        out.p_out.append(at["p_out"])
        out.p_loss.append(at["p_loss"])
        out.efficiency.append(at["efficiency"])
    out.by = {c: v for c, v in by.items() if any(x > 0 for x in v)}
    # continuous conduction ends where the inductor current's valley reaches zero: I_L = ΔI/2 (a buck's I_L is
    # I_out, a boost's I_out/(1 - D_sw) = I_out/d) [Erickson 2001]
    di = ripple_pp(board, one_cfg or config, spec, res.duty if res.duty is not None else 0.5, v_in)
    if di:
        d = res.duty if res.duty is not None else 0.5
        out.ccm_from = di / 2 * (d if boost else 1.0)
    out.notes = list(dict.fromkeys(missing)) + _left_out(spec)
    return out


def _left_out(spec) -> list[str]:
    """What the sweep leaves out for this stage, in words."""
    notes = []
    if not spec.qg:
        notes.append(f"{spec.name}: no qg (the FETs' gate charge, their Qg field): the gate-drive loss is left out")
    elif spec.v_gate is None:
        notes.append(f"{spec.name}: no v_gate, so the gates are driven from {EFF_V_GATE:g} V (assumed)")
    if spec.qg:
        # Q_g·V_gate·f_sw is what a gate supply at V_gate delivers; a linear regulator from the input draws the
        # same charge at V_in (Q_g·V_in·f_sw: the regulator burns the difference) [A80]
        notes.append(f"{spec.name}: the gate-drive loss takes the gates' supply as a rail at their drive voltage; "
                     "fed by a linear regulator from the input it is V_in / V_gate times larger")
    if not spec.iq:
        notes.append(f"{spec.name}: no iq (the controller's supply current): its own consumption is left out")
    if spec.core_loss is None:
        notes.append(f"{spec.name}: no core_loss (the inductor's core loss at this frequency and ripple, from the "
                     "maker's tool or curves): left out")
    notes.append("not modelled: the winding's AC resistance (it needs the maker's loss "
                 "data), the capacitors' ESR loss from their ripple current; below the marked current a controller "
                 "may skip pulses or run discontinuous, which the model (continuous conduction) does not")
    return notes


def measured_path(config, spec) -> Optional[Path]:
    """The stage's measured-efficiency CSV: relative to the board's groundtruth/ folder (or absolute)."""
    if not spec.efficiency_measured:
        return None
    p = Path(spec.efficiency_measured)
    if not p.is_absolute():
        from ..config.settings import PROJECT_DIR
        p = Path(config.base_dir or ".") / PROJECT_DIR / p
    return p


def read_measured(path: Path) -> tuple[list[tuple], str]:
    """(points [(current A, efficiency 0-1, loss W or None)], a note) from a CSV: the first three columns current,
    efficiency (in % when any value is above 1.5, else a fraction) and optionally the loss in W; a header row and
    rows that are not numbers are skipped. The note says what went wrong (a missing file, no rows)."""
    if not path.is_file():
        return [], f"measured efficiency: {path} not found"
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for line in csv.reader(f, dialect):
                try:
                    vals = [float(x.strip().replace("%", "")) for x in line[:3] if x.strip()]
                except ValueError:
                    continue
                if len(vals) >= 2 and all(math.isfinite(v) for v in vals):
                    rows.append(vals)
    except OSError as e:
        return [], f"measured efficiency: {path}: {e}"
    if not rows:
        return [], f"measured efficiency: no rows of numbers in {path.name}"
    pct = any(r[1] > 1.5 for r in rows)
    pts = sorted((r[0], r[1] / 100.0 if pct else r[1], r[2] if len(r) > 2 else None) for r in rows)
    return pts, f"measured: {path.name}, {len(pts)} point(s)"


def with_measured(e: EfficiencyResult, config) -> EfficiencyResult:
    """The view with the stage's measured points (read here, on the computer that draws)."""
    spec = next((c for c in config.converters if c.name == e.net), None)
    path = measured_path(config, spec) if spec is not None else None
    if path is None:
        return e
    pts, note = read_measured(path)
    return replace(e, measured=pts, measured_note=note)
