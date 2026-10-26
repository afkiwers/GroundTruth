"""Issue records shared by the rule checks, the solver, the reports and the plugin UI."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Optional

SEVERITY_RANK = {"off": -1, "info": 0, "warning": 1, "error": 2}

# check id -> (default severity, short title)
CHECKS: dict[str, tuple[str, str]] = {
    "route_missing": ("error", "No copper path between driver and receiver"),
    "no_reference": ("error", "No reference plane next to the signal layer"),
    "gap_crossing": ("error", "Trace crosses a gap in its reference plane"),
    "off_plane": ("warning", "Trace runs without a reference plane"),
    "layer_change_stitch": ("error", "Reference layer change without a nearby return via"),
    "layer_change_decap": ("warning", "Reference net change without a nearby decoupling capacitor"),
    "edge_proximity": ("warning", "Trace close to the edge of its reference plane"),
    "reference_net": ("info", "Trace referenced to a non-ground plane"),
    "schematic_differs": ("info", "A part parameter set in GroundTruth differs from the schematic field"),
    "unusual_value": ("info", "A power-stage value outside the usual range for its kind (used as written)"),
    "return_path_broken": ("error", "Return path broken on the reference layer"),
    "return_detour": ("warning", "Return current detours (loop inductance above ideal)"),
    "plane_resonance": ("warning", "Plane resonance raises the return-loop emission"),
    "bandwidth_limit": ("warning", "A signal path's or op-amp stage's bandwidth above its limit (max_bandwidth, "
                                   "an op-amp's Bandwidth field)"),
    "opamp_stability": ("warning", "An op-amp's loop has little phase margin: it rings (below 45°) or is close to "
                                   "oscillating (below 20°: an error)"),
    "emi_margin": ("warning", "Estimated radiated emission close to or above the limit"),
    "band_unsolved": ("warning", "Part of the radiated band could not be solved (emission held, not computed)"),
    "dc_unsolved": ("warning", "A power stage's DC path could not be solved (left out of its loss, input current and "
                               "heat)"),
    "dc_path_missing": ("warning", "A power stage's output connector is not reached from its inductor through parts: "
                                   "its DC output path is not modelled"),
    "hot_loop": ("warning", "Switch-node overshoot from the converter's hot-loop inductance"),
    "conducted_emission": ("warning", "Estimated conducted emission at the converter input close to or above the limit"),
    "power_loop_broken": ("error", "Converter hot loop cannot be modelled"),
    "current_density": ("warning", "High DC current density in a converter's power path"),
    "via_overload": ("warning", "Via carries more current than its IPC-2221 capacity"),
    "dc_drop": ("warning", "DC voltage drop to a load above the rail's limit"),
    "bridge_operating_point": ("warning", "Motor bridge cannot reach its phase current at this speed and bus voltage"),
    "victim_coupling": ("warning", "A motor bridge's or power stage's switching moves a part's ground or puts "
                                "voltage on a sensitive net"),
    "thermal_hotspot": ("warning", "Copper temperature above the limit (thermal solve)"),
    "part_temperature": ("warning", "Part temperature above the limit (thermal solve)"),
    "pdn_impedance": ("warning", "A supply rail's impedance at an IC above its target (PDN)"),
    "decap_missing": ("warning", "An IC's supply pin with no decoupling capacitor close by"),
    "clearance_voltage": ("error", "Copper of two nets closer than IPC-2221B asks for their voltage difference"),
    "cap_voltage": ("warning", "A capacitor's voltage over its (derated) rating"),
    "esd_protection": ("info", "A connector's pins without an ESD / TVS part on their nets close by"),
    "resistor_power": ("warning", "A resistor's loss over its (derated) power rating"),
    "vds_rating": ("warning", "A switch node's peak over the switches' (derated) voltage rating"),
    "impedance": ("warning", "A net's characteristic impedance outside its target along part of its tracks"),
    "diff_impedance": ("warning", "A differential pair's impedance outside its target"),
    "diff_skew": ("warning", "A differential pair's two nets differ in delay by more than allowed"),
    "crosstalk": ("warning", "A fast net couples more than allowed into a track running beside it"),
    "via_stub": ("warning", "A via's unused barrel resonates within the signal's spectrum"),
    "tuning_delay": ("warning", "A KiCad tuning profile's unit delay differs from the board's cross-sections"),
    "loop_phase_margin": ("warning", "A converter's control loop has too little phase or gain margin"),
    "loop_crossover": ("warning", "A converter's control loop crosses over too close to its switching frequency"),
}


@dataclass
class Issue:
    """One finding of a check or the solver: which check (a key of CHECKS), how serious, on which net, a message
    for the user, where on the board, and what it concerns (the KiCad items' uuids, used to select them in KiCad).
    `waived` is set when the settings' waivers name its fingerprint."""
    check: str
    severity: str
    net: str
    message: str
    location: tuple[float, float]       # mm, board coordinates
    layer: str = ""
    length_mm: float = 0.0
    items: list[str] = field(default_factory=list)   # KiCad uuids of the offending items
    line: int = 0                        # line in the .kicad_pcb (for CI reports)
    key: str = ""                        # discriminator for the fingerprint
    details: dict = field(default_factory=dict)
    waived: bool = False
    waiver_reason: str = ""

    @property
    def title(self) -> str:
        """The check's short title (CHECKS), or the check id for an unknown one."""
        return CHECKS.get(self.check, ("", self.check))[1]

    @property
    def fingerprint(self) -> str:
        """Stable across unrelated edits: built from check, net and the offending items' uuids."""
        # What identifies the issue, best first: an explicit key the check chose (e.g. a route's pins), the
        # items' uuids (stable while those items exist), else the location rounded to 0.1 mm. A waiver stores
        # this hash, so it must not change when something elsewhere on the board is edited.
        basis = self.key or ",".join(sorted(self.items)) or f"{self.location[0]:.1f},{self.location[1]:.1f}"
        return hashlib.sha1(f"{self.check}|{self.net}|{basis}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        """The issue as plain data for the JSON report, with its fingerprint and title added."""
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        d["title"] = self.title
        return d


def resolve_severity(check: str, overrides: dict[str, str], default: Optional[str] = None) -> str:
    """The severity an issue of `check` gets: the settings' rules.severity override if there is one, else the
    caller's default, else the check's own default from CHECKS ("off" means the issue is not reported)."""
    return overrides.get(check, default or CHECKS.get(check, ("warning", ""))[0])


def dedupe(issues: list[Issue]) -> list[Issue]:
    """The issues with repeats removed (the same fingerprint found twice, e.g. by two receivers of one net), the
    first of each kept, order unchanged."""
    seen = set()
    out = []
    for i in issues:
        if i.fingerprint in seen:
            continue
        seen.add(i.fingerprint)
        out.append(i)
    return out


def sort_issues(issues: list[Issue]) -> list[Issue]:
    """Errors first, then warnings, then info; within a severity by net, check and location (a stable order for
    the Issues tab and the reports)."""
    return sorted(issues, key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), i.net, i.check, i.location))
