"""Set GT detection labels (analysis/tags.py) in a KiCad project, in place: a hidden `GT` field on each symbol in the
.kicad_sch files and the same property on its footprint in the .kicad_pcb, as Update PCB from Schematic would
write it. Only when the user asked for their project to be changed.

    .venv/Scripts/python tools/dev/label_project.py PROJECT_DIR --backup DIR Q301=conv1_hs Q302=conv1_ls ...
    .venv/Scripts/python tools/dev/label_project.py PROJECT_DIR --backup DIR --remove Q301 Q302 ...
    .venv/Scripts/python tools/dev/label_project.py PROJECT_DIR --backup DIR --field "EA gm" U303=1.8mmho

--field sets another hidden field instead of GT (a controller's loop figures, components/fields.py: "Control Mode",
"Vref", "EA gm", ...), the same way.

Refuses while KiCad has the project open (a ~*.lck file next to it: KiCad would overwrite the change on its next
save). Copies every file it changes into --backup first. A label on a part that already has a GT field replaces it.
Works only on symbols whose own Reference property names them (a sheet used once); for a reused sheet the field is
shared by all copies anyway, so label one copy's reference and every copy gets it. Line endings are kept.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import uuid
from pathlib import Path


def _blocks(text: str, head: str):
    """(start, end) of every top-level block starting with `head` at one tab of indent (a schematic's placed
    symbols, a board's footprints), found by balancing parentheses outside strings."""
    for m in re.finditer(r"(?m)^\t\(" + head + r"\b", text):
        i, depth, in_str = m.start() + 1, 0, False
        while i < len(text):
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    yield m.start(), i + 1
                    break
            i += 1


def _prop_span(block: str, name: str):
    """(start, end) of a property block `(property "name" ...)` inside a symbol / footprint, from its line start."""
    m = re.search(r'(?m)^\t\t\(property "' + re.escape(name) + r'" ', block)
    if not m:
        return None
    depth, i, in_str = 0, m.start() + 2, False
    while i < len(block):
        c = block[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
        i += 1
    return None


def _ref(block: str) -> str:
    """The Reference property of a symbol or footprint block."""
    m = re.search(r'\(property "Reference" "([^"]*)"', block)
    return m.group(1) if m else ""


def _set_field(block: str, value: str | None, nl: str, kind: str, name: str = "GT") -> str:
    """The block with its property `name` (GT) set to value (None: removed)."""
    span = _prop_span(block, name)
    if span:
        end = span[1] + (len(nl) if block[span[1]:span[1] + len(nl)] == nl else 0)
        block = block[:span[0]] + block[end:]
    if value is None:
        return block
    value = value.replace('"', "'")
    if kind == "symbol":
        at = re.search(r"\n\t\t\(at ([^)]*)\)", block)
        pos = at.group(1) if at else "0 0 0"
        prop = (f'\t\t(property "{name}" "{value}"{nl}\t\t\t(at {pos}){nl}\t\t\t(hide yes){nl}\t\t\t(show_name no){nl}'
                f'\t\t\t(do_not_autoplace no){nl}\t\t\t(effects{nl}\t\t\t\t(font{nl}\t\t\t\t\t(size 1.27 1.27){nl}'
                f'\t\t\t\t){nl}\t\t\t){nl}\t\t){nl}')
        anchor = _prop_span(block, "Reference")
    else:
        # a copy of the footprint's own hidden Description (or Datasheet) property: its layer, angle and font
        anchor = _prop_span(block, "Description") or _prop_span(block, "Datasheet") or _prop_span(block, "Value")
        tmpl = block[anchor[0]:anchor[1]]
        tmpl = re.sub(r'^\t\t\(property "[^"]*" "(?:[^"\\]|\\.)*"', f'\t\t(property "{name}" "{value}"', tmpl)
        tmpl = re.sub(r'\(uuid "[^"]*"\)', f'(uuid "{uuid.uuid4()}")', tmpl)
        if "(hide yes)" not in tmpl:
            tmpl = tmpl.replace(f"{nl}\t\t\t(uuid", f"{nl}\t\t\t(hide yes){nl}\t\t\t(uuid", 1)
        prop = tmpl + nl
    end = anchor[1] + (len(nl) if block[anchor[1]:anchor[1] + len(nl)] == nl else 0)
    return block[:end] + prop + block[end:]


def label_text(text: str, labels: dict[str, str | None], head: str, kind: str,
               name: str = "GT") -> tuple[str, set[str]]:
    """A file's text with the field `name` (GT) set (or removed) on every block whose reference is labelled, keeping
    the file's line ending; also returns the references it changed."""
    nl = "\r\n" if "\r\n" in text[:2000] else "\n"
    out, last, done = [], 0, set()
    for a, b in _blocks(text, head):
        block = text[a:b]
        ref = _ref(block)
        if ref in labels:
            out.append(text[last:a])
            out.append(_set_field(block, labels[ref], nl, kind, name))
            last = b
            done.add(ref)
    out.append(text[last:])
    return "".join(out), done


def main(argv: list[str]) -> int:
    """The command line (see the module): refuses while KiCad has the project open, copies every file it changes
    into --backup first, then writes the labels into the schematic sheets and the board."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path)
    ap.add_argument("--backup", type=Path, required=True, help="folder for copies of the files before the change")
    ap.add_argument("--remove", action="store_true", help="remove the field of the parts listed")
    ap.add_argument("--field", default="GT", help="the field to set (default GT, the detection label)")
    ap.add_argument("labels", nargs="+", help="REF=LABEL (or REF with --remove)")
    args = ap.parse_args(argv)
    proj = args.project
    if list(proj.glob("~*.lck")) or list(proj.glob("*.lck")):
        print(f"{proj} is open in KiCad (lock files): close it first", file=sys.stderr)
        return 1
    labels = {a: None for a in args.labels} if args.remove else dict(a.split("=", 1) for a in args.labels)
    files = sorted(proj.glob("*.kicad_sch")) + sorted(proj.glob("*.kicad_pcb"))
    args.backup.mkdir(parents=True, exist_ok=True)
    found: dict[str, set] = {"symbol": set(), "footprint": set()}
    for f in files:
        kind, head = ("symbol", "symbol") if f.suffix == ".kicad_sch" else ("footprint", "footprint")
        raw = f.read_bytes().decode("utf-8")
        new, done = label_text(raw, labels, head, kind, args.field)
        if new != raw:
            if not (args.backup / f.name).exists():
                shutil.copy2(f, args.backup / f.name)
            f.write_bytes(new.encode("utf-8"))
            print(f"{f.name}: {', '.join(sorted(done))}")
        found[kind] |= done
    for kind in ("symbol", "footprint"):
        missing = sorted(set(labels) - found[kind])
        if missing:
            print(f"no {kind} found for: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
