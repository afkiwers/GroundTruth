"""The plugin imports its window's modules in a start-up worker thread (ui/app.py). wx's extension modules
initialise wxWidgets on their first import, which must happen on the GUI thread: every `import wx.<ext>` at the top
of a ui module must be in app.WX_EXTENSIONS (imported on the GUI thread before the worker starts). The window did
not open when fixes_panel imported wx.html at its top (2026-11-02: "timer can only be started from the main
thread")."""
import ast
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "groundtruth" / "ui"


def _module_level_wx_imports(path: Path) -> set[str]:
    """The wx extension modules a file imports at module level (not inside a function or class)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.startswith("wx.")}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("wx."):
            out.add(node.module)
    return out


def test_every_wx_extension_imported_at_a_ui_modules_top_is_preloaded_on_the_gui_thread():
    """app.WX_EXTENSIONS covers every module-level `import wx.<ext>` under ui/ (read from the sources: importing
    app here needs no display)."""
    app = ast.parse((UI / "app.py").read_text(encoding="utf-8"))
    listed = next(ast.literal_eval(n.value) for n in app.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "WX_EXTENSIONS" for t in n.targets))
    used = {name: f.name for f in UI.glob("*.py") for name in _module_level_wx_imports(f)}
    missing = {name: where for name, where in used.items() if name not in listed}
    assert not missing, f"add to ui/app.WX_EXTENSIONS: {missing}"
    assert {"wx.html", "wx.grid"} <= set(listed)
