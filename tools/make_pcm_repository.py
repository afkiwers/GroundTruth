"""A KiCad Plugin and Content Manager repository for GroundTruth, made from release packages (the user: "every time I
create a new release it should create those automatically"; .github/workflows/release.yml runs it on GitHub).

    .venv/Scripts/python tools/make_pcm_repository.py entry ZIP URL OUT.json
        # one release's version entry: the package's metadata.json (from inside ZIP, as make_pcm_package.py wrote
        # it) and the download fields KiCad needs (URL, SHA-256, sizes), uploaded beside the zip as <zip>.pcm.json
    .venv/Scripts/python tools/make_pcm_repository.py site ENTRIES_DIR ICON_ZIP BASE_URL OUT_DIR
        # repository.json, packages.json and resources.zip in OUT_DIR from every release's entry: the newest
        # release's metadata, every release's version (so KiCad offers updates and older versions)
    .venv/Scripts/python tools/make_pcm_repository.py check-no-key FILE_OR_DIR ...
        # fails when a private key (PEM) is inside: the built packages and the repository are checked before upload

In KiCad: Plugin and Content Manager > Manage... > Add, the URL BASE_URL/repository.json; GroundTruth is then listed
in that repository and each new release shows as an update.

The files follow KiCad's PCM schema (go.kicad.org/pcm/schemas/v1 and v2, which differ only in the optional
`schema_version` and stricter license / type checks; none is used here, so one set of files serves KiCad clients
of either kind: GitHub Pages can't pick a file by the client's Accept header as KiCad's own repository does). The
archive's own metadata.json has no download_* fields (KiCad's rule); they are only in packages.json.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PEM = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def version_key(text: str) -> tuple:
    """A version "major.minor.patch" as a tuple of ints, for sorting."""
    return tuple(int(p) for p in text.split("."))


def entry(zip_path: Path, url: str) -> dict:
    """{"package": the package's metadata without its versions, "version": its one version with download_url,
    download_sha256, download_size and install_size (the sum of the files' unpacked sizes)}."""
    data = zip_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        meta = json.loads(z.read("metadata.json"))
        install = sum(i.file_size for i in z.infolist())
    (version,) = meta.pop("versions")
    meta.pop("$schema", None)
    version.update(download_url=url, download_sha256=hashlib.sha256(data).hexdigest(), download_size=len(data),
                   install_size=install)
    return {"package": meta, "version": version}


def _resource(url: str, data: bytes, now: datetime) -> dict:
    """A repository.json reference to one published file: its URL, SHA-256 and update time."""
    return {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "update_timestamp": int(now.timestamp()),
            "update_time_utc": now.strftime("%Y-%m-%d %H:%M:%S")}


def site(entries: list[dict], icon_zip: Path, base_url: str, now: datetime) -> dict[str, bytes]:
    """The repository's files {name: bytes}: packages.json (the newest entry's metadata with every entry's version,
    newest first, one per version number), resources.zip (<identifier>/icon.png, the package icon KiCad lists) and
    repository.json pointing at both."""
    if not entries:
        raise ValueError("no release entries")
    entries = sorted(entries, key=lambda e: version_key(e["version"]["version"]), reverse=True)
    package = dict(entries[0]["package"])
    seen, versions = set(), []
    for e in entries:
        v = e["version"]
        if v["version"] not in seen:
            seen.add(v["version"])
            versions.append(v)
    package["versions"] = versions
    packages = (json.dumps({"packages": [package]}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    res = io.BytesIO()
    with zipfile.ZipFile(icon_zip) as src, zipfile.ZipFile(res, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{package['identifier']}/icon.png", src.read("resources/icon.png"))
    resources = res.getvalue()
    base = base_url.rstrip("/")
    repository = {
        "$schema": "https://go.kicad.org/pcm/schemas/v1#/definitions/Repository",
        "name": "GroundTruth",
        "maintainer": package["author"],
        "packages": _resource(f"{base}/packages.json", packages, now),
        "resources": _resource(f"{base}/resources.zip", resources, now),
    }
    return {"repository.json": (json.dumps(repository, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            "packages.json": packages, "resources.zip": resources}


def key_inside(path: Path) -> list[str]:
    """The files under `path` (a file, a folder, or the files inside a zip) that hold a PEM private key."""
    found = []
    files = [p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts] if path.is_dir() else [path]
    for p in files:
        data = p.read_bytes()
        if PEM.search(data):
            found.append(str(p))
        if p.suffix == ".zip":
            with zipfile.ZipFile(p) as z:
                found += [f"{p}!{n}" for n in z.namelist() if PEM.search(z.read(n))]
    return found


def main() -> int:
    """The three commands (see the module's docstring)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("entry")
    e.add_argument("zip", type=Path)
    e.add_argument("url")
    e.add_argument("out", type=Path)
    s = sub.add_parser("site")
    s.add_argument("entries", type=Path)
    s.add_argument("icon_zip", type=Path)
    s.add_argument("base_url")
    s.add_argument("out", type=Path)
    c = sub.add_parser("check-no-key")
    c.add_argument("paths", type=Path, nargs="+")
    args = ap.parse_args()
    if args.cmd == "entry":
        args.out.write_text(json.dumps(entry(args.zip, args.url), indent=2) + "\n", encoding="utf-8")
        print(args.out)
    elif args.cmd == "site":
        entries = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(args.entries.glob("*.pcm.json"))]
        files = site(entries, args.icon_zip, args.base_url, datetime.now(timezone.utc))
        args.out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (args.out / name).write_bytes(data)
        print(f"{args.out}: {len(entries)} release(s), newest "
              f"{json.loads(files['packages.json'])['packages'][0]['versions'][0]['version']}")
    else:
        found = [f for p in args.paths for f in key_inside(p)]
        if found:
            print("a private key is in: " + ", ".join(found), file=sys.stderr)
            return 1
        print("no private key found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
