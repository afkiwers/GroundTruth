# GroundTruth: GPU support for the plugin

The plugin's `tools` folder (after a Plugin and Content Manager install:
`<Documents>\KiCad\10.0\3rdparty\plugins\io_github_afkiwers_groundtruth\tools`) adds GPU support to the GroundTruth
plugin installed on this PC, for Simulate running on this PC. (A compute server gets its GPU support from its own
installer: `install_server.cmd install --cuda cu126`.) It installs PyTorch with GPU
support into the plugin's own Python environment: CUDA for an NVIDIA card, ROCm for an AMD Radeon (see "AMD
Radeon" below). Simulate and Suggest fixes then run the field solver's iterations on the graphics card. The
results are the same as on the CPU, to the solver's tolerance.

Contents:

| File | What it does |
|---|---|
| `install_gpu.py` | Installs PyTorch with CUDA into the plugin's environment and checks that it works on the GPU. |
| `install_gpu_amd.cmd` | For an AMD Radeon: double-click it (KiCad closed). It runs `install_gpu.py --amd` and then `check_gpu.py` with KiCad 10's Python, and waits for a key so you can read the result. `install_gpu_amd.cmd gfx1153` in a console gives the chip. |
| `check_gpu.py` | Afterwards: says whether GroundTruth itself will use the GPU, and times the solver's kind of work (a double-precision FFT) on the CPU and on the GPU. Keep it next to `install_gpu.py`: it uses that file. |
| `GPU_README.md` | This file. |

Both run the plugin environment's Python as a child process and print its output as it comes (through a pipe:
on some machines that `python.exe` shows nothing in the console it was started from).

## Before you start

- **KiCad 10** is installed (`C:\Program Files\KiCad\10.0`).
- **The GroundTruth plugin is installed**: KiCad > Plugin and Content Manager > *Install from File…* with the
  `groundtruth-…-pcm.zip`. Then **start the plugin once in KiCad** (PCB editor, the GroundTruth button) so KiCad
  builds the plugin's Python environment, and **close KiCad**.
- **A current graphics driver.** NVIDIA: in a console, `nvidia-smi` shows the card and, top right, "CUDA Version:
  12.x"; if `nvidia-smi` is not found, install the driver from nvidia.com first. AMD: a recent *AMD Software:
  Adrenalin Edition* driver from amd.com.
- **Internet access and about 5 GB of free disk space** (PyTorch with CUDA is about 2.5 GB to download).

## Install

1. Unzip this folder anywhere, e.g. `C:\Temp\groundtruth-gpu`.
2. Open a console (cmd) in that folder and run the installer with KiCad's Python:

   ```bat
   "C:\Program Files\KiCad\10.0\bin\python.exe" install_gpu.py
   ```

   It finds the plugin's environment
   (`%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.afkiwers.groundtruth`) and first prints it, with the
   CUDA version the driver supports (from `nvidia-smi`) and the build it installs. Then it installs PyTorch from
   PyTorch's CUDA 12.6 index there (`pip install torch --index-url https://download.pytorch.org/whl/cu126`) and
   checks the GPU. The last line should read
   `GPU NVIDIA GeForce RTX 3050 ... computes in double precision: GroundTruth will use it`.

3. Check what GroundTruth sees:

   ```bat
   "C:\Program Files\KiCad\10.0\bin\python.exe" check_gpu.py
   ```

   It prints the environment and the plugin folder it found, then `GroundTruth will use GPU <name>` and the time of
   a 1024 × 1024 complex FFT on the CPU and on the GPU (or `GroundTruth will use the CPU: <reason>`).

4. Start KiCad again. After a Simulate, the status line ends with `solved on GPU <name>` when the GPU did the
   work. In the plugin, **Preferences… > GPU** chooses per computer: *From the settings file* (the board's
   `analysis.gpu`, `auto` by default), `auto`, `on` or `off`.

## The RTX 3050

- The default build (`cu126`) is right for it: it needs a driver showing CUDA 12.6 or later in `nvidia-smi`.
  With an older driver, update it, or use `install_gpu.py --cuda cu118`.
- GeForce cards compute in double precision at a small fraction (1/64) of their single-precision speed, and the
  solver works in double precision. The speed-up comes mostly from the card's memory bandwidth, so expect a
  modest gain: `check_gpu.py` shows it for the FFT, and a whole Simulate gains less, because the sparse
  factorisations stay on the CPU.
- A laptop RTX 3050 has 4 or 6 GB of memory. A solve that does not fit is done on the CPU instead, and the
  status line then says how many solves fell back.

## AMD Radeon (e.g. the Radeon 860M)

- The quick way: close KiCad, then double-click `install_gpu_amd.cmd`. It does the two steps below (install, then
  check) and keeps its window open at the end.
- By hand: run `install_gpu.py --amd`. It reads the card's name from Windows and installs AMD's PyTorch build on ROCm for
  that card's chip from AMD's index (`https://repo.amd.com/rocm/whl-multi-arch/`; everything comes from that one
  index). For a Radeon 860M the chip is **gfx1152**, so it runs
  `pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ "torch[device-gfx1152]==2.12.0+rocm7.14.1"`.
  Without an NVIDIA driver on the PC, plain `install_gpu.py` does the same.
- If the card's name doesn't tell its chip (a Radeon 840M is gfx1152 or gfx1153, depending on the processor),
  give it: `install_gpu.py --amd gfx1152`. AMD's "Install PyTorch for ROCm" page lists the chip of each card.
- The check's last line reads `GPU AMD Radeon(TM) 860M Graphics computes in double precision: GroundTruth will
  use it`; `check_gpu.py` also prints `PyTorch ... on AMD ROCm ...`.
- **Is it faster?** The Radeon 860M is an integrated GPU: it shares the computer's memory (and its bandwidth) with
  the CPU, and like all Radeon gaming GPUs computes in double precision at a small fraction of its
  single-precision speed. The solver works in double precision, so it may not beat a modern CPU. `check_gpu.py`
  ends with a verdict: if it says the GPU is not clearly faster, set **Preferences… > GPU: off** in the plugin, or
  time one Simulate with GPU `on` and one with `off`. Its memory is part of the computer's RAM; a solve that
  doesn't fit is done on the CPU, and the status line says how many fell back.

## Options

| Command | When |
|---|---|
| `install_gpu.py --cuda cu118` | An older driver (`nvidia-smi` shows CUDA 11.8 to 12.5). |
| `install_gpu.py --cuda cu128` | Not needed for an RTX 3050 (for RTX 50-series cards). |
| `install_gpu.py --amd` | An AMD Radeon (Windows or Linux), the chip from the card's name; see "AMD Radeon". |
| `install_gpu.py --amd gfx1152` | The same with the chip given. `--amd-torch VERSION` picks another PyTorch build (default `2.12.0+rocm7.14.1`, the newest with Windows wheels for KiCad's Python 3.11). |
| `install_gpu.py --cuda rocm6.3` | An AMD card on Linux, from PyTorch's own index (an older ROCm than `--amd`). |
| `install_gpu.py --dry-run` | Shows what it would run, changes nothing. |
| `install_gpu.py --python PATH` / `check_gpu.py --python PATH` | Another environment, e.g. a venv the `groundtruth` command line runs from. |
| `check_gpu.py --plugin DIR` | The plugin is not where KiCad puts it under `Documents\KiCad\10.0` (`3rdparty\plugins\io_github_afkiwers_groundtruth` from the Plugin and Content Manager, or `plugins\io.github.afkiwers.groundtruth`): DIR is the folder that holds its `groundtruth` package. |

Pick a build not newer than the "CUDA Version" `nvidia-smi` shows; `install_gpu.py` prints that version but does not
choose the build for you.

The setting `analysis.gpu` in a board's settings file (`groundtruth/<board>.groundtruth.yaml`) is `auto` by default (GPU when PyTorch finds one,
otherwise the CPU); `on` refuses to run without the GPU, `off` never uses it.

## Problems

- **"no Python at …"**: the plugin has not been started in KiCad yet; start it once, close KiCad, run again.
- **"the GroundTruth plugin was not found under Documents/KiCad/10.0"** (`check_gpu.py`): install the plugin, or
  pass `--plugin DIR`.
- **"PyTorch finds no GPU"**: update the graphics driver; NVIDIA: try `--cuda cu118`; AMD: check the chip
  (`--amd gfx1152` / `gfx1153`).
- **"the card's chip is not known here"** (`--amd`): give the chip, e.g. `--amd gfx1152`.
- **"pip failed (exit code …)"** or **pip fails to download**: read pip's output above it; check the internet
  connection or the company proxy (pip honours `HTTPS_PROXY`), or try another `--cuda` build.
- **"GroundTruth will use the CPU"** after a successful install: KiCad was open during the install, or the
  plugin was reinstalled since (a reinstall can rebuild its environment): run `install_gpu.py` again.

## Remove it

Delete the folder `%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.afkiwers.groundtruth` with KiCad closed.
KiCad rebuilds it without PyTorch on the next start, and GroundTruth runs on the CPU as before.
