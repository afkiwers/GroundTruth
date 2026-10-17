"""Minimal, fast S-expression reader for KiCad files.

Produces nested Python lists; atoms are str (quoted strings are unquoted).
A node ``(at 1 2 90)`` becomes ``["at", "1", "2", "90"]``.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse(text: str) -> list:
    """The file's tree. A regex tokeniser (parentheses, quoted strings with escapes, bare atoms) and a stack of
    the open lists: "(" opens a list inside the current one, ")" closes it."""
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("Unbalanced ')' in S-expression")
            stack.pop()
        elif tok[0] == '"':
            stack[-1].append(tok[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("Unbalanced '(' in S-expression")
    root = stack[0]
    return root[0] if len(root) == 1 else root


# ---- helpers for navigating nodes -------------------------------------------

def children(node: list, name: str):
    """Yield child nodes whose head is ``name``."""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            yield c


def child(node: list, name: str):
    """The first child node named `name`, None without one."""
    for c in children(node, name):
        return c
    return None


def value(node: list, name: str, default=None, idx: int = 1):
    """Atom `idx` of the child named `name` ("(width 0.2)" -> "0.2"), `default` without it."""
    c = child(node, name)
    if c is None or len(c) <= idx:
        return default
    return c[idx]


def fvalue(node: list, name: str, default: float | None = None, idx: int = 1):
    """value() as a float."""
    v = value(node, name, None, idx)
    return default if v is None else float(v)


def xy(node: list | None, default=(0.0, 0.0)):
    """An "(at x y ...)" style node's first two numbers."""
    if node is None:
        return default
    return float(node[1]), float(node[2])


def pts(node: list | None) -> list[tuple[float, float]]:
    """Points of a ``(pts (xy ..) (xy ..) (arc ..))`` node; arcs are tessellated."""
    from .geometry_utils import arc_points  # local import avoids cycle

    out: list[tuple[float, float]] = []
    if node is None:
        return out
    for c in node[1:]:
        if not isinstance(c, list):
            continue
        if c[0] == "xy":
            out.append((float(c[1]), float(c[2])))
        elif c[0] == "arc":
            s = xy(child(c, "start"))
            m = xy(child(c, "mid"))
            e = xy(child(c, "end"))
            p = arc_points(s, m, e)
            if out and p and abs(out[-1][0] - p[0][0]) < 1e-9 and abs(out[-1][1] - p[0][1]) < 1e-9:
                p = p[1:]
            out.extend(p)
    return out
