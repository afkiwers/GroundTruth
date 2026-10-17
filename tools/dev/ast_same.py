"""Do the Python files differ only in comments and docstrings? For documentation passes: every changed file is
parsed (old from git, new from disk), its docstrings removed, and the two syntax trees compared. Comments never
reach the tree, so a pure documentation change gives identical trees; anything else is printed and the exit code
is 1.

    .venv/Scripts/python tools/dev/ast_same.py [REV]        # changed .py files against REV (default HEAD)
    .venv/Scripts/python tools/dev/ast_same.py main src/groundtruth/solver/peec.py ...
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """The tree without its docstrings: a string constant as the first statement of a module, class or function
    is dropped (an empty body keeps a `pass`, so the tree stays valid)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def same(old_src: str, new_src: str) -> bool:
    """True when the two sources are the same program apart from comments, docstrings and layout."""
    dump = lambda s: ast.dump(strip_docstrings(ast.parse(s)), include_attributes=False)
    return dump(old_src) == dump(new_src)


def main(argv: list[str]) -> int:
    """The command line: [REV] [FILES]; without files, every tracked .py file that differs from REV (HEAD). Exit
    code 1 when any file's code changed."""
    rev = argv[0] if argv and not argv[0].endswith(".py") else "HEAD"
    files = [a for a in argv if a.endswith(".py")]
    if not files:          # every .py file that differs from the revision (tracked ones)
        out = subprocess.run(["git", "diff", "--name-only", rev, "--", "*.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
        files = [f for f in out.split() if f]
    bad = 0
    for f in files:
        new = (ROOT / f).read_text(encoding="utf-8")
        old = subprocess.run(["git", "show", f"{rev}:{f}"], cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8").stdout
        if not same(old, new):
            print(f"CODE CHANGED: {f}")
            bad += 1
    print(f"{len(files)} file(s) compared against {rev}: "
          + ("only comments and docstrings changed" if not bad else f"{bad} with code changes"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
