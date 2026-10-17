"""Minimal S-expression reader for KiCad files.

Lists become `SList` (a list subclass carrying the 1-based source line of its opening
parenthesis), atoms and quoted strings become `str`.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Iterator, Optional, Union

_TOKEN = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+')
_ESCAPES = re.compile(r"\\(.)")


class SList(list):
    """A parenthesised list of the file: its items are atoms (str) and nested SLists; `line` is where it starts
    (issues point CI reports at that line). The helpers look up child lists by their first atom, the tag."""
    __slots__ = ("line",)

    def __init__(self, line: int = 0):
        """An empty list that opened on source line `line`."""
        super().__init__()
        self.line = line

    @property
    def tag(self) -> str:
        """The list's first atom ("footprint", "segment", ...), "" when it has none."""
        return self[0] if self and isinstance(self[0], str) else ""

    def find(self, tag: str) -> Optional["SList"]:
        """First direct child list with the given tag."""
        for item in self:
            if isinstance(item, SList) and item and item[0] == tag:
                return item
        return None

    def find_all(self, tag: str) -> Iterator["SList"]:
        """Every direct child list with the given tag, in order."""
        for item in self:
            if isinstance(item, SList) and item and item[0] == tag:
                yield item

    def value(self, tag: str, default=None, index: int = 1):
        """Atom at `index` of the first child list `tag` (e.g. value("width") -> "0.2")."""
        child = self.find(tag)
        if child is None or len(child) <= index or isinstance(child[index], SList):
            return default
        return child[index]

    def float(self, tag: str, default: float = 0.0) -> float:
        """value(tag) as a float ("(width 0.2)" -> 0.2), `default` without it."""
        v = self.value(tag)
        return float(v) if v is not None else default

    def xy(self, tag: str, default=(0.0, 0.0)) -> tuple[float, ...]:
        """Numeric atoms of child list `tag`, e.g. (at 1 2 90) -> (1.0, 2.0, 90.0)."""
        child = self.find(tag)
        if child is None:
            return default
        return tuple(float(v) for v in child[1:] if not isinstance(v, SList))

    def atoms(self) -> list[str]:
        """The list's atoms after its tag (nested lists left out)."""
        return [v for v in self[1:] if not isinstance(v, SList)]


SExpr = Union[SList, str]


class SExprError(ValueError):
    """Text that is not one well-formed S-expression (unbalanced parentheses, stray atoms)."""
    pass


def block_end(text: str, start: int) -> int:
    """Index just past the ')' closing the list that opens at text[start] ('('), or -1. Quoted strings may
    contain parentheses; they are skipped like in `parse`."""
    depth = 0
    for m in _TOKEN.finditer(text, start):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                return m.end()
    return -1


def parse(text: str) -> SList:
    """Parse one top-level S-expression."""
    # A tokeniser (the regex) and a stack of the open lists: "(" opens a list inside the current one, ")" closes
    # it, an atom is appended. A list's line number is found by binary search in the newline offsets.
    newlines = [m.start() for m in re.finditer("\n", text)]
    stack: list[SList] = []
    root: Optional[SList] = None
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            node = SList(bisect_right(newlines, m.start()) + 1)
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if not stack:
                raise SExprError(f"unbalanced ')' at offset {m.start()}")
            node = stack.pop()
            if not stack:
                root = node
                break
        else:
            if not stack:
                raise SExprError(f"atom outside of list at offset {m.start()}")
            quoted = m.group(1)
            if quoted is not None:
                stack[-1].append(_ESCAPES.sub(lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), quoted)
                                 if "\\" in quoted else quoted)
            else:
                stack[-1].append(tok)
    if stack or root is None:
        raise SExprError("unexpected end of input (unbalanced parentheses)")
    return root


def dumps(expr: SExpr) -> str:
    """Serialise back to text (used by tests and the synthetic board generator)."""
    if isinstance(expr, (SList, list)):
        return "(" + " ".join(dumps(e) for e in expr) + ")"
    s = str(expr)
    if s == "" or re.search(r'[\s()"]', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s
