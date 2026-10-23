# The command line

`groundtruth` for scripts and CI: the commands, options and exit codes. Back to the [README](../../README.md).

```sh
groundtruth init board.kicad_pcb                 # create/update the settings file
groundtruth detect board.kicad_pcb --write       # suggest nets (driver/receivers/timing), power stages and motor bridges for review
groundtruth info board.kicad_pcb                 # stackup, counts, parts missing model values
groundtruth check board.kicad_pcb                # Tier 1
groundtruth simulate board.kicad_pcb -v          # Tier 1 + Tier 2, power stages and motor bridges
groundtruth suggest board.kicad_pcb --write      # propose, predict and verify fixes; store them as what-if edits
groundtruth compare board.kicad_pcb              # the board with and without the enabled what-if edits
groundtruth simulate board.kicad_pcb --history   # also keep the run in groundtruth/history/
groundtruth simulate board.kicad_pcb --config groundtruth/configs/adc.gtconfig.yaml   # a saved configuration, this run only
groundtruth history board.kicad_pcb [--compare 1 [2]]   # the runs kept (1 = the newest), or two compared
groundtruth verify board.kicad_pcb --config groundtruth/configs/release.gtconfig.yaml     --thresholds groundtruth/release.gtthresholds.yaml --out-dir results   # the CI release gate (below)
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

**Release gate:** `groundtruth verify BOARD --config C --thresholds T` runs a saved configuration and judges it against pass / fail criteria, one JUnit test per criterion (exit 0 pass, 1 fail, 2 setup error); see [CI (GitLab)](ci.md#ci-gitlab).

Example boards to try (an op-amp stage in a signal path, series parts across nets, an RC filter, a termination),
each ready to simulate with its settings file: [examples/](../../examples/README.md). For a GitLab pipeline, see
[examples/gitlab-ci.yml](../../examples/gitlab-ci.yml) and [CI (GitLab)](ci.md#ci-gitlab) below.
