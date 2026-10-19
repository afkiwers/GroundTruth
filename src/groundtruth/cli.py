"""groundtruth command line: init | detect | info | check | simulate | suggest | compare | history.

Exit codes: 0 = passed, 1 = issues at or above --fail-on (or EMI margin below --max-dbuv-margin),
2 = configuration / parse error. Never imports wx or kipy, so it runs in a slim CI container.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from . import __version__

EXIT_OK, EXIT_ISSUES, EXIT_CONFIG = 0, 1, 2
FORMATS = ("text", "json", "junit", "gitlab-codequality", "html")
OUTPUT_NAMES = {"json": "groundtruth-report.json", "junit": "groundtruth-junit.xml",
                "gitlab-codequality": "gl-code-quality-report.json", "html": "groundtruth-report.html"}


class CliError(Exception):
    """A problem the user can fix (a missing file, invalid settings, an unknown net): printed as "error: ..." and
    the command exits with EXIT_CONFIG, without a traceback."""
    pass


def _utf8_console():
    """Print UTF-8 whatever the console's code page: Windows consoles default to cp1252, which cannot print the
    µ, Ω, → and ≈ in the messages (a character it can't encode becomes "?" instead of an exception)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load(board_arg: str, refill: bool):
    """Read the board file (refill: on a temporary copy whose zones kicad-cli refilled first); CliError when it
    cannot be read. The board keeps the user's path as its source, so reports name their file, not the copy."""
    from .adapters.file_source import BoardLoadError, load_board
    path = board_arg
    if refill:
        from .ci.refill import RefillError, refill_copy
        try:
            path = refill_copy(board_arg)
        except RefillError as e:
            raise CliError(str(e)) from e
    try:
        board = load_board(path)
    except (BoardLoadError, ValueError) as e:
        raise CliError(str(e)) from e
    board.source = board_arg  # report the user's path, not the temp copy
    return board


def _settings(board, board_arg: str, settings_arg: Optional[str]):
    """The board's settings file (the --settings path, else <board folder>/groundtruth/<board>.groundtruth.yaml),
    parsed and checked against the board; the schematic fields' values applied in memory. CliError with every
    problem listed when it is missing, invalid or does not match the board."""
    from .config.settings import ConfigError, SettingsFile, settings_path, validate
    path = Path(settings_arg) if settings_arg else settings_path(board_arg)
    if not path.exists():
        raise CliError(f"settings {path} not found; create it with: groundtruth init {board_arg}")
    try:
        sc = SettingsFile.load(path)
    except ConfigError as e:
        raise CliError("invalid settings:\n  " + "\n  ".join(e.messages)) from e
    errors = validate(sc.config, board)
    if errors:
        raise CliError("settings does not match the board:\n  " + "\n  ".join(errors))
    from .components.fields import apply_fields
    apply_fields(sc.config, board)          # the schematic's current values (in memory: `init` writes them)
    return sc


# --------------------------------------------------------------------------- commands
def cmd_init(args) -> int:
    """`groundtruth init`: create the settings file with every part's suggested model, or (when it exists) merge
    the board's current parts into it, keeping the user's edits (--force starts over)."""
    from .config.settings import ConfigError, SettingsFile, settings_path
    board = _load(args.board, args.refill_zones)
    path = Path(args.settings) if args.settings else settings_path(args.board)
    if path.exists() and not args.force:
        try:
            sc = SettingsFile.load(path)
        except ConfigError as e:
            raise CliError("invalid settings:\n  " + "\n  ".join(e.messages)) from e
        changes = sc.merge_board(board)
        sc.save()
        print(f"updated {path}: {len(changes['added'])} added, {len(changes['updated'])} changed, "
              f"{len(changes['removed'])} removed part(s)")
        for k, refs in changes.items():
            if refs:
                print(f"  {k}: {', '.join(refs[:20])}{' ...' if len(refs) > 20 else ''}")
    else:
        sc = SettingsFile.new(path, board)
        sc.save()
        models = Counter(e.model.kind for e in sc.config.components.values())
        print(f"wrote {path}: {len(sc.config.components)} parts ({', '.join(f'{v} {k}' for k, v in models.items())})")
        print(f"reference nets: {', '.join(sc.config.reference_nets)}")
        print("next: add the nets to analyse under 'nets:' (see the comment in the file), then run "
              f"'groundtruth check {args.board}'")
    for w in board.warnings:
        print(f"note: {w}")
    return EXIT_OK


def cmd_info(args) -> int:
    """`groundtruth info`: the board's stackup and counts, and the settings' state (what is listed, which part
    models still lack values)."""
    from .config.settings import ConfigError, SettingsFile, settings_path
    board = _load(args.board, False)
    st = board.stackup
    print(f"{board.source}  (file {board.file_version}, KiCad {board.generator_version or '?'})")
    print(f"copper layers: {len(st.copper)}  board thickness {st.board_thickness:g} mm")
    for i, layer in enumerate(st.copper):
        print(f"  {layer.name:8s} z={layer.z:.3f} mm  t={layer.thickness * 1000:.0f} µm")
        if i < len(st.dielectrics):
            d = st.dielectrics[i]
            print(f"  {'':8s}   {d.name}: {d.thickness:g} mm, εr {d.epsilon_r:g}")
    print(f"nets {len(board.nets)}, tracks {len(board.tracks)}, vias {len(board.vias)}, "
          f"footprints {len(board.footprints)}, zones {len(board.zones)}")
    path = Path(args.settings) if args.settings else settings_path(args.board)
    if path.exists():
        try:
            sc = SettingsFile.load(path)
        except ConfigError as e:
            raise CliError("invalid settings:\n  " + "\n  ".join(e.messages)) from e
        cfg = sc.config
        missing = [f"{r} ({', '.join(e.model.missing_params())})" for r, e in cfg.components.items()
                   if not e.removed and e.model.missing_params()]
        print(f"settings {path}: {len(cfg.nets)} net(s), {len(cfg.converters)} converter(s) and {len(cfg.bridges)} "
              f"bridge(s) to analyse, "
              f"reference {', '.join(cfg.reference_nets)}")
        if missing:
            print(f"parts with missing model values: {', '.join(missing)}")
    for w in board.warnings:
        print(f"note: {w}")
    return EXIT_OK


def cmd_detect(args) -> int:
    """`groundtruth detect`: list the power stages, motor bridges and fast signal nets detection finds, each with
    its confidence and reasons, marked new or already listed; --write adds the new ones (unreviewed)."""
    from .analysis.detect import detect_bridges, detect_converters, detect_nets, power_nets
    from .components.values import format_si
    from .config.settings import ConfigError, SettingsFile, settings_path
    board = _load(args.board, False)
    path = Path(args.settings) if args.settings else settings_path(args.board)
    try:
        sc = SettingsFile.load(path) if path.exists() else SettingsFile.new(path, board)
    except ConfigError as e:
        raise CliError("invalid settings:\n  " + "\n  ".join(e.messages)) from e
    stages = detect_converters(board, sc.config)
    have = {frozenset(c.parts) for c in sc.config.converters}
    for d in stages:
        s = d.spec
        mark = "listed " if frozenset(s.parts) in have else "new    "
        cell = f"switch {', '.join(s.switch)}" if s.switch else f"high {', '.join(s.high_side)} / low {', '.join(s.low_side)}"
        print(f"{mark}[{d.confidence:6s}] converter {s.name}: {s.topology}, {cell}, rail {s.rail}, ground {s.ground}"
              + (f", input {'/'.join(s.input_connector)}" if s.input_connector else "")
              + (f", output {'/'.join(s.output_connector)}" if s.output_connector else ""))
        for r in d.reasons:
            print(f"           {r}")
    bridges = detect_bridges(board, sc.config)
    have_b = {frozenset(b.parts) for b in sc.config.bridges}
    for d in bridges:
        b = d.spec
        mark = "listed " if frozenset(b.parts) in have_b else "new    "
        legs = "; ".join(f"{leg.phase}: high {', '.join(leg.high_side)} / low {', '.join(leg.low_side)}"
                         + (f" -> {leg.output}" if leg.output else "") for leg in b.legs)
        print(f"{mark}[{d.confidence:6s}] {b.name}: {len(b.legs)} legs ({legs}), bus {b.rail}, ground {b.ground}"
              + (f", input {'/'.join(b.input_connector)}" if b.input_connector else ""))
        for r in d.reasons:
            print(f"           {r}")
    from .analysis.tags import board_tags
    labels = board_tags(board)
    if labels:                  # GT fields on the board (detection labels): what they gave, and what is wrong
        print(labels.summary())
    stage_specs = [d.spec for d in stages] + [s for d in bridges for s in d.spec.stages()]
    found = detect_nets(board, sc.config, args.min_score, skip=power_nets(board, sc.config, stage_specs))
    listed = {n.name for n in sc.config.nets}
    from .analysis.detect import refreshes
    updates, fragments = refreshes(found, sc.config.nets)
    replacing = {d.spec.name for d, _ in updates}
    from .analysis.si import detect_pairs
    pairs = detect_pairs(board, sc.config)
    for p in pairs:
        print(f"new    [name  ] pair {p.label}: {p.p} / {p.n}"
              + (f", Zdiff {format_si(p.zdiff, 'Ω')}" if p.zdiff else "") + f", edge {format_si(p.rise_time, 's')}"
              + f" ({p.note})")
    if not found and not stages and not bridges and not pairs:
        print("no candidate nets or power stages found (try --min-score 1 or 0)")
        return EXIT_OK
    for d in found:
        s = d.spec
        mark = "update " if s.name in replacing else "listed " if s.name in listed else "new    "
        print(f"{mark}[{d.confidence:6s}] {s.name}  {s.driver} -> {', '.join(s.receivers)}  "
              f"{d.signal_class.name}, {format_si(s.freq, 'Hz')}, {format_si(s.rise_time, 's')}")
        for r in d.reasons:
            print(f"           {r}")
    if fragments:
        print(f"unreviewed entries inside a detected path (removed with --write): {', '.join(fragments)}")
    if args.write:
        replaced, dropped = sc.refresh_detected([d.spec for d, _ in updates], fragments)
        if replaced or dropped:
            print(f"replaced {len(replaced)} unreviewed net(s) by their new detection ({', '.join(replaced) or '-'}), "
                  f"removed {len(dropped)} piece(s) of their paths ({', '.join(dropped) or '-'})")
        added = sc.add_detected([d.spec for d in found])
        conv = sc.add_detected_converters([d.spec for d in stages], {r: m for d in stages for r, m in d.models.items()})
        brid = sc.add_detected_bridges([d.spec for d in bridges], {r: m for d in bridges for r, m in d.models.items()})
        if pairs:
            sc.set_si_pairs(list(sc.config.si.pairs) + pairs)
        sc.save()
        print(f"added {len(added)} net(s), {len(conv)} converter(s), {len(brid)} bridge(s) and {len(pairs)} "
              f"differential pair(s) to {path} as "
              "'source: auto, reviewed: false'. Review driver, receivers and timing (nets), f_sw, i_out, v_in/v_out, "
              "rise_time, input_connector, output_connector (converters) and set each bridge's operating point and "
              "motor, in the plugin or in the file, then set 'reviewed: true'.")
    else:
        print("(dry run; add --write to put the new nets, converters, bridges and pairs into the settings for "
              "review)")
    return EXIT_OK


def _run(args, simulate: bool) -> int:
    """`groundtruth check` (simulate False: the geometric rule checks) and `groundtruth simulate` (the field solver,
    emission estimates, power stages and bridges; optionally thermal and current capacity). Writes the requested
    report formats, keeps the run in the history with --history, and returns the CI exit code: EXIT_ISSUES when
    an issue reaches --fail-on or a margin is below --max-dbuv-margin."""
    from .analysis.simulate import simulate as run_sim
    from .core.issues import sort_issues
    from .report import formats
    from .rules.checks import run_checks

    t0 = time.time()
    board = _load(args.board, args.refill_zones)
    sc = _settings(board, args.board, args.settings)
    cfg = sc.config
    _solver_options(cfg, args)
    on = cfg.active(args.nets)              # rows switched off (enabled: false) are not analysed, unless named
    # check without nets still runs the board-wide checks (voltage-aware safety, schematic, values); simulate needs
    # something to solve
    if simulate and (cfg.nets or cfg.converters or cfg.bridges or cfg.dc_loads or cfg.pdn) and not (
            on.nets or on.converters or on.bridges or on.dc_loads or on.pdn):
        raise CliError(f"every net, converter, bridge and DC load in {sc.path} is switched off (enabled: false); "
                       "switch one on, or name it with --nets")
    if simulate and not (cfg.nets or cfg.converters or cfg.bridges or cfg.dc_loads or cfg.pdn):
        raise CliError(f"nothing to simulate in {sc.path}; add nets, converters, bridges, DC loads or PDN rails, "
                       "or run 'groundtruth detect --write'")
    if not simulate and cfg.nets and not on.nets:
        print(f"every net in {sc.path} is switched off (enabled: false): only the board-wide checks ran; switch one "
              "on, or name it with --nets", file=sys.stderr)
    unreviewed = ([n.name for n in on.nets if not n.reviewed] + [c.name for c in on.converters if not c.reviewed]
                  + [b.name for b in on.bridges if not b.reviewed])
    off = [s.name for s in (*cfg.nets, *cfg.converters, *cfg.bridges) if not s.enabled]
    if off and not args.quiet:
        named = set(args.nets or ())
        print(f"note: switched off, not analysed: {', '.join(n for n in off if n not in named) or '-'}"
              + (f"; run because named: {', '.join(n for n in off if n in named)}" if named & set(off) else ""),
              file=sys.stderr)
    if unreviewed and not args.quiet:
        print(f"note: {len(unreviewed)} auto-detected net(s)/converter(s) not reviewed yet: {', '.join(unreviewed)}",
              file=sys.stderr)
    nets = args.nets or None
    if nets:
        unknown = (set(nets) - {n.name for n in cfg.nets} - {c.name for c in cfg.stages()} - {b.name for b in cfg.bridges}
                   - {d.name for d in cfg.dc_loads} - {d.label for d in cfg.pdn})
        if unknown:
            raise CliError(f"--nets not in the settings: {', '.join(sorted(unknown))}")
    edits = [m for m in cfg.modifications if m.enabled] if args.what_if else []
    if args.what_if:
        board, cfg = _apply_what_if(board, cfg, args.quiet)

    def progress(msg, frac):
        """The solver's progress on stderr (only with --verbose: simulate passes it on then)."""
        if not args.quiet:
            print(f"  [{frac * 100:5.1f}%] {msg}", file=sys.stderr)

    converters, bridges, thermal, capacity, solver, combined, dc_loads = [], [], None, [], None, None, []
    pdn, si = [], None
    if simulate:
        res = run_sim(board, cfg, nets, progress=progress if args.verbose else None)
        issues, routes, converters, bridges, solver = res.issues, res.routes, res.converters, res.bridges, res.solver
        combined, dc_loads = res.dc_combined, list(res.dc_loads)
        pdn = list(getattr(res, "pdn", ()))
        si = getattr(getattr(res, "checks", None), "si", None)
        _say_solver(res.solver, args)
        if args.thermal or args.capacity:
            try:
                from .analysis.thermal import run_capacity, run_thermal
                import pyamg  # noqa: F401  (the [thermal] extra)
            except ImportError as e:
                flag = "--thermal" if args.thermal else "--capacity"
                raise CliError(f"{flag} needs the thermal extra: pip install \"groundtruth-pcb[thermal]\" ({e})")
        if args.thermal:
            thermal, th_issues = run_thermal(board, cfg, list(converters) + list(bridges) + dc_loads,
                                             progress=progress if args.verbose else None)
            if thermal.error:
                print(f"thermal: {thermal.error}", file=sys.stderr)
                thermal = None
            else:
                issues = list(issues) + th_issues
        if args.capacity:
            capacity = run_capacity(board, cfg, list(converters) + list(bridges) + dc_loads,
                                    progress=progress if args.verbose else None)
            for c in capacity:
                if c.error:
                    print(f"current capacity: {c.error}", file=sys.stderr)
            capacity = [c for c in capacity if not c.error]
    else:
        res = run_checks(board, cfg, nets=nets)
        issues, routes, si = res.issues, [], res.si
        if (cfg.converters or cfg.bridges) and not args.quiet:
            print("note: converters and bridges are analysed by 'groundtruth simulate'", file=sys.stderr)

    # a baseline marks known issues as waived: CI fails only on new ones
    if args.baseline:
        n = formats.apply_baseline(issues, formats.load_baseline(args.baseline))
        if n and not args.quiet:
            print(f"baseline: {n} known issue(s) not counted", file=sys.stderr)
    issues = sort_issues(issues)
    if args.write_baseline:
        formats.write_baseline(args.write_baseline, issues)

    out_dir = Path(args.out_dir)
    fmts = args.format or ["text"]
    analysed = [n.name for n in cfg.nets if not nets or n.name in nets]
    analysed_conv = [c.net for c in converters] + [b.net for b in bridges]
    checks = formats.TIER1_CHECKS + (formats.TIER2_CHECKS if simulate else ())
    mode = ("simulate (Tier 1 + Tier 2)" if simulate else "check (Tier 1)") + (" with what-if edits" if args.what_if else "")
    if simulate and args.history:
        _keep_run(args, board, sc, cfg, issues, routes, converters, bridges, thermal, capacity, solver, mode, edits,
                  time.time() - t0, combined, dc_loads)
    for fmt in fmts:
        if fmt == "text":
            if not args.quiet:
                print(formats.to_text(issues, routes, board.warnings, converters, dc_loads, pdn, si))
                for b in bridges:
                    print("\n".join(formats.bridge_text(b)))
                if thermal is not None:
                    print(_thermal_text(thermal))
                if capacity:
                    print(_capacity_text(capacity))
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / OUTPUT_NAMES[fmt]
        if fmt == "json":
            text = formats.to_json(args.board, issues, routes, {"mode": mode, "version": __version__}, converters,
                                   bridges, dc_loads, pdn, si, thermal, capacity)
        elif fmt == "junit":
            text = formats.to_junit(args.board, issues, analysed, checks, args.fail_on, analysed_conv)
        elif fmt == "gitlab-codequality":
            text = formats.to_codequality(args.board, issues)
        else:
            from .report.html import render_html
            text = render_html(board, issues, routes, mode, str(sc.path), converters=converters, view3d=simulate,
                               bridges=bridges, thermal=thermal, capacity=capacity, solver=solver, config=cfg,
                               dc_combined=combined, dc_loads=dc_loads, pdn=pdn, si=si)
            if simulate:
                _write_pngs(board, list(routes) + list(converters) + dc_loads + ([thermal] if thermal else [])
                            + capacity,
                            issues, out_dir)
        target.write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"wrote {target}", file=sys.stderr)

    failed = formats.failing(issues, args.fail_on)
    margin_fail = []
    if simulate and args.max_dbuv_margin is not None:
        for r in list(routes) + list(converters) + list(bridges):
            for what, w in (("radiated", r.worst), ("conducted", getattr(r, "conducted_worst", None))):
                if w and w["margin_db"] < args.max_dbuv_margin:
                    margin_fail.append(r)
                    print(f"FAIL: {r.net} {r.driver}->{r.receiver} {what} margin {w['margin_db']:+.1f} dB < "
                          f"{args.max_dbuv_margin:g} dB", file=sys.stderr)
    if not args.quiet:
        print(f"{'FAILED' if failed or margin_fail else 'passed'} (--fail-on {args.fail_on}) in {time.time() - t0:.1f}s",
              file=sys.stderr)
    return EXIT_ISSUES if failed or margin_fail else EXIT_OK


def _keep_run(args, board, sc, cfg, issues, routes, converters, bridges, thermal, capacity, solver, mode, edits,
              seconds, combined=None, dc_loads=()):
    """simulate --history: this run in the board's history folder (report/history.py), with its report page."""
    from .report import formats
    from .report.history import HistoryError, cell_mm_of, edit_texts, history_for, run_meta
    from .report.html import render_html
    source = "file (zones refilled by kicad-cli for the run)" if args.refill_zones else "file"
    try:
        board_bytes = Path(args.board).read_bytes()
        history = history_for(args.board)
        run = history.record(
            board_bytes, sc.dumps(),
            formats.to_json(args.board, issues, routes, {"mode": mode, "version": __version__}, converters, bridges,
                            list(dc_loads), thermal=thermal, capacity=capacity),
            run_meta(args.board, source, by="cli", resolution=cfg.analysis.resolution, what_if=args.what_if,
                     edits=edit_texts(edits), only=args.nets or [], seconds=seconds, solver=solver,
                     cell_mm=cell_mm_of([r.grid.d for r in list(routes) + list(converters)
                                         if getattr(r, "grid", None) is not None], cfg.analysis.resolution)))
    except (OSError, HistoryError) as e:
        print(f"history: could not keep this run ({e})", file=sys.stderr)
        return
    try:
        history.write_report(run, render_html(board, issues, routes, mode, str(sc.path), converters=converters,
                                              bridges=bridges, thermal=thermal, capacity=capacity, solver=solver,
                                              config=cfg, dc_combined=combined, dc_loads=list(dc_loads)))
    except Exception as e:      # noqa: BLE001 - the run is kept; only its page is missing
        print(f"history: kept as {run.folder}, without its report ({type(e).__name__}: {e})", file=sys.stderr)
        return
    if not args.quiet:
        print(f"history: kept as {run.folder}", file=sys.stderr)


def cmd_history(args) -> int:
    """The runs kept for a board (simulate --history, and every Simulate in the plugin), or two compared."""
    import json
    from .report.history import HistoryError, compare, comparison_text, history_for, runs_text
    history = history_for(args.board)
    runs = history.runs()                           # newest first
    if args.compare:
        if len(args.compare) > 2:
            raise CliError("--compare takes one run (against the run before it) or two")
        try:
            picked = [history.find(r, runs) for r in args.compare]
        except HistoryError as e:
            raise CliError(str(e)) from e
        if len(picked) == 1:
            k = runs.index(picked[0])
            if k + 1 >= len(runs):
                raise CliError(f"run {picked[0].id} is the oldest: there is no run before it")
            picked.append(runs[k + 1])
        b, a = sorted(picked, key=runs.index)       # a: the older
        cmp = compare(a, b)
        print(json.dumps(cmp, indent=2, ensure_ascii=False) if args.json else comparison_text(cmp))
        return EXIT_OK
    if args.json:
        print(json.dumps([{"id": r.id, **r.meta} for r in runs], indent=2, ensure_ascii=False))
    else:
        print(f"{history.root}:")
        print(runs_text(runs))
    return EXIT_OK


def _apply_what_if(board, cfg, quiet: bool):
    """The board and settings with the settings' enabled what-if edits applied (in memory: the files are not
    changed). CliError when there are none or one cannot be applied."""
    from .analysis.whatif import ModificationError, apply_modifications
    active = [m for m in cfg.modifications if m.enabled]
    if not active:
        raise CliError("--what-if: no enabled modifications in the settings (see 'modifications:' or 'groundtruth suggest --write')")
    try:
        board, cfg = apply_modifications(board, cfg, active)
    except ModificationError as e:
        raise CliError(f"invalid modification: {e}") from e
    if not quiet:
        print(f"what-if: {len(active)} modification(s) applied: " + "; ".join(m.label() for m in active),
              file=sys.stderr)
    return board, cfg


def _solver_options(cfg, args):
    """--gpu overrides analysis.gpu for this run; --resolution sets the models' node factor."""
    if getattr(args, "gpu", None):
        cfg.analysis.gpu = args.gpu
    if getattr(args, "resolution", None):
        cfg.analysis.resolution = float(args.resolution)
    if getattr(args, "all_cores", False):
        cfg.analysis.all_cores = True


def _say_solver(solver: dict, args):
    """Where the solves ran, when it was not plainly the CPU (stderr: the reports stay the same)."""
    from .solver.gpu import describe
    if solver and not getattr(args, "quiet", False) and (solver.get("device", "CPU") != "CPU"
                                                          or solver.get("gpu_fallbacks")):
        print(f"solver: {describe(solver)}", file=sys.stderr)


def _prepare(args):
    """Board + settings + selected nets for the analysis commands."""
    board = _load(args.board, getattr(args, "refill_zones", False))
    sc = _settings(board, args.board, args.settings)
    _solver_options(sc.config, args)
    if not sc.config.nets and not sc.config.converters and not sc.config.bridges:
        raise CliError(f"no nets or converters listed in {sc.path}; add them or run 'groundtruth detect --write'")
    nets = args.nets or None
    if nets:
        unknown = set(nets) - {n.name for n in sc.config.nets} - {c.name for c in sc.config.stages()}             - {b.name for b in sc.config.bridges}
        if unknown:
            raise CliError(f"--nets not in the settings: {', '.join(sorted(unknown))}")
    return board, sc, nets


def cmd_suggest(args) -> int:
    """`groundtruth suggest`: the fix search (candidate stitching vias, decoupling caps, bridging caps, fills per
    issue, each simulated, the best combination verified); --write stores them as what-if edits."""
    from .analysis.suggest import as_modifications, suggest_fixes
    from .report import formats
    t0 = time.time()
    board, sc, nets = _prepare(args)

    def progress(msg, frac):
        """Progress on stderr with --verbose."""
        if args.verbose:
            print(f"  [{frac * 100:5.1f}%] {msg}", file=sys.stderr)

    res = suggest_fixes(board, sc.config, nets, progress, max_fixes=args.max_fixes, candidates=args.candidates,
                        verify=not args.no_verify, verify_all=args.verify_all)
    _say_solver(res.solver, args)
    if not args.quiet:
        print(formats.suggestions_text(res))
    out_dir = Path(args.out_dir)
    for fmt in args.format or []:
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            target = out_dir / "groundtruth-suggestions.json"
            target.write_text(formats.suggestions_json(args.board, res), encoding="utf-8")
        else:
            from .report.html import render_html
            target = out_dir / "groundtruth-suggestions.html"
            target.write_text(render_html(board, res.before.issues, res.before.routes, "fix suggestions",
                                          str(sc.path), suggestions=res, converters=res.before.converters,
                                          bridges=res.before.bridges, solver=res.solver, config=sc.config,
                                          dc_combined=res.before.dc_combined),
                              encoding="utf-8")
        if not args.quiet:
            print(f"wrote {target}", file=sys.stderr)
    if args.write:
        # kept: the selected fixes, and any that lowered the emission or the loop inductance
        useful = [s for s in res.suggestions if s.modification is not None and s.delta_db is not None
                  and (s.selected or s.delta_db < 0 or (s.delta_l or 0) < 0)]
        mods = as_modifications(useful)
        n = sc.add_modifications(mods)
        sc.save()
        print(f"added {n} what-if modification(s) to {sc.path} ({sum(m.enabled for m in mods)} enabled). "
              f"Run 'groundtruth compare {args.board}' to check them, then make the changes in KiCad.", file=sys.stderr)
    if not args.quiet:
        print(f"done in {time.time() - t0:.1f}s", file=sys.stderr)
    return EXIT_OK


def cmd_compare(args) -> int:
    """`groundtruth compare`: simulate the board as it is and with the enabled what-if edits, and print the
    differences per net and stage and the issues fixed and new."""
    from .analysis.simulate import simulate as run_sim
    from .analysis.suggest import compare_results, issue_changes
    from .report import formats
    t0 = time.time()
    board, sc, nets = _prepare(args)
    board2, cfg2 = _apply_what_if(board, sc.config, args.quiet)

    def progress(msg, frac):
        """Progress on stderr with --verbose."""
        if args.verbose:
            print(f"  [{frac * 100:5.1f}%] {msg}", file=sys.stderr)

    before = run_sim(board, sc.config, nets, progress=progress)
    after = run_sim(board2, cfg2, nets, progress=progress)
    rows = compare_results(before, after)
    fixed, new = issue_changes(before.issues, after.issues)
    if not args.quiet:
        print(formats.comparison_text(rows, fixed, new))
    if args.format and "json" in args.format:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "groundtruth-compare.json"
        target.write_text(formats.comparison_json(args.board, rows, fixed, new), encoding="utf-8")
        if not args.quiet:
            print(f"wrote {target}", file=sys.stderr)
    if not args.quiet:
        print(f"done in {time.time() - t0:.1f}s", file=sys.stderr)
    return EXIT_OK


def _thermal_text(th) -> str:
    """The thermal solve for the text report: the hottest copper, the power, the eight hottest parts."""
    lines = [f"thermal: hottest copper {th.t_max:.1f} °C on {th.hotspot_layer} at ({th.hotspot[0]:.2f}, "
             f"{th.hotspot[1]:.2f}), {th.ambient_c:g} °C ambient; {th.power_w:.2f} W ({th.copper_w:.2f} W copper, "
             f"{th.parts_w:.2f} W parts)"]
    for p in th.parts[:8]:
        lines.append(f"  {p['ref']:10s} {p['t_c']:6.1f} °C" + (f"  junction ≈{p['t_junction']:.1f} °C"
                                                               if p["t_junction"] else "")
                     + (f"  {p['power_w']:.2f} W ({p['why']})" if p["power_w"] else ""))
    lines += [f"  note: {n}" for n in th.notes]
    return "\n".join(lines)


def _capacity_text(views) -> str:
    """The current-capacity results for the text report: per stage the current at the temperature limit, as a
    share of its operating point, and what limits it."""
    from .analysis.thermal import capacity_text
    c0 = views[0].capacity
    lines = [f"current capacity (the hottest copper or part junction at {c0['limit_c']:g} °C, "
             f"{views[0].ambient_c:g} °C ambient; one stage scaled, the others at their operating points, or all "
             f"scaled together):"]
    for v in views:
        c = v.capacity
        on = c["scale"] > 0 and c.get("bound") != "zero"
        if c.get("currents") is not None:
            share = ("of every operating point: " + ", ".join(f"{n} {i:.3g} A" for n, i in c["currents"].items())
                     if on else "over the limit even without load")
        else:
            share = (f"operating point {c['i_nominal']:g} A = {100 / c['scale']:.0f} % of it" if on
                     else "over the limit even without load")
        lines.append(f"  {c['stage']:24s} {capacity_text(c):>9s}   {share}; limited by {c['limited_by']}")
        lines += [f"    note: {n}" for n in v.notes if n.startswith("the search")]
    notes = [n for n in views[0].notes if not n.startswith("the search")]
    lines += [f"  note: {n}" for n in notes]
    return "\n".join(lines)


def _write_pngs(board, routes, issues, out_dir: Path):
    """Each view's heatmap, cross-section and spectrum as PNGs, and its 3D page (view3d_<stem>.html, linked from
    the HTML report)."""
    from .report.plots import draw_heatmap, draw_section, draw_spectrum, figure_png
    from .report.view3d import file_stem, has_view, write_view3d
    for rr in routes:
        if rr.grid is None:
            continue
        stem = file_stem(rr)
        size = (8, 5) if len(rr.main_layers()[:4]) <= 1 else (9, 7)
        (out_dir / f"heatmap_{stem}.png").write_bytes(figure_png(draw_heatmap, board, rr, issues, size=size))
        if getattr(rr, "section", None):
            (out_dir / f"section_{stem}.png").write_bytes(figure_png(draw_section, board, rr, size=(8, 3.2)))
        (out_dir / f"spectrum_{stem}.png").write_bytes(figure_png(draw_spectrum, rr, size=(8, 3.2)))
        if has_view(rr):
            write_view3d(board, rr, out_dir)


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    """The command-line parser: one subcommand per command, each with its options and help text."""
    p = argparse.ArgumentParser(prog="groundtruth", description="KiCad return-path and EMI analysis")
    p.add_argument("--version", action="version", version=f"groundtruth {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def board_args(sp):
        """The board file and --settings, which every board command takes."""
        sp.add_argument("board", help="path to the .kicad_pcb file")
        sp.add_argument("--settings", help="settings file (default: groundtruth/<board>.groundtruth.yaml next to "
                                           "the board)")

    def solver_args(sp):
        """--gpu, --resolution and --all-cores, for the commands that run the field solver."""
        sp.add_argument("--gpu", choices=("auto", "on", "off"),
                        help="solver iterations on a GPU (PyTorch with CUDA/ROCm): auto uses one if there is one, "
                             "on requires it (default: analysis.gpu in the settings, auto)")
        sp.add_argument("--resolution", type=float, choices=(1, 2, 4, 8, 16, 20, 25), metavar="N",
                        help="every model gets N times the nodes (cells 1/sqrt(N) in size): 1 (default), 2, 4, 8, "
                             "16, 20, 25; memory and time grow about as much")
        sp.add_argument("--all-cores", action="store_true",
                        help="use every physical core: auto threads unlimited and several frequencies per refinement "
                             "round (faster on big machines; figures can differ slightly between machines)")

    sp = sub.add_parser("init", help="create or update the settings with suggested part models")
    board_args(sp)
    sp.add_argument("--force", action="store_true", help="overwrite an existing settings instead of merging")
    sp.add_argument("--refill-zones", action="store_true", help="refill zones with kicad-cli on a temp copy first")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("detect", help="suggest nets (driver/receivers/timing) and power stages to analyse, for "
                                        "review")
    board_args(sp)
    sp.add_argument("--min-score", type=int, default=2, choices=(0, 1, 2, 3),
                    help="3 = clearly fast (SPI clock, RMII, USB), 2 = likely fast (default), 1 = moderate, 0 = all")
    sp.add_argument("--write", action="store_true", help="add new suggestions to the settings (unreviewed)")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("info", help="show stackup, counts and settings status")
    board_args(sp)
    sp.set_defaults(func=cmd_info)

    for name, sim, helptext in (("check", False, "Tier 1 geometric return-path checks (fast)"),
                                ("simulate", True, "Tier 1 + Tier 2 field solver and EMI estimate")):
        sp = sub.add_parser(name, help=helptext)
        board_args(sp)
        sp.add_argument("--nets", nargs="+", help="only these nets or converters (names as in the settings)")
        sp.add_argument("--fail-on", choices=("info", "warning", "error"), default="error")
        sp.add_argument("--format", action="append", choices=FORMATS,
                        help="output format, repeatable (default: text)")
        sp.add_argument("--out-dir", default=".", help="directory for report files (default: .)")
        sp.add_argument("--refill-zones", action="store_true", help="refill zones with kicad-cli on a temp copy first")
        sp.add_argument("--baseline", help="JSON baseline of accepted issues (by fingerprint)")
        sp.add_argument("--write-baseline", metavar="FILE", help="write current issues as a new baseline")
        sp.add_argument("-q", "--quiet", action="store_true")
        sp.add_argument("--what-if", action="store_true",
                        help="apply the settings's enabled what-if modifications (never use this to gate CI)")
        if sim:
            sp.add_argument("--max-dbuv-margin", type=float, metavar="DB",
                            help="fail if any net's or converter's estimated emission (radiated or conducted) is "
                                 "within DB of the limit")
            sp.add_argument("-v", "--verbose", action="store_true", help="show solver progress")
            solver_args(sp)
            sp.add_argument("--thermal", action="store_true",
                            help="also solve the copper and part temperatures of the power stages (their DC paths "
                                 "and losses heat the board; settings: thermal; needs the [thermal] extra)")
            sp.add_argument("--capacity", action="store_true",
                            help="also find each power stage's current capacity: the load current that brings the "
                                 "hottest copper or part junction to thermal.capacity_c (default max_c), the other "
                                 "stages at their operating points (needs the [thermal] extra)")
            sp.add_argument("--history", action="store_true",
                            help="keep this run in the board's history: <board folder>/groundtruth/history/ "
                                 "(a copy of the board and settings, the results as JSON, the HTML report); list "
                                 "and compare runs with 'groundtruth history'")
        else:
            sp.set_defaults(history=False)
        sp.set_defaults(func=(lambda a, s=sim: _run(a, s)))

    sp = sub.add_parser("history", help="list the runs kept for a board (simulate --history, the plugin's "
                                        "Simulate), or compare two")
    sp.add_argument("board", help="path to the .kicad_pcb file")
    sp.add_argument("--compare", nargs="+", metavar="RUN",
                    help="compare two runs, or one against the run before it; a run is its id (or the start of "
                         "it) or its number in the list (1 = the newest)")
    sp.add_argument("--json", action="store_true", help="print JSON instead of text")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("suggest", help="propose and verify fixes (stitching vias, decaps, bridge caps, fills)")
    board_args(sp)
    sp.add_argument("--nets", nargs="+", help="only these nets")
    sp.add_argument("--max-fixes", type=int, default=3, help="fixes selected per net (default 3)")
    sp.add_argument("--candidates", type=int, default=3, help="candidate positions tried per issue (default 3)")
    sp.add_argument("--no-verify", action="store_true", help="skip the combined verification run")
    sp.add_argument("--verify-all", action="store_true",
                    help="verify by simulating every net and power stage again (default: only those the fixes can "
                         "change; routes far below the limit with no fix keep their results)")
    sp.add_argument("--write", action="store_true",
                    help="add the suggestions to the settings as what-if modifications (selected ones enabled)")
    sp.add_argument("--format", action="append", choices=("json", "html"), help="also write a report file")
    sp.add_argument("--out-dir", default=".")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    solver_args(sp)
    sp.set_defaults(func=cmd_suggest)

    sp = sub.add_parser("compare", help="baseline vs. the settings's enabled what-if modifications")
    board_args(sp)
    sp.add_argument("--nets", nargs="+", help="only these nets")
    sp.add_argument("--format", action="append", choices=("json",), help="also write a JSON file")
    sp.add_argument("--out-dir", default=".")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    solver_args(sp)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("serve", help="run the compute server: the plugin on other computers sends its Simulate, "
                                      "Suggest fixes and Thermal jobs here (local network)")
    # the defaults live in the server's settings file (server.json next to the key, made on the first start); an
    # option given here overrides its entry for this run
    sp.add_argument("--host", default="0.0.0.0", help="address to listen on (default: every network of this PC)")
    sp.add_argument("--port", type=int, help="port (settings file; default 8765; open it in the firewall for the "
                                             "private network)")
    sp.add_argument("--key-file", help="the access key's file (default: %%LOCALAPPDATA%%\\GroundTruth\\server.key, "
                                       "made with a new key on the first start)")
    sp.add_argument("--new-key", action="store_true", help="replace the key with a new one (every plugin then needs "
                                                           "the new key)")
    sp.add_argument("--show-key", action="store_true", help="(the access key is shown at every start anyway)")
    sp.add_argument("--workers", type=int, help="jobs at a time (settings file; default 1: each job gets every core "
                                                "and the GPU, the others wait in the queue; with as many as GPUs, "
                                                "each job gets its own)")
    sp.add_argument("--threads", help="solver threads per job: auto or a number (settings file; default '-': the "
                                      "job's own, from the plugin's Preferences)")
    sp.add_argument("--gpu", choices=("-", "auto", "on", "off"),
                    help="solver iterations on this PC's GPUs (settings file; default auto; '-': the job's own)")
    sp.add_argument("--keep-hours", type=float, help="how long a finished job's result waits for its owner "
                                                     "(settings file; default 24)")
    sp.add_argument("--lead", action=argparse.BooleanOptionalAction, default=None,
                    help="this server is the cluster's lead (else the first server to find another becomes lead; "
                         "--no-lead overrides the settings file's lead)")
    sp.add_argument("--peers", help="servers of the cluster on another subnet, comma-separated (host or host:port "
                                    "of their UDP announcements); on the same subnet they find each other")
    sp.add_argument("--beacon-port", type=int, help="the UDP port the servers find each other on (settings file; "
                                                    "default 8766; 0: no cluster)")
    sp.add_argument("--log", help="also log the jobs to this file")
    sp.set_defaults(func=cmd_serve)
    return p


def cmd_serve(args) -> int:
    """`groundtruth serve`: the compute server (remote/server.py) until Ctrl+C."""
    if sys.version_info < (3, 11):
        # the server's own code starts on an older Python, but every job would fail importing the analysis (a Mac
        # server installed on KiCad.app's Python 3.9 failed each Simulate on itertools.pairwise): refuse to start
        print(f"GroundTruth needs Python 3.11 or newer; this server runs on {sys.version.split()[0]} "
              f"({sys.executable}). Reinstall it with a newer Python (install_server: see its README).",
              file=sys.stderr, flush=True)
        return 1
    from .remote import protocol
    from .remote.server import serve
    path = Path(args.key_file) if args.key_file else protocol.default_key_file()
    if args.new_key and path.exists():
        path.unlink()
    from . import build_version
    from .remote.protocol import code_id
    print(f"{build_version()}, code {code_id()}: the compute server is starting...", flush=True)
    try:
        key, made = protocol.read_key(path, create=True)
    except protocol.ProtocolError as e:
        raise CliError(str(e))
    # what the users need, every start (the user found an empty window: the key used to show only on the first
    # start), before anything else that could take a moment
    from .remote.server import local_addresses, settings
    names = local_addresses()
    port = args.port or settings().get("port", 8765)
    print(("\nA NEW access key was made" if made else "\nAccess key") + f" (kept in {path}):\n\n    {key}\n\n"
          "In each user's plugin: Preferences, Compute server: run analyses on a GroundTruth compute server,\n"
          f"Server address: {' or '.join(f'{n}:{port}' if port != 8765 else n for n in names)}, "
          "Access key: the key above.\n", flush=True)
    from .remote.server import settings, settings_file
    cfg = settings(overrides={"port": args.port, "workers": args.workers, "threads": args.threads, "gpu": args.gpu,
                              "lead": args.lead, "peers": args.peers, "beacon_port": args.beacon_port,
                              "keep_hours": args.keep_hours})
    print(f"Settings ({settings_file()}, options given override them): port {cfg['port']}, {cfg['workers']} job(s) "
          f"at a time, solver threads {cfg['threads'] if cfg['threads'] != '-' else 'as each job asks'}, GPU "
          f"{cfg['gpu'] if cfg['gpu'] != '-' else 'as each job asks'}, results kept {cfg['keep_hours']:g} h.\n",
          flush=True)
    print("Cluster: " + ("off (beacon_port 0)" if not cfg["beacon_port"] else
                         f"finds the other servers with the same key on UDP {cfg['beacon_port']}"
                         + ("; this server is the lead" if cfg["lead"] else "; the first to find another leads")
                         + (f"; peers on other subnets: {', '.join(cfg['peers'])}" if cfg["peers"] else "")) + "\n",
          flush=True)
    if cfg["address"]:
        print(f"Address: announces {cfg['address']} to the cluster and the plugins (server.json \"address\")\n",
              flush=True)
    if cfg["remote_update"]:
        print("Remote updates: on (packages signed with this install's release key, from the users' plugins)\n",
              flush=True)
    return serve(args.host, cfg["port"], key, cfg["workers"], cfg["threads"], cfg["gpu"], args.log, cfg["keep_hours"],
                 cfg["beacon_port"], cfg["lead"], tuple(cfg["peers"]), remote_update=bool(cfg["remote_update"]),
                 address=str(cfg["address"] or ""))


def main(argv: Optional[list[str]] = None) -> int:
    """The `groundtruth` entry point: parse the arguments, run the command, turn the user's errors (CliError, a
    GPU required but missing) into "error: ..." and EXIT_CONFIG."""
    _utf8_console()
    args = build_parser().parse_args(argv)
    from .solver.gpu import GpuUnavailable
    try:
        return args.func(args)
    except (CliError, GpuUnavailable) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
