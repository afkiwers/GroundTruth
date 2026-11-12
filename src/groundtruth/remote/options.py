"""What a compute server can install into its own Python environment when the plugin asks (POST /v1/options): the
GPU builds of PyTorch and the MKL sparse solver, the packages that so far needed someone at the server
(install_server --cuda, tools/install_gpu.py, tools/install_sparse.py). The user, 2026-11-12: "currently the user
must install gpu packages etc manually and have access to the server. Make available options installable
remotely, hiding it behind the already existing password protection".

Security: the request carries an option's NAME and "install" or "remove", nothing else. The name must be a key of
OPTIONS; the pip command is built here from fixed arguments and the fixed package indexes the install tools use
(tools/install_gpu.py's INDEX and AMD_INDEX; tests/test_remote_options.py holds the two in step). No package
name, version, index address or option text ever comes from the network, the command runs without a shell, and
the request is signed with the server's key like every other (remote/protocol.py). In the plugin the dialog also
asks for this computer's password (ui/jobs_password.py).
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

TORCH_INDEX = "https://download.pytorch.org/whl/{build}"        # PyTorch's own index, one folder per CUDA build
AMD_INDEX = "https://repo.amd.com/rocm/whl-multi-arch/"         # AMD's ROCm builds of PyTorch
AMD_TORCH = "2.12.0+rocm7.14.1"                                 # the AMD build with Windows wheels (install_gpu.py)
CUDA_BUILDS = ("cu118", "cu126", "cu128")
AMD_TARGETS = ("gfx1100", "gfx1101", "gfx1102", "gfx1103", "gfx1150", "gfx1151", "gfx1152", "gfx1200", "gfx1201")
MKL_EXTRA = ("intel-openmp", "tbb", "tcmlib", "umf")            # what mkl brings along, removed with it
INSTALL_TIMEOUT_S = 3600.0                                      # a 2.5 GB download on a slow line


def _table() -> dict:
    """name -> {label, about, package (the distribution whose presence means "installed"), install, remove}: the
    arguments after `python -m pip`."""
    out = {}
    for build in CUDA_BUILDS:
        out[f"gpu-{build}"] = {
            "label": f"GPU: PyTorch for NVIDIA, CUDA build {build}",
            "about": ("about 2.5 GB; for drivers showing CUDA 11.8 to 12.5" if build == "cu118" else
                      "about 2.5 GB; the usual choice" if build == "cu126" else "about 2.5 GB; the newest drivers"),
            "package": "torch", "group": "gpu",
            "install": ["install", "torch", "--index-url", TORCH_INDEX.format(build=build)],
            "remove": ["uninstall", "-y", "torch"]}
    for gfx in AMD_TARGETS:
        out[f"gpu-amd-{gfx}"] = {
            "label": f"GPU: PyTorch for AMD Radeon, chip {gfx}",
            "about": "about 3 GB; AMD's ROCm build",
            "package": "torch", "group": "gpu",
            "install": ["install", "--index-url", AMD_INDEX, f"torch[device-{gfx}]=={AMD_TORCH}"],
            "remove": ["uninstall", "-y", "torch"]}
    out["sparse-mkl"] = {
        "label": "Sparse solver: Intel MKL (PARDISO)",
        "about": "about 150 MB; x86 processors only",
        "package": "mkl", "group": "sparse",
        "install": ["install", "--upgrade", "mkl"],
        "remove": ["uninstall", "-y", "mkl", *MKL_EXTRA]}
    return out


OPTIONS = _table()
ACTIONS = ("install", "remove")


def command(name: str, action: str, python: Optional[str] = None) -> list[str]:
    """The pip command of an option's action, from the fixed table only: ValueError for a name that is not one of
    OPTIONS' keys or an action that is not install / remove (whatever was asked for is never put into a command)."""
    if name not in OPTIONS or action not in ACTIONS:
        raise ValueError("unknown option or action")
    return [python or sys.executable, "-m", "pip", "--disable-pip-version-check", *OPTIONS[name][action]]


def unsuitable(name: str) -> str:
    """Why this machine cannot take the option ("" when it can): MKL is x86 only, the GPU builds are not for a Mac."""
    if name not in OPTIONS:
        return "unknown option"
    if OPTIONS[name]["group"] == "sparse" and platform.machine().lower() not in ("amd64", "x86_64"):
        return f"Intel MKL runs on x86 only, not on this {platform.machine()} machine"
    if OPTIONS[name]["group"] == "gpu" and sys.platform == "darwin":
        return "no GPU build is used on a Mac: the solves run on the CPU there"
    return ""


def installed() -> dict:
    """{package: version or None} of the packages the options install, read from the environment's metadata (no
    import: importing torch takes a minute and gigabytes)."""
    from importlib import metadata
    out = {}
    for package in sorted({o["package"] for o in OPTIONS.values()}):
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            out[package] = None
    return out


def listing() -> list[dict]:
    """The options for the status answer: [{name, label, about, group, package, unsuitable}]."""
    return [{"name": name, "label": o["label"], "about": o["about"], "group": o["group"], "package": o["package"],
             "unsuitable": unsuitable(name)} for name, o in OPTIONS.items()]


def run(name: str, action: str, log_file: Path, python: Optional[str] = None) -> int:
    """Run an option's pip command, its output appended to log_file; the exit code (non-zero: failed; 124: it took
    longer than INSTALL_TIMEOUT_S). No shell, no input."""
    cmd = command(name, action, python)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8", errors="replace") as out:
        out.write("\n$ " + " ".join(cmd) + "\n")
        out.flush()
        try:
            return subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=INSTALL_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            out.write(f"\nstopped after {INSTALL_TIMEOUT_S / 60:.0f} minutes\n")
            return 124
        except OSError as e:
            out.write(f"\ncould not start pip: {e}\n")
            return 1
