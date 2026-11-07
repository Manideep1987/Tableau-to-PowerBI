"""Tableau calculation AST -> DAX translator.

Two translation modes:

* ``row``  - the calculation is row-level and becomes a DAX *calculated column* on a
  host table.  Columns from related (one-side) tables are reached with RELATED().
* ``agg``  - the calculation is aggregate and becomes a DAX *measure*.  Aggregations
  of expressions become iterator functions (SUMX, AVERAGEX, ...).

Semantic differences that change numbers are handled explicitly:

* Tableau NULL propagates through ``+``/``-``/string concatenation and makes
  comparisons unknown; DAX BLANK behaves as 0 / "" there.  When an operand may be
  NULL the translator emits null-preserving guards.
* COUNTD ignores NULL -> DISTINCTCOUNTNOBLANK.
* Division by zero returns NULL in Tableau -> DIVIDE().
* INT truncates toward zero -> TRUNC.
* FIXED LODs ignore dimension filters -> ALLEXCEPT / REMOVEFILTERS.
* Table calculations depend on the view -> window functions over ALLSELECTED on the
  addressing fields of the worksheet that uses them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .ast import (AGGREGATES, TABLE_CALCS, Bool, BinOp, Case, DateLit, Field, Func, If, In,
                  LOD, Node, Null, Num, Str, Unary, is_aggregate, is_scalar_minmax)
from .parser import parse_formula


class TranslationError(Exception):
    pass


# ------------------------------------------------------------ resolver API --
@dataclass
class ColInfo:
    table: str
    column: str
    datatype: str = "string"
    nullable: bool = False


@dataclass
class CalcInfo:
    kind: str                 # measure | column
    table: str                # host table (column) or measure table
    name: str                 # PBI object name
    datatype: str = "real"
    ast: Optional[Node] = None
    nullable: bool = False


@dataclass
class ParamInfo:
    measure: str              # name of the "<param> Value" measure
    table: str
    column: str
    datatype: str
    value: object = None


class Resolver:
    """Interface the converter implements to map Tableau names to the PBI model."""
    fact_table: str = "Fact"
    measure_table: str = "_Measures"

    def column(self, name: str) -> Optional[ColInfo]:
        raise NotImplementedError

    def calc(self, name: str) -> Optional[CalcInfo]:
        raise NotImplementedError

    def parameter(self, name: str) -> Optional[ParamInfo]:
        raise NotImplementedError

    def is_one_side(self, host: str, other: str) -> bool:
        """True when ``other`` is on the one-side of a relationship chain from ``host``."""
        return host != other


@dataclass
class DaxResult:
    expression: str
    mode: str
    datatype: str = "real"
    warnings: list = field(default_factory=list)
    functions: set = field(default_factory=set)
    confidence: str = "high"            # high | medium | low
    host_table: Optional[str] = None

    @property
    def status(self) -> str:
        if self.confidence == "low":
            return "Manual review"
        if self.warnings:
            return "Converted with notes"
        return "Converted"


# ------------------------------------------------------------ helpers --
def q_table(t: str) -> str:
    return "'" + t.replace("'", "''") + "'"


def q_col(t: str, c: str) -> str:
    return f"{q_table(t)}[{c.replace(']', ']]')}]"


def q_measure(m: str) -> str:
    return "[" + m.replace("]", "]]") + "]"


def dax_str(s: str) -> str:
    return '"' + str(s).replace('"', '""') + '"'


def dax_num(v) -> str:
    if isinstance(v, bool):
        return "TRUE()" if v else "FALSE()"
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) < 1e15 else repr(v)
    return repr(v) if isinstance(v, float) else str(v)


def dax_literal(v) -> str:
    if v is None:
        return "BLANK()"
    if isinstance(v, bool):
        return "TRUE()" if v else "FALSE()"
    if isinstance(v, (int, float)):
        return dax_num(v)
    return dax_str(v)


DATE_PART_UNITS = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK",
                   "day": "DAY", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
                   "dayofyear": "DAY", "weekday": "DAY", "iso-week": "WEEK"}


@dataclass
class _Ctx:
    mode: str                           # row | agg
    host: Optional[str] = None          # host table for row mode
    addressing: list = field(default_factory=list)   # list[(table, column)] for table calcs
    vars: list = field(default_factory=list)          # collected VAR definitions
    in_iter: Optional[str] = None       # table currently iterated (row context inside X-functions)

    def child(self, **kw):
        c = _Ctx(self.mode, self.host, self.addressing, self.vars, self.in_iter)
        for k, v in kw.items():
            setattr(c, k, v)
        return c


class DaxTranslator:
    def __init__(self, resolver: Resolver):
        self.r = resolver
        self.warnings: list[str] = []
        self.functions: set[str] = set()
        self.confidence = "high"
        self._var_n = 0

    # ---------------------------------------------------------- public --
    def translate(self, formula_or_ast, mode: str = "agg", host: Optional[str] = None,
                  addressing: Optional[list] = None) -> DaxResult:
        self.warnings, self.functions, self.confidence, self._var_n = [], set(), "high", 0
        ast = parse_formula(formula_or_ast) if isinstance(formula_or_ast, str) else formula_or_ast
        ctx = _Ctx(mode, host or (self.r.fact_table if mode == "row" else None), addressing or [])
        expr, typ = self.tr(ast, ctx)
        if ctx.vars:
            lines = [f"VAR {n} = {e}" for n, e in ctx.vars]
            expr = "\n".join(lines) + "\nRETURN\n    " + expr
        return DaxResult(expr, mode, typ, list(dict.fromkeys(self.warnings)), set(self.functions),
                         self.confidence, ctx.host)

    # ------------------------------------------------------------ core --
    def warn(self, msg, level="medium"):
        self.warnings.append(msg)
        order = {"high": 0, "medium": 1, "low": 2}
        if order[level] > order[self.confidence]:
            self.confidence = level

    def fn(self, name):
        self.functions.add(name)
        return name

    def tr(self, n: Node, ctx: _Ctx):
        m = getattr(self, "t_" + type(n).__name__)
        return m(n, ctx)

    def t_Num(self, n, ctx):
        return dax_num(n.value), "real" if isinstance(n.value, float) else "integer"

    def t_Str(self, n, ctx):
        return dax_str(n.value), "string"

    def t_Bool(self, n, ctx):
        return ("TRUE()" if n.value else "FALSE()"), "boolean"

    def t_Null(self, n, ctx):
        return "BLANK()", "null"

    def t_DateLit(self, n, ctx):
        txt = n.value.strip()
        date_part, _, time_part = txt.partition(" ")
        try:
            y, mo, d = [int(x) for x in date_part.split("-")]
        except ValueError:
            self.warn(f"Unrecognised date literal #{txt}#", "low")
            return f"DATEVALUE({dax_str(txt)})", "date"
        if time_part:
            hh, mm, *ss = (time_part.split(":") + ["0"])[:3]
            return f"DATE({y}, {mo}, {d}) + TIME({int(hh)}, {int(mm)}, {int(float(ss[0]))})", "datetime"
        return f"DATE({y}, {mo}, {d})", "date"

    # ---------------------------------------------------------- fields --
    def t_Field(self, n: Field, ctx: _Ctx):
        if n.is_parameter:
            p = self.r.parameter(n.name)
            if p is None:
                raise TranslationError(f"Unknown parameter [{n.name}]")
            if ctx.mode == "row":
                self.warn(f"Calculated column uses parameter '{p.table}': it is evaluated at refresh "
                          f"with the default value. Use a measure for interactive behaviour.")
            return q_measure(p.measure), p.datatype
        c = self.r.calc(n.name)
        if c is not None:
            if c.kind == "measure":
                if ctx.mode == "row" and ctx.in_iter is None:
                    raise TranslationError(
                        f"Row-level expression references aggregate calculation [{n.name}]")
                return q_measure(c.name), c.datatype
            return self._column_ref(c.table, c.name, c.datatype, ctx), c.datatype
        col = self.r.column(n.name)
        if col is None:
            raise TranslationError(f"Unknown field [{n.name}]")
        if ctx.mode == "agg" and ctx.in_iter is None:
            # a bare column in measure context: Tableau would reject it, DAX needs an aggregate
            self.warn(f"Field [{n.name}] used without aggregation; wrapped in SELECTEDVALUE().")
            return f"{self.fn('SELECTEDVALUE')}({q_col(col.table, col.column)})", col.datatype
        return self._column_ref(col.table, col.column, col.datatype, ctx), col.datatype

    def _column_ref(self, table, column, datatype, ctx: _Ctx):
        ref = q_col(table, column)
        current = ctx.in_iter or ctx.host
        if current and table != current:
            if self.r.is_one_side(current, table):
                return f"{self.fn('RELATED')}({ref})"
            self.warn(f"Column {ref} is on the many-side of {q_table(current)}; aggregated with "
                      f"MAXX(RELATEDTABLE()).", "low")
            return f"MAXX(RELATEDTABLE({q_table(table)}), {ref})"
        return ref

    # -------------------------------------------------------- nullness --
    def may_be_null(self, n: Node) -> bool:
        if isinstance(n, Field):
            if n.is_parameter:
                return False
            c = self.r.calc(n.name)
            if c is not None:
                return c.nullable
            col = self.r.column(n.name)
            return bool(col and col.nullable)
        if isinstance(n, Null):
            return True
        if isinstance(n, In):
            return self.may_be_null(n.subject)
        if isinstance(n, Func):
            if n.name in ("ZN", "ISNULL", "COUNT", "COUNTD", "SIZE", "INDEX", "TODAY", "NOW", "PI", "RANK",
                          "RANK_DENSE", "RANK_UNIQUE", "RANK_MODIFIED", "FIRST", "LAST"):
                return False
            if n.name == "IFNULL":
                return self.may_be_null(n.args[1]) if len(n.args) > 1 else False
            if n.name == "IIF":
                return any(self.may_be_null(a) for a in n.args)
            return any(self.may_be_null(a) for a in n.args)
        if isinstance(n, If):
            return n.else_ is None or any(self.may_be_null(v) for _, v in n.branches) or self.may_be_null(n.else_)
        if isinstance(n, Case):
            return n.else_ is None or any(self.may_be_null(v) for _, v in n.whens)
        if isinstance(n, BinOp) and n.op in ("/", "%"):
            # x / 0 is NULL in Tableau even when x and the divisor are never NULL
            if not (isinstance(n.right, Num) and n.right.value != 0):
                return True
        if isinstance(n, (BinOp, Unary)):
            return any(self.may_be_null(c) for c in n.children())
        return False

    # ------------------------------------------------------- operators --
    def t_BinOp(self, n: BinOp, ctx: _Ctx):
        op = n.op
        if op in ("AND", "OR"):
            a, _ = self.tr(n.left, ctx)
            b, _ = self.tr(n.right, ctx)
            if not (self.may_be_null(n.left) or self.may_be_null(n.right)):
                return f"({a} {'&&' if op == 'AND' else '||'} {b})", "boolean"
            # Tableau three-valued logic: NULL AND FALSE = FALSE, NULL AND TRUE = NULL,
            # NULL OR TRUE = TRUE, NULL OR FALSE = NULL (DAX treats BLANK as FALSE).
            self.functions.update({"ISBLANK", "IF"})
            if op == "AND":
                return (f"IF((NOT ISBLANK({a}) && NOT({a})) || (NOT ISBLANK({b}) && NOT({b})), FALSE(), "
                        f"IF(ISBLANK({a}) || ISBLANK({b}), BLANK(), TRUE()))"), "boolean"
            return (f"IF((NOT ISBLANK({a}) && {a}) || (NOT ISBLANK({b}) && {b}), TRUE(), "
                    f"IF(ISBLANK({a}) || ISBLANK({b}), BLANK(), FALSE()))"), "boolean"
        a, ta = self.tr(n.left, ctx)
        b, tb = self.tr(n.right, ctx)
        # Tableau NULL propagates at row level and at aggregate level (e.g. COUNTD(x) + MIN(null))
        nullable = self.may_be_null(n.left) or self.may_be_null(n.right)
        guard_terms = [x for x, node in ((a, n.left), (b, n.right)) if self.may_be_null(node)]
        if op == "+" and ("string" in (ta, tb)):
            expr = f"{a} & {b}"
            if nullable:
                expr = self._null_guard(guard_terms, expr)
            return expr, "string"
        if op == "/":
            return f"{self.fn('DIVIDE')}({a}, {b})", "real"
        if op == "%":
            return f"{self.fn('MOD')}({a}, {b})", "real"
        if op == "^":
            return f"{self.fn('POWER')}({a}, {b})", "real"
        if op in ("+", "-"):
            if ta in ("date", "datetime") and tb in ("date", "datetime") and op == "-":
                expr = f"({a} - {b})"
                rtype = "real"
            else:
                expr = f"({a} {op} {b})"
                rtype = ta if ta in ("date", "datetime") else (
                    "real" if "real" in (ta, tb) else ("integer" if ta == tb == "integer" else "real"))
            if nullable:
                expr = self._null_guard(guard_terms, expr)
            return expr, rtype
        if op == "*":
            return f"({a} * {b})", "real" if "real" in (ta, tb) else ta
        if op in ("=", "<>", "<", ">", "<=", ">="):
            expr = f"{a} {op} {b}"
            if isinstance(n.right, Null) or isinstance(n.left, Null):
                other = b if isinstance(n.left, Null) else a
                expr = f"{'NOT ' if op == '<>' else ''}ISBLANK({other})"
                return expr, "boolean"
            if nullable:
                # Tableau: a comparison with NULL is NULL (unknown), not FALSE
                expr = self._null_guard(guard_terms, f"({expr})")
            else:
                expr = f"({expr})"
            return expr, "boolean"
        raise TranslationError(f"Unsupported operator {op}")

    def _null_guard(self, terms, expr):
        if not terms:
            return expr
        cond = " || ".join(f"ISBLANK({t})" for t in terms)
        self.functions.add("ISBLANK")
        return f"IF({cond}, BLANK(), {expr})"

    def t_Unary(self, n: Unary, ctx):
        v, t = self.tr(n.operand, ctx)
        if n.op == "NOT":
            if self.may_be_null(n.operand):          # NOT NULL is NULL in Tableau, TRUE in DAX
                return self._null_guard([v], f"NOT({v})"), "boolean"
            return f"NOT({v})", "boolean"
        return f"-{v}" if v[0] not in "-" else f"-({v})", t

    def t_In(self, n: In, ctx):
        s, _ = self.tr(n.subject, ctx)
        vals = ", ".join(self.tr(v, ctx)[0] for v in n.values)
        e = f"{s} IN {{{vals}}}"
        e = f"NOT({e})" if n.negated else e
        if self.may_be_null(n.subject):              # NULL IN (...) is NULL; DAX would match 0 / ""
            e = self._null_guard([s], e)
        return e, "boolean"

    # ------------------------------------------------- control flow --
    def t_If(self, n: If, ctx):
        branches = [(self.tr(c, ctx)[0], self.tr(v, ctx)) for c, v in n.branches]
        else_ = self.tr(n.else_, ctx) if n.else_ is not None else None
        typ = next((t for _, (_, t) in branches if t != "null"), else_[1] if else_ else "string")
        if len(branches) == 1:
            c, (v, _) = branches[0]
            self.functions.add("IF")
            return (f"IF({c}, {v}, {else_[0]})" if else_ else f"IF({c}, {v})"), typ
        self.functions.update({"SWITCH", "TRUE"})
        parts = ",\n        ".join(f"{c}, {v}" for c, (v, _) in branches)
        tail = f",\n        {else_[0]}" if else_ else ""
        return f"SWITCH(TRUE(),\n        {parts}{tail}\n    )", typ

    def t_Case(self, n: Case, ctx):
        s, _ = self.tr(n.subject, ctx)
        whens = [(self.tr(m, ctx)[0], self.tr(v, ctx)) for m, v in n.whens]
        else_ = self.tr(n.else_, ctx) if n.else_ is not None else None
        typ = next((t for _, (_, t) in whens if t != "null"), else_[1] if else_ else "string")
        parts = ", ".join(f"{m}, {v}" for m, (v, _) in whens)
        tail = f", {else_[0]}" if else_ else ""
        self.functions.add("SWITCH")
        expr = f"SWITCH({s}, {parts}{tail})"
        if self.may_be_null(n.subject):              # CASE NULL -> ELSE; DAX SWITCH(BLANK) matches 0 / ""
            expr = f"IF(ISBLANK({s}), {else_[0] if else_ else 'BLANK()'}, {expr})"
        return expr, typ

    # ---------------------------------------------------------- LODs --
    def _lod_dims(self, n: LOD):
        dims = []
        for d in n.dims:
            if not isinstance(d, Field):
                self.warn("LOD dimension is an expression; only field references are converted.", "low")
                continue
            c = self.r.calc(d.name)
            if c is not None and c.kind == "column":
                dims.append((c.table, c.name))
                continue
            col = self.r.column(d.name)
            if col is None:
                raise TranslationError(f"Unknown LOD dimension [{d.name}]")
            dims.append((col.table, col.column))
        return dims

    def t_LOD(self, n: LOD, ctx: _Ctx):
        dims = self._lod_dims(n)
        fact = self.r.fact_table
        inner, typ = self.tr(n.expr, _Ctx("agg", None, ctx.addressing, ctx.vars, None))
        self.functions.add("CALCULATE")
        row_level = ctx.mode == "row" or ctx.in_iter is not None
        if row_level:
            # Evaluate the LOD for the current row: remove every filter, then filter the
            # LOD dimensions to the current row's values.
            if n.kind != "FIXED":
                self.warn(f"{n.kind} LOD used row-level is view-dependent in Tableau; converted as "
                          f"FIXED on its dimensions.", "low")
            filters = ["REMOVEFILTERS()"]
            var_lines = []
            cur = ctx.in_iter or ctx.host
            for t, c in dims:
                self._var_n += 1
                vname = f"__lod{self._var_n}"
                ref = q_col(t, c) if t == cur else f"RELATED({q_col(t, c)})"
                var_lines.append(f"VAR {vname} = {ref}")
                filters.append(f"{q_col(t, c)} = {vname}")
            self.functions.add("REMOVEFILTERS")
            body = f"CALCULATE({inner}, {', '.join(filters)})"
            if var_lines:
                return "(" + " ".join(var_lines) + f" RETURN {body})", typ
            return body, typ
        # measure context (LOD aggregated by the view)
        if n.kind == "FIXED":
            if not dims:
                return f"CALCULATE({inner}, REMOVEFILTERS())", typ
            keep = ", ".join(q_col(t, c) for t, c in dims)
            self.functions.add("ALLEXCEPT")
            return f"CALCULATE({inner}, ALLEXCEPT({q_table(fact)}, {keep}))", typ
        if n.kind == "EXCLUDE":
            rm = ", ".join(q_col(t, c) for t, c in dims)
            self.functions.add("REMOVEFILTERS")
            # EXCLUDE drops the dimension from the view grain but dimension filters still apply
            # (Tableau order of operations): ALLSELECTED keeps the visual filters on the column.
            self.functions.add("ALLSELECTED")
            return f"CALCULATE({inner}, ALLSELECTED({rm}))", typ
        # INCLUDE: evaluated at view grain + dims, then the outer aggregate applies
        return f"CALCULATE({inner})", typ

    def lod_measure(self, n: LOD, outer_agg: str) -> DaxResult:
        """Measure for an aggregated LOD pill, e.g. SUM({FIXED [Region]: SUM([Sales])}).

        Tableau evaluates the LOD at its own grain and aggregates the results over the
        combinations of view dimensions x LOD dimensions (not over records)."""
        self.warnings, self.functions, self.confidence, self._var_n = [], set(), "high", 0
        dims = self._lod_dims(n)
        ctx = _Ctx("agg")
        fact = self.r.fact_table
        inner, typ = self.tr(n.expr, ctx)
        iter_fn = {"sum": "SUMX", "avg": "AVERAGEX", "min": "MINX", "max": "MAXX",
                   "med": "MEDIANX", "cnt": "COUNTX", "count": "COUNTX"}.get(outer_agg, "SUMX")
        if outer_agg in ("ctd", "cntd"):
            self.warn("COUNTD over an LOD converted with DISTINCTCOUNT over the LOD values.")
        if not dims:
            base = f"CALCULATE({inner}, REMOVEFILTERS())" if n.kind == "FIXED" else f"CALCULATE({inner})"
            expr = base
        else:
            grain = ", ".join(q_col(t, c) for t, c in dims)
            if n.kind == "FIXED":
                keep = grain
                value = f"CALCULATE({inner}, ALLEXCEPT({q_table(fact)}, {keep}))"
                expr = f"{iter_fn}(SUMMARIZE({q_table(fact)}, {grain}), {value})"
            elif n.kind == "INCLUDE":
                expr = f"{iter_fn}(SUMMARIZE({q_table(fact)}, {grain}), CALCULATE({inner}))"
            else:
                expr = f"CALCULATE({inner}, ALLSELECTED({grain}))"   # keeps filters on the dims
        self.functions.update({"CALCULATE", iter_fn, "SUMMARIZE"})
        return DaxResult(expr, "agg", typ, list(self.warnings), set(self.functions), self.confidence)

    # ----------------------------------------------------- functions --
    def t_Func(self, n: Func, ctx: _Ctx):
        name = n.name
        if name in AGGREGATES and not is_scalar_minmax(n):
            return self._aggregate(n, ctx)
        if name in TABLE_CALCS:
            return self._table_calc(n, ctx)
        h = getattr(self, "f_" + name, None)
        if h is not None:
            return self._null_safe_func(n, ctx, h(n, ctx))
        simple = SIMPLE_FUNCS.get(name)
        if simple:
            args = [self.tr(a, ctx)[0] for a in n.args]
            dax_name, typ = simple
            self.functions.add(dax_name)
            return self._null_safe_func(n, ctx, (f"{dax_name}({', '.join(args)})", typ))
        self.warn(f"Function {name}() has no automatic DAX equivalent.", "low")
        args = [self.tr(a, ctx)[0] for a in n.args]
        return f"/* TODO {name} */ {name}({', '.join(args)})", "string"

    NULL_AWARE = {"ZN", "IFNULL", "ISNULL", "IIF", "IFERROR", "ISDATE", "TODAY", "NOW", "USERNAME", "FULLNAME",
                  "USERDOMAIN", "ISMEMBEROF", "PI", "MAKEDATE"}

    def _null_safe_func(self, n: Func, ctx: _Ctx, result):
        """Tableau scalar functions return NULL for a NULL argument; DAX treats BLANK as
        0 / "" / 1899-12-30. Guard row-level calls whose arguments may be NULL."""
        expr, typ = result
        row_level = ctx.mode == "row" or ctx.in_iter is not None
        if not row_level or n.name in self.NULL_AWARE:
            return expr, typ
        nullable = [a for a in n.args if self.may_be_null(a) and not isinstance(a, (Str, Num, Bool))]
        if not nullable:
            return expr, typ
        terms = [self.tr(a, ctx)[0] for a in nullable]
        return self._null_guard(terms, expr), typ

    def _agg_target(self, arg: Node):
        """If ``arg`` is a plain column (or row-level calc column) return its DAX ref."""
        if isinstance(arg, Field) and not arg.is_parameter:
            c = self.r.calc(arg.name)
            if c is not None and c.kind == "column":
                return q_col(c.table, c.name), c.datatype, c.table
            if c is None:
                col = self.r.column(arg.name)
                if col is not None:
                    return q_col(col.table, col.column), col.datatype, col.table
        return None

    def _zero_count(self, expr: str, arg: Node, table: str) -> str:
        """Tableau COUNT / COUNTD of only-NULL values is 0 (the row is shown); DAX returns BLANK
        (the row disappears). Return 0 when the table has rows in the current context."""
        if not self.may_be_null(arg):
            return expr
        fact = self.r.fact_table if table != self.r.fact_table and self.r.is_one_side(self.r.fact_table, table) else table
        self.functions.update({"COUNTROWS", "ISBLANK"})
        return f"IF(ISBLANK({expr}), IF(COUNTROWS({q_table(fact)}) > 0, 0), {expr})"

    def _through_fact(self, expr: str, table: str, ctx: _Ctx) -> str:
        """Aggregations of a dimension-table column are cross-filtered through the fact
        table, reproducing Tableau relationship semantics (only related rows count)."""
        fact = self.r.fact_table
        if ctx.in_iter is None and table != fact and self.r.is_one_side(fact, table):
            self.functions.add("CALCULATE")
            self.warnings.append(f"Aggregation over {q_table(table)} is filtered through {q_table(fact)} "
                                 f"so that filters on other dimensions apply (Tableau relationship semantics).")
            return f"CALCULATE({expr}, {q_table(fact)})"
        return expr

    def _iter_table(self, arg: Node) -> str:
        """Table to iterate for an aggregate over an expression: the fact table when the
        expression touches it, otherwise the only table referenced."""
        tables = set()
        for nd in arg.walk():
            if isinstance(nd, Field) and not nd.is_parameter:
                c = self.r.calc(nd.name)
                if c is not None and c.kind == "column":
                    tables.add(c.table)
                elif c is None:
                    col = self.r.column(nd.name)
                    if col:
                        tables.add(col.table)
        if len(tables) == 1:
            return tables.pop()
        return self.r.fact_table

    def _aggregate(self, n: Func, ctx: _Ctx):
        name = n.name
        if not n.args:
            raise TranslationError(f"{name}() requires an argument")
        arg = n.args[0]
        if ctx.mode == "row" and ctx.in_iter is None:
            raise TranslationError(f"Aggregate {name}() inside a row-level calculation")
        target = self._agg_target(arg)
        if name == "ATTR":
            if target is None:
                raise TranslationError("ATTR() of an expression is not supported")
            self.functions.add("SELECTEDVALUE")
            fallback = '"*"' if target[1] == "string" else "BLANK()"
            if target[1] != "string":
                self.warn("ATTR() returns '*' for multiple values in Tableau; Power BI returns BLANK.")
            return f"SELECTEDVALUE({target[0]}, {fallback})", target[1]
        if target is not None:
            ref, dt, _ = target
            simple = {"SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX", "MEDIAN": "MEDIAN",
                      "COUNTD": "DISTINCTCOUNTNOBLANK", "STDEV": "STDEV.S", "STDEVP": "STDEV.P",
                      "VAR": "VAR.S", "VARP": "VAR.P"}
            if name == "COUNT":
                f = "COUNT" if dt in ("integer", "real", "date", "datetime") else "COUNTA"
                self.functions.add(f)
                return self._zero_count(self._through_fact(f"{f}({ref})", target[2], ctx), arg, target[2]), "integer"
            if name == "PERCENTILE":
                p, _ = self.tr(n.args[1], ctx)
                self.functions.add("PERCENTILE.INC")
                return f"PERCENTILE.INC({ref}, {p})", "real"
            f = simple.get(name)
            if f:
                self.functions.add(f)
                rtype = "integer" if name == "COUNTD" else ("real" if name in ("AVG", "STDEV", "STDEVP", "VAR", "VARP", "MEDIAN") else dt)
                expr = self._through_fact(f"{f}({ref})", target[2], ctx)
                if name == "COUNTD":
                    expr = self._zero_count(expr, arg, target[2])
                return expr, rtype
        # aggregate over an expression -> iterator
        table = self._iter_table(arg)
        inner_ctx = ctx.child(in_iter=table)
        inner, it = self.tr(arg, inner_ctx)
        iters = {"SUM": "SUMX", "AVG": "AVERAGEX", "MIN": "MINX", "MAX": "MAXX", "MEDIAN": "MEDIANX",
                 "COUNT": "COUNTX", "STDEV": "STDEVX.S", "STDEVP": "STDEVX.P", "VAR": "VARX.S",
                 "VARP": "VARX.P"}
        if name == "COUNTD":
            self.functions.update({"COUNTROWS", "DISTINCT", "SELECTCOLUMNS", "FILTER"})
            return (f"COUNTROWS(FILTER(DISTINCT(SELECTCOLUMNS({q_table(table)}, \"__v\", {inner})), "
                    f"NOT ISBLANK([__v])))"), "integer"
        if name == "PERCENTILE":
            p, _ = self.tr(n.args[1], ctx)
            self.functions.add("PERCENTILEX.INC")
            return f"PERCENTILEX.INC({q_table(table)}, {inner}, {p})", "real"
        f = iters.get(name)
        if not f:
            raise TranslationError(f"Unsupported aggregate {name}")
        self.functions.add(f)
        return self._through_fact(f"{f}({q_table(table)}, {inner})", table, ctx), (
            "integer" if name == "COUNT" else ("real" if name != "SUM" else it))

    # --------------------------------------------------- table calcs --
    def _addressing(self):
        return None

    def _table_calc(self, n: Func, ctx: _Ctx):
        name = n.name
        if not ctx.addressing:
            self.warn(f"{name}() is a table calculation; no worksheet addressing found, "
                      f"computed along the first view dimension in Power BI.", "medium")
        addr = ctx.addressing
        self.functions.add("ALLSELECTED")
        if addr:
            alls = ", ".join(q_col(t, c) for t, c in addr)
            allsel = f"ALLSELECTED({alls})"
            order = ", ".join(q_col(t, c) for t, c in addr)
            # Tableau partitions contain only the cells that have data: the relation is the set of
            # addressing values present in the current partition (other filters kept).
            self.functions.update({"CALCULATETABLE", "SUMMARIZE"})
            rel = f"CALCULATETABLE(SUMMARIZE({q_table(self.r.fact_table)}, {alls}), {allsel})"
        else:
            allsel = "ALLSELECTED()"
            order = None
            rel = allsel
        arg = self.tr(n.args[0], ctx)[0] if n.args else None

        def window(frm, frm_t, to, to_t):
            self.functions.add("WINDOW")
            return f"WINDOW({frm}, {frm_t}, {to}, {to_t}, {rel}, ORDERBY({order}))"

        if name in ("RUNNING_SUM", "RUNNING_AVG", "RUNNING_MIN", "RUNNING_MAX", "RUNNING_COUNT"):
            if not order:
                raise TranslationError(f"{name}() needs an addressing dimension")
            if name == "RUNNING_SUM":
                return f"CALCULATE({arg}, {window(1, 'ABS', 0, 'REL')})", "real"
            agg = {"RUNNING_AVG": "AVERAGEX", "RUNNING_MIN": "MINX", "RUNNING_MAX": "MAXX",
                   "RUNNING_COUNT": "COUNTX"}[name]
            self.functions.add(agg)
            return f"{agg}({window(1, 'ABS', 0, 'REL')}, CALCULATE({arg}))", "real"
        if name in ("WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT",
                    "WINDOW_MEDIAN", "TOTAL"):
            if len(n.args) > 1:
                self.warn(f"{name}() with explicit offsets converted to WINDOW() on the addressing order.")
                s = self.tr(n.args[1], ctx)[0]
                e = self.tr(n.args[2], ctx)[0] if len(n.args) > 2 else "0"
                tbl = window(s, "REL", e, "REL")
            else:
                tbl = rel
            if name in ("WINDOW_SUM", "TOTAL") and len(n.args) <= 1:
                if name == "TOTAL":
                    self.warn("TOTAL() uses ALLSELECTED: matches Tableau grand-total for additive measures.")
                return f"CALCULATE({arg}, {allsel})", "real"
            agg = {"WINDOW_SUM": "SUMX", "TOTAL": "SUMX", "WINDOW_AVG": "AVERAGEX", "WINDOW_MIN": "MINX",
                   "WINDOW_MAX": "MAXX", "WINDOW_COUNT": "COUNTX", "WINDOW_MEDIAN": "MEDIANX"}[name]
            self.functions.add(agg)
            return f"{agg}({tbl}, CALCULATE({arg}))", "real"
        if name in ("RANK", "RANK_DENSE", "RANK_UNIQUE", "RANK_MODIFIED"):
            direction = "DESC"
            if len(n.args) > 1 and isinstance(n.args[1], Str) and n.args[1].value.lower() == "asc":
                direction = "ASC"
            ties = "DENSE" if name == "RANK_DENSE" else "SKIP"
            if name in ("RANK_UNIQUE", "RANK_MODIFIED"):
                self.warn(f"{name}() tie handling differs; RANKX SKIP used.")
            self.functions.add("RANKX")
            return f"RANKX({rel}, CALCULATE({arg}), , {direction}, {ties})", "integer"
        if name == "RANK_PERCENTILE":
            self.functions.add("RANKX")
            return (f"DIVIDE(RANKX({rel}, CALCULATE({arg}), , ASC, SKIP) - 1, "
                    f"COUNTROWS({rel}) - 1)"), "real"
        if name == "INDEX":
            if not order:
                raise TranslationError("INDEX() needs an addressing dimension")
            self.functions.add("ROWNUMBER")
            return f"ROWNUMBER({rel}, ORDERBY({order}))", "integer"
        if name == "SIZE":
            return f"COUNTROWS({rel})", "integer"
        if name in ("FIRST", "LAST"):
            self.functions.add("ROWNUMBER")
            if name == "FIRST":
                return f"1 - ROWNUMBER({rel}, ORDERBY({order}))", "integer"
            return f"COUNTROWS({rel}) - ROWNUMBER({rel}, ORDERBY({order}))", "integer"
        if name == "LOOKUP":
            off = self.tr(n.args[1], ctx)[0] if len(n.args) > 1 else "0"
            self.functions.add("OFFSET")
            return f"CALCULATE({arg}, OFFSET({off}, {rel}, ORDERBY({order})))", "real"
        if name == "PREVIOUS_VALUE":
            self.warn("PREVIOUS_VALUE() is recursive; converted as OFFSET(-1) of the expression.", "low")
            self.functions.add("OFFSET")
            return f"CALCULATE({arg}, OFFSET(-1, {rel}, ORDERBY({order})))", "real"
        raise TranslationError(f"Unsupported table calculation {name}")

    # ------------------------------------------------ specific funcs --
    def _args(self, n, ctx):
        return [self.tr(a, ctx) for a in n.args]

    def f_IIF(self, n, ctx):
        a = self._args(n, ctx)
        expr = f"IF({a[0][0]}, {a[1][0]}, {a[2][0]})"
        if self.may_be_null(n.args[0]):              # IIF(NULL, ...) returns the 'unknown' argument / NULL
            unknown = a[3][0] if len(a) == 4 else "BLANK()"
            expr = f"IF(ISBLANK({a[0][0]}), {unknown}, {expr})"
        return expr, a[1][1]

    def f_IFNULL(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.add("COALESCE")
        return f"COALESCE({a[0][0]}, {a[1][0]})", a[0][1]

    def f_ZN(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.add("COALESCE")
        return f"COALESCE({a[0][0]}, 0)", a[0][1] if a[0][1] != "null" else "real"

    def f_ISNULL(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.add("ISBLANK")
        return f"ISBLANK({a[0][0]})", "boolean"

    def f_IFERROR(self, n, ctx):
        a = self._args(n, ctx)
        return f"IFERROR({a[0][0]}, {a[1][0]})", a[0][1]

    def f_ISDATE(self, n, ctx):
        a = self._args(n, ctx)
        return f"NOT ISERROR(DATEVALUE({a[0][0]}))", "boolean"

    def f_MIN(self, n, ctx):
        return self._minmax(n, ctx, "MIN")

    def f_MAX(self, n, ctx):
        return self._minmax(n, ctx, "MAX")

    def _minmax(self, n, ctx, f):
        a = self._args(n, ctx)
        return f"{f}({a[0][0]}, {a[1][0]})", a[0][1]

    # strings
    def f_CONTAINS(self, n, ctx):
        a = self._args(n, ctx)
        self.warn("CONTAINS() is case-sensitive in most Tableau sources; DAX CONTAINSSTRING is case-insensitive.")
        self.functions.add("CONTAINSSTRING")
        return f"CONTAINSSTRING({a[0][0]}, {a[1][0]})", "boolean"

    def f_STARTSWITH(self, n, ctx):
        a = self._args(n, ctx)
        return f"(LEFT({a[0][0]}, LEN({a[1][0]})) = {a[1][0]})", "boolean"

    def f_ENDSWITH(self, n, ctx):
        a = self._args(n, ctx)
        return f"(RIGHT({a[0][0]}, LEN({a[1][0]})) = {a[1][0]})", "boolean"

    def f_FIND(self, n, ctx):
        a = self._args(n, ctx)
        start = a[2][0] if len(a) > 2 else "1"
        self.functions.add("FIND")
        return f"FIND({a[1][0]}, {a[0][0]}, {start}, 0)", "integer"

    def f_FINDNTH(self, n, ctx):
        self.warn("FINDNTH() has no DAX equivalent; manual rewrite needed.", "low")
        a = self._args(n, ctx)
        return f"FIND({a[1][0]}, {a[0][0]}, 1, 0)", "integer"

    def f_MID(self, n, ctx):
        a = self._args(n, ctx)
        ln = a[2][0] if len(a) > 2 else f"LEN({a[0][0]})"
        return f"MID({a[0][0]}, {a[1][0]}, {ln})", "string"

    def f_REPLACE(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.add("SUBSTITUTE")
        return f"SUBSTITUTE({a[0][0]}, {a[1][0]}, {a[2][0]})", "string"

    def f_SPLIT(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.update({"PATHITEM", "SUBSTITUTE"})
        idx = a[2][0]
        if idx.startswith("-"):
            self.functions.add("PATHITEMREVERSE")
            return f"PATHITEMREVERSE(SUBSTITUTE({a[0][0]}, {a[1][0]}, \"|\"), {idx[1:]})", "string"
        return f"PATHITEM(SUBSTITUTE({a[0][0]}, {a[1][0]}, \"|\"), {idx})", "string"

    def f_LTRIM(self, n, ctx):
        self.warn("LTRIM/RTRIM mapped to TRIM, which also collapses inner spaces.")
        a = self._args(n, ctx)
        return f"TRIM({a[0][0]})", "string"

    f_RTRIM = f_LTRIM

    def f_TRIM(self, n, ctx):
        a = self._args(n, ctx)
        return f"TRIM({a[0][0]})", "string"

    def f_SPACE(self, n, ctx):
        a = self._args(n, ctx)
        return f"REPT(\" \", {a[0][0]})", "string"

    def f_STR(self, n, ctx):
        a = self._args(n, ctx)
        if a[0][1] in ("date", "datetime"):
            return f"FORMAT({a[0][0]}, \"yyyy-mm-dd\")", "string"
        return f"CONVERT({a[0][0]}, STRING)", "string"

    def f_INT(self, n, ctx):
        a = self._args(n, ctx)
        self.functions.add("TRUNC")
        return f"TRUNC({a[0][0]})", "integer"

    def f_FLOAT(self, n, ctx):
        a = self._args(n, ctx)
        return f"CONVERT({a[0][0]}, DOUBLE)", "real"

    def f_ROUND(self, n, ctx):
        a = self._args(n, ctx)
        d = a[1][0] if len(a) > 1 else "0"
        return f"ROUND({a[0][0]}, {d})", "real"

    def f_CEILING(self, n, ctx):
        a = self._args(n, ctx)
        return f"CEILING({a[0][0]}, 1)", "integer"

    def f_FLOOR(self, n, ctx):
        a = self._args(n, ctx)
        return f"FLOOR({a[0][0]}, 1)", "integer"

    def f_DIV(self, n, ctx):
        a = self._args(n, ctx)
        return f"QUOTIENT({a[0][0]}, {a[1][0]})", "integer"

    def f_LOG(self, n, ctx):
        a = self._args(n, ctx)
        return f"LOG({a[0][0]}, {a[1][0] if len(a) > 1 else 10})", "real"

    def f_ZN_(self, n, ctx):
        return self.f_ZN(n, ctx)

    # dates
    def _part(self, node):
        if not isinstance(node, Str):
            raise TranslationError("Date part must be a string literal")
        return node.value.lower()

    def f_DATEPART(self, n, ctx):
        part = self._part(n.args[0])
        d, _ = self.tr(n.args[1], ctx)
        m = {"year": f"YEAR({d})", "quarter": f"QUARTER({d})", "month": f"MONTH({d})",
             "day": f"DAY({d})", "weekday": f"WEEKDAY({d}, 1)", "week": f"WEEKNUM({d}, 1)",
             "iso-week": f"WEEKNUM({d}, 21)", "hour": f"HOUR({d})", "minute": f"MINUTE({d})",
             "second": f"SECOND({d})", "dayofyear": f"({d} - DATE(YEAR({d}), 1, 1) + 1)",
             "iso-year": f"YEAR({d})"}
        if part not in m:
            raise TranslationError(f"Unsupported DATEPART unit {part}")
        return m[part], "integer"

    def f_DATENAME(self, n, ctx):
        part = self._part(n.args[0])
        d, _ = self.tr(n.args[1], ctx)
        m = {"month": f"FORMAT({d}, \"MMMM\")", "weekday": f"FORMAT({d}, \"dddd\")",
             "year": f"FORMAT({d}, \"yyyy\")", "quarter": f"\"Q\" & QUARTER({d})",
             "day": f"FORMAT({d}, \"d\")"}
        if part not in m:
            return f"CONVERT({self.f_DATEPART(n, ctx)[0]}, STRING)", "string"
        if part in ("month", "weekday"):
            self.warn("DATENAME() output depends on the model culture (FORMAT locale).")
        return m[part], "string"

    def f_DATETRUNC(self, n, ctx):
        part = self._part(n.args[0])
        d, t = self.tr(n.args[1], ctx)
        m = {"year": f"DATE(YEAR({d}), 1, 1)",
             "quarter": f"DATE(YEAR({d}), (QUARTER({d}) - 1) * 3 + 1, 1)",
             "month": f"DATE(YEAR({d}), MONTH({d}), 1)",
             "week": f"({d} - WEEKDAY({d}, 1) + 1)",
             "day": f"TRUNC({d})"}
        if part not in m:
            raise TranslationError(f"Unsupported DATETRUNC unit {part}")
        return m[part], "date"

    def f_DATEADD(self, n, ctx):
        part = self._part(n.args[0])
        k, _ = self.tr(n.args[1], ctx)
        d, t = self.tr(n.args[2], ctx)
        self.functions.add("EDATE") if part in ("month", "quarter", "year") else None
        m = {"year": f"EDATE({d}, 12 * ({k}))", "quarter": f"EDATE({d}, 3 * ({k}))",
             "month": f"EDATE({d}, {k})", "week": f"({d} + 7 * ({k}))", "day": f"({d} + {k})",
             "hour": f"({d} + ({k}) / 24)", "minute": f"({d} + ({k}) / 1440)"}
        if part not in m:
            raise TranslationError(f"Unsupported DATEADD unit {part}")
        return m[part], t

    def f_DATEDIFF(self, n, ctx):
        part = self._part(n.args[0])
        a, _ = self.tr(n.args[1], ctx)
        b, _ = self.tr(n.args[2], ctx)
        unit = DATE_PART_UNITS.get(part)
        if not unit:
            raise TranslationError(f"Unsupported DATEDIFF unit {part}")
        if part == "week":
            self.warn("DATEDIFF('week') week-start conventions may differ (Tableau uses the data source start of week).")
        self.functions.add("DATEDIFF")
        return f"DATEDIFF({a}, {b}, {unit})", "integer"

    def f_MAKEDATE(self, n, ctx):
        a = self._args(n, ctx)
        return f"DATE({a[0][0]}, {a[1][0]}, {a[2][0]})", "date"

    def f_MAKEDATETIME(self, n, ctx):
        a = self._args(n, ctx)
        return f"({a[0][0]} + {a[1][0]})", "datetime"

    def f_DATE(self, n, ctx):
        a = self._args(n, ctx)
        if a[0][1] == "string":
            return f"DATEVALUE({a[0][0]})", "date"
        return f"TRUNC({a[0][0]})", "date"

    def f_DATETIME(self, n, ctx):
        a = self._args(n, ctx)
        if a[0][1] == "string":
            return f"(DATEVALUE({a[0][0]}) + TIMEVALUE({a[0][0]}))", "datetime"
        return a[0][0], "datetime"

    def f_DATEPARSE(self, n, ctx):
        a = self._args(n, ctx)
        self.warn("DATEPARSE() format string is ignored; DATEVALUE uses the model culture.")
        return f"DATEVALUE({a[1][0]})", "date"

    def f_TODAY(self, n, ctx):
        return "TODAY()", "date"

    def f_NOW(self, n, ctx):
        return "NOW()", "datetime"

    def f_ISOWEEK(self, n, ctx):
        a = self._args(n, ctx)
        return f"WEEKNUM({a[0][0]}, 21)", "integer"


SIMPLE_FUNCS = {
    "ABS": ("ABS", "real"), "SQRT": ("SQRT", "real"), "EXP": ("EXP", "real"), "LN": ("LN", "real"),
    "POWER": ("POWER", "real"), "SIGN": ("SIGN", "integer"), "PI": ("PI", "real"),
    "SIN": ("SIN", "real"), "COS": ("COS", "real"), "TAN": ("TAN", "real"),
    "ASIN": ("ASIN", "real"), "ACOS": ("ACOS", "real"), "ATAN": ("ATAN", "real"),
    "DEGREES": ("DEGREES", "real"), "RADIANS": ("RADIANS", "real"), "SQUARE": ("POWER", "real"),
    "LEFT": ("LEFT", "string"), "RIGHT": ("RIGHT", "string"), "LEN": ("LEN", "integer"),
    "UPPER": ("UPPER", "string"), "LOWER": ("LOWER", "string"), "ASCII": ("UNICODE", "integer"),
    "CHAR": ("UNICHAR", "string"), "PROPER": ("PROPER", "string"),
    "YEAR": ("YEAR", "integer"), "MONTH": ("MONTH", "integer"), "DAY": ("DAY", "integer"),
    "QUARTER": ("QUARTER", "integer"), "WEEK": ("WEEKNUM", "integer"),
    "HOUR": ("HOUR", "integer"), "MINUTE": ("MINUTE", "integer"), "SECOND": ("SECOND", "integer"),
    "USERNAME": ("USERPRINCIPALNAME", "string"), "FULLNAME": ("USERNAME", "string"),
}


def translate(formula: str, resolver: Resolver, mode="agg", host=None, addressing=None) -> DaxResult:
    return DaxTranslator(resolver).translate(formula, mode, host, addressing)
