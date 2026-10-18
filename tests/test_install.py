"""tools/install_plugin.py: a reinstall replaces the plugin's files in place (the folder itself may be held open by
a running plugin window on Windows, so it is emptied, never removed)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _installer():
    spec = importlib.util.spec_from_file_location("install_plugin", ROOT / "tools" / "install_plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reinstall_replaces_the_files_in_the_same_folder(tmp_path):
    inst = _installer()
    target = inst.install(tmp_path)
    (target / "stale.py").write_text("old", encoding="utf-8")
    (target / "groundtruth" / "old_module.py").write_text("old", encoding="utf-8")
    folder_id = target.stat().st_ino
    assert inst.install(tmp_path) == target
    assert not (target / "stale.py").exists() and not (target / "groundtruth" / "old_module.py").exists()
    assert (target / "plugin.json").exists() and (target / "groundtruth" / "analysis" / "detect.py").exists()
    assert (target / "docs" / "REFERENCES.md").exists()          # the help's References section shows it
    assert not list(target.rglob("__pycache__"))
    assert target.stat().st_ino == folder_id                     # the same folder, emptied and refilled
