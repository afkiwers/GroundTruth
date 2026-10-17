"""Copy a board and give parts a GT field (detection labels, analysis/tags.py), as if set on their symbols: for
trying labels on a real board without touching it.

    .venv/Scripts/python tools/dev/add_labels.py BOARD.kicad_pcb OUT.kicad_pcb Q1=hb_hs Q2=hb_ls J1=hb_out ...

A label for a part that already has a GT field replaces it. The copy goes to OUT (never next to the board unless
asked); the board itself is only read.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def _drop_gt(block: str) -> str:
    """A footprint block without its GT properties. Each one's end is found by counting parentheses (quoted
    strings skipped): a property KiCad or label_project.py wrote nests (effects (font (size ...))) three deep, which
    the regex used before did not match, so the old label stayed and was read instead of the new one."""
    while True:
        m = re.search(r'\n[ \t]*\(property "GT" ', block)
        if not m:
            return block
        depth, i, quoted = 0, m.start() + 1, False
        while i < len(block):
            c = block[i]
            if quoted:
                if c == "\\":
                    i += 1
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = block[:m.start()] + block[i + 1:]


def add_labels(text: str, labels: dict[str, str]) -> tuple[str, list[str]]:
    """The board text with a (property "GT" ...) in each labelled footprint; the references not found."""
    done = set()

    def one(m: re.Match) -> str:
        """One footprint block: when its reference is to be labelled, drop any GT property it has and add the new
        one (hidden, on F.Fab) before its Reference property."""
        block = m.group(0)
        ref = re.search(r'\(property "Reference" "([^"]*)"', block)
        if not ref or ref.group(1) not in labels:
            return block
        done.add(ref.group(1))
        block = _drop_gt(block)
        value = labels[ref.group(1)].replace('"', "'")
        return re.sub(r'(\(property "Reference" "[^"]*")',
                      lambda r: f'(property "GT" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.Fab")\n\t\t\t(hide yes)\n'
                                f'\t\t)\n\t\t{r.group(1)}', block, count=1)
    # a footprint block: from "(footprint" to the next one at the same indent (or the end of the board's items)
    out = re.sub(r'\t\(footprint .*?(?=\n\t\((?:footprint|gr_|segment|via|zone|arc|group|embedded|dimension|'
                 r'target|image|generated|table)|\n\)\s*$)', one, text, flags=re.S)
    return out, sorted(set(labels) - done)


def main(argv: list[str]) -> int:
    """The command line: BOARD OUT ref=label ...; refuses to overwrite the source board, copies the .kicad_pro
    beside the copy and names references it did not find."""
    if len(argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(argv[0]), Path(argv[1])
    if dst.resolve() == src.resolve():
        print("refusing to overwrite the board itself")
        return 2
    labels = dict(a.split("=", 1) for a in argv[2:])
    text, missing = add_labels(src.read_text(encoding="utf-8"), labels)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    pro = src.with_suffix(".kicad_pro")
    if pro.exists() and not dst.with_suffix(".kicad_pro").exists():
        dst.with_suffix(".kicad_pro").write_bytes(pro.read_bytes())
    print(f"{dst}: {len(labels) - len(missing)} part(s) labelled" + (f"; not found: {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
