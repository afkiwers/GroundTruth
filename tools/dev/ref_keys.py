"""Do the source citations in the code match docs/REFERENCES.md? Every `[Key Year]` / `[Key Year, §x]` / `[A12]`
cited under src/groundtruth or tests must be a key of the list (a source's first column, or an assumption's id),
and every key of the list should be cited somewhere (an unused one is printed as a warning). Exit code 1 on an
unknown key.

    .venv/Scripts/python tools/dev/ref_keys.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# a citation: an author (or standard) and a year, or a standard's name alone, or an assumption id; an optional
# location after a comma ("[Jackson 1999, §5.6]", "[MIL-STD-461G, Table II]")
CITE = re.compile(r"\[((?:[A-Z][\w-]*(?: [\w.-]+)*?)|A\d+)(?:, [^\]]+)?\]")


def keys_in_list() -> set[str]:
    """The keys docs/REFERENCES.md defines: the first column of every table row."""
    keys = set()
    for line in (ROOT / "docs" / "REFERENCES.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("| ") and not line.startswith(("| Key", "| Id", "|---")):
            keys.add(line.split("|")[1].strip())
    return keys


def cited() -> dict[str, list[str]]:
    """Every key cited in the code: key -> the places ("file:line")."""
    known = keys_in_list()
    out: dict[str, list[str]] = {}
    for path in sorted([*(ROOT / "src" / "groundtruth").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in CITE.finditer(line):
                key = m.group(1)
                # a bracket that is not a citation (an index, a list, a matrix) has no year and no known key
                if key in known or re.search(r"\b(1[89]|20)\d\d\b", key) or re.fullmatch(r"A\d+", key):
                    out.setdefault(key, []).append(f"{path.relative_to(ROOT)}:{n}")
    return out


def main() -> int:
    """Print unknown keys (an error) and unused ones (a warning)."""
    known, used = keys_in_list(), cited()
    unknown = {k: v for k, v in used.items() if k not in known}
    for k, places in sorted(unknown.items()):
        print(f"unknown key [{k}]: {', '.join(places[:3])}")
    for k in sorted(known - set(used)):
        print(f"not cited in the code: [{k}]")
    print(f"{len(used)} keys cited, {len(known)} in docs/REFERENCES.md, {len(unknown)} unknown")
    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
