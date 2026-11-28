"""BlueStacks control conditions: `StartCondition` / `EnableCondition` and the like.

A condition is a boolean expression over identifiers, for example
`(WiFi1 || Tutorial) && !(Settings || (Store && !Quicksetting))`.
- Identifiers are a scheme's image ids (things the game shows, matched by texture CRC) or
  states set by `State` controls and scripts.
- An empty condition is always true.

Evaluation is three-valued (Kleene logic):
- `lookup(name)` returns True, False or None for unknown.
- Images are unknown while the renderer cannot detect them (imgd.py); with detection an
  image is true while it is on screen.
- The caller decides what an unknown result means (by default, enabled).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Callable, Optional

Tri = Optional[bool]
_TOKEN = re.compile(r"\s*(?:(\|\||&&|[!()+\-*/<>=]=?)|([A-Za-z_][A-Za-z0-9_.]*)|(\d+(?:\.\d+)?))")


class ExprError(ValueError):
    pass


def _tokens(src: str) -> list[str]:
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ExprError(f"bad character at {pos}: {src[pos:pos + 10]!r}")
        out.append(m.group(1) or m.group(2) or m.group(3))
        pos = m.end()
    return out


@lru_cache(maxsize=4096)
def compile_cond(src: str):
    """Parse once into a nested tuple tree: ('or', a, b), ('and', a, b), ('not', a),
    ('id', name), ('num', v), ('arith', ...). An empty string compiles to None."""
    if not src or not src.strip():
        return None
    toks = _tokens(src)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take(t=None):
        nonlocal pos
        tok = peek()
        if tok is None or (t is not None and tok != t):
            raise ExprError(f"expected {t or 'a term'} in {src!r}")
        pos += 1
        return tok

    def parse_or():
        node = parse_and()
        while peek() == "||":
            take()
            node = ("or", node, parse_and())
        return node

    def parse_and():
        node = parse_not()
        while peek() == "&&":
            take()
            node = ("and", node, parse_not())
        return node

    def parse_not():
        if peek() == "!":
            take()
            return ("not", parse_not())
        return parse_arith()

    def parse_arith():
        node = parse_atom()
        while peek() in ("+", "-", "*", "/", "<", ">", "<=", ">=", "==", "!="):
            op = take()
            node = ("arith", op, node, parse_atom())
        return node

    def parse_atom():
        tok = peek()
        if tok == "(":
            take()
            node = parse_or()
            take(")")
            return node
        tok = take()
        if re.match(r"\d", tok):
            return ("num", float(tok))
        if not re.match(r"[A-Za-z_]", tok):
            raise ExprError(f"unexpected {tok!r} in {src!r}")
        return ("id", tok)

    tree = parse_or()
    if pos != len(toks):
        raise ExprError(f"trailing {toks[pos]!r} in {src!r}")
    return tree


def evaluate(src: str, lookup: Callable[[str], Tri]) -> Tri:
    """Kleene-logic value of a condition (None = cannot tell); empty = True."""
    try:
        tree = compile_cond(src)
    except ExprError:
        return None
    if tree is None:
        return True
    return _eval(tree, lookup)


def identifiers(src: str) -> set[str]:
    try:
        tree = compile_cond(src)
    except ExprError:
        return set()
    out: set[str] = set()

    def walk(n):
        if n is None:
            return
        if n[0] == "id":
            out.add(n[1])
        else:
            for c in n[1:]:
                if isinstance(c, tuple):
                    walk(c)
    walk(tree)
    return out


def _eval(n, lookup) -> Tri:
    kind = n[0]
    if kind == "id":
        return lookup(n[1])
    if kind == "num":
        return n[1] != 0
    if kind == "not":
        v = _eval(n[1], lookup)
        return None if v is None else not v
    if kind == "and":
        a, b = _eval(n[1], lookup), _eval(n[2], lookup)
        if a is False or b is False:
            return False
        return None if a is None or b is None else True
    if kind == "or":
        a, b = _eval(n[1], lookup), _eval(n[2], lookup)
        if a is True or b is True:
            return True
        return None if a is None or b is None else False
    return None                      # arithmetic: positions of detected images (Smart)
