# Installing GroundTruth

The KiCad plugin, the command line, the development setup, and where things are. Back to the [README](../../README.md).

## The KiCad plugin

First, in KiCad, enable the API: **Preferences → Plugins → Enable KiCad API**, then restart KiCad. It is needed however the plugin is installed.

**From the GroundTruth repository** (recommended: updates arrive by themselves): **Tools → Plugin and Content Manager → Manage…**, **Add** `https://afkiwers.github.io/GroundTruth/repository.json`, pick that repository in the manager's list, install **GroundTruth**, **Apply Pending Changes**. Every new release then shows as an update.

**From a zip:** each [release](https://github.com/afkiwers/GroundTruth/releases) has the package attached (`groundtruth-<version>-<commit>-pcm.zip`); `build_package.cmd` in the repository folder builds one from the current files (`--committed`: from the last commit; `python tools/make_pcm_package.py` on any system). In KiCad: **Tools → Plugin and Content Manager → Install from File…**, pick the zip, **Apply Pending Changes**. A newer zip installs as an update.

Then close every KiCad window and start KiCad again. On the first start KiCad builds the plugin's Python environment in the background (several hundred MB from PyPI, a few minutes). In the PCB editor the button **Analyse return paths** appears at the right end of the top toolbar; if it's missing after the first start, the environment was not ready yet: **Tools → External Plugins → Refresh Plugins**, or restart KiCad. A second button, **Add selected net to GroundTruth**, adds the net of the selected track to the Signal paths tab (see "Finding the nets to analyse" in [the settings file](settings.md)). **Clear GT drawings** is in the plugin menu and the window's top bar.

**A copy for development:** `python tools/install_plugin.py` copies `plugin/` and the `groundtruth` package from the repository into `<Documents>/KiCad/<10.x+>/plugins/io.github.iwers.groundtruth` for every KiCad 10+ it finds (`--dest` sets the folder). Keep only one install: with both a package and a copy, every button shows twice (`python tools/install_plugin.py --uninstall` removes the copy).

## The command line

`groundtruth` needs Python 3.11 or newer, and neither KiCad nor a display. GroundTruth is not on PyPI: install a release's wheel, which carries the release's number (`groundtruth --version`):

```sh
pip install "groundtruth-pcb @ https://github.com/afkiwers/GroundTruth/releases/download/0.1.42593/groundtruth_pcb-0.1.42593-py3-none-any.whl"
```

`simulate --thermal`, `--capacity` and a thermal criterion of `verify` need the `thermal` extra (numba, pyamg, psutil): `pip install "groundtruth-pcb[thermal] @ <the wheel's URL>"`. The newest code between releases: `pip install "groundtruth-pcb @ git+https://github.com/afkiwers/GroundTruth.git@main"`. Don't `pip install groundtruth`: that name on PyPI belongs to an unrelated project. For pipelines see [CI](ci.md).

## Development

The development environment (Python 3.11 or newer) uses the python.org interpreter; the MSYS/MinGW Python cannot use PyPI wheels.

```sh
git clone https://github.com/afkiwers/GroundTruth.git
cd GroundTruth
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[plugin,dev,thermal]"
.venv\Scripts\python -m pytest
```

The source tree: [Development](development.md); how releases are made: [Releases](releases.md).

## Where to find it

Checked on KiCad 10.0.5, Windows:

| What | Where |
|---|---|
| The button | PCB editor (open a board), top toolbar, right end, next to other plugin buttons such as Interactive HTML BOM. Its tooltip is "Analyse return paths". A small "GroundTruth – starting" window answers the click at once; then the window **&lt;board&gt; – GroundTruth** opens. It is not in the project manager or the schematic editor. |
| Start-up log | `%LOCALAPPDATA%\GroundTruth\plugin.log` (`~/.cache/groundtruth/plugin.log` elsewhere): the time of each start-up step, for a slow start, and wxWidgets' own messages (which KiCad would otherwise pop up in a Messages dialog). |
| Demo board | `demo\groundtruth_demo.kicad_pro` in the installed plugin folder (next row): a 4-layer board whose clock changes layers with no stitching via. Open it with **File → Open Project**, then follow `demo\README.txt`: Run checks, Simulate (the return fans out over both GND planes), Suggest fixes (the stitching via). Reinstalling the plugin resets it. |
| The installed plugin | `<Documents>\KiCad\10.0\plugins\io.github.iwers.groundtruth` (with OneDrive: `C:\Users\<you>\OneDrive\Dokumente\KiCad\10.0\plugins\...`). **Tools → External Plugins → Open Plugin Directory** opens the `plugins` folder. |
| Its Python environment | `%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.iwers.groundtruth`, which KiCad builds on first start. Delete this folder to make KiCad rebuild it. |
| Name in KiCad | "GroundTruth – return path / EMI analysis", identifier `io.github.iwers.groundtruth` |

To open the same window on a file without KiCad (drawing on the board is disabled in this mode), run `groundtruth-gui board.kicad_pcb`.

## The plugin window

- **Tabs:** **Signal paths** (signal paths to analyse: driver → series parts / op-amps → receivers, across nets), **Converters** (DC-DC power stages), **Bridges** (motor bridges), **DC loads** (rails with the currents their loads draw), **Power integrity** (PDN rails and their target impedance), **Signal integrity** (impedance targets, differential pairs, crosstalk), **Parts** (part models), **Issues** (the findings, shown in KiCad on a click), **Results** (current maps, cross-section, spectra, Bode plots, temperatures), **Fixes** (suggested fixes and what-if edits), **History** (every Simulate kept, compared) and **Server jobs** (analyses handed to a compute server).
- **Top bar:** **Reload board**, **Refill zones** (live KiCad only), **Run checks** (Tier 1), **Simulate** (Tier 1 + Tier 2, power stages and bridges), **Thermal**, **Export report…**, the **with what-if edits** tickbox, **Cancel**, **Configuration ▾** (save and load configurations, Back to last settings), **Undo** / **Redo**, **Clear GT drawings** (live KiCad only), and at the right **Preferences…** and **Help** (F1).

Each is described in these guides and in the plugin's own help (F1).

## GPU acceleration (optional)

Simulate and Suggest fixes can run the field solver's iterations on a graphics card: the FFT convolutions, the
layer-kernel products and the iteration vectors. The sparse factorisations stay on the CPU. Results are the same
as on the CPU, to the solver's tolerance (about 1e-5). Without a GPU, or without the GPU library, everything runs on
the CPU as before, so this is only worth doing on the machine that has the card.

**What you need**

| | |
|---|---|
| A GPU | NVIDIA (Windows or Linux), or AMD on Linux (ROCm). Apple GPUs and Intel integrated graphics are not used. |
| Its driver | A current NVIDIA driver. `nvidia-smi` (in a console) shows "CUDA Version: 12.x" at the top right: the highest CUDA build the driver can run. |
| PyTorch with GPU support | Installed into the Python environment GroundTruth runs in: the KiCad plugin's own environment, or the venv you run the CLI from. Around 2.5 GB to download. GroundTruth does not install it by itself. |
| Disk and memory | Around 5 GB free for PyTorch. A large board needs a few GB of GPU memory; a solve that does not fit is done on the CPU instead. |

The solver computes in double precision. Data-centre cards (A100, H100) do that at full speed; GeForce and most
workstation cards at a small fraction of their single-precision speed. The speed-up therefore depends on the card:
measure it with `tools/dev/gpu_check.py` (below). With `analysis.gpu: auto` GroundTruth measures it too: once per
session it times the solver's FFT convolution on the GPU against the CPU and uses the GPU only from 1.5x; otherwise
the run says "solved on CPU (the … GPU computes … at 0.3x the CPU's speed here …)". An RTX 3050 Laptop GPU beside a
16-thread CPU measured 0.3-0.8x, and a full refboard Simulate took over 2 h on it against 18 min on the CPU.
`gpu: on` uses the GPU regardless. One process at a time computes on a GPU: under auto a second one runs on the CPU
("in use by another process"), under on it waits; two processes sharing one card had stalled each other for hours.

**How to install it**

1. Install the plugin as above and start it once in KiCad: KiCad then builds the plugin's Python environment
   (`%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.iwers.groundtruth`).
2. Close KiCad.
3. In a console on the GPU machine, run the installer with any Python. KiCad's own works; it ships with KiCad:

   ```bat
   "C:\Program Files\KiCad\10.0\bin\python.exe" tools\install_gpu.py
   ```

   Run it in the repo folder (for a copied install), or in the plugin folder for a package install
   (`<Documents>\KiCad\10.0\3rdparty\plugins\io_github_iwers_groundtruth`), which carries the script as
   `tools\install_gpu.py`. It finds the plugin's environment, runs `pip install torch` there (an NVIDIA card:
   from PyTorch's CUDA 12.6 index; an AMD Radeon, when no NVIDIA driver is found: AMD's ROCm build for the card's
   chip, from AMD's index), and checks that PyTorch sees the GPU and computes in double precision on it. The last line reads
   "GPU <name> computes in double precision: GroundTruth will use it".
4. Optionally, `tools\check_gpu.py` (run the same way) says whether GroundTruth itself will use the GPU and times a
   double-precision FFT on the CPU and on the GPU.
5. Start KiCad again.

On another PC, these scripts are in the installed plugin's `tools` folder, with `install_gpu_amd.cmd` (an AMD
Radeon, double-click) and `GPU_README.md` (these steps). A compute server gets PyTorch from its own installer
(`install_server.cmd install --cuda cu126`).

Options of `install_gpu.py`:

| Option | When |
|---|---|
| `--cuda cu128` | Newer cards (GeForce RTX 50 series) and drivers showing CUDA 12.8 or later. Pick a build not newer than the CUDA version `nvidia-smi` shows: `cu118`, `cu126` (default), `cu128`. |
| `--cuda cu118` | An older driver (`nvidia-smi` shows CUDA 11.8 to 12.5), when updating it is not an option. |
| `--amd` | An AMD Radeon (Windows or Linux): PyTorch on ROCm from AMD's index (`repo.amd.com/rocm/whl-multi-arch`), built for the card's chip, which it takes from the card's name (Radeon 860M: gfx1152; also 880M/890M, 8040S-8060S, 740M-780M, RX 7600-7900, RX 9060/9070). A recent AMD Software: Adrenalin driver. |
| `--amd gfx1152` | The same with the chip given: a card whose name doesn't tell (a Radeon 840M is gfx1152 or gfx1153); AMD's "Install PyTorch for ROCm" page lists the chip of each card. `--amd-torch VERSION` picks another PyTorch build (default 2.12.0+rocm7.14.1, the newest with Windows wheels for KiCad's Python 3.11). |
| `--cuda rocm6.3` | AMD cards on Linux, from PyTorch's own index (older ROCm; `--amd` is newer). |
| `--python PATH` | Another environment: the venv you run `groundtruth` from on a server or CI machine, e.g. `--python .venv\Scripts\python.exe`. |
| `--dry-run` | Show what it would run, change nothing. |

If the check says "PyTorch finds no GPU", update the driver or try another build (`--cuda`, `--amd GFX`). An
integrated GPU (a Radeon 860M, say) shares the computer's memory and computes in double precision far slower than
in single precision, so it may not beat the CPU: `check_gpu.py` ends with a verdict, and Preferences… > GPU: off
keeps the solver on the CPU. To remove it
again, delete the plugin's environment folder (KiCad rebuilds it without PyTorch on the next start) or run
`<that python> -m pip uninstall torch`.

**Using it.** The setting `analysis.gpu` in the settings file is `auto` by default: the GPU is used when PyTorch
finds one, the CPU otherwise. `on` refuses to run without a GPU (to be sure it is used), `off` never uses one. On
the command line, `groundtruth simulate|suggest|compare ... --gpu on|off|auto` overrides the setting for one run;
in the plugin, **GPU** in **Preferences…** overrides it on this computer. After Simulate or
Suggest fixes, the status line ends with "solved on GPU <name>" when the GPU did the work. To compare GPU and CPU on
your board (results and speed), run `python tools/dev/gpu_check.py BOARD SETTINGS --only NET` from the repo on the
GPU machine.
