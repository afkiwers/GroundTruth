"""Build a KiCad Plugin and Content Manager package: a zip for Plugin and Content Manager > Install from File.

    .venv/Scripts/python tools/make_pcm_package.py            # out/dist/groundtruth-<version>-<commit>-pcm.zip
    .venv/Scripts/python tools/make_pcm_package.py --working-tree   # the files on disk (build_package.cmd does this)
    .venv/Scripts/python tools/make_pcm_package.py --status stable --out DIR

Layout (dev-docs.kicad.org/en/addons): metadata.json at the root, the plugin directly in plugins/ (plugin.json,
the entry scripts, icons, requirements.txt, the groundtruth package, the demo project), the 64 px package icon in
resources/icon.png. The version entry says runtime "ipc" (KiCad 9.0.1+: an IPC API plugin) and kicad_version
10.0. KiCad installs it to <Documents>/KiCad/10.0/3rdparty/plugins/io_github_iwers_groundtruth and builds the
plugin's Python environment from requirements.txt on the next start, as for a copied plugin.

By default the files come from the last commit (git archive): a package then matches a commit (its name says
which). --working-tree packs the files as they are on disk ("-dirty" in the name when they differ from the commit),
except the demo, always from the last commit: the repo's copy is the one the user tries out in KiCad.
Each build gets a higher version (major.minor from pyproject.toml, an automatic patch number), so the Plugin and
Content Manager always takes a newer zip as an update.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

IDENTIFIER = "io.github.iwers.groundtruth"
DESCRIPTION = "Return-path and EMI analysis: plane gaps, missing stitching vias, return-current heatmaps, " \
              "radiated and conducted emission estimates, power stages."
DESCRIPTION_FULL = """GroundTruth analyses where the return currents of fast nets and switching power stages flow on a \
KiCad 10 board.

- Rule checks: plane gaps under traces, layer changes without a stitching via or decoupling capacitor, traces near \
plane edges.
- A field solver (PEEC) for the return current in every plane, with heatmaps and current arrows, loop inductance, \
plane resonances and a radiated emission estimate per net.
- Power stages: the hot loop, input capacitors, conducted emission with a LISN, DC paths and current density, and \
a thermal solve of copper and part temperatures with the current capacity.
- Suggest fixes: stitching vias and capacitors placed where they are legal, predicted with the solver and verified \
together; what-if edits to compare.

Needs the KiCad API (Preferences > Plugins > Enable KiCad API). On first start KiCad builds the plugin's Python \
environment (several hundred MB, a few minutes). A demo project is in the plugin folder (demo/README.txt).

GPU (NVIDIA, or AMD on Linux): run tools/install_gpu.py from the plugin folder once, with KiCad's Python \
("C:/Program Files/KiCad/10.0/bin/python.exe" tools/install_gpu.py on Windows). It installs PyTorch with CUDA into \
the plugin's environment, and the solver then uses the GPU (analysis.gpu: auto)."""


def git(*args: str) -> bytes:
    """Run git in the repo and return its output (raises when git fails)."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


SKIP = ("__pycache__", ".pyc", ".lck", ".kicad_prl", "-backups", ".history", "fp-info-cache")   # never packed
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


# also packed, as plugins/tools/...: GPU support for the plugin on a PC with a graphics card, run there (the
# separate GPU package, make_gpu_package.py, was dropped on 2026-10-14: these were all it held); and as
# plugins/docs/REFERENCES.md the sources and assumptions, which the help's References section shows (ui/help.py)
EXTRA = ("tools/install_gpu.py", "tools/check_gpu.py", "tools/install_gpu_amd.cmd", "tools/GPU_README.md",
         "docs/REFERENCES.md")


def package_dest(path: str) -> str:
    """plugin/... -> plugins/..., src/groundtruth/... -> plugins/groundtruth/..., tools/x -> plugins/tools/x,
    docs/x -> plugins/docs/x"""
    if path.startswith("plugin/"):
        return "plugins/" + path[len("plugin/"):]
    return "plugins/" + (path[len("src/"):] if path.startswith("src/") else path)


def committed_files() -> dict[str, bytes]:
    """plugin/ and src/groundtruth as in the last commit."""
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", "plugin", "src/groundtruth",
                                              *EXTRA)))
    return {package_dest(m.name): tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def working_files() -> dict[str, bytes]:
    """plugin/ and src/groundtruth as they are on disk (KiCad's and Python's working files left out), except the
    demo: that comes from the last commit, because the repo's copy is the one the user tries out in KiCad."""
    out = {}
    for base in (ROOT / "plugin", ROOT / "src" / "groundtruth"):
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if not p.is_file() or any(s in rel for s in SKIP) or rel.startswith("plugin/demo/"):
                continue
            out[package_dest(rel)] = p.read_bytes()
    for rel in EXTRA:
        out[package_dest(rel)] = (ROOT / rel).read_bytes()
    out.update({k: v for k, v in committed_files().items() if k.startswith("plugins/demo/")})
    return out


def main() -> int:
    """Build the package: the plugin folder and the groundtruth package (from the last commit, or the working tree),
    metadata.json with a version that grows with every build (the manager only updates to a higher version), the
    icon, zipped into --out."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "dist")
    ap.add_argument("--status", default="testing", choices=("stable", "testing", "development"))
    ap.add_argument("--working-tree", action="store_true",
                    help="pack the files as they are on disk (uncommitted changes included; the demo always from the "
                         "last commit) instead of the last commit")
    args = ap.parse_args()
    import tomllib
    major_minor = ".".join(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
                           .split(".")[:2])
    # The manager compares versions, so each build gets a higher one: major.minor from pyproject, the patch number
    # counts 10-minute steps since 2026-01-01 (the schema allows up to 6 digits: until 2045).
    build = int((datetime.now(timezone.utc) - EPOCH).total_seconds() // 600)
    version = f"{major_minor}.{build}"
    commit = git("rev-parse", "--short", "HEAD").decode().strip()
    changed = [line for line in git("status", "--porcelain", "--", "plugin", "src/groundtruth").decode().splitlines()
               if line.strip() and "plugin/demo/" not in line and not any(s in line for s in SKIP)]
    if args.working_tree:
        files = working_files()
        source = f"the files on disk (commit {commit}" + (f" + {len(changed)} uncommitted change(s))" if changed
                                                          else ")")
        tag = f"{commit}-dirty" if changed else commit
    else:
        files = committed_files()
        source, tag = f"commit {commit}", commit
        if changed:
            print(f"note: {len(changed)} uncommitted change(s) under plugin/ or src/groundtruth are NOT in the package "
                  f"(--working-tree packs them)", file=sys.stderr)
    # the build, readable without Python (the server launcher shows it) and by groundtruth.build_version()
    files["plugins/version.txt"] = (f"GroundTruth {version} ({tag}, built "
                                    f"{datetime.now():%Y-%m-%d %H:%M})\n").encode("utf-8")
    # the server package of this same build, signed with this PC's release key (remote/release.py): the plugin updates
    # the compute servers to its own version with it (Preferences, Compute server); also written to --out, so one
    # build gives both installers with one version
    from make_server_package import build as build_server
    from groundtruth.remote import release
    folder, server_zip, server_sig = build_server(version, tag, files["plugins/version.txt"].decode("utf-8"),
                                                  args.working_tree)
    files[f"plugins/{release.UPDATE_DIR}/{release.UPDATE_ZIP}"] = server_zip
    files[f"plugins/{release.UPDATE_DIR}/{release.UPDATE_SIG}"] = (server_sig + "\n").encode("utf-8")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{folder}.zip").write_bytes(server_zip)
    (args.out / f"{folder}.zip.sig").write_text(server_sig + "\n", encoding="utf-8")
    print(args.out / f"{folder}.zip")
    metadata = {
        "$schema": "https://go.kicad.org/pcm/schemas/v1",
        "name": "GroundTruth",
        "description": DESCRIPTION,
        "description_full": DESCRIPTION_FULL,
        "identifier": IDENTIFIER,
        "type": "plugin",
        "author": {"name": "afkiwers & his good old mate claude", "contact": {"email": "iwers11@gmail.com"}},
        "license": "GPL-3.0",              # the LICENSE file (GNU GPL v3); the PCM schema's name for it
        "resources": {},
        "tags": ["emi", "return-path", "pcb", "simulation", "power", "thermal"],   # lowercase, dashes
        "versions": [{"version": version, "status": args.status, "kicad_version": "10.0", "runtime": "ipc"}],
    }
    assert len(DESCRIPTION) <= 150, len(DESCRIPTION)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"groundtruth-{version}-{tag}-pcm.zip"
    from make_icons import draw
    icon = io.BytesIO()
    draw(64).save(icon, format="PNG")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
        z.writestr("resources/icon.png", icon.getvalue())
        for dest, data in sorted(files.items()):
            if dest.endswith(".cmd"):              # git keeps LF; cmd.exe wants CRLF (labels and blocks misparse)
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            z.writestr(dest, data)
    with zipfile.ZipFile(target) as z:              # what KiCad needs is there
        names = set(z.namelist())
        for need in ("metadata.json", "resources/icon.png", "plugins/plugin.json", "plugins/groundtruth_action.py",
                     "plugins/requirements.txt", "plugins/groundtruth/ui/app.py", "plugins/demo/README.txt",
                     "plugins/tools/install_gpu.py"):
            assert need in names, need
    print(f"{target}")
    print(f"  {target.stat().st_size / 1e6:.2f} MB, {len(files)} files, version {version} ({args.status}), from "
          f"{source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
