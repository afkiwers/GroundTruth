"""Live tests against a running KiCad 10+ (IPC API enabled). Windows-focused; see CLAUDE.md for the setup.

    python tools/dev/live_kicad.py owner                        # which KiCad process serves the API (no request)
    python tools/dev/live_kicad.py make DIR                     # scratch project DIR/live.kicad_pro + live.kicad_pcb
    python tools/dev/live_kicad.py read --expect NAME.kicad_pcb # read-only: live adapter vs file parser
    python tools/dev/live_kicad.py timing --expect NAME.kicad_pcb  # read-only: request times, KiCad GUI blocking
    python tools/dev/live_kicad.py freeze [--seconds 40]  # KiCad GUI freezes while the user clicks the button
    python tools/dev/live_kicad.py write DIR [--shot out.png] [--keep]   # plugin window: draw, select, suggest,
                                                                         # place, clear (--keep: leave the drawings)

Safety:
  * `read` never writes. It refuses unless the open board's file name is --expect, so a different project open
    in KiCad (possibly company or customer work) is never read by accident.
  * `write` refuses unless the open board is DIR/live.kicad_pcb, the scratch board made by `make`. It writes
    markers, contours and suggestions (removed again at the end) and places a real via on the scratch board.
    Close the scratch project in KiCad afterwards without saving.
  * Only one KiCad process serves the API on Windows; the user opens the scratch project in their own KiCad.
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def pipe_path() -> str:
    """The API pipe's name: %TEMP%\\kicad\\api.sock (KiCad's default on Windows)."""
    return os.path.join(tempfile.gettempdir(), "kicad", "api.sock")


def pipe_owner() -> int | None:
    """PID of the process serving the default API pipe (Windows), without sending any request."""
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wt.HANDLE
    h = k32.CreateFileW("\\\\.\\pipe\\" + pipe_path(), 0xC0000000, 0, None, 3, 0, None)
    if h in (None, wt.HANDLE(-1).value):
        return None
    pid = wt.ULONG()
    ok = k32.GetNamedPipeServerProcessId(h, ctypes.byref(pid))
    k32.CloseHandle(h)
    return pid.value if ok else None


def cmd_owner(_):
    """`owner`: which process serves the API pipe (its PID and window title), without any API request, so it is safe
    while other projects are open."""
    pid = pipe_owner()
    if pid is None:
        print("no KiCad API server found (is KiCad running with the API enabled?)")
        return 1
    title = ""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/V", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True).stdout.strip()
        title = out.split('","')[-1].rstrip('"') if out else ""
    print(f"API served by PID {pid}: {title}")
    return 0


def cmd_make(args):
    """`make DIR`: the scratch project (the layer-change test board, zones filled by kicad-cli when installed) that
    `write` will accept."""
    from boardgen import layer_change_board
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    board = layer_change_board(None).write(d / "live.kicad_pcb")
    (d / "live.kicad_pro").write_text('{\n  "meta": {\n    "filename": "live.kicad_pro",\n    "version": 3\n  }\n}\n',
                                      encoding="utf-8")
    cli = shutil.which("kicad-cli") or os.path.join(os.environ.get("ProgramFiles", ""), "KiCad", "10.0", "bin",
                                                     "kicad-cli.exe")
    if os.path.exists(cli):   # let KiCad itself fill the zones and write the file in its own format
        r = subprocess.run([cli, "pcb", "drc", "--refill-zones", "--save-board", "-o", str(d / "drc.rpt"),
                            str(board)], capture_output=True, text=True)
        print("kicad-cli refill:", "ok" if r.returncode == 0 else r.stderr.strip()[-300:])
    print(f"scratch project: {d / 'live.kicad_pro'}")
    print("open it in KiCad (File > Open Project), then open its PCB editor")
    return 0


def _connect():
    """Connect to KiCad's API: (the open board, its file path); exits when no PCB editor is open."""
    from kipy.errors import ApiError

    from groundtruth.adapters.kipy_source import board_file_path, connect
    k = connect()
    print("KiCad", k.get_version())
    try:
        kb = k.get_board()
    except ApiError as e:
        sys.exit(f"no board is open in KiCad's PCB editor ({e})")
    return kb, Path(board_file_path(kb) or "")


def cmd_read(args):
    """`read --expect NAME`: read the open board over the API and from its saved file and print where they differ
    (counts, stackup, pads, vias, tracks, zone fill areas). Refuses unless the open board's file name is NAME.
    Never writes."""
    from shapely.geometry import Polygon

    from groundtruth.adapters.file_source import load_board
    from groundtruth.adapters.kipy_source import read_board
    kb, path = _connect()
    if path.name != args.expect:
        print(f"refusing: the open board is {path.name}, not {args.expect}")
        return 2
    t = time.time()
    live = read_board(kb)
    print(f"read over IPC: {time.time() - t:.1f}s | {path}")
    filed = load_board(str(path))

    def summary(b):
        """A board's item counts and copper layers."""
        return {"copper_layers": b.copper_layers, "tracks": len(b.tracks), "arcs": sum(x.is_arc for x in b.tracks),
                "vias": len(b.vias), "footprints": len(b.footprints), "pads": len(b.pads()), "zones": len(b.zones),
                "nets": len(b.nets)}
    sl, sf = summary(live), summary(filed)
    for key in sl:
        print(f"  {key:14s} live {sl[key]!s:50.50s} file {sf[key]!s:50.50s}{'' if sl[key] == sf[key] else '  <-- differs'}")
    same_cu = [vars(x) for x in live.stackup.copper] == [vars(x) for x in filed.stackup.copper]
    same_de = [vars(x) for x in live.stackup.dielectrics] == [vars(x) for x in filed.stackup.dielectrics]
    print(f"  stackup copper equal {same_cu}, dielectrics equal {same_de}")
    pl, pf = {p.pin: p for p in live.pads()}, {p.pin: p for p in filed.pads()}
    common = set(pl) & set(pf)
    dev = max((math.dist(pl[q].position, pf[q].position) for q in common), default=0.0)
    print(f"  pads matched {len(common)}/{len(pl)}/{len(pf)}, max position difference {dev * 1000:.1f} µm, "
          f"nets differing {sum(pl[q].net != pf[q].net for q in common)}, "
          f"layer sets differing {sum(sorted(pl[q].layers) != sorted(pf[q].layers) for q in common)}")

    def key_v(x):
        """A via's identity for matching live and file vias (position to the µm, net, layers, drill)."""
        return round(x.position[0], 3), round(x.position[1], 3), x.net, tuple(x.layers), round(x.drill, 3)

    def key_t(x):
        """A track's identity for matching (layer, net, ends to the µm, width)."""
        return x.layer, x.net, *(round(c, 3) for c in (*x.start, *x.end)), round(x.width, 3)
    for name, kf, a, b in (("vias", key_v, live.vias, filed.vias), ("tracks", key_t, live.tracks, filed.tracks)):
        ca, cb = collections.Counter(map(kf, a)), collections.Counter(map(kf, b))
        print(f"  {name}: only live {sum((ca - cb).values())}, only file {sum((cb - ca).values())}")

    def fills(b):
        """Zone fill area per (layer, net)."""
        out = collections.defaultdict(float)
        for z in b.zones:
            for layer, polys in z.filled.items():
                for outline, holes in polys:
                    out[(layer, z.net)] += Polygon(outline, holes).buffer(0).area
        return out
    fl, ff = fills(live), fills(filed)
    worst = max(((abs(fl.get(q, 0) - ff.get(q, 0)) / max(ff.get(q, 0), 1e-9), q) for q in set(fl) | set(ff)),
                default=(0.0, None))
    print(f"  zone fills: {len(fl)} live / {len(ff)} file (layer, net) groups, worst area difference "
          f"{worst[0] * 100:.2f} % at {worst[1]}; unfilled zones live {len(live.unfilled_zones())}")
    return 0


def cmd_write(args):
    """`write DIR`: drive the plugin window on the scratch board (refuses any other board): run checks, draw
    markers, show every issue, simulate, draw contours and arrows, suggest, draw and place the fixes' vias, check
    again, and clear the drawings (unless --keep). Dialogs are answered yes."""
    import wx

    def fake_box(msg, caption="", style=0, *a, **k):
        """wx.MessageBox stand-in: print the dialog and answer yes."""
        print("  dialog:", caption, "|", str(msg).replace("\n", " / ")[:240])
        return wx.YES
    wx.MessageBox = fake_box          # the window's confirmations are answered "yes"
    from groundtruth.adapters.file_source import load_board
    from groundtruth.adapters.kipy_source import read_board
    from groundtruth.config.settings import NetSpec
    from groundtruth.ui import main_frame

    scratch = (Path(args.dir) / "live.kicad_pcb").resolve()
    kb, path = _connect()
    if path.resolve() != scratch:
        print(f"refusing: the open board is {path}, not the scratch board {scratch}")
        return 2
    if args.revert:                   # start from the saved scratch board (drops earlier test vias and drawings)
        kb.revert()
        time.sleep(2)
        kb, path = _connect()
    lb, fb = read_board(kb), load_board(str(path))
    for name, get in (("copper layers", lambda b: b.copper_layers), ("tracks", lambda b: len(b.tracks)),
                      ("vias", lambda b: len(b.vias)), ("pads", lambda b: len(b.pads())),
                      ("zone fills", lambda b: sorted(l for z in b.zones for l in z.filled)),
                      ("stackup", lambda b: [(c.name, round(c.z, 4)) for c in b.stackup.copper])):
        print(f"  live vs file {name}: {get(lb)} | {get(fb)}{'' if get(lb) == get(fb) else '  <-- differs'}")

    def groups():
        """GroundTruth's drawing groups on the board, with their item counts."""
        return {g.name: len(g.proto.items) for g in kb.get_groups() if g.name.startswith("groundtruth")}

    def vias():
        """The board's vias (position in mm, net)."""
        return [(round(v.position.x / 1e6, 3), round(v.position.y / 1e6, 3), v.net.name) for v in kb.get_vias()]

    app = wx.App(False)
    f = main_frame.MainFrame(main_frame.Session(kicad_board=kb))
    f.Show()

    def pump(n=30):
        """Let the window handle events for about n x 20 ms."""
        for _ in range(n):
            wx.Yield()
            time.sleep(0.02)

    def wait():
        """Every slow step is a job in the window's worker thread; a finished job may start the next (placing
        vias reloads the board), so wait until no job has run for a moment."""
        t = time.time()
        while time.time() - t < 900:
            pump(5)
            if not f.busy():
                pump(15)
                if not f.busy():
                    return

    def status():
        """The window's status line."""
        return f.GetStatusBar().GetStatusText(0)

    wait()                            # the board is read by a background job
    print("loaded:", status(), "| vias:", vias())
    f.nets.nets = [NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9)]
    f.nets._commit()
    f.run(False)
    wait()
    print("check:", sorted((i.check, i.severity) for i in f.issues))
    f.on_draw_issues()
    wait()
    print("issue markers:", groups(), "|", status())
    from groundtruth.analysis.highlight import issue_highlight
    for issue in f.issues:              # every issue: select, highlight the net, zoom (in a background thread)
        kb.clear_selection()            # so a show that did nothing cannot pass on the last one's selection
        f.on_issue_selected(issue)
        assert f._highlighter.flush(15), "showing the issue did not finish"
        pump(5)                         # its status line
        sel = {s.id.value for s in kb.get_selection()}
        h = issue_highlight(issue, f.board, f.settings.config, f.routes)
        print(f"shown {issue.check}: {len(sel)} selected, lit {len(h.lit)}, layer {h.layer or '-'} | {status()}")
        assert "no action" not in status() and "could not show" not in status(), status()   # RunAction names
        assert sel == set(h.focus) | set(h.context), (sel, h)
    f.session.clear_highlight()
    kb.clear_selection()
    f.run(True)
    wait()
    rr = f.routes[0]
    print(f"simulate: {status()} | L/ideal {rr.l_ratio:.2f}, worst margin {rr.worst['margin_db']:+.1f} dB")
    f.on_draw_contours(rr, max(rr.layer_share, key=rr.layer_share.get))
    wait()
    print("contours:", groups(), "|", status())
    f.on_draw_arrows(rr, max(rr.layer_share, key=rr.layer_share.get))
    wait()
    print("current arrows:", groups(), "|", status())
    f.on_suggest()
    wait()
    print("suggestions:", [(s.id, s.kind, s.location, s.delta_db and round(s.delta_db, 1), s.selected)
                           for s in f.fixes.result.suggestions])
    ticked = f.fixes.checked()
    f.on_draw_suggestions(ticked)
    wait()
    print("suggestions drawn:", groups(), "|", status())
    f.on_place_vias(ticked)
    wait()
    print("placed:", status(), "| vias:", vias())
    f.run(False)
    wait()
    print("check after placing:", sorted((i.check, i.severity) for i in f.issues))
    if args.shot:
        _screenshot("PCB Editor", "live", args.shot)
    if args.keep:
        print("kept the drawings: look at them in KiCad, then use Clear GT drawings (or close without saving)")
    else:
        f.on_clear()
        wait()
        print("after clear:", groups(), "| vias:", vias(), "|", status())
    f.Destroy()
    app.Destroy()
    print("done: close the scratch project in KiCad without saving")
    return 0


def _screenshot(title_part: str, name_part: str, out: str):
    """Grab the KiCad window whose title contains both parts (Windows)."""
    if os.name != "nt":
        return
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def enum(hwnd, _):
        """EnumWindows callback: collect visible windows whose title has both parts."""
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if user32.IsWindowVisible(hwnd) and title_part in buf.value and name_part in buf.value:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, (r.left, r.top, r.right, r.bottom), buf.value))
        return True
    user32.EnumWindows(enum, 0)
    if not found:
        print("no window found for the screenshot")
        return
    hwnd, rect, title = found[0]
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)
    from PIL import ImageGrab
    ImageGrab.grab(bbox=rect, all_screens=True).save(out)
    print(f"screenshot of '{title}': {out}")


class _UiProbe:
    """Pings KiCad's main window every 20 ms with WM_NULL (SendMessageTimeout) from a thread and records how long
    each ping waits: how long KiCad's GUI thread was busy (frozen for the user). A GUI thread can also keep
    pumping messages while ignoring the user (a modal loop disables every window; a busy cursor), so each ping
    also notes whether all of KiCad's windows are enabled and which cursor is shown; `changes` lists the moments
    either changed. Windows only."""

    CURSORS = {32512: "arrow", 32514: "wait (hourglass)", 32650: "app starting (arrow + hourglass)"}

    def __init__(self, pid: int):
        """A probe on the visible windows of KiCad's process `pid` (the first one is pinged); the probe thread is
        made but not started."""
        import ctypes
        import ctypes.wintypes as wt
        import threading
        self.u32, self.wt, self.ctypes = ctypes.windll.user32, wt, ctypes
        self.hwnds: list[int] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def enum(hwnd, _):
            """EnumWindows callback: collect the visible windows of the process."""
            p = wt.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and self.u32.IsWindowVisible(hwnd):
                self.hwnds.append(hwnd)
            return True
        self.u32.EnumWindows(enum, 0)
        self.hwnd = self.hwnds[0] if self.hwnds else None
        self.u32.LoadCursorW.restype = wt.HANDLE
        self.cursors = {self.u32.LoadCursorW(None, wt.LPCWSTR(n)): name for n, name in self.CURSORS.items()}
        self.samples: list[tuple[float, float]] = []
        self.changes: list[tuple[float, str]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def _state(self) -> str:
        """What the user would see: whether every KiCad window is enabled, and the cursor shown."""
        class CURSORINFO(self.ctypes.Structure):
            _fields_ = [("cbSize", self.wt.DWORD), ("flags", self.wt.DWORD), ("hCursor", self.wt.HANDLE),
                        ("pt", self.wt.POINT)]
        ci = CURSORINFO(cbSize=self.ctypes.sizeof(CURSORINFO))
        self.u32.GetCursorInfo(self.ctypes.byref(ci))
        cursor = self.cursors.get(ci.hCursor, "other")
        disabled = sum(1 for h in self.hwnds if not self.u32.IsWindowEnabled(h))
        return f"KiCad windows {'enabled' if not disabled else f'{disabled} DISABLED'}, cursor {cursor}"

    def run(self):
        """The probe thread: ping every 20 ms, recording each ping's wait, and the moments the enabled/cursor state
        changed."""
        res = self.wt.DWORD()
        last = None
        while not self.stop.is_set():
            t = time.perf_counter()
            self.u32.SendMessageTimeoutW(self.hwnd, 0, 0, 0, 0, 20000, self.ctypes.byref(res))
            self.samples.append((t, time.perf_counter() - t))
            state = self._state()
            if state != last:
                self.changes.append((t, state))
                last = state
            time.sleep(0.02)

    def worst(self, t0: float, t1: float) -> float:
        """The longest ping wait overlapping [t0, t1]."""
        return max((d for t, d in self.samples if t0 <= t <= t1 or t <= t1 <= t + d), default=0.0)


def cmd_timing(args):
    """Read-only: time each API request the plugin makes to read the board, and how long KiCad's GUI was blocked
    meanwhile (a WM_NULL probe on KiCad's window). Refuses unless the open board is --expect."""
    from kipy.util.board_layer import layer_from_canonical_name

    from groundtruth.adapters.kipy_source import _KipyReader
    kb, path = _connect()
    if path.name != args.expect:
        print(f"refusing: the open board is {path.name}, not {args.expect}")
        return 2
    probe = _UiProbe(pipe_owner()) if os.name == "nt" else None
    if probe is not None and probe.hwnd is None:
        probe = None
    if probe:
        probe.thread.start()
        time.sleep(0.3)
    rows = []

    def timed(name, fn):
        """Run one request, recording its time, how long KiCad's GUI was blocked meanwhile and how many items it
        returned."""
        t0 = time.perf_counter()
        out = fn()
        t1 = time.perf_counter()
        n = len(out) if hasattr(out, "__len__") else ""
        rows.append((name, t1 - t0, probe.worst(t0, t1) if probe else float("nan"), n))
        return out

    timed("get_stackup", kb.get_stackup)
    timed("get_tracks", kb.get_tracks)
    timed("get_vias", kb.get_vias)
    fps = timed("get_footprints", kb.get_footprints)
    by_layer: dict = {}
    reader = _KipyReader(kb)
    for fpi in fps:
        for p in fpi.definition.pads:
            layers = [reader.layer_name(l) for l in p.padstack.layers]
            if layers:
                by_layer.setdefault(layers[0], []).append(p)
    for layer, pads in by_layer.items():
        if layer.endswith(".Cu"):
            timed(f"get_pad_shapes_as_polygons {layer} ({len(pads)} pads)",
                  lambda pads=pads, layer=layer: kb.get_pad_shapes_as_polygons(pads, layer_from_canonical_name(layer)))
    timed("get_zones (with fills)", kb.get_zones)
    timed("get_shapes", kb.get_shapes)
    if probe:
        time.sleep(0.3)
        probe.stop.set()
    print(f"{'request':55s} {'time':>8s} {'KiCad GUI blocked':>18s} {'items':>6s}")
    for name, dt, blocked, n in rows:
        print(f"{name:55s} {dt * 1e3:7.0f}ms {blocked * 1e3:16.0f}ms {n!s:>6s}")
    if probe:
        print(f"worst GUI block during the read: {max(d for _, d in probe.samples) * 1e3:.0f} ms")
    return 0


def cmd_freeze(args):
    """Watch KiCad's GUI thread for `--seconds` while the user clicks the plugin's toolbar button: prints every
    freeze longer than 50 ms on a timeline with the moments the plugin's Python process starts and its window
    appears. Sends no API request."""
    import ctypes
    import ctypes.wintypes as wt
    pid = pipe_owner()
    if pid is None:
        print("no KiCad API server found")
        return 1
    probe = _UiProbe(pid)
    if probe.hwnd is None:
        print("KiCad's window not found")
        return 1
    u32 = ctypes.windll.user32
    events: list[tuple[float, str]] = []
    seen_proc, seen_win = False, set()

    def plugin_windows() -> set[str]:
        """Titles of the visible plugin windows: the start window, then '<board> – GroundTruth'."""
        found = set()

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def enum(hwnd, _):
            """EnumWindows callback: collect visible windows with GroundTruth in the title."""
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if "GroundTruth" in buf.value and u32.IsWindowVisible(hwnd):
                    found.add(buf.value)
            return True
        u32.EnumWindows(enum, 0)
        return found

    def pythonw_pids() -> set[str]:
        # KiCad starts plugins with the venv's pythonw.exe (tasklist is ~10x faster than a command-line query)
        """PIDs of the running pythonw.exe processes (tasklist)."""
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
        return {line.split('","')[1] for line in r.stdout.splitlines() if line.startswith('"pythonw')}

    before = pythonw_pids()
    probe.thread.start()
    t0 = time.perf_counter()
    print(f"watching KiCad (PID {pid}) for {args.seconds} s: click the GroundTruth button now")
    while time.perf_counter() - t0 < args.seconds:
        now = time.perf_counter()
        if not seen_proc:
            new = pythonw_pids() - before
            if new:
                seen_proc = True
                events.append((now, f"new pythonw.exe running (PID {', '.join(sorted(new))})"))
        for title in plugin_windows() - seen_win:
            seen_win.add(title)
            events.append((now, f"window visible: {title}"))
        time.sleep(0.1)
    probe.stop.set()
    blocks = [(t, d) for t, d in probe.samples if d > 0.05]
    timeline = sorted([(t, f"KiCad GUI blocked {d * 1e3:.0f} ms") for t, d in blocks] + events + probe.changes)
    for t, what in timeline:
        print(f"  {t - t0:6.2f} s  {what}")
    print(f"worst block: {max((d for _, d in probe.samples), default=0) * 1e3:.0f} ms; "
          f"{len(probe.samples)} probes")
    return 0


def main(argv=None):
    """The command line: the subcommands owner, make, read, timing, freeze and write (see the module)."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("owner").set_defaults(func=cmd_owner)
    sp = sub.add_parser("make")
    sp.add_argument("dir")
    sp.set_defaults(func=cmd_make)
    sp = sub.add_parser("read")
    sp.add_argument("--expect", required=True, help="file name of the board that must be open, e.g. x.kicad_pcb")
    sp.set_defaults(func=cmd_read)
    sp = sub.add_parser("freeze")
    sp.add_argument("--seconds", type=float, default=40.0)
    sp.set_defaults(func=cmd_freeze)
    sp = sub.add_parser("timing")
    sp.add_argument("--expect", required=True, help="file name of the board that must be open")
    sp.set_defaults(func=cmd_timing)
    sp = sub.add_parser("write")
    sp.add_argument("dir")
    sp.add_argument("--shot", help="save a screenshot of the PCB editor with everything drawn")
    sp.add_argument("--revert", action="store_true", help="revert the scratch board to its saved state first")
    sp.add_argument("--keep", action="store_true", help="leave the drawings on the board at the end (no clear)")
    sp.set_defaults(func=cmd_write)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
