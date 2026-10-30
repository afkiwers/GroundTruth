"""The `groundtruth/<board>.groundtruth.yaml` settings: analysed nets, part models, rule settings and waivers.

The file is read and written with ruamel.yaml in round-trip mode so that user comments and
ordering survive when `groundtruth init` (or the plugin UI) updates it.
"""
from __future__ import annotations

import copy
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..components.defaults import suggest_model
from ..components.models import ComponentModel
from ..components.values import format_si, parse_value
from ..core.board import Board
from .modifications import Modification, ModificationError

SETTINGS_SUFFIX = ".groundtruth.yaml"
SCHEMA_VERSION = 1
GND_PATTERN = re.compile(r"(^|[/_\-])(A|D|P|S|CHASSIS|EARTH)?GND(\d*|_.*)?$|^(VSS|0V)$", re.IGNORECASE)

HEADER = """\
GroundTruth settings file: return-path / EMI analysis of {board}
Edit by hand or from the KiCad plugin. `groundtruth init` refreshes the parts list and keeps your edits.
Part models: short | open | resistor (R) | cap (C, ESR, ESL) | inductor (L, R) | ferrite (R, L)
Net entries: driver/receivers are "REF.PAD"; freq in Hz (or "20MHz"); rise_time in s (or "2ns").
Converters: switching power stages (hot loop, input capacitors, conducted emission); `groundtruth detect` suggests them.
Bridges: motor drives (half-bridge legs on one bus, six-step), with the operating point and the motor; detected too.
"""

TOPOLOGIES = ("buck", "boost", "custom")
CONDUCTED_STANDARDS = ("cispr32", "cispr25", "mil461", "none")


class ConfigError(ValueError):
    """Settings that cannot be used. Parsing collects every problem it finds before raising, so the user sees all
    of them at once (`messages`, one per problem, each naming the entry: "nets[2].freq: ...")."""

    def __init__(self, messages: list[str]):
        """The problems found; the exception's text is all of them, joined by "; "."""
        super().__init__("; ".join(messages))
        self.messages = messages


PROJECT_DIR = "groundtruth"     # next to the board: everything GroundTruth keeps for it (the KiCad root stays tidy)


def project_dir(board_path: Union[str, Path]) -> Path:
    """<board folder>/groundtruth: the boards' settings files, the simulation history, exported reports (the user:
    "This keeps the kicad root folder cleaner and not cluttered"). Shared by the boards of one folder: each file
    carries its board's name."""
    return Path(board_path).parent / PROJECT_DIR


def settings_path(board_path: Union[str, Path]) -> Path:
    """<board folder>/groundtruth/<board>.groundtruth.yaml. Only there: a settings file next to the board (the
    layout before 2026-10-10) is not read (the user: no compatibility with earlier versions)."""
    p = Path(board_path)
    return project_dir(p) / (p.stem + SETTINGS_SUFFIX)


# --------------------------------------------------------------------------- dataclasses
@dataclass
class NetSpec:
    """A signal net to analyse: its driver and receiver pins, the signal (frequency, edge times, amplitude), the
    source and load circuit, the parts the path may run through, and where it came from (manual or detected,
    reviewed or not). Unset optional values fall back to the analysis defaults."""
    name: str
    driver: str
    receivers: list[str]
    freq: float
    rise_time: float
    amplitude_v: float = 3.3
    duty: float = 0.5
    current_a: Optional[float] = None   # overrides the line-current estimate (peak A at the fundamental)
    driver_ohms: Optional[float] = None   # driver output resistance (default analysis.driver_ohms)
    load_pf: Optional[float] = None       # input capacitance per receiver (default analysis.load_pf)
    fall_time: Optional[float] = None     # s, when it differs from rise_time
    driver_nh: Optional[float] = None     # source inductance (package, bond wire), nH
    driver_pf: Optional[float] = None     # source output capacitance, pF
    load_ohms: Optional[float] = None     # resistance at the receivers (a termination there), ohms; none = open
    through: list[str] = field(default_factory=list)   # parts the path may pass although they don't pass a signal
                                          # by default (an AC-coupling capacitor, a resistor > 1 kΩ): emi/path.py
    driver_return: Optional[str] = None   # explicit return pin, e.g. "U1.GND" pad "U1.17"
    receiver_return: Optional[str] = None
    source: str = "manual"                # manual | auto (suggested by `groundtruth detect`)
    reviewed: bool = True                 # auto suggestions start unreviewed
    signal_class: str = ""                # e.g. "SPI clock" (informational)
    enabled: bool = True                  # off: kept with its values, left out of checks and Simulate (Config.active)
    # the Bode sweep of the path's circuit (analysis/simulate.path_responses): from / to (Hz) and its points; unset:
    # a hundredth of the signal's frequency to the top of its harmonics' band, SWEEP_POINTS
    sweep_from: Optional[float] = None
    sweep_to: Optional[float] = None
    sweep_points: Optional[int] = None
    # the path's bandwidth limit (Hz): its −3 dB frequency at the receiver above it is a bandwidth_limit issue (an
    # ADC input's anti-alias filter, a sensor's); None: no limit
    max_bandwidth: Optional[float] = None


SWEEP_MIN_HZ = 0.01                       # the lowest frequency a Bode sweep may start at (near DC, never 0: log axis)
SWEEP_POINTS = (20, 5000)                 # the fewest and most points a sweep may have


@dataclass
class ConverterSpec:
    """A switching power stage. The switches commutate the load current within the edge time; that current
    flows around the hot loop: rail capacitors -> high-side switch -> switch node -> low-side switch -> ground."""
    name: str
    rail: str                             # net of the hot loop's capacitors: VIN of a buck, VOUT of a boost
    ground: str                           # power ground net
    switch_node: Optional[str] = None
    high_side: list[str] = field(default_factory=list)   # parts between rail and switch node (FET, IC with a switch)
    low_side: list[str] = field(default_factory=list)    # parts between switch node and ground (FET, catch diode)
    switch: list[str] = field(default_factory=list)      # integrated power stage between rail and ground (both
                                                         # switches inside: the hot loop runs through the IC)
    topology: str = "buck"                # buck | boost | custom (sets the pulse amplitude and duty defaults)
    f_sw: float = 500e3
    i_out: float = 1.0                    # load current, A
    v_in: Optional[float] = None
    v_out: Optional[float] = None
    duty: Optional[float] = None          # fraction of the period the high-side switch conducts
    rise_time: float = 10e-9              # current commutation (switch-node edge) time
    c_oss: Optional[float] = None         # switch output capacitance at the bus voltage: ringing frequency and peak
    q_oss: Optional[float] = None         # switch output charge (0 → bus voltage): hard-switching loss Q_oss·V·f_sw
    r_damp: Optional[float] = None        # damping resistance of the ringing (default 0.1 ohm, decays in a few cycles)
    c_stray: Optional[float] = None       # switch node to chassis/heatsink capacitance: common-mode estimate
    input_connector: list[str] = field(default_factory=list)   # ["J1.1", "J1.2"]: + and - pins, where the LISN goes
    output_connector: list[str] = field(default_factory=list)  # ["J2.1", "J2.2"]: + and - pins of the load (DC
                                                               # output path and ground return)
    spread: Optional[float] = None        # spread spectrum: f_sw dithered from f_sw to f_sw·(1 + spread), e.g. 0.2
    spread_mode: str = "random"           # random (a new frequency every cycle, or spread_rate per s) | triangle
    spread_rate: Optional[float] = None   # triangle: modulation frequency; random: hops per second (default: per cycle)
    conducted: Optional[str] = None       # conducted standard for this stage (default analysis.conducted)
    # interleaving (analysis/interleave.py): stages with the same group name are phases of one controller, switching
    # at one frequency, this one delayed by phase_deg of the switching period (None: the group's stages spread
    # evenly, 360°/n apart in the settings' order); their conducted and radiated emissions are added as phasors
    interleave: str = ""
    phase_deg: Optional[float] = None
    source: str = "manual"                # manual | auto (suggested by `groundtruth detect`)
    reviewed: bool = True
    enabled: bool = True                  # off: kept with its values, left out of checks and Simulate (Config.active)
    note: str = ""
    # thermal solve (analysis/thermal.py): the switches' and the inductor's losses
    rds_on: Optional[float] = None        # high-side (or integrated) switch R_DS(on), ohm
    rds_on_low: Optional[float] = None    # low-side FET R_DS(on), ohm (default: rds_on)
    dcr: Optional[float] = None           # inductor DC resistance, ohm
    diode_vf: Optional[float] = None      # catch diode forward voltage, V (default 0.5)
    # a synchronous rectifier FET (a buck's low side, a boost's high side): both switches are off for a dead time
    # before every turn-on, and the inductor current then flows through the rectifier in reverse at v_sd; at the
    # main switch's hard turn-on its body diode gives up q_rr (silicon; GaN has none) [A48, A49]. One dead time per
    # switch-node edge (YAML `dead_time: 20ns` sets both): rise = low side off -> high side on, fall = high side
    # off -> low side on (a buck's rise is its hard edge, a boost's fall)
    dead_rise: Optional[float] = None     # s (default DEFAULT_DEAD_TIME, an adaptive controller's)
    dead_fall: Optional[float] = None     # s (default DEFAULT_DEAD_TIME)
    v_sd: Optional[float] = None          # V, its reverse conduction (default: GaN 2 V, silicon body diode 0.8 V)
    q_rr: Optional[float] = None          # C, reverse-recovery charge of the side's FETs together (default none)
    # keys taken from the parts' schematic fields (components/fields.py): refreshed from them on every open and run;
    # the other parameters are set in GroundTruth (flagged where they differ from the schematic)
    from_schematic: list[str] = field(default_factory=list)
    # an RC snubber across the switch node: its parts [C, R] on the board (detected, or a bridge leg's), or proposed
    # values (snubber_c F, snubber_r ohm: YAML `snubber: {C: 2.2n, R: 1.5}`, the fix advisor's what-if)
    snubber: list[str] = field(default_factory=list)
    snubber_c: Optional[float] = None
    snubber_r: Optional[float] = None
    # set only on the stages made from a motor bridge's legs (BridgeSpec.stages), never read from or written to the
    # converters section: the bridge's name and the leg's phase
    bridge: Optional[str] = None
    phase: str = ""
    phase_output: str = ""                # the motor connector pin of a leg ("J1.A")
    # a leg's low side on a net of its own that reaches `ground` through series parts (per-phase current sensing):
    # that net (the hot loop's low-side pads are on it) and the parts (the shunt, a lumped branch to ground)
    low_return: str = ""
    low_shunt: list[str] = field(default_factory=list)
    # the control loop (analysis/loop.py): overrides of what the board and the controller's symbol fields give:
    # {mode, vref, gm, ro, ea_gain, ea_gbw, vsense_max, ith_range, cs_gain, slope, ramp, r_sense} (values with units)
    loop: dict = field(default_factory=dict)

    @property
    def parts(self) -> list[str]:
        """Every switching part of the stage, each once, in order (dict.fromkeys keeps the first occurrence)."""
        return list(dict.fromkeys(self.high_side + self.low_side + self.switch))

    def pulse(self) -> tuple[float, float]:
        """(amplitude in A, duty) of the current pulse the hot loop carries: the load current for a buck, the
        input (inductor) current for a boost; the duty is the high-side conduction time."""
        if self.topology == "boost":
            # a boost's hot loop carries the inductor (input) current I_out * V_out / V_in (power balance, no
            # losses), and its high-side (synchronous rectifier) conducts for 1 - D = V_in / V_out of the period
            if self.v_in and self.v_out and self.v_out > self.v_in:
                return self.i_out * self.v_out / self.v_in, self.duty or self.v_in / self.v_out
            # without both voltages the duty (given, else ½) is the high side's share: I_out = d·I_L, so the
            # inductor current is I_out / d (before 2026-10-17 the load current itself: 1/d too little)
            d = self.duty or 0.5
            return self.i_out / d, d
        if self.duty is not None:
            return self.i_out, self.duty
        if self.topology == "buck" and self.v_in and self.v_out and 0 < self.v_out < self.v_in:
            return self.i_out, self.v_out / self.v_in          # an ideal buck: D = V_out / V_in
        return self.i_out, 0.5


PWM_SCHEMES = ("hpwm_lon", "hon_lpwm", "bipolar")
DRIVE_MODES = ("motoring", "generating")
SWEEP_MAX = 64                                 # operating points of a bridge sweep (currents × speeds)
CONNECTIONS = ("star", "delta")
EMF_SHAPES = ("trapezoidal", "sinusoidal")


@dataclass
class DcLoad:
    """One load on a DC rail (DcLoadSpec): the pin drawing the current, the pin its current returns through (to the
    ground net) and the current, A."""
    pin: str
    current: float
    ret: str = ""                       # the return pin ("" = only the supply side is modelled)


@dataclass
class PdnSpec:
    """A supply rail's power-distribution network to check (settings `pdn:`, analysis/pdn.py): the impedance its
    ICs see between their supply and ground pins from 10 kHz to 1 GHz (decaps, planes, vias, the regulator) against
    a target impedance V·ripple / I_step, and the decaps near each supply pin."""
    rail: str
    name: str = ""                      # default: the rail's name
    ground: str = ""                    # default: the first reference net
    voltage: Optional[float] = None     # V; default from the rail's name (+3V3: 3.3)
    ripple: float = 0.05                # the allowed ripple, a fraction of the voltage
    i_step: Optional[float] = None      # A, the load step the rail must carry; without it no target (curves only)
    source: str = ""                    # the part feeding the rail (a regulator, a connector); "" = found; none
    vrm_r: float = 5e-3                 # Ω, the source's output resistance (a regulator's, flat in its bandwidth)
    vrm_l: float = 20e-9                # H, its output inductance (above the regulator's loop bandwidth)
    parts: list[str] = field(default_factory=list)   # the ICs to check; default every part with supply pins on it
    check_to: float = 100e6             # Hz: the target applies up to here (the package and die decouple above)
    decap_mm: float = 3.0               # a supply pin with no decap pad on the rail this close is an issue
    enabled: bool = True
    note: str = ""

    @property
    def label(self) -> str:
        """Its name in lists and issues: `name`, else the rail."""
        return self.name or self.rail


@dataclass
class DcLoadSpec:
    """A DC rail with loads (the user, of a passive distribution board: "how can I simulate a load across the
    highlighted trace?"): the supply pin feeding it (and its return pin), the loads drawing fixed currents. Solved on
    the whole-board DC copper model (analysis/dcload.py): the current density, the drop to each load, the loss, the
    busiest vias, the temperatures and the current capacity (Thermal, Current capacity)."""
    name: str
    source: str                         # the supply pin, "REF.PAD"
    loads: list[DcLoad] = field(default_factory=list)
    source_return: str = ""             # its return pin: with it (and every load's), the ground return is solved too
    max_drop: Optional[float] = None    # V: a load seeing a larger total drop is an issue (dc_drop)
    enabled: bool = True
    note: str = ""

    @property
    def current(self) -> float:
        """The rail's total current, A."""
        return float(sum(ld.current for ld in self.loads))

    @property
    def pins(self) -> list[str]:
        """Every pin it names."""
        return [p for p in [self.source, self.source_return, *(x for ld in self.loads for x in (ld.pin, ld.ret))]
                if p]


@dataclass
class LegSpec:
    """One half-bridge of a motor bridge: the high-side switches from the bus rail to its phase (switch) node, the
    low-side switches from the node to ground. The node goes to the motor, whose winding is the inductor: there is
    none on the board."""
    phase: str                            # "A", "B", "C" (from the connector's pins or the node's name)
    switch_node: str
    high_side: list[str] = field(default_factory=list)
    low_side: list[str] = field(default_factory=list)
    output: str = ""                      # the connector pin to the motor, "J1.A"
    driver: str = ""                      # gate driver IC (information, for highlighting)
    inputs: list[str] = field(default_factory=list)    # the driver's input nets (HIN, LIN): where the PWM comes from
    snubber: list[str] = field(default_factory=list)   # [C, R] from the node to the rail or ground
    # the low side's own net when it reaches the bridge's ground through series parts (a per-phase current-sense
    # shunt): that net ("" = the low side is on the bridge's ground) and those parts
    ground_return: str = ""
    shunt: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list[str]:
        """The leg's switches, each once."""
        return list(dict.fromkeys(self.high_side + self.low_side))


@dataclass
class MotorSpec:
    """The motor on the bridge's outputs, by its terminal (line-to-line) data, as datasheets give it and as it is
    measured at the connector: R and L between two terminals and the back-EMF constant as the line-to-line peak per
    1000 rpm. Star or delta then only changes the shape of the back-EMF the terminals see (analysis/motor.py)."""
    connection: str = "star"              # star | delta
    r_ll: Optional[float] = None          # ohm, between two terminals
    l_ll: Optional[float] = None          # H, between two terminals
    ke_ll: Optional[float] = None         # V line-to-line peak per 1000 rpm
    pole_pairs: Optional[int] = None
    emf: str = "trapezoidal"              # trapezoidal (BLDC) | sinusoidal (PMSM)
    advance_deg: float = 0.0              # commutation advance, electrical degrees


@dataclass
class BridgeSpec:
    """A motor bridge: half-bridge legs on one bus rail and ground, driven in six-step (Hall) commutation. The
    operating point and the motor are the user's; the legs are found by `groundtruth detect`."""
    name: str
    rail: str
    ground: str
    legs: list[LegSpec] = field(default_factory=list)
    input_connector: list[str] = field(default_factory=list)   # [+, -] where the bus comes in (the LISN goes there)
    conducted: Optional[str] = None       # conducted standard (default analysis.conducted)
    v_bus: Optional[float] = None         # V
    f_pwm: float = 20e3                   # Hz
    i_phase: Optional[float] = None       # A, mean current of the conducting phase pair (> 0; the sign is `mode`)
    mode: str = "motoring"                # motoring | generating
    speed_rpm: Optional[float] = None     # mechanical rpm (0: locked rotor, cranking)
    pwm: str = "hpwm_lon"                 # hpwm_lon | hon_lpwm | bipolar
    synchronous: bool = True              # the chopping leg's other switch conducts in the off time
    dead_time: float = 50e-9              # s, before every turn-on
    rise_time: float = 2e-9               # s, switch-node edge
    d_max: float = 0.98                   # largest duty (bootstrap refresh)
    rds_on: Optional[float] = None        # ohm, one high-side FET
    rds_on_low: Optional[float] = None    # ohm, one low-side FET (default rds_on)
    v_sd: Optional[float] = None          # V, reverse conduction of an off switch (GaN ~2 V, Si body diode ~0.8 V)
    c_oss: Optional[float] = None
    q_oss: Optional[float] = None
    r_damp: Optional[float] = None
    c_stray: Optional[float] = None       # phase node, cable and motor frame to chassis (common mode)
    motor: MotorSpec = field(default_factory=MotorSpec)
    sweep_i_phase: list[float] = field(default_factory=list)    # A: operating points Simulate also evaluates,
    sweep_speed_rpm: list[float] = field(default_factory=list)  # every current at every speed (analysis/bridge.py)
    source: str = "manual"
    reviewed: bool = True
    enabled: bool = True                  # off: kept with its values, left out of checks and Simulate (Config.active)
    note: str = ""
    from_schematic: list[str] = field(default_factory=list)     # as ConverterSpec.from_schematic

    @property
    def parts(self) -> list[str]:
        """Every switch of every leg, each once."""
        return list(dict.fromkeys(r for leg in self.legs for r in leg.parts))

    def leg_name(self, leg: LegSpec) -> str:
        """A leg's name as a power stage: the bridge's name and the phase ("bridge J1 A")."""
        return f"{self.name} {leg.phase}"

    def stages(self) -> list[ConverterSpec]:
        """The legs as power stages for the hot-loop solver (analysis/power.py): one switching cell each, with the
        bus as its rail. Current and duty are placeholders until the drive model (analysis/bridge.py) sets them."""
        return [ConverterSpec(
            name=self.leg_name(leg), rail=self.rail, ground=self.ground, switch_node=leg.switch_node,
            high_side=list(leg.high_side), low_side=list(leg.low_side), topology="custom", f_sw=self.f_pwm,
            i_out=self.i_phase or 1.0, v_in=self.v_bus, duty=0.5, rise_time=self.rise_time, c_oss=self.c_oss,
            q_oss=self.q_oss, r_damp=self.r_damp, c_stray=self.c_stray, input_connector=list(self.input_connector),
            conducted=self.conducted, source=self.source, reviewed=self.reviewed, rds_on=self.rds_on,
            rds_on_low=self.rds_on_low, diode_vf=self.v_sd, bridge=self.name, phase=leg.phase,
            snubber=list(leg.snubber), phase_output=leg.output, low_return=leg.ground_return,
            low_shunt=list(leg.shunt)) for leg in self.legs]


@dataclass
class AnalysisSettings:
    """The settings file's `analysis:` section: grid and model sizes, the emission limit and distance, the solver's
    threads and GPU, the default source and load of the nets, and the conducted standard."""
    grid_mm: Union[str, float] = "auto"
    margin_mm: float = 10.0
    solve_points: int = 4
    max_nodes: int = 100_000
    via_plating_um: float = 25.0          # via barrel plating (current density, IPC-2221 capacity)
    limit_class: str = "B"
    distance_m: Optional[float] = None    # None = the limit's own (CISPR 32: 10 m < 1 GHz, 3 m above)
    # a radiated limit of the user's instead of CISPR 32 (emi/radiation.RadiatedLimit: MIL-STD-461 RE102 for the
    # platform, a customer's curve)
    radiated_limit: Optional[object] = None
    ground_reflection: bool = True
    planes: str = "auto"                  # auto = every plane layer in the region, nearest = one plane only
    plane_capacitance: bool = True        # displacement current / cavity resonance between facing planes
    cavity_max_nodes: int = 40_000        # grid budget of the board-wide plane-capacitance model
    max_solves: int = 10                  # harmonics solved per route (initial + adaptive refinement)
    max_frequency: Optional[float] = None  # Hz: the highest harmonic evaluated (None = auto: the edge's own band,
                                           # simulate.band_top); above it nothing is computed and the result says so
    resolution: float = 1.0               # every model's node budget x this, its cell / sqrt(this): the plugin's
                                          # per-machine "Resolution" and the CLI's --resolution (not in the file)
    driver_ohms: float = 25.0             # default driver output resistance
    load_pf: float = 5.0                  # default receiver input capacitance
    threads: Union[str, int] = "auto"     # parallel solves: "auto" (physical cores, at most 4) or a number
    gpu: str = "auto"                     # solver iterations on a GPU (PyTorch): auto (if one is there) | on | off
    sparse: Optional[str] = None          # the preconditioner's factorisation (solver/sparse.py): auto | superlu |
                                          # pardiso | accelerate; None: GROUNDTRUTH_SPARSE, else auto
    all_cores: bool = False               # "Use all cores" (per machine, Preferences / CLI --all-cores; not in the
                                          # file): auto threads = every physical core, several frequencies per
                                          # refinement round while threads would idle (simulate.solve_setups)
    conducted: str = "cispr32"            # conducted emission of converters: cispr32 | cispr25 | mil461 | none
    conducted_class: str = "B"            # cispr32: A | B, cispr25: 1-5, mil461: basic | 28V | 115V | 220V | 270V | 440V
    mil461: str = "G"                     # MIL-STD-461 revision: G (2015) | H (2026); only CE101 differs here
    ce101: str = "none"                   # MIL-STD-461 CE101 (lead current, 30 Hz-10 kHz; ships under 461H to
                                          # 20 kHz): none | aircraft | ships
    ce101_source_v: Optional[float] = None    # nominal EUT source voltage (CE101-4 curve); default: the stage's input
    ce101_load_a: Optional[float] = None      # EUT load current (CE101-1 relaxation); default: the stage's input current

    def limit_spec(self):
        """The radiated limit the estimates are checked against: the user's curve, else the CISPR 32 class."""
        return self.radiated_limit if self.radiated_limit is not None else self.limit_class

    def limit_text(self) -> str:
        """That limit in words ("CISPR 32 class B", or the user limit's name)."""
        from ..emi.radiation import describe_limit
        return describe_limit(self.limit_spec())

    def ce101_spec(self, source_v: Optional[float], load_a: Optional[float]):
        """The CE101 limit for a stage (emi/conducted.Ce101), or None when CE101 is off. The settings' source
        voltage and load current win over the stage's own input voltage and current (the EUT's are what the
        standard means; one stage's current relaxes CE101-1 less: the safe side)."""
        if self.ce101 == "none":
            return None
        from ..emi.conducted import Ce101
        v = self.ce101_source_v if self.ce101_source_v is not None else (source_v or 0.0)
        i = self.ce101_load_a if self.ce101_load_a is not None else (load_a or 0.0)
        return Ce101(self.ce101, float(v), float(i), self.mil461)


@dataclass
class RuleSettings:
    """The `rules:` section: the thresholds of the rule checks and the Simulate issues, and per-check severity
    overrides (error | warning | info | off)."""
    stitch_distance_mm: float = 2.0
    decap_distance_mm: float = 5.0
    edge_factor: float = 3.0
    min_gap_mm: float = 0.1
    detour_warning: float = 1.25          # loop L / ideal loop L
    detour_error: float = 2.0
    emi_margin_db: float = 6.0            # warn when estimate is within this many dB of the limit
    resonance_db: float = 10.0            # flag a plane resonance raising the loop moment this much over f0
    overshoot_warning: float = 0.25       # converter switch-node overshoot L·di/dt as a fraction of v_in
    overshoot_error: float = 0.5
    current_density_warning: float = 50.0  # A/mm², converter DC paths: copper (1 mm average) and via plating [A40]
    current_density_error: float = 100.0
    via_load_warning: float = 100.0         # % of a via's IPC-2221 capacity (10 K rise)
    via_load_error: float = 150.0           # about a 25 K rise
    severity: dict[str, str] = field(default_factory=dict)  # per-check override


@dataclass
class ThermalSettings:
    """The thermal solve (analysis/thermal.py): PCB Thermal's environment and mesh quality, and the limits for
    the thermal issues. theta_jb: part -> K/W junction to board, for junction estimates."""
    ambient_c: float = 25.0
    orientation: str = "horizontal"      # horizontal | vertical
    convection: str = "natural"          # natural | forced | fixed
    airspeed: float = 1.0                # m/s, forced
    h_top: float = 10.0                  # W/m²K, fixed
    h_bottom: float = 5.0
    radiation: bool = True
    quality: str = "normal"              # draft | normal | fine
    warn_c: float = 85.0
    max_c: float = 105.0
    theta_jb: dict = field(default_factory=dict)
    capacity_c: Optional[float] = None   # current capacity: the hottest copper/junction allowed (default max_c)
    # electro-thermal coupling (analysis/electrothermal.py): the DC paths solved again with the copper at its
    # temperature, then Thermal again, until no path's loss or via's current moves by more than the tolerance, at
    # most `electrothermal_rounds` rounds (1-10)
    electrothermal: bool = False
    electrothermal_rounds: int = 3
    electrothermal_tolerance: float = 0.01   # a share: 0.01 = 1 %


@dataclass
class DrawingSettings:
    """The KiCad User layers the plugin draws on: issue markers, suggested fixes, current contours and arrows, and
    the return current's arrows apart (results/vectors.split_return)."""
    issues_layer: str = "User.9"
    suggest_layer: str = "User.8"
    current_layer: str = "User.7"
    return_layer: str = "User.6"


@dataclass
class SafetySettings:
    """The `safety:` section (analysis/safety.py): net voltages beside the ones known from the converters, bridges,
    PDN rails and net names; the clearance table's column choice; the derating of capacitors, resistors and switches;
    where ESD protection is looked for."""
    voltages: dict = field(default_factory=dict)        # net -> V (peak), winning over what is known otherwise
    coated: bool = False        # a conformally coated assembly: IPC-2221B A5 / A7 where the solder mask is open
                                # (under the mask B4 either way; safety.spacing_column)
    # "ipc": the solder mask counts as IPC-2221B's permanent polymer coating where it covers a gap (B4); "bare":
    # it is left out and all outer copper is taken as uncoated (safety standards don't credit ordinary mask)
    solder_mask: str = "ipc"
    clearance_mm: Optional[list] = None                 # [[V, mm], ...] for external layers instead of B2 / B4
    cap_derating: dict = field(default_factory=dict)    # {ceramic, tantalum, electrolytic: fraction of the rating}
    resistor_derating: float = 0.5                      # a resistor's loss up to this fraction of its rating
    vds_derating: float = 0.8                           # a switch node's peak up to this fraction of the Vds rating
    esd_mm: float = 10.0                                # an ESD / TVS part on a connector pin's net this close
    esd_connectors: list = field(default_factory=list)  # the board's outside world (warnings); empty: all (info)
    gate_drive_v: float = 10.0                          # a gate or bootstrap net rides up to this above its source


@dataclass
class ImpedanceTarget:
    """A controlled impedance (settings `si: impedance:`, analysis/si.py): the nets it applies to (names, or
    patterns with * and ?), or a KiCad net class from the project file, and the single-ended Z0 with its tolerance."""
    z0: float                                           # Ω
    nets: list = field(default_factory=list)            # net names or wildcard patterns
    netclass: str = ""                                  # a KiCad net class (the .kicad_pro beside the board)
    tol: float = 0.10                                   # fraction of z0
    layers: list = field(default_factory=list)          # only on these copper layers (empty: every layer)


@dataclass
class DiffPairSpec:
    """A differential pair (settings `si: pairs:`, analysis/si.py): its two nets, the differential impedance to
    hold (none: shown, not checked), the edge time that sets the allowed skew and the crosstalk it suffers."""
    p: str
    n: str
    name: str = ""
    zdiff: Optional[float] = None       # Ω
    tol: float = 0.10                   # fraction of zdiff
    rise_time: float = 1e-9             # s, the pair's edge (20-80 %)
    max_skew: Optional[float] = None    # s, the delay mismatch allowed; default rise_time / 10
    source: str = "manual"              # manual | auto (suggested by `groundtruth detect`)
    reviewed: bool = True
    enabled: bool = True
    note: str = ""

    @property
    def label(self) -> str:
        """Its name in lists and issues: `name`, else "P / N" (the nets' last path parts)."""
        return self.name or f"{self.p.rsplit('/', 1)[-1]} / {self.n.rsplit('/', 1)[-1]}"

    @property
    def skew_limit(self) -> float:
        """The allowed delay mismatch (s): max_skew, else a tenth of the edge [A33] (the common mode a skew makes is about
        half the swing times skew / rise time: 5 % at a tenth)."""
        return self.max_skew if self.max_skew is not None else self.rise_time / 10


@dataclass
class SiSettings:
    """The `si:` section (analysis/si.py, the plan's M3)."""
    impedance: list = field(default_factory=list)       # ImpedanceTarget
    pairs: list = field(default_factory=list)           # DiffPairSpec
    crosstalk_max: float = 0.05         # NEXT or FEXT above this share of the aggressor's swing: an issue [A33]
    stub_factor: float = 3.0            # a via stub resonating below this times the signal's knee (0.35 / t_r)
    step_mm: float = 2.0                # cross-sections along a track this far apart


@dataclass
class ReportSettings:
    """What the exported HTML report's cover shows besides the board (report/html.py); all optional."""
    title: str = ""                       # default "EMI and power analysis"
    company: str = ""
    author: str = ""
    document: str = ""                    # a document or revision number


@dataclass
class ComponentEntry:
    """One part in the settings' `components:` list: its value and footprint as last seen on the board (to notice
    a changed part) and its model. `removed`: no longer on the board, kept so a model the user set is not lost."""
    reference: str
    value: str
    footprint: str
    model: ComponentModel
    removed: bool = False
    tht: dict = field(default_factory=dict)      # its through-hole pin values (analysis/pins.py), {key: value}


@dataclass
class Waiver:
    """An accepted issue: its fingerprint (core/issues.Issue.fingerprint) and the user's reason."""
    fingerprint: str
    reason: str = ""


STACK_SIDES = ("above", "below")
STACK_FLIPS = ("auto", "no", "yes")


@dataclass
class StackBoard:
    """Another board of a stack (a logic board above the power board): its file, where it sits, and the connectors
    that join it to this board, pin by pin (by number). Its position comes from those pins: the boards are placed
    so the mating pins line up."""
    name: str                     # its parts and nets get this prefix (logic:U8); nets joined by a pin keep ours
    file: str                     # its .kicad_pcb, relative to the settings file
    gap: float                    # mm between the two boards' facing surfaces
    connectors: list[list[str]] = field(default_factory=list)    # [this board's ref, its ref] per connector
    side: str = "above"           # above | below this board
    flip: str = "auto"            # auto | no | yes: it is mirrored (its top faces this board); auto: from the pins


@dataclass
class VictimSettings:
    """What the switching may upset (analysis/victims.py): parts whose ground moves, nets that pick up voltage.
    Empty lists: found on the board (microcontrollers, reset supervisors; reset, crystal, Hall, debug, PWM nets)."""
    parts: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    enabled: bool = True          # off: Simulate skips the victims (one more model per leg)
    reference: str = ""           # the pin the ground shift is measured against (default: the bridge's input minus)
    shift_warning_v: float = 0.3  # peak ground shift of a victim part
    induced_warning_v: float = 0.3   # peak voltage induced in a victim net's loop
    injected_warning_ma: float = 1.0  # peak current the phase nodes push into a victim net
    drive_ohms: float = 50.0      # a victim net's driver (push-pull output); a resistor driving it counts as itself
                                  # [A32]
    input_pf: float = 5.0         # a victim net's receiver input [A32]


@dataclass
class Config:
    """Everything the settings file holds, parsed: what to analyse (nets, converters, bridges), how (analysis,
    rules, thermal), the part models, waivers, what-if edits and a stack's other boards. The analyses take this,
    never the YAML."""
    reference_nets: list[str] = field(default_factory=lambda: ["GND"])
    nets: list[NetSpec] = field(default_factory=list)
    converters: list[ConverterSpec] = field(default_factory=list)
    bridges: list[BridgeSpec] = field(default_factory=list)
    dc_loads: list[DcLoadSpec] = field(default_factory=list)          # DC rails with loads (analysis/dcload.py)
    pdn: list[PdnSpec] = field(default_factory=list)                  # supply rails' PDN impedance (analysis/pdn.py)
    components: dict[str, ComponentEntry] = field(default_factory=dict)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    rules: RuleSettings = field(default_factory=RuleSettings)
    drawing: DrawingSettings = field(default_factory=DrawingSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    si: SiSettings = field(default_factory=SiSettings)
    report: ReportSettings = field(default_factory=ReportSettings)
    thermal: ThermalSettings = field(default_factory=ThermalSettings)
    waivers: list[Waiver] = field(default_factory=list)
    modifications: list[Modification] = field(default_factory=list)   # what-if edits (never applied by plain runs)
    stack: list[StackBoard] = field(default_factory=list)              # the other boards of a stack
    victims: VictimSettings = field(default_factory=VictimSettings)
    tht: dict = field(default_factory=dict)      # the board's through-hole pin defaults (analysis/pins.py)
    base_dir: str = ""            # the board's folder (stack board files are relative to it: SettingsFile.config)
    # Run checks' checks switched off on this computer (the plugin's Preferences, ui.json; never in the settings
    # file): their issues are left out, and a safety check that is off is not run (rules/checks.run_checks)
    checks_off: frozenset = field(default_factory=frozenset)

    def model_for(self, reference: str) -> Optional[ComponentModel]:
        """The model of a part, or None when the settings don't list it (or it is marked removed)."""
        e = self.components.get(reference)
        return e.model if e and not e.removed else None

    def active(self, names: Optional[Iterable[str]] = None) -> "Config":
        """The settings as the analyses see them: nets, converters and motor bridges switched off (`enabled:
        false`, the tick box on their tab) left out, unless named (CLI --nets, Check in Simulate). The user: "if
        one wants to not simulate a particular net, converter 'row' one has to delete it which then looses all the
        data entered". The same object when nothing is off."""
        named = set(names or ())

        def on(s) -> bool:
            """Whether a net, converter or bridge takes part: switched on, or named (a bridge also by a leg)."""
            legs = [s.leg_name(leg) for leg in s.legs] if isinstance(s, BridgeSpec) else []
            return s.enabled or s.name in named or any(leg in named for leg in legs)
        if all(on(s) for s in [*self.nets, *self.converters, *self.bridges, *self.dc_loads]) and \
                all(d.enabled or d.label in named for d in self.pdn):
            return self
        from dataclasses import replace as _replace
        return _replace(self, nets=[n for n in self.nets if on(n)], converters=[c for c in self.converters if on(c)],
                        bridges=[b for b in self.bridges if on(b)], dc_loads=[d for d in self.dc_loads if on(d)],
                        pdn=[d for d in self.pdn if d.enabled or d.label in named])

    def stages(self) -> list[ConverterSpec]:
        """Every switching stage: the converters and the motor bridges' legs (the code that finds a stage's nets,
        parts or copper takes it from here, so a leg is never unknown to it)."""
        return list(self.converters) + [s for b in self.bridges for s in b.stages()]

    def stage_specs(self, name: str) -> list[ConverterSpec]:
        """The stages a name stands for: a converter, one leg, or a bridge (all its legs)."""
        for b in self.bridges:
            if name == b.name:
                return b.stages()
        return [s for s in self.stages() if s.name == name]


# --------------------------------------------------------------------------- parsing helpers
def _threads(v: Any, errors: list[str]) -> Union[str, int]:
    """analysis.threads: "auto" or a whole number >= 1; anything else is reported in `errors` and taken as auto."""
    if v in (None, "auto"):
        return "auto"
    if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) >= 1:
        return int(v)
    errors.append(f"analysis.threads must be auto or a whole number >= 1, not {v!r}")
    return "auto"


def _sparse(v: Any, errors: list[str]) -> Optional[str]:
    """analysis.sparse: auto | superlu | pardiso | accelerate, or None when not given (anything else reported in
    `errors`, taken as not given)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s not in ("auto", "superlu", "pardiso", "accelerate"):
        errors.append(f"analysis.sparse must be auto, superlu, pardiso or accelerate, not {v!r}")
        return None
    return s


def _gpu(v: Any, errors: list[str]) -> str:
    """analysis.gpu: auto | on | off (anything else reported in `errors`, taken as auto)."""
    # YAML reads a bare on/off as true/false
    s = {True: "on", False: "off", None: "auto"}.get(v, v) if isinstance(v, (bool, type(None))) else str(v).lower()
    if s not in ("auto", "on", "off"):
        errors.append(f"analysis.gpu must be auto, on or off, not {v!r}")
        return "auto"
    return s


def _refs(v: Any) -> list[str]:
    """A list of references or pins as the file may write it: a YAML list, one string, or "Q1, Q2"."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [r.strip() for r in v.split(",") if r.strip()]
    return [str(r) for r in v]


def _share(v: Any, what: str) -> float:
    """A share as written in the settings: "1%" or "1 %" (per cent), or a plain fraction (0.01)."""
    if isinstance(v, str) and v.strip().endswith("%"):
        return _num(v.strip()[:-1].strip(), what) / 100.0
    return _num(v, what)


def _opt(v: Any, what: str, unit: str = "") -> Optional[float]:
    """An optional number: None for a missing value or "", "auto", "none"; else as _num."""
    return None if v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto", "none")) else _num(v, what, unit)


def _num(v: Any, what: str, unit: str = "") -> float:
    """A number as written in the settings: a YAML number, or text with an SI prefix and optionally its unit
    ("20MHz", "2ns", "4.7k"); `unit` is stripped first so "100V" reads as 100. Raises ConfigError naming `what`."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if unit and s.lower().endswith(unit.lower()):
            s = s[: -len(unit)]
        x = parse_value(s)
        if x is not None:
            return x
    raise ConfigError([f"{what}: cannot read number from {v!r}"])


def _mm(v: Any, what: str) -> float:
    """A length in mm: 5, 5.0 or "5mm" (not SI: "5m" would be 5 milli-units)."""
    if isinstance(v, str) and v.strip().lower().endswith("mm"):
        v = v.strip()[:-2]
    return _num(v, what)


def _yaml() -> YAML:
    """The YAML reader/writer: ruamel's round-trip mode keeps the user's comments, key order and quoting."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # keep one flow-style component per line
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _flow(d: dict) -> CommentedMap:
    """A mapping written in flow style on one line ({model: cap, C: 100n, ...}): one part or net per line keeps a
    long settings file readable and its diffs small."""
    m = CommentedMap(d)
    m.fa.set_flow_style()
    return m


def _choice(v: Any, default: str) -> str:
    """A keyword setting (mode, pwm, connection ...): lower case, `default` when missing."""
    return str(v if v is not None else default).strip().lower()


def _signed(v: Any, what: str) -> float:
    """A plain number that may be negative (parse_value reads no sign)."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ConfigError([f"{what}: cannot read number from {v!r}"]) from None


def _converter_snubber(v: Any, where: str) -> dict:
    """A converter's `snubber`: [C, R] parts on the board, or {C: 2.2n, R: 1.5} proposed values."""
    if isinstance(v, dict):
        return {"snubber_c": _opt(v.get("C"), f"{where}.snubber.C", "F"),
                "snubber_r": _opt(v.get("R"), f"{where}.snubber.R", "ohm")}
    refs = _refs(v)
    if refs and len(refs) != 2:
        raise ConfigError([f"{where}.snubber: two parts [C, R], or values {{C: 2.2n, R: 1.5}}"])
    return {"snubber": refs}


def _bridge_from_data(b: dict, where: str, i: int) -> BridgeSpec:
    """A `bridges:` entry as a BridgeSpec (numbers with or without units); raises ConfigError for unreadable values."""
    legs = []
    for k, leg in enumerate(b.get("legs") or []):
        if not isinstance(leg, dict):
            raise ConfigError([f"{where}.legs[{k}]: must be a mapping"])
        legs.append(LegSpec(phase=str(leg.get("phase") or k + 1), switch_node=str(leg.get("switch_node") or ""),
                            high_side=_refs(leg.get("high_side")), low_side=_refs(leg.get("low_side")),
                            output=str(leg.get("output") or ""), driver=str(leg.get("driver") or ""),
                            inputs=_refs(leg.get("inputs")), snubber=_refs(leg.get("snubber")),
                            ground_return=str(leg.get("ground_return") or ""), shunt=_refs(leg.get("shunt"))))
    m = b.get("motor") or {}
    if not isinstance(m, dict):
        raise ConfigError([f"{where}.motor: must be a mapping"])
    sw = b.get("sweep") or {}
    if not isinstance(sw, dict):
        raise ConfigError([f"{where}.sweep: must be a mapping (i_phase: [...], speed_rpm: [...])"])
    sweep = {key: [_num(x, f"{where}.sweep.{key}", unit) for x in _refs(sw.get(key))]
             for key, unit in (("i_phase", "A"), ("speed_rpm", "rpm"))}
    pp = _opt(m.get("pole_pairs"), f"{where}.motor.pole_pairs")
    motor = MotorSpec(connection=_choice(m.get("connection"), "star"),
                      r_ll=_opt(m.get("r_ll"), f"{where}.motor.r_ll", "ohm"),
                      l_ll=_opt(m.get("l_ll"), f"{where}.motor.l_ll", "H"),
                      ke_ll=_opt(m.get("ke_ll"), f"{where}.motor.ke_ll", "V"),
                      pole_pairs=None if pp is None else int(round(pp)),
                      emf=_choice(m.get("emf"), "trapezoidal"),
                      advance_deg=_signed(m.get("advance_deg"), f"{where}.motor.advance_deg"))
    return BridgeSpec(
        name=str(b.get("name") or f"bridge {i + 1}"), rail=str(b.get("rail") or ""), ground=str(b.get("ground") or ""),
        legs=legs, input_connector=_refs(b.get("input_connector")),
        conducted=(str(b["conducted"]).lower().replace(" ", "").replace("-", "") if b.get("conducted") else None),
        v_bus=_opt(b.get("v_bus"), f"{where}.v_bus", "V"), f_pwm=_num(b.get("f_pwm", 20e3), f"{where}.f_pwm", "Hz"),
        i_phase=_opt(b.get("i_phase"), f"{where}.i_phase", "A"), mode=_choice(b.get("mode"), "motoring"),
        speed_rpm=_opt(b.get("speed_rpm"), f"{where}.speed_rpm", "rpm"), pwm=_choice(b.get("pwm"), "hpwm_lon"),
        synchronous=bool(b.get("synchronous", True)),
        dead_time=_num(b.get("dead_time", 50e-9), f"{where}.dead_time", "s"),
        rise_time=_num(b.get("rise_time", 2e-9), f"{where}.rise_time", "s"),
        d_max=_num(b.get("d_max", 0.98), f"{where}.d_max"),
        rds_on=_opt(b.get("rds_on"), f"{where}.rds_on", "ohm"),
        rds_on_low=_opt(b.get("rds_on_low"), f"{where}.rds_on_low", "ohm"),
        v_sd=_opt(b.get("v_sd"), f"{where}.v_sd", "V"), c_oss=_opt(b.get("c_oss"), f"{where}.c_oss", "F"),
        q_oss=_opt(b.get("q_oss"), f"{where}.q_oss", "C"), r_damp=_opt(b.get("r_damp"), f"{where}.r_damp", "ohm"),
        c_stray=_opt(b.get("c_stray"), f"{where}.c_stray", "F"), motor=motor,
        sweep_i_phase=sweep["i_phase"], sweep_speed_rpm=sweep["speed_rpm"], source=str(b.get("source", "manual")), reviewed=bool(b.get("reviewed", b.get("source") != "auto")),
        enabled=bool(b.get("enabled", True)),
        note=str(b.get("note", "") or ""), from_schematic=_refs(b.get("from_schematic")))


def _bridge_problems(b: BridgeSpec) -> list[str]:
    """What makes a bridge entry unusable at all (missing nets, legs without switches, bad choices). Missing
    operating or motor data is not here: an auto-detected bridge has none yet (simulation_blockers)."""
    out = []
    if not b.rail or not b.ground:
        out.append("needs rail and ground nets")
    if not b.legs:
        out.append("needs legs (each: phase, switch_node, high_side, low_side)")
    for leg in b.legs:
        if not leg.switch_node or not leg.high_side or not leg.low_side:
            out.append(f"leg {leg.phase}: needs switch_node, high_side and low_side")
    for key in ("phase", "switch_node"):
        values = [getattr(leg, key) for leg in b.legs]
        if len(set(values)) != len(values):
            out.append(f"two legs have the same {key}")
    for key, allowed in (("mode", DRIVE_MODES), ("pwm", PWM_SCHEMES)):
        if getattr(b, key) not in allowed:
            out.append(f"{key} must be one of {', '.join(allowed)}")
    for key, allowed in (("connection", CONNECTIONS), ("emf", EMF_SHAPES)):
        if getattr(b.motor, key) not in allowed:
            out.append(f"motor.{key} must be one of {', '.join(allowed)}")
    if b.input_connector and len(b.input_connector) != 2:
        out.append("input_connector needs two pins: [plus, minus]")
    if any(x <= 0 for x in b.sweep_i_phase) or any(x < 0 for x in b.sweep_speed_rpm):
        out.append("sweep: phase currents must be positive, speeds not negative")
    if len(b.sweep_i_phase or [0]) * len(b.sweep_speed_rpm or [0]) > SWEEP_MAX:
        out.append(f"sweep: at most {SWEEP_MAX} operating points (currents × speeds)")
    if b.conducted is not None and b.conducted not in CONDUCTED_STANDARDS:
        out.append(f"conducted must be one of {', '.join(CONDUCTED_STANDARDS)}")
    if b.f_pwm <= 0 or b.rise_time <= 0 or b.dead_time < 0:
        out.append("f_pwm and rise_time must be positive, dead_time not negative")
    if not 0 < b.d_max <= 1:
        out.append("d_max must be between 0 and 1")
    for key in ("v_bus", "i_phase"):
        if getattr(b, key) is not None and getattr(b, key) <= 0:
            out.append(f"{key} must be positive (the current's direction is `mode`)")
    if b.speed_rpm is not None and b.speed_rpm < 0:
        out.append("speed_rpm must not be negative")
    for key in ("r_ll", "l_ll", "ke_ll", "pole_pairs"):
        v = getattr(b.motor, key)
        if v is not None and v <= 0:
            out.append(f"motor.{key} must be positive")
    return out


def simulation_blockers(b: BridgeSpec) -> list[str]:
    """What a bridge still needs before it can be simulated: the operating point and the motor (detection leaves
    them to the user)."""
    out = []
    if len(b.legs) != 3:
        out.append(f"six-step needs three legs, this bridge has {len(b.legs)}")
    for key, what in (("v_bus", "the bus voltage (v_bus)"), ("i_phase", "the phase current (i_phase)"),
                      ("speed_rpm", "the speed (speed_rpm, 0 for a locked rotor)")):
        if getattr(b, key) is None:
            out.append(f"set {what}")
    for key, what in (("r_ll", "the motor's line-to-line resistance (r_ll)"),
                      ("l_ll", "the motor's line-to-line inductance (l_ll)")):
        if getattr(b.motor, key) is None:
            out.append(f"set {what}")
    if b.speed_rpm:
        for key, what in (("ke_ll", "the motor's back-EMF constant (ke_ll, V line-to-line peak per 1000 rpm)"),
                          ("pole_pairs", "the motor's pole pairs")):
            if getattr(b.motor, key) is None:
                out.append(f"set {what}")
    return out


def _si_from_data(d: Any, errors: list[str]) -> "SiSettings":
    """The `si:` section (see SiSettings): impedance targets {z0, nets | class, tol, layers}, pairs {p, n, name,
    zdiff, tol, rise_time, max_skew, source, reviewed, enabled, note}, crosstalk_max, stub_factor, step_mm; values
    with units ("50ohm", "10%", "500ps"); problems into `errors`."""
    s = SiSettings()
    if not isinstance(d, dict):
        errors.append("si: must be a mapping")
        return s
    for i, t in enumerate(d.get("impedance") or []):
        where = f"si.impedance[{i}]"
        try:
            nets = t.get("nets") or []
            if isinstance(nets, str):
                nets = [x.strip() for x in nets.split(",") if x.strip()]
            target = ImpedanceTarget(_num(t.get("z0"), f"{where}.z0", "ohm"), [str(x) for x in nets],
                                     str(t.get("class") or t.get("netclass") or "").strip(),
                                     _fraction(t.get("tol", 0.10), f"{where}.tol"),
                                     [str(x) for x in (t.get("layers") or [])])
            if not target.nets and not target.netclass:
                raise ConfigError([f"{where}: give its nets (names or patterns) or a net class"])
            if target.z0 <= 0:
                raise ConfigError([f"{where}.z0 must be above 0 Ω"])
            s.impedance.append(target)
        except (ConfigError, TypeError, ValueError, AttributeError) as e:
            errors.extend(e.messages if isinstance(e, ConfigError) else [f"{where}: {e}"])
    for i, p in enumerate(d.get("pairs") or []):
        where = f"si.pairs[{i}]"
        try:
            if not p.get("p") or not p.get("n"):
                raise ConfigError([f"{where}: needs both nets, p and n"])
            s.pairs.append(DiffPairSpec(
                str(p["p"]).strip(), str(p["n"]).strip(), str(p.get("name") or ""),
                _opt(p.get("zdiff"), f"{where}.zdiff", "ohm"), _fraction(p.get("tol", 0.10), f"{where}.tol"),
                _num(p.get("rise_time", 1e-9), f"{where}.rise_time", "s"),
                _opt(p.get("max_skew"), f"{where}.max_skew", "s"), str(p.get("source") or "manual"),
                bool(p.get("reviewed", True)), bool(p.get("enabled", True)), str(p.get("note") or "")))
        except (ConfigError, TypeError, ValueError, AttributeError) as e:
            errors.extend(e.messages if isinstance(e, ConfigError) else [f"{where}: {e}"])
    try:
        s.crosstalk_max = _fraction(d.get("crosstalk_max", 0.05), "si.crosstalk_max")
        s.stub_factor = _num(d.get("stub_factor", 3.0), "si.stub_factor")
        s.step_mm = _num(d.get("step_mm", 2.0), "si.step_mm", "mm")
    except (ConfigError, TypeError, ValueError) as e:
        errors.extend(e.messages if isinstance(e, ConfigError) else [f"si: {e}"])
    if s.step_mm <= 0:
        errors.append("si.step_mm must be above 0")
    return s


def _safety_from_data(d: Any, errors: list[str]) -> "SafetySettings":
    """The `safety:` section (see SafetySettings); numbers with units and percentages ("48V", "50%"); problems
    into `errors`, the defaults kept for what can't be read."""
    s = SafetySettings()
    if not isinstance(d, dict):
        errors.append("safety: must be a mapping")
        return s
    try:
        s.voltages = {str(k): _num(v, f"safety.voltages.{k}", "V") for k, v in (d.get("voltages") or {}).items()}
        s.coated = bool(d.get("coated", False))
        s.solder_mask = str(d.get("solder_mask", "ipc")).strip().lower()
        if s.solder_mask not in ("ipc", "bare"):
            errors.append(f"safety.solder_mask: '{d.get('solder_mask')}': use ipc (the mask counts as coating where it "
                          "covers a gap) or bare (left out)")
            s.solder_mask = "ipc"
        if d.get("clearance_mm") is not None:
            s.clearance_mm = [[_num(v, "safety.clearance_mm", "V"), _num(mm, "safety.clearance_mm", "mm")]
                              for v, mm in d.get("clearance_mm")]
        s.cap_derating = {str(k).lower(): _fraction(v, f"safety.cap_derating.{k}")
                          for k, v in (d.get("cap_derating") or {}).items()}
        s.resistor_derating = _fraction(d.get("resistor_derating", 0.5), "safety.resistor_derating")
        s.vds_derating = _fraction(d.get("vds_derating", 0.8), "safety.vds_derating")
        s.esd_mm = _num(d.get("esd_mm", 10.0), "safety.esd_mm", "mm")
        s.esd_connectors = [str(x).strip() for x in (d.get("esd_connectors") or [])]
        s.gate_drive_v = _num(d.get("gate_drive_v", 10.0), "safety.gate_drive_v", "V")
    except (ConfigError, TypeError, ValueError) as e:
        errors.extend(e.messages if isinstance(e, ConfigError) else [f"safety: {e}"])
    bad = [k for k in s.cap_derating if k not in ("ceramic", "tantalum", "electrolytic")]
    if bad:
        errors.append(f"safety.cap_derating: {', '.join(bad)}: use ceramic, tantalum, electrolytic")
    return s


def _fraction(v: Any, what: str) -> float:
    """A fraction written as a number (0.05) or a percentage ("5%")."""
    if isinstance(v, str) and v.strip().endswith("%"):
        return _num(v.strip()[:-1], what) / 100.0
    return _num(v, what)


def _pdn_from_data(d: dict, where: str) -> PdnSpec:
    """One pdn entry: {rail, name, ground, voltage, ripple, i_step, source, vrm_r, vrm_l, parts, check_to, decap_mm,
    enabled, note}; numbers with units ("3.3V", "2A", "5%", "20nH", "100MHz")."""
    rail = str(d.get("rail") or "").strip()
    if not rail:
        raise ConfigError([f"{where}: needs its rail (the supply net)"])
    parts = d.get("parts") or []
    if isinstance(parts, str):
        parts = [x.strip() for x in parts.replace(";", ",").split(",") if x.strip()]
    return PdnSpec(rail=rail, name=str(d.get("name") or ""), ground=str(d.get("ground") or "").strip(),
                   voltage=_opt(d.get("voltage"), f"{where}.voltage", "V"),
                   ripple=_fraction(d.get("ripple", 0.05), f"{where}.ripple"),
                   i_step=_opt(d.get("i_step"), f"{where}.i_step", "A"),
                   source=str(d.get("source") or "").strip(),
                   vrm_r=_num(d.get("vrm_r", 5e-3), f"{where}.vrm_r", "ohm"),
                   vrm_l=_num(d.get("vrm_l", 20e-9), f"{where}.vrm_l", "H"),
                   parts=[str(x).strip() for x in parts],
                   check_to=_num(d.get("check_to", 100e6), f"{where}.check_to", "Hz"),
                   decap_mm=_num(d.get("decap_mm", 3.0), f"{where}.decap_mm", "mm"),
                   enabled=bool(d.get("enabled", True)), note=str(d.get("note") or ""))


def pdn_problems(spec: PdnSpec) -> list[str]:
    """What makes a pdn entry unusable: a ripple outside (0, 1), a step or voltage <= 0, negative source values."""
    out = []
    if not 0 < spec.ripple < 1:
        out.append("ripple must be a fraction between 0 and 1 (e.g. 5%)")
    if spec.i_step is not None and spec.i_step <= 0:
        out.append("i_step must be above 0 A")
    if spec.voltage is not None and spec.voltage <= 0:
        out.append("voltage must be above 0 V")
    if spec.vrm_r < 0 or spec.vrm_l < 0:
        out.append("vrm_r and vrm_l can't be negative")
    return out


def _dc_load_from_data(d: dict, where: str, i: int) -> DcLoadSpec:
    """One dc_loads entry: {name, source, source_return, loads: [{pin, return, current}], max_drop, enabled, note}.
    A load may also be written "J2.2/J2.1=0.5A" (pin / return = current)."""
    loads = []
    for k, ld in enumerate(d.get("loads") or []):
        w = f"{where}.loads[{k}]"
        if isinstance(ld, str):
            ld = parse_load_text(ld, w)
        if not isinstance(ld, dict):
            raise ConfigError([f"{w}: must be a mapping {{pin, return, current}}"])
        loads.append(DcLoad(str(ld.get("pin", "")).strip(), _num(ld.get("current"), f"{w}.current", "A"),
                            str(ld.get("return", "") or "").strip()))
    return DcLoadSpec(str(d.get("name") or f"DC load {i + 1}"), str(d.get("source", "")).strip(), loads,
                      str(d.get("source_return", "") or "").strip(), _opt(d.get("max_drop"), f"{where}.max_drop", "V"),
                      bool(d.get("enabled", True)), str(d.get("note", "") or ""))


def parse_load_text(text: str, where: str = "load") -> dict:
    """A load written as text, "J2.2/J2.1=0.5A" or "J2.2=500mA" (no return): {pin, return, current}."""
    pins, _, amps = str(text).partition("=")
    if not amps.strip():
        raise ConfigError([f"{where}: write a load as PIN/RETURN=CURRENT, e.g. J2.2/J2.1=0.5A (got {text!r})"])
    pin, _, ret = pins.partition("/")
    return {"pin": pin.strip(), "return": ret.strip(), "current": amps.strip()}


def load_text(ld: DcLoad) -> str:
    """A load as text, the inverse of parse_load_text: "J2.2/J2.1=500mA"."""
    from ..components.values import format_si
    return f"{ld.pin}{'/' + ld.ret if ld.ret else ''}={format_si(ld.current, 'A')}"


def net_problems(spec: NetSpec) -> list[str]:
    """What makes a net's signal impossible: a frequency or an edge not above 0, an edge longer than the high (rise)
    or low (fall) time it sits in, a duty outside (0, 1), an amplitude or a given current not above 0, a negative
    source or load. (Before 2026-10-17 Simulate crashed on f = 0, t_r = 0 or D = 0 and gave nonsense margins on a
    duty above 1 or a negative swing.)"""
    out = []
    if not spec.freq > 0:
        out.append("freq must be above 0 Hz")
    if not spec.rise_time > 0 or (spec.fall_time is not None and not spec.fall_time > 0):
        out.append("rise_time (and fall_time) must be above 0 s")
    if not 0 < spec.duty < 1:
        out.append("duty must be between 0 and 1")
    elif spec.freq > 0 and spec.rise_time > 0:
        period = 1.0 / spec.freq
        fall = spec.fall_time if spec.fall_time is not None else spec.rise_time
        if spec.rise_time > spec.duty * period or (fall > 0 and fall > (1 - spec.duty) * period):
            # a trapezoid's edges must fit in its high and low times; said with the numbers to change
            f3 = lambda v, u: format_si(float(f"{v:.3g}"), u)       # noqa: E731
            out.append(f"the edges don't fit the signal: rise {f3(spec.rise_time, 's')} / fall {f3(fall, 's')}, but at "
                       f"{f3(spec.freq, 'Hz')} and {spec.duty * 100:g} % duty it is high for "
                       f"{f3(spec.duty * period, 's')} and low for {f3((1 - spec.duty) * period, 's')}. Shorten the rise "
                       f"/ fall time to at most that, or lower the frequency")
    if not spec.amplitude_v > 0:
        out.append("amplitude_v must be above 0 V")
    if spec.current_a is not None and not spec.current_a > 0:
        out.append("current_a must be above 0 A")
    for key in ("driver_ohms", "load_pf", "driver_nh", "driver_pf"):
        v = getattr(spec, key)
        if v is not None and v < 0:
            out.append(f"{key} must not be negative")
    if spec.load_ohms is not None and not spec.load_ohms > 0:
        out.append("load_ohms must be above 0 Ω (leave it out for no termination)")
    for key in ("sweep_from", "sweep_to"):
        v = getattr(spec, key)
        if v is not None and not v >= SWEEP_MIN_HZ:
            out.append(f"{key} must be at least {SWEEP_MIN_HZ:g} Hz (a log sweep cannot start at 0)")
    if spec.sweep_from is not None and spec.sweep_to is not None and not spec.sweep_to > spec.sweep_from:
        out.append("sweep_to must be above sweep_from")
    if spec.sweep_points is not None and not SWEEP_POINTS[0] <= spec.sweep_points <= SWEEP_POINTS[1]:
        out.append(f"sweep_points must be {SWEEP_POINTS[0]} to {SWEEP_POINTS[1]}")
    if spec.max_bandwidth is not None and not spec.max_bandwidth > 0:
        out.append("max_bandwidth must be above 0 Hz (leave it out for no limit)")
    return out


def dc_load_problems(spec: DcLoadSpec) -> list[str]:
    """What makes a DC load unusable: no source, no loads, a load without a pin or with a current <= 0, a return on
    some but not all (the ground side must balance: the source's return with every load's, or none)."""
    out = []
    if not spec.source:
        out.append("needs its source pin (source: REF.PAD)")
    if not spec.loads:
        out.append("needs at least one load (loads: [{pin, return, current}])")
    for k, ld in enumerate(spec.loads):
        if not ld.pin:
            out.append(f"load {k + 1} needs its pin")
        if not ld.current > 0:
            out.append(f"load {k + 1} ({ld.pin}) needs a current above 0")
    rets = [bool(ld.ret) for ld in spec.loads]
    if spec.loads and (bool(spec.source_return) != all(rets) or (any(rets) and not all(rets))):
        out.append("give the source's return pin and every load's return pin, or none (supply side only)")
    if spec.max_drop is not None and spec.max_drop <= 0:
        out.append("max_drop must be above 0 V")
    return out


def pin_values(data: Any, where: str) -> tuple[dict, list[str]]:
    """A `tht:` entry (the board's, or a part's) as {key: value} and its problems (analysis/pins.py)."""
    from ..analysis.pins import values_from_data
    return values_from_data(data, where)


def _radiated_limit(data: Any, errors: list[str]):
    """`analysis: radiated_limit:` a built-in MIL-STD-461G RE102 curve by name (emi/radiation.RE102_LIMITS:
    re102_ground_army, re102_aircraft_external, ...), or `{name, distance_m, points: [[f, dBµV/m], ...], bands: mil461 |
    cispr}` as an emi/radiation.RadiatedLimit (the points need not be sorted; frequencies with units, "2MHz"); None
    and errors when it can't be read."""
    from ..emi.radiation import RE102_LIMITS, RadiatedLimit
    where = "analysis.radiated_limit"
    if isinstance(data, str):
        key = data.strip().lower()
        if key not in RE102_LIMITS:
            errors.append(f"{where}: {data!r} is not a built-in limit ({', '.join(RE102_LIMITS)}); a curve of your own "
                          "is {name, distance_m, points: [[f, dBµV/m], ...]}")
            return None
        return RE102_LIMITS[key]
    if not isinstance(data, dict):
        errors.append(f"{where} must be a mapping: {{name, distance_m, points: [[f, dBµV/m], ...]}}")
        return None
    points = []
    for k, p in enumerate(data.get("points") or []):
        try:
            f = parse_value(str(p[0])) if isinstance(p[0], str) else float(p[0])
            points.append((float(f), float(p[1])))
        except (TypeError, ValueError, IndexError):
            errors.append(f"{where}.points[{k}]: [frequency, dBµV/m], e.g. [2MHz, 24]")
            return None
    points.sort()
    if len(points) < 2 or points[0][0] <= 0 or len({f for f, _ in points}) != len(points):
        errors.append(f"{where}.points: at least two different frequencies above 0")
        return None
    bands = str(data.get("bands", "mil461")).lower()
    if bands not in ("mil461", "cispr"):
        errors.append(f"{where}.bands must be mil461 or cispr")
        return None
    try:
        distance = float(data.get("distance_m", 1.0))
    except (TypeError, ValueError):
        errors.append(f"{where}.distance_m must be a number (m)")
        return None
    return RadiatedLimit(str(data.get("name") or "user radiated limit"), tuple(points), distance, bands)


def config_from_data(data: Any) -> Config:
    """Parse the settings document (the YAML as plain mappings and lists) into a Config. Every section is read
    even after an error, so ConfigError lists all problems at once; entries that are unusable are left out and
    reported, the rest is kept. Numbers may carry units and SI prefixes ("20MHz"); defaults fill what is missing."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(["settings must be a mapping at top level"])
    errors: list[str] = []
    cfg = Config()

    refs = data.get("reference_nets", cfg.reference_nets)
    cfg.reference_nets = [str(r) for r in (refs or [])]

    a = data.get("analysis") or {}
    try:
        grid = a.get("grid_mm", "auto")
        cfg.analysis = AnalysisSettings(
            grid_mm="auto" if str(grid).lower() == "auto" else _num(grid, "analysis.grid_mm"),
            margin_mm=_num(a.get("margin_mm", 10.0), "analysis.margin_mm"),
            solve_points=int(a.get("solve_points", 4)),
            max_nodes=int(a.get("max_nodes", 100_000)),
            via_plating_um=_num(a.get("via_plating_um", 25.0), "analysis.via_plating_um"),
            limit_class=str(a.get("limit_class", "B")).upper(),
            distance_m=None if a.get("distance_m") in (None, "auto") else _num(a.get("distance_m"), "analysis.distance_m"),
            ground_reflection=bool(a.get("ground_reflection", True)),
            planes=str(a.get("planes", "auto")),
            plane_capacitance=bool(a.get("plane_capacitance", True)),
            cavity_max_nodes=int(a.get("cavity_max_nodes", 40_000)),
            max_solves=int(a.get("max_solves", 10)),
            max_frequency=None if str(a.get("max_frequency", "auto")).lower() == "auto"
            else _num(a.get("max_frequency"), "analysis.max_frequency", "Hz"),
            driver_ohms=_num(a.get("driver_ohms", 25.0), "analysis.driver_ohms", "ohm"),
            load_pf=_num(a.get("load_pf", 5.0), "analysis.load_pf", "pF"),
            threads=_threads(a.get("threads", "auto"), errors),
            gpu=_gpu(a.get("gpu", "auto"), errors),
            sparse=_sparse(a.get("sparse"), errors),
            conducted=str(a.get("conducted", "cispr32")).lower().replace(" ", "").replace("-", ""),
            conducted_class=str(a.get("conducted_class", "B")),
            # "G", "H", "461H" or "MIL-STD-461H" all name the revision
            mil461=str(a.get("mil461", "G")).strip().upper().removeprefix("MIL-STD-").removeprefix("461"),
            ce101=str(a.get("ce101", "none")).lower(),
            ce101_source_v=None if a.get("ce101_source_v") in (None, "auto")
            else _num(a.get("ce101_source_v"), "analysis.ce101_source_v", "V"),
            ce101_load_a=None if a.get("ce101_load_a") in (None, "auto")
            else _num(a.get("ce101_load_a"), "analysis.ce101_load_a", "A"),
        )
        if cfg.analysis.conducted not in CONDUCTED_STANDARDS:
            errors.append(f"analysis.conducted must be one of {', '.join(CONDUCTED_STANDARDS)}")
        from ..emi.conducted import CE101_PLATFORMS, MIL461_REVISIONS
        if cfg.analysis.mil461 not in MIL461_REVISIONS:
            errors.append(f"analysis.mil461 must be one of {', '.join(MIL461_REVISIONS)} (MIL-STD-461G or 461H)")
        if cfg.analysis.ce101 not in CE101_PLATFORMS:
            errors.append(f"analysis.ce101 must be one of {', '.join(CE101_PLATFORMS)}")
        elif cfg.analysis.ce101 != "none" and cfg.analysis.conducted == "none":
            errors.append("analysis.ce101 needs a conducted model at the input: set analysis.conducted (mil461)")
        if cfg.analysis.planes not in ("auto", "nearest"):
            errors.append("analysis.planes must be auto or nearest")
        if cfg.analysis.max_frequency is not None and not cfg.analysis.max_frequency > 0:
            errors.append("analysis.max_frequency must be auto or a frequency above 0 (e.g. 1GHz)")

        if cfg.analysis.limit_class not in ("A", "B"):
            errors.append("analysis.limit_class must be A or B")
        if a.get("radiated_limit") is not None:
            cfg.analysis.radiated_limit = _radiated_limit(a.get("radiated_limit"), errors)
    except ConfigError as e:
        errors.extend(e.messages)

    r = data.get("rules") or {}
    try:
        cfg.rules = RuleSettings(
            stitch_distance_mm=_num(r.get("stitch_distance_mm", 2.0), "rules.stitch_distance_mm"),
            decap_distance_mm=_num(r.get("decap_distance_mm", 5.0), "rules.decap_distance_mm"),
            edge_factor=_num(r.get("edge_factor", 3.0), "rules.edge_factor"),
            min_gap_mm=_num(r.get("min_gap_mm", 0.1), "rules.min_gap_mm"),
            detour_warning=_num(r.get("detour_warning", 1.25), "rules.detour_warning"),
            detour_error=_num(r.get("detour_error", 2.0), "rules.detour_error"),
            emi_margin_db=_num(r.get("emi_margin_db", 6.0), "rules.emi_margin_db"),
            resonance_db=_num(r.get("resonance_db", 10.0), "rules.resonance_db"),
            overshoot_warning=_num(r.get("overshoot_warning", 0.25), "rules.overshoot_warning"),
            overshoot_error=_num(r.get("overshoot_error", 0.5), "rules.overshoot_error"),
            current_density_warning=_num(r.get("current_density_warning", 50.0), "rules.current_density_warning"),
            current_density_error=_num(r.get("current_density_error", 100.0), "rules.current_density_error"),
            via_load_warning=_num(r.get("via_load_warning", 100.0), "rules.via_load_warning"),
            via_load_error=_num(r.get("via_load_error", 150.0), "rules.via_load_error"),
            severity={str(k): str(v) for k, v in (r.get("severity") or {}).items()},
        )
        for k, v in cfg.rules.severity.items():
            if v not in ("error", "warning", "info", "off"):
                errors.append(f"rules.severity.{k}: must be error|warning|info|off, not {v!r}")
    except ConfigError as e:
        errors.extend(e.messages)

    t = data.get("thermal") or {}
    try:
        cfg.thermal = ThermalSettings(
            ambient_c=_num(t.get("ambient_c", 25.0), "thermal.ambient_c", "C"),
            orientation=str(t.get("orientation", "horizontal")).lower(),
            convection=str(t.get("convection", "natural")).lower(),
            airspeed=_num(t.get("airspeed", 1.0), "thermal.airspeed"),
            h_top=_num(t.get("h_top", 10.0), "thermal.h_top"),
            h_bottom=_num(t.get("h_bottom", 5.0), "thermal.h_bottom"),
            radiation=bool(t.get("radiation", True)),
            quality=str(t.get("quality", "normal")).lower(),
            warn_c=_num(t.get("warn_c", 85.0), "thermal.warn_c", "C"),
            max_c=_num(t.get("max_c", 105.0), "thermal.max_c", "C"),
            theta_jb={str(k): _num(v, f"thermal.theta_jb.{k}") for k, v in (t.get("theta_jb") or {}).items()},
            capacity_c=(_num(t["capacity_c"], "thermal.capacity_c", "C") if t.get("capacity_c") is not None
                        else None),
            electrothermal=bool(t.get("electrothermal", False)),
            electrothermal_rounds=int(_num(t.get("electrothermal_rounds", 3), "thermal.electrothermal_rounds")),
            electrothermal_tolerance=_share(t.get("electrothermal_tolerance", 0.01),
                                            "thermal.electrothermal_tolerance"))
        if cfg.thermal.orientation not in ("horizontal", "vertical"):
            errors.append("thermal.orientation must be horizontal or vertical")
        if cfg.thermal.convection not in ("natural", "forced", "fixed"):
            errors.append("thermal.convection must be natural, forced or fixed")
        if cfg.thermal.quality not in ("draft", "normal", "fine"):
            errors.append("thermal.quality must be draft, normal or fine")
        if not 1 <= cfg.thermal.electrothermal_rounds <= 10:
            errors.append("thermal.electrothermal_rounds must be 1 to 10 (the most rounds before it stops)")
        if not 0.001 <= cfg.thermal.electrothermal_tolerance <= 0.5:
            errors.append("thermal.electrothermal_tolerance must be 0.1 % to 50 % (e.g. 1% or 0.01)")
    except ConfigError as e:
        errors.extend(e.messages)

    r = data.get("report") or {}
    cfg.report = ReportSettings(**{k: str(r.get(k) or "") for k in ("title", "company", "author", "document")})
    d = data.get("drawing") or {}
    cfg.drawing = DrawingSettings(str(d.get("issues_layer", "User.9")), str(d.get("suggest_layer", "User.8")),
                                  str(d.get("current_layer", "User.7")), str(d.get("return_layer", "User.6")))
    cfg.safety = _safety_from_data(data.get("safety") or {}, errors)
    cfg.si = _si_from_data(data.get("si") or {}, errors)

    for i, n in enumerate(data.get("nets") or []):
        where = f"nets[{i}]"
        if not isinstance(n, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            name = n.get("name")
            driver = n.get("driver")
            # `receivers` (a list) or, as in early files, one `receiver`
            receivers = n.get("receivers") or ([n["receiver"]] if n.get("receiver") else [])
            if not name or not driver or not receivers:
                errors.append(f"{where}: needs name, driver and receivers")
                continue
            if isinstance(receivers, str):
                receivers = [receivers]
            spec = NetSpec(
                name=str(name), driver=str(driver), receivers=[str(x) for x in receivers],
                freq=_num(n.get("freq"), f"{where}.freq", "Hz"),
                rise_time=_num(n.get("rise_time"), f"{where}.rise_time", "s"),
                amplitude_v=_num(n.get("amplitude_v", 3.3), f"{where}.amplitude_v", "V"),
                duty=_num(n.get("duty", 0.5), f"{where}.duty"),
                current_a=None if n.get("current_a") is None else _num(n.get("current_a"), f"{where}.current_a", "A"),
                driver_ohms=None if n.get("driver_ohms") is None else _num(n.get("driver_ohms"), f"{where}.driver_ohms", "ohm"),
                load_pf=None if n.get("load_pf") is None else _num(n.get("load_pf"), f"{where}.load_pf", "pF"),
                fall_time=None if n.get("fall_time") is None else _num(n.get("fall_time"), f"{where}.fall_time", "s"),
                driver_nh=None if n.get("driver_nh") is None else _num(n.get("driver_nh"), f"{where}.driver_nh", "nH"),
                driver_pf=None if n.get("driver_pf") is None else _num(n.get("driver_pf"), f"{where}.driver_pf", "pF"),
                load_ohms=None if n.get("load_ohms") is None else _num(n.get("load_ohms"), f"{where}.load_ohms", "ohm"),
                sweep_from=None if n.get("sweep_from") is None else _num(n.get("sweep_from"), f"{where}.sweep_from",
                                                                         "Hz"),
                sweep_to=None if n.get("sweep_to") is None else _num(n.get("sweep_to"), f"{where}.sweep_to", "Hz"),
                sweep_points=None if n.get("sweep_points") is None else int(_num(n.get("sweep_points"),
                                                                                 f"{where}.sweep_points")),
                max_bandwidth=None if n.get("max_bandwidth") is None else _num(n.get("max_bandwidth"),
                                                                               f"{where}.max_bandwidth", "Hz"),
                through=[str(x) for x in ([n["through"]] if isinstance(n.get("through"), str)
                                          else (n.get("through") or []))],
                driver_return=n.get("driver_return"), receiver_return=n.get("receiver_return"),
                # a detected entry is unreviewed until the user marks it; a manual one is reviewed by definition
                source=str(n.get("source", "manual")), reviewed=bool(n.get("reviewed", n.get("source") != "auto")),
                enabled=bool(n.get("enabled", True)),
                signal_class=str(n.get("class", "") or ""),
            )
            problems = net_problems(spec)
            errors.extend(f"{where} ({spec.name}): {m}" for m in problems)
            if not problems:
                cfg.nets.append(spec)
        except ConfigError as e:
            errors.extend(e.messages)
        except KeyError:
            errors.append(f"{where}: missing freq or rise_time")

    for i, c in enumerate(data.get("converters") or []):
        where = f"converters[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            spec = ConverterSpec(
                name=str(c.get("name") or c.get("switch_node") or f"converter {i + 1}"),
                rail=str(c.get("rail") or ""), ground=str(c.get("ground") or ""),
                switch_node=str(c["switch_node"]) if c.get("switch_node") else None,
                high_side=_refs(c.get("high_side")), low_side=_refs(c.get("low_side")), switch=_refs(c.get("switch")),
                topology=str(c.get("topology", "buck")).lower(),
                f_sw=_num(c.get("f_sw", 500e3), f"{where}.f_sw", "Hz"),
                i_out=_num(c.get("i_out", 1.0), f"{where}.i_out", "A"),
                v_in=_opt(c.get("v_in"), f"{where}.v_in", "V"), v_out=_opt(c.get("v_out"), f"{where}.v_out", "V"),
                duty=_opt(c.get("duty"), f"{where}.duty"),
                rise_time=_num(c.get("rise_time", 10e-9), f"{where}.rise_time", "s"),
                c_oss=_opt(c.get("c_oss"), f"{where}.c_oss", "F"),
                q_oss=_opt(c.get("q_oss"), f"{where}.q_oss", "C"),
                r_damp=_opt(c.get("r_damp"), f"{where}.r_damp", "ohm"),
                c_stray=_opt(c.get("c_stray"), f"{where}.c_stray", "F"),
                input_connector=_refs(c.get("input_connector")),
                output_connector=_refs(c.get("output_connector")),
                spread=_opt(c.get("spread"), f"{where}.spread"),
                spread_mode=str(c.get("spread_mode", "random")).lower(),
                spread_rate=_opt(c.get("spread_rate"), f"{where}.spread_rate", "Hz"),
                conducted=(str(c["conducted"]).lower().replace(" ", "").replace("-", "") if c.get("conducted")
                           else None),
                interleave=str(c.get("interleave") or "").strip(),
                phase_deg=_opt(c.get("phase_deg"), f"{where}.phase_deg"),
                source=str(c.get("source", "manual")), reviewed=bool(c.get("reviewed", c.get("source") != "auto")),
                enabled=bool(c.get("enabled", True)),
                note=str(c.get("note", "") or ""),
                rds_on=_opt(c.get("rds_on"), f"{where}.rds_on", "ohm"),
                rds_on_low=_opt(c.get("rds_on_low"), f"{where}.rds_on_low", "ohm"),
                dcr=_opt(c.get("dcr"), f"{where}.dcr", "ohm"),
                diode_vf=_opt(c.get("diode_vf"), f"{where}.diode_vf", "V"),
                # dead_time: both edges; dead_rise / dead_fall: one edge (they win over dead_time)
                dead_rise=_opt(c.get("dead_rise", c.get("dead_time")), f"{where}.dead_rise", "s"),
                dead_fall=_opt(c.get("dead_fall", c.get("dead_time")), f"{where}.dead_fall", "s"),
                v_sd=_opt(c.get("v_sd"), f"{where}.v_sd", "V"),
                q_rr=_opt(c.get("q_rr"), f"{where}.q_rr", "C"),
                # "dead_time" in a list of 2026-10-20 (one dead time for both edges) is both edges now
                from_schematic=list(dict.fromkeys(k for r in _refs(c.get("from_schematic"))
                                                  for k in (("dead_rise", "dead_fall") if r == "dead_time" else (r,)))),
                loop={str(k): (v if isinstance(v, (int, float)) else str(v)) for k, v in (c.get("loop") or {}).items()},
                **_converter_snubber(c.get("snubber"), where),
            )
        except ConfigError as e:
            errors.extend(e.messages)
            continue
        problems = []
        if not spec.rail or not spec.ground:
            problems.append("needs rail and ground nets")
        if spec.topology not in TOPOLOGIES:
            problems.append(f"topology must be one of {', '.join(TOPOLOGIES)}")
        if not spec.switch and not (spec.high_side and spec.low_side):
            problems.append("needs high_side and low_side parts (discrete switches, catch diode) or switch "
                            "(an integrated power stage)")
        if (spec.high_side or spec.low_side) and not spec.switch_node:
            problems.append("high_side/low_side need the switch_node net")
        for key in ("input_connector", "output_connector"):
            if getattr(spec, key) and len(getattr(spec, key)) != 2:
                problems.append(f"{key} needs two pins: [plus, minus]")
        if spec.conducted is not None and spec.conducted not in CONDUCTED_STANDARDS:
            problems.append(f"conducted must be one of {', '.join(CONDUCTED_STANDARDS)}")
        if spec.f_sw <= 0 or spec.rise_time <= 0 or spec.i_out <= 0:
            problems.append("f_sw, rise_time and i_out must be positive")
        if spec.duty is not None and not 0 < spec.duty < 1:
            problems.append("duty must be between 0 and 1")
        dead_sum = (spec.dead_rise or 0.0) + (spec.dead_fall or 0.0)
        if any(v is not None and v < 0 for v in (spec.dead_rise, spec.dead_fall, spec.v_sd, spec.q_rr)):
            problems.append("dead_rise, dead_fall (dead_time), v_sd and q_rr must not be negative")
        elif spec.f_sw > 0 and dead_sum * spec.f_sw >= 0.5:
            problems.append(f"the dead times ({dead_sum * 1e9:g} ns per period together) are half of the "
                            f"{1e6 / spec.f_sw:g} µs period or more: check their unit (ns)")
        # a buck steps down, a boost up: given both voltages the wrong way round the stage would be solved at a made-up
        # duty of ½ (ConverterSpec.pulse), a buck drawing more input current than it delivers
        if spec.v_in and spec.v_out and spec.topology in ("buck", "boost") and \
                (spec.v_out >= spec.v_in) == (spec.topology == "buck"):
            problems.append(f"a {spec.topology} needs v_out {'below' if spec.topology == 'buck' else 'above'} v_in "
                            f"(v_in {spec.v_in:g} V, v_out {spec.v_out:g} V): check the voltages or the topology")
        if spec.spread is not None and not 0 <= spec.spread <= 1:
            problems.append("spread is a fraction of f_sw between 0 and 1, e.g. 0.2 for +20 %")
        if spec.spread_mode not in ("random", "triangle"):
            problems.append("spread_mode must be random or triangle")
        if spec.spread and spec.spread_mode == "triangle" and not spec.spread_rate:
            problems.append("spread_mode triangle needs spread_rate (the modulation frequency)")
        if spec.phase_deg is not None and not spec.interleave:
            problems.append("phase_deg needs interleave (the name of the group of interleaved stages)")
        errors.extend(f"{where} ({spec.name}): {m}" for m in problems)
        if not problems:
            cfg.converters.append(spec)

    for i, b in enumerate(data.get("bridges") or []):
        where = f"bridges[{i}]"
        if not isinstance(b, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            bridge = _bridge_from_data(b, where, i)
        except ConfigError as e:
            errors.extend(e.messages)
            continue
        problems = _bridge_problems(bridge)
        errors.extend(f"{where} ({bridge.name}): {m}" for m in problems)
        if not problems:
            cfg.bridges.append(bridge)
    for i, d in enumerate(data.get("dc_loads") or []):
        where = f"dc_loads[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            spec = _dc_load_from_data(d, where, i)
        except ConfigError as e:
            errors.extend(e.messages)
            continue
        problems = dc_load_problems(spec)
        errors.extend(f"{where} ({spec.name}): {m}" for m in problems)
        if not problems:
            cfg.dc_loads.append(spec)
    for i, d in enumerate(data.get("pdn") or []):
        where = f"pdn[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            spec = _pdn_from_data(d, where)
        except ConfigError as e:
            errors.extend(e.messages)
            continue
        problems = pdn_problems(spec)
        errors.extend(f"{where} ({spec.label}): {m}" for m in problems)
        if not problems:
            cfg.pdn.append(spec)
    # converters, bridges, the bridges' legs and the DC loads share one namespace: a name selects a view, a stage or a
    # CLI target
    names = ([c.name for c in cfg.converters] + [n for b in cfg.bridges for n in [b.name, *map(b.leg_name, b.legs)]]
             + [d.name for d in cfg.dc_loads])
    for dup in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"the name {dup!r} is used by more than one converter, bridge, leg or DC load")

    for ref, c in (data.get("components") or {}).items():
        if not isinstance(c, dict):
            errors.append(f"components.{ref}: must be a mapping")
            continue
        try:
            model = ComponentModel.from_dict(c)
        except (ValueError, TypeError) as e:
            errors.append(f"components.{ref}: {e}")
            continue
        tht, problems = pin_values(c.get("tht"), f"components.{ref}.tht")
        errors.extend(problems)
        cfg.components[str(ref)] = ComponentEntry(str(ref), str(c.get("value", "")), str(c.get("footprint", "")),
                                                  model, bool(c.get("removed", False)), tht)
    # the board's through-hole pin defaults (analysis/pins.py: a part's GT field and its own entry come first)
    cfg.tht, problems = pin_values(data.get("tht"), "tht")
    errors.extend(problems)

    for i, s in enumerate(data.get("stack") or []):
        where = f"stack[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        try:
            sb = StackBoard(str(s.get("name") or f"board{i + 2}"), str(s.get("file") or ""),
                            _mm(s.get("gap"), f"{where}.gap"),
                            [[str(a) for a in pair] for pair in (s.get("connectors") or [])],
                            str(s.get("side", "above")), str(s.get("flip", "auto")))
        except ConfigError as e:
            errors.extend(e.messages)
            continue
        problems = []
        if not sb.file:
            problems.append("file is missing (the other board's .kicad_pcb)")
        if sb.gap <= 0:
            problems.append("gap must be positive (mm between the boards' facing surfaces)")
        if not sb.connectors or any(len(p) != 2 for p in sb.connectors):
            problems.append("connectors: a list of [this board's ref, the other board's ref] pairs")
        if sb.side not in STACK_SIDES:
            problems.append(f"side must be {' or '.join(STACK_SIDES)}")
        if sb.flip not in STACK_FLIPS:
            problems.append(f"flip must be {', '.join(STACK_FLIPS)}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sb.name):
            problems.append("name: letters, digits, _ or - (it prefixes the board's parts and nets)")
        errors.extend(f"{where}: {m}" for m in problems)
        if not problems:
            cfg.stack.append(sb)
    if len({s.name for s in cfg.stack}) != len(cfg.stack):
        errors.append("stack: every board needs its own name")

    v = data.get("victims") or {}
    try:
        cfg.victims = VictimSettings(
            [str(r) for r in (v.get("parts") or [])], [str(n) for n in (v.get("nets") or [])],
            bool(v.get("enabled", True)), str(v.get("reference") or ""),
            _num(v.get("shift_warning_v", 0.3), "victims.shift_warning_v", "V"),
            _num(v.get("induced_warning_v", 0.3), "victims.induced_warning_v", "V"),
            _num(v.get("injected_warning_ma", 1.0), "victims.injected_warning_ma"),
            _num(v.get("drive_ohms", 50.0), "victims.drive_ohms", "Ω"),
            _num(v.get("input_pf", 5.0), "victims.input_pf", "pF"))
    except ConfigError as e:
        errors.extend(e.messages)

    for w in data.get("waivers") or []:
        if isinstance(w, dict) and w.get("fingerprint"):
            cfg.waivers.append(Waiver(str(w["fingerprint"]), str(w.get("reason", ""))))
        else:
            errors.append("waivers: each entry needs a fingerprint")

    for k, m in enumerate(data.get("modifications") or [], 1):
        try:
            if not isinstance(m, dict):
                raise ModificationError(f"modifications[{k}]: must be a mapping")
            cfg.modifications.append(Modification.from_dict(m, f"modifications[{k}]"))
        except ModificationError as e:
            errors.append(str(e))

    if errors:
        raise ConfigError(errors)
    return cfg


# --------------------------------------------------------------------------- document I/O
class SettingsFile:
    """Round-trip YAML document plus the parsed Config."""

    def __init__(self, path: Path, doc: CommentedMap):
        """A settings file at `path` with its YAML document `doc` (parsed now: ConfigError if it is invalid)."""
        self.path = Path(path)
        self.doc = doc
        self.config = config_from_data(doc)

    @property
    def config(self) -> Config:
        """The parsed settings (made again from the document after every change through the set_ methods)."""
        return self._config

    @config.setter
    def config(self, cfg: Config):
        """Set the parsed settings, giving them the board's folder (base_dir)."""
        # a stack's other boards are found relative to the board's folder: the settings file's, or for one in the
        # board's groundtruth folder, the folder above it (stack.file was written next to the board before that)
        folder = self.path.parent
        cfg.base_dir = str(folder.parent if folder.name == PROJECT_DIR else folder)
        self._config = cfg

    @classmethod
    def load(cls, path: Union[str, Path]) -> "SettingsFile":
        """Read a settings file. ConfigError for an unreadable file, invalid YAML or invalid settings."""
        path = Path(path)
        try:
            doc = _yaml().load(path.read_text(encoding="utf-8")) or CommentedMap()
        except OSError as e:
            raise ConfigError([f"cannot read {path}: {e}"]) from e
        except Exception as e:  # ruamel parser errors
            raise ConfigError([f"{path}: invalid YAML: {e}"]) from e
        return cls(path, doc)

    @classmethod
    def new(cls, path: Union[str, Path], board: Board) -> "SettingsFile":
        """A new settings file for `board` (not written yet: save()): the defaults of every section written out so
        the user sees what can be set, a commented example before each list, the reference nets guessed from the
        net names, and every part with its suggested model (merge_board)."""
        doc = CommentedMap()
        doc["version"] = SCHEMA_VERSION
        doc["reference_nets"] = CommentedSeq(guess_reference_nets(board))
        doc["reference_nets"].fa.set_flow_style()
        doc["analysis"] = CommentedMap(grid_mm="auto", margin_mm=10.0, solve_points=4, limit_class="B",
                                       distance_m="auto", ground_reflection=True,
                                       planes="auto", plane_capacitance=True, cavity_max_nodes=40000, max_solves=10, max_frequency="auto",
                                       driver_ohms=25.0, load_pf=5.0, threads="auto", gpu="auto", conducted="cispr32",
                                       conducted_class="B", via_plating_um=25.0)
        doc["rules"] = CommentedMap(stitch_distance_mm=2.0, decap_distance_mm=5.0, edge_factor=3.0, min_gap_mm=0.1,
                                    detour_warning=1.25, detour_error=2.0, emi_margin_db=6.0, resonance_db=10.0,
                                    overshoot_warning=0.25, overshoot_error=0.5, current_density_warning=50.0,
                                    current_density_error=100.0, via_load_warning=100.0, via_load_error=150.0)
        doc["nets"] = CommentedSeq()
        doc["converters"] = CommentedSeq()
        doc["bridges"] = CommentedSeq()
        doc["components"] = CommentedMap()
        doc["waivers"] = CommentedSeq()
        doc["modifications"] = CommentedSeq()
        doc.yaml_set_comment_before_after_key(
            "modifications", before="What-if edits for `groundtruth compare` / `--what-if` (plain check/simulate ignore them), e.g.\n"
                                    "  - {type: via, at: [121.0, 122.6], net: GND, size: 0.6, drill: 0.3, enabled: true}")
        doc.yaml_set_start_comment(HEADER.format(board=Path(board.source).name))
        doc.yaml_set_comment_before_after_key(
            "converters", before="Switching power stages (`groundtruth detect --write` suggests them), e.g.\n"
                                 "  - {name: buck 5V, topology: buck, rail: VIN, switch_node: SW, ground: GND, high_side: Q1,\n"
                                 "     low_side: Q2, f_sw: 400kHz, v_in: 24, v_out: 5, i_out: 3, rise_time: 5ns, c_oss: 1nF,\n"
                                 "     input_connector: [J1.1, J1.2], output_connector: [J2.1, J2.2]}\n"
                                 "  integrated converter IC: switch: U5 instead of high_side/low_side/switch_node\n"
                                 "  a synchronous rectifier FET: dead_time (both edges; dead_rise / dead_fall one each,\n"
                                 "  or the controller's GT field dead:20ns), v_sd (its reverse conduction), q_rr\n"
                                 "  (a silicon body diode's recovery)\n"
                                 "  output_connector: the load's + and - pins (DC output path and ground return)")
        doc.yaml_set_comment_before_after_key(
            "bridges", before="Motor bridges (`groundtruth detect --write` finds the legs; the operating point and the "
                              "motor are yours), e.g.\n"
                              "  - name: bridge J1\n"
                              "    rail: VBUS\n"
                              "    ground: GND\n"
                              "    legs:\n"
                              "      - {phase: A, switch_node: PH_A, high_side: [Q1], low_side: [Q2], output: J1.A}\n"
                              "    input_connector: [J5.1, J5.2]\n"
                              "    v_bus: 48\n"
                              "    f_pwm: 20kHz\n"
                              "    i_phase: 100\n"
                              "    mode: motoring\n"
                              "    speed_rpm: 3000\n"
                              "    motor: {connection: star, r_ll: 20m, l_ll: 40uH, ke_ll: 5, pole_pairs: 7}")
        doc.yaml_set_comment_before_after_key(
            "nets", before="Nets to analyse (manual list), e.g.\n"
                           "  - {name: /SPI_SCK, driver: U1.12, receivers: [U3.5], freq: 20MHz, rise_time: 2ns, amplitude_v: 3.3}")
        sc = cls(path, doc)
        sc.merge_board(board)
        return sc

    def merge_board(self, board: Board) -> dict[str, list[str]]:
        """Add new parts with suggested models, flag removed ones, refresh untouched suggestions."""
        comps = self.doc.get("components")
        if comps is None:
            comps = self.doc["components"] = CommentedMap()
        added, updated, removed = [], [], []
        present = set()
        # Three cases per part: new (added with its suggested model); changed value or footprint (its model is
        # replaced only if it was still the untouched suggestion for the old part: the user's edits are kept,
        # with a note to check them); gone from the board (marked removed, not deleted).
        for fp in sorted(board.footprints, key=lambda f: _natural_key(f.reference)):
            ref = fp.reference
            if not ref or ref in present:
                continue
            present.add(ref)
            suggestion = suggest_model(fp)
            if ref not in comps:
                comps[ref] = _flow({"value": fp.value, "footprint": fp.footprint_name, **suggestion.to_dict()})
                added.append(ref)
                continue
            entry = comps[ref]
            old_value = str(entry.get("value", ""))
            old_fp = str(entry.get("footprint", ""))
            if old_value != fp.value or old_fp != fp.footprint_name or entry.get("removed"):
                # Was the stored model still the untouched suggestion for the stored part?
                stale = _suggested_for(fp, old_value, old_fp).to_dict()
                current = {k: v for k, v in entry.items() if k not in ("value", "footprint", "removed", "tht")}
                untouched = _same_model(stale, current)
                entry["value"] = fp.value
                entry["footprint"] = fp.footprint_name
                entry.pop("removed", None)
                if untouched:
                    for k in list(current):
                        entry.pop(k, None)
                    for k, v in suggestion.to_dict().items():
                        entry[k] = v
                elif old_value != fp.value:
                    entry["note"] = f"value changed from {old_value!r}; check model"
                updated.append(ref)
            # The symbol says the part is an op-amp stage (GT amp_in / amp_out labels, or a Gain field) while its
            # model is still the default 'open', from before it said so: it becomes that amplifier. (refboard's
            # U301 / U305 got their labels after the settings were made, then a new value: 'open' with "check model",
            # and the ADC paths could not pass them.)
            if entry.get("model") == "open" and suggestion.kind == "amplifier":
                for k in [k for k in entry if k not in ("value", "footprint", "tht")]:
                    entry.pop(k, None)
                for k, v in suggestion.to_dict().items():
                    entry[k] = v
                if ref not in updated:
                    updated.append(ref)
        for ref in list(comps):
            if ref not in present and not comps[ref].get("removed"):
                comps[ref]["removed"] = True
                removed.append(ref)
        self.config = config_from_data(self.doc)
        synced = self.sync_fields(board)
        return {"added": added, "updated": updated, "removed": removed,
                "schematic": synced["converters"] + synced["bridges"] + synced["parts"]}

    def sync_fields(self, board: Board) -> dict[str, list[str]]:
        """Take the parts' schematic fields into the stages and part models (components/fields.apply_fields) and
        write what changed. Returns apply_fields' result."""
        from ..components.fields import apply_fields
        cfg = self.config
        done = apply_fields(cfg, board)
        if done["converters"]:
            self.set_converters(cfg.converters)
        if done["bridges"]:
            self.set_bridges(cfg.bridges)
        if done["parts"]:
            comps = self.doc["components"]
            for ref in done["parts"]:
                entry = comps[ref]
                keep = {k: entry[k] for k in ("value", "footprint", "tht") if k in entry}
                comps[ref] = _flow({**keep, **cfg.components[ref].model.to_dict()})
            self.config = config_from_data(self.doc)
        return done

    def _replace(self, key: str, value) -> None:
        """Set the document's top-level `key` to `value` only if the settings with it still parse: the new
        document is parsed first (a shallow copy with the new entry), so an invalid entry raises ConfigError and
        leaves both the document and the config as they were. (Writing first left an invalid entry in the
        document: the tab restored its own list, but every later change failed on the stale entry.)"""
        probe = dict(self.doc)
        probe[key] = value
        config = config_from_data(probe)
        self.doc[key] = value
        self.config = config

    def set_component(self, reference: str, model: ComponentModel):
        """Set one part's model (its value and footprint entries kept); ConfigError leaves the settings as they
        were."""
        comps = self.doc["components"]
        entry = comps.get(reference) or _flow({})
        keep = {k: entry[k] for k in ("value", "footprint") if k in entry}
        new = _flow({**keep, **model.to_dict(), **({"tht": entry["tht"]} if "tht" in entry else {})})
        probe = dict(self.doc)
        probe["components"] = {**comps, reference: new}
        config = config_from_data(probe)
        comps[reference] = new
        self.config = config

    def set_component_pins(self, reference: str, values: dict):
        """Set one part's through-hole pin values ({key: value}, analysis/pins.py; empty: removed, so the GT field
        and the board's defaults apply); ConfigError leaves the settings as they were."""
        from ..analysis.pins import to_data
        comps = self.doc["components"]
        entry = comps.get(reference)
        if entry is None:
            raise ConfigError([f"{reference} is not in the settings' parts"])
        new = _flow({k: v for k, v in entry.items() if k != "tht"})
        if values:
            new["tht"] = _flow(to_data(values))
        probe = dict(self.doc)
        probe["components"] = {**comps, reference: new}
        config = config_from_data(probe)
        comps[reference] = new
        self.config = config

    def set_pin_defaults(self, values: dict):
        """Set the board's through-hole pin defaults (`tht:`; empty: removed, the built-in values apply)."""
        from ..analysis.pins import to_data
        if values:
            self._replace("tht", _flow(to_data(values)))
        elif "tht" in self.doc:
            probe = {k: v for k, v in self.doc.items() if k != "tht"}
            config = config_from_data(probe)
            del self.doc["tht"]
            self.config = config

    def set_nets(self, nets: list[NetSpec]):
        """Replace the `nets:` list. Each net is one flow-style line; values with units ("20MHz", "2ns"), optional
        keys only when set, so a hand-written file stays as short as the user wrote it. An invalid list raises
        ConfigError and leaves the settings as they were (_replace)."""
        seq = CommentedSeq()
        for n in nets:
            d = {"name": n.name, "driver": n.driver, "receivers": list(n.receivers), "freq": format_si(n.freq, "Hz"),
                 "rise_time": format_si(n.rise_time, "s"), "amplitude_v": n.amplitude_v}
            if n.duty != 0.5:
                d["duty"] = n.duty
            for key in ("current_a", "driver_ohms", "load_pf", "driver_nh", "driver_pf", "load_ohms"):
                if getattr(n, key) is not None:
                    d[key] = getattr(n, key)
            if n.fall_time is not None:
                d["fall_time"] = format_si(n.fall_time, "s")
            for key in ("sweep_from", "sweep_to", "max_bandwidth"):   # with their unit ("1Hz", "10MHz")
                if getattr(n, key) is not None:
                    d[key] = format_si(getattr(n, key), "Hz")
            if n.sweep_points is not None:
                d["sweep_points"] = n.sweep_points
            if n.through:
                d["through"] = list(n.through)
            for key in ("driver_return", "receiver_return"):
                if getattr(n, key):
                    d[key] = getattr(n, key)
            if n.source != "manual":
                d["source"] = n.source
                d["reviewed"] = n.reviewed
            if not n.enabled:
                d["enabled"] = False
            if n.signal_class:
                d["class"] = n.signal_class
            seq.append(_flow(d))
        self._replace("nets", seq)

    def set_converters(self, specs: list[ConverterSpec]):
        """Replace the `converters:` list, one flow-style line per stage (optional keys only when set, values with
        units). An invalid list raises ConfigError and leaves the settings as they were (_replace)."""
        seq = CommentedSeq()
        for c in specs:
            d: dict = {"name": c.name, "topology": c.topology, "rail": c.rail}
            if c.switch_node:
                d["switch_node"] = c.switch_node
            d["ground"] = c.ground
            for key in ("high_side", "low_side", "switch"):
                refs = getattr(c, key)
                if refs:
                    d[key] = refs[0] if len(refs) == 1 else CommentedSeq(refs)
            d["f_sw"] = format_si(c.f_sw, "Hz")
            for key in ("v_in", "v_out"):
                if getattr(c, key) is not None:
                    d[key] = getattr(c, key)
            d["i_out"] = c.i_out
            if c.duty is not None:
                d["duty"] = c.duty
            d["rise_time"] = format_si(c.rise_time, "s")
            if c.c_oss is not None:
                d["c_oss"] = format_si(c.c_oss, "F")
            if c.q_oss is not None:
                d["q_oss"] = format_si(c.q_oss, "C")
            if c.r_damp is not None:
                d["r_damp"] = c.r_damp
            if c.c_stray is not None:
                d["c_stray"] = format_si(c.c_stray, "F")
            for key, _unit in (("rds_on", "Ω"), ("rds_on_low", "Ω"), ("dcr", "Ω")):
                if getattr(c, key) is not None:
                    d[key] = format_si(getattr(c, key), "")
            if c.diode_vf is not None:
                d["diode_vf"] = c.diode_vf
            if c.dead_rise is not None and c.dead_rise == c.dead_fall:
                d["dead_time"] = format_si(c.dead_rise, "s")           # both edges alike: the short form
            else:
                for key in ("dead_rise", "dead_fall"):
                    if getattr(c, key) is not None:
                        d[key] = format_si(getattr(c, key), "s")
            if c.v_sd is not None:
                d["v_sd"] = c.v_sd
            if c.q_rr is not None:
                d["q_rr"] = format_si(c.q_rr, "C")
            if c.snubber:
                d["snubber"] = list(c.snubber)
            elif c.snubber_c and c.snubber_r:
                d["snubber"] = {"C": format_si(c.snubber_c, "F"), "R": format_si(c.snubber_r, "")}
            for key in ("input_connector", "output_connector"):
                if getattr(c, key):
                    d[key] = list(getattr(c, key))
            if c.spread:
                d["spread"] = c.spread
                d["spread_mode"] = c.spread_mode
                if c.spread_rate:
                    d["spread_rate"] = format_si(c.spread_rate, "Hz")
            if c.loop:
                d["loop"] = _flow(dict(c.loop))
            if c.conducted:
                d["conducted"] = c.conducted
            if c.interleave:
                d["interleave"] = c.interleave
                if c.phase_deg is not None:
                    d["phase_deg"] = c.phase_deg
            if c.source != "manual":
                d["source"] = c.source
                d["reviewed"] = c.reviewed
            if not c.enabled:
                d["enabled"] = False
            if c.from_schematic:
                d["from_schematic"] = list(c.from_schematic)
            if c.note:
                d["note"] = c.note
            seq.append(_flow(d))
        self._replace("converters", seq)

    def add_detected_converters(self, specs: list[ConverterSpec], models: Optional[dict] = None) -> list[str]:
        """Append auto-detected converters whose switching parts are not covered yet, with the part models detection
        suggests for them (models: reference -> ComponentModel, see add_suggested_models). Returns the names
        added."""
        have = {frozenset(c.parts) for c in self.config.converters} | {c.name for c in self.config.converters}
        new = [c for c in specs if frozenset(c.parts) not in have and c.name not in have]
        if new:
            self.set_converters(list(self.config.converters) + new)
            self.add_suggested_models(models or {})
        return [c.name for c in new]

    def set_pdn(self, specs: list[PdnSpec]):
        """Write the pdn entries (one flow-style line each, defaults left out). Parsed before the document changes:
        an invalid entry raises ConfigError and leaves the settings as they were."""
        from ..components.values import format_si
        seq = CommentedSeq()
        for s in specs:
            d = {"rail": s.rail}
            if s.name and s.name != s.rail:
                d["name"] = s.name
            if s.ground:
                d["ground"] = s.ground
            if s.voltage is not None:
                d["voltage"] = format_si(s.voltage, "V")
            if s.ripple != 0.05:
                d["ripple"] = f"{s.ripple * 100:g}%"
            if s.i_step is not None:
                d["i_step"] = format_si(s.i_step, "A")
            if s.source:
                d["source"] = s.source
            if s.vrm_r != 5e-3:
                d["vrm_r"] = format_si(s.vrm_r, "ohm")
            if s.vrm_l != 20e-9:
                d["vrm_l"] = format_si(s.vrm_l, "H")
            if s.parts:
                d["parts"] = list(s.parts)
            if s.check_to != 100e6:
                d["check_to"] = format_si(s.check_to, "Hz")
            if s.decap_mm != 3.0:
                d["decap_mm"] = s.decap_mm
            if not s.enabled:
                d["enabled"] = False
            if s.note:
                d["note"] = s.note
            seq.append(_flow(d))
        self._replace("pdn", seq)

    def set_si_pairs(self, pairs: list[DiffPairSpec]):
        """Write `si: pairs:` (one flow-style line each, defaults left out), keeping the rest of the `si:` section.
        Parsed before the document changes: an invalid entry raises ConfigError and leaves the settings as they
        were."""
        from ..components.values import format_si
        seq = CommentedSeq()
        for p in pairs:
            d = {"p": p.p, "n": p.n}
            if p.name:
                d["name"] = p.name
            if p.zdiff is not None:
                d["zdiff"] = format_si(p.zdiff, "ohm")
            if p.tol != 0.10:
                d["tol"] = f"{p.tol * 100:g}%"
            if p.rise_time != 1e-9:
                d["rise_time"] = format_si(p.rise_time, "s")
            if p.max_skew is not None:
                d["max_skew"] = format_si(p.max_skew, "s")
            if p.source != "manual":
                d["source"] = p.source
            if not p.reviewed:
                d["reviewed"] = False
            if not p.enabled:
                d["enabled"] = False
            if p.note:
                d["note"] = p.note
            seq.append(_flow(d))
        si = self.doc.get("si")
        si = CommentedMap(si) if isinstance(si, dict) else CommentedMap()
        si["pairs"] = seq
        self._replace("si", si)

    def set_si_targets(self, targets: list[ImpedanceTarget]):
        """Write `si: impedance:` (one flow-style line each, defaults left out), keeping the rest of `si:`."""
        from ..components.values import format_si
        seq = CommentedSeq()
        for t in targets:
            d = {}
            if t.nets:
                d["nets"] = list(t.nets)
            if t.netclass:
                d["class"] = t.netclass
            d["z0"] = format_si(t.z0, "ohm")
            if t.tol != 0.10:
                d["tol"] = f"{t.tol * 100:g}%"
            if t.layers:
                d["layers"] = list(t.layers)
            seq.append(_flow(d))
        si = self.doc.get("si")
        si = CommentedMap(si) if isinstance(si, dict) else CommentedMap()
        si["impedance"] = seq
        self._replace("si", si)

    def set_si_limits(self, crosstalk_max: float, stub_factor: float, step_mm: float):
        """Write the `si:` limits (crosstalk_max as a percentage, stub_factor, step_mm), keeping the rest of `si:`;
        a default value is left out of the file."""
        si = self.doc.get("si")
        si = CommentedMap(si) if isinstance(si, dict) else CommentedMap()
        for key, value, default, text in (("crosstalk_max", crosstalk_max, 0.05, f"{crosstalk_max * 100:g}%"),
                                          ("stub_factor", stub_factor, 3.0, stub_factor),
                                          ("step_mm", step_mm, 2.0, step_mm)):
            if abs(value - default) > 1e-12:
                si[key] = text
            elif key in si:
                del si[key]
        self._replace("si", si)

    def set_victim_nets(self, nets: list[str]):
        """Write `victims: nets:` (the nets a bridge's or power stage's switching may upset, analysis/victims.py),
        keeping the rest of `victims:`; an empty list removes the entry: the victims are then found on the board
        at each run."""
        v = self.doc.get("victims")
        v = CommentedMap(v) if isinstance(v, dict) else CommentedMap()
        if nets:
            v["nets"] = _flow_seq(nets)
        elif "nets" in v:
            del v["nets"]
        if v:
            self._replace("victims", v)
        elif "victims" in self.doc:
            del self.doc["victims"]
            self.config = config_from_data(self.doc)

    def set_dc_loads(self, specs: list[DcLoadSpec]):
        """Write the DC loads (one flow-style line each; a load as {pin, return, current}). Parsed before the document
        changes: an invalid entry raises ConfigError and leaves the settings as they were."""
        from ..components.values import format_si
        seq = CommentedSeq()
        for s in specs:
            d = {"name": s.name, "source": s.source}
            if s.source_return:
                d["source_return"] = s.source_return
            d["loads"] = [_flow({"pin": ld.pin, **({"return": ld.ret} if ld.ret else {}),
                                 "current": format_si(ld.current, "A")}) for ld in s.loads]
            if s.max_drop is not None:
                d["max_drop"] = format_si(s.max_drop, "V")
            if not s.enabled:
                d["enabled"] = False
            if s.note:
                d["note"] = s.note
            seq.append(_flow(d))
        self._replace("dc_loads", seq)

    def set_bridges(self, bridges: list[BridgeSpec]):
        """Write the bridges (block style: one mapping per bridge, its legs and motor as one-line maps). The new list
        is parsed before the document changes, so an invalid entry raises ConfigError and leaves the settings as
        they were."""
        seq = CommentedSeq()
        for b in bridges:
            seq.append(_bridge_to_doc(b))
        self._replace("bridges", seq)

    def add_detected_bridges(self, specs: list[BridgeSpec], models: Optional[dict] = None) -> list[str]:
        """Append auto-detected bridges whose legs are not listed yet (by their parts or name), with the part models
        detection suggests (current sensors and pass FETs on the input path as shorts). Returns the names added."""
        have = {frozenset(b.parts) for b in self.config.bridges} | {b.name for b in self.config.bridges}
        new = [b for b in specs if frozenset(b.parts) not in have and b.name not in have]
        if new:
            self.set_bridges(list(self.config.bridges) + new)
            self.add_suggested_models(models or {})
        return [b.name for b in new]

    def add_suggested_models(self, models: dict) -> list[str]:
        """Set part models detection suggests (a surge stopper's FET as a closed switch), only for parts whose model
        is still the default 'open': a model the user set is never replaced. Returns the references set."""
        done = []
        for ref, model in models.items():
            current = self.config.model_for(ref)
            if ref in self.config.components and (current is None or current.kind == "open"):
                self.set_component(ref, model)
                done.append(ref)
        return done

    def add_detected(self, specs: list[NetSpec]) -> list[str]:
        """Append auto-detected nets that are not listed yet. Returns the names added."""
        have = {n.name for n in self.config.nets}
        new = [s for s in specs if s.name not in have]
        if new:
            self.set_nets(list(self.config.nets) + new)
        return [s.name for s in new]

    def refresh_detected(self, updates: list[NetSpec], drop: list[str]) -> tuple[list[str], list[str]]:
        """Replace listed nets by newer detections of the same name (each keeps its on/off tick) and remove the
        `drop` entries (pieces a replacement covers): analysis.detect.refreshes says which. Returns (the names
        replaced, the names removed)."""
        new = {s.name: s for s in updates}
        out, replaced = [], []
        for n in self.config.nets:
            if n.name in drop:
                continue
            if n.name in new:
                from dataclasses import replace
                out.append(replace(new[n.name], enabled=n.enabled))
                replaced.append(n.name)
            else:
                out.append(n)
        removed = [n.name for n in self.config.nets if n.name in drop]
        if replaced or removed:
            self.set_nets(out)
        return replaced, removed

    def set_modifications(self, mods: list[Modification]):
        """Replace the `modifications:` (what-if edits) list (ConfigError leaves the settings as they were)."""
        seq = CommentedSeq()
        for m in mods:
            seq.append(_flow(m.to_dict()))
        self._replace("modifications", seq)

    def add_modifications(self, mods: list[Modification]) -> int:
        """Append what-if edits that are not there yet (same type, nets, layer and geometry). Returns the count
        added."""
        have = {m.key() for m in self.config.modifications}
        new = []
        for m in mods:
            if m.key() not in have:
                have.add(m.key())
                new.append(m)
        if new:
            self.set_modifications(list(self.config.modifications) + new)
        return len(new)

    def enable_modifications(self, keys: set, on: bool, note: str = "") -> int:
        """Switch what-if edits with these keys on or off (off e.g. once they were placed on the real board, so a
        what-if run does not count them twice). Returns the count changed."""
        mods = list(self.config.modifications)
        n = 0
        for m in mods:
            if m.enabled != on and m.key() in keys:
                m.enabled = on
                if note:
                    m.note = f"{m.note}; {note}" if m.note else note
                n += 1
        if n:
            self.set_modifications(mods)
        return n

    def add_waiver(self, fingerprint: str, reason: str):
        """Accept an issue: append its fingerprint and the user's reason to `waivers:`."""
        w = self.doc.get("waivers")
        if w is None:
            w = self.doc["waivers"] = CommentedSeq()
        w.append(_flow({"fingerprint": fingerprint, "reason": reason}))
        self.config = config_from_data(self.doc)

    def dumps(self) -> str:
        """The settings as YAML text (comments and order kept)."""
        return _dumps(self.doc)

    def save(self, path: Optional[Union[str, Path]] = None):
        """Write the settings (to its own path unless another is given), atomically."""
        _write_atomic(Path(path) if path else self.path, self.dumps())

    def snapshot(self) -> Callable[[], None]:
        """Saving in two halves for a GUI: copy the document now (~60 ms for a 200-part board) and return a
        function that writes that copy (the YAML emitting, ~400 ms), safe to run in another thread while this
        one keeps editing the settings."""
        return self.snapshot_doc()[1]

    def snapshot_doc(self) -> tuple[CommentedMap, Callable[[], None]]:
        """As snapshot, with the copied document itself too: the plugin keeps it as an undo step (ui/undo.py), so a
        save and its undo step share one deep copy. Neither the caller nor the writer may change it."""
        doc, path = copy.deepcopy(self.doc), self.path
        return doc, lambda: _write_atomic(path, _dumps(doc))

    def restore(self, doc: CommentedMap) -> None:
        """Take a document kept earlier (an undo step) as the settings again: a deep copy of it (the kept one stays
        as it is), parsed. ConfigError leaves the settings as they were."""
        doc = copy.deepcopy(doc)
        config = config_from_data(doc)
        self.doc, self.config = doc, config

    def frozen_text(self) -> Callable[[], str]:
        """The settings as they are now, for another thread: the document copied now (as snapshot), its YAML text
        made when the returned function is called (the simulation history keeps it with the run)."""
        doc = copy.deepcopy(self.doc)
        return lambda: _dumps(doc)


def _flow_seq(items) -> CommentedSeq:
    """A list written in flow style on one line ([Q1, Q2])."""
    s = CommentedSeq(list(items))
    s.fa.set_flow_style()
    return s


def _bridge_to_doc(b: BridgeSpec) -> CommentedMap:
    """A bridge as written to the settings file: optional values only when set, numbers with their units."""
    d = CommentedMap()
    d["name"] = b.name
    d["rail"] = b.rail
    d["ground"] = b.ground
    legs = CommentedSeq()
    for leg in b.legs:
        e: dict = {"phase": leg.phase, "switch_node": leg.switch_node,
                   "high_side": leg.high_side[0] if len(leg.high_side) == 1 else _flow_seq(leg.high_side),
                   "low_side": leg.low_side[0] if len(leg.low_side) == 1 else _flow_seq(leg.low_side)}
        for key in ("output", "driver", "ground_return"):
            if getattr(leg, key):
                e[key] = getattr(leg, key)
        for key in ("inputs", "snubber", "shunt"):
            if getattr(leg, key):
                e[key] = _flow_seq(getattr(leg, key))
        legs.append(_flow(e))
    d["legs"] = legs
    if b.input_connector:
        d["input_connector"] = _flow_seq(b.input_connector)
    if b.conducted:
        d["conducted"] = b.conducted
    for key in ("v_bus", "i_phase"):
        if getattr(b, key) is not None:
            d[key] = getattr(b, key)
    d["f_pwm"] = format_si(b.f_pwm, "Hz")
    d["mode"] = b.mode
    if b.speed_rpm is not None:
        d["speed_rpm"] = b.speed_rpm
    d["pwm"] = b.pwm
    d["synchronous"] = b.synchronous
    d["dead_time"] = format_si(b.dead_time, "s")
    d["rise_time"] = format_si(b.rise_time, "s")
    if b.d_max != 0.98:
        d["d_max"] = b.d_max
    for key, unit in (("rds_on", ""), ("rds_on_low", ""), ("c_oss", "F"), ("q_oss", "C"), ("c_stray", "F")):
        if getattr(b, key) is not None:
            d[key] = format_si(getattr(b, key), unit)
    for key in ("v_sd", "r_damp"):
        if getattr(b, key) is not None:
            d[key] = getattr(b, key)
    m = b.motor
    motor: dict = {"connection": m.connection}
    for key, unit in (("r_ll", ""), ("l_ll", "H")):
        if getattr(m, key) is not None:
            motor[key] = format_si(getattr(m, key), unit)
    if m.ke_ll is not None:
        motor["ke_ll"] = m.ke_ll
    if m.pole_pairs is not None:
        motor["pole_pairs"] = m.pole_pairs
    motor["emf"] = m.emf
    if m.advance_deg:
        motor["advance_deg"] = m.advance_deg
    d["motor"] = _flow(motor)
    if b.sweep_i_phase or b.sweep_speed_rpm:
        sweep = {}
        if b.sweep_i_phase:
            sweep["i_phase"] = _flow_seq(float(f"{x:.6g}") for x in b.sweep_i_phase)
        if b.sweep_speed_rpm:
            sweep["speed_rpm"] = _flow_seq(float(f"{x:.6g}") for x in b.sweep_speed_rpm)
        d["sweep"] = _flow(sweep)
    if b.source != "manual":
        d["source"] = b.source
        d["reviewed"] = b.reviewed
    if not b.enabled:
        d["enabled"] = False
    if b.from_schematic:
        d["from_schematic"] = _flow_seq(b.from_schematic)
    if b.note:
        d["note"] = b.note
    return d


def _dumps(doc: CommentedMap) -> str:
    """A YAML document as text."""
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _write_atomic(path: Path, text: str):
    """Write a temporary file and rename it over the target: an interrupted save never leaves half a file. The
    folder (the board's groundtruth folder) is made when missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _suggested_for(fp, value: str, footprint_name: str) -> ComponentModel:
    """The model that would have been suggested for this part with another value and footprint (its stored ones):
    a shallow copy of the footprint with those swapped in, through suggest_model."""
    from copy import copy
    clone = copy(fp)
    clone.value = value
    clone.lib_id = footprint_name
    return suggest_model(clone)


def _same_model(a: dict, b: dict) -> bool:
    """Two models (as settings entries) are the same: equal kind and parameters, numbers to rounding; the note and
    the field links don't count."""
    keys = (set(a) | set(b)) - {"note", "from_schematic"}
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if abs(va - vb) > 1e-12 * max(abs(va), abs(vb), 1e-30):
                return False
        elif va != vb:
            return False
    return True


def _natural_key(s: str):
    """A sort key that orders numbers by value: C2 before C10 ("C", 2, "" against "C", 10, "")."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def guess_reference_nets(board: Board) -> list[str]:
    """The board's ground nets by name (GND, AGND, PGND, GND_ISO, VSS, 0V ...; only the last part of a
    hierarchical name is looked at), or ["GND"] when none matches."""
    nets = [n for n in board.nets if GND_PATTERN.search(n.split("/")[-1])]
    return nets or ["GND"]


_SWITCH_RANGES = (("c_oss", 1e-13, 1e-6, "a switch's output capacitance (pF to nF)", "p", "F"),
                  ("q_oss", 1e-12, 1e-5, "a switch's output charge (nC)", "n", "C"),
                  ("c_stray", 0.0, 1e-6, "a stray capacitance (pF)", "p", "F"),
                  ("rise_time", 1e-11, 1e-5, "a switching edge (ns)", "n", "s"),
                  ("rds_on", 1e-5, 1.0, "a switch's R_DS(on) (mΩ)", "m", "Ω"),
                  ("rds_on_low", 1e-5, 1.0, "a switch's R_DS(on) (mΩ)", "m", "Ω"))
_CONVERTER_RANGES = _SWITCH_RANGES + (("f_sw", 1e3, 1e8, "a switching frequency (kHz to MHz)", "k", "Hz"),
                                      ("dcr", 1e-5, 1.0, "an inductor's DC resistance (mΩ)", "m", "Ω"),
                                      ("dead_rise", 0.0, 2e-6, "a dead time (ns)", "n", "s"),
                                      ("dead_fall", 0.0, 2e-6, "a dead time (ns)", "n", "s"),
                                      ("v_sd", 0.0, 10.0, "a reverse-conduction voltage (V)", "", "V"),
                                      ("q_rr", 0.0, 1e-5, "a reverse-recovery charge (nC)", "n", "C"))
_BRIDGE_RANGES = _SWITCH_RANGES + (("f_pwm", 1e3, 5e6, "a PWM frequency (kHz)", "k", "Hz"),
                                   ("dead_time", 0.0, 2e-6, "a dead time (ns)", "n", "s"),
                                   ("v_bus", 1.0, 1000.0, "a bus voltage (V)", "", "V"),
                                   ("i_phase", 0.0, 5000.0, "a phase current (A)", "", "A"),
                                   ("speed_rpm", 0.0, 2e5, "a motor speed (rpm)", "", "rpm"),
                                   ("v_sd", 0.0, 10.0, "a reverse-conduction voltage (V)", "", "V"))
_MOTOR_RANGES = (("r_ll", 1e-4, 100.0, "a motor's line-to-line resistance (mΩ to Ω)", "m", "Ω"),
                 ("l_ll", 1e-7, 0.1, "a motor's line-to-line inductance (µH to mH)", "µ", "H"),
                 ("ke_ll", 0.01, 1000.0, "a back-EMF constant (V per 1000 rpm)", "", "V/krpm"),
                 ("pole_pairs", 1, 50, "a number of pole pairs", "", ""))


def _out_of_range(obj, ranges, prefix_name: str = "") -> list[str]:
    """For each (key, lo, hi, what, prefix, unit) of `ranges`: a message when obj.key is set but outside lo..hi,
    with a hint to add the unit prefix when the value looks like one is missing ("c_oss 482 F ... write 482p")."""
    out = []
    for key, lo, hi, what, prefix, unit in ranges:
        v = getattr(obj, key)
        if v is not None and not lo <= v <= hi:
            hint = (f"; if its unit is missing, write {v:g}{prefix}"
                    if prefix and (v > hi or key in ("f_sw", "f_pwm")) else "")
            out.append(f"{prefix_name}{key} {format_si(v, unit)} is unusual for {what}: used as written{hint}")
    return out


def implausible(c: Union[ConverterSpec, BridgeSpec]) -> list[str]:
    """Values outside the usual range for their kind, often a missing unit prefix: 'c_oss: 482' is read as 482 F
    (seen on refboard: the ringing went to 0 Hz and every margin became meaningless). They never block a
    run (the user: "if the user decides that's the value, it's the value"): the rule check `unusual_value` lists
    them as info issues, each saying what to write if a unit is missing. A bridge's motor data are checked too."""
    if isinstance(c, BridgeSpec):
        return _out_of_range(c, _BRIDGE_RANGES) + _out_of_range(c.motor, _MOTOR_RANGES, "motor.")
    return _out_of_range(c, _CONVERTER_RANGES)


def validate(config: Config, board: Board) -> list[str]:
    """Cross-check the settings against the board. Returns error messages (empty = OK). Rows switched off are
    not checked (they may be off because the board no longer has their parts)."""
    config = config.active()
    errors = []
    board_nets = set(board.nets)
    for r in config.reference_nets:
        if r not in board_nets:
            errors.append(f"reference net {r!r} does not exist on the board")
    for d in config.dc_loads:
        for pin in d.pins:
            if board.find_pin(pin) is None:
                errors.append(f"DC load {d.name}: pin {pin} does not exist on the board")
    for d in config.pdn:
        for net in [d.rail] + ([d.ground] if d.ground else []):
            if net not in board_nets:
                errors.append(f"PDN {d.label}: net {net!r} does not exist on the board")
        for ref in d.parts + ([d.source] if d.source and d.source.lower() != "none" else []):
            if board.footprint(ref) is None:
                errors.append(f"PDN {d.label}: part {ref} does not exist on the board")
    for n in config.nets:
        if n.name not in board_nets:
            errors.append(f"net {n.name!r} does not exist on the board")
        for pin in [n.driver, *n.receivers, *(p for p in (n.driver_return, n.receiver_return) if p)]:
            if board.find_pin(pin) is None:
                errors.append(f"net {n.name!r}: pin {pin!r} not found (use REF.PAD, e.g. U1.12)")
        if n.freq <= 0 or n.rise_time <= 0:
            errors.append(f"net {n.name!r}: freq and rise_time must be positive")
        if n.fall_time is not None and n.fall_time <= 0:
            errors.append(f"net {n.name!r}: fall_time must be positive")
        for key in ("driver_nh", "driver_pf"):
            if getattr(n, key) is not None and getattr(n, key) < 0:
                errors.append(f"net {n.name!r}: {key} must not be negative")
        if n.load_ohms is not None and n.load_ohms <= 0:
            errors.append(f"net {n.name!r}: load_ohms must be positive (leave it out for no termination)")
        for ref in n.through:
            fp = board.footprint(ref)
            if fp is None:
                errors.append(f"net {n.name!r}: part {ref!r} in `through` is not on the board")
            elif len({p.net for p in fp.pads if p.net}) != 2:
                errors.append(f"net {n.name!r}: part {ref!r} in `through` must join two nets (a 2-pin part)")
    for c in config.converters:
        for net in (c.rail, c.ground, c.switch_node):
            if net and net not in board_nets:
                errors.append(f"converter {c.name!r}: net {net!r} does not exist on the board")
        ends = {"high_side": (c.rail, c.switch_node), "low_side": (c.switch_node, c.ground),
                "switch": (c.rail, c.ground)}
        for ref in c.parts:
            fp = board.footprint(ref)
            if fp is None:
                errors.append(f"converter {c.name!r}: part {ref!r} not found")
                continue
            for role, (na, nb) in ends.items():
                if ref in getattr(c, role) and not ({na, nb} <= fp.nets()):
                    errors.append(f"converter {c.name!r}: {ref} ({role}) needs pads on {na} and {nb}")
        for key in ("input_connector", "output_connector"):
            for pin in getattr(c, key):
                if board.find_pin(pin) is None:
                    errors.append(f"converter {c.name!r}: {key} pin {pin!r} not found (use REF.PAD)")
    used = {ref: f"converter {c.name!r}" for c in config.converters for ref in c.parts}
    for b in config.bridges:
        for net in (b.rail, b.ground, *(n for leg in b.legs for n in (leg.switch_node, leg.ground_return))):
            if net and net not in board_nets:
                errors.append(f"bridge {b.name!r}: net {net!r} does not exist on the board")
        for leg in b.legs:
            for ref in leg.shunt:
                if board.footprint(ref) is None:
                    errors.append(f"bridge {b.name!r} leg {leg.phase}: shunt {ref!r} not found")
            # a low side on the leg's own return net (a shunt per phase to the bridge's ground) has its pads there
            for refs, (na, nb), role in ((leg.high_side, (b.rail, leg.switch_node), "high side"),
                                         (leg.low_side, (leg.switch_node, leg.ground_return or b.ground), "low side")):
                for ref in refs:
                    fp = board.footprint(ref)
                    if fp is None:
                        errors.append(f"bridge {b.name!r} leg {leg.phase}: part {ref!r} not found")
                    elif not {na, nb} <= fp.nets():
                        errors.append(f"bridge {b.name!r} leg {leg.phase}: {ref} ({role}) needs pads on {na} and {nb}")
                    elif ref in used:
                        errors.append(f"bridge {b.name!r} leg {leg.phase}: {ref} is also in {used[ref]}")
                    used.setdefault(ref, f"bridge {b.name!r}")
            if leg.output and board.find_pin(leg.output) is None:
                errors.append(f"bridge {b.name!r} leg {leg.phase}: output pin {leg.output!r} not found (use REF.PAD)")
        for pin in b.input_connector:
            if board.find_pin(pin) is None:
                errors.append(f"bridge {b.name!r}: input_connector pin {pin!r} not found (use REF.PAD)")
    return errors
