# Development

The source tree and how the code is laid out. Back to the [README](../../README.md).

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
                 make_demo.py, make_icons.py; dev/: developer scripts (each one's docstring says how to run it)
tests/           synthetic board generator + test-suite
```
