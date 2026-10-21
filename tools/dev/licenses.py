"""Every Python package GroundTruth needs, with its version and licence, from the installed packages' own metadata:
to keep the help's "Third-party software and licences" section (src/groundtruth/ui/help.html) up to date.

    .venv/Scripts/python tools/dev/licenses.py            # name, version, licence, who needs it, home page
    .venv/Scripts/python tools/dev/licenses.py --check    # the installed ones the help does not name (exit 1)

The roots are the plugin's requirements (plugin/requirements.txt) and the optional extras of pyproject.toml (gpu,
server); their own requirements are followed (optional extras of theirs are not: GroundTruth does not install them).
A package not installed here (NVIDIA's CUDA wheels on a machine without them) is listed as such. Licences come from
License-Expression, else the licence classifiers, else the License field: generic ones ("BSD License") are named
exactly in the help."""
import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTRAS = ("gpu", "server")                  # pyproject's optional extras that ship (dev / thermal: dev-only, or in
                                            # requirements.txt already)


def norm(name: str) -> str:
    """A package name as pip compares them (case, '-', '_', '.' alike)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def roots() -> list[str]:
    """The packages GroundTruth asks for: the plugin's requirements and the shipping extras' (pyproject.toml)."""
    names = []
    for line in (ROOT / "plugin" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(re.split(r"[\s<>=!~\[;]", line, maxsplit=1)[0])
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for extra in EXTRAS:
        m = re.search(rf"^{extra}\s*=\s*\[(.*?)\]", text, re.M)
        if m:
            names += [re.split(r"[\s<>=!~\[;]", x.strip().strip('"'), maxsplit=1)[0] for x in m.group(1).split(",")
                      if x.strip()]
    return list(dict.fromkeys(names))


def licence(d) -> str:
    """A distribution's licence from its metadata: License-Expression, the classifiers, the License field."""
    m = d.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    cls = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
    if cls:
        return "; ".join(cls)
    return ((m.get("License") or "").strip().splitlines() or [""])[0][:80]


def closure() -> list[tuple[str, str, str, str, str]]:
    """[(name, version or "not installed", licence, needed by, home page)] of the roots and their requirements, in
    breadth-first order (a package once, under the first that needs it)."""
    seen, out = set(), []
    todo = [(r, "GroundTruth") for r in roots()]
    while todo:
        name, via = todo.pop(0)
        if norm(name) in seen:
            continue
        seen.add(norm(name))
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            out.append((name, "not installed", "", via, ""))
            continue
        m = d.metadata
        home = m.get("Home-page") or next((u.split(",", 1)[1].strip() for u in m.get_all("Project-URL") or []
                                           if u.lower().startswith(("homepage", "home", "source", "repository"))), "")
        out.append((m["Name"], d.version, licence(d), via, home))
        for req in d.requires or []:
            req, _, marker = req.partition(";")
            if "extra" in marker:                       # an optional extra of theirs: not installed by GroundTruth
                continue
            if "sys_platform" in marker and "win32" not in marker and "!=" not in marker:
                continue                                # another platform's
            todo.append((re.split(r"[\s<>=!~\[(]", req.strip(), maxsplit=1)[0], m["Name"]))
    return out


def unnamed(rows) -> list[str]:
    """The installed packages the help's licence section does not name (matched without case, '-' or '_')."""
    page = (ROOT / "src" / "groundtruth" / "ui" / "help.html").read_text(encoding="utf-8")
    sec = page.split('<a name="licenses">', 1)[1] if '<a name="licenses">' in page else ""
    words = {norm(w) for w in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", re.sub(r"<[^>]+>", " ", sec))}
    return [name for name, version, *_ in rows if version != "not installed" and norm(name) not in words]


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="list the installed packages the help does not name")
    args = ap.parse_args()
    rows = closure()
    if args.check:
        missing = unnamed(rows)
        for name in missing:
            print(f"not in the help: {name}")
        return 1 if missing else 0
    for row in rows:
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
