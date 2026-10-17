__version__ = "0.1.0"


def build_version() -> str:
    """The installed build, as written into the plugin folder's version.txt by tools/make_pcm_package.py (the
    package's version and commit) or tools/install_plugin.py (a development install); "development (source
    tree)" when running from the repository."""
    from pathlib import Path
    try:
        return (Path(__file__).resolve().parents[1] / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "development (source tree)"
