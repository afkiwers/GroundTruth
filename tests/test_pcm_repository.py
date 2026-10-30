"""The release workflow's tools (.github/workflows/release.yml): the KiCad repository made from release packages
(tools/make_pcm_repository.py) and the release key's guards (a build signs only with the key the servers trust, and
never makes a new one while tools/release_key.pub names it; no private key in what is uploaded)."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import make_pcm_repository as repo  # noqa: E402

META = {"$schema": "https://go.kicad.org/pcm/schemas/v1", "name": "GroundTruth", "description": "d",
        "description_full": "dd", "identifier": "io.github.afkiwers.groundtruth", "type": "plugin",
        "author": {"name": "afkiwers", "contact": {"email": "a@b.c"}}, "license": "GPL-3.0", "resources": {},
        "tags": ["emi"]}


def _package(path: Path, version: str, status: str = "stable") -> Path:
    """A package zip as make_pcm_package.py lays it out: metadata.json with one version, the icon, a plugin file."""
    meta = dict(META, versions=[{"version": version, "status": status, "kicad_version": "10.0", "runtime": "ipc"}])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta))
        z.writestr("resources/icon.png", b"\x89PNG icon")
        z.writestr("plugins/plugin.json", "{}" * 500)
    return path


def test_the_kicad_repository_lists_every_release_with_its_download(tmp_path):
    """entry: the version with KiCad's download fields (the zip's SHA-256 and size, the unpacked size), the
    archive's own metadata.json left without them. site: packages.json with the newest release's metadata and every
    version newest first (one per number), resources.zip with <identifier>/icon.png, repository.json pointing at
    both with their SHA-256 and UTC time in KiCad's format; the PCM schema's required fields are all there."""
    old = _package(tmp_path / "groundtruth-0.1.42000-aaa-pcm.zip", "0.1.42000")
    new = _package(tmp_path / "groundtruth-0.1.42600-bbb-pcm.zip", "0.1.42600", "testing")
    e_old = repo.entry(old, "https://x/releases/download/v1/old.zip")
    e_new = repo.entry(new, "https://x/releases/download/v2/new.zip")
    data = new.read_bytes()
    v = e_new["version"]
    assert v["download_sha256"] == hashlib.sha256(data).hexdigest() and v["download_size"] == len(data)
    assert v["install_size"] == sum(i.file_size for i in zipfile.ZipFile(new).infolist())
    assert v["status"] == "testing" and v["runtime"] == "ipc" and "versions" not in e_new["package"]
    assert "download_url" not in zipfile.ZipFile(new).read("metadata.json").decode()
    now = datetime(2026, 10, 23, 9, 30, 5, tzinfo=timezone.utc)
    files = repo.site([e_old, e_new, e_new], new, "https://afkiwers.github.io/GroundTruth/", now)
    packages = json.loads(files["packages.json"])
    (pkg,) = packages["packages"]
    assert [x["version"] for x in pkg["versions"]] == ["0.1.42600", "0.1.42000"]
    for key in ("name", "description", "description_full", "identifier", "type", "author", "license", "resources",
                "versions"):
        assert key in pkg, key
    for x in pkg["versions"]:
        assert re.fullmatch(r"\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?", x["version"]) and x["kicad_version"] == "10.0"
        assert re.fullmatch(r"[a-f0-9]{64}", x["download_sha256"])
    with zipfile.ZipFile(io.BytesIO(files["resources.zip"])) as z:
        assert z.namelist() == ["io.github.afkiwers.groundtruth/icon.png"]
    r = json.loads(files["repository.json"])
    assert r["name"] == "GroundTruth" and "schema_version" not in r
    assert r["packages"]["url"] == "https://afkiwers.github.io/GroundTruth/packages.json"
    assert r["packages"]["sha256"] == hashlib.sha256(files["packages.json"]).hexdigest()
    assert r["resources"]["sha256"] == hashlib.sha256(files["resources.zip"]).hexdigest()
    assert r["packages"]["update_time_utc"] == "2026-10-23 09:30:05"
    assert r["packages"]["update_timestamp"] == int(now.timestamp())


def test_a_private_key_is_found_in_files_folders_and_zips(tmp_path):
    """check-no-key: a PEM private key in a file, in a folder or inside a zip is found (the workflow then stops
    before uploading); the public half (hex) and ordinary files are not."""
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "release_key.pub").write_text("38534d7f" * 8 + "\n")
    _package(clean / "p.zip", "0.1.1")
    assert repo.key_inside(clean) == []
    # the header put together here, so this file itself holds no key-looking line (the workflow checks the repo)
    pem = b"-----BEGIN " + b"PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI...\n-----END " + b"PRIVATE KEY-----\n"
    with zipfile.ZipFile(clean / "leak.zip", "w") as z:
        z.writestr("plugins/key.pem", pem)
    (clean / "notes.txt").write_bytes(b"x" + pem)
    found = repo.key_inside(clean)
    assert any(f.endswith("leak.zip!plugins/key.pem") for f in found) and any("notes.txt" in f for f in found)


def test_the_repository_holds_the_public_half_only():
    """The repository (as committed) has no private key, and tools/release_key.pub is a 32-byte Ed25519 public key in
    hex; .gitignore keeps key files out."""
    import subprocess
    # the release workflow's own check over the tracked files (its PEM pattern does not match itself)
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout.split(b"\0")
    assert [f for name in tracked if name for f in repo.key_inside(ROOT / name.decode())] == []
    assert re.fullmatch(r"[0-9a-f]{64}", (ROOT / "tools" / "release_key.pub").read_text().strip())
    ignore = (ROOT / ".gitignore").read_text()
    assert "*.pem" in ignore and "release_ed25519*" in ignore


def test_a_build_signs_only_with_the_key_the_servers_trust(tmp_path, monkeypatch):
    """make_server_package.build with tools/release_key.pub present: a key from the environment (the workflow's
    secret) that is not the trusted one stops the build, a missing one too (no new key is made); the trusted one
    signs, and the package carries that public half."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import make_server_package as msp
    from groundtruth.remote import release

    def write_key(path):
        """A new Ed25519 key in PEM at path; its public half in hex."""
        k = Ed25519PrivateKey.generate()
        path.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        return release.public_hex(k)
    trusted = tmp_path / "trusted.pem"
    other = tmp_path / "other.pem"
    pub = tmp_path / "release_key.pub"
    pub.write_text(write_key(trusted) + "\n")
    write_key(other)
    monkeypatch.setenv(msp.PUB_ENV, str(pub))
    monkeypatch.setattr(msp, "source_files", lambda working_tree=False: {"groundtruth/__init__.py": b""})
    monkeypatch.setenv(release.KEY_ENV, str(other))
    with pytest.raises(SystemExit, match="not the one the servers trust"):
        msp.build("0.1.1", "abc", "GroundTruth 0.1.1")
    monkeypatch.setenv(release.KEY_ENV, str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit, match="no release key"):
        msp.build("0.1.1", "abc", "GroundTruth 0.1.1")
    assert not (tmp_path / "missing.pem").exists()                 # no new key made
    monkeypatch.setenv(release.KEY_ENV, str(trusted))
    folder, data, sig = msp.build("0.1.1", "abc", "GroundTruth 0.1.1")
    expected = pub.read_text().strip()
    assert release.verify(expected, data, sig)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.read(f"{folder}/{release.PUBLIC_FILE}").decode().strip() == expected
