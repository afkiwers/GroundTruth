"""Screenshots of the plugin window, for checking the look (theme, tiles, heatmaps) without KiCad.

    .venv/Scripts/python tools/dev/window_shot.py BOARD.kicad_pcb [--settings S.groundtruth.yaml] [--theme dark]
        [--simulate] [--tabs "Signal paths,Results"] [--out DIR] [--size 1920x1040] [--scroll] [--runs N]

The board (and the settings file) is copied into DIR first, so nothing is written next to a real board. Each tab
in --tabs is saved as DIR/<theme>_<tab>.png; --scroll adds the Results tab's page scrolled to its end
(<theme>_Results_scrolled.png: the cross-section and the spectrum under the maps). --runs N simulates N times
(kept in DIR/groundtruth/history when Preferences keep runs); the History tab is captured with its two newest runs
compared. Captured with Win32 PrintWindow, which works while the screen
itself cannot be read (a locked or minimised remote desktop)."""
import argparse
import ctypes
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def capture(frame, path: Path) -> bool:
    """Save the window as a PNG: Windows' PrintWindow into a bitmap (with PW_RENDERFULLCONTENT, so hardware-drawn
    content is included), copied into a wx bitmap. False when PrintWindow failed."""
    import wx
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # handles are pointer-sized: without these, ctypes passes them as 32-bit ints ("int too long to convert")
    H, I = ctypes.c_void_p, ctypes.c_int
    user32.GetWindowDC.argtypes, user32.GetWindowDC.restype = [H], H
    user32.ReleaseDC.argtypes = [H, H]
    user32.PrintWindow.argtypes = [H, H, ctypes.c_uint]
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [H], H
    gdi32.CreateCompatibleBitmap.argtypes, gdi32.CreateCompatibleBitmap.restype = [H, I, I], H
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [H, H], H
    gdi32.BitBlt.argtypes = [H, I, I, I, I, H, I, I, ctypes.c_uint32]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    hwnd = frame.GetHandle()
    w, h = frame.GetSize()
    hdc_win = user32.GetWindowDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc, hbmp)
    ok = bool(user32.PrintWindow(hwnd, hdc, 2))          # PW_RENDERFULLCONTENT
    bmp = wx.Bitmap(w, h, 24)
    mem = wx.MemoryDC(bmp)
    gdi32.BitBlt(mem.GetHandle(), 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
    mem.SelectObject(wx.NullBitmap)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(hwnd, hdc_win)
    img = bmp.ConvertToImage()
    if img.HasAlpha():
        img.ClearAlpha()
    img.SaveFile(str(path), wx.BITMAP_TYPE_PNG)
    return ok


def main() -> int:
    """The command line (see the module): open the window on a copy of the board, optionally simulate, then capture
    each tab asked for once the window has been idle."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--settings")
    ap.add_argument("--theme", default="system", choices=["system", "light", "dark"])
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--tabs", default="Signal paths,Converters,Results")
    ap.add_argument("--out")
    ap.add_argument("--size", default="1400x900")
    ap.add_argument("--scroll", action="store_true", help="also the Results tab scrolled down to its last plot")
    ap.add_argument("--runs", type=int, default=0, help="simulate this many times (the History tab's runs)")
    ap.add_argument("--real-prefs", action="store_true",
                   help="use this computer's Preferences and server job list (default: a private folder, so the "
                        "window computes here and never touches the user's ui.json or remote_jobs)")
    args = ap.parse_args()
    if not args.real_prefs:
        # a private %LOCALAPPDATA%\GroundTruth: the user's Preferences (a compute server) and job list stay out of it
        import os
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="window_shot_prefs_")
    out = Path(args.out or tempfile.mkdtemp(prefix="gt_shot_"))
    out.mkdir(parents=True, exist_ok=True)
    board = out / Path(args.board).name
    shutil.copy2(args.board, board)
    pro = Path(args.board).with_suffix(".kicad_pro")
    if pro.exists():
        shutil.copy2(pro, out / pro.name)
    if args.settings:
        from groundtruth.config.settings import settings_path
        settings_path(board).parent.mkdir(exist_ok=True)
        shutil.copy2(args.settings, settings_path(board))
    import wx
    from groundtruth.ui import theme
    app = wx.App(False)
    theme.init_app(app, args.theme)
    wx.MessageBox = lambda msg, *a, **k: (print(f"  [message box] {msg.splitlines()[0][:100]}"), wx.NO)[1]
    from groundtruth.ui import main_frame
    f = main_frame.MainFrame(main_frame.Session(file_path=str(board)))
    w, h = (int(v) for v in args.size.split("x"))
    f.SetSize((w, h))
    f.Centre()
    f.Show()
    steps = [lambda: f.run(False)] + [lambda: f.run(True)] * max(args.runs, 1 if args.simulate else 0)
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    if args.scroll and "Results" in tabs:
        tabs.insert(tabs.index("Results") + 1, "Results scrolled")
    state = {"idle": 0, "phase": "steps"}

    def tick(_):
        """Timer every 100 ms: once the window has been idle for 8 ticks (no job, render or history work), take the
        next step (switch a tab, capture, scroll), and close when done."""
        busy = f.busy() or (hasattr(f, "results_panel") and (f.results_panel.rendering() or f.history_busy()))
        state["idle"] = 0 if busy else state["idle"] + 1
        if state["idle"] < 8:                             # 8 x 100 ms idle
            return
        state["idle"] = 0
        if steps:
            steps.pop(0)()
            return
        if tabs:
            name = tabs.pop(0)
            if name == "Results scrolled":                # the Results tab's page scrolled to its end
                if state.get("scrolled") is None:
                    f.results_panel.page.Scroll(-1, f.results_panel.page.GetVirtualSize().height)
                    state["scrolled"] = True
                    tabs.insert(0, name)                  # capture on the next idle tick, once painted
                    return
                path = out / f"{args.theme}_Results_scrolled.png"
                print(f"{path}: {'ok' if capture(f, path) else 'PrintWindow failed'}")
                return
            if name == "History" and len(f.history.runs) >= 2 and state.get("compared") is None:
                f.history.list.Select(0)
                f.history.list.Select(1)
                f.history._selection_changed()
                f.history._on_button("compare")
                state["compared"] = True
                tabs.insert(0, name)
                return
            k = next(i for i in range(f.notebook.GetPageCount()) if f.notebook.GetPageText(i) == name)
            if f.notebook.GetSelection() != k:
                f.notebook.SetSelection(k)
                tabs.insert(0, name)                      # capture on the next idle tick, once painted
                state["shot"] = name
                return
            path = out / f"{args.theme}_{name}.png"
            print(f"{path}: {'ok' if capture(f, path) else 'PrintWindow failed'}")
            if name == "Results" and f.results_panel.routes:
                print(f"  tiles: {f.results_panel.tile_text()}")
            return
        timer.Stop()
        f.Destroy()
        app.ExitMainLoop()
    timer = wx.Timer()
    timer.Bind(wx.EVT_TIMER, tick)
    timer.Start(100)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
