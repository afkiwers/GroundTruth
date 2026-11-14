"""The board's power tree: where each supply rail comes from and what it feeds, as a graph for a flow chart (the
user, with a picture of one: "it would be nice if the user could create a flow chart similar to this per button
press to better understand the topology"). report/power_tree.py lays it out and draws it; the Power integrity and
Converters tabs show it in a window (Power tree…).

What it is read from (no solve; the board and the settings only):

- **rails**: every net a part draws supply from or puts out (schematic pin types power_in / power_out), a power
  stage's input and output (Converters tab: analysis/pdn.stage_outputs, its internal nets behind a sense shunt
  left out), and the nets joined to those through a series link; grounds left out (safety.is_ground and the
  reference nets). Each with its voltage where known (a stage's V out, the voltage model's: safety.net_voltages,
  else the name: pdn.rail_voltage);
- **sources**: connectors with a pad on a rail and one on a ground, except a stage's output connector (a load);
- **SMPS**: the enabled power stages, input rail → output rail (a boost's input is its inductor's far side), with
  V out, I out, f_sw and the efficiency at its load where a Simulate's sweep has it;
- **LDOs**: a part with a power_in pad on one rail and a power_out pad on another, not a stage's own part;
- **series links**: a series pass FET (detect.pass_switches), a 0R / ferrite / fuse, or a shunt up to LINK_MAX_OHM
  between two rails; the side nearer a source feeds the other;
- **loads**: per rail the ICs drawing from it (pdn.supply_parts; a stage's output connector too), up to LOAD_MAX
  each, more as one group;
- **capacitors**: per rail its capacitors to ground grouped by value ("4 × 10 µF"), at most CAP_GROUPS groups.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..components.values import format_si

LINK_MAX_OHM = 1.0          # a resistor between two rails up to this: a shunt (a series link), not a divider
LOAD_MAX = 4                # loads of a rail shown one by one; more as one group
CAP_GROUPS = 3              # capacitor values of a rail shown as groups; the rest as one
REFS_SHOWN = 4              # references listed in a group before "+n more"
CONNECTORS = ("J", "P", "X", "CN", "CON")
KINDS = ("source", "link", "smps", "ldo", "rail", "load", "caps")


@dataclass
class TreeNode:
    """One box: its id, kind (KINDS), title, lines under it, the parts it stands for and its net (a rail's)."""
    id: str
    kind: str
    title: str
    lines: list = field(default_factory=list)
    refs: list = field(default_factory=list)
    net: str = ""


@dataclass
class PowerTree:
    """The graph: nodes, edges (from, to, kind: "power" or "caps"), notes."""
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def node(self, nid: str) -> Optional[TreeNode]:
        """The node with this id."""
        return next((n for n in self.nodes if n.id == nid), None)


def _short(net: str) -> str:
    """A net's name without its sheet path ("/DCDC Stage/Rails/+5V" -> "+5V") and KiCad's text markup's braces
    ("V_{BATT}" -> "V_BATT")."""
    return (net.rsplit("/", 1)[-1] or net).replace("{", "").replace("}", "")


def _prefix(ref: str) -> str:
    """A reference's letters ("J12" -> "J")."""
    return re.match(r"[A-Za-z_]*", ref).group(0).upper()


def _refs_text(refs: list) -> str:
    """References for a box: the first REFS_SHOWN, then "+n more"."""
    shown = ", ".join(refs[:REFS_SHOWN])
    return shown + (f" +{len(refs) - REFS_SHOWN} more" if len(refs) > REFS_SHOWN else "")


def power_tree(board, config, results=None) -> PowerTree:
    """The board's power tree (see the module); `results`: a Simulate's (its stages' efficiency at their load)."""
    from .loop import controller
    from .pdn import decaps, stage_outputs, supply_parts
    from .power import switch_inductor
    from .safety import is_ground
    tree = PowerTree()
    grounds = set(config.reference_nets) | {n for n in board.nets if is_ground(n)}
    ground = config.reference_nets[0] if config.reference_nets else "GND"
    stages = [s for s in config.converters if s.enabled and not s.bridge]
    eff = {c.net: c.efficiency for c in getattr(results, "converters", ()) if getattr(c, "efficiency", None)}

    # ---------------------------------------------------------------- the stages and the nets they own
    stage_io, internal, stage_parts, out_connectors, in_connectors = [], set(), set(), set(), set()
    for spec in stages:
        outs, out_rail, _ = stage_outputs(board, config, spec)
        node, ind = switch_inductor(board, config, spec)
        if spec.topology == "boost":
            far = [p.net for p in ind.pads if p.net and p.net != node] if ind is not None else []
            in_rail = far[0] if far else ""
        else:
            in_rail = spec.rail
        internal |= (outs - {out_rail}) | ({node} if node else set())
        stage_parts |= set(spec.parts) | ({ind.reference} if ind is not None else set())
        ctrl = controller(board, spec)[0]
        if ctrl is not None:
            stage_parts.add(ctrl.reference)
        if spec.output_connector:
            out_connectors.add(spec.output_connector[0].rsplit(".", 1)[0])
        if spec.input_connector and _prefix(spec.input_connector[0]) in CONNECTORS:
            in_connectors.add(spec.input_connector[0].rsplit(".", 1)[0])
        stage_io.append((spec, in_rail, out_rail))

    # ---------------------------------------------------------------- the rails
    rails = set()
    for fp in board.footprints:
        if fp.dnp:
            continue
        for p in fp.pads:
            if p.net and p.pin_type in ("power_in", "power_out"):
                rails.add(p.net)
    rails |= {n for _, a, b in stage_io for n in (a, b) if n}
    # a stage's input connector's + pin: the supply may enter there through a fuse or a filter before its rail
    for spec, _, _ in stage_io:
        if spec.input_connector and _prefix(spec.input_connector[0]) in CONNECTORS:
            rails |= {p.net for p in board.pin_pads(spec.input_connector[0]) if p.net}
    rails -= grounds | internal
    links = _links(board, config, grounds, stage_parts)
    grew = True
    while grew:                     # nets joined to a rail through a link are rails (the pass FET's input side)
        grew = False
        for ref, (a, b, _) in links.items():
            for x, y in ((a, b), (b, a)):
                if x in rails and y not in rails and y not in grounds | internal:
                    rails.add(y)
                    grew = True
    volts = _voltages(board, config, rails, stage_io)

    def rail_id(net):
        """A rail's node id."""
        return f"rail:{net}"
    for net in sorted(rails, key=lambda n: (-(volts.get(n) or 0.0), n)):
        v = volts.get(net)
        tree.nodes.append(TreeNode(rail_id(net), "rail", _short(net), [format_si(v, "V") if v else "voltage ?"],
                                   net=net))

    # ---------------------------------------------------------------- converters, LDOs, links, sources
    for spec, a, b in stage_io:
        if not (a in rails and b in rails):
            tree.notes.append(f"{spec.name}: its input or output rail was not found")
            continue
        lines = [f"{format_si(spec.v_out, 'V')} out" if spec.v_out else "V out ?",
                 f"{spec.i_out:g} A, {format_si(spec.f_sw, 'Hz')}"]
        e = eff.get(spec.name)
        if e is not None and not e.error and e.currents:
            k = e.at(spec.i_out)
            lines.append(f"η {100 * e.efficiency[k]:.0f} %")
        if spec.interleave:
            n = sum(1 for s in stages if s.interleave == spec.interleave)
            lines.append(f"{n} phases ({spec.interleave})")
        nid = f"smps:{spec.name}"
        tree.nodes.append(TreeNode(nid, "smps", spec.name, lines, list(spec.parts)))
        tree.edges += [(rail_id(a), nid, "power"), (nid, rail_id(b), "power")]
    for fp in board.footprints:
        if fp.dnp or fp.reference in stage_parts:
            continue
        ins = {p.net for p in fp.pads if p.pin_type == "power_in" and p.net in rails}
        outs = {p.net for p in fp.pads if p.pin_type == "power_out" and p.net in rails} - ins
        if ins and outs:
            a, b = sorted(ins)[0], sorted(outs)[0]
            nid = f"ldo:{fp.reference}"
            v = volts.get(b)
            tree.nodes.append(TreeNode(nid, "ldo", fp.reference, [fp.value or "", f"{format_si(v, 'V')} out" if v
                                                                   else ""], [fp.reference]))
            tree.edges += [(rail_id(a), nid, "power"), (nid, rail_id(b), "power")]
    # connectors: a stage's input connector is a source; another one too unless it draws from its rail (a power_in
    # pin: a debug header's VTref) or its rail is fed by a stage or a regulator already (then it is a load)
    fed = {e[1] for e in tree.edges if e[0].startswith(("smps:", "ldo:"))}
    sources, plugged = [], []
    for fp in board.footprints:
        if fp.dnp or _prefix(fp.reference) not in CONNECTORS or fp.reference in out_connectors:
            continue
        pads = [p for p in fp.pads if p.net in rails]
        on = sorted({p.net for p in pads})
        if not on or not any(p.net in grounds for p in fp.pads):
            continue
        if fp.reference not in in_connectors and (any(p.pin_type == "power_in" for p in pads)
                                                  or any(rail_id(n) in fed for n in on)):
            plugged.append(fp.reference)
            continue
        nid = f"src:{fp.reference}"
        v = volts.get(on[0])
        tree.nodes.append(TreeNode(nid, "source", fp.reference, [f"{format_si(v, 'V')}" if v else fp.value or ""],
                                   [fp.reference]))
        tree.edges += [(nid, rail_id(n), "power") for n in on]
        sources += on
    # the links, oriented from the side nearer a source (fed first) to the other
    depth = _depths(tree, sources, links, rail_id)
    for ref, (a, b, text) in links.items():
        if a not in rails or b not in rails:
            continue
        if depth.get(rail_id(b), 1e9) < depth.get(rail_id(a), 1e9):
            a, b = b, a
        nid = f"link:{ref}"
        tree.nodes.append(TreeNode(nid, "link", ref, [text], [ref]))
        tree.edges += [(rail_id(a), nid, "power"), (nid, rail_id(b), "power")]

    # ---------------------------------------------------------------- loads and capacitors per rail
    fed_parts = {r for n in tree.nodes if n.kind in ("ldo", "smps") for r in n.refs}
    for net in sorted(rails):
        ics = [fp.reference for fp, _, _ in supply_parts(board, net, ground) if fp.reference not in fed_parts
               and fp.reference not in stage_parts]
        # a stage's output connector (or the part its output pins name) and the connectors plugged on the rail
        ics += [r for r in sorted(out_connectors | set(plugged)) if r not in ics and r not in stage_parts
                and board.footprint(r) is not None and any(p.net == net for p in board.footprint(r).pads)]
        ics = list(dict.fromkeys(ics))
        if len(ics) > LOAD_MAX:
            tree.nodes.append(TreeNode(f"load:{net}", "load", f"{len(ics)} loads", [_refs_text(ics)], ics, net))
            tree.edges.append((rail_id(net), f"load:{net}", "power"))
        else:
            for ref in ics:
                fp = board.footprint(ref)
                nid = f"load:{net}:{ref}"
                tree.nodes.append(TreeNode(nid, "load", ref, [fp.value if fp is not None else ""], [ref], net))
                tree.edges.append((rail_id(net), nid, "power"))
        groups: dict = {}
        for fp in decaps(board, config, net, ground):
            m = config.model_for(fp.reference)
            c = m.get("C") if m is not None else None
            groups.setdefault(format_si(c, "F") if c else (fp.value or "?"), []).append(fp.reference)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        rest = [r for _, refs in ordered[CAP_GROUPS:] for r in refs]
        for k, (value, refs) in enumerate(ordered[:CAP_GROUPS]):
            nid = f"caps:{net}:{k}"
            tree.nodes.append(TreeNode(nid, "caps", value, [f"{len(refs)} × {value}", _refs_text(refs)], refs, net))
            tree.edges.append((rail_id(net), nid, "caps"))
        if rest:
            nid = f"caps:{net}:rest"
            tree.nodes.append(TreeNode(nid, "caps", "other", [f"{len(rest)} capacitor{'s' if len(rest) > 1 else ''}", _refs_text(rest)], rest, net))
            tree.edges.append((rail_id(net), nid, "caps"))
    unfed = [n.title for n in tree.nodes if n.kind == "rail" and not any(e[1] == n.id for e in tree.edges)]
    if unfed:
        tree.notes.append(f"no source found for {', '.join(unfed)} (no connector, stage, regulator or link feeds it)")
    return tree


def _links(board, config, grounds: set, skip: set = frozenset()) -> dict:
    """The series links between two non-ground nets: {ref: (net a, net b, its text)}: pass FETs
    (detect.pass_switches; one already modelled as a short or a resistor: its drain and source, the gate left
    out), parts modelled as a short, a ferrite or a fuse, and resistors up to LINK_MAX_OHM."""
    from .detect import GATE_FN, pass_switches
    out = {ref: (a, b, "pass FET") for ref, (a, b) in pass_switches(board, config).items()}
    for fp in board.footprints:
        if fp.dnp or fp.reference in out:
            continue
        gate = _prefix(fp.reference) in ("Q", "T", "M") and any(GATE_FN.match(p.pin_function or "") for p in fp.pads)
        nets = sorted({p.net for p in fp.pads if p.net and not (gate and GATE_FN.match(p.pin_function or ""))})
        if len(nets) != 2 or set(nets) & grounds:
            continue
        if gate:                                 # a FET modelled as a series part: a pass FET (surge stopper)
            m = config.model_for(fp.reference)
            if m is not None and m.kind in ("short", "resistor") and (m.get("R", 0.0) or 0.0) <= LINK_MAX_OHM:
                out[fp.reference] = (nets[0], nets[1], "pass FET")
            continue
        m = config.model_for(fp.reference)
        if m is None:
            continue
        if m.kind in ("short", "ferrite"):
            out[fp.reference] = (nets[0], nets[1], fp.value or m.kind)
        elif m.kind == "inductor" and fp.reference not in skip:    # an input / output filter's inductor
            out[fp.reference] = (nets[0], nets[1], f"filter {format_si(m.get('L'), 'H')}" if m.get("L")
                                 else "filter inductor")
        elif m.kind == "resistor" and (m.get("R", 1e9) or 0.0) <= LINK_MAX_OHM:
            out[fp.reference] = (nets[0], nets[1], f"shunt {format_si(m.get('R'), 'Ω')}")
    return out


def _voltages(board, config, rails: set, stage_io: list) -> dict:
    """Each rail's voltage: a stage's V out (and V in) first, then the safety voltage model's, then its name's."""
    from .pdn import rail_voltage
    from .safety import net_voltages
    out = {}
    for spec, a, b in stage_io:
        if spec.v_out and b:
            out.setdefault(b, spec.v_out)
        if spec.v_in and a:
            out.setdefault(a, spec.v_in)
    try:
        known = net_voltages(board, config)
    except Exception:                       # the voltage model is a nicety here: names are the fallback
        known = {}
    for net in rails:
        if net not in out:
            v = known.get(net, (None,))[0] if net in known else None
            out[net] = v if v else rail_voltage(net)
    return out


def _depths(tree: PowerTree, sources: list, links: dict, rail_id) -> dict:
    """Each rail node's distance from a source rail (breadth first over the stages, LDOs and the links both
    ways): where a link's power comes from."""
    adj: dict = {}
    for a, b, _ in tree.edges:
        adj.setdefault(a, set()).add(b)
    for a, b, _ in links.values():
        adj.setdefault(rail_id(a), set()).add(rail_id(b))
        adj.setdefault(rail_id(b), set()).add(rail_id(a))
    depth = {rail_id(n): 0 for n in sources}
    todo = list(depth)
    while todo:
        x = todo.pop(0)
        for y in adj.get(x, ()):
            if y not in depth:
                depth[y] = depth[x] + 1
                todo.append(y)
    return depth
