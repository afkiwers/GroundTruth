"""Functions and methods without a docstring, per file, with their line numbers and lengths: the to-do list of a
documentation pass.

    .venv/Scripts/python tools/dev/undocumented.py src/groundtruth/config/settings.py [more files or folders]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def undocumented(path: Path) -> list[tuple[int, int, str]]:
    """(line, number of lines, qualified name) of every function in the file without a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def visit(node, prefix=""):
        """Walk the tree, recording functions (with their class and outer function in the name) that have no
        docstring."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                if not ast.get_docstring(child):
                    out.append((child.lineno, child.end_lineno - child.lineno + 1, name))
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:           # a function defined inside a loop, an if or a with block is found too
                visit(child, prefix)
    visit(tree)
    return out


def main(argv: list[str]) -> int:
    """The command line: files or folders (default src/groundtruth); prints each file's list and the total."""
    paths = []
    for a in argv or ["src/groundtruth"]:
        p = Path(a)
        paths += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    total = 0
    for p in paths:
        rows = undocumented(p)
        total += len(rows)
        if rows:
            print(f"{p}: {len(rows)}")
            for line, n, name in rows:
                print(f"  {line:5d} ({n:3d} lines) {name}")
    print(f"{total} without a docstring")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
