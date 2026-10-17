"""Signed remote updates of the compute servers.

The user (2026-10-16): "Currently I have to physically go to the servers an update them manually. Can we have a
feature that can be enabled when the server is installed for the first time that allows it to update remotely? ...
when the groundtruth plugin is open and the user goes to preferences, it will show the user which server is out of
date and offers to update it through a button press". Asked: only builds signed by the user's release key may update
a server ("Signed builds only"), and a busy server waits for its running jobs ("Wait for running jobs").

An update replaces code that runs as SYSTEM (root on a Mac), so three things must hold before a server takes one
(server.ComputeServer._take_update):

1. the server was installed with remote updates on (`install_server.cmd install --remote-update on`, asked on a first
   install; server.json `remote_update`), and runs as the installed service (its runner applies the update);
2. the package is signed with the release key whose public half the server got from its own install
   (`release_key.pub` in the install folder): an Ed25519 signature over the server package's zip bytes. The private
   half stays on the build PC (`private_key_path()`, never in the repository); the access key alone can't push code;
3. the package is newer than the server's own build (`version_number`): an old signed package can't be replayed to
   take a server back to a version with a known fault.

The request itself is signed with the access key like every other (protocol.py).

The KiCad package carries the server package of the same build (tools/make_pcm_package.py: `plugins/server_update/`),
so a plugin updates the servers to exactly its own version (`bundled`).
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

PUBLIC_FILE = "release_key.pub"             # the release key's public half, hex: in the server package and install
UPDATE_DIR = "server_update"                # in the plugin folder: the server package of the plugin's own build
UPDATE_ZIP = "groundtruth-server.zip"
UPDATE_SIG = UPDATE_ZIP + ".sig"            # the Ed25519 signature of the zip's bytes, hex
MAX_UPDATE = 64 * 2 ** 20                   # a server package is about 1 MB
UPDATE_EXIT = 75                            # the server's exit code when an update waits for the service's runner
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def private_key_path() -> Path:
    """The release key's private half on the build PC: next to this user's ui.json, never in the repository."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "GroundTruth" if base else Path.home() / ".groundtruth") / "release" / "release_ed25519.pem"


def load_private(path: Optional[Path] = None, create: bool = False):
    """(the private key, made now): read from `path` (private_key_path()); with `create` made when missing. Raises
    FileNotFoundError without it, ImportError without the cryptography library."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = Path(path or private_key_path())
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None), False
    if not create:
        raise FileNotFoundError(path)
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    # written for this user only where the system allows it (a profile folder is private on Windows anyway)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key, True


def public_hex(private) -> str:
    """The public half of a private key, as the 64 hex digits servers keep in release_key.pub."""
    from cryptography.hazmat.primitives import serialization
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def sign(private, data: bytes) -> str:
    """The Ed25519 signature of `data`, hex."""
    return private.sign(data).hex()


def verify(public: str, data: bytes, signature: str) -> bool:
    """Whether `signature` (hex) is the release key `public`'s (hex) over `data`. False for anything malformed."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public.strip()))
        key.verify(bytes.fromhex(signature.strip()), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def version_number(text: Optional[str]) -> Optional[tuple]:
    """(major, minor, build) of a build's text ("GroundTruth 0.1.41452 (57602a8, built ...)"); None for a development
    copy ("development (source tree)") or anything else."""
    m = VERSION.search(text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def zip_version(data: bytes) -> str:
    """The build text in a server package (its version.txt, in the package's one top folder); "" without one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.count("/") == 1 and n.endswith("/version.txt")), None)
            return z.read(name).decode("utf-8").strip() if name else ""
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return ""


def safe_names(names: list) -> bool:
    """Whether every entry of a zip unpacks inside its target folder (no absolute paths, drive letters or ..)."""
    for n in names:
        p = n.replace("\\", "/")
        if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or any(part == ".." for part in p.split("/")):
            return False
    return True


def plugin_dir() -> Path:
    """The installed plugin's folder (the groundtruth package's parent)."""
    return Path(__file__).resolve().parents[2]


def bundled(folder: Optional[Path] = None) -> Optional[dict]:
    """The server package this plugin carries ({zip: Path, sig: hex, version: its build text}), None in a development
    copy or a package built without it."""
    d = Path(folder or plugin_dir()) / UPDATE_DIR
    z, s = d / UPDATE_ZIP, d / UPDATE_SIG
    try:
        data = z.read_bytes()
        return {"zip": z, "sig": s.read_text(encoding="utf-8").strip(), "version": zip_version(data)}
    except OSError:
        return None


def compare(status: dict, mine_version: str, mine_code: str) -> tuple[str, str]:
    """(state, text) of a server against this plugin: "same" (the same code), "older" (an earlier build: offer the
    update), "newer" (a later build than the plugin: update the plugin instead), "unknown" (no build number)."""
    if status.get("code") == mine_code:
        return "same", "up to date"
    theirs, mine = version_number(status.get("version")), version_number(mine_version)
    if theirs is None or mine is None:
        return ("older", "out of date") if theirs is None and mine is not None else ("unknown", "another build")
    if theirs < mine:
        return "older", "out of date"
    if theirs > mine:
        return "newer", "newer than this plugin"
    return "unknown", "same build, other code"
