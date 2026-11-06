"""A DAX reference evaluator used to *execute the converted Power BI model offline*.

It parses the DAX produced by the converter (measures and calculated columns) and
evaluates it over the model data with DAX semantics:

* filter context (column filters, tuple filters, expanded-table filters),
* row context, context transition (CALCULATE / measure references),
* CALCULATE modifiers (ALL, ALLEXCEPT, REMOVEFILTERS, ALLSELECTED, KEEPFILTERS),
* relationships (many-to-one, single direction filter propagation, RELATED),
* BLANK arithmetic rules (BLANK+1 = 1, BLANK*1 = BLANK, BLANK = 0 is TRUE),
* iterators, RANKX, window functions (WINDOW, OFFSET, ROWNUMBER) over ALLSELECTED.

It deliberately shares *no code* with the Tableau reference engine so that the two
results are independent evidence for number validation.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd


class DaxError(Exception):
    pass


# ================================================================ parser ==
@dataclass
class T:
    kind: str
    value: Any
    pos: int


def _lex(src: str) -> list[T]:
    toks, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i) or src.startswith("--", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "'":
            j, buf = i + 1, []
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(T("TABLE", "".join(buf), i))
            i = j + 1
            continue
        if c == "[":
            j, buf = i + 1, []
            while j < n:
                if src[j] == "]":
                    if j + 1 < n and src[j + 1] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(T("BRACKET", "".join(buf), i))
            i = j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n:
                if src[j] == '"':
                    if j + 1 < n and src[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(T("STR", "".join(buf), i))
            i = j + 1
            continue
        m = re.match(r"\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+", src[i:])
        if m:
            toks.append(T("NUM", float(m.group(0)) if any(x in m.group(0) for x in ".eE") else int(m.group(0)), i))
            i += len(m.group(0))
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_\.]*", src[i:])
        if m:
            toks.append(T("IDENT", m.group(0), i))
            i += len(m.group(0))
            continue
        for op in ("&&", "||", "<=", ">=", "<>", "==", "(", ")", "{", "}", ",", "+", "-", "*", "/", "^",
                   "&", "=", "<", ">"):
            if src.startswith(op, i):
                toks.append(T("OP", op, i))
                i += len(op)
                break
        else:
            raise DaxError(f"Unexpected character {c!r} at {i}")
    toks.append(T("EOF", None, n))
    return toks


class DaxParser:
    def __init__(self, src):
        self.t = _lex(src)
        self.i = 0

    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def nxt(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def op(self, v, k=0):
        tk = self.peek(k)
        return tk.kind == "OP" and tk.value == v

    def kw(self, v, k=0):
        tk = self.peek(k)
        return tk.kind == "IDENT" and tk.value.upper() == v

    def expect(self, v):
        if not self.op(v):
            raise DaxError(f"Expected {v!r} at {self.peek().pos}, got {self.peek().value!r}")
        self.nxt()

    def parse(self):
        e = self.expr()
        if self.peek().kind != "EOF":
            raise DaxError(f"Trailing token {self.peek().value!r} at {self.peek().pos}")
        return e

    def expr(self):
        if self.kw("VAR"):
            defs = []
            while self.kw("VAR"):
                self.nxt()
                name = self.nxt().value
                self.expect("=")
                defs.append((name, self.expr()))
            if not self.kw("RETURN"):
                raise DaxError("VAR without RETURN")
            self.nxt()
            return ("var", defs, self.expr())
        return self.or_()

    def or_(self):
        l = self.and_()
        while self.op("||"):
            self.nxt()
            l = ("bin", "||", l, self.and_())
        return l

    def and_(self):
        l = self.not_()
        while self.op("&&"):
            self.nxt()
            l = ("bin", "&&", l, self.not_())
        return l

    def not_(self):
        if self.kw("NOT") and not self.op("(", 1):
            self.nxt()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        l = self.concat()
        while True:
            tk = self.peek()
            if tk.kind == "OP" and tk.value in ("=", "==", "<>", "<", ">", "<=", ">="):
                self.nxt()
                l = ("bin", tk.value, l, self.concat())
            elif self.kw("IN"):
                self.nxt()
                l = ("in", l, self.primary())
            elif self.kw("NOT") and self.kw("IN", 1):
                self.nxt()
                self.nxt()
                l = ("not", ("in", l, self.primary()))
            else:
                return l

    def concat(self):
        l = self.add()
        while self.op("&"):
            self.nxt()
            l = ("bin", "&", l, self.add())
        return l

    def add(self):
        l = self.mul()
        while self.op("+") or self.op("-"):
            o = self.nxt().value
            l = ("bin", o, l, self.mul())
        return l

    def mul(self):
        l = self.pow_()
        while self.op("*") or self.op("/"):
            o = self.nxt().value
            l = ("bin", o, l, self.pow_())
        return l

    def pow_(self):
        l = self.unary()
        while self.op("^"):
            self.nxt()
            l = ("bin", "^", l, self.unary())
        return l

    def unary(self):
        if self.op("-"):
            self.nxt()
            return ("neg", self.unary())
        if self.op("+"):
            self.nxt()
            return self.unary()
        return self.primary()

    def primary(self):
        tk = self.peek()
        if tk.kind == "NUM":
            self.nxt()
            return ("num", tk.value)
        if tk.kind == "STR":
            self.nxt()
            return ("str", tk.value)
        if tk.kind == "TABLE":
            self.nxt()
            if self.peek().kind == "BRACKET":
                return ("col", tk.value, self.nxt().value)
            return ("table", tk.value)
        if tk.kind == "BRACKET":
            self.nxt()
            return ("meas", tk.value)
        if tk.kind == "OP" and tk.value == "(":
            self.nxt()
            e = self.expr()
            self.expect(")")
            return e
        if tk.kind == "OP" and tk.value == "{":
            self.nxt()
            rows = []
            if not self.op("}"):
                rows.append(self._ctor_row())
                while self.op(","):
                    self.nxt()
                    rows.append(self._ctor_row())
            self.expect("}")
            return ("ctor", rows)
        if tk.kind == "IDENT":
            self.nxt()
            if self.peek().kind == "BRACKET" and self.peek().pos == tk.pos + len(tk.value):
                return ("col", tk.value, self.nxt().value)
            if self.op("("):
                self.nxt()
                args = []
                if not self.op(")"):
                    args.append(self._arg())
                    while self.op(","):
                        self.nxt()
                        args.append(self._arg())
                self.expect(")")
                return ("func", tk.value.upper(), args)
            return ("ident", tk.value)
        raise DaxError(f"Unexpected token {tk.value!r} at {tk.pos}")

    def _arg(self):
        if self.op(",") or self.op(")"):
            return ("missing",)
        return self.expr()

    def _ctor_row(self):
        if self.op("("):
            # tuple row
            save = self.i
            self.nxt()
            vals = [self.expr()]
            if self.op(","):
                while self.op(","):
                    self.nxt()
                    vals.append(self.expr())
                self.expect(")")
                return vals
            self.i = save
        return [self.expr()]


_PARSE_CACHE: dict = {}


def parse_dax(src: str):
    if src not in _PARSE_CACHE:
        _PARSE_CACHE[src] = DaxParser(src).parse()
    return _PARSE_CACHE[src]


# ============================================================ filters ==
@dataclass(frozen=True)
class ColF:
    key: str
    values: frozenset


@dataclass(frozen=True)
class TupF:
    keys: tuple
    values: frozenset


@dataclass(frozen=True)
class TabF:
    table: str
    rows: frozenset


@dataclass
class TableVal:
    df: pd.DataFrame
    lineage: list            # column keys with lineage
    base: Optional[str] = None   # base table (rows of its expanded frame)
    ident: str = ""


@dataclass
class RowCtx:
    ident: str
    values: dict
    own: tuple               # keys used for context transition


@dataclass
class Ctx:
    filters: tuple = ()
    rows: tuple = ()
    env: dict = field(default_factory=dict)


class Missing:
    pass


MISSING = Missing()


def _key(t, c):
    return f"{t}[{c}]"


def _py(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime() if not pd.isna(v) else None
    if v is pd.NaT:
        return None
    return v


def _to_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(v))
    return pd.Timestamp(str(v)).to_pydatetime()


# ============================================================ engine ==
class DaxEngine:
    def __init__(self, tables: dict, relationships: list, measures: dict,
                 calc_columns: dict | None = None, today: date | None = None):
        """
        tables:        {table: DataFrame with plain column names}
        relationships: [(from_table, from_col, to_table, to_col)] many -> one
        measures:      {measure name: DAX expression}
        calc_columns:  {table: [(column, DAX expression)]} evaluated on load
        """
        self.today = today or date.today()
        self.base = {}
        for t, df in tables.items():
            d = df.copy()
            d.columns = [_key(t, c) for c in d.columns]
            self.base[t] = d.reset_index(drop=True)
        self.rels = [r for r in relationships if r[0] in self.base and r[2] in self.base]
        self.measures = {k: v for k, v in measures.items()}
        self._expanded = {}
        self._mask_cache = {}
        self._eval_cache = {}
        self.shadow: tuple = ()
        self.errors: list = []
        self.calc_columns = calc_columns or {}
        self._compute_calc_columns()

    # ---------------------------------------------------- model helpers --
    def own_keys(self, table):
        return tuple(self.base[table].columns)

    def one_side(self, table):
        return [(fc, tt, tc) for (ft, fc, tt, tc) in self.rels if ft == table]

    def expanded(self, table) -> pd.DataFrame:
        if table in self._expanded:
            return self._expanded[table]
        df = self.base[table].copy()
        seen = {table}

        def join(cur_df, tname):
            for fc, tt, tc in self.one_side(tname):
                if tt in seen:
                    continue
                seen.add(tt)
                right = self.base[tt]
                lk, rk = _key(tname, fc), _key(tt, tc)
                if lk not in cur_df.columns:
                    continue
                r = right.drop_duplicates(subset=[rk])
                cur_df = cur_df.merge(r, how="left", left_on=lk, right_on=rk)
                cur_df = join(cur_df, tt)
            return cur_df
        df = join(df, table)
        df = df.reset_index(drop=True)
        self._expanded[table] = df
        return df

    def table_of(self, key):
        return key.split("[", 1)[0]

    def _invalidate(self):
        self._expanded.clear()
        self._mask_cache.clear()
        self._eval_cache.clear()

    def _compute_calc_columns(self):
        # dims (tables without outgoing relationships) first, facts last
        order = sorted(self.calc_columns, key=lambda t: len(self.one_side(t)))
        for t in order:
            cols = list(self.calc_columns[t])
            done = set()
            pending = cols[:]
            guard = 0
            while pending and guard < 50:
                guard += 1
                nxt = []
                for name, expr in pending:
                    refs = set(re.findall(r"'([^']+)'\[([^\]]+)\]", expr))
                    if any(rt == t and rc in [n for n, _ in cols] and rc not in done and rc != name
                           for rt, rc in refs):
                        nxt.append((name, expr))
                        continue
                    self._calc_column(t, name, expr)
                    done.add(name)
                if len(nxt) == len(pending):
                    for name, expr in nxt:
                        self._calc_column(t, name, expr)
                    break
                pending = nxt

    def _calc_column(self, table, name, expr):
        key = _key(table, name)
        try:
            ast = parse_dax(expr)
        except DaxError as e:
            self.errors.append(f"{key}: {e}")
            self.base[table][key] = None
            self._invalidate()
            return
        self._invalidate()
        exp = self.expanded(table)
        own = self.own_keys(table)
        values = []
        recs = exp.to_dict("records")
        for i, rec in enumerate(recs):
            rec = {k: _py(v) for k, v in rec.items()}
            rec["__idx"] = i
            ctx = Ctx((), (RowCtx(table, rec, own),), {})
            try:
                values.append(self.eval(ast, ctx))
            except Exception as e:     # keep going, record the problem
                values.append(None)
                if len(self.errors) < 50:
                    self.errors.append(f"{key}: {e}")
        # expanded frame rows align 1:1 with the base table (left joins on unique keys)
        self.base[table][key] = pd.Series(values, dtype=object).values
        self._invalidate()

    # --------------------------------------------------------- filtering --
    def domain(self, key) -> list:
        t = self.table_of(key)
        if t not in self.base or key not in self.base[t].columns:
            return []
        s = self.base[t][key]
        return list(dict.fromkeys(_py(v) for v in s.tolist()))

    def mask(self, table, filters) -> np.ndarray:
        ck = (table, filters)
        if ck in self._mask_cache:
            return self._mask_cache[ck]
        df = self.expanded(table)
        m = np.ones(len(df), dtype=bool)
        cols = set(df.columns)
        for f in filters:
            if isinstance(f, ColF):
                if f.key in cols:
                    m &= self._isin(df[f.key], f.values)
            elif isinstance(f, TupF):
                common = [k for k in f.keys if k in cols]
                if not common:
                    continue
                idx = [f.keys.index(k) for k in common]
                allowed = {tuple(_norm(t[i]) for i in idx) for t in f.values}
                sub = df[common].to_dict("records")
                m &= np.array([tuple(_norm(_py(r[k])) for k in common) in allowed for r in sub], dtype=bool)
            elif isinstance(f, TabF):
                if f.table == table:
                    sel = np.zeros(len(df), dtype=bool)
                    sel[list(f.rows)] = True
                    m &= sel
                else:
                    src = self.expanded(f.table)
                    common = [k for k in df.columns if k in set(src.columns)]
                    if not common:
                        continue
                    rows = src.iloc[sorted(f.rows)][common].to_dict("records")
                    allowed = {tuple(_norm(_py(r[k])) for k in common) for r in rows}
                    sub = df[common].to_dict("records")
                    m &= np.array([tuple(_norm(_py(r[k])) for k in common) in allowed for r in sub], dtype=bool)
        self._mask_cache[ck] = m
        return m

    @staticmethod
    def _isin(series, values):
        vals = [v for v in values if v is not None]
        extra = [pd.Timestamp(v) for v in vals if isinstance(v, datetime)]
        m = series.isin(vals + extra).values
        if None in values:
            m = m | series.isna().values
        return m

    def column_values(self, key, ctx: Ctx) -> list:
        t = self.table_of(key)
        arr = self.expanded(t)[key].values
        return [_py(v) for v in arr[self.mask(t, ctx.filters)]]

    def frame(self, table, ctx: Ctx) -> pd.DataFrame:
        df = self.expanded(table)
        m = self.mask(table, ctx.filters)
        return df[m]

    # ------------------------------------------------- filter operations --
    @staticmethod
    def remove(filters, keys=None, tables=None, all_=False, keep=None):
        """Remove filters on ``keys`` (column keys) / ``tables`` / everything."""
        out = []
        for f in filters:
            if all_:
                if keep is None:
                    continue
                if isinstance(f, ColF):
                    if f.key in keep:
                        out.append(f)
                elif isinstance(f, TupF):
                    ks = [k for k in f.keys if k in keep]
                    if ks:
                        out.append(_project(f, ks))
                continue
            if isinstance(f, ColF):
                if (keys and f.key in keys) or (tables and f.key.split("[", 1)[0] in tables):
                    continue
                out.append(f)
            elif isinstance(f, TupF):
                ks = [k for k in f.keys if not ((keys and k in keys) or (tables and k.split("[", 1)[0] in tables))]
                if ks:
                    out.append(_project(f, ks) if len(ks) != len(f.keys) else f)
            elif isinstance(f, TabF):
                if tables and f.table in tables:
                    continue
                out.append(f)
        return tuple(out)

    # ========================================================= evaluate ==
    def eval(self, node, ctx: Ctx):
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind == "str":
            return node[1]
        if kind == "missing":
            return MISSING
        if kind == "col":
            key = _key(node[1], node[2])
            for rc in reversed(ctx.rows):
                if key in rc.values:
                    return rc.values[key]
            raise DaxError(f"Column {key} has no row context")
        if kind == "meas":
            name = node[1]
            if name in ctx.env:
                return ctx.env[name]
            for rc in reversed(ctx.rows):
                if f"[{name}]" in rc.values:
                    return rc.values[f"[{name}]"]
            if name not in self.measures:
                raise DaxError(f"Unknown measure [{name}]")
            return self._measure(name, ctx)
        if kind == "ident":
            name = node[1]
            if name in ctx.env:
                return ctx.env[name]
            up = name.upper()
            if up in ("TRUE",):
                return True
            if up in ("FALSE",):
                return False
            return ("enum", up)
        if kind == "var":
            env = dict(ctx.env)
            c2 = Ctx(ctx.filters, ctx.rows, env)
            for name, e in node[1]:
                env[name] = self.eval(e, c2)
            return self.eval(node[2], c2)
        if kind == "neg":
            v = self.eval(node[1], ctx)
            return None if v is None else -v
        if kind == "not":
            return not _truthy(self.eval(node[1], ctx))
        if kind == "bin":
            return self._bin(node, ctx)
        if kind == "in":
            v = self.eval(node[1], ctx)
            tv = self.table(node[2], ctx)
            col = tv.df.columns[0]
            vals = {_norm(_py(x)) for x in tv.df[col].tolist()}
            return _norm(v) in vals
        if kind == "func":
            return self._func(node[1], node[2], ctx)
        if kind in ("table", "ctor"):
            return self.table(node, ctx)
        raise DaxError(f"Cannot evaluate {kind}")

    def _measure(self, name, ctx: Ctx):
        ast = parse_dax(self.measures[name])
        c2 = self._transition(ctx)
        ck = (name, c2.filters)
        if ck in self._eval_cache:
            return self._eval_cache[ck]
        v = self.eval(ast, Ctx(c2.filters, (), {}))
        self._eval_cache[ck] = v
        return v

    def _transition(self, ctx: Ctx) -> Ctx:
        if not ctx.rows:
            return ctx
        filters = ctx.filters
        seen = set()
        for rc in reversed(ctx.rows):
            if rc.ident in seen:
                continue
            seen.add(rc.ident)
            keys = [k for k in rc.own if k in rc.values]
            if not keys:
                continue
            filters = self.remove(filters, keys=set(keys))
            for k in keys:
                filters = filters + (ColF(k, frozenset([_norm(rc.values[k])])),)
        return Ctx(filters, ctx.rows, ctx.env)

    # ------------------------------------------------------- operators --
    def _bin(self, node, ctx):
        op = node[1]
        if op == "&&":
            return _truthy(self.eval(node[2], ctx)) and _truthy(self.eval(node[3], ctx))
        if op == "||":
            return _truthy(self.eval(node[2], ctx)) or _truthy(self.eval(node[3], ctx))
        a = self.eval(node[2], ctx)
        b = self.eval(node[3], ctx)
        if op == "&":
            return _text(a) + _text(b)
        if op in ("+", "-"):
            if a is None and b is None:
                return None
            if isinstance(a, datetime) or isinstance(b, datetime):
                if op == "-" and isinstance(a, datetime) and isinstance(b, datetime):
                    return (a - b).total_seconds() / 86400.0
                if isinstance(a, datetime):
                    return a + timedelta(days=(b or 0)) if op == "+" else a - timedelta(days=(b or 0))
                return b + timedelta(days=(a or 0))
            a = 0 if a is None else a
            b = 0 if b is None else b
            return a + b if op == "+" else a - b
        if op == "*":
            if a is None or b is None:
                return None
            return a * b
        if op == "/":
            if a is None:
                return None
            if b in (None, 0):
                return math.inf if (a or 0) >= 0 else -math.inf
            return a / b
        if op == "^":
            if a is None or b is None:
                return None
            return a ** b
        if op in ("=", "==", "<>", "<", ">", "<=", ">="):
            if op == "==":
                if a is None or b is None:
                    return a is None and b is None
                op = "="
            x, y = _cmp_pair(a, b)
            return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]
        raise DaxError(f"Unknown operator {op}")

    # ------------------------------------------------------ table exprs --
    def table(self, node, ctx: Ctx) -> TableVal:
        kind = node[0]
        if kind == "meas" and node[1] in ctx.env and isinstance(ctx.env[node[1]], TableVal):
            return ctx.env[node[1]]
        if kind == "ident" and node[1] in ctx.env and isinstance(ctx.env[node[1]], TableVal):
            return ctx.env[node[1]]
        if kind == "table":
            t = node[1]
            df = self.frame(t, ctx).copy()
            df["__idx"] = df.index
            return TableVal(df, list(self.own_keys(t)), base=t, ident=t)
        if kind == "ctor":
            rows = [[_py(self.eval(e, ctx)) for e in r] for r in node[1]]
            ncol = max((len(r) for r in rows), default=1)
            cols = ["[Value]"] if ncol == 1 else [f"[Value{i + 1}]" for i in range(ncol)]
            return TableVal(pd.DataFrame(rows, columns=cols), [], ident="ctor")
        if kind == "func":
            return self._table_func(node[1], node[2], ctx)
        raise DaxError(f"Expression is not a table: {node[0]}")

    def _cols_of(self, args):
        out = []
        for a in args:
            if a[0] == "col":
                out.append(_key(a[1], a[2]))
        return out

    def _distinct(self, keys, filters, ctx=None):
        """Existing combinations of ``keys`` under ``filters``.

        Columns reachable through one expanded table give the combinations that exist
        in that table (auto-exist); columns on unrelated tables are cross-joined, as
        SUMMARIZECOLUMNS does."""
        remaining = list(keys)
        parts = []
        while remaining:
            best, cover = None, []
            for cand in [self.table_of(remaining[0])] + list(self.base):
                cols = set(self.expanded(cand).columns)
                cov = [k for k in remaining if k in cols]
                if len(cov) > len(cover):
                    best, cover = cand, cov
            if not cover:
                raise DaxError(f"Column {remaining[0]} not found in the model")
            df = self.expanded(best)[self.mask(best, filters)][cover].drop_duplicates()
            parts.append(df.reset_index(drop=True))
            remaining = [k for k in remaining if k not in cover]
        out = parts[0]
        for p in parts[1:]:
            out = out.merge(p, how="cross")
        return out[list(keys)].reset_index(drop=True)

    def _table_func(self, name, args, ctx: Ctx) -> TableVal:
        if name in ("ALL", "REMOVEFILTERS", "ALLNOBLANKROW"):
            if args and args[0][0] == "table":
                t = args[0][1]
                df = self.expanded(t).copy()
                df["__idx"] = df.index
                return TableVal(df, list(self.own_keys(t)), base=t, ident=f"ALL:{t}")
            keys = self._cols_of(args)
            return TableVal(self._distinct(keys, ()), keys, ident="ALL:" + ",".join(keys))
        if name in ("VALUES", "DISTINCT"):
            if args[0][0] == "col":
                keys = self._cols_of(args)
                return TableVal(self._distinct(keys, ctx.filters), keys, ident="VALUES:" + keys[0])
            tv = self.table(args[0], ctx)
            return TableVal(tv.df.drop_duplicates(subset=[c for c in tv.df.columns if c != "__idx"]),
                            tv.lineage, ident="DISTINCT")
        if name == "ALLSELECTED":
            keys = self._cols_of(args)
            if not keys:
                raise DaxError("ALLSELECTED() without columns is not supported as a table")
            return TableVal(self._distinct(keys, self.shadow), keys, ident="ALLSELECTED:" + ",".join(keys))
        if name == "FILTER":
            tv = self.table(args[0], ctx)
            keep = []
            for rec in tv.df.to_dict("records"):
                rec = {k: _py(v) for k, v in rec.items()}
                rc = RowCtx(tv.ident, rec, tuple(tv.lineage))
                if _truthy(self.eval(args[1], Ctx(ctx.filters, ctx.rows + (rc,), ctx.env))):
                    keep.append(True)
                else:
                    keep.append(False)
            return TableVal(tv.df[np.array(keep, dtype=bool)] if len(keep) else tv.df, tv.lineage, tv.base,
                            tv.ident)
        if name == "SUMMARIZE":
            src = self.table(args[0], ctx)
            keys = self._cols_of(args[1:])
            df = src.df[keys].drop_duplicates().reset_index(drop=True)
            return TableVal(df, keys, ident="SUMMARIZE:" + ",".join(keys))
        if name in ("ADDCOLUMNS", "SELECTCOLUMNS"):
            src = self.table(args[0], ctx)
            recs = src.df.to_dict("records")
            new = {}
            pairs = [(args[i], args[i + 1]) for i in range(1, len(args) - 1, 2)]
            out_rows = []
            for rec in recs:
                prec = {k: _py(v) for k, v in rec.items()}
                rc = RowCtx(src.ident, prec, tuple(src.lineage))
                row = dict(prec) if name == "ADDCOLUMNS" else {}
                for nm, ex in pairs:
                    label = f"[{self.eval(nm, ctx)}]"
                    row[label] = self.eval(ex, Ctx(ctx.filters, ctx.rows + (rc,), ctx.env))
                out_rows.append(row)
            df = pd.DataFrame(out_rows)
            lineage = src.lineage if name == "ADDCOLUMNS" else []
            return TableVal(df, lineage, None, name)
        if name == "RELATEDTABLE":
            t = args[0][1]
            c2 = self._transition(ctx)
            df = self.frame(t, c2).copy()
            df["__idx"] = df.index
            return TableVal(df, list(self.own_keys(t)), base=t, ident=t)
        if name in ("WINDOW", "OFFSET"):
            return self._window(name, args, ctx)
        if name == "GENERATESERIES":
            s, e = self.eval(args[0], ctx), self.eval(args[1], ctx)
            st = self.eval(args[2], ctx) if len(args) > 2 else 1
            vals, v = [], s
            while v <= e + 1e-9:
                vals.append(v)
                v += st
            return TableVal(pd.DataFrame({"[Value]": vals}), [], ident="series")
        if name == "TOPN":
            raise DaxError("TOPN not supported")
        raise DaxError(f"Unsupported table function {name}")

    def _orderby(self, node, ctx):
        if node[0] != "func" or node[1] != "ORDERBY":
            raise DaxError("ORDERBY expected")
        keys, dirs = [], []
        args = node[2]
        i = 0
        while i < len(args):
            a = args[i]
            keys.append(_key(a[1], a[2]))
            d = "ASC"
            if i + 1 < len(args) and args[i + 1][0] == "ident":
                d = args[i + 1][1].upper()
                i += 1
            dirs.append(d)
            i += 1
        return keys, dirs

    def _current(self, keys, ctx: Ctx):
        cur = []
        for k in keys:
            val = MISSING
            for rc in reversed(ctx.rows):
                if k in rc.values:
                    val = rc.values[k]
                    break
            if val is MISSING:
                for f in ctx.filters:
                    if isinstance(f, ColF) and f.key == k and len(f.values) == 1:
                        val = next(iter(f.values))
            if val is MISSING:
                return None
            cur.append(_norm(val))
        return tuple(cur)

    def _sorted_rel(self, rel: TableVal, keys, dirs):
        df = rel.df.copy()
        recs = df.to_dict("records")
        def sk(r):
            return tuple(_sort_key(_py(r[k])) for k in keys)
        recs.sort(key=sk)
        if dirs and dirs[0] == "DESC":
            recs.reverse()
        return recs

    def _window(self, name, args, ctx):
        if name == "OFFSET":
            n = self.eval(args[0], ctx)
            rel = self.table(args[1], ctx)
            keys, dirs = self._orderby(args[2], ctx)
        else:
            frm, frm_t = self.eval(args[0], ctx), self.eval(args[1], ctx)[1]
            to, to_t = self.eval(args[2], ctx), self.eval(args[3], ctx)[1]
            rel = self.table(args[4], ctx)
            keys, dirs = self._orderby(args[5], ctx)
        recs = self._sorted_rel(rel, keys, dirs)
        cur = self._current(keys, ctx)
        pos = None
        if cur is not None:
            for i, r in enumerate(recs):
                if tuple(_norm(_py(r[k])) for k in keys) == cur:
                    pos = i
                    break
        if pos is None:
            sel = []
        elif name == "OFFSET":
            j = pos + int(n)
            sel = [recs[j]] if 0 <= j < len(recs) else []
        else:
            def resolve(v, t):
                if t == "ABS":
                    return int(v) - 1 if v > 0 else len(recs) + int(v)
                return pos + int(v)
            a, b = resolve(frm, frm_t), resolve(to, to_t)
            a, b = max(a, 0), min(b, len(recs) - 1)
            sel = recs[a:b + 1] if a <= b else []
        df = pd.DataFrame(sel, columns=list(rel.df.columns))
        return TableVal(df, rel.lineage, None, f"{name}:{','.join(keys)}")

    def as_filter(self, tv: TableVal):
        if tv.base is not None:
            return TabF(tv.base, frozenset(int(i) for i in tv.df["__idx"].tolist()))
        keys = [k for k in tv.lineage if k in tv.df.columns]
        if not keys:
            raise DaxError("Table without lineage used as filter")
        if len(keys) == 1:
            return ColF(keys[0], frozenset(_norm(_py(v)) for v in tv.df[keys[0]].tolist()))
        return TupF(tuple(keys), frozenset(tuple(_norm(_py(v)) for v in r)
                                           for r in tv.df[keys].itertuples(index=False, name=None)))

    # ------------------------------------------------------- CALCULATE --
    def calculate(self, expr, fargs, ctx: Ctx):
        new_filters, keep_filters, removals = [], [], []
        for fa in fargs:
            if fa[0] == "missing":
                continue
            keep = False
            if fa[0] == "func" and fa[1] == "KEEPFILTERS":
                keep = True
                fa = fa[2][0]
            if fa[0] == "func" and fa[1] in ("ALL", "REMOVEFILTERS", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW"):
                removals.append(fa)
                continue
            if fa[0] == "func" and fa[1] in ("USERELATIONSHIP", "CROSSFILTER"):
                continue
            if fa[0] in ("table", "func", "ctor") and not self._is_boolean(fa):
                f = self.as_filter(self.table(fa, ctx))
            else:
                f = self._bool_filter(fa, ctx)
            (keep_filters if keep else new_filters).append(f)
        c2 = self._transition(ctx)
        filters = c2.filters
        for r in removals:
            nm, a = r[1], r[2]
            if nm in ("ALL", "REMOVEFILTERS", "ALLNOBLANKROW"):
                if not a:
                    filters = ()
                elif a[0][0] == "table":
                    t = a[0][1]
                    reach = self._expanded_tables(t)
                    filters = self.remove(filters, tables=reach)
                else:
                    filters = self.remove(filters, keys=set(self._cols_of(a)))
            elif nm == "ALLEXCEPT":
                t = a[0][1]
                keep = set(self._cols_of(a[1:]))
                reach = self._expanded_tables(t)
                out = []
                for f in filters:
                    if isinstance(f, ColF) and self.table_of(f.key) in reach and f.key not in keep:
                        continue
                    if isinstance(f, TupF):
                        ks = [k for k in f.keys if not (self.table_of(k) in reach and k not in keep)]
                        if ks:
                            out.append(_project(f, ks))
                        continue
                    if isinstance(f, TabF) and f.table in reach:
                        continue
                    out.append(f)
                filters = tuple(out)
            elif nm == "ALLSELECTED":
                keys = set(self._cols_of(a))
                if not a:
                    filters = self.shadow
                else:
                    filters = self.remove(filters, keys=keys)
                    for f in self.shadow:
                        if isinstance(f, ColF) and f.key in keys:
                            filters = filters + (f,)
                        elif isinstance(f, TupF) and set(f.keys) & keys:
                            filters = filters + (_project(f, [k for k in f.keys if k in keys]),)
        for f in new_filters:
            if isinstance(f, ColF):
                filters = self.remove(filters, keys={f.key})
            elif isinstance(f, TupF):
                filters = self.remove(filters, keys=set(f.keys))
            filters = filters + (f,)
        filters = filters + tuple(keep_filters)
        if _pure(expr):
            ck = ("calc", id(expr), filters)
            if ck not in self._eval_cache:
                self._eval_cache[ck] = self.eval(expr, Ctx(filters, (), {}))
            return self._eval_cache[ck]
        return self.eval(expr, Ctx(filters, ctx.rows, ctx.env))

    def _expanded_tables(self, t):
        out, stack = {t}, [t]
        while stack:
            x = stack.pop()
            for _, tt, _ in self.one_side(x):
                if tt not in out:
                    out.add(tt)
                    stack.append(tt)
        return out

    def _is_boolean(self, node):
        if node[0] == "bin" and node[1] in ("=", "==", "<>", "<", ">", "<=", ">=", "&&", "||"):
            return True
        if node[0] in ("in", "not"):
            return True
        return False

    def _bool_filter(self, node, ctx: Ctx):
        cols = sorted({_key(n[1], n[2]) for n in _walk(node) if n[0] == "col"})
        if not cols:
            raise DaxError("Boolean filter without a column")
        if len(cols) > 1:
            raise DaxError("Boolean filter over several columns is not supported")
        key = cols[0]
        allowed = []
        for v in self.domain(key):
            rc = RowCtx("bool", {key: v}, (key,))
            if _truthy(self.eval(node, Ctx(ctx.filters, ctx.rows + (rc,), ctx.env))):
                allowed.append(_norm(v))
        return ColF(key, frozenset(allowed))

    # ------------------------------------------------------- functions --
    def _func(self, name, args, ctx: Ctx):
        ev = lambda i: self.eval(args[i], ctx)
        if name == "CALCULATE":
            return self.calculate(args[0], args[1:], ctx)
        if name in ("TRUE",):
            return True
        if name in ("FALSE",):
            return False
        if name == "BLANK":
            return None
        if name == "IF":
            c = _truthy(ev(0))
            if c:
                return ev(1)
            return ev(2) if len(args) > 2 else None
        if name == "IFERROR":
            try:
                v = ev(0)
                if isinstance(v, float) and math.isinf(v):
                    return ev(1)
                return v
            except Exception:
                return ev(1)
        if name == "SWITCH":
            subj = ev(0)
            i = 1
            while i + 1 < len(args):
                if _cmp_eq(subj, ev(i)):
                    return ev(i + 1)
                i += 2
            return ev(i) if i < len(args) else None
        if name == "DIVIDE":
            a, b = ev(0), ev(1)
            alt = ev(2) if len(args) > 2 else None
            if b is None or b == 0:
                return alt
            if a is None:
                return None
            return a / b
        if name == "COALESCE":
            for i in range(len(args)):
                v = ev(i)
                if v is not None:
                    return v
            return None
        if name == "ISBLANK":
            return ev(0) is None
        if name == "NOT":
            return not _truthy(ev(0))
        if name == "AND":
            return _truthy(ev(0)) and _truthy(ev(1))
        if name == "OR":
            return _truthy(ev(0)) or _truthy(ev(1))
        if name in ("RELATED",):
            return self.eval(args[0], ctx)
        if name == "EARLIER":
            key = _key(args[0][1], args[0][2])
            hits = [rc for rc in ctx.rows if key in rc.values]
            if len(hits) < 2:
                raise DaxError("EARLIER without outer row context")
            return hits[-2].values[key]
        if name == "SELECTEDVALUE":
            key = _key(args[0][1], args[0][2])
            vals = self._distinct([key], ctx.filters)[key].tolist()
            vals = list(dict.fromkeys(_py(v) for v in vals))
            if len(vals) == 1:
                return vals[0]
            return ev(1) if len(args) > 1 else None
        if name == "HASONEVALUE":
            key = _key(args[0][1], args[0][2])
            return len(self._distinct([key], ctx.filters)) == 1
        # ---- aggregations over a column
        if name in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK",
                    "MEDIAN", "STDEV.S", "STDEV.P", "VAR.S", "VAR.P", "PERCENTILE.INC", "COUNTBLANK") \
                and args and args[0][0] == "col" and not (name in ("MIN", "MAX") and len(args) == 2):
            key = _key(args[0][1], args[0][2])
            vals = self.column_values(key, ctx)
            if name == "PERCENTILE.INC":
                return _agg("PERCENTILE.INC", vals, ev(1))
            return _agg(name, vals)
        if name in ("MIN", "MAX") and len(args) == 2:
            a, b = ev(0), ev(1)
            vals = [v for v in (a, b) if v is not None]
            if not vals:
                return None
            if len(vals) == 1:
                vals.append(0)   # DAX: BLANK treated as 0 in scalar MIN/MAX
            return min(vals) if name == "MIN" else max(vals)
        if name == "COUNTROWS":
            if not args:
                raise DaxError("COUNTROWS() needs a table")
            tv = self.table(args[0], ctx)
            n = len(tv.df)
            return n if n else None
        # ---- iterators
        if name in ("SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "MEDIANX", "STDEVX.S", "STDEVX.P",
                    "VARX.S", "VARX.P", "PERCENTILEX.INC", "CONCATENATEX"):
            base_ctx = ctx
            tv = self.table(args[0], base_ctx)
            vals = []
            for rec in tv.df.to_dict("records"):
                rec = {k: _py(v) for k, v in rec.items()}
                own = tuple(self.own_keys(tv.base)) if tv.base else tuple(tv.lineage)
                rc = RowCtx(tv.ident, rec, own)
                vals.append(self.eval(args[1], Ctx(base_ctx.filters, ctx.rows + (rc,), ctx.env)))
            agg = {"SUMX": "SUM", "AVERAGEX": "AVERAGE", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT",
                   "MEDIANX": "MEDIAN", "STDEVX.S": "STDEV.S", "STDEVX.P": "STDEV.P", "VARX.S": "VAR.S",
                   "VARX.P": "VAR.P", "PERCENTILEX.INC": "PERCENTILE.INC"}.get(name)
            if name == "CONCATENATEX":
                return ", ".join(_text(v) for v in vals)
            if agg == "PERCENTILE.INC":
                return _agg(agg, vals, ev(2))
            return _agg(agg, vals)
        if name == "RANKX":
            tv = self.table(args[0], ctx)
            vals = []
            for rec in tv.df.to_dict("records"):
                rec = {k: _py(v) for k, v in rec.items()}
                own = tuple(self.own_keys(tv.base)) if tv.base else tuple(tv.lineage)
                rc = RowCtx(tv.ident, rec, own)
                vals.append(self.eval(args[1], Ctx(ctx.filters, ctx.rows + (rc,), ctx.env)))
            value = ev(2) if len(args) > 2 and args[2][0] != "missing" else self.eval(args[1], ctx)
            order = "DESC"
            if len(args) > 3 and args[3][0] != "missing":
                order = self.eval(args[3], ctx)
                order = order[1] if isinstance(order, tuple) else ("ASC" if order in (1, True) else "DESC")
            ties = "SKIP"
            if len(args) > 4 and args[4][0] != "missing":
                ties = self.eval(args[4], ctx)[1]
            vv = 0 if value is None else value
            nums = [0 if v is None else v for v in vals]
            if ties == "DENSE":
                better = {v for v in nums if (v > vv if order == "DESC" else v < vv)}
                return 1 + len(better)
            return 1 + sum(1 for v in nums if (v > vv if order == "DESC" else v < vv))
        if name == "ROWNUMBER":
            rel = self.table(args[0], ctx)
            keys, dirs = self._orderby(args[1], ctx)
            recs = self._sorted_rel(rel, keys, dirs)
            cur = self._current(keys, ctx)
            for i, r in enumerate(recs):
                if tuple(_norm(_py(r[k])) for k in keys) == cur:
                    return i + 1
            return None
        # ---- scalar functions
        return self._scalar(name, args, ctx)

    def _scalar(self, name, args, ctx):
        vals = [self.eval(a, ctx) for a in args]
        v0 = vals[0] if vals else None
        if name in ("LEFT", "RIGHT"):
            if v0 is None:
                return ""
            n = int(vals[1]) if len(vals) > 1 else 1
            s = _text(v0)
            return s[:n] if name == "LEFT" else (s[-n:] if n else "")
        if name == "MID":
            s = _text(v0)
            st, ln = int(vals[1]), int(vals[2])
            return s[st - 1:st - 1 + ln]
        if name == "LEN":
            return len(_text(v0))
        if name == "UPPER":
            return _text(v0).upper()
        if name == "LOWER":
            return _text(v0).lower()
        if name == "TRIM":
            return re.sub(r" +", " ", _text(v0).strip())
        if name == "PROPER":
            return _text(v0).title()
        if name == "SUBSTITUTE":
            return _text(v0).replace(_text(vals[1]), _text(vals[2]))
        if name == "REPT":
            return _text(v0) * int(vals[1])
        if name in ("FIND", "SEARCH"):
            find, within = _text(vals[0]), _text(vals[1])
            start = int(vals[2]) if len(vals) > 2 and vals[2] is not MISSING else 1
            nf = vals[3] if len(vals) > 3 else MISSING
            hay = within if name == "FIND" else within.lower()
            needle = find if name == "FIND" else find.lower()
            i = hay.find(needle, start - 1)
            if i < 0:
                if nf is MISSING:
                    raise DaxError(f"{name}: text not found")
                return nf
            return i + 1
        if name == "CONTAINSSTRING":
            return _text(vals[1]).lower() in _text(v0).lower()
        if name == "CONCATENATE":
            return _text(vals[0]) + _text(vals[1])
        if name == "UNICHAR":
            return chr(int(v0))
        if name == "UNICODE":
            return ord(_text(v0)[0])
        if name == "PATHITEM":
            parts = _text(v0).split("|")
            i = int(vals[1])
            return parts[i - 1] if 0 < i <= len(parts) else None
        if name == "PATHITEMREVERSE":
            parts = _text(v0).split("|")[::-1]
            i = int(vals[1])
            return parts[i - 1] if 0 < i <= len(parts) else None
        if name == "CONVERT":
            target = vals[1][1] if isinstance(vals[1], tuple) else str(vals[1]).upper()
            if v0 is None:
                return None
            if target == "STRING":
                return _text(v0)
            if target in ("DOUBLE", "CURRENCY", "DECIMAL"):
                return float(v0)
            if target in ("INTEGER", "INT64"):
                return int(round(float(v0)))
            if target == "BOOLEAN":
                return _truthy(v0)
            return v0
        if name == "VALUE":
            return float(_text(v0))
        if name == "FORMAT":
            return _format(v0, vals[1])
        # dates
        if name in ("YEAR", "MONTH", "DAY", "QUARTER", "HOUR", "MINUTE", "SECOND"):
            d = _to_dt(v0)
            if d is None:
                return None
            return {"YEAR": d.year, "MONTH": d.month, "DAY": d.day, "QUARTER": (d.month - 1) // 3 + 1,
                    "HOUR": d.hour, "MINUTE": d.minute, "SECOND": d.second}[name]
        if name == "WEEKDAY":
            d = _to_dt(v0)
            if d is None:
                return None
            rt = int(vals[1]) if len(vals) > 1 else 1
            wd = d.isoweekday()  # Mon=1..Sun=7
            if rt == 1:
                return wd % 7 + 1
            if rt == 2:
                return wd
            return wd - 1
        if name == "WEEKNUM":
            d = _to_dt(v0)
            if d is None:
                return None
            rt = int(vals[1]) if len(vals) > 1 else 1
            if rt == 21:
                return d.isocalendar()[1]
            jan1 = datetime(d.year, 1, 1)
            offset = (jan1.isoweekday() % 7) if rt == 1 else (jan1.isoweekday() - 1)
            return ((d - jan1).days + offset) // 7 + 1
        if name == "DATE":
            y, m, dd = int(vals[0]), int(vals[1]), int(vals[2])
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            return datetime(y, m, 1) + timedelta(days=dd - 1)
        if name == "TIME":
            return (int(vals[0]) * 3600 + int(vals[1]) * 60 + int(vals[2])) / 86400.0
        if name == "EDATE":
            d = _to_dt(v0)
            if d is None:
                return None
            n = int(vals[1])
            y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
            import calendar
            day = min(d.day, calendar.monthrange(y, m)[1])
            return datetime(y, m, day, d.hour, d.minute, d.second)
        if name == "DATEDIFF":
            a, b = _to_dt(vals[0]), _to_dt(vals[1])
            if a is None or b is None:
                return None
            unit = vals[2][1] if isinstance(vals[2], tuple) else str(vals[2])
            if unit == "DAY":
                return (b.date() - a.date()).days
            if unit == "MONTH":
                return (b.year - a.year) * 12 + b.month - a.month
            if unit == "YEAR":
                return b.year - a.year
            if unit == "QUARTER":
                return (b.year - a.year) * 4 + ((b.month - 1) // 3) - ((a.month - 1) // 3)
            if unit == "WEEK":
                sa = a.date() - timedelta(days=a.isoweekday() % 7)
                sb = b.date() - timedelta(days=b.isoweekday() % 7)
                return (sb - sa).days // 7
            if unit == "HOUR":
                return int((b - a).total_seconds() // 3600)
            if unit == "MINUTE":
                return int((b - a).total_seconds() // 60)
            if unit == "SECOND":
                return int((b - a).total_seconds())
            raise DaxError(f"DATEDIFF unit {unit}")
        if name == "TODAY":
            return datetime(self.today.year, self.today.month, self.today.day)
        if name == "NOW":
            return datetime(self.today.year, self.today.month, self.today.day)
        if name == "DATEVALUE":
            return pd.Timestamp(_text(v0)).to_pydatetime().replace(hour=0, minute=0, second=0)
        # math
        if name == "ABS":
            return None if v0 is None else abs(v0)
        if name == "ROUND":
            if v0 is None:
                return None
            d = int(vals[1]) if len(vals) > 1 else 0
            q = 10 ** d
            return math.floor(abs(v0) * q + 0.5) / q * (1 if v0 >= 0 else -1)
        if name == "TRUNC":
            if v0 is None:
                return None
            if isinstance(v0, datetime):
                return datetime(v0.year, v0.month, v0.day)
            d = int(vals[1]) if len(vals) > 1 else 0
            q = 10 ** d
            return math.trunc(v0 * q) / q if d else math.trunc(v0)
        if name == "INT":
            return None if v0 is None else math.floor(v0)
        if name == "FLOOR":
            if v0 is None:
                return None
            sig = vals[1] if len(vals) > 1 else 1
            return math.floor(v0 / sig) * sig
        if name == "CEILING":
            if v0 is None:
                return None
            sig = vals[1] if len(vals) > 1 else 1
            return math.ceil(v0 / sig) * sig
        if name == "MOD":
            return None if v0 is None else v0 - vals[1] * math.floor(v0 / vals[1])
        if name == "QUOTIENT":
            return int(v0 / vals[1])
        if name == "POWER":
            return None if v0 is None else v0 ** vals[1]
        if name == "SQRT":
            return None if v0 is None else math.sqrt(v0)
        if name == "EXP":
            return math.exp(v0)
        if name == "LN":
            return math.log(v0)
        if name == "LOG":
            return math.log(v0, vals[1] if len(vals) > 1 else 10)
        if name == "SIGN":
            return 0 if not v0 else (1 if v0 > 0 else -1)
        if name == "PI":
            return math.pi
        if name in ("SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN", "DEGREES", "RADIANS"):
            return getattr(math, name.lower())(v0)
        if name == "USERPRINCIPALNAME" or name == "USERNAME":
            return "validator@local"
        raise DaxError(f"Unsupported DAX function {name}()")

    # ============================================================ query ==
    def query(self, dims: list, measures: list, filters: list = (), value_filters: list = ()):
        """Run a SUMMARIZECOLUMNS-like visual query.

        dims:          [(table, column)]
        measures:      [measure names]
        filters:       [ColF] shadow (visual/page) filters on columns
        value_filters: [(measure, min, max, include_null)] applied per group (HAVING)
        """
        self._eval_cache.clear()
        shadow = tuple(filters)
        self.shadow = shadow
        keys = [_key(t, c) for t, c in dims]
        if keys:
            combos = self._distinct(keys, shadow)
            groups = [tuple(_py(v) for v in r) for r in combos.itertuples(index=False, name=None)]
        else:
            groups = [()]
        if value_filters:
            passing = []
            for g in groups:
                cf = shadow + tuple(ColF(k, frozenset([_norm(v)])) for k, v in zip(keys, g))
                ok = True
                for m, lo, hi, inc_null in value_filters:
                    v = self._measure(m, Ctx(cf))
                    if v is None:
                        ok = ok and inc_null
                        continue
                    if lo is not None and v < lo - 1e-9:
                        ok = False
                    if hi is not None and v > hi + 1e-9:
                        ok = False
                groups_ok = ok
                if groups_ok:
                    passing.append(g)
            groups = passing
            if keys:
                shadow = shadow + (TupF(tuple(keys), frozenset(tuple(_norm(x) for x in g) for g in groups)),)
                self.shadow = shadow
                self._eval_cache.clear()
        rows = []
        for g in groups:
            cf = shadow + tuple(ColF(k, frozenset([_norm(v)])) for k, v in zip(keys, g))
            vals = []
            for m in measures:
                try:
                    v = self._measure(m, Ctx(cf))
                except DaxError as e:
                    self.errors.append(f"[{m}]: {e}")
                    v = None
                vals.append(_py(v))
            if measures and all(v is None for v in vals):
                continue
            rows.append(list(g) + vals)
        cols = [f"{t}[{c}]" for t, c in dims] + list(measures)
        return pd.DataFrame(rows, columns=cols)

    def evaluate_scalar(self, expr: str, filters: tuple = ()):
        self.shadow = tuple(filters)
        return self.eval(parse_dax(expr), Ctx(tuple(filters)))


# ============================================================ helpers ==
def _walk(node):
    yield node
    if isinstance(node, tuple):
        for x in node[1:]:
            if isinstance(x, tuple) and x and isinstance(x[0], str):
                yield from _walk(x)
            elif isinstance(x, list):
                for y in x:
                    if isinstance(y, tuple) and y and isinstance(y[0], str):
                        yield from _walk(y)
                    elif isinstance(y, (list, tuple)):
                        for z in y:
                            if isinstance(z, tuple) and z and isinstance(z[0], str):
                                yield from _walk(z)


_AGG_FUNCS = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK",
              "MEDIAN", "COUNTROWS"}
_PURE: dict = {}


def _pure(node) -> bool:
    """True for expressions that only depend on the filter context (no row context)."""
    k = id(node)
    if k in _PURE:
        return _PURE[k]
    r = False
    if node[0] == "func" and node[1] in _AGG_FUNCS and all(a[0] in ("col", "table") for a in node[2]):
        r = True
    elif node[0] == "meas":
        r = True
    elif node[0] in ("num", "str"):
        r = True
    elif node[0] == "bin":
        r = _pure(node[2]) and _pure(node[3])
    elif node[0] == "func" and node[1] in ("DIVIDE", "COALESCE", "IF") and all(_pure(a) for a in node[2]):
        r = True
    _PURE[k] = r
    return r


def _project(f: TupF, keys):
    if len(keys) == 1:
        i = f.keys.index(keys[0])
        return ColF(keys[0], frozenset(t[i] for t in f.values))
    idx = [f.keys.index(k) for k in keys]
    return TupF(tuple(keys), frozenset(tuple(t[i] for i in idx) for t in f.values))


def _norm(v):
    v = _py(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, date) and not isinstance(v, datetime):
        return datetime(v.year, v.month, v.day)
    return v


def _sort_key(v):
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, datetime):
        return (2, v.timestamp())
    return (3, str(v))


def _truthy(v):
    if v is None:
        return False
    if isinstance(v, str):
        if v.lower() in ("true", "false"):
            return v.lower() == "true"
        return bool(v)
    return bool(v)


def _text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.strftime("%m/%d/%Y") if (v.hour, v.minute) == (0, 0) else v.isoformat()
    return str(v)


def _cmp_pair(a, b):
    if a is None and b is None:
        return 0, 0
    if a is None:
        a = "" if isinstance(b, str) else (datetime.min if isinstance(b, datetime) else 0)
    if b is None:
        b = "" if isinstance(a, str) else (datetime.min if isinstance(a, datetime) else 0)
    if isinstance(a, bool) and not isinstance(b, bool):
        a = int(a)
    if isinstance(b, bool) and not isinstance(a, bool):
        b = int(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.lower(), b.lower()          # DAX comparisons are case-insensitive
    if isinstance(a, str) != isinstance(b, str):
        return str(a), str(b)
    return a, b


def _cmp_eq(a, b):
    x, y = _cmp_pair(a, b)
    return x == y


def _agg(name, vals, p=None):
    if name == "COUNTBLANK":
        return sum(1 for v in vals if v is None) or None
    if name == "DISTINCTCOUNT":
        n = len({_norm(v) for v in vals})
        return n or None
    nn = [v for v in vals if v is not None]
    if name == "COUNTA" or name == "COUNT":
        return len(nn) or None
    if name == "DISTINCTCOUNTNOBLANK":
        return len({_norm(v) for v in nn}) or None
    if not nn:
        return None
    if name == "SUM":
        return float(sum(nn)) if any(isinstance(v, float) for v in nn) else sum(nn)
    if name == "AVERAGE":
        return sum(nn) / len(nn)
    if name == "MIN":
        return min(nn)
    if name == "MAX":
        return max(nn)
    if name == "MEDIAN":
        return float(np.median(nn))
    if name in ("STDEV.S", "VAR.S"):
        if len(nn) < 2:
            return None
        v = float(np.var(nn, ddof=1))
        return math.sqrt(v) if name == "STDEV.S" else v
    if name in ("STDEV.P", "VAR.P"):
        v = float(np.var(nn))
        return math.sqrt(v) if name == "STDEV.P" else v
    if name == "PERCENTILE.INC":
        return float(np.percentile(nn, float(p) * 100))
    raise DaxError(f"Unknown aggregate {name}")


def _format(v, fmt):
    if v is None:
        return ""
    fmt = str(fmt)
    d = _to_dt(v) if isinstance(v, (datetime, date)) else None
    if d is not None:
        mapping = {"MMMM": d.strftime("%B"), "MMM": d.strftime("%b"), "dddd": d.strftime("%A"),
                   "ddd": d.strftime("%a"), "yyyy": str(d.year), "d": str(d.day),
                   "yyyy-mm-dd": d.strftime("%Y-%m-%d"), "mm/dd/yyyy": d.strftime("%m/%d/%Y")}
        if fmt in mapping:
            return mapping[fmt]
        return d.strftime("%Y-%m-%d")
    if fmt in ("0", "General", "General Number"):
        return _text(round(v) if fmt == "0" else v)
    return _text(v)
