"""Optional GPU backend for the field solver's iterations (PyTorch: NVIDIA CUDA, AMD ROCm on Windows and Linux).

The user: "this laptop is used for development but the plugin will run on a laptop with a much bigger GPU or even a
big server". So `analysis.gpu` (auto | on | off) moves the GMRES iterations to a GPU when PyTorch finds one that
computes in double precision:
  * on the GPU: the L_p product (FFT convolution and layer-kernel multiply-add), the sparse incidence products,
    the preconditioner's admittance products and the Krylov basis (Gram-Schmidt);
  * on the CPU: the factorisations (SuperLU) and their triangular solves (`op.solve_S`), as before; only the
    node-sized vector of each preconditioner application crosses over (~1 MB per iteration).
Double precision only: single precision stalled GMRES near plane resonances (see peec.py).

`auto` uses the GPU when one is there and stays on the CPU otherwise (also when PyTorch is not installed, without
importing anything); `on` refuses to run without one; `off` never looks. A solve the GPU cannot finish (out of
memory, an operation the device lacks) is done again on the CPU, and the run reports how often (`Run`).

Development and tests: GROUNDTRUTH_GPU_DEVICE=cpu runs this same code with PyTorch on the CPU, so the GPU path is
tested on a machine without a GPU; only the device differs on the target (`tools/dev/gpu_check.py` compares the
two there). Nothing here is imported unless a mode other than `off` asks for it: peec.py never imports torch."""
from __future__ import annotations

import os
import threading
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

MODES = ("auto", "on", "off")
ENV_DEVICE = "GROUNDTRUTH_GPU_DEVICE"


class GpuUnavailable(RuntimeError):
    """analysis.gpu is on, but there is no usable GPU (or no PyTorch)."""


@dataclass
class Device:
    """A device PyTorch can compute on in double precision: the torch module (imported only when needed), the
    device and its name for the status line."""
    torch: object          # the torch module
    device: object         # torch.device
    name: str              # "NVIDIA GeForce RTX 4090", "cpu (PyTorch)"


@dataclass
class Run:
    """What one simulation (or fix search) ran on."""
    device: Optional[Device] = None
    fallbacks: int = 0                 # solves the GPU could not finish, done again on the CPU
    error: str = ""                    # the first such failure
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def label(self) -> str:
        """The device's name, or "CPU"."""
        return self.device.name if self.device is not None else "CPU"

    def failed(self, e: BaseException):
        """Count a solve the device could not finish (it is redone on the CPU); keep the first error's text."""
        with self._lock:
            self.fallbacks += 1
            if not self.error:
                self.error = f"{type(e).__name__}: {e}"
        try:                        # out of device memory: give back what the failed solve held
            if self.device is not None and self.device.device.type == "cuda":
                self.device.torch.cuda.empty_cache()
        except Exception:           # noqa: BLE001
            pass

    def summary(self) -> dict:
        """Where the solves ran, as plain data for the result and the reports (see describe)."""
        return {"device": self.label, "gpu_fallbacks": self.fallbacks, "gpu_error": self.error}


_probed: dict = {}
_probe_lock = threading.Lock()
_active: list = [None]             # the current Run (one job at a time: the plugin and the CLI)


def probe(mode: str) -> Optional[Device]:
    """The device `mode` selects (cached), or None for the CPU. Raises GpuUnavailable for `on` without one."""
    if mode not in MODES:
        raise ValueError(f"analysis.gpu must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "off":
        return None
    forced = os.environ.get(ENV_DEVICE, "").strip()
    with _probe_lock:
        key = (mode, forced)
        if key not in _probed:
            _probed[key] = _find(forced)
        found, why = _probed[key]
    if found is None and mode == "on":
        raise GpuUnavailable(f"analysis.gpu is on, but {why}. Install PyTorch with GPU support (tools/install_gpu.py; "
                             "--amd for an AMD Radeon) or set analysis.gpu to auto.")
    return found


def _find(forced: str) -> tuple[Optional[Device], str]:
    """(device, "") for the first usable one (the forced one from GROUNDTRUTH_GPU_DEVICE, else CUDA/ROCm), or
    (None, why not). "Usable" is tested, not assumed: a small complex128 FFT and matrix product must run there."""
    try:
        import torch
    except ImportError:
        return None, "PyTorch is not installed"
    names = [forced] if forced else (["cuda"] if torch.cuda.is_available() else [])
    if not names:
        return None, f"PyTorch {torch.__version__} finds no CUDA/ROCm GPU"
    for name in names:
        try:
            dev = torch.device(name)
            a = torch.ones((4, 4), dtype=torch.complex128, device=dev)       # double precision works there
            float((torch.fft.fft2(a) @ a).abs().sum())
            label = torch.cuda.get_device_name(dev) if dev.type == "cuda" else f"{dev.type} (PyTorch)"
            return Device(torch, dev, label), ""
        except Exception as e:      # noqa: BLE001 - a device that cannot do this is not one to use
            why = f"the {name} device cannot compute in double precision ({e})"
    return None, why


@contextmanager
def use(mode: str):
    """Solves inside run on the device `mode` selects; yields the Run. Nested uses keep the outer run."""
    if _active[0] is not None:
        yield _active[0]
        return
    run = Run(probe(mode))
    _active[0] = run
    try:
        yield run
    finally:
        _active[0] = None


def describe(summary: dict) -> str:
    """Run.summary() as a phrase: "GPU NVIDIA RTX A4000", "CPU", or with the solves that fell back."""
    device = summary.get("device") or "CPU"
    text = device if device == "CPU" else f"GPU {device}"
    n = summary.get("gpu_fallbacks") or 0
    if n:
        text += f" ({n} solve(s) done on the CPU instead: {summary.get('gpu_error', '')})"
    return text


def active() -> Optional[Run]:
    """The current run when it has a device (solves then go to it), else None (the CPU does them)."""
    run = _active[0]
    return run if run is not None and run.device is not None else None


def release(stack):
    """Drop a stack's device copies (kernels, topology); they are made again when needed."""
    for k in ("_gpu", "_gpu_kernels"):
        stack.__dict__.pop(k, None)


def derived(stack):
    """For PlaneStack.with_branches' shallow copy: its own incidence matrices (more branches); the kernels and
    strip indices (same grid) stay shared, so each fix candidate does not upload them again."""
    for k in ("_gpu", "_gpu_lock"):
        stack.__dict__.pop(k, None)


# --------------------------------------------------------------------------- the device side of a solve
class TorchVectors:
    """GMRES's vector work on the device (peec._gmres): the basis stays there, the small Hessenberg column
    comes back to the CPU."""

    def __init__(self, dev: Device):
        """Vector work on `dev`."""
        self.t, self.d = dev.torch, dev.device

    def zeros(self, n):
        """A complex zero vector of length n on the device."""
        return self.t.zeros(n, dtype=self.t.complex128, device=self.d)

    def basis(self, rows, n):
        """An uninitialised (rows, n) basis array on the device."""
        return self.t.empty((rows, n), dtype=self.t.complex128, device=self.d)

    def copy(self, v):
        """A copy of v (on the device)."""
        return v.clone()

    def norm(self, v) -> float:
        """The Euclidean norm of v, as a Python float (one value crosses to the CPU)."""
        return float(self.t.linalg.vector_norm(v))

    def any(self, v) -> bool:
        """Whether v has a non-zero entry."""
        return bool(self.t.any(v != 0))

    def project(self, V, j, w):
        """(h = V^H w on the CPU, w - V h on the device) over the first j+1 basis vectors: one Gram-Schmidt pass."""
        Vj = V[: j + 1]
        h = self.t.mv(Vj.conj(), w)
        w = self.t.addmv(w, Vj.T, h, alpha=-1)
        return h.cpu().numpy(), w

    def combine(self, y: np.ndarray, V, k):
        """sum_i y[i] V[i] over the first k basis vectors (y comes from the CPU)."""
        return self.t.mv(V[:k].T, self.t.as_tensor(y, dtype=self.t.complex128, device=self.d))


class _Orient:
    """One strip orientation's device data: its range of unknowns, array shape, FFT size, scatter and gather
    indices and the kernels as (N, N, pixels) real tensors."""
    __slots__ = ("lo", "hi", "shape", "P", "into", "outof", "K")


class TorchMath:
    """PlaneStack.solve's matrix work on the device (peec._HostMath is the numpy twin)."""

    def __init__(self, run: Run, stack):
        """The device math for solves on `stack` in `run` (the stack's device copies made on first use)."""
        self.run = run
        dev = run.device
        self.t, self.d, self.st = dev.torch, dev.device, stack
        self.vec = TorchVectors(dev)
        self.data = _stack_data(dev, stack)

    def dev(self, v: np.ndarray):
        """A numpy vector as a complex128 tensor on the device."""
        return self.t.as_tensor(np.ascontiguousarray(v), dtype=self.t.complex128, device=self.d)

    def host(self, x) -> np.ndarray:
        """A device tensor back as a numpy array."""
        return x.cpu().numpy()

    def _spmv(self, A, x):
        """A real sparse matrix times a complex vector (through its real view: both parts in one product)."""
        t = self.t
        return t.view_as_complex((A @ t.view_as_real(x)).contiguous())

    def _op(self, op):
        """The operator's frequency data on the device (made once per operator and device)."""
        key = str(self.d)
        cache = op.__dict__.setdefault("_gpu", {})
        o = cache.get(key)
        if o is None:
            t, st = self.t, self.st
            o = {"w": float(op.w), "R": self.dev(op.R_strip.astype(complex)), "z": self.dev(op.z_tail)}
            if st.blocks:            # per-position layer blocks: Dinv = Dr + j·Di, two real sparse products
                o["Dr"] = _csr(t, self.d, op.Dinv.real)
                o["Di"] = _csr(t, self.d, op.Dinv.imag)
            else:
                o["D"] = self.dev(op.Dinv.diagonal())
            cache[key] = o
        return o

    def _dinv(self, o, v):
        """The preconditioner admittance times v: a diagonal product, or (sheet blocks) the complex sparse matrix
        as two real ones, (Dr + j Di)(a + j b) = (Dr a - Di b) + j (Dr b + Di a)."""
        if "D" in o:
            return o["D"] * v
        t = self.t
        V2 = t.view_as_real(v)
        a, b = o["Dr"] @ V2, o["Di"] @ V2                # [Dr·re, Dr·im], [Di·re, Di·im]
        return t.complex(a[:, 0] - b[:, 1], a[:, 1] + b[:, 0])

    def Lp(self, I):
        """L_p I on the device (as PlaneStack.Lp_matvec): scatter, FFT, the layer-kernel multiply-add as one einsum
        over the real kernels and the spectra's real/imaginary pairs, inverse FFT, gather."""
        t, N = self.t, self.st.N
        out = t.empty(self.st.ns, dtype=t.complex128, device=self.d)
        for o in self.data["orients"]:
            X = t.zeros(o.shape[0] * o.shape[1] * o.shape[2], dtype=t.complex128, device=self.d)
            X[o.into] = I[o.lo:o.hi]
            F = t.fft.fft2(X.view(o.shape), s=o.P)
            Fr = t.view_as_real(F).reshape(N, -1, 2)
            G = t.einsum("tsp,spc->tpc", o.K, Fr).contiguous()
            Y = t.fft.ifft2(t.view_as_complex(G).view(N, *o.P))
            out[o.lo:o.hi] = Y.reshape(-1)[o.outof]
        if "dL" in self.data:               # strips narrower than the pitch (PlaneStack._widths)
            out += self.data["dL"] * I
            if "M_near" in self.data:
                out += self._spmv(self.data["M_near"], I.contiguous())
        return out

    def matvec(self, op, x):
        """The system matrix times x (as peec._HostMath.matvec)."""
        o, st, t = self._op(op), self.st, self.t
        nb, ns = st.nb, st.ns
        Ib = x[:nb]
        top = t.empty(nb, dtype=t.complex128, device=self.d)
        top[:ns] = o["R"] * Ib[:ns] + (1j * o["w"]) * self.Lp(Ib[:ns])
        top[ns:] = o["z"] * Ib[ns:]
        top -= self._spmv(self.data["Ar"], x[nb:])
        return t.cat([top, self._spmv(self.data["ArT"], Ib)])

    def precond(self, op, r):
        """The preconditioner (as peec._HostMath.precond): the sparse products on the device, the factorisation's
        solve on the CPU (only that node-sized vector crosses over)."""
        o, nb = self._op(op), self.st.nb
        r1, r2 = r[:nb], r[nb:]
        s = r2 - self._spmv(self.data["ArT"], self._dinv(o, r1))
        y = self.dev(op.solve_S(self.host(s)))          # the factorisation's solves stay on the CPU
        return self.t.cat([self._dinv(o, r1 + self._spmv(self.data["Ar"], y)), y])


def _csr(t, device, A):
    """A scipy sparse matrix (real) as a torch CSR tensor on the device."""
    A = A.tocsr()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")          # "sparse CSR support is in beta" and the invariant-check note
        return t.sparse_csr_tensor(t.as_tensor(A.indptr, dtype=t.int64), t.as_tensor(A.indices, dtype=t.int64),
                                   t.as_tensor(A.data, dtype=t.float64), size=A.shape).to(device)


def _stack_data(dev: Device, stack) -> dict:
    """A stack's device copies: incidence matrices (per stack) and kernels with strip indices (shared by the
    stacks derived from it, see derived()); made once per stack and device."""
    key = str(dev.device)
    cache = stack.__dict__.setdefault("_gpu", {})
    data = cache.get(key)
    if data is not None:
        return data
    with stack.__dict__.setdefault("_gpu_lock", threading.Lock()):
        data = cache.get(key)
        if data is not None:
            return data
        t = dev.torch
        if np.iscomplexobj(stack.Ar.data):
            raise TypeError("complex incidence matrix")
        kernels = stack.__dict__.setdefault("_gpu_kernels", {})
        if key not in kernels:
            kernels[key] = _orients(dev, stack)
        data = {"Ar": _csr(t, dev.device, stack.Ar), "ArT": _csr(t, dev.device, stack.ArT),
                "orients": kernels[key]}
        if getattr(stack, "dL", None) is not None:
            data["dL"] = t.as_tensor(stack.dL.astype(complex), dtype=t.complex128, device=dev.device)
        if getattr(stack, "M_near", None) is not None:
            data["M_near"] = _csr(t, dev.device, stack.M_near)
        cache[key] = data
        return data


def _orients(dev: Device, stack) -> list:
    """The stack's per-orientation device data (_Orient): strip indices and full kernel spectra uploaded."""
    t = dev.torch
    out = []
    for orient, lo, hi, shape, P in (("x", 0, stack.nxb, (stack.N, stack.nx - 1, stack.ny), stack.Px),
                                     ("y", stack.nxb, stack.ns, (stack.N, stack.nx, stack.ny - 1), stack.Py)):
        if hi <= lo:
            continue
        into, outof = stack._strip_index(orient)
        K = stack.Kx if orient == "x" else stack.Ky
        o = _Orient()
        o.lo, o.hi, o.shape, o.P = lo, hi, shape, tuple(P)
        o.into = t.as_tensor(into, dtype=t.int64, device=dev.device)
        o.outof = t.as_tensor(outof, dtype=t.int64, device=dev.device)
        o.K = t.as_tensor(np.stack([np.stack([K[(a, b)].reshape(-1) for b in range(stack.N)])
                                    for a in range(stack.N)]), dtype=t.float64, device=dev.device)
        out.append(o)
    return out
