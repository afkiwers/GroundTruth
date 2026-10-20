# CLAUDE.md — GroundTruth (formerly pcbemi)

KiCad 10+ return-path / EMI analysis: a KiCad IPC plugin (wxPython) **and** a CI command line
(`groundtruth`) sharing one KiCad-independent analysis core. Start every session by reading
[docs/HANDOFF.md](docs/HANDOFF.md) from its top "Current state" block: current status, the work in progress, open
problems and the next steps (the dated entries below it are newest first).
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the physics, data flow and design decisions.
User-facing docs: [README.md](README.md).

## Environment (Windows 11, PowerShell + Git Bash)

Since 2026-10-18 the work is on a bigger machine (user `andre`, 16 threads, 32 GB, NVIDIA RTX 3050 Laptop 4 GB,
driver 527 / CUDA 12.0). The notes below were written on the old laptop (user `iwers`); where this machine differs:
- the repo is `C:\Users\andre\Documents\github\GroundTruth`; Documents is not redirected: KiCad's plugins are in
  `C:\Users\andre\Documents\KiCad\10.0\plugins`; KiCad 10.0 at `C:\Program Files\KiCad\10.0` as before;
- kraft_flux_mini_2 is the user's copy `C:\Users\andre\Downloads\flux-2\flux-2.kicad_pcb` (2026-10-17, the current
  board; `Downloads\flux\flux\flux.kicad_pcb` is an older 10-14 copy without U303's fields). Read only, never
  write next to it; the scratch settings are in `out/`
  (copied from the laptop: `out/audit/`, `out/kraft_refresh/`, `out/converge/`, the audit sources);
- the venv is python.org 3.13 with `out/audit_validation/venv_constraints.txt` and PyTorch **cu118** (the CUDA
  build, not the CPU one: `pip install torch --index-url https://download.pytorch.org/whl/cu118`), so `gpu: auto`
  sees the RTX 3050; since 2026-10-19 auto times it against the CPU and keeps to the CPU here (0.3-0.8x; a full
  kraft Simulate took > 2 h on it, 18 min on the CPU). `gpu: on` / `--gpu on` still forces it.
- **No release key here** (`%LOCALAPPDATA%\GroundTruth\release` is on the laptop): `tools/make_pcm_package.py` would
  create a new one, and the user's compute servers trust the laptop's. Don't build packages here until the user has
  copied the key over (the user chose to skip the package on 2026-10-19). KiCad has GroundTruth from the Plugin and
  Content Manager (`Documents\KiCad\10.0\3rdparty\plugins`), so `tools/install_plugin.py` would show every button
  twice: don't run it either.

- **Do not use the default `python`**: it's MSYS/MinGW 3.12 and cannot install PyPI wheels.
  The project venv uses python.org 3.13: `.venv\Scripts\python`. Recreate with
  `py -3.13 -m venv .venv` and `.venv\Scripts\python -m pip install -e ".[plugin,dev,thermal]"` (thermal:
  numba, pyamg, psutil for the thermal solve; `src/groundtruth/thermal/` is the solver of the user's PCB
  Thermal app, `C:\Users\iwers\Downloads\Thermal solver`). The dev venv also has PyTorch's CPU build
  (`.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu`, session 6) for
  the GPU backend's tests (`tests/test_gpu.py`, skipped without it; `GROUNDTRUTH_GPU_DEVICE=cpu` runs the GPU code
  on the CPU). This laptop has no usable GPU (Intel UHD 620); the plugin's target machines have big GPUs. Never
  install PyTorch into the KiCad plugin venv here (`tools/install_gpu.py` is for the GPU machines).
- KiCad 10.0 is installed at `C:\Program Files\KiCad\10.0` (bundled Python 3.11 + wx 4.2.2;
  `kicad-cli.exe` in `bin`). Plugin code must stay **Python 3.11 compatible**.
- The user's Documents folder is redirected to OneDrive: KiCad plugins live in
  `C:\Users\iwers\OneDrive\Dokumente\KiCad\10.0\plugins`. `tools/install_plugin.py` finds it.
- The user enabled the KiCad API server on 2026-10-04 (`kicad_common.json`, `api.enable_server: true`).
  Never change the user's KiCad config yourself. What was learned about live KiCad 10.0.5 on Windows:
  - The setting can silently revert: every KiCad process keeps its own copy of `kicad_common.json` and writes
    it back when it closes. A KiCad window opened before the API was enabled set `enable_server` back to
    `false` on 2026-10-04. When the pipe is missing, read the file (read-only) and ask the user to enable
    the API again in the running KiCad. It starts without a restart, but the Preferences dialog must be
    closed again.
  - Only **one** KiCad process serves the API, at `\\.\pipe\%TEMP%\kicad\api.sock`. A second `kicad.exe`
    (even with TEMP redirected) and a standalone `pcbnew.exe` start no server. So live tests run in the
    user's own KiCad.
  - Before connecting, check which process owns the pipe: `GetNamedPipeServerProcessId`, see HANDOFF.
    The user may have other KiCad windows open with company or customer work. Never read those.
  - Board requests need the PCB editor open; the project manager alone answers "no handler". `RunAction`
    is not handled by the project manager, so the PCB editor can't be opened through the API.
  - KiCad starts a plugin action as `<venv>\Scripts\pythonw.exe <entry script>` **without** plugin.json's
    `args`, so every action needs its own entry script. The plugin logs each start-up step to
    `%LOCALAPPDATA%\GroundTruth\plugin.log`.
  - While a modal dialog is open or a tool is active in the PCB editor, every board request returns
    AS_BUSY. `kipy_source.when_ready` retries, then raises `KiCadBusyError` with a message for the user.
  - Writes (markers, vias) only go to the scratch board (`out/live/live.kicad_pro`, made by
    `tools/dev/live_kicad.py make out/live` and opened by the user in their KiCad), never to a real design.
    `tools/dev/live_kicad.py` enforces this. If you ever start a KiCad process yourself, close it with a forced
    kill, so it doesn't save settings or the recent-files list, and delete its `~*.lck` files.
- Console code page is cp1252: the CLI reconfigures stdout to UTF-8; for ad-hoc scripts set
  `PYTHONIOENCODING=utf-8`.
- Git Bash rewrites arguments that start with `/` into Windows paths (`/CPU/SPI_SCK` →
  `C:/Program Files/Git/CPU/SPI_SCK`). Prefix such commands with `MSYS_NO_PATHCONV=1`, or use PowerShell.
- Bash has no foreground `sleep`; for long runs, log to a file in the scratchpad and run in the background.

## Commands

```sh
.venv/Scripts/python -m pytest -q                 # ~30 min, ~640 tests in one process (1 live-KiCad test skipped; GUI tests
                                                  # need a display; GUI timing tests can fail on a loaded machine: rerun
                                                  # them alone). Fits in memory since 2026-10-10 (it ran out before)
.venv/Scripts/python tools/dev/run_tests_split.py # the same, one file per process (~40 min)
.venv/Scripts/groundtruth --help                       # init | detect | info | check | simulate | suggest | compare | history
.venv/Scripts/groundtruth history BOARD [--compare 1 [2]]   # the runs kept in <board folder>/groundtruth/history/
                                                  # (every plugin Simulate; CLI: simulate --history)
.venv/Scripts/groundtruth detect BOARD --settings X --write   # nets AND power stages (converters), unreviewed
.venv/Scripts/groundtruth-gui board.kicad_pcb          # plugin window on a file (no KiCad needed)
.venv/Scripts/groundtruth serve [--port 8765] [--workers 1] [--keep-hours 24]   # the compute server (remote/,
                                                  # HANDOFF "compute server"; settings %LOCALAPPDATA%\GroundTruth\
                                                  # server.json); the plugin: Preferences, Compute server; jobs
                                                  # detached (remote/jobs.py, the Server jobs tab)
                                                  # [--lead|--no-lead] [--peers H,...] [--beacon-port 8766]: servers
                                                  # with one key form a cluster (remote/cluster.py, UDP 8766)
.venv/Scripts/python tools/install_plugin.py --dry-run
.venv/Scripts/python tools/dev/cavity_check.py    # plate-pair capacitance and cavity resonance by hand
.venv/Scripts/python tools/dev/live_kicad.py owner # live KiCad tests: owner | make | read | write | timing | freeze (see HANDOFF)
.venv/Scripts/python tools/dev/power_check.py BOARD SETTINGS --only SW1 --f-sw 500k --i-out 5 --v-in 24 --c-oss 600p
                                                  # power stages of a board with test parameters (see HANDOFF)
.venv/Scripts/python tools/dev/real_settings.py BOARD OUT.groundtruth.yaml [--spread-mode random|triangle|off]
                                                  # kraft_flux_mini_2 settings file with the REAL operating points (session 4)
.venv/Scripts/python tools/install_plugin.py      # copy the plugin into KiCad after every code change
.venv/Scripts/python tools/install_plugin.py --uninstall   # remove that copy (when the PCM package is installed instead)
.venv/Scripts/python tools/make_pcm_package.py    # out/dist/groundtruth-<ver>-<commit>-pcm.zip for Plugin and Content
                                                  # Manager > Install from File (from HEAD; --working-tree: files on disk)
                                                  # and the signed server package of the same build (in the plugin for
                                                  # remote updates, and in out/dist); signs with the release key in
                                                  # %LOCALAPPDATA%\GroundTruth\release (made on the first build: the
                                                  # user's; never commit or print it)
build_package.cmd [--committed]                   # the user's console/double-click wrapper (working tree by default)
.venv/Scripts/python tools/dev/gui_probe.py BOARD --settings S --simulate   # GUI-thread stalls on a board copy (see ARCHITECTURE "Plugin")
            [--thermal] [--capacity] [--theme dark] [--threshold-ms 150] [--default-gil]   # also Thermal / capacity
.venv/Scripts/python tools/dev/build_profile.py   # how long each tab's build step blocks the GUI thread
.venv/Scripts/python tools/dev/mem_probe.py BOARD SETTINGS --fail memory|error|cancel|none [--warm 2 --repeat 3]
            [--threads 4] [--who] [--trace N]     # memory a (failed) Simulate leaves behind: working set and private
                                                  # bytes per run (see ARCHITECTURE "Preconditioner", Memory)
.venv/Scripts/python tools/dev/mem_profile.py BOARD SETTINGS [--only NET ...] [--repeat 2] [--threads 4]
            [--operator-cache N] [--save-result R.json] [--json OUT.json] [--trace]   # a Simulate's memory: peak per
                                                  # phase, a census of the live solver objects near the peak,
                                                  # factorisations made (ARCHITECTURE "Memory use of a Simulate";
                                                  # before/after: run main's copy in a `git worktree`)
.venv/Scripts/python tools/dev/mem_boards.py --rounds 3 B1 S1 B2 S2   # several boards in one process: memory after each
.venv/Scripts/python tools/dev/window_shot.py BOARD --theme dark --simulate --tabs Heatmap [--scroll] [--size 1920x1040]
                                                  # window screenshots (--scroll: also the Heatmap page scrolled down;
                                                  # --runs 2 --tabs History: two kept runs, compared)
.venv/Scripts/python tools/dev/thermal_check.py BOARD SETTINGS --only SW1,SW2 --rds-on 4m --dcr 5m --quality draft
                                                  # thermal solve of power stages with test loss values
            [--capacity] [--cache out/m3/stages.pickle]   # current capacity (+ a fresh-solve check); cache Simulate
.venv/Scripts/python tools/dev/bridge_check.py BOARD SETTINGS --v-bus 48 --f-pwm 20k --i-phase 100 --speed 3000
            --motor 10m,30u,8,7,star [--rds-on 0.7m --c-oss 2n --q-oss 170n --dead 47n --bridge NAME]
                                                  # one motor bridge at an operating point and motor (--help; --budgets
                                                  # 100000,200000 for grid convergence; --sweep-i 50,100,200
                                                  # --sweep-speed 0,3000; --no-victims; long: run in the background)
.venv/Scripts/python tools/dev/add_labels.py BOARD OUT.kicad_pcb Q1=hb_hs Q2=hb_ls J1=hb_out
                                                  # a copy of a board with GT detection labels (analysis/tags.py)
.venv/Scripts/python tools/dev/victim_survey.py BOARD   # a motor-drive board's ICs and sensitive nets, distance to the bridge
.venv/Scripts/python tools/dev/detect_dump.py OUT.json BOARD [BOARD ...]   # everything detection finds, as JSON:
                                                  # refactoring detection must not change it (run main in a worktree
                                                  # for the baseline, then `cmp` the two files)
.venv/Scripts/python tools/dev/label_project.py PROJECT_DIR --backup DIR Q301=conv1_hs ... [--remove]
                                                  # GT labels IN PLACE (schematic symbols + board footprints): only
                                                  # when the user asks; refuses while KiCad has the project open
.venv/Scripts/python tools/dev/contour_preview.py BOARD SETTINGS --only NET [--layer L] [--cache F.pickle] [--zoom X0,Y0,X1,Y1]
                                                  # the contour map Draw contours puts into KiCad (results/contours.py),
                                                  # over the heatmap as a PNG: levels, smoothing, labels without KiCad
.venv/Scripts/python tools/dev/mirror_vias.py BOARD OUT.kicad_pcb --axis-x 141.84 --region 142,86,168,95 [--nets GND]
            [--map SW1=SW2] [--min-gap 0.55] [--dry-run]   # a COPY with one stage's vias mirrored onto its twin
                                                  # where it has none (clearance-checked); then kicad-cli pcb drc
                                                  # --refill-zones --save-board OUT (kraft conv1 -> conv2, session 10)
.venv/Scripts/python tools/dev/dc_combined_check.py BOARD SETTINGS [--only STAGE ...] [--resolution 2]
                                                  # every stage's DC paths on the whole-board DC model, each alone and
                                                  # all together (power.combine_dc), the DC issues, the Simulate's time
.venv/Scripts/python tools/dev/dc_region_check.py BOARD SETTINGS --only STAGE [--margins 5,100]
                                                  # a stage's DC paths on its region margins (before dc_board_build)
.venv/Scripts/python tools/dev/pin_check.py BOARD SETTINGS --part J101 --values "fill:75, contact:0.5m" [--only STAGE ...]
                                                  # a part's through-hole pin values (analysis/pins.py): Simulate
                                                  # without/with them, its heat, DC paths, densities near it
.venv/Scripts/python tools/dev/track_width_check.py [--widths 0.4,0.1 --angles 0,30,45 --offsets 0,0.03,0.06]
            [--thermal] [--src DIR] [--json OUT]   # tracks' R', drop, J, heat (and temperature) against closed forms
                                                  # and an independent cross-section (audit round 5; --src: main's tree)
.venv/Scripts/python tools/dev/track_capacity_check.py   # a 0.4 / 0.1 mm track's current capacity against the cross-section
.venv/Scripts/python tools/dev/tol_check.py BOARD --settings S --out-dir OUT [--tol 1e-10 --cycles 60] [--nets ...]
                                                  # a Simulate at another GMRES tolerance (+ the KVL / KCL residuals
                                                  # apart): result_diff against the default's (audit round 6)
.venv/Scripts/python tools/dev/thermal_tol_check.py [--width 0.1 --amps 1] | [--board B --settings S]   # Thermal's
                                                  # answer at its tolerances against CG 1e-12 (the error they leave)
.venv/Scripts/python tools/dev/adversarial_dc.py [--cases neck_short,acute,parallel,tee,corner,...] [--res 1,2,4]
                                                  # adversarial copper against the sheet solves (both pad models)
.venv/Scripts/python tools/dev/metamorphic_dc.py  # rotations, shifts, re-drawn copper, reciprocity, linearity, 2x copper
.venv/Scripts/python tools/dev/angle_scan.py --width 0.1 [--angles 0:90:3] [--offsets 0,0.1] [--res 1]   # R' by angle
.venv/Scripts/python tools/dev/cavity_analytic_converge.py [--pitches 1,0.5,0.25]   # cavity f10, plate C, loop L
                                                  # against closed forms by pitch
.venv/Scripts/python tools/dev/stress_inputs.py   # degenerate inputs: zero widths / thicknesses / currents, runaway
.venv/Scripts/python tools/dev/neck_check.py [--necks 2,5,20] [--resolutions 1,2,4,16]   # a neck's added R against
                                                  # an independent sheet solve per resolution (the width step, A39)
.venv/Scripts/python tools/dev/dc_thermal_summary.py BOARD SETTINGS [--src DIR] [--json OUT] [--quality fine]
            [--refine-ring N] [--no-thermal]   # every stage's DC paths and Thermal's figures, to compare two trees
.venv/Scripts/python tools/dev/topology_diff.py BOARD SETTINGS --src DIR   # pads joined by one tree's DC model only
.venv/Scripts/python tools/dev/current_where.py BOARD STAGES.pickle X0 Y0 X1 Y1
                                                  # which views/layers/nets carry current inside a box
.venv/Scripts/python -u tools/dev/profile_sim.py BOARD SETTINGS --only SW1 [--suggest] [--threads 1] [--out F.prof]
                                                  # where Simulate / the fix search spend their time, factorisations and
                                                  # GMRES iterations counted (run in the background: long)
.venv/Scripts/python tools/dev/solver_bench.py BOARD SETTINGS --only SPI_SCK [--solve]   # one GMRES iteration, part by part
.venv/Scripts/python tools/dev/solve_probe.py BOARD SETTINGS --only SPI_SCK --f 1.58e9 [--resolution 4]
                                                  # one solve of a net's board-wide (or route) model: converges? its
                                                  # iterations and time (a "GMRES did not converge" note)
.venv/Scripts/python tools/dev/fix_check.py BOARD SETTINGS [--only conv1] | --test-buck   # the fix advisor's design
                                                  # changes per power stage (analysis/fix_advisor.py)
.venv/Scripts/python tools/dev/py311_check.py     # the plugin's code compiles on KiCad's Python 3.11 (the venv is 3.13,
                                                  # which accepts newer f-string syntax); tests/test_py311.py runs it
.venv/Scripts/python tools/dev/ast_same.py        # changed files vs HEAD with docstrings stripped: a docs/comments-only
                                                  # change must print "only comments and docstrings changed"
.venv/Scripts/python tools/dev/undocumented.py FILES   # functions and methods without a docstring (0 in src/groundtruth;
                                                  # new code keeps it so)
.venv/Scripts/python tools/dev/result_diff.py A.json B.json   # two simulate JSON reports number by number (speed work
                                                  # must not change results): run main in a `git worktree` for A
.venv/Scripts/python tools/dev/gpu_probe.py       # what this machine's GPU could do for the solver (needs pyopencl)
python tools/install_gpu.py [--cuda cu118|cu126|cu128] [--python PY] [--dry-run]   # ON A GPU MACHINE: PyTorch with
                                                  # CUDA into the KiCad plugin venv (or PY's environment): analysis.gpu auto
                                                  # uses it (default cu126; cu118 for drivers showing CUDA 11.8-12.5).
                                                  # AMD Radeon (Windows/Linux): --amd [gfx1152] = AMD's ROCm build
                                                  # from repo.amd.com/rocm/whl-multi-arch (chip from the card's name).
                                                  # Relays the child's output through a pipe (the venv's python.exe can
                                                  # print nothing to the console otherwise)
python tools/check_gpu.py [--python PY] [--plugin DIR]   # after it: will the installed plugin use the GPU + FFT timing
.venv/Scripts/python tools/make_server_package.py # out/dist/groundtruth-server-<ver>-<commit>.zip: the compute
                                                  # server as a service (install_server.cmd; macOS: install_server.command,
                                                  # launchd; server_setup.py --dry-run --platform mac plans it here)
python tools/dev/gpu_check.py BOARD SETTINGS --only SPI_SCK   # on the GPU machine: GPU vs CPU, results and speed-up
.venv/Scripts/python tools/dev/transfer_check.py BOARD SETTINGS --only NET   # per board-wide frequency and plane cut:
                                                  # displacement vs via current, KCL, cavity modes ("12.9× signal")
.venv/Scripts/python tools/dev/cavity_converge.py BOARD SETTINGS --only NET --resolutions 1,2,4
            [--freqs 500e6:1500e6:20e6] [--tol 1e-7] [--json OUT]   # a net's board-wide model on the SAME
                                                  # frequencies at several resolutions: do its resonances converge?
                                                  # (4x: ~1 h for 50 frequencies; run in the background)
.venv/Scripts/python tools/dev/section_check.py BOARD SETTINGS --only NET [--budgets 100000,200000]
                                                  # the cross-section on two grids (convergence) + PNGs
.venv/Scripts/python tools/dev/stitch_contrast.py [--capacitance]   # out/stitch_contrast.png: a layer change without /
                                                  # with a stitching via, log and linear colour scale
.venv/Scripts/python tools/make_examples.py       # examples/: small example projects with their settings (op-amp path,
                                                  # series path, RC filter, termination) from tests/boardgen.py; checked in
.venv/Scripts/python tools/make_demo.py           # plugin/demo/: the demo project shipped in the plugin folder (the
                                                  # layer-change test board, zones filled by kicad-cli); checked in
.venv/Scripts/python tools/make_icons.py [--preview X]   # plugin/icons/groundtruth-*.png (toolbar icons)
%TEMP%\femvenv\Scripts\python tools/dev/fem_spike.py 1e6 1.0 pardiso   # the FEM spike (docs/FEM_OPTION.md): NGSolve
                                                  # in a venv of its own (setup in its docstring), never this one
.venv/Scripts/python tools/dev/fix_combo_probe.py BOARD SETTINGS SUGGESTIONS.json NET   # selected fixes alone vs
                                                  # together (low-rank prediction) vs the report's verification
.venv/Scripts/python tools/dev/gap_probe.py BOARD SETTINGS SUGGESTIONS.json NET [F1,F2,...]   # does the baseline peak
                                                  # between its solved harmonics?
"/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new --disable-gpu --hide-scrollbars \
    --user-data-dir=<scratch>/edge_profile --window-size=1400,900 --screenshot=<scratch>/shot.png \
    "file:///C:/.../view3d_<stem>.html#board"     # look at a 3D page (report/view3d.py) without a display; the
                                                  # "sync ... ERR_ABORTED" lines are Edge's own, not the page's
```

Real boards for manual testing (read-only, never write next to them; pass `--settings` pointing to a
scratch folder). A settings file for them is made with `groundtruth init BOARD --settings X` + `groundtruth detect BOARD
--settings X --write`; the session scratchpad is NOT kept between sessions or accounts, so put reusable scripts
in `tools/dev/`, never only in the scratchpad.
- `C:\Users\iwers\OneDrive\Dokumente\github\kraft_flux_mini_2\kraft_flux_mini_2.kicad_pcb`: KiCad 10, 6 copper layers since the user
  changed it on 2026-10-05 ~21:30 (4 before: every result in the docs up to session 6 is from the 4-layer version),
  SPI nets `/CPU/SPI_SCK` (U1102.12→U1103.6), `/CPU/SPI_MOSI` (U1102.14→U1103.5),
  `/CPU/SPI_MISO` (U1103.2→U1102.13), `/CPU/SW_CLK` (J1101.4→U1102.28).
  **GT labels** since 2026-10-07, set at the user's request ("add labels to my Kraft flux schematic ... Leave them
  afterward", `tools/dev/label_project.py`, in the schematic and the board): conv1 Q301/Q302/L301, conv2
  Q303/Q304/L302, conv3 U502 (sw) / D501 (ls) / L502, conv4 U503 (sw) / L503, J101 conv1_in,conv2_in,conv3_in, J104
  conv1_out, J106 conv2_out. The files before: `out/kraft_backup_2026-10-07/`. Since 2026-10-11 also the two current-sense ADC paths, asked for twice ("can you add those?", then "The
  OPA needs labels too!"): U302 / U304 sig_drv:3 (ZXCT1107 outputs), U301 / U305 amp_in:1, amp_out:4 (TLV6001
  followers), U1102 sig_rx:9, sig_rx:10. Detection gives /CPU/ADC_I_RAIL_1 U302.3 → U301 → R303 4.7 kΩ → U1102.9
  and /CPU/ADC_I_RAIL_2 U304.3 → U305 → R337 → U1102.10 (R304 / R334 10 kΩ loads and C304 / C342 100 nF as
  shunts). Before: `out/kraft_backup_2026-10-11/` (no ADC labels), `out/kraft_backup_2026-10-11b/` (the first,
  incomplete ADC labels).
  Power stages (all found by `groundtruth detect`), rail +VDC unless noted:

  | Stage | Controller | Switches | Switch node | Inductor |
  |---|---|---|---|---|
  | SW1 | LTC7890 (U303) | EPC2306 GaN, Q301 high side / Q302 low side | `/DCDC Stage/Rails/SW1`, poured on F.Cu + In1 | L301 5.6 µH |
  | SW2 | LTC7890 (U303) | EPC2306 GaN, Q303 high side / Q304 low side | `/DCDC Stage/Rails/SW2`, poured on F.Cu + In1 | L302 5.6 µH |
  | Diode buck | LM5008 (U502, integrated high side) | catch diode D501 | `Net-(D501-K)`, poured | L502 to +12V |
  | U503 | LT3970 (integrated, rail +12V) | inside the IC | `Net-(U503-SW)`, poured | L503 to +5V |

  The input is the XT60 J101 (pin 2 V_BATT, pin 1 GND). V_BATT reaches +VDC through the surge-stopper FET
  Q601, so for a conducted estimate Q601 needs a short/resistor model. Detection recognises it as a series pass
  FET and suggests a short (`detect.pass_switches`); by hand: `power_check.py --series Q601 --connector
  J101.2,J101.1`.
- `C:\Users\iwers\OneDrive\Dokumente\github\ness-2-web-pcb\ness_2_wifi.kicad_pcb`: 2-layer, KiCad 9 format.
- `...\github\kraftFlux-mini-4\kraft_flux_mini_4\kraft_flux_mini_4.kicad_pcb`: placed but unrouted.
- Motor bridges are tested on `tests/boardgen.bridge_board`; `GT` symbol fields (detection labels,
  `analysis/tags.py`) make detection explicit where needed.

## Rules for working in this repo

- **kipy (kicad-python) is used only in `src/groundtruth/adapters/kipy_source.py` and `kipy_sink.py`**
  (and `ui/` imports wx). `cli.py` and the core must never import wx or kipy. `tests/test_cli.py` enforces it.
  CI installs only the base package.
- Everything geometric is in **mm** in the board model and **metres** inside `solver/`.
- Code edits: prefer the Edit tool. Python-in-heredoc patch scripts have corrupted `\n` and `\d`
  escapes several times. If you must script edits, write the script with the Write tool (scratchpad), run it,
  and read/write files with `encoding="utf-8"` (the working copy is CRLF; git stores LF, the warning is harmless).
- **The plugin window must never freeze.** The user: "it should never not respond" (and earlier, about a click
  that seemed to freeze KiCad: "This can't happen"). Nothing slow on the GUI thread: KiCad calls and analysis are
  jobs (`MainFrame._start`), settings saves and heatmap renders go to `ui/background.py` workers, no wx objects at
  import time (the start-up worker imports the modules). Before shipping a GUI change, run
  `tools/dev/gui_probe.py` on a real board copy and compare the stalls (see ARCHITECTURE "Plugin").
- **Every `wx.Timer` owned by a window is stopped on `EVT_WINDOW_DESTROY`**, not only in the close handler: a
  timer that outlives its window fires into freed memory (heap corruption 0xc0000374 / access violation; it
  crashed the GUI tests every time in session 5). A pytest run that ends with exit code 127 or 139 and "Windows
  fatal exception" is such a crash, not a test failure: look at the "Current thread" stack in the log.
  `EVT_WINDOW_DESTROY` bubbles up from child windows: a handler bound on a frame must check the window id
  (`MainFrame._stop_timers`), or closing any child frame stops the frame's timers.
- The plugin's help (`src/groundtruth/ui/help.html`, top bar **Help** / F1) explains every tab and how the
  analyses fit together; the user asked for it. A feature that changes what a tab does or what Simulate computes
  updates it in the same branch.
- The user's per-computer choices (Preferences…: resolution, solver threads, GPU, thermal quality, via markers,
  vias shown, part outlines, report author and company, simulation history on/off, theme; and the toggles' state: current arrows, log scale,
  cross-section, issue zoom/highlight) live in `%LOCALAPPDATA%\GroundTruth\ui.json`, never in a board's
  settings file. `tests/conftest.py` points LOCALAPPDATA at a private folder for every test (its ui.json turns the
  simulation history off); a test that changes a preference still patches `theme.pref_path` (or LOCALAPPDATA)
  itself: never write the user's real file.
- **A board's GroundTruth files live in `<board folder>/groundtruth/`** (`settings.project_dir`): its settings
  `<board>.groundtruth.yaml` (only there; no compatibility with the old place beside the board) and the
  simulation history. **Every plugin Simulate is kept** in `groundtruth/history/` (runs/, boards/). Tools
  that open the window on a real board must work on a copy (gui_probe, window_shot do), or they write a history
  next to the user's board.
- Power stages (`analysis/power.py`) are not routes: they are solved as a hot loop (switch filaments +
  injections, capacitors as branches, optional LISN). Pours of the nets involved are always solved copper. The
  user stressed this: "copper pours are not neglected, This is very important".
- Synthetic test boards come from `tests/boardgen.py` (real KiCad 10 S-expression text). Add a
  scenario there rather than checking in board files.
- Every function, method and class in `src/groundtruth` has a docstring, and non-obvious bodies say in comments
  what is done and which assumption or technique is used (the user, 2026-10-11: "the code in a state where it can
  be easily read/understood by a human"). Keep it so for new code (`tools/dev/undocumented.py`).
- Every new physics feature needs a validation test against an analytic result (see `tests/test_solver.py`,
  `tests/test_multilayer.py`, `tests/test_capacitance.py`).
- Every formula, method, theorem and assumption cites its source where the code uses it, as a key of
  [docs/REFERENCES.md](docs/REFERENCES.md) in square brackets (`[Grover 1946]`, `[Jackson 1999, §5.6]`, `[A12]`);
  what has no published source is an assumption there, with its reason and evidence (the user, 2026-10-18: "add
  references of the assumptions/methods/theorems, also call them out locally where they are used, in all
  calculations"). `tools/dev/ref_keys.py` checks that every cited key is in the list.
- Check grid convergence on the real board before trusting a new model result. Compare two grid budgets.
  That is how the rasterisation topology bug was found (see HANDOFF).
- Git: the user asked for a repo. `main` holds the reviewed state; unmerged branches are listed in HANDOFF (all earlier feature
  branches were merged and deleted on 2026-10-05). Start new work on a new feature branch from `main`. Commit messages end with the `Co-Authored-By` trailer the session provides. Don't push
  (the remote is `origin`, github.com/afkiwers/GroundTruth) and don't merge to `main` without asking.
- The user steers per milestone: finish a milestone, show results on their real boards, then ask before
  starting the next. Don't assume old tool versions (they're on KiCad 10 and want 10+ only).
