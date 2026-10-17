"""What the plugin and the compute server (remote/server.py) exchange, and how each side trusts what it gets.

The jobs and their results are Python objects (a Board and its Config in, a SimulationResult out), sent as pickles:
the same objects the thermal child process gets (analysis/thermal_proc.py). Unpickling can run code, so a pickle is
only ever read after two checks:

- **Signed.** Every request and every response carries an HMAC-SHA256 over its method, path, time, a one-off nonce
  and the SHA-256 of its body, made with the access key both sides share (`groundtruth serve` makes it). Nobody
  without the key can send the server a job or the plugin a result; a request older than MAX_SKEW_S, or a nonce
  seen before, is refused (no replay), and a response is bound to its request's nonce (an old result can't be
  passed off as a new one).
- **Restricted.** `loads` builds only GroundTruth's own classes, NumPy arrays and a few plain builtins
  (SafeUnpickler); any other global (os.system, subprocess, eval ...) is refused. A defence in depth: a signed
  pickle already comes from someone holding the key.

The body is the pickle compressed with zlib (level 1: fast; a result's maps shrink to about a third). The traffic
is not encrypted: the server is meant for the local network, as the user's (2026-10-13: "Local network").

`code_id()` names the exact code on each side (a hash of the package's .py files): the server refuses a job from a
plugin of another version, since the result's classes and numbers must match what the window expects.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import pickle
import secrets
import time
import zlib
from pathlib import Path
from typing import Optional

PROTOCOL = 1                       # the request paths are /v1/...
MAX_SKEW_S = 300.0                 # a request's time may differ from the server's clock by this much
DEFAULT_PORT = 8765
KEY_ENV = "GROUNDTRUTH_SERVER_KEY"  # the access key from the environment (tests, a service), else the key file

# Builtins a result may hold: numbers, containers. Everything else of builtins (eval, exec, getattr, __import__,
# open ...) is refused.
_BUILTINS = {"complex", "set", "frozenset", "slice", "range", "bytearray", "bytes", "float", "int", "list", "dict",
             "tuple", "str", "bool", "object"}
# NumPy's array and scalar reconstruction (numpy 2 moved numpy.core to numpy._core)
_NUMPY = {("numpy", "dtype"), ("numpy", "ndarray"),
          ("numpy._core.numeric", "_frombuffer"), ("numpy.core.numeric", "_frombuffer"),
          ("numpy._core.multiarray", "_reconstruct"), ("numpy.core.multiarray", "_reconstruct"),
          ("numpy._core.multiarray", "scalar"), ("numpy.core.multiarray", "scalar")}
_PLAIN = {("collections", "OrderedDict"), ("collections", "defaultdict"), ("datetime", "datetime"),
          ("datetime", "date"), ("datetime", "timedelta"), ("datetime", "timezone"), ("copyreg", "_reconstructor")}


class ProtocolError(Exception):
    """A request or response that fails a check: no or a wrong signature, too old, replayed, refused content."""


class SafeUnpickler(pickle.Unpickler):
    """An unpickler that builds only GroundTruth's classes (types, not functions), NumPy arrays and plain builtins."""

    def find_class(self, module: str, name: str):
        """The class `module.name` if it is allowed, else ProtocolError."""
        if module == "builtins" and name in _BUILTINS:
            return super().find_class(module, name)
        if (module, name) in _NUMPY or (module, name) in _PLAIN:
            return super().find_class(module, name)
        if module.startswith("numpy.") and name.endswith("DType"):       # numpy 2's dtype classes (Float64DType)
            return super().find_class(module, name)
        if module == "groundtruth" or module.startswith("groundtruth."):
            obj = super().find_class(module, name)
            if isinstance(obj, type):                   # a class: built from its own state, never a function call
                return obj
        raise ProtocolError(f"refused to load {module}.{name}")


def dumps(obj) -> bytes:
    """`obj` as a compressed pickle (the body of a job or a result)."""
    return zlib.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), 1)


def loads(data: bytes):
    """The object of a body made by dumps, with SafeUnpickler. Only call it on a body whose signature checked."""
    return SafeUnpickler(io.BytesIO(zlib.decompress(data))).load()


def load_raw(data: bytes):
    """An uncompressed pickle (a job's input as the server keeps it) read with SafeUnpickler."""
    return SafeUnpickler(io.BytesIO(data)).load()


def load_file(path) -> object:
    """An uncompressed pickle file read with SafeUnpickler (the server's worker reads its job so)."""
    with open(path, "rb") as f:
        return SafeUnpickler(f).load()


def _digest(key: str, parts: list[str]) -> str:
    """HMAC-SHA256 of the parts, one per line, as hex."""
    return hmac.new(key.encode("utf-8"), "\n".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def body_hash(body: bytes) -> str:
    """SHA-256 of a body, hex."""
    return hashlib.sha256(body).hexdigest()


def sign_request(key: str, method: str, path: str, body: bytes = b"") -> dict:
    """The headers that sign a request: X-GT-Time, X-GT-Nonce, X-GT-Sig."""
    t, nonce = f"{time.time():.3f}", secrets.token_hex(16)
    return {"X-GT-Time": t, "X-GT-Nonce": nonce,
            "X-GT-Sig": _digest(key, ["REQ", method.upper(), path, t, nonce, body_hash(body)])}


def check_request(key: str, method: str, path: str, headers, body: bytes, seen: "NonceCache") -> str:
    """The request's nonce when its signature, time and nonce check out, else ProtocolError."""
    t, nonce, sig = headers.get("X-GT-Time"), headers.get("X-GT-Nonce"), headers.get("X-GT-Sig")
    if not (t and nonce and sig):
        raise ProtocolError("unsigned request")
    want = _digest(key, ["REQ", method.upper(), path, t, nonce, body_hash(body)])
    if not hmac.compare_digest(want, sig):
        raise ProtocolError("wrong access key (the signature does not match)")
    try:
        skew = abs(time.time() - float(t))
    except ValueError:
        raise ProtocolError("bad request time")
    if skew > MAX_SKEW_S:
        raise ProtocolError(f"the request's time is {skew:.0f} s off the server's clock (set both clocks right)")
    if not seen.add(nonce):
        raise ProtocolError("request replayed")
    return nonce


def sign_response(key: str, nonce: str, status: int, body: bytes) -> str:
    """The X-GT-Sig of a response to the request with `nonce`."""
    return _digest(key, ["RESP", nonce, str(status), body_hash(body)])


def check_response(key: str, nonce: str, status: int, sig: Optional[str], body: bytes):
    """ProtocolError unless the response is signed by the key's holder for this request."""
    if not sig or not hmac.compare_digest(sign_response(key, nonce, status, body), sig):
        raise ProtocolError("the server's answer is not signed with this access key")


class NonceCache:
    """The nonces of the last 2 × MAX_SKEW_S seconds (older requests are refused by their time anyway): a nonce
    comes only once. Not thread-safe: the server calls it under its own lock."""

    def __init__(self):
        """Empty."""
        self._seen: dict[str, float] = {}

    def add(self, nonce: str) -> bool:
        """True if `nonce` is new (and remember it), False if it was seen before."""
        now = time.time()
        if len(self._seen) > 1000:                  # forget the ones too old to be replayed
            self._seen = {n: t for n, t in self._seen.items() if now - t < 2 * MAX_SKEW_S}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


# Left out of code_id: the window's code (the server never runs it, the results don't depend on it) and the server's
# own queue/log/GPU-check module (a server-side fix must not make every laptop reinstall; its answers are read with
# .get, so a field added there is optional for an older plugin). An incompatible change of the endpoints or of what
# a job returns belongs in client.py / worker.py / protocol.py, which are counted.
CODE_ID_SKIP = ("ui/", "remote/server.py")


def code_id() -> str:
    """A short hash of the groundtruth package's .py files (line endings ignored: the plugin is a CRLF working-tree
    copy, the PCM package git's LF): equal on both sides exactly when they compute the same results. The window's
    code (ui/) is left out: a change there (a wider drop-down list) must not make the server refuse the plugin's
    jobs until the server is reinstalled too."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if p.relative_to(root).as_posix().startswith(CODE_ID_SKIP):
            continue
        h.update(p.relative_to(root).as_posix().encode("utf-8"))
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def new_key() -> str:
    """A new random access key (32 bytes, URL-safe text)."""
    return secrets.token_urlsafe(32)


HOME_ENV = "GROUNDTRUTH_HOME"      # the server's folder (key, server.json, jobs, log): the service sets it


def default_key_file() -> Path:
    """Where `groundtruth serve` keeps its key, its settings, jobs and log sit next to it: GROUNDTRUTH_HOME when set
    (the service install, tools/server_setup.py: C:\\ProgramData\\GroundTruth), else
    %LOCALAPPDATA%\\GroundTruth (~/.groundtruth elsewhere)."""
    home = os.environ.get(HOME_ENV)
    if home:
        return Path(home) / "server.key"
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "GroundTruth" if base else Path.home() / ".groundtruth") / "server.key"


def read_key(path: Optional[Path] = None, create: bool = False) -> tuple[str, bool]:
    """(the access key, whether it was just made): from GROUNDTRUTH_SERVER_KEY, else the key file; `create` makes
    the file with a new key when it is missing."""
    env = os.environ.get(KEY_ENV)
    if env:
        return env.strip(), False
    path = path or default_key_file()
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if len(key) < 16:
            raise ProtocolError(f"{path}: the access key is too short (16 characters at least)")
        return key, False
    if not create:
        raise ProtocolError(f"no access key: {path} is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = new_key()
    path.write_text(key + "\n", encoding="utf-8")
    return key, True
