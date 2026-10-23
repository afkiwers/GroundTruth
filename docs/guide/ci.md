# CI (GitLab)

Checking a board repository in a pipeline, and the release gate `groundtruth verify`. Back to the [README](../../README.md).

The `groundtruth` command line needs neither KiCad nor a display, so a board repository can check every change in a pipeline. [examples/gitlab-ci.yml](../../examples/gitlab-ci.yml) is a complete pipeline to copy; its three main jobs:

| Job | When | Runs | Shows in GitLab | Fails when |
|---|---|---|---|---|
| `emi-check` | every merge request | `groundtruth check`: the rule checks (seconds) | the MR's Code Quality widget and Tests tab | an issue is an error |
| `emi-simulate` | the default branch, or by hand | `groundtruth simulate`: the field solver (minutes) | the HTML report as an artifact, the Tests tab | an error, or an emission within 3 dB of its limit |
| `emi-verify` | tags and the default branch | `groundtruth verify`: a saved configuration judged against a thresholds file | the Tests tab, one test per criterion | a criterion fails |

`emi-verify` is the release gate: let the jobs that make production files wait for it (`needs: [emi-verify]`).

## Setting up a board repository

1. **The common settings file.** Commit `groundtruth/<board>.groundtruth.yaml` next to the board: the part models, rules, waivers and everything else the board's simulations share. The KiCad plugin writes it; on the command line `groundtruth init BOARD` and `groundtruth detect BOARD --write` make it, then review the nets and power stages. Every CI run needs it; `verify` stops with exit code 2 without it.
2. **A configuration for the gate.** In the plugin, set the tabs up as the release should be simulated (which signal paths, converters and bridges are ticked, their operating points, the run settings), then **Configuration ▾ → Save configuration as…** with a name such as `release`: it is written to `groundtruth/configs/release.gtconfig.yaml`. Commit it. (See [Configurations](settings.md#the-settings-file-groundtruthboardgroundtruthyaml): what is in a configuration and what stays shared.)
3. **The thresholds.** `groundtruth verify --write-template groundtruth/release.gtthresholds.yaml` writes the documented template ([examples/release.gtthresholds.yaml](../../examples/release.gtthresholds.yaml)). Set the limits, delete what you don't want judged, commit it.
4. **Try it on your machine** before the pipeline does:

   ```sh
   groundtruth verify hardware/myboard.kicad_pcb \
       --config hardware/groundtruth/configs/release.gtconfig.yaml \
       --thresholds hardware/groundtruth/release.gtthresholds.yaml \
       --out-dir results
   ```

5. **The pipeline.** Copy [examples/gitlab-ci.yml](../../examples/gitlab-ci.yml) to `.gitlab-ci.yml`, set `BOARD`, and in `emi-verify` set `GT_CONFIG` and `GT_THRESHOLDS`. Set `GROUNDTRUTH_VERSION` to a GroundTruth release (below).

The repository then holds:

```
hardware/
  myboard.kicad_pcb
  myboard.kicad_pro
  groundtruth/
    myboard.groundtruth.yaml        # the common settings: part models, rules, waivers (always needed)
    release.gtthresholds.yaml       # the pass / fail criteria
    configs/
      release.gtconfig.yaml         # what the gate simulates, and how
.gitlab-ci.yml
```

## Installing GroundTruth in a job

GroundTruth is not on PyPI. Every [release](https://github.com/afkiwers/GroundTruth/releases) has a Python wheel attached, with the release's number (the same as its KiCad package's); a job installs that one, so every pipeline runs the same analysis code and a new GroundTruth arrives only when you change the number:

```sh
pip install "groundtruth-pcb @ https://github.com/afkiwers/GroundTruth/releases/download/0.1.42593/groundtruth_pcb-0.1.42593-py3-none-any.whl"
pip install "groundtruth-pcb[thermal] @ https://github.com/afkiwers/GroundTruth/releases/download/0.1.42593/groundtruth_pcb-0.1.42593-py3-none-any.whl"   # with Thermal
groundtruth --version                               # groundtruth 0.1.42593
```

It needs Python 3.11 or newer (`python:3.12-slim` is enough; no git). The newest code instead, between releases: `pip install "groundtruth-pcb @ git+https://github.com/afkiwers/GroundTruth.git@main"` (its version says `0.1.dev0`). The `[thermal]` extra is only needed for a temperature criterion (`thermal: max_c`) or `simulate --thermal`. `--refill-zones` needs KiCad's own command line: use the `kicad/kicad:10.0` image for that job (the example's `emi-check-refill`). A runner with an NVIDIA GPU can speed up the solver (`emi-simulate-gpu`).

## A minimal release gate

```yaml
variables:
  BOARD: hardware/myboard.kicad_pcb
  GROUNDTRUTH_VERSION: "0.1.42593"
  GROUNDTRUTH_WHEEL: "https://github.com/afkiwers/GroundTruth/releases/download/${GROUNDTRUTH_VERSION}/groundtruth_pcb-${GROUNDTRUTH_VERSION}-py3-none-any.whl"

stages: [test, build]

emi-verify:
  stage: test
  image: python:3.12-slim
  timeout: 2h
  rules:
    - if: $CI_COMMIT_TAG
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  before_script:
    - pip install --quiet "groundtruth-pcb @ $GROUNDTRUTH_WHEEL"
  script:
    - groundtruth verify "$BOARD"
        --config hardware/groundtruth/configs/release.gtconfig.yaml
        --thresholds hardware/groundtruth/release.gtthresholds.yaml
        --out-dir emi
  artifacts:
    when: always
    paths: [emi/]
    reports:
      junit: emi/groundtruth-verify-junit.xml

fab-outputs:                          # the production files: only when the gate passed
  stage: build
  needs: [emi-verify]
  image: kicad/kicad:10.0
  rules:
    - if: $CI_COMMIT_TAG
  script:
    - kicad-cli pcb export gerbers -o fab/ "$BOARD"
  artifacts:
    paths: [fab/]
```

## The thresholds file

Every criterion is optional: one that is left out (or `null`) is not judged. Margins are "at least" (dB under the limit line), the other figures "at most" unless marked:

| Section | Criteria |
|---|---|
| `issues` | `max_errors`, `max_warnings`, `max_info`, and `checks: {<check>: <most allowed>}` with the check ids of the Issues tab (`gap_crossing`, `layer_change_stitch`, `clearance_voltage`, `emi_margin`, `victim_coupling`, …). Waived issues never count. |
| `nets` (signal paths) | `radiated_margin_db` (at least, dB); `return_path_ratio`: the return loop's inductance against an ideal plane right under the route (1.0 = ideal). |
| `converters`, `bridges` | `radiated_margin_db`, `conducted_margin_db` (at least, dB); `loop_inductance_nh` (the hot loop), `overshoot_v` (the switch node), `current_density_a_mm2` (DC, the highest in the copper); `efficiency_pct` (at least). |
| `thermal` | `max_c`: the hottest copper or part. Setting it makes verify run Thermal as well. |

`nets`, `converters` and `bridges` take a `default` that applies to every item, and entries by name (as on the plugin's tabs) whose values replace the default's for that item:

```yaml
groundtruth_thresholds: 1
issues:
  max_errors: 0
  checks: {gap_crossing: 0, layer_change_stitch: 0, clearance_voltage: 0}
nets:
  default: {radiated_margin_db: 6}
  /CPU/SPI_SCK: {radiated_margin_db: 10, return_path_ratio: 1.3}
converters:
  default: {conducted_margin_db: 3, loop_inductance_nh: 5}
  buck conv3: {efficiency_pct: 85}
thermal:
  max_c: 105
```

Unknown keys are an error (exit 2), so a misspelt criterion can't switch itself off unnoticed. An item that failed to simulate fails, and so does a name in the thresholds that the run doesn't have (the configuration and the thresholds have drifted apart).

## What a run shows

The job log ends with a summary:

```
verify: 14 passed, 2 failed, 1 skipped
  FAIL  [issues] issues: layer_change_stitch <= 0: measured 1, above the limit 0
  FAIL  [nets] /CPU/SPI_SCK: radiated_margin_db >= 10 dB: measured 7.2 dB, below the limit 10
  skip  [converters] buck conv4: conducted_margin_db >= 3 dB: not measured in this run
results: emi/groundtruth-verify-junit.xml
```

`emi/groundtruth-verify-junit.xml` has one test case per criterion: passed, failed (the value measured against the limit) or skipped (the run gives no such figure, e.g. a stage without a conducted standard). GitLab shows them in the pipeline's and the merge request's **Tests** tab. `emi/groundtruth-report.json` beside it is the run's full JSON report.

| Exit code | Meaning |
|---|---|
| 0 | every criterion passed |
| 1 | a criterion failed (the job fails; `needs:` keeps the production jobs from running) |
| 2 | the run could not be set up: no common settings file, or the configuration or thresholds file missing or invalid |

## Keeping the gate meaningful

- **Pin GroundTruth.** A newer GroundTruth can compute a slightly different figure; with a pinned release, the numbers change only when you change it (and you can rerun the gate on the same commit to compare). The JUnit file's properties and the JSON report name the GroundTruth version that judged.
- **Same resolution.** Set the limits from a run at the resolution the gate uses (`--resolution N`, default 1).
- **Accept issues in the settings, not the thresholds.** A waiver (the Issues tab's Waive, `waivers:` in the settings) says why one issue is fine; a baseline (`check --baseline`) accepts all known ones. Neither counts in `issues:`.
- **No what-if edits.** Verify judges the board as it is: the configuration's what-if edits are never applied.
- **Time.** A 6-layer board with four power stages takes several minutes per stage; give the job a `timeout:` that fits. `--history` keeps each run in `groundtruth/history/` (add it to the artifacts to keep it).

## GitHub Actions

The same gate on GitHub (the JUnit file can be shown with a test-report action of your choice):

```yaml
name: GroundTruth gate
on:
  push:
    tags: ["*"]
jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "groundtruth-pcb @ https://github.com/afkiwers/GroundTruth/releases/download/0.1.42593/groundtruth_pcb-0.1.42593-py3-none-any.whl"
      - run: >
          groundtruth verify hardware/myboard.kicad_pcb
          --config hardware/groundtruth/configs/release.gtconfig.yaml
          --thresholds hardware/groundtruth/release.gtthresholds.yaml
          --out-dir emi
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: groundtruth
          path: emi/
```
