# GroundTruth — return-path and EMI analysis for KiCad

GroundTruth finds return-path problems on high-speed nets in KiCad 10+ boards. Examples are a trace crossing a plane split, a layer change with no stitching via, or a trace running along a plane edge. It also solves where the return current actually flows in the reference plane and estimates how much each detour adds to radiated emission.

It also analyses **DC-DC power stages** (switching at 10 kHz to a few MHz): the hot loop through the switches, input capacitors and pours, the capacitor current sharing, switch-node ringing and overshoot, current density and resistance of the power paths, and the conducted emission at the input through a LISN, with fix suggestions (stitching vias, a capacitor at the switches, input-filter guidance). See [Switching power stages](#switching-power-stages-dc-dc-converters).

It analyses **motor bridges** (BLDC drives, ESCs) too: the six-step drive at an operating point, each leg's hot loop, the conducted emission, the bus capacitors' ripple, the losses, the RMS current density from battery to motor, and the noise the bridge couples into the board's microcontroller nets. See [Motor bridges](#motor-bridges-bldc-drives-escs). A **thermal solve** gives the copper and part temperatures and each stage's current capacity (see [Temperatures](#temperatures-thermal-solve)).

On the same board model it also checks:

- **DC rails with loads**: the drop each load sees, current density and via loading ([DC loads](#dc-loads));
- **power integrity**: each IC's supply impedance from 10 kHz to 1 GHz against a target ([Power integrity](#power-integrity-pdn-impedance));
- **voltage-aware safety**: clearances by IPC-2221B, capacitor and FET voltage ratings, resistor power, ESD protection ([Safety checks](#voltage-aware-safety-checks));
- **signal integrity**: impedance, differential impedance and skew, crosstalk and via stubs from 2D cross-sections ([Signal integrity](#signal-integrity));
- **control loops** of buck and boost stages: crossover, phase and gain margin from the parts on the board and the controller's schematic fields ([Control loop](#control-loop-of-a-buck-or-boost-stage));
- **frequency responses**: Bode plots, poles and zeros of signal paths, power stages and PDN rails ([Frequency responses](#frequency-responses-bode-plots-poles-and-zeros)).

The heavy analyses can run on one or more [compute servers](#compute-server-optional) on the local network.

GroundTruth comes in two forms:

- **KiCad plugin** (IPC API, KiCad 10 and later): you pick part models and nets, run the analysis, look at the heatmaps with current arrows, draw markers, current contours and current arrows onto the board, and export an HTML report.
- **`groundtruth` command line** for CI: it needs no KiCad and no GUI. It returns exit codes and writes GitLab Code Quality, JUnit, JSON and HTML reports.

Both use the same analysis core and the same settings file. On the same board they produce identical results.

## How it works

| Tier | What it does | Speed |
|---|---|---|
| **1 – rule checks** | Finds the copper route from driver to receiver(s), including series 0R and termination parts. For every segment it projects onto the adjacent plane layer(s) in the stackup and flags: gap/split crossings, traces running off the plane, layer changes without a return via (same net) or decap (different net), traces close to a plane edge, and traces referenced to a non-ground plane. | seconds |
| **2 – field solver** | Rasterises the plane copper of **every copper layer** (any layer count) and solves the return current with a quasi-static multi-layer PEEC model: resistive and inductive plane strips, mutual partial inductances between all layer pairs applied as FFT convolutions, exact trace vector potential, and GMRES. Stitching vias and through-hole pads are vertical branches, and they couple to the signal via. Decaps, 0R parts and ferrites between plane nets are lumped branches. **Plane-pair capacitance** comes from the board's physical stackup (dielectric thickness, εr, loss tangent), so the return can also cross between planes as displacement current, and plane resonances show up. The return can therefore change layer and net wherever the copper allows. Results are loop inductance, magnetic moment, the share of return current per layer, the vias, parts and plane pairs that carry it between layers, and plane resonances excited by the net. These are compared with an ideal reference: the route's own reference planes, solid and stitched every 2 mm. From the loop moment and the current on the line at each harmonic it estimates the differential-mode field and compares it with the CISPR 32 class A/B limits. | 1–4 min per board |
| **power stages** | The same solver on each converter's copper: the switching cell as known currents through the switch packages, capacitors and the LISN as branches, pours as solved copper. It gives the hot loop, DC paths, conducted and radiated estimates, and fix suggestions. | 30–70 s per stage |
| **motor bridges** | Each leg solved as a power stage on one model of the bridge, combined with a six-step drive model of the motor currents: conducted emission, capacitor ripple, losses, RMS current maps, victims. | per leg, as a power stage |
| **thermal** | PCB Thermal's solver on the board, heated by the power stages' and bridges' currents and their parts' losses. | after Simulate |
| **DC loads** | A supply rail with the currents its loads draw, solved at DC on the whole board's copper: the drop each load sees (supply and return), current density, loss and the busiest vias. Thermal and Current capacity include it. | part of Simulate |
| **power integrity** | The same field solver on one model of the whole board, from 10 kHz to 1 GHz: each IC's supply impedance between its supply and ground pins, with the decaps, the planes' capacitance, the vias and the regulator together. Each IC's pins also get a fine model of their own surroundings. It gives \|Z(f)\| per IC against the target impedance `V · ripple / I_step`, which part holds the rail at each frequency, the anti-resonance peaks and supply pins with no decap nearby. **Power integrity** tab. | ~10–30 min per rail |
| **signal integrity** | A 2D electrostatic solver on cross-sections cut across the board every 2 mm along each listed net and differential pair, with all the copper, dielectrics and solder mask in the cut. It gives Z0 and εeff, the differential and common-mode impedance, the pair's skew, the crosstalk between tracks side by side and via stub resonances, against targets from the settings or KiCad 10's tuning profiles. **Signal integrity** tab. | seconds, part of Run checks |
| **safety** | Each net's voltage (from its name, the power stages and the parts) checked against clearances (IPC-2221B), capacitor and FET voltage ratings, resistor power and ESD protection. | part of Run checks and Simulate |
| **control loop and responses** | The loop gain of each buck or boost stage from the parts on the board and the controller's schematic fields; Bode plots, poles and zeros of signal paths, power stages and PDN rails, fitted to the solves Simulate already made. | part of Simulate |

Solver validation (`tests/test_solver.py`, `tests/test_multilayer.py`):
- The return-current profile under a microstrip matches image theory (`K(y) = I·h / π(h²+y²)`) to within a few percent.
- Loop inductance agrees with Wheeler's formula to within 1–4 %.
- Inductance rises steadily with slot length.
- At low frequency the current spreads along the resistive path.
- A fully cut plane is reported as a broken return path.
- On a 4-layer layer change, the nearest stitching via carries most of the plane-to-plane transfer, and moving it away enlarges the loop.
- A +3V3 reference plane carries return current only when decaps connect it to GND.
- Plane-pair capacitance matches ε0·εr·A/t exactly, and a plate pair resonates at c/(2a√εr) to within 3 % (`tests/test_capacitance.py`).

**How the solver splits the work.** Each route gets a fine model of its own region, which resolves the loop next to the trace. One coarser board-wide model includes the plane-pair capacitance of the whole board. All routes share it, and it is solved at the signal's actual harmonics. The two models are combined as vectors, so the coarse grid's own error at the fundamental cancels.

**Which harmonics get solved.** Harmonics near the emission limit get more solves. The worst harmonic gets a solve of its own, so the reported worst case is never an interpolation across a resonance.

**Accuracy.** On a dense 4-layer board, two board-wide grids, 0.71 and 0.49 mm, agree to 1.9 dB rms from 200 MHz to 1.6 GHz. They find resonances at the same frequencies, but peak heights can differ by up to 7 dB. A margin set by a resonance peak is therefore good to a few dB. The copper is sampled on a 0.15 mm sub-grid, so thin copper webs between rows of via antipads still conduct and the planes keep their real connectivity.

The emission numbers are meant for ranking and comparing layouts. They are not a compliance prediction, for several reasons:
- the model is quasi-static: it has no retardation and no radiation from plane edges,
- it estimates only the differential-mode loop of one net,
- common-mode currents on cables usually dominate real emissions.

## Install

The development environment (Python 3.11 or newer) uses the python.org interpreter. The MSYS/MinGW Python cannot use PyPI wheels.

```sh
git clone https://github.com/afkiwers/GroundTruth.git
cd GroundTruth
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[plugin,dev,thermal]"
.venv\Scripts\python -m pytest
```

For CI, install only the base package, which contains the CLI and has no wx or kipy dependencies:

```sh
pip install git+https://github.com/afkiwers/GroundTruth.git
```

`simulate --thermal` and `--capacity` need the `thermal` extra (numba, pyamg, psutil): `pip install "groundtruth-pcb[thermal] @ git+https://github.com/afkiwers/GroundTruth.git"`.

The package's install name is `groundtruth-pcb`. Don't `pip install groundtruth`: that name on PyPI belongs to an unrelated project.

To install the KiCad plugin:

1. In KiCad, enable the API: **Preferences → Plugins → Enable KiCad API**, then restart KiCad.
2. Run `python tools/install_plugin.py`. It copies `plugin/` and the `groundtruth` package into `<Documents>/KiCad/<10.x+>/plugins/io.github.iwers.groundtruth` for every KiCad 10+ it finds; `--dest` sets the target directory explicitly.
3. Close every KiCad window and start KiCad again. On first start, KiCad builds the plugin's virtualenv from `plugin/requirements.txt` in the background (several hundred MB from PyPI, a few minutes).
4. In the PCB editor, a toolbar button **Analyse return paths** appears at the right end of the top toolbar. If it's missing after the first start, the virtualenv was not ready yet: use **Tools → External Plugins → Refresh Plugins** or restart KiCad. The Plugin and Content Manager does not list the plugin, because it only shows packages it installed itself. A second button, **Add selected net to GroundTruth**, adds the net of the selected track to the Signal paths table (see "Finding the nets to analyse"). **Clear GT drawings** is available from the plugin menu and the window's top bar.

**Or as a package** (for the Plugin and Content Manager, e.g. on a colleague's machine): run `build_package.cmd` in the repo folder (from a console or by double-click). It builds `out\dist\groundtruth-<version>-<commit>-pcm.zip` from the current files (`build_package.cmd --committed`: from the last commit only; on any system `python tools/make_pcm_package.py` builds from the last commit, `--working-tree` from the files on disk). Every build gets a higher version, so the manager installs a newer zip as an update. In KiCad: **Tools → Plugin and Content Manager → Install from File…**, pick the zip, **Apply Pending Changes**, restart KiCad. The manager installs it to `<Documents>/KiCad/10.0/3rdparty/plugins/io_github_iwers_groundtruth` and lists it under **Installed**, where it can be updated (a newer zip) or uninstalled. Keep only one of the two installs: with both, every button shows twice (`python tools/install_plugin.py --uninstall` removes the copied one). The API must be enabled in either case.

**Where to find it** (checked on KiCad 10.0.5, Windows):

| What | Where |
|---|---|
| The button | PCB editor (open a board), top toolbar, right end, next to other plugin buttons such as Interactive HTML BOM. Its tooltip is "Analyse return paths". A small "GroundTruth – starting" window answers the click at once; then the window **&lt;board&gt; – GroundTruth** opens. It is not in the project manager or the schematic editor. |
| Start-up log | `%LOCALAPPDATA%\GroundTruth\plugin.log` (`~/.cache/groundtruth/plugin.log` elsewhere): the time of each start-up step, for a slow start, and wxWidgets' own messages (which KiCad would otherwise pop up in a Messages dialog). |
| Demo board | `demo\groundtruth_demo.kicad_pro` in the installed plugin folder (next row): a 4-layer board whose clock changes layers with no stitching via. Open it with **File → Open Project**, then follow `demo\README.txt`: Run checks, Simulate (the return fans out over both GND planes), Suggest fixes (the stitching via). Reinstalling the plugin resets it. |
| The installed plugin | `<Documents>\KiCad\10.0\plugins\io.github.iwers.groundtruth` (with OneDrive: `C:\Users\<you>\OneDrive\Dokumente\KiCad\10.0\plugins\...`). **Tools → External Plugins → Open Plugin Directory** opens the `plugins` folder. |
| Its Python environment | `%LOCALAPPDATA%\KiCad\10.0\python-environments\io.github.iwers.groundtruth`, which KiCad builds on first start. Delete this folder to make KiCad rebuild it. |
| Name in KiCad | "GroundTruth – return path / EMI analysis", identifier `io.github.iwers.groundtruth` |

To open the same window on a file without KiCad (drawing on the board is disabled in this mode), run `groundtruth-gui board.kicad_pcb`.

### The plugin window

- **Tabs:** **Signal paths** (signal paths to analyse: driver → series parts / op-amps → receivers, across nets), **Converters** (DC-DC power stages), **Bridges** (motor bridges), **DC loads** (rails with the currents their loads draw), **Power integrity** (PDN rails and their target impedance), **Signal integrity** (impedance targets, differential pairs, crosstalk), **Parts** (part models), **Issues** (the findings, shown in KiCad on a click), **Results** (current maps, cross-section, spectra, Bode plots, temperatures), **Fixes** (suggested fixes and what-if edits), **History** (every Simulate kept, compared) and **Server jobs** (analyses handed to a compute server).
- **Top bar:** **Reload board**, **Refill zones** (live KiCad only), **Run checks** (Tier 1), **Simulate** (Tier 1 + Tier 2, power stages and bridges), **Thermal**, **Export report…**, the **with what-if edits** tickbox, **Cancel**, **Save settings**, **Clear GT drawings** (live KiCad only), and at the right **Preferences…** and **Help** (F1).

Each is described below and in the plugin's own help.

### GPU acceleration (optional)

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
16-thread CPU measured 0.3-0.8x, and a full kraft_flux_mini_2 Simulate took over 2 h on it against 18 min on the CPU.
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

## The settings file: `groundtruth/<board>.groundtruth.yaml`

Everything GroundTruth keeps for a board goes into one folder next to it, `groundtruth/`, so the KiCad project folder stays tidy: the settings file `<board>.groundtruth.yaml`, the simulation history (`history/`) and, offered first by **Export report…**, the exported reports. Boards in the same folder share it; each file carries its board's name. A settings file directly next to the board (the layout before 2026-10-10) is not read. The settings file should be committed with the board. The plugin writes it, and you can also edit it by hand. When the plugin opens a board whose settings list no nets, power stages or motor bridges yet, it detects them in the background (as `groundtruth detect --write` does); they show in orange until you review them. `groundtruth init` creates it, or updates it after board changes: new parts are added, removed parts are flagged, and your edits are kept.

```yaml
reference_nets: [GND]
analysis: {grid_mm: auto, margin_mm: 10, solve_points: 4, limit_class: B, distance_m: auto, ground_reflection: true,
           planes: auto,                         # auto = all plane layers in the region, nearest = one plane only
           max_nodes: 100000,                    # grid budget of each route model
           plane_capacitance: true,              # board-wide model with plane-pair capacitance from the stackup
           cavity_max_nodes: 40000,              # its grid budget (raise for big boards; cost grows ~linearly)
           max_solves: 10,                       # harmonics solved per route (initial + refinement)
           threads: auto, gpu: auto,             # parallel solves (auto: physical cores, at most 4); GPU: auto | on | off
           driver_ohms: 25, load_pf: 5}          # default driver resistance and receiver capacitance
rules: {stitch_distance_mm: 2.0, decap_distance_mm: 5.0, edge_factor: 3.0, min_gap_mm: 0.1,
        detour_warning: 1.25, detour_error: 2.0, emi_margin_db: 6.0, resonance_db: 10.0,
        severity: {reference_net: off}}          # per-check override: error | warning | info | off
nets:                                            # nets to analyse: typed in, or suggested by `groundtruth detect`
  - {name: /CPU/SPI_SCK, driver: U1102.12, receivers: [U1103.6], freq: 20MHz, rise_time: 2ns, amplitude_v: 3.3,
     driver_ohms: 33}                            # optional per net: driver_ohms, load_pf, current_a ...
  - {name: /USB_D_A, driver: U1.5, receivers: [U9.2], freq: 50MHz, rise_time: 1ns, fall_time: 1.5ns,
     driver_nh: 1.5, driver_pf: 3, load_ohms: 50, through: [C12]}   # a path through an AC-coupling C12
  - {name: /CPU/SPI_MISO, driver: U1103.2, receivers: [U1102.13], freq: 10MHz, rise_time: 2ns, amplitude_v: 3.3,
     source: auto, reviewed: false, class: SPI data}   # suggestion, not yet confirmed
  - {name: /CPU/SW_CLK, driver: J1101.4, receivers: [U1102.28], freq: 10MHz, rise_time: 3ns,
     enabled: false}                             # switched off: kept, but not analysed (the tick box in the plugin)
components:                                      # one line per part, pre-filled with a suggested model
  R12: {value: 0R, footprint: R_0402_1005Metric, model: short}
  R7:  {value: 33R, footprint: R_0402_1005Metric, model: resistor, R: 33.0}
  C4:  {value: 100n, footprint: C_0402_1005Metric, model: cap, C: 1e-07, ESR: 0.02, ESL: 4e-10}
  FB1: {value: 600R@100MHz, footprint: L_0603_1608Metric, model: ferrite, R: 0.1, L: 9.5493e-07}
  U1:  {value: STM32, footprint: LQFP-48, model: open}
waivers:
  - {fingerprint: 7bacf605e3401d93, reason: "slow signal, accepted"}
modifications: []                                # what-if edits, see "Fix suggestions" below
report: {title: "EMI and power analysis", company: ACME, author: J. Smith, document: R-1234}   # optional: the report's cover
```

The plugin's **Preferences…** are not in this file: they belong to the computer, not the board (see "Preferences" below). Simulate's resolution (`analysis.resolution`, the plugin's Resolution or the CLI's `--resolution`) is never written to it.

Available part models are `short`, `open`, `resistor (R)`, `cap (C, ESR, ESL, optional C_bias: its DC-bias curve, `75nF@50V` or `95nF@10V, 66nF@30V, 39nF@50V`, applied per capacitor at the DC voltage across it; see [Capacitance at DC bias](#capacitance-at-dc-bias))`, `inductor (L, R)`, `ferrite (R, L)` and `amplifier` (an op-amp in a signal path: `gbw`, optional `aol` (open-loop gain, dB, 100), `fp2` (a second open-loop pole) or `pm` (its phase margin at unity gain, degrees), `z_out` (the open-loop output resistance R_O, 100 Ω), `c_in` (each input to ground, 2 pF), `r_in`, `slew`, `cmrr`; `pins: [in, out]` for a dual or quad op-amp; with these the circuit around it decides its gain (see [Op-amp circuits](#op-amp-circuits-and-the-transfer-function)); `gain` in V/V, negative = inverting, makes it a fixed gain block instead). `C_bias` is a ceramic capacitor's capacitance at its DC voltage, from the maker's DC-bias curve, and replaces C in every solve: a class II ceramic (X7R, X5R) keeps only 30–60 % of its nominal C at half its voltage rating, which raises a converter's ripple and conducted emission. A power stage's results name its ceramic capacitors of 1 µF and up that are still at their nominal C on a rail of 10 V or more. They are used in two places:
- **Signal route:** a route runs through a part whose model passes a signal, such as a 0R, a termination resistor (≤ 1 kΩ) or a ferrite.
- **Plane connections:** caps, 0R and ferrites that bridge plane islands, for example decaps between GND and +3V3, become lumped branches in the solver.

### Part parameters from the schematic (InvenTree fields)

A part's parameters can come from its symbol's fields instead of being typed in: give the parts these fields in the schematic, or name InvenTree's parameter templates so (KiCad's HTTP library hands them to the symbols as fields). One field name per parameter, matched without regard to case:

| Field | Parts | Fills | A bare number is in | Example |
|---|---|---|---|---|
| `Rds(on)` | FET, switch IC | `rds_on` (high side or integrated switch), `rds_on_low` (low side); per FET | mΩ | `3.8milliohm` |
| `Coss` | FET | `c_oss` of the switch-node device (a side's parallel FETs together) | pF | `616 picofarad` |
| `Qoss` | FET | `q_oss` (as Coss) | nC | `38 nanocoulomb` |
| `Vsd` | FET | a bridge's `v_sd` | V | `2.1V` |
| `Vf` | diode | a catch diode's `diode_vf` | V | `0.55V` |
| `Cj` | diode | a diode buck's `c_oss` | pF | `120 picofarad` |
| `DCR` | inductor | `dcr` and the inductor model's R | mΩ | `6.9milliohm` |
| `ESR`, `ESL`, `Cbias` | capacitor | the model's ESR, ESL, C_bias (one value, a point or a curve of 1-12) | mΩ, nH, µF | `5milliohm`, `0.4nH`, `75nF@50V` |
| `Cbias Voltage`, `Cbias Curve`, `Dielectric` | capacitor | the voltage a single Cbias holds at; the curve as text (an InvenTree text template, wins); C0G / NP0: no curve | V, text, text | `50V`, `95nF@10V, 39nF@50V`, `X7R` |
| `Capacitance`, `Inductance`, `Resistance` | C, L, R | the model's C, L, R (else the Value field) | F, H, Ω | `10uF` |
| `Gain` | op-amp stage | the amplifier's gain (a Gain field makes the part an amplifier); `20dB` = 10 | V/V | `10`, `-2`, `20dB` |
| `GBW`, `Bandwidth` | op-amp | GBW: the gain-bandwidth (the circuit model's dominant pole at GBW/Aol). Bandwidth: a gain block's (a part with `Gain`) one pole, else at GBW / \|gain\|; on an op-amp solved in its circuit, **its stage's bandwidth limit**: a stage wider than it is a `bandwidth_limit` issue | MHz | `10MHz` |
| `Zout`, `Cin`, `Rin` | op-amp | the open-loop output impedance (the datasheet's R_O / Z_O; with GBW or Bandwidth the stage's output is Zout·jf/(jf + f_c), divided by the loop below its bandwidth f_c; without, Zout as given; none: 1 Ω), input C and R (load of the input side) | Ω, pF, Ω | `2.3kohm`, `3pF` |
| `Slew Rate` | op-amp | caps each output harmonic at SR / 2πf | V/µs | `20V/us` |
| `Aol`, `Fp2`, `PM` | op-amp | open-loop gain, a second open-loop pole or the phase margin at unity gain (the circuit model) | dB, Hz, ° | `110dB`, `20MHz`, `60` |
| `CMRR` | op-amp | shown only: the path model is single-ended | dB | `90dB` |

- **Values:** read with units as InvenTree sends them (`3.8milliohm`, `616 picofarad`, `6.9 mΩ`) or in datasheet notation (`3.8m`, `616p`, `4m7`); a bare number is in the unit above. A value of the wrong kind (`100V` for Rds(on)) is not used, and the stage's or part's note says why; a value outside the usual range is used as written, with a note. Parallel parts whose fields disagree give no value.
- **In sync:** a stage's or part's value from the schematic is listed in its `from_schematic` (in the settings file) and is read again on every open (`groundtruth init`, the plugin) and before every run, so an InvenTree change arrives by itself once KiCad's symbols and PCB are updated. Detection fills a new stage from its parts' fields.
- **Set here:** a value typed in GroundTruth (the forms, the Parts table, the file) is the user's: kept, and where the schematic says something else, marked `!` in the forms and the Parts table's Schematic column, and reported as the info issue `schematic_differs`. **Use schematic values** (Converters, Bridges) and **Reset filtered to suggested** (Parts) take the schematic's values back; an emptied form field takes it too.
- The plugin's Help (**Part parameters from the schematic**) lists the same table, generated from `components/fields.PARAMS` with the usual ranges.

**Settings reference.** Every key the parser reads, with its default. The power-stage, bridge, victim, stack,
thermal and what-if keys are listed in their own sections below.

| Key | Default | Meaning |
|---|---|---|
| `version` | 1 | File format, written by `init` (not read). |
| `reference_nets` | `[GND]` | Ground nets the return may use and return pins are taken from. `init` guesses them from the net names (GND, AGND, PGND, VSS, 0V, …). |
| `analysis.grid_mm` | `auto` | Grid pitch of the route and power-stage models in mm. `auto`: the dielectric height (0.05 to 0.25 mm), coarsened until `max_nodes` holds. |
| `analysis.margin_mm` | 10 | Copper around a route that its model includes. |
| `analysis.solve_points` | 4 | Harmonics solved first per route: the fundamental plus log-spaced harmonics across the limit band. |
| `analysis.max_solves` | 10 | Harmonics solved per route in all (the first ones plus refinement near the limit). |
| `analysis.max_nodes` | 100000 | Grid budget of each route or power-stage model. |
| `analysis.plane_capacitance` | true | The board-wide model with plane-pair capacitance (displacement current, plane resonances). |
| `analysis.cavity_max_nodes` | 40000 | Grid budget of that board-wide model (raise it for big boards; the cost grows about linearly). |
| `analysis.planes` | `auto` | `auto`: every plane layer in the region; `nearest`: one plane only. |
| `analysis.limit_class` | `B` | CISPR 32 class `A` or `B` for the radiated estimate. |
| `analysis.radiated_limit` | none | Another radiated limit instead of CISPR 32. MIL-STD-461G RE102 by name (1 m, Table II bandwidths, Figures RE102-1 to -4): `re102_ground_army` (Navy mobile & Army), `re102_ground_navy_fixed` (Navy fixed & Air Force), `re102_aircraft_external` (fixed wing external, from 2 MHz), `re102_helicopter` (helicopters, from 10 kHz; `re102_space` the same, 461H's space systems), `re102_aircraft_internal` (fixed wing internal, < 25 m nose to tail), `re102_aircraft_internal_25m`, `re102_ship_above_deck`, `re102_ship_below_deck`, `re102_submarine_external`, `re102_submarine_internal`. Or a curve of your own: `{name: "customer limit", distance_m: 1, bands: mil461, points: [[2MHz, 24], [100MHz, 24], [18GHz, 69]]}`, straight on a log-frequency axis between the points, no limit outside them; `bands`: `mil461` or `cispr`. The radiated band follows the limit (from 2 MHz for ground and fixed wing, 10 kHz for helicopters). The curves are 461G's; 461H (April 2026) keeps them, its Figure RE102-3 naming the 10 kHz curve "Space and Helicopters". |
| `analysis.distance_m` | `auto` | Measurement distance; `auto`: CISPR 32's (10 m below 1 GHz, 3 m above). |
| `analysis.ground_reflection` | true | Doubles the field (+6 dB) for the ground reflection of an open-area or semi-anechoic test site. |
| `analysis.driver_ohms`, `analysis.load_pf` | 25, 5 | A net's driver output resistance and receiver input capacitance, unless the net sets its own. |
| `analysis.conducted`, `analysis.conducted_class` | `cispr32`, `B` | Conducted limit of power stages and bridges: `cispr32` (`A`/`B`), `cispr25` (`1`–`5`), `mil461` (`basic` = `28V`, `115V`, `220V`, `270V`, `440V`), `none`. |
| `analysis.mil461` | `G` | The MIL-STD-461 revision: `G` (11 December 2015) or `H` (17 April 2026, which supersedes G). Only CE101 differs between them here: under `H` the ships' limit runs to 20 kHz (Figure CE101-1 continued to 66 dBµA) and its relaxation is capped at 35 dB; CE102, RE102, the LISN and the bandwidths are the same in both. |
| `analysis.ce101` | `none` | MIL-STD-461 CE101, the power leads' current from 30 Hz to 10 kHz, for ships under 461H to 20 kHz (dBµA, current probe, 10 Hz / 100 Hz / 1 kHz bandwidths): `aircraft` (Figure CE101-4: Army aircraft including flight line, Navy ASW aircraft; curve #1 above 28 V, #2 at 28 V or below) or `ships` (Figure CE101-1: surface ships and submarines, DC, relaxed by 20·log(I/3) above 3 A, at most 35 dB). Needs a conducted model at the input (`conducted`, e.g. `mil461`). A motor bridge's commutation (lines every electrical frequency, strongest at 6·f_e) and a converter switching within the range are read (between 10 and 20 kHz from the CE102 band's solves, the LISN as specified at the input); below 10 kHz the leads (2 × (50 µH + 50 mΩ) to a stiff source) can resonate with the bus capacitors in this band. `ce101_source_v` and `ce101_load_a` override each stage's own input voltage (curve) and current (relaxation). |
| `analysis.via_plating_um` | 25 | Via barrel plating, for the via current density and IPC-2221 capacity. |
| `analysis.threads` | `auto` | Parallel solves: `auto` (physical cores, at most 4) or a number. |
| `analysis.gpu` | `auto` | `auto`, `on` or `off` (see "GPU acceleration"). |
| `rules.stitch_distance_mm` | 2.0 | A layer change between planes of one net needs a via of that net this close (`layer_change_stitch`). Also the distance for power-stage via fixes. |
| `rules.decap_distance_mm` | 5.0 | A layer change between planes of different nets needs a capacitor between them this close (`layer_change_decap`). |
| `rules.edge_factor` | 3.0 | `edge_proximity` when a trace runs closer than this × its dielectric height to its plane's edge. |
| `rules.min_gap_mm` | 0.1 | Uncovered stretches of trace shorter than this are ignored (`gap_crossing`, `off_plane`). |
| `rules.detour_warning`, `rules.detour_error` | 1.25, 2.0 | `return_detour` at this loop inductance / ideal loop inductance. |
| `rules.emi_margin_db` | 6.0 | `emi_margin` and `conducted_emission` when the margin to the limit is below this (an error below 0 dB). |
| `rules.resonance_db` | 10.0 | `plane_resonance` when a plane resonance raises the loop moment this much over its value at the fundamental. |
| `rules.overshoot_warning`, `rules.overshoot_error` | 0.25, 0.5 | `hot_loop`: switch-node overshoot as a fraction of the rail voltage. |
| `rules.current_density_warning`, `rules.current_density_error` | 50, 100 | `current_density` in A/mm² (copper averaged over 1 mm, via plating). |
| `rules.via_load_warning`, `rules.via_load_error` | 100, 150 | `via_overload` in % of a via's IPC-2221 capacity. |
| `rules.severity` | `{}` | Per-check severity override (see "Checks and severities"). |
| `drawing.issues_layer`, `drawing.suggest_layer`, `drawing.current_layer` | `User.9`, `User.8`, `User.7` | Layers the plugin draws issue markers, suggested fixes and contours/arrows on. |
| `report.title`, `company`, `author`, `document` | empty | The HTML report's cover (title default "EMI and power analysis"). |
| `waivers[].fingerprint`, `reason` | | An accepted issue: it is still listed, marked "waived" with the reason, but does not fail the run and is left out of the counts and the Code Quality report. |

A `nets:` entry:

| Key | Default | Meaning |
|---|---|---|
| `name` | | The net name as on the board. |
| `driver`, `receivers` | | Pins as `REF.PAD`; `receiver:` (one pin) works too. |
| `freq`, `rise_time` | | Fundamental and edge time (`20MHz`, `2ns`, or plain Hz and s). |
| `amplitude_v`, `duty` | 3.3, 0.5 | Swing and duty cycle of the trapezoid spectrum. |
| `current_a` | | Overrides the line-current estimate: peak A at the fundamental. |
| `driver_ohms`, `load_pf` | `analysis` | Driver output resistance, input capacitance per receiver. |
| `driver_nh`, `driver_pf`, `fall_time` | none, none, `rise_time` | The source: its series inductance (nH), its output capacitance (pF), a falling edge of its own. |
| `load_ohms` | none | A resistance at the receivers (a parallel termination). |
| `max_bandwidth` | none | The path's bandwidth limit (`1kHz`: an ADC input's anti-alias filter): its −3 dB frequency at the receiver (3 dB below the passband peak) above it is a `bandwidth_limit` issue; also when the response does not fall 3 dB within the sweep while the sweep reaches above the limit. |
| `sweep_from`, `sweep_to`, `sweep_points` | f/100, the band's top, 300 | The path's Bode sweep (`1Hz`, `10MHz`; from 10 mHz; 20 to 5000 points): Simulate and `groundtruth bode` / the Signal paths tab's **Bode** sweep the path's circuit over it. A sensor or ADC input wants `sweep_from: 1Hz`: the gain at the sweep's start is shown as the DC gain. |
| `through` | | Parts the signal path may run through although they don't pass a signal by default (an AC-coupling capacitor, a resistor above 1 kΩ). The path from `driver` to the receivers runs through series parts into other nets, each part with its impedance. |
| `driver_return`, `receiver_return` | | The return pin (`REF.PAD`). Default: the nearest reference-net pad of the same part, else of any part within 10 mm; without one the route gets `return_path_broken`. |
| `source`, `reviewed`, `class` | `manual`, true | Set by detection: `auto`, unreviewed, and the signal class (information only). |

A `components:` entry: `value` and `footprint` (from the board; `init` compares them), `model` with its parameters
(above), `pins` (the two pins the model joins, by pad number or pin name, e.g. `[IP+, IP-]`; default all pads),
`note` (`init` writes "value changed …" there), `removed: true`, which `init` sets when the part has left the
board (its model is then ignored), and `tht` (its through-hole pin values, below).

**Through-hole pins** (`tht`): built in, a soldered pin is copper filling its whole hole, joined to every layer with a
perfect joint. A part's own values (`components: J1: {..., tht: {contact: 0.5m, fill: 75}}`, the Parts tab's Pins
column, or its schematic GT field: `GT = conv_in, contact:0.5m`) and the board's defaults (top-level `tht: {pin:
brass}`, the Parts tab's Pin defaults…) change that, each value from the first place that has it (GT field, the
part's entry, the board's defaults, built in): `contact` (the mating contact, Ω per pin), `joint` (the solder joint,
Ω per pin), `fill` (% of the hole soldered from the side opposite the part; above it only the plating conducts and
the current enters the board where the solder ends), `pin` (`hole`, `copper`, `brass`, `bronze`, `becu`,
`tellurium`), `pin_d` (mm, default the hole less 0.25 mm), `pin_r` (Ω through the board). Contact and joint add I·R
to a DC load's drop and I²·R of heat in Thermal at the fed pins (DC loads, converters' connectors); a connector pin
carrying 3 A or more without a contact value gets a note.

### Checks and severities

Every issue has one of these checks. The default severity is the one listed; where two are given, the threshold
rules above decide.

| Check | Default | Meaning |
|---|---|---|
| `route_missing` | error | No copper path between driver and receiver. |
| `no_reference` | error | The signal layer has no adjacent copper layer to act as reference plane. |
| `gap_crossing` | error | The trace crosses a gap or split in its reference plane. |
| `off_plane` | warning | The trace runs without a reference plane (says when the return falls back to a farther layer). |
| `layer_change_stitch` | error | A signal via moves the return between planes of one net with no via of that net within `stitch_distance_mm`. |
| `layer_change_decap` | warning | A signal via moves the return between planes of different nets with no capacitor between them within `decap_distance_mm`. |
| `edge_proximity` | warning | Trace closer than `edge_factor` × h to its plane's edge. |
| `reference_net` | info | Trace referenced to a non-ground plane. |
| `schematic_differs` | info | A part parameter set in GroundTruth differs from the part's schematic field (see "Part parameters from the schematic"). |
| `unusual_value` | info | A power stage's or bridge's value is outside the usual range for its kind (often a missing unit prefix). It is used as written. |
| `return_path_broken` | error | The solver finds no return: no reference-net pin near a driver or receiver, no plane copper near the route or a return pin, or no copper connection between the return pins (a split plane). |
| `return_detour` | warning / error | Loop inductance over `detour_warning` / `detour_error` × the ideal. |
| `plane_resonance` | warning | A plane-pair resonance raises a route's or hot loop's moment by `resonance_db` or more. |
| `bandwidth_limit` | warning | A signal path's bandwidth at its receiver above its `max_bandwidth`, or an op-amp stage's (from its input to its output, solved in its circuit) above its `Bandwidth` field (1 % tolerance). |
| `opamp_stability` | warning / error | An op-amp's loop has less than 45° of phase margin (it rings); below 20° an error (close to oscillating). Give the op-amp `PM` or `Fp2` for its second pole: with one pole the margin is always near 90°. |
| `emi_margin` | warning / error | Radiated estimate within `emi_margin_db` of the limit / over it (routes and hot loops). |
| `band_unsolved` | warning | A solve did not converge and the band above the last solved frequency is held at its value there, not computed: a plane resonance in it would be missed. Simulate again at another resolution. |
| `hot_loop` | warning / error | Switch-node overshoot over `overshoot_warning` / `overshoot_error`. |
| `conducted_emission` | warning / error | Conducted estimate at the input (differential or common mode) within `emi_margin_db` of the limit / over it. |
| `power_loop_broken` | error | A converter's hot loop cannot be closed in the copper. |
| `current_density` | warning / error | DC (or a bridge's RMS) current density over the `current_density_*` limits. |
| `via_overload` | warning / error | A via over `via_load_warning` / `via_load_error` % of its IPC-2221 capacity. |
| `bridge_operating_point` | warning | A motor bridge cannot reach its phase current at this speed and bus voltage. |
| `victim_coupling` | warning | A bridge's or power stage's switching puts noise or injected current on a victim net, or moves a victim part's ground. |
| `thermal_hotspot` | warning / error | Copper over `thermal.warn_c` / `thermal.max_c`. |
| `part_temperature` | warning / error | A part with losses over `warn_c` / `max_c`; parts heated past `max_c` by the copper alone, as one error. |

`rules.severity` maps a check to `error`, `warning`, `info` or `off` (`severity: {reference_net: off, edge_proximity: info}`).
The override replaces the severity whatever the threshold says, so `{emi_margin: warning}` makes even a failed limit
a warning; `off` drops the check's issues entirely. `--fail-on` then decides which severities fail a CLI run.

### Finding the nets to analyse

`groundtruth detect board.kicad_pcb` (or **Detect…** on the plugin's Signal paths tab) suggests nets with their driver, receivers and default timing:
- **Signal class** comes from the net name and the schematic pin names, for example SPI clock (20 MHz / 2 ns), RMII (50 MHz / 1 ns), USB, SDIO/QSPI, debug clocks, crystals and switching nodes. A net with no class gets 10 MHz / 2 ns, unless an op-amp with a `Slew Rate` or `GBW` / `Bandwidth` field drives it (its output on the driver's net): then the fastest full 3.3 V swing that op-amp can make, edge max(0.8·3.3 V / SR, 0.35 / BW) and frequency min(10 MHz, BW, SR / (π·3.3 V)). kraft_flux_mini_2's U301 (TLV6001, 0.5 V/µs) gives Net-(U301--) 5.28 µs at 48.2 kHz instead of GHz content on a current-sense signal.
- **Driver** comes from the schematic pin types: an output beats a bidirectional pin, which beats a connector/passive pin, which beats an input.
- **Series parts:** suggestions follow 0R/termination resistors and ferrites, so a series-terminated line is one net.
- **Excluded:** power nets.

Each suggestion shows its confidence and the reasons it was made. `--write` adds them to the settings file as `source: auto, reviewed: false`. Reports keep reminding you about unreviewed nets until you mark them reviewed: edit them, press **Mark reviewed**, or set `reviewed: true` in the file. `--min-score` widens the search: 1 adds UART/CAN/PWM, 0 adds I2C.

**Adding a net from KiCad.** Select a track, via or pad in the PCB editor (click it, or right-click it: that selects it too), then press the toolbar button **Add selected net to GroundTruth** (green plus). Its net appears in the Signal paths table of the open GroundTruth window, which comes to the front; if no window is open for the board, one opens. In the window, **Add KiCad selection** on the Signal paths tab does the same. Driver and receivers are suggested from the pin types as above, also for nets whose names match no signal class (then set the frequency and rise time yourself); the new row is unreviewed (orange). Reference nets (GND), supply nets and every net of a power stage's power flow are refused with the reason. That includes the switch node, the inductor's output side and the output path through current-sense shunts, even when a sense amplifier's input pin sits on one. Detection (`groundtruth detect`, **Detect…**) skips them too. KiCad 10 does not let plugins add entries to the right-click menu, hence the button.

**Adding a whole path from KiCad.** Select one bit of copper on each net of a signal path (Ctrl+click: the micro's ADC net, the op-amp's output net, its input net from the current-sense amplifier), then press the same button. GroundTruth puts the nets in order through the parts joining them, takes the direction from the op-amp's input and output pins (or the pin types at the ends; a selected pad fixes an end), and adds one signal path with its Pass-through resistors and the op-amp modelled as an amplifier with its pins. It also works out the GT labels for the schematic symbols; KiCad 10 has no schematic API for plugins, so they are shown to add by hand (Symbol Fields Table), while the path itself works in Simulate at once.

### Switching power stages (DC-DC converters)

A converter is not a signal net. Its switches move the load current between the high-side and the low-side switch within the edge time. That fast current flows around the **hot loop**: input capacitors, high-side switch, switch node, low-side switch, ground and back to the capacitors. groundtruth models the loop as copper, not as lines:

- **Copper:** every net the loop touches is solved, pours and tracks alike. That covers the rail, the switch-node pour, ground, the input path up to the connector, and every plane in the region. A route that runs through its own net's pour is solved through the pour too, so the current spreads over the pour's width and stitching vias.
- **Capacitors:** they are branches with their C, ESR and ESL. The solver gives each one's share of the loop current, at the switching frequency (ripple) and at 100 MHz (the edges).
- **Switches:** they are known currents through each package, injected into the copper at every pad.
- **Conducted emission:** with an input connector, a LISN per line closes the input. The solver then gives the share of the pulsed current that reaches the input lines.

```yaml
converters:
  - {name: buck 5V, topology: buck, rail: VIN, switch_node: SW, ground: GND, high_side: Q1, low_side: Q2,
     f_sw: 400kHz, v_in: 24, v_out: 5, i_out: 3, rise_time: 5ns, c_oss: 1nF, input_connector: [J1.1, J1.2],
     output_connector: [J2.1, J2.2],          # optional: the load (DC output path and ground return)
     c_stray: 20pF,                           # optional: switch node to chassis/heatsink (common mode)
     spread: 0.2, spread_mode: triangle, spread_rate: 10kHz}   # optional: spread spectrum (f_sw … +20 %)
  - {name: 3V3 regulator, rail: +12V, ground: GND, switch: U5, f_sw: 2MHz, i_out: 0.5, v_in: 12, v_out: 3.3}
analysis: {conducted: cispr32, conducted_class: B}   # cispr32 A/B | cispr25 1-5 | mil461 basic/115V/... | none
rules: {overshoot_warning: 0.25, overshoot_error: 0.5}   # switch-node overshoot as a fraction of the rail voltage
```

| Key of a `converters:` entry | Default | Meaning |
|---|---|---|
| `name` | the switch node | Its name in reports and `--nets`. |
| `topology` | `buck` | `buck`, `boost` or `custom` (sets the pulse amplitude and duty). |
| `rail`, `ground`, `switch_node` | | The hot loop's capacitor net (a buck's VIN, a boost's VOUT), power ground, switch node. |
| `high_side`, `low_side`, `switch` | | Parts (one or a list): discrete switches and a catch diode, or `switch` for an IC with both switches inside. |
| `f_sw`, `i_out`, `rise_time` | 500 kHz, 1 A, 10 ns | Switching frequency, load current, current commutation (switch-node edge) time. |
| `v_in`, `v_out` | | Voltages; they also give the duty of a buck or boost. |
| `duty` | from `v_in`/`v_out`, else 0.5 | Fraction of the period the high side conducts. |
| `c_oss`, `q_oss`, `r_damp` | –, –, 0.1 Ω | Switch output capacitance (ringing) and charge (switching loss), ringing damping. |
| `c_stray` | | Switch node to chassis or heatsink: common-mode estimate. |
| `input_connector`, `output_connector` | | `[plus, minus]` pins: the LISN, and the load (DC output path and ground return). |
| `spread`, `spread_mode`, `spread_rate` | –, `random`, per cycle | Spread spectrum (see below). |
| `interleave`, `phase_deg` | –, 360°/n apart | Interleaved stages: a group name shared by the phases of one controller, and this stage's delay in degrees of the switching period (see below). |
| `conducted` | `analysis.conducted` | This stage's conducted standard. |
| `rds_on`, `rds_on_low`, `dcr`, `diode_vf` | –, `rds_on`, –, 0.5 V | Losses for the thermal solve (see "Temperatures"). |
| `source`, `reviewed`, `note` | `manual`, true | Set by detection (`auto`, unreviewed) and its note. |

- **Topologies:** discrete stages use `high_side` and `low_side` (a catch diode counts as the low side), and the high side can be an IC with its own switch. An IC with both switches inside is listed as `switch`; its hot loop runs from its input pins to its ground pins. A boost's hot loop sits on its output, so `rail` is VOUT. A boost's conducted emission comes from its inductor ripple (ΔI = V_in·D/(L·f_sw), with `v_in`, `v_out` and the inductor's L), drawn from the inductor's input net.
- **Results:**
  - the hot-loop inductance and resistance;
  - the L·di/dt overshoot;
  - with `c_oss`, the ringing frequency, its Q and the overshoot, plus the spectrum shaped by that resonance (damping `r_damp`, default 0.1 Ω plus the loop resistance). The spectrum also carries the charge each edge moves into the switch node (C_oss·dv/dt, or `q_oss` per edge when given; with a snubber its current too) on top of the load pulse: the loop current is the pulse × |1 + Y_node·V/I|. Near the ringing this dominates when V/I is large: on kraft_flux_mini_2's 51.8 V stages it is 3× the pulse at 10 A and 6× at 5 A. Without `c_oss` (an integrated stage, a catch diode) neither the ringing nor this current is modelled, so give the diode's junction capacitance as `c_oss` for a diode buck;
  - the capacitor current sharing;
  - the radiated estimate of the hot loop, solved as for nets, plane capacitance included;
  - the conducted emission at the input against CISPR 32 (class A/B average limit), CISPR 25 (voltage method, classes 1–5) or MIL-STD-461 CE102, with the standards' receiver bandwidths.
- **DC power paths** (current density and resistance in the pours): the load current from the switches through the switch node to the inductor, and the input current from the input connector to the high side. The input current is the expected one, from an energy balance: output power plus the losses Thermal uses (conduction, switching edges, output charge, diode, inductor DCR, series parts) plus the DC paths' copper, over the input voltage (no gate drive or core loss); it is shown beside the connector, with the efficiency. With `output_connector: [J2.1, J2.2]` (the load's + and − pins; `groundtruth detect` finds a connector), two more: the output from the inductor (a boost: from its high side) to the load, and the ground return from the load, which splits by Kirchhoff between the low side and the input connector (the input connector takes back the input current). All DC paths of all stages are solved on one copper model of the whole board, so nothing is cut off at the edge of a box around the stage. Parts join the copper over the whole area of their pads: each pin takes its share of the current by its copper area, pads drawn on top of each other (an EPC GaN FET's source pin: a bar under three tabs) counted once.
  - **All power stages together:** with two or more stages, the Results tab's Route list starts with **all power stages DC currents together**: every stage at its own load at once, their currents added (they share the input connector, a surge stopper, the rail and the ground). Its title gives the loads, the total input current and the copper loss. The current-density and via-load checks run on this total, as issues of *all power stages*, in place of each stage's own DC checks. The HTML report shows it first under Power stages (input current, copper loss, hotspots, the busiest vias, the map). Each layer's copper thickness comes from the stackup. The results are the resistance, the voltage drop and loss, the current density in A/mm², and the current in each via and its plating. The density is reported both as the peak cell and as "sustained", averaged over 1 mm: corner peaks at pads don't heat copper, a narrow neck does. Every DC path is also a heatmap, in the plugin's Results tab and in the HTML report. Thresholds: `rules: {current_density_warning: 50, current_density_error: 100}` (A/mm², copper and via plating).
- **Input filter** (guidance): when the conducted estimate misses the margin, the report proposes a π-filter: a series L and a capacitor at the connector, with the stage's input capacitors on the other side. Its corner f_c is set so the 40 dB/decade roll-off gives every harmonic its missing margin plus 6 dB. The proposal includes an example L/C pair and the usual RC damping (R = √(L/C) in series with 4·C). It is first-order guidance: check the damped filter against the converter's input impedance (Middlebrook). The
fix advisor's proposed filter is also drawn: its attenuation among the stage's Bode responses, and the filter's
output impedance next to the converter's input impedance −V²/P (Middlebrook's two curves) in the stage's
"impedances" view.
- **Spread spectrum:** with `spread: 0.2` the switching frequency is dithered from f_sw to f_sw + 20 %. GroundTruth simulates the EMI receiver (its bandwidth, detector and dwell time) on the swept harmonics instead of using a rule of thumb, because the benefit depends strongly on the modulation. A slow sweep passes a peak detector at full amplitude. Random changes every cycle barely spread the line. A sweep at ~10 kHz gained ~9 dB at the fundamental on the example board. Set `spread_mode: random` (with `spread_rate` hops per second, default every cycle) or `triangle` (with `spread_rate`, the modulation frequency). Datasheets often omit the modulation, so try both. The radiated estimate of the stage is read the same way. Where the sweeps of neighbouring harmonics overlap (high harmonics), a peak detector reads the largest single pass: the harmonics are f_sw apart, more than the receiver bandwidth, so only one is in the filter at a time.
- **Interleaved stages:** the channels of a dual or multiphase controller (kraft_flux_mini_2's LTC7890 runs SW1 and SW2 180° apart) share one `interleave:` group name, each with its `phase_deg`. Simulate then adds their input currents at the LISN and their hot loops' fields as phasors per harmonic, with each stage's own transfer and loop moment. 180° apart they cancel at the odd harmonics of f_sw and add at the even ones. Each member stage reports the group's margins together, the same sum in phase for comparison, and its own. `groundtruth detect` proposes a group for the stages one IC switches (0° and 180° for two): check the controller's datasheet. The stages need one f_sw and the same spread spectrum. CE101 is combined the same way (for stages switching within its range), against the limit at the stages' input current together. Tested end to end on two bucks, two boosts (their inductor ripple) and a 2 kHz pair's CE101.
- **Common mode** (budget): the switch node swings the rail voltage with the switching edges. Through its stray capacitance to a chassis or heatsink, that drives a common-mode current back through both LISNs. That capacitance depends on the mechanics, not the board. groundtruth therefore reports the budget: the largest switch-node capacitance to the chassis that keeps the conducted reading under the limit, and the frequency where it binds. With `c_stray: 20pF` in the converter entry it also estimates the common-mode emission and checks it.
- **What the radiated estimate is:** the hot loop's own (differential-mode) radiation at the limit's distance. Cables driven by the switch node's common-mode voltage usually radiate more, so read it as a lower bound for the product, not a compliance prediction.
- **Issues:** `hot_loop` (overshoot), `current_density`, `conducted_emission`, `emi_margin` and `plane_resonance`. A loop that cannot be closed in the copper gives `power_loop_broken`. When the loop closes only through copper beyond the default 5 mm around the stage, the model region widens (10, then 20 mm) and a note says so: that detour is usually the finding.
- **How to fix it** (every Simulate; the Fixes tab's *Design changes* list, each stage in the reports): for a stage with ringing, radiated or conducted issues, the design changes that clear them. Each comes with its parts, the predicted radiated and conducted margins, the overshoot, the extra loss and the issues it clears. They are worked out on the stage's own solves (no new solve), so the stage's row reproduces its reported figures:
  - an RC snubber across the switch node (C a few × C_oss, R ≈ √(L/C_oss), its C·V²·f_sw loss);
  - a slower edge (gate resistor; ½·V·I·Δt·f_sw);
  - the largest hot-loop inductance that meets the targets, if a layout can reach it;
  - the cheapest mix of these when none alone does;
  - a damped input π-filter from standard values, checked against the converter's input impedance (Middlebrook);
  - spread spectrum through the simulated receiver;
  - the common-mode choke and Y capacitors for assumed strays.

  The targets are `rules.emi_margin_db` and `overshoot_warning`, or 80 % of the switches' `Vds` field. **Check in Simulate** (Fixes tab) re-runs a stage with a snubber, edge time or spread applied in memory. A converter can list its snubber as `snubber: [C12, R12]` (parts on the board, also detected) or `snubber: {C: 2.2n, R: 1.5}` (proposed values, modelled as if fitted).
- **Fix suggestions** (`groundtruth suggest`, the Fixes tab) also cover power stages, with two kinds of candidate:
  - stitching vias: a rail-capacitor or switch pad with no via of its net within `stitch_distance_mm` gets a via of that net next to it, landing in the net's copper on the pad's layer and on another layer;
  - capacitors: 100 nF (0402 size) right at the switches, between the high side's rail pads and the low side's ground pads, each pad with a via to its plane. Use a part rated for the rail voltage.

  Each candidate's change of the hot-loop inductance and moment is predicted on the converter's model. Candidates are picked greedily: each round adds the one that helps most with the earlier picks in place, until the next adds less than 1 % or `--max-fixes` is reached. The picks are then verified together, and the comparison lists the hot-loop L and the radiated and conducted margins before and after. Capacitors are suggested, never placed automatically.
- **Detection:** `groundtruth detect` (or **Detect…** on the plugin's Converters tab) also suggests power stages. A net between an inductor and a switching transistor, a catch diode or an IC's SW/LX pin is a switch node. The transistor whose other power pin is on ground is the low side. The input connector is found through series parts (0R, fuses, ferrites, filter inductors, low-ohm resistors). A series switch such as a surge-stopper or reverse-polarity FET is recognised and crossed as closed. It must be a gate-driven FET whose drain and source sit on two supply nets, neither one ground nor an inductor's (a switching FET always touches its inductor). Detection then names it and gives it a `short` model, unless you already set a model for it yourself. Replace that with a `resistor` model of its R_DS(on) for the DC path. Switching frequency, load current, voltages and edge time cannot be read from the board: suggestions start with defaults to review.
- **Limits of the model:**
  - Only differential-mode emission is modelled. Common-mode emission (switch-node dV/dt through stray capacitance to a chassis or heatsink) often dominates above a few MHz.
  - The limit tables were entered from the standards as commonly published; check them against the edition your test plan cites.

### Motor bridges (BLDC drives, ESCs)

A motor bridge is a set of half-bridge legs on one bus: in each leg a high-side switch from the bus to the phase node and a low-side switch from the node to ground. The phase node goes to the motor, whose winding is the inductor, so there is none on the board. The plugin's **Bridges** tab and the `bridges:` section of the settings file hold them; `groundtruth detect` (or **Detect…**, and the first open of a board) finds them:

- a leg is a node with a transistor to ground and one to a non-ground bus, and no inductor on the node or behind a 0R, shunt or ferrite (that would be a converter's switch node); a FET pair without a FET to ground (a load switch, an ORing FET) is no leg;
- legs on one bus and ground form a bridge; each leg gets its phase (from the motor connector's pins, J1.A, or the node's name, PH_A), its gate driver (the IC with a SW / SHx pin on the node) and the driver's input nets (where the PWM comes from), its RC snubber, and the motor connector pin;
- the bus input is found through series parts, with current-sense ICs (pins IP+ / IP-) and ideal-diode or reverse-polarity FETs taken as closed (suggested `short` models; a sensor's model joins only its IP+ and IP- pins: `pins: [IP+, IP-]`), or as two wire tabs (an ESC's battery leads, the minus one behind its shunt).

What you set (Detect cannot read it from the board): the bus voltage, PWM frequency, phase current (the mean current of the conducting phases; `mode: motoring | generating` gives its direction), speed (0 for a locked rotor), the PWM scheme (`hpwm_lon`: the sourcing phase's high side chops, the sinking phase's low side stays on; `hon_lpwm`; `bipolar`), synchronous or not, the dead time and edge time, the switches' R_DS(on), reverse-conduction voltage, C_oss (all parallel switches together) and the motor: star or delta, line-to-line R and L, the back-EMF constant (line-to-line peak per 1000 rpm, as measured at the terminals) and pole pairs.

```yaml
bridges:
  - name: bridge J1
    rail: V_BLDC
    ground: GND
    legs:
      - {phase: A, switch_node: PH_A, high_side: [Q1, Q13], low_side: [Q2, Q14], output: J1.A, snubber: [C18, R82]}
      - {phase: B, ...}
      - {phase: C, ...}
    input_connector: [J5.1, J5.2]
    v_bus: 48
    f_pwm: 20kHz
    i_phase: 100
    mode: motoring
    speed_rpm: 3000
    pwm: hpwm_lon
    dead_time: 47ns
    rise_time: 2ns
    rds_on: 1.4m
    c_oss: 2nF
    q_oss: 170nC
    motor: {connection: star, r_ll: 20m, l_ll: 40µH, ke_ll: 5, pole_pairs: 7, emf: trapezoidal}
    sweep: {i_phase: [50, 100, 200], speed_rpm: [0, 1500, 3000]}
```

| Key of a `bridges:` entry | Default | Meaning |
|---|---|---|
| `name`, `rail`, `ground` | | The bridge, its bus net and ground. |
| `legs[]` | | The legs (six-step needs three): `phase`, `switch_node`, `high_side`, `low_side` (parts, parallel FETs as a list), `output` (motor connector pin), `driver` (gate driver IC, information), `inputs` (the driver's input nets), `snubber` (`[C, R]`). |
| `input_connector` | | `[plus, minus]` where the bus comes in (the LISN goes there). |
| `conducted` | `analysis.conducted` | This bridge's conducted standard. |
| `v_bus`, `i_phase`, `speed_rpm` | | Bus voltage, mean current of the conducting phase pair (positive), mechanical rpm (0: locked rotor). Needed to simulate. |
| `mode` | `motoring` | `motoring` or `generating`: the current's direction. |
| `f_pwm`, `pwm` | 20 kHz, `hpwm_lon` | PWM frequency and scheme (`hpwm_lon`, `hon_lpwm`, `bipolar`). |
| `synchronous` | true | The chopping leg's other switch conducts in the off time. |
| `dead_time`, `rise_time` | 50 ns, 2 ns | Before every turn-on; switch-node edge. |
| `d_max` | 0.98 | Largest duty (bootstrap refresh). |
| `rds_on`, `rds_on_low` | –, `rds_on` | R_DS(on) of one high-side / low-side FET. Without it the conduction is left out. |
| `v_sd` | 2 V | Reverse conduction of an off switch (GaN about 2 V, a Si body diode about 0.8 V). |
| `c_oss`, `q_oss`, `r_damp` | | As for a converter; `c_oss` and `q_oss` for one side's parallel switches together. |
| `c_stray` | | Phase node, cable and motor frame to chassis (common mode). |
| `motor` | | `connection` (`star`/`delta`), `r_ll` and `l_ll` (line to line, needed), `ke_ll` (V line-to-line peak per 1000 rpm) and `pole_pairs` (needed with a speed), `emf` (`trapezoidal` BLDC / `sinusoidal` PMSM), `advance_deg` (commutation advance, electrical degrees, default 0). |
| `sweep` | | `i_phase` and `speed_rpm` lists: extra operating points, every current at every speed, at most 64. |
| `source`, `reviewed`, `note` | `manual`, true | Set by detection. |

Simulate then:

- **drives the motor in six-step** (Hall commutation) at the operating point: the phase, switch and bus currents in steady state, with the PWM, the dead time, reverse conduction while a switch is off, and each commutation (the outgoing phase's current running out, back into the bus for a held-low phase). The duty is found so the phase current is reached; if the bus voltage cannot drive it at that speed, a `bridge_operating_point` issue says what is reached. Star and delta motors are the same at the terminals for a sinusoidal back-EMF; for a trapezoidal one the delta's circulating current is reported;
- **solves each leg's hot loop** (bus capacitors, high side, phase node, low side, ground) on one model of the bridge, at the leg's largest switched current: loop inductance, L·di/dt, ringing with C_oss (damped by the leg's RC snubber) and the radiated estimate, as for a power stage;
- **adds the legs at the input**: each leg's high-side current spectrum (exact, every electrical-frequency sideband of every PWM harmonic) times its share reaching the LISN, read by a simulated EMI receiver (its bandwidth, peak or average detector), against the conducted limit (`conducted_emission`);
- **shares the ripple among the bus capacitors**: each capacitor's ripple current (RMS) at the operating point.

- **counts the parts' losses** from the drive: each FET's conduction (R_DS(on), parallel FETs sharing the current), reverse conduction in the dead times (V_SD), the hard turn-on (overlap ½·V·I·t_r and the output charge Q_oss·V; `q_oss` and `c_oss` are one side's parallel switches together), the snubber resistors (½·C·ΔV² per swing), the bus capacitors' ESR and the series parts on the battery path (give a current sensor or ideal-diode FET a `resistor` model with its real resistance: a `short` gets no loss, only a note). Turn-off is taken as soft (GaN); the gate drive's loss is left out;
- **maps the RMS current density** from the battery to the motor: the phase current through each leg to its motor pin, the bus current from the battery, and the switching currents in the hot loops, as RMS values (what heats the copper), with each via's RMS current against its IPC-2221 capacity (`current_density`, `via_overload`);
- **sweeps operating points** if you list them: `sweep: {i_phase: [50, 100, 200], speed_rpm: [0, 1500, 3000]}` (or the Bridges tab's *Sweep* fields) evaluates every current at every speed from the same solves, a few seconds per point: duty (and whether the current is reached), losses, the busiest capacitor, the RMS density, the conducted and radiated margins.

On the Results tab a bridge's first view shows its drive (the phase and bus currents over one electrical period, with a commutation zoomed in), the capacitors' ripple currents, the conducted spectrum and the losses tile; its legs follow as power-stage views with their maps, then **RMS currents** (the RMS current density map, copper loss, busiest via) and **sweep** (losses, RMS density, conducted and radiated margin against the phase current, one line per speed; a hollow marker where the current is not reached). **Thermal** and **Current capacity** include the bridge: its RMS copper heat and its parts' losses; the capacity is the phase current at which the hottest copper or FET junction reaches the limit. **Suggest fixes** offers each leg's hot-loop fixes (stitching vias, a capacitor at the switches) for the leg's and the bridge's issues. Not yet: H-bridges (two legs).

### What a bridge or a power stage does to the rest of the board (victims)

A motor controller that resets under load is often not short of supply but hit by its own bridge: a ground that moves
between the microcontroller and what drives its pins, a reset or PWM line picking up the hot loop's field, a track
facing a phase node across a stack's gap. When a bridge or a power stage is simulated, GroundTruth also looks at
the **victims**:

- the board's microcontrollers and reset supervisors (by their value: STM32, GD32, ATSAM, PIC, TCM809, MAX809 …);
- its reset, crystal, Hall, debug and PWM / gate-input nets (by name), when they are routed;
- or the ones listed in the settings (`victims: parts:` and `nets:`, which replace the automatic choice).

Each leg's (each stage's) hot loop is solved once more on a model that also holds the victims, at the edge's
frequencies, and the bridge's drive view (the stage's part of the report) gives, at the worst leg's switching edges
(the stage's: its pulse current over its edge, the switch node swinging to its input voltage):

| Result | What it is | Threshold (settings) |
|---|---|---|
| **noise** at each victim net's receiver | The net is in the model, driven at its driver pin (a pull-up drives with its own value, else `drive_ohms`, 50 Ω) and loaded at the receiver pin by `input_pf` (5 pF), each to its local ground. The voltage across the receiver's input: its ground moving against the driver's, plus what the hot loop induces along the track and across any connector pins it crosses. As a coupling inductance M, the peak is M·I/t_r. | `induced_warning_v`, 0.3 V |
| **injected** current | A victim track facing a phase node's copper on another layer with no copper layer between them (a logic board's bottom layer over the bridge's pours) has the capacitance ε0·εr·A/h to it and takes C·V_bus/t_r at every edge. | `injected_warning_ma`, 1 mA |
| **ground** of a victim part | Its ground pins' potential against the reference pin (the bridge's input minus), as an inductance. Information only: a part's ground alone does not upset it. Set `reference:` to a pin it takes signals or supply from to have it checked. | `shift_warning_v`, 0.3 V |

Values over their threshold raise `victim_coupling` issues. A high-impedance net (a reset with a 10 kΩ pull-up)
sees little of what is induced in its loop, since most of it drops across the pull-up, but takes all of an injected
current; a low-impedance one the other way round. `victims: {enabled: false}` skips all of it (it costs one more
model per leg or stage). On the discrete buck test board (12 V, 3 A, 2 ns edges) a reset line 2 mm from the input
pour, pulled up by 10 kΩ, sees 0.15 mV at the microcontroller's pin; routed away in the far corner, less than half
the coupling (tests/test_stage_victims.py).

**Stacked boards (experimental, on hold).** An ESC built as a power board with a logic board above it is one model
when the power board's settings list the other board. This is limited: one other board only, the bridge must be on
this board, it is set in the settings file only (no plugin form), and Thermal, drawing and the 3D view do not cover
the other board. Stacks with more boards (a leg per board, the bus capacitors on their own board) are not supported;
work on stacks is paused.

```yaml
stack:
  - name: logic                  # its parts and nets become logic:U8, logic:/CPU/NRST
    file: ../logic/logic.kicad_pcb   # relative to this board's folder
    gap: 3.2mm                   # between the two boards' facing surfaces
    connectors: [[J7, J1]]       # [this board's connector, the other board's], pins joined by number
    side: above                  # or below
    flip: auto                   # auto | no | yes: the other board turned over (its top facing this board)
victims:
  nets: [logic:/CPU/NRST, logic:/CPU/PWM_AH]   # optional: otherwise found
  drive_ohms: 50
  input_pf: 5
```

The other board is placed from the connectors' pins (the rotation and offset that lay its pins on ours; the run's
notes say how well they line up). Its copper sits at its height, air in the gap; every joined pin becomes a vertical
conductor through the gap, so a ground or a PWM line that crosses on a few pins carries their inductance, and the
nets a pin joins become one net. Nothing is written to either board.

Limits: the edges are taken as the leg's largest hard-switched current over the edge time (the same as L·di/dt);
the noise is a peak estimate at the hot-loop frequency, not a waveform; the switch node's ringing, the motor cable
and anything off the boards are not in it; what the microcontroller or its supervisor tolerate is yours to set as
thresholds (a reset pin's glitch filter, a supervisor's threshold).

### Detection labels (`GT` field)

Detection guesses converters and motor bridges from reference designators, pin names and connections. It gets
most boards right; where it misses a stage, takes the wrong parts, or picks the wrong connector, you can say in
the schematic what a part is. Add a field named **`GT`** to the symbol (Symbol Properties → add field, or many
symbols at once in **Tools → Edit Symbol Fields**), hide it if you like, and update the PCB from the schematic: the
field travels to the board like an MPN does, and both the board file and the live KiCad API carry it. Labels are
optional, change no net name, and boards without them are detected as before.

| Label | On which part |
|---|---|
| `conv_hs` / `conv_ls` | a converter's high-side / low-side switch (each of parallel FETs); a catch diode is `conv_ls` |
| `conv_sw` | a converter IC with its switches inside |
| `conv_L` | the converter's power inductor (found on the switches' net when not labelled) |
| `conv_in` / `conv_out` | the converter's input / output connector |
| `hb_hs` / `hb_ls` | a motor-bridge leg's high-side / low-side switch |
| `hb_out` | the motor connector (or each phase's wire tab) |
| `hb_in` | the bus input connector (both battery tabs when + and - are separate) |
| `ignore` | any part detection should never take as a power-stage part |
| `sig` | the Signal paths tab: every net of this 2-pin part is a signal |
| `sig:9`, `sig:PA3` | the net on that pin (pad number or pin name) is a signal, even with a supply-like name |
| `sig_drv:5` / `sig_rx:9` | ... and this pin drives / receives it (Detect's driver and receivers are replaced) |
| `amp_in:3, amp_out:1` | an op-amp stage's input and output pin: the part becomes an `amplifier` the signal path runs through |
| `amp_inv:2` | its inverting input, where the schematic pin names don't say (+IN / −IN / OUT, +INA … are read by themselves) |

- **Which stage**: an id between the kind and the role ties parts together: `conv1_hs`, `conv1_ls`, `conv1_L`,
  `conv2_hs`, … (letters or digits: `conv5V_hs`); `conv_out_1` (id after the role) works too. A bridge leg's id is
  its phase: `hbA_hs`, `hbB_ls` (A-C, U-W or 1-3).
- **Reused sheets**: a sheet used several times (one per phase, one per rail) has the same field values in every
  copy, since KiCad keeps only the reference per copy. GroundTruth reads each part's sheet copy from the board, so
  `hb_hs` / `hb_ls` in `phase.kicad_sch` used three times make three legs, named and phased after their sheets
  ("Phase A", "Phase B", "Phase C").
- **Shared parts**: one connector feeding several converters: `conv_in` (every converter it reaches) or
  `conv1_in, conv2_in`. Several labels on one part are separated by commas. Bus capacitors need no label: every
  capacitor on the rail is part of the solve.
- **Feedback**: `groundtruth detect` prints "GT labels on this board: …" with every problem (an unknown label, a
  converter with a high side but no low side labelled, labelled switches that share no net). In the plugin, the
  Converters and Bridges tabs show the same line at the bottom; **Show** opens the tab's explanation. A labelled
  stage's first reason reads "labelled (GT): Q1 hs, Q2 ls, L1 L".
- To try labels on a board without touching it: `python tools/dev/add_labels.py BOARD OUT.kicad_pcb Q1=hb_hs
  Q2=hb_ls J1=hb_out` writes a copy with the fields set.

Why a symbol field and not net labels: a label such as `hb_A` would rename the net (and a stage's roles belong to
parts, not nets: which FET is the high side). KiCad's component classes and net classes are meant for design rules.
A field is the one place that reaches the board unchanged, with one value per part.

## CLI

```sh
groundtruth init board.kicad_pcb                 # create/update the settings file
groundtruth detect board.kicad_pcb --write       # suggest nets (driver/receivers/timing), power stages and motor bridges for review
groundtruth info board.kicad_pcb                 # stackup, counts, parts missing model values
groundtruth check board.kicad_pcb                # Tier 1
groundtruth simulate board.kicad_pcb -v          # Tier 1 + Tier 2, power stages and motor bridges
groundtruth suggest board.kicad_pcb --write      # propose, predict and verify fixes; store them as what-if edits
groundtruth compare board.kicad_pcb              # the board with and without the enabled what-if edits
groundtruth simulate board.kicad_pcb --history   # also keep the run in groundtruth/history/
groundtruth history board.kicad_pcb [--compare 1 [2]]   # the runs kept (1 = the newest), or two compared
groundtruth bode board.kicad_pcb [--nets N ...] [--from 1Hz --to 10MHz --points 400] [--json] [--plots]
                                  # the signal paths' Bode sweeps alone (seconds, no field solve): gain, -3 dB,
                                  # poles and zeros; groundtruth-bode.json, a Bode and a pole-zero PNG per path
```

Every command takes `--settings FILE` (default `groundtruth/<board>.groundtruth.yaml` next to the board); `groundtruth --version` prints the version.
`init --force` overwrites an existing settings file instead of merging into it, and `init --refill-zones` refills the
zones first (as below). `detect --min-score 0|1|2|3` sets how sure a net suggestion must be: 3 only clearly fast
signals (SPI clock, RMII, USB), 2 likely fast ones (default), 1 moderate, 0 all; `detect --write` adds the new
suggestions to the settings file, unreviewed.

These options apply to `check` and `simulate`:
- `--format text|json|junit|gitlab-codequality|html` (default `text`, on the console) can be given more than once. The files go to `--out-dir` (default `.`): `groundtruth-report.json`, `groundtruth-junit.xml`, `gl-code-quality-report.json`, `groundtruth-report.html`. With `simulate`, `html` also writes `heatmap_*.png`, `section_*.png`, `spectrum_*.png` and the 3D pages (`view3d_*.html`).
- `--fail-on info|warning|error` (default `error`) sets which severity fails the run. Waived issues never do.
- `--max-dbuv-margin DB` (simulate only) fails the run when an estimated emission (radiated, or a converter's or bridge's conducted emission) comes within DB of the limit.
- `--baseline FILE` accepts the issues listed in that file (by fingerprint; they are marked waived with the reason "baseline"; a missing file accepts none); `--write-baseline FILE` writes the current issues, waived ones left out, to a new baseline.
- `--refill-zones` refills the zones on a temporary copy of the board with `kicad-cli` before analysing it.
- `--nets ...` limits the analysis to the named nets, converters and bridges (or one bridge leg). Converters and bridges are analysed by `simulate` only.
- `--what-if` analyses a copy of the board with the settings file's enabled what-if edits. Never use it to gate CI: CI must judge the real board.
- `-q` prints less; `-v` (simulate) shows the solver's progress.

`simulate`, `suggest` and `compare` also take `--gpu auto|on|off` (see "GPU acceleration") and `--resolution N` (every model gets N = 1, 2, 4, 8, 16, 20 or 25 times the nodes; see "Resolution") and `--all-cores` (every physical core, several frequencies per refinement round: faster on big machines, figures can differ slightly between machines; Preferences "All cores"). `simulate --thermal` adds the thermal solve and `--capacity` the current capacity (see "Temperatures"); `simulate --history` keeps the run (see "Simulation history"). `history` lists a board's runs, newest first, with what changed since the run before; `history --compare RUN [RUN]` compares two runs, or one against the run before it (a run is its number in the list, its id or the start of it), and `--json` prints JSON. `suggest` takes `--max-fixes` (per net, default 3), `--candidates` (positions tried per issue, default 3), `--no-verify` (skip the combined verification run), `--verify-all` (simulate every net and power stage again in it), `--write` (store the suggestions as what-if edits, the selected ones enabled) and `--format json|html` (`groundtruth-suggestions.json` / `.html`); `compare` takes `--format json` (`groundtruth-compare.json`). Both take `--nets`, `--out-dir` (default `.`), `-q` and `-v`.

The exit code is **0** when the run passes, **1** when there are issues at or above `--fail-on` (or the EMI margin gate fails), and **2** for a configuration or parse error. `init`, `detect`, `info`, `suggest`, `compare` and `history` return 0 or 2.

Issue fingerprints are built from the check, the net and the offending items' KiCad UUIDs, so they stay the same when unrelated parts of the board change.

Example boards to try (an op-amp stage in a signal path, series parts across nets, an RC filter, a termination),
each ready to simulate with its settings file: [examples/](examples/README.md). For a GitLab pipeline, see
[examples/gitlab-ci.yml](examples/gitlab-ci.yml). It sets up Tier 1 on every merge request (issues show in the Code Quality widget and the Tests tab) and the simulation with its HTML report on the main branch.

## Fix suggestions and what-if edits

`groundtruth suggest` (or **Suggest fixes** on the plugin's Fixes tab) works through the issues it finds:

| Issue | Suggested fix |
|---|---|
| Layer change, same reference net, no stitching via | a stitching via of that net next to the signal via |
| Layer change between different nets, no decap | a capacitor between the two nets next to the signal via |
| Trace crossing a split between two nets | a bridge capacitor across the split next to the crossing |
| Trace crossing a slot in its own plane | a copper fill that closes the slot |
| Return detour, plane resonance or EMI margin | stitching vias at the route's layer changes, plus advice |
| Off-plane, edge proximity, non-ground reference | advice text only |

1. **Placement:** candidates are placed only where they would be legal. They must lie inside the board outline, keep 0.2 mm from other nets' tracks, pads and vias, and land on the right plane copper. Other nets' zones are not obstacles, because zones refill around a new via.
2. **Prediction:** each candidate's change in loop inductance comes from the route model at the fundamental. Its change in loop moment at the worst harmonic includes the plane-capacitance model, so a fix that damps or moves a plane resonance is judged where it matters. A via or capacitor only adds a branch to the models, so the prediction reuses the baseline factorisations and starts from the baseline solution.
3. **Selection:** the best fix per issue is kept, up to `--max-fixes` per net. A fix predicted to raise the emission by more than 1 dB is never selected, even when it would satisfy a rule. That happens, for example, when a stitching via moves a plane resonance onto a harmonic. Plane resonances are shared by the whole board, so each selected fix is checked again at every harmonic within 20 dB of the warning level, on every net. A fix is dropped when it lowers its own net's worst margin by more than 1 dB. It is also dropped when it lowers another net's worst margin by more than 1 dB and either that loss is bigger than its own gain or the other net falls below the warning level. Smaller trade-offs are kept, and the note says so (for example "worst margin +3.6 → +11.4 dB; also lowers /CPU/SPI_SCK's worst margin +10.8 → +9.2 dB"). Unselected candidates stay in the list with a note.
4. **Verification:** the selected fixes are applied to a copy of the board, and the analysis runs again for everything they can change: the nets and power stages that got a fix, every net within 20 dB of the warning level (a via or capacitor anywhere on the board can move a plane resonance onto its harmonics), and every power stage with a fix inside its model. That gives before/after numbers and lists the issues that are fixed or new. Nets far below the limit and power stages no fix touches keep their results; their rule checks run again on the modified board, and the comparison lists them as "not simulated again". `--verify-all` simulates everything again.

`--write` stores the suggestions in the settings file under `modifications:`, with the selected ones enabled and the prediction in the note. You can also write the entries by hand:

```yaml
modifications:
  - {type: via, at: [121.0, 122.6], net: GND, size: 0.6, drill: 0.3, enabled: true}
  - {type: cap, at: [118.4, 120.1], nets: [GND, +3V3], layer: F.Cu, C: 100n, ESR: 0.02, ESL: 0.4n, enabled: true}
  - {type: fill, layer: In2.Cu, net: GND, polygon: [[110, 120], [114, 120], [114, 123], [110, 123]], enabled: false}
```

Positions are in mm. A via's `size` and `drill` default to 0.6 and 0.3 mm, and `layers: [first, last]` makes it a
blind or buried via (default: through). A capacitor defaults to `layer: F.Cu`, 100 nF, ESR 0.02 Ω and ESL 0.4 nH,
with `angle` its rotation in degrees. A fill needs at least three polygon points. `enabled` defaults to true;
`source: suggested` and `note` are written by `suggest --write`.

What-if edits are applied only by `compare`, `check/simulate --what-if` and the plugin's **with what-if edits** switch. They become ordinary board items on a copy (a via, a two-pad capacitor with its own model, a zone), so every check treats them like real copper. `groundtruth compare` prints per route the loop inductance ratio, the moment, the worst margin and the plane resonance, before and after, plus the issues fixed and introduced. The worst margins are compared at the same harmonics: each run picks its own harmonics to solve, so both runs are also solved at the other's worst harmonic. When that changes a value, the run's own figure is shown in brackets.

On the plugin's **Fixes** tab you can:
- tick suggestions and draw them on `User.8` as crosses with labels,
- place the ticked vias in KiCad as real vias in one undo step, after a confirmation, then refill the zones and run DRC,
- add them to the settings file's what-if edits,
- compare the board with and without the enabled edits.

Capacitors and fills are not placed automatically: a capacitor belongs in the schematic, and a fill changes zone priorities.

## Showing an issue on the board (plugin)

Click an issue in the **Issues** tab and KiCad shows the copper it is about: the copper is selected, its net is highlighted (everything else dimmed) and the view zooms to it. Planes that matter are selected too, but without pulling the zoom out to them, and for plane issues the plane's layer becomes the active one, so a slot or an edge is drawn on top. This changes nothing in the design: no undo step, and the board stays unmodified.

| Issue | Shown |
|---|---|
| Gap crossing | The trace segments over the gap, plus the plane(s) on both sides of it (two nets for a split plane). |
| Off plane / edge proximity / other net's plane | The stretch of trace concerned, plus the plane that stops near it or carries the return. |
| No reference / no plane at all | Every segment of the net on that layer. |
| Layer change without a stitch or decap | The signal via. The nearest stitching via or bridging capacitor is in the zoom when it is close, and only selected when it is far. |
| Missing route | Both pins and the copper already on the net. |
| Simulation issues | The whole route and its pins. For a detour, also the via or part carrying most of the return. For a split plane, both return pins. |
| Power stages | The switches, the capacitors closing the hot loop and the switch-node copper (only the switch node is highlighted, never the rail or ground). For conducted emission, the input connector instead of the capacitors and switch node. For a current-density hotspot, the copper at the hotspot. |

Only the issue's own signal net (for power stages, the switch node) is highlighted. Stitching vias, return pins and planes are selected without lighting up their ground.

**Settings and status.**
- The status bar says what was shown, or that the copper is no longer on the board (re-run the checks after editing).
- **Zoom to it** and **Highlight its net** turn those steps off.
- **Show in KiCad**, a double-click or Enter shows the selected issue again after you moved the view in KiCad.

**What gets restored.**
- A net highlight you set yourself in KiCad stays until an issue brings its own. A highlight GroundTruth set is cleared by the next issue that has none, by `~` in KiCad, and when the GroundTruth window closes.
- Your active layer comes back with the next issue that has no layer of its own, and when the window closes.

**Limits.**
- A click while GroundTruth is drawing or reading in KiCad is not queued (it would move your view later): the status bar asks you to click again.
- In file mode (`groundtruth-gui`), a click only says in the status bar what the issue is about and where.
- The highlight uses KiCad's own editor actions, which KiCad does not promise to keep. A KiCad version without them still gets the selection, and the status bar names the missing action.

**Signal paths and power stages.** The **Signal paths** and **Converters** tabs work the same way. Clicking a net highlights it in KiCad: all its pads, tracks and vias are selected and lit, and its pours are selected. Clicking a power stage shows its switches and capacitors (once simulated) and highlights its whole power flow: the input path, the rail, the switch node, the inductor's output side and the output path to the output connector. Ground is never highlighted, because it would light up the whole board. Ctrl- or Shift-click selects several rows, and they are shown together. Deselecting every row clears GroundTruth's highlight. **Zoom KiCad to the selected nets / power stages** (under each table) turns the zoom off. In both tables, several selected rows can be removed at once with **Remove selected** or the Delete key, or marked reviewed with **Mark reviewed**; Ctrl+A selects all rows. **Update selected** edits one row at a time. The tick box at the start of a row (Nets, Converters and Bridges) switches it off without losing anything you entered: it is saved as `enabled: false`, shown greyed, and left out of Run checks, Simulate and the fix search (and not checked against the board). On the command line a switched-off row runs only when `--nets` names it, and the run lists the rows it left out.

**Export report.** **Export report…** in the top bar writes the results shown (the last Run checks or Simulate, the thermal and capacity views, the last fix search) as one self-contained HTML page: a cover, a summary with key figures, the main findings (one line per check: its counts, its worst case, a link to its rows) and every net, stage and bridge at a glance (operating point, margins, overshoot, efficiency), the board as an interactive view in the manner of InteractiveHtmlBom (front/back, a copper layer's pours and tracks, the parts' pads, outlines and references, the analysed routes and stages, every issue as a numbered marker; pan, zoom, a parts list that finds a part on the board), the issues (filter and sort in the browser), every net, power stage and motor bridge with its figures and tables, temperatures, fixes, and the method and limits. It prints as an A4 document (the browser's Print → PDF). The cover's lines come from the optional `report: {title, company, author, document}` section of the settings file; where it names no author or company, the plugin takes them from **Preferences…**. The CLI's `--format html` writes the same page.

**Simulation history.** Every **Simulate** in the plugin is kept in the board's `groundtruth/` folder, so earlier results stay at hand after the board or the settings changed:

```
groundtruth/history/
  boards/<YYYY-MM-DD_HH-MM-SS>.kicad_pcb.gz
                                   each distinct board once, zipped, named after the run that first kept it
                                   (later runs on the unchanged board use that copy)
  runs/<YYYY-MM-DD_HH-MM-SS>/
    run.json                       when and how it ran (resolution, what-if edits, time taken), which board and
                                   settings, the headline figures, your note
    results.json                   the results, as `simulate --format json`
    settings.groundtruth.yaml      the settings file as the run used it
    report.html                    the report page, as Export report writes it (made in the background
                                   while the window is left alone: its plots would make it stutter)
```

In KiCad the board copy is the open board with any edits not saved yet (a read-only request to KiCad; if KiCad cannot hand it over, the saved file, and the run says so). The **History** tab lists the runs, newest first, with what changed since the run before (board, settings, resolution, what-if), the errors, warnings and worst margins; it keeps a note per run, compares two runs (margins and loop inductance per net, stage and bridge, the issues fixed and new, the settings lines that changed), opens a run's report and board copy, and deletes runs with the board copies no other run needs. **Preferences…** turns keeping runs, or just their report page, off. On a 6-layer, 200-part board a run takes about 0.2 MB of results and 2.9 MB of report, plus 1 MB for the board copy when the board changed. The folder holds copies of the board and its results: share or check it in as you would the board. A board copy is unpacked only after it matches the SHA-256 its run recorded. Two boards in one folder share `groundtruth/history/`: each run records its board, and each board lists only its own runs. The CLI keeps a run with `simulate --history` and reads the folder with `groundtruth history`.

**Help.** **Help** in the top bar (or F1) opens the plugin's own help at the tab you are on: the workflow, what Simulate does with the nets, converters and bridges listed and how their results come together, every tab, Thermal, drawing, the detection labels, the settings file, speed and memory, and the limits of the numbers (`src/groundtruth/ui/help.html`; keep it current with the features).

**Look.** The window follows the Windows app setting, light or dark (PCB Thermal's palette), and **Theme** in **Preferences…** picks one for good (it applies the next time the window opens). The buttons are the plugin's own, with some volume; **Simulate** is the main action.

**Results tab layout.** The plots sit on a page that scrolls, each as wide as the window: the current maps first (they fill the window's height, or less for a wide board), then the cross-section along the route (with a caption under it that explains its plots: each layer's share of the return, and the green bars, the vias and parts that carry the return from one layer to another), then the emission spectrum. The mouse wheel scrolls the page; Ctrl + wheel over a map zooms it. The controls, the KPI tiles and the toolbar stay at the top. The plots' titles, labels and legends read at the size of the window's own text (9 to 10 pt; tick labels 8.5 pt, the bold labels on the maps 8 pt).

**KPI tiles.** The Results tab shows each view's key numbers as tiles, coloured by your rules: margins (red below 0 dB, amber within `rules.emi_margin_db`), overshoot, current density, via load. Hover over a tile for what it means. Moving the mouse over a map shows the value under it (A/m per A, A/mm² or °C), the layer and the position, next to the toolbar; over an issue's circle the issue (severity, check, message; in full as the map's tooltip), over a via's or part's marker its name and share of the current.

**Preferences.** **Preferences…** (top bar, next to Help) holds the user's settings on this computer, for every board, in `%LOCALAPPDATA%\GroundTruth\ui.json`: **Resolution** (below), **Solver threads**, **Use every core** (`--all-cores` on the CLI), **GPU** and the **Thermal quality** (each by default *From the settings file*: the board's `analysis.threads`, `analysis.gpu`, `thermal.quality`), **Via markers**, **Vias shown**, **Part outlines**, **Part names**, the report's **Author** and **Company** (where the board's `report:` names none), the **Theme**, whether every Simulate is kept in the board's history (and with its report page; see "Simulation history"), the **Compute server** (its address and key, **Test connection**, **Find servers**, and **Servers** with **Update** / **Update all**; see [Compute server](#compute-server-optional)), and under *Drawing on the board* the arrow line widths (**Thinnest / Thickest arrow line**, 0.01 / 0.3 mm) and the contour line widths (**Contour line** / **Index contour line**, 0.04 / 0.1 mm). OK keeps them and redraws the maps at once. The working toggles (Current arrows, Log scale, Cross-section, Zoom to it, Highlight its net) keep their last state there too.

**Resolution.** Simulate's grid on this computer: every model (routes, power stages, bridges, the board-wide plane model) gets 1, 2, 4, 8, 16, 20 or 25 times the nodes and cells 1/√ of that in size, also where the node budget does not bind (the cell starts at the dielectric height / √N; `analysis.resolution`, never written to the settings file). After a Simulate each level in Preferences shows its cell size, e.g. "4× (0.21 × 0.21 mm)", the coarsest model's. Memory and time grow about as much as the nodes: 2× to 4× on a laptop, 8× and up on a machine with lots of memory; a solve that runs out of memory (SuperLU's allocation too) is retried alone, then reported. The standard grid is converged for the numbers; finer grids sharpen the maps. **Sharper map** halves the cell of what was simulated (4× its nodes). The CLI: `groundtruth simulate|suggest|compare --resolution N`; the report names it.

**Part outlines and names.** **Part outlines** (Preferences) draws every part's outline over the maps: its courtyard (F/B.CrtYd), else its fabrication outline, else a box around its pads (live KiCad mode reads no outlines yet, so the boxes); top-side parts solid, bottom-side dashed. **Part names** writes each part's reference at its middle, with or without the outlines (on a busy map the names can crowd it). Preferences for every board, like **Vias shown**; the exported report follows them.

**Via loading.** Every via on a power stage's DC path is checked against its IPC-2221 capacity for a 10 K rise, from its drill and the plating (`analysis.via_plating_um`, default 25 µm): `via_overload` warns at `rules.via_load_warning` (100 %) and is an error at `rules.via_load_error` (150 %, about a 25 K rise). IPC-2221 is conservative; the thermal solve gives the actual temperature. A part's plated hole is not checked: its soldered pin carries the current between the layers, so the solver gives it the resistance of the hole's whole cross-section, not of the plating (a DNP part's hole stays plating only).

**Sharper map.** Each pixel of a heatmap is one cell of the solver's grid (0.35 mm for kraft_flux_mini_2's SW1 hot loop). **Sharper map** solves the shown view again with half the cell size (4x the nodes) in the background, about 20 s for a route and up to a minute for a power stage, and shows the result in its place. The numbers in the summary line stay as analysed: the default grid is already converged for them (SW1, 2026-10-18: 1.57 vs 1.53 nH, -19.8 vs -20.8 dB radiated margin at 4x the nodes), so the finer grid sharpens the picture, not the results.

**Larger heatmaps.** The layer maps are laid out to be as large as the region allows: side by side for a tall, narrow region, in a grid for a square or wide one. Zooming or panning one map (toolbar) shows the same spot on every layer.

**What carries the current.** On every heatmap, a green ▲ marks a via and a green ■ a part that carries the current to another layer or net: every one with at least 1 % of the current (of a DC path's or a bridge's RMS current, or of a route's return), with its share next to it from 5 % up ("67 %"). A carrying via is also drawn at its true size (pad ring and drill), which shows when you zoom in. **Via markers** (Preferences) unticked hides them all (parts' ■ and issue circles stay); **Vias shown** limits them to the 5, 10, 20 or 50 busiest; both are a preference for every board, kept with the theme in `%LOCALAPPDATA%\GroundTruth\ui.json`, which the exported report follows too. The checks (current density, via overload) look at every via whatever is shown. The cross-section's side view draws the eight biggest. Open circles mark issues on the net (orange: warning, red: error).

**Plane displacement.** With `analysis.plane_capacitance` on, current also crosses between two facing planes through their capacitance. That current is spread over the whole plane pair and comes from the board-wide model at the worst harmonic, so it has no marker on the map; the tile **plane displacement** gives it (e.g. "12.9× signal, In1.Cu–In2.Cu @ 1450 MHz"), and so do the report's transfer list and the text output. Above 1× it is a resonance: the plane capacitance and the vias joining the planes form a tank, and a current several times the signal current circulates through both, in antiphase (they still add up to what has to cross, `tools/dev/transfer_check.py` shows it frequency by frequency). On the demo board the two GND planes, joined only at the ICs, resonate at 1.45 GHz; on kraft_flux_mini_2 the GND pour on F.Cu and the +3V3 plane on In1 by C1105 and C1110 resonate at 1020 MHz (at 2× and 4× resolution; 940 MHz at 1×) with 4.6× the signal current across them. The model has no radiation loss, so the height of such a peak is an upper estimate. **Main transfer** is always the via or part carrying most of the return.

**Cross-section.** Below the maps, a signal route's return current along the route, layer by layer, driver on the left. Top: the share of the return each plane layer carries past each point of the route within ±20 dielectric heights of the trace (1 to 5 mm), and their sum (dashed, Σ): where the sum dips, the return has left the route. Bottom: a side view of the copper layers, each plane coloured by its share (red: flowing back as the return, blue: flowing with the signal), the signal on its layers with its layer changes, and the vias and parts carrying the return between layers as green bars with their share (dashed with their distance when they are far from the route). It shows at a glance whether the return follows the signal from plane to plane: with a stitching via next to a layer change it swaps planes right there; without one it crosses over gradually and part of it stays on the plane the signal has left. Shares are the part in phase with the signal. The mouse over it gives every layer's share at that point. Untick **Cross-section** to leave it out. Power stages have no route, so no cross-section; the HTML report has it for each net (and `--format html` also writes `section_<net>_<receiver>.png`).

**3D view.** **Open 3D view** (Results tab) shows the selected view's layers stacked in 3D in your web browser: each plane layer's current map with its arrows (the heatmap's colours, more transparent where less current flows), the board outline, the signal on its layers with its layer changes (grey bars), and the vias and parts carrying the current between layers (green bars with their share). Drag to turn and tilt, wheel to zoom, double-click to reset; sliders set the layer spacing (exaggerated: a board is a millimetre thick) and the maps' opacity, and checkboxes hide layers. With plane capacitance there is a second view, **Board-wide model**: that model's plane currents at the worst harmonic, the displacement current between each pair of planes (red: downwards, blue: upwards) and the vias' currents, so a "plane displacement 12.9× signal" can be seen circulating between the planes and the vias that join them. The page is built in the background, written to `%LOCALAPPDATA%\GroundTruth\views\view3d_<net>_<receiver>.html` (overwritten for the same view) and needs no internet: everything is in the file, and its content policy lets it load nothing else. It holds the board's maps and names: share it like the board. `--format html` writes the same pages next to the report and links them (add `#board` to open the board-wide view).

**Wheel zoom.** Hold Ctrl and turn the mouse wheel over a map to zoom in or out about the point under the mouse (in the **Open in window** window the wheel alone zooms); all layers follow. Each zoom is drawn again (in the background, so the window keeps responding) with the current arrows laid out for what is in view: zooming in shows the flow in more detail, not the same arrows larger. The colours keep the whole map's scale. Double-click a map, or press the toolbar's Home button, to see the whole map again. Changing the layer keeps the zoom; another route starts with its whole map. The colour field itself is the solver's grid (one cell per node), so close up it shows the cells; **Sharper map** solves the view on a finer grid. **Open in window** shows the heatmap in a window of its own that you can enlarge or maximise; it follows the route, layer and arrows chosen in the tab.

**Drawing layers.** Markers, suggestions, contours and arrows go on User.9, User.8 and User.7 (settings: `drawing`). A drawing layer the board does not have yet is enabled on the board first, and the status line says so. That is a change to the board setup, which Clear GT drawings does not take back: disable the layer in Board Setup › Board Editor Layers if you want it gone. (Without it, KiCad would create the shapes invisibly and refuse to group them, and only Undo would remove them.)

**Unusual values are used as written** (your value is the value) and listed as the info issue `unusual_value`, with what to write if a unit is missing: a C_oss of `482` is read as 482 F (`482p`?), a rise time of `2` as 2 s (`2n`?), a switching frequency of `750` as 750 Hz (`750k`?). Nothing blocks a run; a stage with such a value gives results that follow from it.

**Parts follow KiCad.** While the **Parts** tab shows, clicking a part (or several) in KiCad's PCB editor selects its rows in the Parts table, with the cursor on its Model cell, so you can edit it right away. A filter that hides the part is cleared. The window checks KiCad's selection twice a second, and only while that tab shows.

**Long status messages** (a refused net's reason, for example) wrap onto up to three lines; the status bar grows for them, and the whole text is also in its tooltip.

**How far a job is.** While something runs (Simulate, Suggest fixes, Thermal, …), the status line reads "what it is doing · 37 % · 1:12 · about 2 min left": the elapsed time counts every second, also through a long single step, and the estimate appears after the first few percent and is smoothed so it does not jump. A job that took 10 s or more ends with "took m:ss". Simulate and the fix search split the bar by how much solver work each step is (a solve on a large model counts more than one on a small model), and their messages count the steps: "evaluating candidates: SPI_MOSI, add a GND stitching via (3 of 12)", "solve 7 of at most 24" (refinement can finish early, then the bar jumps ahead).

**GPU.** With PyTorch installed on a machine with an NVIDIA (or, on Linux, AMD) GPU, the solver's iterations run
there: see "GPU acceleration (optional)" under Install for what to install and how. The status line after Simulate
or Suggest fixes ends with "solved on GPU <name>" when the GPU did the work; a solve the GPU cannot finish (out of
its memory, say) is done on the CPU and counted there.

**Drain and source.** A power stage's heatmaps mark its switches' power pads: D (drain, cyan) and S (source, green) on a FET, K / A on a diode, VIN / SW / GND on an integrated stage, told from the pads' nets. The pads are outlined on their own layer; the letters and the part's name show on every layer, so you can see where a switch sits above an inner plane.

## Op-amp circuits and the transfer function

A signal path is solved as the circuit it is on the board (`analysis/analog_net.py`, `emi/mna.py`): every part with a model and every op-amp channel on its nets and beyond, up to ground and the supply rails, in any topology (inverting, non-inverting, differential, integrators, transimpedance stages, Sallen-Key / MFB / state-variable filters, instrumentation amplifiers, several op-amps with feedback between them). Every track is a transmission line of its own, every via R + L; an unrouted net is joined as the netlist says. An op-amp is its datasheet's small-signal model (Aol, GBW, a second pole or PM, R_O, Cin, Rin; pins from their names or GT labels); a `Gain` field keeps the old fixed gain block.

The Bode views (Simulate, the Signal paths tab's **Bode**, `groundtruth bode`) show the path's response, each op-amp's output and each op-amp's loop gain with its crossover and phase margin (a tile: amber below 45°, red below 20°), the exact poles and zeros, and the **transfer function**: in the part names for the ideal circuit (SymPy; e.g. a Sallen-Key's `1 / (C1·C2·R1·R2·s² + C2·(R1 + R2)·s + 1)`), and numerically for the full circuit in the normal form `K · Π(1 + s/ω) · Π(1 + s/(Q·ω) + s²/ω²) / […]` with each frequency and Q. Each response also has its **step response** (1 V at the driver: final value, 10-90 % rise, overshoot and undershoot, settling into ±2 %; a plot under the Bode plot, a `step` tile, `--plots` writes `<path>_step.png`). The JSON report's responses carry `transfer_function` (`numeric`, `symbolic`), `step` (its figures and the curve) and a loop gain's `phase_margin`. Simulate's emission estimate takes the path's currents from the same circuit. On kraft_flux_mini_2's ADC path (ZXCT1107 → TLV6001 follower → 4.7 kΩ / 100 nF → ADC): `H(s) = 1 / (C304·R303·s + 1)`, numerically K 0.9975, poles 451 Hz (the filter), 686 kHz (the follower), the loop's crossover 671 kHz with 89° of margin.

## Capacitance at DC bias

A class II ceramic (X7R, X5R …) loses capacitance as the DC voltage across it rises. A part's `Cbias` (schematic field, InvenTree parameter, or the Parts tab's **C at bias**) is its DC-bias curve: one value (`75nF`, taken at half the part's Rated Voltage), one point (`75nF@50V`: the curve C0 / (1 + (V/V1)²) through it) or 1-12 points (`95nF@10V, 66nF@30V, 39nF@50V, 20nF@80V`, or `-5%@10V, …`: a monotone cubic through them). InvenTree's numeric templates can't hold the `@`: use a text template **Cbias Curve** (it wins), or **Cbias** with **Cbias Voltage**. Every run gives each capacitor its capacitance at the worst DC voltage across it (`analysis/bias.py`): a net's voltage from its name, PDN rails, the power stages and bridges, the settings' `safety: voltages:`, through series parts; else at most the supply of a low-voltage IC on it (an op-amp's, an MCU's: ≤ 18 V), else at most a divider's top; else 0 V (the nominal C, named in a note). C0G / NP0, tantalum and electrolytics keep their C. The Parts tab's **C in circuit**, the report's *Capacitors at their DC bias* table and the JSON's `capacitors_at_bias` show what was used; the plugin's help (Cbias) has a worked chart.

## Temperatures (thermal solve)

**Thermal** (top bar), or `groundtruth simulate --thermal`, solves the copper and part temperatures of the power
stages after Simulate, with the solver of PCB Thermal (validated against analytic cases; `groundtruth.thermal`).
Motor bridges heat the board with their RMS currents and their parts' losses (see "Motor bridges").

- **Heat:** the power stages' DC paths, as Simulate computed them (switch node → inductor, input, output and
  ground returns, with the switching split of the currents), heat the copper: ρ(T)·|J|² with the paths' current
  vectors added where they share copper. The parts get their losses at the operating point: the switches'
  conduction (D·I²·R_DS(on), (1−D)·I²·R_DS(on); a catch diode (1−D)·I·V_f), the inductor's I²·DCR, the series
  parts on the DC paths (shunts, a surge stopper's FET), and switching on the hard-switched transistor (a buck's
  high side, a boost's low side):
  - **edges:** ½·V·I·t_edge·f_sw per hard edge. Synchronous (a FET rectifies, or an IC with both switches):
    only the turn-on edge, because at turn-off the inductor current carries the switch node across while the
    channel is already off. With a catch or boost diode: both edges.
  - **output charge:** Q_oss·V·f_sw synchronous (the switch dumps its own charge and charges the other FET's
    through itself: for two equal FETs together Q_oss·V per cycle); ½·Q_oss·V·f_sw with a diode (its own energy).
    Without `q_oss`, C_oss·V² stands in for Q_oss·V. For GaN use Q_oss: its capacitance falls steeply with
    voltage, so the small-signal C_oss at the bus voltage underestimates the loss (EPC2306 at 52.8 V, 750 kHz:
    1.5 W from Q_OSS 38 nC, 1.0 W from C_OSS 482 pF).
  - **dead times** (a synchronous rectifier FET): both switches are off before each turns on, one dead time per
    switch-node edge (`dead_rise`: low side off → high side on; `dead_fall`: the other; `dead_time` sets both), and
    the inductor current flows through the rectifier in reverse at `v_sd`. At the hard edge (the main switch's
    turn-on: a buck's rise) for the whole dead time, at the ripple's valley current; at the soft edge, at the peak,
    only after the current has swung the node across (both FETs' output charge 2·Q_oss). Loss V_SD·f_sw·(I_v·t_hard
    + I_p·max(t_soft − 2·Q_oss/I_p, 0)) in the rectifier, both dead times taken out of its channel's conduction time.
    With GaN (V_SD ≈ 1.5-2 V) often as much as the conduction loss. The dead times come from the settings or a GT
    field on the controller IC (`GT = dead:20ns`; per channel `dead1:20ns`; per edge `dead_rise:20ns`,
    `dead1_fall:10ns`; per labelled converter `conv1_dead:20ns`); without one 20 ns per edge (an adaptive
    controller's), with a note.
    `v_sd` from the rectifier FETs' `Vsd` field, else 2 V for GaN, 0.8 V otherwise.
  - **reverse recovery** (`q_rr`, a silicon FET's body diode or a PN catch diode; the FETs' `Qrr` field; GaN has
    none): Q_rr·V·f_sw in the main switch at its hard turn-on. The same charge is a current spike in the hot loop,
    I_rr = √(2·Q_rr·di/dt) over t_rr = √(2·Q_rr/(di/dt)) at di/dt = I/t_edge, added to the pulse's spectrum, and
    its snap-off an overshoot I_rr·√(L/C_oss) in the `hot_loop` issue.
- **Inputs** per power stage (Converters tab; hover a field for what it means): `rds_on`, `rds_on_low`, `dcr`,
  `diode_vf`, `q_oss` (and `c_oss`, which also sets the ringing), `dead_rise` / `dead_fall` (or `dead_time` for
  both), `v_sd`, `q_rr`. A missing value
  leaves that loss out, with a note (the dead time and V_SD take their defaults, also with a note). The `thermal:` settings section: `ambient_c` (25), `orientation`
  (horizontal / vertical), `convection` (natural / forced with `airspeed`, 1 m/s / fixed with `h_top` and
  `h_bottom`, 10 and 5 W/m²K), `radiation` (true), `quality` (draft / normal / fine; default normal), the limits
  `warn_c` (85) and `max_c` (105), `capacity_c` (see "Current capacity" below), `theta_jb` (part → K/W) for
  junction estimates, and the electro-thermal coupling below (`electrothermal`, `electrothermal_rounds`,
  `electrothermal_tolerance`).
- **Electro-thermal coupling** (`thermal.electrothermal: true`, or Preferences… → Electro-thermal; off by default):
  hot copper is more resistive (+0.393 %/K), so a hot via or track hands current to cooler ones beside it. Off,
  the DC paths keep their 20 °C current pattern and only the heat follows ρ(T). On, after the first thermal solve
  every power stage's and DC load's DC paths are solved again on the whole-board DC model with each copper cell and
  via barrel at its solved temperature, then Thermal again, until no path's loss and no via's share of its path's
  current moves by more than `electrothermal_tolerance` (1 %; also `"2%"`) in a round, for at most
  `electrothermal_rounds` rounds (3; 1-10, never more than 10). A round that changes more than the one before ends
  it with the round before kept; Cancel stops it. The notes say whether it settled and in how many rounds; the
  result (JSON `electrothermal`, the report's "Busiest vias at temperature", the plugin's tile) gives the busiest
  vias' currents at 20 °C and at temperature. Bridges' RMS currents are not re-solved, and Current capacity
  keeps the 20 °C current pattern.
- **Results:** a "Thermal: copper temperature" view in the Results tab (one map per copper layer, × the hot
  spot, tiles: hottest copper, total loss, hottest part), the hottest parts as bars against the limits, issues
  `thermal_hotspot` and `part_temperature` (parts without losses of their own are listed together, past
  `max_c`). **Draw contours on board** draws isotherms for this view.
- The board model is the one GroundTruth read (live from KiCad too), adapted to PCB Thermal's. Not in it:
  non-plated holes, copper graphics, solder-mask openings (all faces count as masked).
- Heat leaves only through the board's faces: part bodies, heatsinks, an enclosure and mounting are not modelled
  yet, so large parts come out on the hot (conservative) side.
- **Current capacity** (Results tab, **Current capacity**; `groundtruth simulate --capacity`): per power stage, the
  load current at which the hottest copper or part junction (where `theta_jb` is given) reaches
  `thermal.capacity_c` (default `max_c`), with the other stages at their operating points; with several stages
  also **all power stages** scaled together by one factor ("the board reaches 105 °C at 62 % of every load").
  Each result is a heatmap view at that current (tiles: the capacity, what limits it, the loss). The model is built
  twice, not per trial: a draft model finds where each capacity lies, then a model at `thermal.quality`, its mesh
  planned for the heat at those currents, settles it to 0.25 K. A stage whose neighbour alone already passes the
  limit shows 0 A: read the "all power stages" row then.
- The plugin solves in a process of its own and stops it (with a message) when it uses more than 60 % of the
  computer's memory, or when less than 5 % (0.5 GB) is left free: use `thermal.quality: draft` (or Thermal quality
  *draft* in **Preferences…**) then.

## On-board drawing (plugin)

All drawing happens in one undo step. The items are grouped in `groundtruth-*` groups, so **Clear GT drawings** removes exactly what the plugin drew. The layers can be changed under `drawing:` in the settings file.

- `User.9`: issue markers (circle plus number, severity and check).
- `User.8`: suggested fixes (cross plus id, kind and predicted change).
- `User.7`: a contour map (**Draw contours on board**) like a topographic map's height lines: lines at round values of the current density (1-2-5 steps over three decades below the peak; on a thermal view isotherms in °C), each decade drawn thicker, with the values written along the lines and a legend line above the board; and current arrows (**Draw arrows on board**), thicker where the current density is higher. Both for the route and layer selected in the Results tab.

**Current arrows.** Every heatmap (plugin, HTML report, CLI PNGs) carries small arrows, all the same size, that point the way the current flows. Their number shows the current: where it concentrates, the arrows crowd together; where little flows, only a few are scattered; idle copper has none. Like field lines, the arrow density follows the current density (square-root compressed, so a return current that falls off by decades beside its trace still shows its spread). All layers of a view share one scale, so a layer carrying little current also shows few arrows. Untick **Current arrows** in the Results tab to hide them. AC currents (return currents, hot loops) are drawn at the phase where they are largest, in the direction of the signal's own current; DC paths are drawn as they are. On the board the arrows are line segments on the current-drawing layer, removed by **Clear GT drawings** like everything else the plugin draws; there their line width shows the density too, from the thinnest width three decades below the peak to the thickest at the peak (Preferences).

**Click a marker.** After a Simulate, clicking a marker on a map (an issue's circle, an overloaded via, a via or part carrying current) shows it in KiCad: the via, pad or track at that point is selected and its net lit (not a ground net), as the Issues tab does for an issue. Each view marks only its own issues: a DC path's map shows that path's overloaded vias, not another path's.

**Log scale.** The heatmap colours span three decades below the peak, so even weak current spread over a whole plane shows (a return that has to detour to a far via, or cross between planes through their capacitance, fans out like field lines). Untick **Log scale** in the Results tab for a linear scale from 0 to the peak: it shows where most of the current flows. Temperature views are always linear. `tools/dev/stitch_contrast.py` draws the same route without and with a stitching via at its layer change, on both scales.

Vias placed from the Fixes tab are real design changes. They are not grouped, so **Clear GT drawings** leaves them alone.

## Layout

```
src/groundtruth/
  core/          board model (KiCad-independent), issues
  adapters/      file_source (.kicad_pcb parser), kipy_source / kipy_sink (IPC API, the only kipy users)
  geometry/      shapes, copper per layer/net
  components/    value parser, part models, default suggestions
  config/        settings file (round-trip YAML), what-if edits
  analysis/      route finder, detection (detect.py, GT labels tags.py), Tier 2 orchestration (simulate.py), power
                 stages (power.py), motor bridges (bridge.py, motor.py, bridge_rms.py), victims and stacks
                 (victims.py, stack.py), thermal solve (thermal.py), cross-section, Sharper map, fix suggestions
  rules/         Tier 1 checks
  solver/        PEEC kernels and solver, GPU backend (gpu.py)
  emi/           spectrum, radiation and CISPR 32 limits, conducted emission (LISNs, CISPR 32/25, MIL-STD-461),
                 spread spectrum, a motor drive's PWM line spectrum
  results/       current contours and arrows
  thermal/       PCB Thermal's solver
  report/        text/json/junit/codequality/html, plots, 3D view, simulation history (history.py)
  ui/            wxPython plugin window, Preferences, help.html
  ci/            zone refill with kicad-cli
plugin/          plugin.json, requirements.txt, entry scripts, icons, demo board
tools/           install_plugin.py, make_pcm_package.py, make_server_package.py, install_gpu.py, check_gpu.py,
                 make_demo.py, make_icons.py; dev/: developer scripts (see CLAUDE.md)
tests/           synthetic board generator + test-suite
```

## DC loads

A supply rail with the currents its loads draw (a connector feeding other boards, a fused output): the **DC loads**
tab, or `dc_loads:` in the settings (`{name, source: J1.2, source_return: J1.1, max_drop: 200mV, loads: [{pin: J2.2,
return: J2.1, current: 500mA}]}`). Simulate solves it on the whole board's copper: the drop each load sees (supply and
return), the current density and loss, the busiest vias (issues `current_density`, `via_overload`, `dc_drop`);
Thermal and Current capacity include it. In the tab, **Fill from net…** picks the rail's net and ground, then the
part feeding it and the parts drawing from it (each one's rail pin and ground pin found for you); typing in the Source
pin, Source return and Loads fields lists the board's pins with their nets (Ctrl+Space on an empty field), the
fitting ones first.

## Power integrity (PDN impedance)

A supply rail's impedance as its ICs see it between their supply and ground pins, from 10 kHz to 1 GHz: the decaps,
the planes' capacitance, the vias and the regulator together, solved on one model of the whole board. Below a target
impedance `Z_t = V · ripple / I_step` the rail holds its voltage within the ripple whatever the frequency of the IC's
current step. In the settings:

```yaml
pdn:
  - {rail: +3V3, i_step: 300mA}                     # ripple 5 %, the voltage from the name, the source found
  - {rail: VDD_CORE, voltage: 1.2V, ripple: 3%, i_step: 2A, source: U7, vrm_r: 2mΩ, vrm_l: 10nH, check_to: 50MHz}
```

`ground` (default the first reference net), `source` (the part feeding the rail: found as the part with a `power_out`
pin on it, else a connector; `none` for no source), `vrm_r` / `vrm_l` (its output resistance and inductance: a
regulator's flat output impedance and the inductance above its loop bandwidth; defaults 5 mΩ, 20 nH), `parts` (the
ICs to check; default every part with `power_in` pins on the rail and a pad on the ground), `check_to` (the target
applies up to here, default 100 MHz: above, the package and the die decouple), `decap_mm` (default 3 mm). Simulate
gives per IC |Z(f)|, which part holds the rail at each frequency (its share of the current), the anti-resonance peaks,
the inductance it sees above its decaps and the worst margin to the target; issues `pdn_impedance` (over the target,
naming the part holding the rail there) and `decap_missing` (a supply pin without a decap pad within `decap_mm`). The
HTML report and the plugin's **Power integrity** tab (Find rails lists the rails ICs draw from; the Results tab
shows each rail's curves and what holds it at each frequency). An IC whose pads the model's grid cannot join to the
rail's copper (pads smaller than a cell) is left out with a note: raise the resolution. Each IC's pins are also solved
on a fine model of their own surroundings (the inductance at small pads and vias), added to its board-wide curve: on
kraft_flux_mini_2 (+3V3, a 0.3 A step, source U505; 2026-10-19, `tools/dev/pdn_check.py --budgets 40000,80000`) the
0.86 mm and 0.59 mm board-wide grids then agree within 1-5 % at 100 MHz (margins within 0.4 dB; U1102 0.62 Ω against
the 0.55 Ω target); U203 and U305 join only at 0.59 mm (their pads do not reach the rail's copper on the coarser
grid, which a note says).

## Voltage-aware safety checks

What a net's voltage asks of the copper and the parts around it. Each net's voltage comes from: grounds and the
reference nets (0 V); its name (+12V, VDD_3V3, 48V_BUS); a power stage's input rail and switch node (its v_in; a
boost's v_out), its output after the inductor; a motor bridge's bus and phases; a PDN rail; and through parts in
series with (nearly) no drop (shorts, 0R, shunts and resistors up to 10 Ω, inductors, ferrites, a pass FET given a
short or resistor model: its drain and source, not its gate). Your own win:

```yaml
safety:
  voltages: {/Surge/V_BATT: 58V, Net-(U303-BOOST1): 57V}   # peak, V
  coated: false             # external layers with a permanent coating: IPC-2221B B4 instead of B2
  clearance_mm: [[50, 0.6], [100, 1.5]]   # your own table for external layers ([V, mm], e.g. B3 above 3050 m)
  cap_derating: {ceramic: 50%, tantalum: 50%, electrolytic: 80%}
  resistor_derating: 50%
  vds_derating: 80%
  esd_mm: 10mm              # an ESD / TVS part on a connector pin's net this close
  esd_connectors: [J1, J4]  # the board's outside world: warnings (empty: every connector, info)
```

**Run checks** (the whole board, no solve):
- `clearance_voltage`: copper of two nets on one layer closer than IPC-2221B Table 6-1 asks for their voltage
  difference (B1 inner layers, B2 outer uncoated, B4 outer coated; over 30 V only: below, the fab's basic spacing
  applies), measured edge to edge at the closest point, the largest difference at any moment: a switch node swings
  from 0 V to its rail (so beside its rail it is the rail's voltage apart at times), a FET's gate rides on its source
  and a bootstrap net (a capacitor to the switch node and a driver pin) on its switch node, up to `gate_drive_v`
  (10 V) above it, and on through gate resistors (≤ 100 Ω) to the driver's pins. A part's own pads against each
  other don't count (its maker spaces and rates them); a track against them does. An error; a warning when the
  other net's voltage is not known (taken as 0 V; give it under `voltages`).
- `cap_voltage`: a capacitor's voltage against its rating (its **Rated Voltage** field, InvenTree's name, or
  Voltage; else "50V" in its value), derated by kind: over the rating an error, over the derated value a warning.
  Capacitors at 10 V or more without a rating: one info issue naming them.
- `esd_protection`: a connector's pins (not ground, not unconnected) with no ESD / TVS part (by its value or
  library name: TVS, ESD, SMBJ, PESD, TPD, USBLC …) on the same net within `esd_mm`.

**Simulate** (from the solved currents and voltages):
- `resistor_power`: a resistor's loss I²R in the DC paths (all stages together where combined, DC loads) or a motor
  bridge's RMS current, against its **Rated Power** field (or Power; "1/16W" as written; else by package: 0402
  1/16 W … 2512 1 W, 0612 shunts 1 W), derated. Resistors dissipating 50 mW or more without a rating: one info
  issue naming them.
- `vds_rating`: a power stage's switch-node peak (its rail plus the overshoot of the ringing) against the switches'
  **Vds** field, above `vds_derating` a warning, above the rating an error.

On kraft_flux_mini_2 (2026-10-19, the current board, +VDC 55 V as in the scratch settings, uncoated): clearance 38
errors and 13 warnings (since the voltage ranges and riding nets of 2026-10-17 the gate-drive and bootstrap nets carry
their switch node's voltage, so most of the earlier warnings became errors), none coated; ESD infos at J101, J104,
J106, J1101; C601 (10 nF, 50 V) over its rating (65 V between GND and the surge stopper's gate), 23 capacitors over
the 50 % ceramic derating. Simulate of conv1 and conv2: the switch nodes peak at 95.5 / 95.9 V (55 V + the ringing's
~40 V, as the hot-loop check estimates it with the default damping) against the EPC2306's 100 V: over 80 % of it, a
warning each; no resistor over its derated power. (Before, with +VDC 51.8 V: 16 errors, 27 warnings, 89.6 / 89.9 V.)

## Control loop of a buck or boost stage

Simulate gives each buck or boost stage with a controller on the board its loop gain T(s): crossover, phase margin, gain
margin, the Bode plot and the poles and zeros (exact: the model is a rational function of s). The parts come from
the board, the controller's figures from its symbol fields (InvenTree parameters):

- **the controller**: the IC with a pin on the stage's switch node (SW1 of a dual controller picks channel 1: ITH1,
  V_FB1, SENSE1±), by pin names (FB / VFB / VOSENSE, ITH / COMP / VC, SENSE± / ISNS± / CS±);
- **the compensation** on ITH / COMP: a series R-C to ground and a small C to ground; **the divider** on FB: the
  resistors (and a feed-forward C) from the output on top, to ground below (a resistor from another net, an output
  adjust input, as an AC ground); **the sense resistor** between the SENSE pins' filter resistors (in parallel), or
  the inductor's DCR; **the output capacitors** from the output to ground (C at bias, ESR) and the load V_out / I_out.

The controller's fields (`components/fields.py`, the help's field table):

| Field | Unit | |
|---|---|---|
| `Control Mode` | text | peak current, voltage, COT or internal (the last two: a note, no loop model) |
| `Vref` | V | the feedback reference |
| `EA gm` | S ("1.8mmho") | a transconductance error amplifier: the compensator gm·Z(COMP) |
| `EA Ro` | Ω | its output resistance (gain gm·Ro at low frequency; empty: an integrator) |
| `EA Gain`, `EA GBW` | dB, Hz | instead of gm: an op-amp error amplifier (Z_f / Z_in, ideal) |
| `Vsense Max`, `ITH Range` | V, V | peak current mode: the current-sense gain k = Vsense Max / ITH Range |
| `Current Sense Gain` | V/V | or k directly |
| `Slope Compensation` | V/s ("50mV/us") | at the sense input; empty: half the inductor's down-slope |
| `Ramp` | V | voltage mode: the PWM ramp |

The model (Ridley; Erickson & Maksimovic): peak current mode G_vc = Z'(s)·F_h(s)/R_i with R_i = R_sense/k,
Z' = 1/(Y_o + T_s·(m_c·D' − ½)/L), m_c = 1 + S_e/S_n, the sampling double pole F_h at f_sw/2 with
Q_p = 1/(π·(m_c·D' − ½)); voltage mode G_vd = (V_in/V_ramp)/(1 + (sL + R_L)·Y_o); Y_o = Σ sC/(1 + sC·ESR) + 1/R_load.
A boost (its output is its rail, behind the rectifier; D' = V_in/V_out, a = I_L/V_out, Z_L = sL + R_L): peak current
mode G_vc = (D' − a·Z_L)/(Y_o + a·D' + G·(D' − a·Z_L))/R_i·F_h with G = T_s·D'²·(m_c − ½)/L (the averaged
peak-current law with the duty cycle taken quasi-statically from the inductor, which gives Ridley's buck model
above exactly), voltage mode G_vd = V_out·(D' − a·Z_L)/(Z_L·Y_o + D'²)/V_ramp; both have the right-half-plane zero
at D'²·R_load/L. Continuous conduction is assumed (a note when the load is lighter).
Issues: `loop_phase_margin` (under 45°, or a gain margin under 6 dB: a warning; under 30°: an error),
`loop_crossover` (above f_sw/5, or a boost's above a fifth of its right-half-plane zero), and m_c·D' ≤ ½
(subharmonic oscillation) as the stage's note. Overrides per stage:
`converters: [{..., loop: {mode: current, gm: 1.8m, cs_gain: 0.05, slope: 50mV/us, r_sense: 4m}}]`.

On kraft_flux_mini_2 (U303's fields set on 2026-10-17 at the user's "add those yourself": Control Mode peak current,
Vref 0.8V, EA gm 1.8mmho, Vsense Max 50mV for ILIM floating, ITH Range 1V from the datasheet's
Figure 13, "Maximum Current Sense Threshold vs V_ITH": 0 mV at 0.4 V to 50 mV at 1.4 V; U502 COT, U503 internal): conv1 (28 V, 10 A)
crosses over at 11.2 kHz with 77° of phase margin and 20.2 dB of gain margin (Simulate of 2026-10-18; 22.6 dB when
this was first written), conv2 (5.4 V, 5 A) at 29 kHz with 91°
and 20 dB (R311 22 kΩ + C313 2.2 nF (1.9 nF at bias) + C311 5 pF; R306 / R307 and R331 / R332 with R305 / R333 from
ADJ_RAIL as AC grounds; R309 ∥ R310 and R328 ∥ R329, 4 mΩ; slope compensation assumed at half the down-slope).
`tools/dev/loop_check.py BOARD SETTINGS [--set conv1:ith_range=0.8] [--png DIR]` prints a stage's loop without a
Simulate.

## Frequency responses: Bode plots, poles and zeros

Simulate gives each signal path, power stage and PDN rail its complex frequency responses, with their poles and
zeros (`analysis/transfer.py`):

| Owner | Response | How |
|---|---|---|
| signal path | v at the receivers / v of the driver (lines, series parts, shunts, op-amp stages without slew), each op-amp stage's gain | the path's circuit swept 300 points from a hundredth of its first harmonic to the top of its band; vector fitting |
| power stage | the hot loop's ringing (node current per A of load pulse) | exact rational of L, C_oss, damping and an RC snubber |
| power stage | the input transfer (LISN current per A of loop current) | the conducted band's solves, vector fitted |
| PDN rail | each IC's supply impedance Z(f) | the rail's solves (10 kHz-1 GHz), vector fitted with an s·e term |

Vector fitting (Gustavsen & Semlyen) fits Σ r/(s−a) + d + s·e to the samples, weighting each by 1/|H|; the order
rises until the RMS error is under 1 %, and negligible poles are pruned. The largest error is reported, and a
coarse fit is noted. Poles and zeros are given as natural frequency f0 = |s|/2π and Q = |s|/(−2σ); RHP marks a
right-half-plane zero (a line's delay, approximated). The plugin shows each owner as a **Bode / poles and zeros**
view on the Results tab (magnitude and phase, the s-plane below, the dominant pole in the tiles). The HTML report
puts both plots and a table in each net's, stage's and rail's article, and the console prints the poles and zeros.
Validated against known rational functions and closed forms (tests/test_transfer.py): an RC path's pole at
1/(2π·(R_drv + R)·C) within 1 %, an op-amp stage's at GBW/gain, the ringing's at 1/(2π·sqrt(L·C_oss)) with its Q,
and a PDN rail's poles at its impedance peaks.

## Signal integrity

Run checks solves cross-sections of the board along the listed nets and the differential pairs: a cut across every
copper layer perpendicular to the track (every `step_mm`, 2 mm), with all the copper in it (other nets, pours and
planes as the reference at 0 V), the stackup's dielectrics and solder mask, solved as a 2D electrostatic problem
(`solver/xsection.py`: within 0.2 % of Cohn's exact stripline and coupled-stripline results and of Hammerstad &
Jensen's microstrip). From each cut: Z0 and εeff of a line; Zdiff and Zcomm of a pair; the crosstalk coefficients
of two tracks side by side.

```yaml
si:
  impedance:                       # targets: nets by name or pattern, or a KiCad net class (the .kicad_pro)
    - {nets: [/CPU/SPI_*], z0: 50Ω, tol: 10%}
    - {class: HighSpeed, z0: 45Ω, layers: [F.Cu, B.Cu]}
  pairs:                           # `groundtruth detect` suggests them by name (unreviewed)
    - {p: /USB/D_P, n: /USB/D_N, zdiff: 90Ω, tol: 10%, rise_time: 500ps, max_skew: 10ps}
  crosstalk_max: 5%                # NEXT or FEXT of the aggressor's swing
  stub_factor: 3                   # a via stub resonating below 3 × the knee 0.35/t_r
  step_mm: 2
```

**KiCad 10 tuning profiles** (Board Setup > Tuning Profiles, assigned to net classes) are read from the project
file: a profile's target impedance becomes its class's Z0 target (a Differential profile's, its pairs' Zdiff), and
with time-domain tuning on, its unit delay per layer is checked against the cross-sections' (`tuning_delay`: KiCad
converts lengths to delays with the figure typed there; FR-4 microstrip is ~60 ps/cm, not 100). A profile on the
Default class applies to the listed nets only.

Issues: `impedance` (a net's Z0 outside its target, with the length and the worst cut), `diff_impedance` (a pair's
Zdiff where its nets run together), `diff_skew` (the routed lengths' delay mismatch over max_skew, by default a tenth
of the edge), `crosstalk` (a listed net into a track beside it: NEXT = K_b·min(1, 2·T_d/t_r), FEXT = |K_f|·T_d/t_r,
from the pair's cross-section at its closest stretch and the coupled length), `via_stub` (a via's barrel past the
layers its net uses, resonating at c/(4·l·sqrt(εr))). Listed nets without a target get their Z0 shown. Pairs are
found by name (_P/_N, +/-, CAN_H/CAN_L, DP/DM, X and X*); USB gets 90 Ω, Ethernet / LVDS / HDMI / MIPI / SATA 100 Ω,
PCIe 85 Ω, CAN and RS-485 no target. In the plugin: the **Signal integrity** tab (pairs, targets, limits, Find
pairs, each row's result after Run checks); the console, JSON and HTML reports carry the figures.

On kraft_flux_mini_2 (6 layers, 0.1 mm to the planes): the SPI nets 43.1-43.7 Ω (SPI_MISO up to 62 Ω over 0.27 mm
where In1 has a void under it), SW_CLK 35.6-36.7 Ω on its inner layer, CAN_H / CAN_L 88-98 Ω differential, routed
13.0 / 15.6 mm (17 ps apart), the strongest crosstalk 1.7 % (ADC_I_RAIL_2 into ADC_V_RAIL_2, 42 mm side by side),
the lowest via stub resonance 25 GHz. Run checks with these 8 nets and the pair: about 10 s more (156 cuts, solved in
the solver threads).

## Compute server (optional)

The heavy analyses (Simulate, Suggest fixes, Check in Simulate, Compare what-if, Thermal, Current capacity) can run
on a powerful PC on the local network while you work in KiCad on your own machine:

```sh
groundtruth serve [--host 0.0.0.0] [--port 8765] [--workers 1] [--gpu auto] [--threads auto] [--keep-hours 24] [--log server.log]
                  [--key-file FILE] [--new-key]
                  [--lead | --no-lead] [--peers HOST,...] [--beacon-port 8766]
```

**Several servers form a cluster** when they share the access key: they find each other on the local network (signed
UDP beacons, port 8766) and the first to find another becomes the lead (`--lead` makes one the lead; `--peers` adds
servers on another subnet). `install_server.cmd install --key` joins a further server to the cluster (asks for the
key), Test connection and `install_server.cmd status` show the members, Preferences' **Find servers** lists the servers
with the user's key. **A Simulate is split over the cluster** by item (nets, power stages, motor bridges): a member
passes it to the lead, the lead spreads the items over the servers that answer and joins the parts into the same
result one server gives (analysis/split.py, remote/split.py).

**As a background service** (recommended): unzip the server package `out/dist/groundtruth-server-<version>-<commit>.zip`
(`tools/make_server_package.py`) on the server and double-click `install_server.cmd` (as administrator; KiCad 10 must
be installed there): it installs into `C:\ProgramData\GroundTruth` with its own libraries, opens the firewall
port, registers a scheduled task that starts the server at boot as SYSTEM, and shows the address and key
(`install_server.cmd status | key | stop | start | uninstall`, `--cuda cu126` for an NVIDIA GPU; its README.md).
**Remote updates**: a server installed with `--remote-update on` (asked on a first install) is updated from the
plugin: Preferences, Compute server, **Servers** lists the cluster's servers with their versions, and **Update** (one server) or **Update all**
installs the plugin's own version on the out-of-date ones (their running jobs finish first; a new server that does
not stay up is rolled back by the service). Only packages signed with the release key of the build PC the server was
last installed from by hand are taken (`tools/make_pcm_package.py` signs; the key is in
`%LOCALAPPDATA%\GroundTruth\release\` of the build PC, never in the repository), and only newer ones.
**On a Mac** (Apple Silicon such as an M2, or Intel; in use on an M2 since 2026-10-15): unzip the same package and
double-click `install_server.command` (the first time: right-click, Open, Open), then type the Mac's password. It
installs into `/Library/Application Support/GroundTruth` as the launchd daemon `com.groundtruth.server` (starts at
boot) with a Python 3.11 or newer (KiCad for macOS brings 3.9: install Python 3.13 from python.org first), allows it in the
macOS firewall and shows the address and key; `./install_server.command status | log | key | stop | start | uninstall`,
`install --key` joins a cluster. A Mac solves on its CPU only (no CUDA; Apple GPUs have no double precision).
**Or in a window:** on the server, double-click `groundtruth_server.cmd` in the installed plugin's folder (it runs the server with the Python environment KiCad made for the plugin; KiCad itself need not run; the plugin's Help, "Compute server: set up and run", has the steps). Every start shows the access key (made on the first start, kept in `%LOCALAPPDATA%\GroundTruth\server.key`), the addresses to enter and a READY line. In the plugin:
Preferences, **Compute server**: run analyses on a GroundTruth compute server, its address and the key; **Test
connection**. Simulate and Suggest fixes are **handed over**: the window is free at once, the **Server jobs** tab
lists the job, and its result comes back by itself whenever the server can be reached (a sleeping laptop or a night at
home only delay it) and is shown, or offered there (Show results). Only the user cancels a job (Cancel job, or
Remove from list, which also takes it off the list; a job whose server can't be reached any more is removed after a
second question). Check in Simulate,
Compare what-if, Thermal and Current capacity run there too while the window waits. Jobs queue first come, first
served (`workers` at once; with as many workers as GPUs, one GPU each); every job lives on the server's disk, so a
restart keeps the queue and the results; a result waits 24 h (`keep_hours`) and is removed once fetched. The server's
settings are in `%LOCALAPPDATA%\GroundTruth\server.json` (made on the first start; options override them). A server
that can't be reached when a job starts offers to run it locally. Requests and results are signed (HMAC-SHA256 with the
key, no replay) and only GroundTruth's own data is unpickled; the traffic is not encrypted (local network only). The
server must run the same GroundTruth code (and NumPy major version) as the plugin, otherwise it refuses the job.

## Status and roadmap

- **Done (latest, October 2026):**
  - the sources of every formula, method and theorem, and the model's assumptions, in [docs/REFERENCES.md](docs/REFERENCES.md) and in the help (References and assumptions); the code cites them where it uses them;
  - radiated limits other than CISPR 32: MIL-STD-461G RE102 built in, or a limit curve of your own; spread spectrum in the radiated estimate; MIL-STD-461G CE101;
  - power integrity: PDN impedance per IC against a target, the Power integrity tab;
  - voltage-aware safety checks: clearance by IPC-2221B, capacitor and FET voltage ratings, resistor power, ESD protection, voltage ranges of bootstrap and gate-drive nets;
  - signal integrity: a 2D cross-section solver, impedance, differential impedance and skew, crosstalk, via stubs, KiCad 10 tuning profiles, the Signal integrity tab; a power stage's switch node as a source of noise on other nets (victims);
  - frequency responses: Bode plots, poles and zeros of signal paths, power stages and PDN rails (vector fitting);
  - the control loop of buck and boost stages, with the controller's figures from its schematic fields;
  - DC rails with loads (the DC loads tab) and through-hole pins joined around their barrel;
  - spread-spectrum fields in the Converters tab.
- **Done (compute server):** analyses handed to one or more servers on the local network, a cluster that splits a Simulate by item and by each machine's memory and speed, a Windows service and a macOS launchd daemon, signed remote updates from the plugin.
- **Done (the plugin):**
  - **Preferences…** (per computer): Simulate's resolution (1–25× the nodes, also `--resolution`), solver threads, GPU, thermal quality, via markers, part outlines, the report's author and company, the theme; the working toggles remember their state;
  - Results tab: hover readout over issue circles and via/part markers, part outlines, every via and part with at least 1 % of the current marked, Sharper map, wheel zoom, cross-section along the route, plane displacement tile, 3D view in the browser;
  - **Export report…** (and `--format html`): one self-contained HTML report;
  - the simulation history: every Simulate kept with its board copy, settings, results and report; the History tab and `groundtruth history` compare runs;
  - **Help** (F1): the plugin's own help;
  - the Plugin and Content Manager package (`build_package.cmd`), the demo board in the plugin folder;
  - the optional GPU backend (PyTorch) with its installer and a package for another PC.
- **Done (motor bridges, milestones 1–2):**
  - detection of half-bridge legs and bridges, the Bridges tab, detection labels (`GT` symbol field);
  - the six-step drive model, each leg's hot loop, conducted emission at the input, bus-capacitor ripple;
  - losses, the RMS current density from battery to motor, thermal and current capacity, operating-point sweeps, fix suggestions for the legs;
  - victims on the same board (noise at microcontroller nets, injected current, ground shift).
- **Done (thermal):** copper and part temperatures of the power stages (PCB Thermal's solver), switching losses, current capacity.
- **Done (earlier):**
  - renamed from pcbemi to GroundTruth, with a new toolbar icon;
  - spread-spectrum switching (`spread`, `spread_mode`, `spread_rate`), with the EMI receiver simulated;
  - interleaved stages (`interleave`, `phase_deg`): a controller's phases added as phasors, conducted and radiated;
  - DC output path and ground return of power stages (`output_connector`), with the resistance split into copper and series parts (current-sense shunts);
  - fix search: each selected fix is checked at every harmonic near the limit on every net, and before/after comparisons are made at the same harmonics.
- **Done (M6, power stages):**
  - DC-DC hot loops with pours as solved copper;
  - capacitor current sharing;
  - ringing and overshoot estimates;
  - conducted emission through a LISN (CISPR 32, CISPR 25, MIL-STD-461);
  - converter detection and the plugin's Converters tab;
  - parallel solving (`analysis.threads`), receiver bandwidth, switching frequencies down to 10 kHz.
- **Done (M5 and plane capacitance):**
  - plane-pair capacitance from the physical stackup in a board-wide model solved at the signal's harmonics
  - the `plane_resonance` warning
  - the line-current emission model
  - topology-preserving rasterisation
  - what-if edits (vias, capacitors, fills) with `compare` and `--what-if`
  - `suggest` with placement rules, solver predictions and a verification run
  - the plugin's Fixes tab, including placing vias in KiCad
- **Done (M4):** multi-layer return paths, net auto-detection with review, and per-layer heatmaps with a list of the vias/parts carrying the return.
- **Done (M1–M3):**
  - parsing (any copper layer count), part models, settings file
  - Tier 1 checks
  - single-reference-plane solver with lumped bridging parts
  - CLI and CI reports
  - wx plugin
- **Tested live on KiCad 10.0.5**, on a scratch board, with `tools/dev/live_kicad.py`: reading the board, checks, simulation, fix search, drawing markers, contours and suggestions, selecting items, placing a via, and clearing. The installed plugin's toolbar button works: it opens the window on a real 4-layer board.
- **Not done yet:**
  - modelling an input filter as a what-if edit (needs a series part on the input path; until then, draw the filter into the design; the fix advisor's proposed filter is shown as frequency responses);
  - common-mode currents on cables;
  - the combined emission of interleaved stages (SW1 and SW2 of one controller are analysed separately);
  - H-bridges (two legs) as motor bridges;
  - stacked boards beyond the experimental two-board `stack:` (on hold: a stack of several boards with a leg on each needs a merge of N boards);
  - part bodies, heatsinks, an enclosure and mounting in the thermal model; part outlines in live KiCad mode (boxes around the pads until then).

The plugin window stays responsive. Reading the board, the analysis and every call into KiCad run in the background, and **Cancel** stops a running analysis. If KiCad shows a dialog or runs a tool in the PCB editor, it refuses all requests; the plugin then waits a few seconds and asks you to close the dialog or press Esc.

For contributors (and for picking the work up in a new session): [CLAUDE.md](CLAUDE.md),
[docs/HANDOFF.md](docs/HANDOFF.md) (status, open problems, next steps),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (physics, data flow, design decisions),
[docs/REFERENCES.md](docs/REFERENCES.md) (the sources of every formula, method and theorem, and the model's
assumptions, which the code cites where it uses them) and [docs/VALIDATION_AUDIT.md](docs/VALIDATION_AUDIT.md)
(the independent checks of the results).

## License

GroundTruth is free software under the GNU General Public License v3.0: see [LICENSE](LICENSE).
