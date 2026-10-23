"""GroundTruth: return-path and EMI analysis for KiCad 10+ boards (the KiCad plugin and the `groundtruth` command line).

One version number for everything (the user: "Yes make it one number"): a build's number is pyproject's major.minor
and a counter (tools/make_pcm_package.py, e.g. 0.1.42593), and a release is tagged with it. `__version__` is that
number wherever the code runs: the KiCad plugin's and the compute server's packages carry it in version.txt, a
release's wheel (the CI install) in its metadata; a source checkout says so.
"""
from __future__ import annotations

import re
from pathlib import Path


def _version() -> str:
    """This build's number: the package's version.txt ("GroundTruth 0.1.42593 (abc1234, built ...)": the KiCad plugin,
    the compute server; "dev" for a development install's, which has none), else "dev" in the repository's source
    tree, else the installed distribution's version (a release's wheel carries its tag), else "unknown"."""
    here = Path(__file__).resolve()
    try:
        m = re.match(r"GroundTruth (\d+\.\d+\.\d+)", (here.parents[1] / "version.txt").read_text(encoding="utf-8"))
        return m.group(1) if m else "dev"   # no number: tools/install_plugin.py's development install
    except OSError:
        pass
    root = here.parents[2]
    if (root / "pyproject.toml").exists() and (root / ".git").exists():
        return "dev"                      # running from the repository: not a build (the commit says which code)
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("groundtruth-pcb")
    except Exception:  # noqa: BLE001 - PackageNotFoundError, or metadata unreadable
        return "unknown"


__version__ = _version()


def build_version() -> str:
    """The installed build, as written into the plugin folder's version.txt by tools/make_pcm_package.py (the
    package's version and commit) or tools/install_plugin.py (a development install); "development (source
    tree)" when running from the repository, and "GroundTruth <version>" for a wheel (the CI install)."""
    try:
        return (Path(__file__).resolve().parents[1] / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "development (source tree)" if __version__ == "dev" else f"GroundTruth {__version__}"
