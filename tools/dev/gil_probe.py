"""Which background work keeps Python's GIL from the GUI thread for long: the plugin's window freezes while a worker
holds it (ui/gil.py). Simulates a board once (or loads a pickled result), then runs each piece of the work the
window starts after a Simulate in a worker thread, while this thread asks for the GIL every millisecond with the
plugin's switch interval, and prints the longest waits and where the worker was when it let go.

    .venv/Scripts/python tools/dev/gil_probe.py BOARD SETTINGS [--only NET ...] [--cache R.pickle] [--top 8]
        [--what report,heatmap]

report: the history's report page (report/html.render_html, as MainFrame._keep_report writes it); heatmap: every
view's figure (report/plots.draw_heatmap) drawn to a canvas, as the Results tab's render worker does. No window,
no files next to the board (--cache is written where you say).
"""
from __future__ import annotations

import argparse
import pickle
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def watch(work, top: int, threshold: float = 0.05):
    """Run work() in a thread; return (seconds, [(wait s, where the worker was when it let go)])."""
    from groundtruth.ui.gil import GUI_SWITCH_INTERVAL
    saved = sys.getswitchinterval()
    sys.setswitchinterval(GUI_SWITCH_INTERVAL)
    box = {}

    def run():
        """The worker: note its thread id (to find its stack later), run the work, keep an exception."""
        box["ident"] = threading.get_ident()
        try:
            work()
        except Exception as e:      # noqa: BLE001
            box["error"] = e
    th = threading.Thread(target=run)
    waits = []
    t0 = last = time.perf_counter()
    th.start()
    try:
        while th.is_alive():
            time.sleep(0.001)
            now = time.perf_counter()
            if now - last > threshold:
                frame = sys._current_frames().get(box.get("ident"))
                where = " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                   for f in reversed(traceback.extract_stack(frame)[-4:])) if frame else "?"
                waits.append((now - last, where))
            last = now
    finally:
        sys.setswitchinterval(saved)
    th.join()
    if "error" in box:
        print(f"   failed: {type(box['error']).__name__}: {box['error']}")
    return time.perf_counter() - t0, sorted(waits, reverse=True)[:top]


def main() -> int:
    """The command line (see the module): simulate (or load --cache), then time the report page and the heatmaps in
    a worker while measuring this thread's waits for the GIL."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("--only", nargs="+")
    ap.add_argument("--cache", type=Path, help="pickle of the simulation result: made once, then reused")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--what", default="report,heatmap")
    args = ap.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.simulate import simulate
    from groundtruth.components.fields import apply_fields
    from groundtruth.config.settings import SettingsFile
    board = load_board(args.board)
    sc = SettingsFile.load(args.settings)
    cfg = sc.config
    apply_fields(cfg, board)
    if args.cache and args.cache.exists():
        with open(args.cache, "rb") as fh:
            issues, routes, converters, bridges = pickle.load(fh)
    else:
        t = time.perf_counter()
        res = simulate(board, cfg, args.only)
        print(f"simulated in {time.perf_counter() - t:.0f} s")
        issues, routes, converters, bridges = res.issues, res.routes, res.converters, res.bridges
        if args.cache:
            with open(args.cache, "wb") as fh:
                pickle.dump((issues, routes, converters, bridges), fh)
    what = args.what.split(",")
    if "report" in what:
        from groundtruth.report.html import render_html
        secs, waits = watch(lambda: render_html(board, issues, routes, "simulation", str(sc.path),
                                                converters=converters, bridges=bridges, config=cfg), args.top)
        print(f"\nreport page: {secs:.1f} s; longest waits for the GIL:")
        for w, where in waits:
            print(f"  {w * 1e3:6.0f} ms   {where}")
    if "heatmap" in what:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from groundtruth.report.plots import draw_heatmap, draw_section, draw_spectrum
        views = list(routes) + list(converters)

        def heat():         # each view's map, cross-section and spectrum, as ResultsPanel.redraw renders them
            """Every view's map, cross-section and spectrum rendered offscreen, as the Results tab does."""
            for rr in views:
                for draw, size in ((lambda f: draw_heatmap(f, board, rr, issues), (12, 7)),
                                   (lambda f: draw_section(f, board, rr), (12, 3)),
                                   (lambda f: draw_spectrum(f, rr), (12, 4))):
                    fig = Figure(figsize=size, dpi=100)
                    draw(fig)
                    FigureCanvasAgg(fig).draw()
        secs, waits = watch(heat, args.top)
        print(f"\nheatmaps ({len(views)} views): {secs:.1f} s; longest waits for the GIL:")
        for w, where in waits:
            print(f"  {w * 1e3:6.0f} ms   {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
