"""The help's "Third-party software and licences" names every Python package GroundTruth installs (tools/dev/
licenses.py: the plugin's requirements, the shipping extras and what they pull in, as installed here), and the
project's licence is the same everywhere (the user: "fix this -> ... README and LICENSE indicate GPL-3.0, but pyproject
and PCM metadata say MIT")."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    """tools/dev/licenses.py as a module."""
    spec = importlib.util.spec_from_file_location("licenses_tool", ROOT / "tools" / "dev" / "licenses.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_help_names_every_installed_package():
    """Every package of the closure installed in this environment is named in the help's licence section (a new
    dependency without its licence there fails here)."""
    tool = _tool()
    rows = tool.closure()
    names = {tool.norm(r[0]) for r in rows}
    assert {"numpy", "scipy", "kicad-python", "matplotlib"} <= names
    assert tool.unnamed(rows) == []


def test_the_licence_is_gpl_3_everywhere():
    """LICENSE is the GNU GPL v3; README, pyproject.toml and the KiCad package's metadata say so."""
    assert "GNU GENERAL PUBLIC LICENSE" in (ROOT / "LICENSE").read_text(encoding="utf-8")[:200]
    assert "Version 3" in (ROOT / "LICENSE").read_text(encoding="utf-8")[:200]
    assert "GNU General Public License v3.0" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r'^license = \{ text = "GPL-3\.0-only" \}', (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                     re.M)
    assert '"license": "GPL-3.0",' in (ROOT / "tools" / "make_pcm_package.py").read_text(encoding="utf-8")
    help_page = (ROOT / "src" / "groundtruth" / "ui" / "help.html").read_text(encoding="utf-8")
    assert "GNU General Public License v3.0" in help_page.split('<a name="licenses">', 1)[1]
