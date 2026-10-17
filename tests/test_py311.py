"""The plugin runs on KiCad's bundled Python 3.11; the dev venv is 3.13. Syntax only 3.12+ accepts must not reach
the plugin (tools/dev/py311_check.py). Skipped where KiCad 10 is not installed (CI)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "dev"))
from py311_check import KICAD_PYTHON, check  # noqa: E402


@pytest.mark.skipif(not KICAD_PYTHON.exists(), reason="KiCad 10's Python is not installed here")
def test_the_plugin_code_compiles_on_kicads_python():
    r = check(KICAD_PYTHON)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("3.11")
