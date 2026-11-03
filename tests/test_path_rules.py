"""A signal path's nets, routes and circuit on adversarial boards (audit 2026-11, the verification brief's Phase 2):
each test is one finding of the audit, its expected value from an independent oracle written here (a nodal solver,
an enumerator of simple paths over the parts' own roles, closed forms), never from GroundTruth's own code.

Boards are built in memory from the core.board dataclasses (no file, no copper unless a test routes it): the
netlist is what these tests are about."""
import math
import random
import time

import numpy as np
import pytest

from groundtruth.components.defaults import suggest_model
from groundtruth.components.models import ComponentModel
from groundtruth.config.settings import ComponentEntry, Config, NetSpec
from groundtruth.core.board import Board, CopperLayer, Footprint, Pad, Stackup, Track

TAU = 2 * math.pi
R_S = 25.0                     # the driver's default output resistance (analysis.driver_ohms)


# ------------------------------------------------------------------------------------------------ boards
def part(ref, pads, value="", fields=None, dnp=False):
    """A part: pads [(number, net, pin type, pin name)]."""
    return dict(ref=ref, value=value, pads=pads, fields=fields or {}, dnp=dnp)


def R(ref, a, b, value="1k", dnp=False):
    """A resistor between nets a and b."""
    return part(ref, [(1, a, "passive", ""), (2, b, "passive", "")], value, dnp=dnp)


def C(ref, a, b, value="100n"):
    """A capacitor between nets a and b."""
    return part(ref, [(1, a, "passive", ""), (2, b, "passive", "")], value)


def IC(ref, net, ptype="output"):
    """A chip with one signal pin (pad 1) and its ground pin."""
    return part(ref, [(1, net, ptype, "IO"), (2, "GND", "power_in", "GND")], "IC")


def OPA(ref, inp, inn, out, vp="+5V"):
    """A single op-amp by its pin names: +IN 3, -IN 2, OUT 1, V+ 5, V- 4."""
    return part(ref, [(3, inp, "input", "+IN"), (2, inn, "input", "-IN"), (1, out, "output", "OUT"),
                      (5, vp, "power_in", "V+"), (4, "GND", "power_in", "V-")], "OPA")


def QUAD(ref, chans, reverse=False):
    """A quad op-amp, channels [(in+, in−, out)] as pads (3k+3, 3k+2, 3k+1), told by GT list labels (reverse: the
    channels written in the other order)."""
    pads, roles = [], []
    for k, (i, n, o) in enumerate(chans):
        pads += [(3 * k + 1, o, "output", ""), (3 * k + 2, n, "input", ""), (3 * k + 3, i, "input", "")]
        roles.append((3 * k + 3, 3 * k + 2, 3 * k + 1))
    pads += [(40, "+5V", "power_in", "V+"), (41, "GND", "power_in", "V-")]
    roles = roles[::-1] if reverse else roles
    gt = ", ".join(f"amp_{r}:[{','.join(str(x[k]) for x in roles)}]" for k, r in enumerate(("in", "inv", "out")))
    return part(ref, pads, "QUAD", {"GT": gt})


def BUF(ref, chans):
    """A logic buffer, channels [(in net, out net)] told by GT labels."""
    pads, gt = [], []
    for k, (i, o) in enumerate(chans):
        pads += [(2 * k + 1, i, "input", ""), (2 * k + 2, o, "output", "")]
        gt += [f"buf_in:{2 * k + 1}", f"buf_out:{2 * k + 2}"]
    pads += [(40, "+5V", "power_in", "VCC"), (41, "GND", "power_in", "GND")]
    return part(ref, pads, "BUF", {"GT": ", ".join(gt)})


def REG(ref="U90", rail="+5V"):
    """A regulator: its output (power_out) makes `rail` a supply."""
    return part(ref, [(1, rail, "power_out", "VOUT"), (2, "GND", "power_in", "GND")], "REG")


def board_of(parts, routed=False) -> Board:
    """A Board of these parts, pads 1 mm apart; routed: every pad on a large circle and each net's pads joined by
    straight tracks in a chain (on a strictly convex circle no track touches another net's pad)."""
    fps, allpads = [], []
    for k, p in enumerate(parts):
        pads = [Pad(p["ref"], str(num), (10.0 * k, 1.0 * j), 0.0, (0.5, 0.5), "rect", ["F.Cu"], net or "", "smd",
                    pin_type=pt, pin_function=fn) for j, (num, net, pt, fn) in enumerate(p["pads"])]
        allpads += pads
        fps.append(Footprint(p["ref"], p["value"], "Lib:X", (10.0 * k, 0.0), 0.0, "F.Cu", pads, dnp=p["dnp"],
                             fields=dict(p["fields"])))
    stack = Stackup([CopperLayer("F.Cu", 0, 0.0), CopperLayer("B.Cu", 1, 1.6)])
    b = Board("t.kicad_pcb", stack, nets=sorted({q.net for q in allpads if q.net}), footprints=fps)
    if routed:
        rad = max(50.0, 3.0 * len(allpads))
        for i, q in enumerate(allpads):
            q.position = (100 + rad * math.cos(TAU * i / len(allpads)), 100 + rad * math.sin(TAU * i / len(allpads)))
        by: dict = {}
        for q in allpads:
            if q.net:
                by.setdefault(q.net, []).append(q)
        for net, qs in by.items():
            for a, c in zip(qs, qs[1:]):
                b.tracks.append(Track(a.position, c.position, 0.2, "F.Cu", net))
    return b


def config_of(board, models=None, drop=()) -> Config:
    """Every part's suggested model (as `groundtruth init` writes them), `models` instead where given; parts in
    `drop` with no settings entry (added to the board after the settings were made)."""
    c = Config()
    c.reference_nets = ["GND"]
    for fp in board.footprints:
        if fp.reference not in drop:
            c.components[fp.reference] = ComponentEntry(fp.reference, fp.value, fp.lib_id,
                                                        (models or {}).get(fp.reference) or suggest_model(fp))
    return c


def amp(**prm):
    """An op-amp model, 100 MHz GBW unless given."""
    return ComponentModel("amplifier", {"gbw": 100e6, **prm})


def ideal_cap(c):
    """A capacitor without ESR or ESL (the oracle's)."""
    return ComponentModel("cap", {"C": c, "ESR": 0.0, "ESL": 0.0})


# ------------------------------------------------------------------------------------------------ oracles
def nodal(elements, out, f):
    """An independent MNA solve (numpy): ("R", a, b, Ω) | ("C", a, b, F) | ("V", p, n, V) one source | ("E", p, n,
    cp, cn, gain) a VCVS. Node "0" is ground. V(out) per frequency."""
    nodes = sorted({x for e in elements for x in e[1:5] if isinstance(x, str)} - {"0"})
    idx = {n: k for k, n in enumerate(nodes)}
    branches = [e for e in elements if e[0] in ("V", "E")]
    res = []
    for fr in np.atleast_1d(f):
        s = 1j * TAU * fr
        m = len(nodes) + len(branches)
        A = np.zeros((m, m), complex)
        rhs = np.zeros(m, complex)
        for e in elements:
            if e[0] in ("R", "C"):
                y = 1 / e[3] if e[0] == "R" else s * e[3]
                for p, q, sg in ((e[1], e[1], 1), (e[2], e[2], 1), (e[1], e[2], -1), (e[2], e[1], -1)):
                    if p != "0" and q != "0":
                        A[idx[p], idx[q]] += sg * y
        for k, e in enumerate(branches):
            row = len(nodes) + k
            for node, sg in ((e[1], 1), (e[2], -1)):
                if node != "0":
                    A[idx[node], row] += sg
                    A[row, idx[node]] += sg
            if e[0] == "V":
                rhs[row] = e[3]
            else:
                for node, sg in ((e[3], -1), (e[4], 1)):
                    if node != "0":
                        A[row, idx[node]] += sg * e[5]
        res.append(np.linalg.solve(A, rhs)[idx[out]])
    return np.array(res)


def noninverting(gain_ideal, a0=1e5):
    """A non-inverting stage's gain with an op-amp of open-loop gain a0 [Franco 2015]."""
    beta = 1 / gain_ideal
    return a0 / (1 + a0 * beta)


def simple_routes(roles, start, targets, rails, max_nets=30):
    """Every simple route start → a target as (nets, hop keys), over the parts' roles as the test wrote them (not
    GroundTruth's reading of the board): ("p", a, b, key) a passive either way, ("d", a, b, key) one way."""
    out_edges: dict = {}
    for kind, a, b, key in roles:
        out_edges.setdefault(a, []).append((b, key))
        if kind == "p":
            out_edges.setdefault(b, []).append((a, key))
    found = set()

    def walk(net, nets, keys):
        """Every simple extension of the route ending at `net`."""
        for nxt, key in out_edges.get(net, ()):
            if nxt in nets or key in keys:
                continue
            if nxt in targets:
                found.add((tuple(nets + [nxt]), tuple(keys + [key])))
            elif nxt not in rails and len(nets) + 1 < max_nets:
                walk(nxt, nets + [nxt], keys + [key])
    walk(start, [start], [])
    return found


def gt_routes(pm):
    """GroundTruth's routes in the oracle's terms (a buffer hop by its input pin, as the oracle keys it)."""
    return {(tuple(r.nets), tuple(h.pin_in if h.kind == "buffer" else h.part for h in r.hops)) for r in pm.routes}


def network(parts, driver="U1.1", receivers=("U2.1",), name="SIG_IN", models=None, drop=(), through=(), **spec_kw):
    """(board, config, spec, AnalogNetwork) of a path on these parts."""
    from groundtruth.analysis.analog_net import analog_network
    board = board_of(parts)
    cfg = config_of(board, models, drop)
    spec = NetSpec(name, driver, list(receivers), 1e3, 1e-6, through=list(through), **spec_kw)
    return board, cfg, spec, analog_network(board, cfg, spec, receivers[0])


def h_at(net, f):
    """The circuit's response at its output node."""
    return net.circuit.transfer(np.atleast_1d(np.asarray(f, float)), net.out_node)


# ------------------------------------------------------------------------------- the path's nets (route)
@pytest.mark.parametrize("reverse", [False, True])
def test_a_cascade_through_one_quad_is_followed_through_every_channel(reverse):
    """G-1: IA → U5A → OA → U5B → OB → U5C → OC through one quad: every net is on the path from any of them, in either
    label order (only the first channel was followed: from IA {IA, OA}), and every stage is on the path."""
    from groundtruth.analysis.route import path_nets, signal_nets
    parts = [IC("U1", "IA"), IC("U2", "OC", "input"), REG(),
             QUAD("U5", [("IA", "OA", "OA"), ("OA", "OB", "OB"), ("OB", "OC", "OC")], reverse)]
    board = board_of(parts)
    cfg = config_of(board)
    for n in ("IA", "OA", "OB", "OC"):
        assert signal_nets(board, cfg, n) == {"IA", "OA", "OB", "OC"}
    assert path_nets(board, cfg, "IA", "OC") == {"IA", "OA", "OB", "OC"}
    _, _, _, net = network(parts)
    assert all(net.on_path(nm) for nm, _ in net.opamps) and len(net.opamps) == 3


def test_no_path_runs_backwards_through_an_op_amp_or_a_buffer():
    """G-2: A –R1– B → U3 (+IN B, OUT C) → C, and a buffer C → U4 → D: from D or C back to A there is no path (an
    output drives no input); find_route says so and which way the signal goes."""
    from groundtruth.analysis.route import RouteError, find_route, path_nets
    parts = [IC("U1", "A"), IC("U2", "D", "input"), R("R1", "A", "B", "100"), OPA("U3", "B", "C", "C"),
             BUF("U4", [("C", "D")]), REG()]
    board = board_of(parts, routed=True)
    cfg = config_of(board)
    assert path_nets(board, cfg, "A", "D") == {"A", "B", "C", "D"}
    assert path_nets(board, cfg, "D", "A") == set() and path_nets(board, cfg, "C", "A") == set()
    assert find_route(board, cfg, "A", "U1.1", "U2.1").parts == ["R1", "U3", "U4"]
    with pytest.raises(RouteError, match="swap the driver and the receiver"):
        find_route(board, cfg, "D", "U2.1", "U1.1")


@pytest.mark.parametrize("r12", ["0", "1k", "10k"])
def test_a_bias_follower_beside_the_path_is_not_on_it(r12):
    """G-3 / E-6: C1 (Pass through) → X → follower U3 → OUT; X biased through R12 from VB, the output of follower U4
    whose input is a divider: U4 is in the circuit for its loading, never on the path, whatever R12 is (through 0R or
    1k it was, and its output and loop gain were plotted)."""
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), C("C1", "SIG_IN", "X"), R("R12", "VB", "X", r12),
             OPA("U3", "X", "OUT", "OUT"), OPA("U4", "VBDIV", "VB", "VB"), R("R13", "+5V", "VBDIV", "10k"),
             R("R14", "VBDIV", "GND", "10k"), REG()]
    _, _, _, net = network(parts, through=["C1"], models={"U3": amp(), "U4": amp()})
    assert net.on_path("U3") and not net.on_path("U4")
    assert {"U3", "U4"} == {nm for nm, _ in net.opamps}


def test_reference_and_monitor_followers_are_not_on_the_path():
    """G-3: a reference follower (its + input on a supply, its output tied to the path by 0R) and a monitor follower
    (its input on the path, its output to a connector): neither carries the signal to the receiver."""
    parts = [IC("U1", "A"), IC("U2", "C", "input"), R("R1", "A", "B", "100"), OPA("U5", "B", "C", "C"),
             OPA("U6", "+2V5", "BIAS", "BIAS"), R("R9", "BIAS", "B", "0"), OPA("U7", "B", "MON", "MON"),
             part("J1", [(1, "MON", "passive", ""), (2, "GND", "passive", "")], "CONN"), REG(), REG("U91", "+2V5")]
    _, _, _, net = network(parts, name="A")
    assert net.on_path("U5") and not net.on_path("U6") and not net.on_path("U7")
    assert "MON" not in net.path_nets


def test_a_traced_route_through_a_dual_or_an_inverting_stage_can_be_routed():
    """G-4: a dual op-amp told by its pin names (no labels), and an inverting stage whose + input is on a bias net:
    the route the two-pad trace finds is one find_route follows (they told channels apart differently)."""
    from groundtruth.analysis.path_map import proposal_for_route, routes_between
    from groundtruth.analysis.route import find_route
    dual = part("U3", [(3, "B", "input", "+INA"), (2, "C", "input", "-INA"), (1, "C", "output", "OUTA"),
                       (5, "Y", "input", "+INB"), (6, "Z", "input", "-INB"), (7, "Z", "output", "OUTB"),
                       (8, "+5V", "power_in", "V+"), (4, "GND", "power_in", "V-")], "DUAL")
    inv = [IC("U1", "A"), IC("U2", "O", "input"), R("R10", "A", "N", "1k"), R("R11", "N", "O", "10k"),
           OPA("U3", "MID", "N", "O"), R("R20", "+5V", "MID", "10k"), R("R21", "MID", "GND", "10k"), REG()]
    for parts, models in (([IC("U1", "A"), IC("U2", "C", "input"), R("R1", "A", "B", "100"), dual, REG()],
                           {"U3": amp()}), (inv, None)):     # a dual by its pin names: the user models it
        board = board_of(parts, routed=True)
        cfg = config_of(board, models)
        start, end, routes = routes_between(board, cfg, "U1.1", "U2.1")
        prop = proposal_for_route(board, cfg, routes[0], start, end)
        for ref, m in prop.models.items():
            cfg.components[ref].model = m
        route = find_route(board, cfg, prop.spec.name, prop.spec.driver, prop.spec.receivers[0], prop.spec.through)
        assert "U3" in route.parts


def test_a_path_never_runs_through_a_supply():
    """G-5: A –330 Ω– +3V3 (a regulator's output) –330 Ω– C: no path through the supply, nor further along it."""
    from groundtruth.analysis.route import RouteError, find_route, signal_nets
    parts = [IC("U1", "A"), IC("U2", "C", "input"), R("R1", "A", "+3V3", "330"), R("R2", "+3V3", "C", "330"),
             REG("U9", "+3V3"), part("FB1", [(1, "+3V3", "passive", ""), (2, "+3V3A", "passive", "")], "BLM"),
             R("R3", "+3V3A", "D", "10")]
    board = board_of(parts, routed=True)
    cfg = config_of(board)
    assert signal_nets(board, cfg, "A") == {"A"}
    with pytest.raises(RouteError, match=r"\+3V3"):
        find_route(board, cfg, "A", "U1.1", "U2.1")


def test_the_copper_route_keeps_to_its_net_rather_than_detour_through_parts():
    """G-11: the driver and receiver on net A, with R1 and R2 from A into B and back: the route is A's own copper,
    no parts (by length alone it could pass both resistors)."""
    from groundtruth.analysis.route import find_route
    parts = [IC("U1", "A"), R("R1", "A", "B", "100"), R("R2", "B", "A", "100"), IC("U2", "A", "input")]
    board = board_of(parts, routed=True)
    route = find_route(board, config_of(board), "A", "U1.1", "U2.1")
    assert route.parts == []


def test_a_part_not_fitted_passes_nothing():
    """G-17: a DNP 0R between the ends: no path in signal_nets, path_nets or the route map."""
    from groundtruth.analysis.path_map import trace
    from groundtruth.analysis.route import path_nets, signal_nets
    parts = [IC("U1", "A"), IC("U2", "B", "input"), R("R1", "A", "B", "0", dnp=True)]
    board = board_of(parts)
    cfg = config_of(board, {"R1": ComponentModel("short")})
    assert signal_nets(board, cfg, "A") == {"A"} and path_nets(board, cfg, "A", "B") == set()
    assert trace(board, cfg, "U1.1", ["U2.1"]).routes == []


# ------------------------------------------------------------------------------- the route map (path_map)
def grid(n, direct_last=True, reachable=True):
    """An n×n mesh of 100 Ω resistors from U1 (corner G0_0) to U2 (corner G{n-1}_{n-1}, or a net no part reaches)
    and a resistor R0 straight from the start to the end, listed last (or first)."""
    parts = [IC("U1", "G0_0")]
    k = 0
    for i in range(n):
        for j in range(n):
            for di, dj in ((0, 1), (1, 0)):
                if i + di < n and j + dj < n:
                    k += 1
                    parts.append(R(f"R{k}", f"G{i}_{j}", f"G{i + di}_{j + dj}", "100"))
    end = f"G{n - 1}_{n - 1}" if reachable else "FAR"
    parts.append(IC("U2", end, "input"))
    direct = R("R0", "G0_0", end, "100")
    return parts + [direct] if direct_last else [direct] + parts


def test_the_route_cap_keeps_the_shortest_routes():
    """G-6: a 5×5 mesh with a resistor straight across listed last: at 50 routes the first is that 1-hop route (it
    was missing, the first had 16 hops), and the routes kept are the 50 shortest the oracle finds."""
    from groundtruth.analysis.path_map import trace
    parts = grid(5)
    board = board_of(parts)
    pm = trace(board, config_of(board), "U1.1", ["U2.1"], max_routes=50, branches=False)
    assert len(pm.routes[0].hops) == 1 and pm.routes[0].hops[0].part == "R0"
    roles = [("p", p["pads"][0][1], p["pads"][1][1], p["ref"]) for p in parts if p["ref"].startswith("R")]
    lengths = sorted(len(r[1]) for r in simple_routes(roles, "G0_0", {"G4_4"}, set()))
    assert [len(r.hops) for r in pm.routes] == lengths[:50]
    assert any("stopped after 50 routes" in n for n in pm.notes)


def test_an_end_no_route_reaches_is_known_at_once():
    """G-7: an end that can't be reached in a 7×7 mesh (2 × 10¹⁰ simple paths to explore before): no route, at once."""
    from groundtruth.analysis.path_map import trace
    board = board_of(grid(7, reachable=False)[:-1])
    t0 = time.perf_counter()
    pm = trace(board, config_of(board), "U1.1", ["U2.1"])
    assert pm.routes == [] and time.perf_counter() - t0 < 5.0


def test_a_connector_or_a_two_net_ic_is_no_series_part():
    """G-8: a 2-pin connector and an IC with two nets connected join no nets (their pins are not joined inside);
    a resistor named by its role (RIN) still does."""
    from groundtruth.analysis.path_map import trace
    for mid in (part("J1", [(1, "A", "passive", ""), (2, "B", "passive", "")], "CONN"),
                part("U4", [(1, "A", "input", "IN"), (2, "B", "output", "OUT")], "IC")):
        board = board_of([IC("U1", "A"), mid, IC("U2", "B", "input")])
        assert trace(board, config_of(board), "U1.1", ["U2.1"]).routes == []
    board = board_of([IC("U1", "A"), R("RIN", "A", "B"), IC("U2", "B", "input")])
    assert [h.part for h in trace(board, config_of(board), "U1.1", ["U2.1"]).routes[0].hops] == ["RIN"]


@pytest.mark.parametrize("mid", ["Net-(U3-VOUT)", "DAC_VOUT", "VIN_SENSE"])
def test_a_signal_named_like_a_supply_is_no_barrier(mid):
    """G-9: KiCad's automatic name after a VOUT pin, a DAC's output, a sensed input: signals, not rails."""
    from groundtruth.analysis.path_map import trace
    board = board_of([IC("U1", "A"), R("R1", "A", mid), R("R2", mid, "B"), IC("U2", "B", "input")])
    assert [h.part for h in trace(board, config_of(board), "U1.1", ["U2.1"]).routes[0].hops] == ["R1", "R2"]


def test_two_channels_of_one_buffer_follow_each_other():
    """G-10: A → U4 (channel 1) → B → U4 (channel 2) → C is a route (one package's channels were one hop)."""
    from groundtruth.analysis.path_map import trace
    board = board_of([IC("U1", "A"), BUF("U4", [("A", "B"), ("B", "C")]), IC("U2", "C", "input"), REG()])
    pm = trace(board, config_of(board), "U1.1", ["U2.1"])
    assert [(h.part, h.pin_in) for h in pm.routes[0].hops] == [("U4", "U4.1"), ("U4", "U4.3")]


def _random_board(rng):
    """A random small board and its parts' roles for the oracle: resistors and capacitors between random nets
    (some to GND), single op-amps (by pin names, an input sometimes on the output: a follower), single-channel
    buffers, 3-net ICs (pass nothing), 5 % DNP; +5V a regulator's output."""
    nets = [f"N{k}" for k in range(rng.randint(3, 10))]
    parts = [IC("U1", nets[0]), IC("U2", nets[-1], "input"), REG()]
    roles = []
    for k in range(rng.randint(3, 18)):
        kind = rng.random()
        if kind < 0.6:
            a, b = rng.sample(nets + ["GND"], 2)
            dnp = rng.random() < 0.05
            parts.append(R(f"R{k}", a, b, rng.choice(["100", "10k"]), dnp=dnp) if rng.random() < 0.6
                         else dict(C(f"C{k}", a, b), dnp=dnp))
            if not dnp:
                roles.append(("p", a, b, parts[-1]["ref"]))
        elif kind < 0.8:
            inp, out = rng.sample(nets, 2)
            inn = out if rng.random() < 0.5 else rng.choice(nets)
            parts.append(OPA(f"U{10 + k}", inp, inn, out))
            roles += [("d", inp, out, f"U{10 + k}")] + ([("d", inn, out, f"U{10 + k}")] if inn not in (inp, out)
                                                         else [])
        elif kind < 0.9:
            a, b = rng.sample(nets, 2)
            parts.append(BUF(f"U{10 + k}", [(a, b)]))
            roles.append(("d", a, b, f"U{10 + k}.1"))
        else:
            a, b, c = rng.sample(nets, 3)
            parts.append(part(f"U{10 + k}", [(1, a, "input", "A"), (2, b, "input", "B"), (3, c, "output", "Y")]))
    return parts, roles, nets


def test_every_route_against_an_independent_enumerator():
    """G-6/G-10/G-12 and the A81 rules on 150 seeded random boards: GroundTruth's routes are exactly the simple paths
    an enumerator over the parts' own roles finds (no extra, none missing, no duplicates), shortest first; with a
    cap of 5, the 5 shortest."""
    from groundtruth.analysis.path_map import trace
    compared = 0
    for seed in range(150):
        parts, roles, nets = _random_board(random.Random(seed))
        board = board_of(parts)
        cfg = config_of(board)
        want = simple_routes(roles, nets[0], {nets[-1]}, {"GND", "+5V"})
        pm = trace(board, cfg, "U1.1", ["U2.1"], max_routes=10_000, branches=False)
        got = gt_routes(pm)
        assert got == want, seed
        assert len(got) == len(pm.routes)                                   # no duplicates
        assert [len(r.hops) for r in pm.routes] == sorted(len(r.hops) for r in pm.routes)
        capped = trace(board, cfg, "U1.1", ["U2.1"], max_routes=5, branches=False)
        assert [len(r.hops) for r in capped.routes] == sorted(len(w[1]) for w in want)[:5], seed
        compared += bool(want)
    assert compared > 60


def test_the_default_route_does_not_depend_on_the_files_order():
    """G-13: the same boards with their parts in reverse order: the same routes in the same order."""
    from groundtruth.analysis.path_map import trace
    for seed in range(60):
        parts, _, _ = _random_board(random.Random(seed))
        texts = []
        for order in (parts, parts[::-1]):
            board = board_of(order)
            pm = trace(board, config_of(board), "U1.1", ["U2.1"], branches=False)
            texts.append([(tuple(r.nets), tuple(h.part for h in r.hops)) for r in pm.routes])
        assert texts[0] == texts[1], seed


def test_a_long_chain_and_an_unconnected_end():
    """G-14 / G-15: a chain of 1200 resistors with max_nets 2000 (a recursion limit before); an end on a pad with no
    net is said as such."""
    from groundtruth.analysis.path_map import trace
    parts = [IC("U1", "C0")] + [R(f"R{k}", f"C{k}", f"C{k + 1}", "100") for k in range(1200)] + \
        [IC("U2", "C1200", "input"), part("U3", [(1, "", "input", "IO")])]
    board = board_of(parts)
    pm = trace(board, config_of(board), "U1.1", ["U2.1"], max_nets=2000, branches=False)
    assert len(pm.routes) == 1 and len(pm.routes[0].hops) == 1200
    pm = trace(board, config_of(board), "U1.1", ["U3.1"], branches=False)
    assert "U3.1: the pad is on no net (unconnected)" in pm.notes


# ------------------------------------------------------------------------------- the path's circuit
@pytest.mark.parametrize("mid,extra", [("DIV_MID", None), ("VBAT_DIV", None), ("+VSENSE", None),
                                       ("VREF_ADC", None), ("DIV_MID", "power_in")])
def test_a_net_of_the_path_is_never_ac_ground(mid, extra):
    """E-1: U1.1 → R1 100k (Pass through) → mid → R3 1k → U2.1, R2 10k from mid to GND, C1 100 nF at the receiver:
    the DC gain R2/(R_s + R1 + R2) and the 1 kHz response as the nodal solve has them, whether mid is named like a
    rail or an IC's power_in pin is on it (the signal was shorted: 0, −6000 dB). A note names a supply-like net."""
    parts = [IC("U1", "IN_NODE"), IC("U2", "ADC_IN", "input"), R("R1", "IN_NODE", mid, "100k"),
             R("R2", mid, "GND", "10k"), R("R3", mid, "ADC_IN", "1k"), C("C1", "ADC_IN", "GND", "100n")]
    if extra:                       # a pin of type power_in not named as a supply (a gate driver's GNDB)
        parts.append(part("U7", [(1, mid, extra, "SNS"), (2, "GND", "power_in", "GND")], "IC"))
    _, _, _, net = network(parts, name="IN_NODE", through=["R1"], load_pf=0.0, models={"C1": ideal_cap(100e-9)})
    want = nodal([("V", "s", "0", 1.0), ("R", "s", "i", R_S), ("R", "i", "m", 100e3), ("R", "m", "0", 10e3),
                  ("R", "m", "a", 1e3), ("C", "a", "0", 100e-9)], "a", [1.0, 1e3])
    assert h_at(net, [1.0, 1e3]) == pytest.approx(want, rel=1e-6)
    assert (extra is not None) == any("runs through" in n for n in net.notes)


def test_a_power_out_net_on_the_way_is_no_route():
    """E-1, a regulator's output in the middle: AC ground for a signal; find_route says the path runs only through
    it (not a silent zero response)."""
    from groundtruth.analysis.route import RouteError, find_route
    parts = [IC("U1", "A"), IC("U2", "C", "input"), R("R1", "A", "MID", "100"), R("R2", "MID", "C", "100"),
             REG("U9", "MID")]
    board = board_of(parts, routed=True)
    with pytest.raises(RouteError, match="only through MID"):
        find_route(board, config_of(board), "A", "U1.1", "U2.1")


@pytest.mark.parametrize("vm", ["VMID", "VREF_MID", "+BIAS"])
def test_a_divider_tap_named_like_a_rail_is_a_node(vm):
    """E-2: C1 100 nF (Pass through) → X → follower → OUT, X biased by R12 10k from a divider R13 / R14 10k off +5V:
    the AC coupling's corner 1/(2π·C1·(R_s + R12 + R13 ∥ R14)), whatever the divider's tap is named (VREF_MID was
    grounded: R12 alone, 159 Hz for 106 Hz)."""
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), C("C1", "SIG_IN", "X"), R("R12", "X", vm, "10k"),
             R("R13", "+5V", vm, "10k"), R("R14", vm, "GND", "10k"), OPA("U3", "X", "OUT", "OUT"), REG()]
    _, _, _, net = network(parts, through=["C1"], models={"U3": amp()}, load_pf=0.0)
    f = [10.0, 106.0, 1e3]
    want = nodal([("V", "s", "0", 1.0), ("R", "s", "i", R_S), ("C", "i", "x", 100e-9), ("R", "x", "v", 10e3),
                  ("R", "v", "0", 10e3), ("R", "v", "0", 10e3)], "x", f)
    assert np.abs(h_at(net, f)) == pytest.approx(np.abs(want), rel=2e-3)


def test_an_op_amp_driving_the_path_is_its_source():
    """E-3: the driver pin is a follower's output (U3.1): the source stands for it, so the response is the RC's,
    1/(1 + jω(R_s + R1)C1), not the source fighting the op-amp (−88 dB); a note says so."""
    parts = [IC("U2", "ADC", "input"), OPA("U3", "BIAS", "OUT_A", "OUT_A"), R("R5", "+5V", "BIAS", "10k"),
             R("R6", "BIAS", "GND", "10k"), R("R1", "OUT_A", "ADC", "1k"), C("C1", "ADC", "GND", "100n"), REG()]
    _, _, _, net = network(parts, driver="U3.1", name="OUT_A", models={"U3": amp()}, load_pf=0.0)
    f = np.array([10.0, 1e3, 1e5])
    assert np.abs(h_at(net, f)) == pytest.approx(np.abs(1 / (1 + 1j * TAU * f * (R_S + 1e3) * 100e-9)), rel=1e-3)
    assert any("the source stands for it" in n for n in net.notes)


def _dual(pins=()):
    """A dual op-amp told by its pin names, channel A a non-inverting gain of 10 (R10 1k to GND, R11 9k)."""
    return [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), R("R10", "FB", "GND", "1k"), R("R11", "FB", "OUT", "9k"),
            part("U3", [(3, "SIG_IN", "input", "+INA"), (2, "FB", "input", "-INA"), (1, "OUT", "output", "OUTA"),
                        (5, "Y", "input", "+INB"), (6, "Z", "input", "-INB"), (7, "Z", "output", "OUTB"),
                        (8, "+5V", "power_in", "V+"), (4, "GND", "power_in", "V-")], "DUAL"), REG()]


@pytest.mark.parametrize("pins", [[], ["3", "1"], ["+INA", "OUTA"]])
def test_pins_on_a_dual_keep_its_feedback(pins):
    """E-4: `pins: [in, out]` on a dual (the documented way to name the channel a route passes) takes IN− from the
    same channel: the gain stays 10 (it became a unity gain block, −20 dB)."""
    m = amp()
    m.pins = list(pins)
    _, _, _, net = network(_dual(), models={"U3": m})
    assert abs(h_at(net, 1.0)[0]) == pytest.approx(noninverting(10.0), rel=1e-3)
    assert all(kind == "macromodel" for _, kind in net.opamps)


def test_the_circuits_size_limit_never_cuts_the_feedback():
    """E-5: a gain-10 stage with 60 loads (10k + 10k each) on its output, listed before its feedback resistors: past
    MAX_NETS the loads are left out (a note says so), never the feedback net (open loop: 76 794)."""
    loads = []
    for k in range(60):
        loads += [R(f"RA{k}", "OUT", f"L{k}", "10k"), R(f"RB{k}", f"L{k}", "GND", "10k")]
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input")] + loads + \
        [OPA("U3", "SIG_IN", "FB", "OUT"), R("R10", "FB", "GND", "1k"), R("R11", "FB", "OUT", "9k"), REG()]
    _, _, _, net = network(parts, models={"U3": amp()})
    assert "FB" in net.nets and {"R10", "R11"} <= set(net.parts)
    assert abs(h_at(net, 1.0)[0]) == pytest.approx(noninverting(10.0), rel=2e-3)
    assert any("the circuit stops at" in n and "may be wrong" in n for n in net.notes)


def test_a_long_series_chain_keeps_its_receiver():
    """E-5: 65 series 100 Ω resistors (0R-like, they pass a signal) to the receiver, with a 10k load at its end: all
    on the path, so none is cut (the receiver's net was): DC R_L/(R_s + 6500 + R_L)."""
    parts = [IC("U1", "C0"), IC("U2", "C65", "input"), R("R99", "C65", "GND", "10k")] + \
        [R(f"R{k}", f"C{k}", f"C{k + 1}", "100") for k in range(65)]
    _, _, _, net = network(parts, name="C0")
    assert abs(h_at(net, 1.0)[0]) == pytest.approx(10e3 / (R_S + 6500 + 10e3), rel=1e-6)


def test_parts_left_out_of_the_circuit_are_named():
    """E-7 / E-8: a resistor array (a resistor model on more than two nets), a feedback resistor with no settings
    entry and an analog switch ('open') are left out with a note each, and the op-amp left without DC feedback is
    named (it read 99 504 in silence)."""
    rn = part("RN1", [(1, "SIG_IN", "passive", ""), (8, "M", "passive", ""), (2, "M", "passive", ""),
                      (7, "GND", "passive", ""), (3, "P3", "passive", ""), (6, "P6", "passive", "")], "10k")
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), rn, OPA("U3", "M", "N", "OUT"), R("R11", "N", "OUT", "9k"),
             R("R10", "N", "GND", "1k"), part("U8", [(1, "N", "passive", "S"), (2, "SWA", "passive", "D"),
                                                     (3, "CTL", "input", "IN")], "SWITCH"), REG()]
    _, _, _, net = network(parts, models={"U3": amp(), "RN1": ComponentModel("resistor", {"R": 10e3})},
                           drop=("R11",), through=["RN1"])
    text = " ".join(net.notes)
    assert "RN1 (a resistor on" in text and "R11 (no settings entry)" in text and "U8 (model open)" in text
    assert "U3: no DC path from the output back to IN−" in text


def test_channels_of_a_dual_named_by_digits_and_a_part_u31():
    """E-9: a dual U3 whose pins are named by digits (+IN1, OUT2 ...) and a single op-amp U31, three followers in a
    row: three channels U3.1, U31, U3.2, the gain 1 (U3's "U31" took U31's place: 0)."""
    dual = part("U3", [(3, "SIG_IN", "input", "+IN1"), (2, "M1", "input", "-IN1"), (1, "M1", "output", "OUT1"),
                       (5, "M2", "input", "+IN2"), (6, "OUT", "input", "-IN2"), (7, "OUT", "output", "OUT2"),
                       (8, "+5V", "power_in", "V+"), (4, "GND", "power_in", "V-")], "DUAL")
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), dual, OPA("U31", "M1", "M2", "M2"), REG()]
    _, _, _, net = network(parts, models={"U3": amp(), "U31": amp()})
    assert sorted(nm for nm, _ in net.opamps) == ["U3.1", "U3.2", "U31"]
    assert abs(h_at(net, 1.0)[0]) == pytest.approx(noninverting(1.0) ** 3, rel=1e-3)


def test_every_receiver_carries_the_load():
    """E-10: R1 1k → A (U2.1) → R2 1k → B (U5.1), load 1k at each receiver: the same circuit whichever receiver is
    analysed, as the nodal solve with both loads has it (+25 % / +68 % before)."""
    from groundtruth.analysis.analog_net import analog_network
    parts = [IC("U1", "SIG_IN"), IC("U2", "A", "input"), IC("U5", "B", "input"), R("R1", "SIG_IN", "A", "1k"),
             R("R2", "A", "B", "1k")]
    board = board_of(parts)
    cfg = config_of(board)
    spec = NetSpec("SIG_IN", "U1.1", ["U2.1", "U5.1"], 1e3, 1e-6, load_ohms=1e3, load_pf=0.0)
    for rx, node in (("U2.1", "a"), ("U5.1", "b")):
        net = analog_network(board, cfg, spec, rx)
        want = nodal([("V", "s", "0", 1.0), ("R", "s", "i", R_S), ("R", "i", "a", 1e3), ("R", "a", "b", 1e3),
                      ("R", "a", "0", 1e3), ("R", "b", "0", 1e3)], node, [1.0])
        assert h_at(net, 1.0) == pytest.approx(want, rel=1e-6)


def test_a_part_not_fitted_is_left_out_of_the_circuit():
    """E-11: a DNP shunt with a model of its own (marked DNP after the settings were made) is not in the circuit, as
    the DC bias and the shunts leave it out; a note names it."""
    parts = [IC("U1", "SIG_IN"), IC("U2", "OUT", "input"), R("R1", "SIG_IN", "OUT", "1k"),
             R("R2", "OUT", "GND", "1k", dnp=True)]
    _, _, _, net = network(parts, models={"R2": ComponentModel("resistor", {"R": 1e3})}, load_pf=0.0)
    assert "R2" not in net.parts and "not fitted (DNP), left out: R2" in net.notes
    assert abs(h_at(net, 1.0)[0]) == pytest.approx(1.0, rel=1e-6)


def test_a_vias_pieces_add_up_to_the_whole_barrel():
    """E-12: a via through four layers split per layer pair: its pieces' L add up to the whole barrel's partial
    inductance [Grover 1946] (each piece's own L summed left out the mutual terms: 0.43 for 0.63 nH)."""
    from groundtruth.analysis.analog_net import via_piece
    from groundtruth.solver.peec import via_impedance
    zs = {"F.Cu": 0.0175, "In1.Cu": 0.2525, "In2.Cu": 1.2875, "B.Cu": 1.5225}
    layers = list(zs)
    pieces = [via_piece(layers, a, b, zs, 0.3) for a, b in zip(layers, layers[1:])]
    whole = via_impedance((zs["B.Cu"] - zs["F.Cu"]) * 1e-3, 0.3e-3)
    assert sum(l for _, l in pieces) == pytest.approx(whole(1e6).imag / (TAU * 1e6), rel=1e-9)
    assert sum(r for r, _ in pieces) == pytest.approx(whole(0.0).real, rel=1e-9)
