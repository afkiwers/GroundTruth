"""The Plugin and Content Manager package (tools/make_pcm_package.py): KiCad's layout, metadata that passes the PCM
schema's rules (checked against the published schema when the package was first built), from the last commit."""
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_pcm_package_has_kicads_layout_and_valid_metadata(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "make_pcm_package.py"), "--out", str(tmp_path)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    (zpath,) = tmp_path.glob("groundtruth-*-pcm.zip")
    z = zipfile.ZipFile(zpath)
    names = set(z.namelist())
    assert {n.split("/")[0] for n in names} == {"metadata.json", "plugins", "resources"}
    assert {"plugins/plugin.json", "plugins/groundtruth_action.py", "plugins/requirements.txt",
            "plugins/groundtruth/solver/peec.py", "plugins/demo/groundtruth_demo.kicad_pro",
            "plugins/docs/REFERENCES.md", "resources/icon.png"} <= names     # the references: the help shows them
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".lck", ".kicad_prl"))]
    m = json.loads(z.read("metadata.json"))
    plugin = json.loads(z.read("plugins/plugin.json"))
    assert m["identifier"] == plugin["identifier"] and m["type"] == "plugin"
    assert len(m["description"]) <= 150 and "contact" in m["author"]
    assert all(re.fullmatch(r"[a-z][-a-z0-9]{0,48}[a-z0-9]", t) for t in m["tags"])          # the schema's rule
    (v,) = m["versions"]
    assert v["runtime"] == "ipc" and v["kicad_version"] == "10.0" and v["status"] in ("stable", "testing")
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(z.read("resources/icon.png"))).size == (64, 64)


def test_a_release_package_carries_its_tag_as_the_version(tmp_path):
    """--version (the release workflow passes its tag, a build number the user tagged it with): the package's
    metadata, version.txt and the zip's name carry exactly it, a leading v dropped; --print-version shows the current
    build number; a tag that is not major.minor.patch, or not pyproject's major.minor, is refused."""
    tool = [sys.executable, str(ROOT / "tools" / "make_pcm_package.py"), "--out", str(tmp_path)]
    now = subprocess.run(tool + ["--print-version"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    major_minor, build = now.rsplit(".", 1)
    tag = f"{major_minor}.{int(build) - 7}"              # a build number tagged earlier: kept exactly
    r = subprocess.run(tool + ["--version", "v" + tag, "--status", "stable"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    (zpath,) = tmp_path.glob("groundtruth-*-pcm.zip")
    z = zipfile.ZipFile(zpath)
    (v,) = json.loads(z.read("metadata.json"))["versions"]
    assert v["version"] == tag and v["status"] == "stable" and zpath.name.startswith(f"groundtruth-{tag}-")
    assert z.read("plugins/version.txt").decode().startswith(f"GroundTruth {tag} ")
    r = subprocess.run(tool + ["--version", "release-1"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "major.minor.patch" in r.stderr
    r = subprocess.run(tool + ["--version", "9.9.1"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "build numbering" in r.stderr and "--print-version" in r.stderr
