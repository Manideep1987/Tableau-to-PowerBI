"""AST node types shared by the Tableau formula parser, translator and engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class Node:
    def children(self):
        return []

    def walk(self):
        yield self
        for c in self.children():
            if c is not None:
                yield from c.walk()


@dataclass
class Num(Node):
    value: float


@dataclass
class Str(Node):
    value: str


@dataclass
class Bool(Node):
    value: bool


@dataclass
class Null(Node):
    pass


@dataclass
class DateLit(Node):
    value: str          # ISO text


@dataclass
class Field(Node):
    name: str
    datasource: Optional[str] = None

    @property
    def is_parameter(self) -> bool:
        return (self.datasource or "").lower() == "parameters"


@dataclass
class Func(Node):
    name: str           # upper-case
    args: list = field(default_factory=list)

    def children(self):
        return self.args


@dataclass
class BinOp(Node):
    op: str             # + - * / % ^ = <> < > <= >= AND OR
    left: Node
    right: Node

    def children(self):
        return [self.left, self.right]


@dataclass
class Unary(Node):
    op: str             # - | NOT
    operand: Node

    def children(self):
        return [self.operand]


@dataclass
class If(Node):
    branches: list      # list[(cond, value)]
    else_: Optional[Node] = None

    def children(self):
        out = []
        for c, v in self.branches:
            out += [c, v]
        if self.else_ is not None:
            out.append(self.else_)
        return out


@dataclass
class Case(Node):
    subject: Node
    whens: list         # list[(match, value)]
    else_: Optional[Node] = None

    def children(self):
        out = [self.subject]
        for m, v in self.whens:
            out += [m, v]
        if self.else_ is not None:
            out.append(self.else_)
        return out


@dataclass
class LOD(Node):
    kind: str           # FIXED | INCLUDE | EXCLUDE
    dims: list
    expr: Node

    def children(self):
        return list(self.dims) + [self.expr]


@dataclass
class In(Node):
    subject: Node
    values: list
    negated: bool = False

    def children(self):
        return [self.subject] + list(self.values)


AGGREGATES = {"SUM", "AVG", "COUNT", "COUNTD", "MIN", "MAX", "MEDIAN", "ATTR",
              "STDEV", "STDEVP", "VAR", "VARP", "PERCENTILE", "COLLECT"}

TABLE_CALCS = {"RUNNING_SUM", "RUNNING_AVG", "RUNNING_MIN", "RUNNING_MAX", "RUNNING_COUNT",
               "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT",
               "WINDOW_MEDIAN", "TOTAL", "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_UNIQUE",
               "RANK_PERCENTILE", "INDEX", "FIRST", "LAST", "SIZE", "LOOKUP", "PREVIOUS_VALUE"}


def is_scalar_minmax(node) -> bool:
    """MIN(a, b) / MAX(a, b) with two arguments are scalar functions, not aggregations."""
    return isinstance(node, Func) and node.name in ("MIN", "MAX") and len(node.args) == 2


def is_aggregate(node: Node, calc_lookup=None, _seen=None) -> bool:
    """True when the expression produces an aggregate (measure-level) result.

    ``calc_lookup`` maps a field name to a parsed AST for referenced calcs so that
    references to aggregate calculations are recognised.
    """
    _seen = _seen or set()
    if isinstance(node, LOD):
        return False            # LODs are row-level results
    if isinstance(node, Func):
        if (node.name in AGGREGATES and not is_scalar_minmax(node)) or node.name in TABLE_CALCS:
            return True
    if isinstance(node, Field) and calc_lookup and not node.is_parameter:
        if node.name in _seen:
            return False
        sub = calc_lookup(node.name)
        if sub is not None:
            return is_aggregate(sub, calc_lookup, _seen | {node.name})
        return False
    return any(is_aggregate(c, calc_lookup, _seen) for c in node.children() if c is not None
               and not isinstance(node, LOD))


def contains(node: Node, pred) -> bool:
    return any(pred(n) for n in node.walk())
