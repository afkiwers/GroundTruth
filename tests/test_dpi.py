"""The GUI process declares itself DPI-aware before its first window (ui/dpi.py), so Windows draws it at the
display's scaling instead of stretching a 100 % bitmap (blurry at 125-150 %). Each check runs in a fresh process:
DPI awareness can be set only once per process."""
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPI awareness is a Windows setting")

CHECK = r"""
import ctypes, sys
sys.path.insert(0, {src!r})
from groundtruth.ui.dpi import dpi_aware
got = dpi_aware() if {declare} else ""
u = ctypes.windll.user32
u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
u.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
v2 = bool(u.AreDpiAwarenessContextsEqual(u.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4)))
import wx
app = wx.App(False)
frame = wx.Frame(None)
u.GetDpiForWindow.argtypes = [ctypes.c_void_p]
window_dpi = u.GetDpiForWindow(ctypes.c_void_p(frame.GetHandle()))
u.GetDpiForSystem.restype = ctypes.c_uint
print(got, "|", v2, "|", window_dpi, "|", u.GetDpiForSystem())
frame.Destroy()
"""


def _run(declare: bool) -> list[str]:
    """The check in a fresh Python: (what dpi_aware set, per-monitor v2?, the frame's DPI, the system's DPI)."""
    import groundtruth
    from pathlib import Path
    src = str(Path(groundtruth.__file__).parents[1])
    out = subprocess.run([sys.executable, "-c", CHECK.format(src=src, declare=declare)], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [s.strip() for s in out.stdout.strip().splitlines()[-1].split("|")]


def test_the_gui_process_is_per_monitor_dpi_aware():
    """dpi_aware sets per-monitor v2, and a wx frame made afterwards reports the display's real DPI (a DPI-unaware
    window reports 96 on every display and is stretched by Windows)."""
    pytest.importorskip("wx")
    got, v2, window_dpi, _ = _run(declare=True)
    assert got == "per-monitor v2" and v2 == "True"
    unaware_got, unaware_v2, unaware_dpi, system_dpi = _run(declare=False)
    assert unaware_v2 == "False" and unaware_dpi == "96"         # without it: Windows' stretched 100 %
    if system_dpi != "96":                                        # a scaled display: the frame now sees it
        assert window_dpi != "96"


def test_the_plugin_and_the_gui_command_declare_it_before_the_app(monkeypatch):
    """Both entry points (plugin_main, gui_main) call dpi_aware before they make wx.App."""
    import inspect
    from groundtruth.ui import app
    for fn in (app.plugin_main, app.gui_main):
        code = inspect.getsource(fn)
        assert code.index("dpi_aware()") < code.index("wx.App(False)"), fn.__name__
