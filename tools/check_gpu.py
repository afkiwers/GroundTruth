"""After tools/install_gpu.py, on the machine with the GPU: will the installed GroundTruth plugin use it, and how
fast is the card at what the solver does (double-precision complex FFTs)?

    python tools/check_gpu.py                    # the KiCad 10 plugin venv and the installed plugin
    python tools/check_gpu.py --python PATH --plugin DIR

Runs with any Python (KiCad's own works). It starts the plugin venv's Python with the installed plugin's
`groundtruth` package on its path and asks GroundTruth's own device probe (analysis.gpu: auto), then times a 2-D
FFT of a 1024 × 1024 complex128 array on the CPU (numpy) and on the GPU (PyTorch). The FFT speed-up is only a
guide: a whole Simulate gains less (the sparse factorisations stay on the CPU).
"""
from __future__ import annotations

import argparse
import ctypes
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_gpu import PLUGIN_ID, plugin_python, run_shown  # noqa: E402

PROBE = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import numpy as np
from groundtruth.solver import gpu
dev = gpu.probe("auto")
if dev is None:
    _, why = gpu._find("")
    raise SystemExit(f"GroundTruth will use the CPU: {why}")
print(f"GroundTruth will use GPU {dev.name} (analysis.gpu: auto)")
a = (np.random.default_rng(1).standard_normal((1024, 1024)) + 0j)
t = time.perf_counter()
for _ in range(5):
    np.fft.fft2(a)
cpu = (time.perf_counter() - t) / 5
torch = dev.torch
sync = torch.cuda.synchronize if dev.device.type == "cuda" else (lambda: None)
g = torch.from_numpy(a).to(dev.device)
torch.fft.fft2(g)
sync()
t = time.perf_counter()
for _ in range(20):
    torch.fft.fft2(g)
sync()
gpu_t = (time.perf_counter() - t) / 20
print(f"complex128 FFT 1024 x 1024: CPU {cpu * 1e3:.1f} ms, GPU {gpu_t * 1e3:.2f} ms ({cpu / gpu_t:.1f}x)")
if getattr(torch.version, "hip", None):
    print(f"PyTorch {torch.__version__} on AMD ROCm {torch.version.hip}")
if cpu / gpu_t < 1.5:
    # an integrated GPU (a Radeon 860M, say) shares the CPU's memory and computes in double precision at a small
    # fraction of its single-precision speed: it may not beat the CPU at the solver's work
    print("verdict: the GPU is not clearly faster than the CPU at this; a Simulate will likely be as fast or slower "
          "on it. In the plugin, Preferences... > GPU: off keeps the solver on the CPU (or compare one Simulate "
          "with GPU on and off)")
else:
    print("verdict: the GPU is faster at this; a whole Simulate gains less (the factorisations stay on the CPU)")
"""


def documents() -> Path:
    """The user's Documents folder (on Windows also when it is redirected, e.g. to OneDrive)."""
    if platform.system() == "Windows":
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:      # CSIDL_PERSONAL
            return Path(buf.value)
    return Path.home() / "Documents"


def plugin_dir() -> Path | None:
    """The installed plugin: a Plugin and Content Manager install, or a copy in KiCad's plugins folder."""
    base = documents() / "KiCad" / "10.0"
    for d in (base / "3rdparty" / "plugins" / PLUGIN_ID.replace(".", "_"), base / "plugins" / PLUGIN_ID):
        if (d / "groundtruth").is_dir():
            return d
    return None


def main() -> int:
    """The command line: find the plugin's Python and the installed plugin (or take them from --python and
    --plugin), then run the probe in that environment (shown live) and return its exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", type=Path, help="the environment's Python (default: the KiCad plugin venv)")
    ap.add_argument("--plugin", type=Path, help="the folder holding the plugin's groundtruth package")
    args = ap.parse_args()
    py = args.python or plugin_python()
    plugin = args.plugin or plugin_dir()
    if not py.exists():
        print(f"no Python at {py}: start the GroundTruth plugin in KiCad once, or pass --python", file=sys.stderr)
        return 1
    if plugin is None or not (plugin / "groundtruth").is_dir():
        print("the GroundTruth plugin was not found under Documents/KiCad/10.0: install it first, or pass --plugin",
              file=sys.stderr)
        return 1
    print(f"environment: {py}\nplugin: {plugin}", flush=True)
    rc = run_shown([str(py), "-c", PROBE, str(plugin)])
    if rc != 0:
        print(f"the plugin's Python stopped with exit code {rc:#x}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
