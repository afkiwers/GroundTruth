"""One Simulate split over several compute servers (the user, 2026-10-14: "can we have multiple servers (cluster) to
do the sim faster?"; the goal: one Simulate faster).

A Simulate is made of independent items: each signal net, each power stage, each motor bridge has models of its
own; only the rule checks and the stages' DC paths together (combine_dc) look at all of them. So the split is per
item (remote/split.py plans and runs it): every part is an ordinary Simulate of some items (`simulate(part=)`),
one of them also runs every rule check of the job, and `merge_parts` joins the parts into the result the whole run
would have given: routes, stages and bridges in the settings' order, the issues collected the same way, and the
stages' DC paths combined from what each part sent (power.DcBoard: every part builds the same whole-board DC model
from the same board and settings, which its fingerprint checks).

What can differ from one run: the models and their frequencies are each item's own either way, but which
factorisation preconditions a frequency (PlaneStack._plan, the ±25 % borrow) depends on the other items solved in
the same process, so GMRES ends at a slightly different point within its tolerance (rtol 1e-5): figures agree to
that, not bit for bit (tests/test_split.py compares with a tolerance). Nothing here needs the board.
"""
from __future__ import annotations

from typing import Optional

from ..config.settings import Config
from .simulate import SimulationResult, finish_issues

# relative cost of the items, for spreading them over the servers before measured speeds exist: a route per
# receiver; a power stage solves a hot loop, its DC paths and an input path (on a large motor-drive board ~670k-node models, a net's
# are far smaller); a bridge is its legs plus the drive's receiver
COST_ROUTE = 1.0
COST_STAGE = 6.0
COST_BRIDGE_LEG = 6.0
# an item's peak memory before it was measured: per million nodes of its models' budget (analysis.max_nodes x the
# resolution; the board-wide plane model's cavity_max_nodes x the resolution for a net with plane capacitance).
# refboard's conv1 at 16x (budget 1.6 M, 1.18 M nodes) used ~9 GB on 4 threads on build_pc: ~6 GB per M
GB_PER_MNODE = 6.0
FIT_SHARE = 0.8                 # an item goes to a server only if its memory is within this share of the free memory


def split_items(config: Config, nets: Optional[list] = None) -> list[dict]:
    """The items a Simulate of `nets` (None: everything switched on) is made of, each {name, kind, cost, mem_gb}:
    the nets, power stages and motor bridges simulate would take, in the settings' order, with their relative cost
    and their estimated peak memory (GB_PER_MNODE x their models' node budget). A bridge is one item (its legs and
    its drive are finished together); a leg named alone stands for its bridge, as in simulate."""
    cfg = config.active(nets)
    a = cfg.analysis
    named = set(nets or ())
    budget = a.max_nodes * a.resolution / 1e6                   # one model's node budget, million nodes
    cavity = (a.cavity_max_nodes * a.resolution / 1e6) if getattr(a, "plane_capacitance", False) else 0.0
    out = []
    for n in cfg.nets:
        if not nets or n.name in named:
            out.append({"name": n.name, "kind": "net", "cost": COST_ROUTE * max(1, len(n.receivers)),
                        "mem_gb": GB_PER_MNODE * (budget + cavity)})
    for c in cfg.converters:
        if not nets or c.name in named:
            out.append({"name": c.name, "kind": "stage", "cost": COST_STAGE, "mem_gb": GB_PER_MNODE * budget})
    for b in cfg.bridges:
        if not nets or b.name in named or any(b.leg_name(leg) in named for leg in b.legs):
            out.append({"name": b.name, "kind": "bridge", "cost": COST_BRIDGE_LEG * max(1, len(b.legs)) + COST_ROUTE,
                        "mem_gb": GB_PER_MNODE * budget * max(1, len(b.legs))})
    for d in getattr(cfg, "dc_loads", ()):        # a DC rail: one DC solve on the whole-board DC model (3x budget)
        if not nets or d.name in named:
            out.append({"name": d.name, "kind": "dcload", "cost": COST_ROUTE, "mem_gb": GB_PER_MNODE * budget * 3})
    for d in getattr(cfg, "pdn", ()):             # a PDN rail: ~30 solves of a whole-board model (the cavity budget)
        if not nets or d.label in named:
            out.append({"name": d.label, "kind": "pdn", "cost": COST_ROUTE * 3,
                        "mem_gb": GB_PER_MNODE * max(cavity, a.cavity_max_nodes * a.resolution / 1e6)})
    return out


SPEED_CORES = 8                 # cores beyond this don't make one Simulate faster (its solves use ~4 threads)


def server_speed(cores, busy: int = 0, measured: Optional[float] = None) -> float:
    """A server's relative speed for a share of a split, divided by the jobs already running or queued there (its
    share waits behind them): `measured` (the work it got through per second in earlier parts, remote/split.Stats)
    when known, else its cores up to SPEED_CORES. Not its cores alone: a Simulate's solves run on about 4 threads
    (analysis.threads auto), so a 16-core server is not twice an 8-core one for it; counting all cores sent both of
    the user's two power stages to the 16-core lead and none to the 8-core member."""
    base = measured if measured else min(float(cores or 1), SPEED_CORES)
    return base / (1 + max(0, int(busy)))


def fits(item: dict, server: dict) -> bool:
    """Whether a server has the memory for an item: its estimated (or measured) peak within FIT_SHARE of the
    server's free memory (unknown free memory: yes). The user: "scale the work load depending on the machines
    capabilities"; an item can't be split further, so a server too small for it never gets it (a 16x power stage
    of ~9 GB on a laptop with 2 GB free was stopped by its memory watchdog and redone on the lead)."""
    free = server.get("free_gb")
    return free is None or float(item.get("mem_gb") or 0.0) <= FIT_SHARE * float(free)


def plan_split(items: list[dict], servers: list[dict]) -> list[list[dict]]:
    """The items spread over `servers` ([{"speed": server_speed(...), "busy": jobs there, "free_gb": free memory},
    ...], the first is the one splitting) by what each can do: an item only to a server it fits (memory, `fits`;
    when it fits none, to the server with the most free memory); the costliest first, each to the fitting server that
    would then finish soonest (load / speed: greedy "longest processing time first", within 4/3 of the best). Then
    every idle server (busy 0) still without an item gets one it fits from the server finishing last that has two or
    more: a server waiting idle never helps. One list per server (some may be empty: fewer items than servers, busy
    servers, or servers too small for every item)."""
    loads = [0.0] * len(servers)
    out: list[list[dict]] = [[] for _ in servers]
    speeds = [max(float(s.get("speed") or 1.0), 1e-3) for s in servers]
    biggest = max(range(len(servers)), key=lambda j: (float(servers[j].get("free_gb") or 0.0), -j))
    for it in sorted(items, key=lambda i: -float(i["cost"])):
        able = [j for j, s in enumerate(servers) if fits(it, s)] or [biggest]
        k = min(able, key=lambda j: ((loads[j] + it["cost"]) / speeds[j], j))
        loads[k] += it["cost"]
        out[k].append(it)
    for k, s in enumerate(servers):
        if out[k] or int(s.get("busy") or 0):
            continue
        offers = [(j, it) for j in range(len(servers)) if len(out[j]) >= 2 for it in out[j] if fits(it, s)]
        if not offers:
            continue
        # from the server finishing last, its smallest item that fits here (the donor keeps its big ones)
        j, it = min(offers, key=lambda o: (-loads[o[0]] / speeds[o[0]], float(o[1]["cost"])))
        out[j].remove(it)
        out[k].append(it)
        loads[j] -= it["cost"]
        loads[k] += it["cost"]
    return out


def merge_parts(config: Config, nets: Optional[list], parts: list[SimulationResult],
                notes: Optional[list] = None, board=None) -> SimulationResult:
    """The whole Simulate from its parts (see the module): the rule checks of the part that ran them all, every
    part's routes, stages and bridges in the settings' order, the notes once each (and `notes`, about the split),
    the solver summaries together, the stages' DC paths combined and the issues finished as simulate does."""
    cfg = config.active(nets)
    main = next((p for p in parts if p.part and p.part.get("checks")), None)
    if main is None:
        raise ValueError("no part ran the rule checks: the split can't be merged")
    result = SimulationResult(main.checks)
    # the order a single run gives: routes as the checks traced them (net, receiver), stages as configured then the
    # bridges' legs, bridges as configured
    route_order = {k: n for n, k in enumerate(main.checks.routes)}
    stage_order = {name: n for n, name in enumerate([c.name for c in cfg.converters] +
                                                     [s.name for b in cfg.bridges for s in b.stages()])}
    bridge_order = {b.name: n for n, b in enumerate(cfg.bridges)}
    result.routes = sorted((r for p in parts for r in p.routes),
                           key=lambda r: route_order.get((r.net, r.receiver), len(route_order)))
    result.converters = sorted((r for p in parts for r in p.converters),
                               key=lambda r: stage_order.get(r.net, len(stage_order)))
    result.bridges = sorted((r for p in parts for r in p.bridges),
                            key=lambda r: bridge_order.get(r.net, len(bridge_order)))
    load_order = {d.name: n for n, d in enumerate(getattr(cfg, "dc_loads", ()))}
    result.dc_loads = sorted((r for p in parts for r in getattr(p, "dc_loads", ())),
                             key=lambda r: load_order.get(r.net, len(load_order)))
    pdn_order = {d.label: n for n, d in enumerate(getattr(cfg, "pdn", ()))}
    result.pdn = sorted((r for p in parts for r in getattr(p, "pdn", ())),
                        key=lambda r: pdn_order.get(r.name, len(pdn_order)))
    for text in [n for p in parts for n in p.notes] + list(notes or []):
        if text not in result.notes:
            result.notes.append(text)
    result.groups = max((p.groups for p in parts), default=1)
    devices = []
    for p in parts:
        d = (p.solver or {}).get("device")
        if d and d not in devices:
            devices.append(d)
    result.solver = {"device": " + ".join(devices) or "CPU",
                     "gpu_fallbacks": sum(int((p.solver or {}).get("gpu_fallbacks") or 0) for p in parts),
                     "gpu_error": next(((p.solver or {}).get("gpu_error") for p in parts
                                        if (p.solver or {}).get("gpu_error")), "")}
    # the build the parts ran on (a cluster's servers all run the same code: protocol.code_id)
    build = next(((p.solver or {}).get("build") for p in parts if (p.solver or {}).get("build")), "")
    if build:
        result.solver["build"] = build
    issues = list(main.checks.issues) + [i for p in parts for i in p.part.get("issues", [])]
    route_issues = [i for p in parts for i in p.part.get("route_issues", [])]
    stage_issues = [i for p in parts for i in p.part.get("stage_issues", [])]
    result.issues = finish_issues(result, issues, route_issues, stage_issues,
                                  list(result.converters) + list(result.dc_loads), cfg, board)
    return result
