"""out/dist/groundtruth-server-<version>-<commit>.zip: everything a server PC needs to run the GroundTruth compute
server as a background service (tools/server_setup.py, the double-click tools/install_server.cmd and
install_server.command, tools/SERVER_README.md as README.md, the groundtruth package, the plugin's requirements.txt
and a version.txt), from HEAD. The version matches the KiCad package built at the same time
(tools/make_pcm_package.py): the server and the users' plugins must run the same analysis code.

Signed (remote/release.py): the package holds release_key.pub, the public half of this PC's release key (made on the
first build, kept in %LOCALAPPDATA%\\GroundTruth\\release\\, never in the repository; on GitHub the release workflow's
secret, .github/workflows/release.yml), which a server trusts from its install on; the build checks the key against
tools/release_key.pub (the trusted public half) and never makes a new one while that file exists; `<zip>.sig` beside the zip is the key's Ed25519 signature of the zip's bytes. The KiCad package carries
such a signed server package of its own build, for the plugin's remote update of the servers.

    .venv/Scripts/python tools/make_server_package.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FILES = {"tools/server_setup.py": "server_setup.py", "tools/install_server.cmd": "install_server.cmd",
         "tools/install_server.command": "install_server.command",
         "tools/SERVER_README.md": "README.md", "plugin/requirements.txt": "requirements.txt"}
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)       # as make_pcm_package.py: the same version for both
# the public half (hex) of the release key the installed servers trust: public, so it is in the repository; a
# build checks its key against it (the private half never is: on the build PC, or the release workflow's secret)
EXPECTED_PUBLIC = ROOT / "tools" / "release_key.pub"
PUB_ENV = "GROUNDTRUTH_RELEASE_PUB"     # another trusted public half's file (the tests: their own, or none)


def expected_public() -> Path:
    """The trusted public half's file: the one PUB_ENV names, else EXPECTED_PUBLIC. A file that is not there: no
    check, and a missing key is made (the tests build with throwaway keys so)."""
    return Path(os.environ[PUB_ENV]) if os.environ.get(PUB_ENV) else EXPECTED_PUBLIC


def git(*args: str) -> bytes:
    """Run git in the repo and return its output."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def zip_entry(name: str, executable: bool = False) -> zipfile.ZipInfo:
    """A zip entry with Unix permissions macOS honours: rwxr-xr-x for a launcher, else rw-r--r--.

    The creator system must say Unix (3): built on Windows, zipfile writes 0 (MS-DOS), and macOS's Archive Utility
    then ignores the mode bits, so install_server.command unpacked without its execute bit and a double-click
    failed with "you do not have appropriate access privileges" (the user's first try on a Mac, 2026-10-15)."""
    info = zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6])
    info.create_system = 3
    info.external_attr = (0o100755 if executable else 0o100644) << 16     # a regular file and its mode
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def source_files(working_tree: bool = False) -> dict[str, bytes]:
    """FILES and src/groundtruth as in the last commit (or, `working_tree`, as on disk), by their place in the
    package (without its top folder)."""
    out = {}
    if working_tree:
        for src, name in FILES.items():
            out[name] = (ROOT / src).read_bytes()
        base = ROOT / "src" / "groundtruth"
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                out["groundtruth/" + p.relative_to(base).as_posix()] = p.read_bytes()
        return out
    for src, name in FILES.items():
        out[name] = git("show", f"HEAD:{src}")
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", "src/groundtruth")))
    for m in tar.getmembers():
        if m.isfile() and "__pycache__" not in m.name and not m.name.endswith(".pyc"):
            out[m.name[len("src/"):]] = tar.extractfile(m).read()
    return out


def build(version: str, tag: str, version_text: str, working_tree: bool = False) -> tuple[str, bytes, str]:
    """(the package's folder name, the zip's bytes, its signature hex): the files (source_files), version.txt with
    `version_text` (the same text as the KiCad package's of this build), release_key.pub and the signature of the
    zip by this PC's release key (made on the first build). With tools/release_key.pub in the repository (the public
    half of the key the servers trust) no key is ever made, and a key that does not match it stops the build: a
    package signed by another key would be refused by every server (a PC or a CI run without the right key)."""
    from groundtruth.remote import release
    trusted = expected_public()
    expected = trusted.read_text(encoding="utf-8").strip() if trusted.exists() else None
    try:
        private, made = release.load_private(create=expected is None and not os.environ.get(release.KEY_ENV))
    except FileNotFoundError:
        raise SystemExit(f"no release key at {release.private_key_path()}: copy the key the servers trust there (or "
                         f"name it in {release.KEY_ENV}); a new key would not be accepted by them")
    if expected is not None and release.public_hex(private) != expected:
        raise SystemExit(f"the release key at {release.private_key_path()} is not the one the servers trust "
                         f"({trusted.name}): its packages would be refused")
    if made:
        print(f"made a new release key: {release.private_key_path()} (keep it, and back it up: servers installed from "
              "its packages take remote updates only from packages signed with it)", file=sys.stderr)
    folder = f"groundtruth-server-{version}-{tag}"
    files = source_files(working_tree)
    files["version.txt"] = version_text.encode("utf-8")
    files[release.PUBLIC_FILE] = (release.public_hex(private) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            data = files[name]
            if name.endswith(".cmd"):           # git keeps LF; cmd.exe wants CRLF (labels and blocks misparse)
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if name.endswith(".command"):       # macOS: LF only (bash reads a CR as part of the word), executable
                z.writestr(zip_entry(f"{folder}/{name}", executable=True), data.replace(b"\r\n", b"\n"))
            else:
                z.writestr(zip_entry(f"{folder}/{name}"), data)
    data = buf.getvalue()
    return folder, data, release.sign(private, data)


def main() -> int:
    """Build out/dist/groundtruth-server-*.zip (and its .sig) from the last commit."""
    import tomllib
    commit = git("rev-parse", "--short", "HEAD").decode().strip()
    major_minor = ".".join(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
                           .split(".")[:2])
    version = f"{major_minor}.{int((datetime.now(timezone.utc) - EPOCH).total_seconds() // 600)}"
    text = f"GroundTruth {version} ({commit}, built {datetime.now():%Y-%m-%d %H:%M})\n"
    folder, data, sig = build(version, commit, text)
    out = ROOT / "out" / "dist" / f"{folder}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    out.with_name(out.name + ".sig").write_text(sig + "\n", encoding="utf-8")
    with zipfile.ZipFile(out) as z:
        n = len(z.namelist())
    print(out)
    print(f"  {out.stat().st_size / 1e6:.2f} MB, {n} files, version {version}, from commit {commit}; signed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
