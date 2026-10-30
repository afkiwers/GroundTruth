"""Install PyTorch with GPU support where GroundTruth runs, so `analysis.gpu: auto` (the default) uses the GPU.

    python tools/install_gpu.py                      # KiCad 10 plugin venv: NVIDIA (CUDA 12.6) or AMD, as found
    python tools/install_gpu.py --cuda cu128         # another CUDA build (newer GPUs / drivers)
    python tools/install_gpu.py --amd                # AMD Radeon (Windows or Linux): the chip from the card's name
    python tools/install_gpu.py --amd gfx1152        # AMD with the chip given (Radeon 860M: gfx1152)
    python tools/install_gpu.py --python PATH        # another environment (the CLI's, a server's venv)
    python tools/install_gpu.py --dry-run            # show what would run

Run it on the machine with the GPU (not needed elsewhere: without PyTorch everything runs on the CPU as before).
NVIDIA: `pip install torch --index-url https://download.pytorch.org/whl/<build>` (~2.5 GB); which CUDA build: one
not newer than the "CUDA Version" `nvidia-smi` shows. AMD: PyTorch built on ROCm for the card's chip, from AMD's
wheel index (`torch[device-<gfx>]==<version>`, everything from that one index); PyTorch on ROCm answers through the
same `torch.cuda` calls, so GroundTruth uses it like an NVIDIA card. With neither option it takes NVIDIA when
`nvidia-smi` is there, else an AMD card Windows names, else CUDA 12.6 as before. Afterwards it checks that PyTorch
sees the GPU and computes in double precision there.

The KiCad plugin venv is the one KiCad made for the plugin (%LOCALAPPDATA%/KiCad/10.0/python-environments/
io.github.afkiwers.groundtruth on Windows, KiCad 10's Python 3.11); restart KiCad afterwards.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "io.github.afkiwers.groundtruth"
INDEX = "https://download.pytorch.org/whl/{build}"
AMD_INDEX = "https://repo.amd.com/rocm/whl-multi-arch/"
# The newest AMD build with Windows wheels for Python 3.11 (KiCad 10's) for every chip below, checked on AMD's index
# on 2026-10-12 (2.13.0 has Linux wheels only so far). --amd-torch picks another.
AMD_TORCH = "2.12.0+rocm7.14.1"
# Radeon names as Windows shows them -> the chip (LLVM target) AMD builds PyTorch for. Only names with one chip:
# a Radeon 840M is gfx1152 or gfx1153 depending on the processor, so it needs --amd <gfx> (AMD's docs list which).
AMD_CHIPS = (
    (r"\b860M\b", "gfx1152"),                           # Ryzen AI 5/7 300 (Krackan Point)
    (r"\b8[89]0M\b", "gfx1150"),                        # Ryzen AI 9 HX 370 / 365 (Strix Point)
    (r"\b80[456]0S\b", "gfx1151"),                      # Ryzen AI Max (Strix Halo)
    (r"\b7[468]0M\b", "gfx1103"),                       # Ryzen 7040 / 8040 (Phoenix, Hawk Point)
    (r"\bRX 7900\b", "gfx1100"),
    (r"\bRX 7[78]00\b", "gfx1101"),
    (r"\bRX 7600\b", "gfx1102"),
    (r"\bRX 9070\b", "gfx1201"),
    (r"\bRX 9060\b", "gfx1200"),
)
GFX = re.compile(r"^gfx[0-9a-f]{3,4}$")
CHECK = (
    "import torch\n"
    "print('PyTorch', torch.__version__, '(ROCm ' + torch.version.hip + ')' if torch.version.hip else '')\n"
    "if not torch.cuda.is_available():\n"
    "    raise SystemExit('PyTorch finds no GPU: check the driver, or try another build (--cuda / --amd)')\n"
    "d = torch.device('cuda')\n"
    "a = torch.ones((64, 64), dtype=torch.complex128, device=d)\n"
    "float((torch.fft.fft2(a) @ a).abs().sum())\n"
    "print('GPU', torch.cuda.get_device_name(d), 'computes in double precision: GroundTruth will use it')\n"
)


def plugin_python() -> Path:
    """The KiCad 10 plugin venv's Python."""
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "KiCad" / "10.0"
        exe = Path("Scripts") / "python.exe"
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Caches" / "kicad" / "10.0"
        exe = Path("bin") / "python"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kicad" / "10.0"
        exe = Path("bin") / "python"
    return base / "python-environments" / PLUGIN_ID / exe


def driver_cuda() -> str:
    """The CUDA version the NVIDIA driver supports (from nvidia-smi), or "" without one."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return ""
    try:
        out = subprocess.run([smi], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for part in out.split("|"):
        if "CUDA Version" in part:
            return part.split("CUDA Version:")[-1].strip()
    return ""


def video_cards() -> list[str]:
    """The graphics adapters Windows lists ("AMD Radeon(TM) 860M Graphics", ...); [] elsewhere or when it can't
    say (the names only pick a default, so nothing here may fail the install)."""
    if platform.system() != "Windows":
        return []
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def amd_chip(names: list[str]) -> tuple[str, str]:
    """(gfx target, the card's name) of the first AMD Radeon in `names` whose chip is known (AMD_CHIPS), or
    ("", the Radeon's name when there is one whose chip is not known, else "")."""
    radeons = [n for n in names if "radeon" in n.lower()]
    for name in radeons:
        for pattern, gfx in AMD_CHIPS:
            if re.search(pattern, name, re.IGNORECASE):
                return gfx, name
    return "", radeons[0] if radeons else ""


def install_command(py: Path, cuda: str = "", amd: str = "", amd_torch: str = AMD_TORCH) -> list[str]:
    """The pip command: AMD's ROCm build for chip `amd` (every package from AMD's index: no mixing with PyPI, so no
    other index can supply a package of the same name), else PyTorch's CUDA build `cuda`."""
    if amd:
        return [str(py), "-m", "pip", "install", "--index-url", AMD_INDEX, f"torch[device-{amd}]=={amd_torch}"]
    return [str(py), "-m", "pip", "install", "torch", "--index-url", INDEX.format(build=cuda)]


def run_shown(cmd: list[str]) -> int:
    """Run cmd, print its output here as it comes, return its exit code. Through a pipe: on some machines the
    plugin venv's python.exe writes nothing to the console it was started from (it ran, but showed nothing)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                         text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        print(line, end="", flush=True)
    return p.wait()


def main() -> int:
    """The command line: choose the build (NVIDIA's CUDA or AMD's ROCm for the card's chip, see the module), then
    pip-install it into the plugin's Python (or --python's), relaying pip's output, and check the GPU."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", type=Path, help="the environment's Python (default: the KiCad plugin venv)")
    ap.add_argument("--cuda", help="NVIDIA: PyTorch's CUDA build cu118, cu126, cu128, ... (default cu126)")
    ap.add_argument("--amd", nargs="?", const="auto", metavar="GFX",
                    help="AMD Radeon: PyTorch on ROCm for this chip (gfx1152 for a Radeon 860M); without GFX the "
                         "chip comes from the card's name")
    ap.add_argument("--amd-torch", default=AMD_TORCH, help=f"AMD: the PyTorch version (default {AMD_TORCH})")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cuda and args.amd:
        ap.error("choose --cuda (NVIDIA) or --amd (AMD), not both")
    py = args.python or plugin_python()
    if not py.exists():
        print(f"no Python at {py}: start the GroundTruth plugin in KiCad once (KiCad then makes its venv), or pass "
              "--python", file=sys.stderr)
        return 1
    cuda = driver_cuda()
    print(f"environment: {py}\nNVIDIA driver supports CUDA {cuda or '(no nvidia-smi found)'}")
    amd = ""
    if args.amd or (not args.cuda and not cuda):
        gfx, card = amd_chip(video_cards())
        if card:
            print(f"AMD card: {card}" + (f" (chip {gfx})" if gfx else " (chip not known here)"))
        if args.amd and args.amd != "auto":
            amd = args.amd.lower()
        elif args.amd and not gfx:
            print("the card's chip is not known here: give it, e.g. --amd gfx1152 (AMD's PyTorch for ROCm "
                  "documentation lists the chip of each Radeon)", file=sys.stderr)
            return 1
        else:
            amd = gfx
        if amd and not GFX.match(amd):
            print(f"--amd {amd}: not a chip name like gfx1152", file=sys.stderr)
            return 1
    if amd:
        print(f"build: AMD ROCm for {amd}, PyTorch {args.amd_torch}")
    else:
        print(f"build: {args.cuda or 'cu126'}")
    install = install_command(py, args.cuda or "cu126", amd, args.amd_torch)
    check = [str(py), "-c", CHECK]
    print("would run:" if args.dry_run else "running:", " ".join(install))
    if args.dry_run:
        return 0
    rc = run_shown(install)
    if rc != 0:
        print(f"pip failed (exit code {rc:#x}): see above (another build: --cuda / --amd GFX / --amd-torch?)",
              file=sys.stderr)
        return 1
    rc = run_shown(check)
    if rc != 0:
        print(f"the check failed (exit code {rc:#x}): see above", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
