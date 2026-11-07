"""Tableau reference engine: computes worksheet results with *Tableau* semantics.

Implements, independently of the DAX code path:

* record-level calculations with Tableau NULL propagation (NULL+1 = NULL, NULL
  comparisons are unknown, IF unknown -> ELSE),
* FIXED LODs attached to every record (computed after context filters, before
  dimension filters), INCLUDE / EXCLUDE evaluated at view grain,
* Tableau's order of operations: context filters -> FIXED/Top N/sets ->
  dimension filters -> aggregation -> measure filters -> table calculations,
* table calculations (RUNNING_*, WINDOW_*, TOTAL, RANK*, INDEX, LOOKUP, SIZE)
  with addressing/partitioning from the worksheet (Table across/down, Rows,
  Columns, specific fields),
* groups, bins, member sets and Top-N sets.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from ..formula.ast import (AGGREGATES, TABLE_CALCS, Bool, BinOp, Case, DateLit, Field, Func, If, In,
                           LOD, Node, Null, Num, Str, Unary, is_aggregate, is_scalar_minmax)
from ..formula.parser import parse_formula
from ..model import AGG_DERIVATIONS, Datasource, FieldRef, Filter, Workbook, Worksheet


class TableauEvalError(Exception):
    pass


DATE_PARTS = {"yr", "qr", "mn", "dy", "wk", "tyr", "tqr", "tmn", "my", "twk", "tdy"}
AGG_OF = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "cnt": "COUNT", "count": "COUNT",
          "ctd": "COUNTD", "cntd": "COUNTD", "med": "MEDIAN", "attr": "ATTR", "stdev": "STDEV",
          "stdevp": "STDEVP", "var": "VAR", "varp": "VARP"}


def _nz(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, np.generic):
        v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    if v is pd.NaT:
        return None
    return v


def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return pd.Timestamp(str(v)).to_pydatetime()


class TableauEngine:
    def __init__(self, wb: Workbook, ds: Datasource, records: pd.DataFrame, today: Optional[date] = None,
                 user: Optional[str] = None):
        """``user`` is the identity returned by USERNAME(). With ``user=None`` user filters
        (data source filters built on USERNAME()) are not applied - the unsecured view the
        model owner sees in Power BI - so numeric parity and security are tested separately."""
        self.user = user
        self.wb, self.ds = wb, ds
        self.today = today or date.today()
        self.n = len(records)
        self.cols = {c: [_nz(v) for v in records[c].tolist()] for c in records.columns}
        self.params = {}
        for p in wb.parameters.values():
            self.params[p.internal_name] = p.current_value
            self.params[p.caption] = p.current_value
        self.calcs = {}
        self.asts = {}
        for name, cf in ds.calculations.items():
            self.calcs[name] = cf
            self.calcs.setdefault(cf.caption, cf)
            if cf.kind == "formula" and cf.formula:
                ast = parse_formula(cf.formula)
                self.asts[name] = ast
                self.asts.setdefault(cf.caption, ast)
        self._vec = {}
        self.context = np.ones(self.n, dtype=bool)
        # Relationships (logical layer): fields of a related table are aggregated at that table's
        # native level of detail - each related row counts once, not once per fact record.
        self.root = ds.tables[0].name if ds.tables else None
        self.row_ids = {c[len("__row__"):]: self.cols[c] for c in self.cols if c.startswith("__row__")}
        self.col_table = {name: col.table for name, col in ds.columns.items()}

    # ---------------------------------------------------------- lookup --
    def _calc_ast(self, name):
        return self.asts.get(name)

    def is_agg_calc(self, name):
        ast = self._calc_ast(name)
        return ast is not None and is_aggregate(ast, self._calc_ast) and not isinstance(ast, LOD)

    # ----------------------------------------------------- row vectors --
    def field_vector(self, name):
        if name in self.cols:
            return self.cols[name]
        for cname, col in self.ds.columns.items():
            if col.caption == name and cname in self.cols:
                return self.cols[cname]
        cf = self.calcs.get(name)
        if cf is None:
            raise TableauEvalError(f"Unknown field [{name}]")
        key = ("calc", cf.internal_name, self._ctx_key())
        if key in self._vec:
            return self._vec[key]
        if cf.kind == "group":
            src = self.field_vector(cf.extra["source"])
            mapping = {}
            for b in cf.extra.get("bins", []):
                for v in b["values"]:
                    mapping[v] = b["label"]
            other = cf.extra.get("other")
            vec = [mapping.get(v, other if other else v) for v in src]
        elif cf.kind == "bin":
            src = self.field_vector(cf.extra["source"])
            size = cf.extra.get("size") or "1"
            if str(size).startswith("[Parameters]"):
                size = self.params.get(str(size).split("].[")[-1].strip("[]"))
            size = float(size)
            vec = [None if v is None else math.floor(v / size) * size for v in src]
            vec = [int(v) if v is not None and float(v).is_integer() else v for v in vec]
        elif cf.kind == "set":
            vec = self._set_vector(cf)
        else:
            ast = self.asts.get(cf.internal_name)
            if ast is None:
                raise TableauEvalError(f"Calculation [{name}] has no formula")
            if is_aggregate(ast, self._calc_ast):
                raise TableauEvalError(f"[{name}] is an aggregate calculation")
            vec = self.row_vector(ast)
        self._vec[key] = vec
        return vec

    def _ctx_key(self):
        return hash(self.context.tobytes()) if not self.context.all() else 0

    def _set_vector(self, cf):
        e = cf.extra
        src = self.field_vector(e["source"])
        if e.get("set_type") == "members":
            members = set(e.get("members") or [])
            return [v in members for v in src]
        if e.get("set_type") == "top":
            ast = parse_formula(e.get("expression") or "COUNT([Number of Records])")
            idx_by = {}
            for i, v in enumerate(src):
                if self.context[i]:
                    idx_by.setdefault(v, []).append(i)
            scores = {k: self.agg(ast, np.array(ix)) for k, ix in idx_by.items()}
            count = e.get("count") or "10"
            if str(count).startswith("[Parameters]"):
                count = self.params.get(str(count).split("].[")[-1].strip("[]"))
            n = int(float(count))
            desc = e.get("end", "top") == "top"
            ranked = sorted(scores, key=lambda k: (scores[k] is None, -(scores[k] or 0) if desc else (scores[k] or 0)))
            top = set(ranked[:n])
            return [v in top for v in src]
        raise TableauEvalError(f"Unsupported set type {e.get('set_type')}")

    def tables_of(self, node: Node, _seen=None) -> set:
        _seen = _seen or set()
        out = set()
        for n in node.walk():
            if isinstance(n, LOD):
                out.add(self.root)
            if isinstance(n, Field) and not n.is_parameter:
                if n.name in self.col_table:
                    out.add(self.col_table[n.name])
                elif n.name not in _seen and self._calc_ast(n.name) is not None:
                    out |= self.tables_of(self._calc_ast(n.name), _seen | {n.name})
                elif n.name in self.calcs and self.calcs[n.name].extra.get("source"):
                    out.add(self.col_table.get(self.calcs[n.name].extra["source"]))
        out.discard(None)
        return out

    def native_rows(self, arg: Node, idx):
        if self.ds.model_style != "relationships" or not self.row_ids:
            return idx
        tables = self.tables_of(arg)
        if len(tables) != 1:
            return idx
        t = next(iter(tables))
        if t == self.root or t not in self.row_ids:
            return idx
        rid = self.row_ids[t]
        seen, keep = set(), []
        for i in idx:
            r = rid[i]
            if r is None or r in seen:
                continue
            seen.add(r)
            keep.append(i)
        return np.array(keep, dtype=int)

    def row_vector(self, node: Node):
        key = ("node", id(node), self._ctx_key())
        if key in self._vec:
            return self._vec[key]
        if isinstance(node, Field):
            if node.is_parameter:
                v = self.params.get(node.name)
                vec = [v] * self.n
            else:
                vec = self.field_vector(node.name)
        elif isinstance(node, LOD):
            vec = self._lod_vector(node)
        else:
            vec = [self.row_value(node, i) for i in range(self.n)]
        self._vec[key] = vec
        return vec

    def _lod_vector(self, node: LOD):
        """FIXED LOD: aggregate per dims over context-filtered records, attach to every record."""
        if node.kind != "FIXED":
            # row-level INCLUDE/EXCLUDE without a view: behave as FIXED on their dims
            pass
        dims = [self.row_vector(d) for d in node.dims]
        groups = {}
        for i in range(self.n):
            if not self.context[i]:
                continue
            k = tuple(d[i] for d in dims)
            groups.setdefault(k, []).append(i)
        vals = {k: self.agg(node.expr, np.array(ix)) for k, ix in groups.items()}
        return [vals.get(tuple(d[i] for d in dims)) for i in range(self.n)]

    def row_value(self, node: Node, i: int):
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return node.value
        if isinstance(node, Bool):
            return node.value
        if isinstance(node, Null):
            return None
        if isinstance(node, DateLit):
            return _as_dt(node.value)
        if isinstance(node, (Field, LOD)):
            return self.row_vector(node)[i]
        if isinstance(node, BinOp):
            return self._binop(node.op, lambda: self.row_value(node.left, i), lambda: self.row_value(node.right, i))
        if isinstance(node, Unary):
            v = self.row_value(node.operand, i)
            if node.op == "NOT":
                return None if v is None else (not v)
            return None if v is None else -v
        if isinstance(node, If):
            for c, val in node.branches:
                if self.row_value(c, i) is True:
                    return self.row_value(val, i)
            return self.row_value(node.else_, i) if node.else_ is not None else None
        if isinstance(node, Case):
            s = self.row_value(node.subject, i)
            for m, val in node.whens:
                if s is not None and _eq(s, self.row_value(m, i)):
                    return self.row_value(val, i)
            return self.row_value(node.else_, i) if node.else_ is not None else None
        if isinstance(node, In):
            s = self.row_value(node.subject, i)
            if s is None:
                return None
            r = any(_eq(s, self.row_value(v, i)) for v in node.values)
            return (not r) if node.negated else r
        if isinstance(node, Func):
            if (node.name in AGGREGATES and not is_scalar_minmax(node)) or node.name in TABLE_CALCS:
                raise TableauEvalError(f"{node.name}() in a row-level context")
            return self._scalar(node, lambda a: self.row_value(a, i))
        raise TableauEvalError(f"Cannot evaluate {type(node).__name__}")

    # -------------------------------------------------------- operators --
    def _binop(self, op, fa, fb):
        if op == "AND":
            a = fa()
            if a is False:
                return False
            b = fb()
            if b is False:
                return False
            if a is None or b is None:
                return None
            return True
        if op == "OR":
            a = fa()
            if a is True:
                return True
            b = fb()
            if b is True:
                return True
            if a is None or b is None:
                return None
            return False
        a, b = fa(), fb()
        if a is None or b is None:
            return None
        if op == "+":
            if isinstance(a, str) or isinstance(b, str):
                return f"{a}{b}"
            if isinstance(a, datetime):
                return a + timedelta(days=b)
            return a + b
        if op == "-":
            if isinstance(a, datetime) and isinstance(b, datetime):
                return (a - b).total_seconds() / 86400.0
            if isinstance(a, datetime):
                return a - timedelta(days=b)
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                return None
            return a / b
        if op == "%":
            return math.fmod(a, b) if b else None
        if op == "^":
            return a ** b
        if isinstance(a, datetime) or isinstance(b, datetime):
            a, b = _as_dt(a), _as_dt(b)
        if isinstance(a, bool) and not isinstance(b, bool):
            a = int(a)
        if op == "=":
            return _eq(a, b)
        if op == "<>":
            return not _eq(a, b)
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    def _scalar(self, node: Func, ev):
        name = node.name
        args = node.args
        a = lambda k: ev(args[k])
        if name == "ZN":
            v = a(0)
            return 0 if v is None else v
        if name == "IFNULL":
            v = a(0)
            return a(1) if v is None else v
        if name == "ISNULL":
            return a(0) is None
        if name == "IIF":
            c = a(0)
            if c is True:
                return a(1)
            if c is False:
                return a(2)
            return a(3) if len(args) > 3 else None
        if name in ("MIN", "MAX"):     # two-argument scalar form
            x, y = a(0), a(1)
            if x is None or y is None:
                return None
            return min(x, y) if name == "MIN" else max(x, y)
        vals = [a(k) for k in range(len(args))]
        v0 = vals[0] if vals else None
        if name in ("LEFT", "RIGHT", "MID", "UPPER", "LOWER", "LEN", "TRIM", "LTRIM", "RTRIM", "REPLACE",
                    "CONTAINS", "STARTSWITH", "ENDSWITH", "FIND", "SPLIT", "STR", "PROPER", "SPACE"):
            if v0 is None and name != "STR":
                return None
            if name == "LEFT":
                return str(v0)[:int(vals[1])]
            if name == "RIGHT":
                n = int(vals[1])
                return str(v0)[-n:] if n else ""
            if name == "MID":
                st = int(vals[1]) - 1
                return str(v0)[st:st + int(vals[2])] if len(vals) > 2 else str(v0)[st:]
            if name == "UPPER":
                return str(v0).upper()
            if name == "LOWER":
                return str(v0).lower()
            if name == "LEN":
                return len(str(v0))
            if name == "TRIM":
                return str(v0).strip()
            if name == "LTRIM":
                return str(v0).lstrip()
            if name == "RTRIM":
                return str(v0).rstrip()
            if name == "REPLACE":
                return str(v0).replace(str(vals[1]), str(vals[2]))
            if name == "CONTAINS":
                return str(vals[1]) in str(v0)
            if name == "STARTSWITH":
                return str(v0).startswith(str(vals[1]))
            if name == "ENDSWITH":
                return str(v0).endswith(str(vals[1]))
            if name == "FIND":
                start = int(vals[2]) - 1 if len(vals) > 2 else 0
                return str(v0).find(str(vals[1]), start) + 1
            if name == "SPLIT":
                parts = str(v0).split(str(vals[1]))
                k = int(vals[2])
                try:
                    return parts[k - 1] if k > 0 else parts[k]
                except IndexError:
                    return ""
            if name == "STR":
                if v0 is None:
                    return None
                if isinstance(v0, float) and v0.is_integer():
                    return str(int(v0))
                if isinstance(v0, datetime):
                    return v0.strftime("%Y-%m-%d")
                return str(v0)
            if name == "PROPER":
                return str(v0).title()
            if name == "SPACE":
                return " " * int(v0)
        if name in ("DATEPART", "DATENAME", "DATETRUNC", "DATEADD", "DATEDIFF"):
            part = str(vals[0]).lower()
            if name == "DATEADD":
                k, d = vals[1], _as_dt(vals[2])
                if d is None or k is None:
                    return None
                return _date_add(d, part, int(k))
            if name == "DATEDIFF":
                x, y = _as_dt(vals[1]), _as_dt(vals[2])
                if x is None or y is None:
                    return None
                return _date_diff(part, x, y)
            d = _as_dt(vals[1])
            if d is None:
                return None
            if name == "DATEPART":
                return _date_part(part, d)
            if name == "DATENAME":
                if part == "month":
                    return d.strftime("%B")
                if part == "weekday":
                    return d.strftime("%A")
                if part == "quarter":
                    return f"Q{(d.month - 1) // 3 + 1}"
                return str(_date_part(part, d))
            return _trunc(part, d)
        if name in ("YEAR", "MONTH", "DAY", "QUARTER", "WEEK"):
            d = _as_dt(v0)
            if d is None:
                return None
            return _date_part({"YEAR": "year", "MONTH": "month", "DAY": "day", "QUARTER": "quarter",
                               "WEEK": "week"}[name], d)
        if name == "MAKEDATE":
            return datetime(int(vals[0]), int(vals[1]), int(vals[2]))
        if name in ("USERNAME", "FULLNAME", "USERDOMAIN"):
            return self.user
        if name in ("ISMEMBEROF", "ISUSERNAME", "ISFULLNAME"):
            return name != "ISMEMBEROF" and self.user is not None and str(vals[0]).lower() == str(self.user).lower()
        if name == "TODAY" or name == "NOW":
            return datetime(self.today.year, self.today.month, self.today.day)
        if name == "DATE":
            d = _as_dt(v0)
            return None if d is None else datetime(d.year, d.month, d.day)
        if name in ("INT", "FLOAT", "ROUND", "ABS", "CEILING", "FLOOR", "SQRT", "POWER", "EXP", "LN", "LOG",
                    "SIGN", "DIV", "SQUARE", "PI"):
            if name == "PI":
                return math.pi
            if v0 is None:
                return None
            if name == "INT":
                return int(v0)          # truncates toward zero
            if name == "FLOAT":
                return float(v0)
            if name == "ROUND":
                d = int(vals[1]) if len(vals) > 1 else 0
                q = 10 ** d
                return math.floor(abs(v0) * q + 0.5) / q * (1 if v0 >= 0 else -1)
            if name == "ABS":
                return abs(v0)
            if name == "CEILING":
                return math.ceil(v0)
            if name == "FLOOR":
                return math.floor(v0)
            if name == "SQRT":
                return math.sqrt(v0)
            if name == "POWER":
                return v0 ** vals[1]
            if name == "SQUARE":
                return v0 * v0
            if name == "EXP":
                return math.exp(v0)
            if name == "LN":
                return math.log(v0)
            if name == "LOG":
                return math.log(v0, vals[1] if len(vals) > 1 else 10)
            if name == "SIGN":
                return 0 if v0 == 0 else (1 if v0 > 0 else -1)
            if name == "DIV":
                return int(v0 / vals[1])
        raise TableauEvalError(f"Function {name}() not supported by the reference engine")

    # ------------------------------------------------------ aggregates --
    def agg(self, node: Node, idx: np.ndarray, view=None):
        """Evaluate an aggregate expression over the records ``idx``."""
        if isinstance(node, Func) and node.name in AGGREGATES and not is_scalar_minmax(node):
            vec = self.row_vector(node.args[0])
            idx = self.native_rows(node.args[0], idx)
            vals = [vec[i] for i in idx]
            return _aggregate(node.name, vals, None if len(node.args) < 2 else self.agg(node.args[1], idx, view))
        if isinstance(node, Func) and node.name in TABLE_CALCS:
            if view is None:
                raise TableauEvalError(f"{node.name}() needs a view (table calculation)")
            return view.table_calc(node)
        if isinstance(node, (Num, Str, Bool, Null, DateLit)):
            return self.row_value(node, 0) if self.n else None
        if isinstance(node, Field):
            if node.is_parameter:
                return self.params.get(node.name)
            ast = self._calc_ast(node.name)
            if ast is not None and (is_aggregate(ast, self._calc_ast)) and not isinstance(ast, LOD):
                return self.agg(ast, idx, view)
            raise TableauEvalError(f"Non-aggregated field [{node.name}] in aggregate context")
        if isinstance(node, BinOp):
            return self._binop(node.op, lambda: self.agg(node.left, idx, view), lambda: self.agg(node.right, idx, view))
        if isinstance(node, Unary):
            v = self.agg(node.operand, idx, view)
            if node.op == "NOT":
                return None if v is None else (not v)
            return None if v is None else -v
        if isinstance(node, If):
            for c, val in node.branches:
                if self.agg(c, idx, view) is True:
                    return self.agg(val, idx, view)
            return self.agg(node.else_, idx, view) if node.else_ is not None else None
        if isinstance(node, Case):
            s = self.agg(node.subject, idx, view)
            for m, val in node.whens:
                if s is not None and _eq(s, self.agg(m, idx, view)):
                    return self.agg(val, idx, view)
            return self.agg(node.else_, idx, view) if node.else_ is not None else None
        if isinstance(node, Func):
            return self._scalar(node, lambda a: self.agg(a, idx, view))
        if isinstance(node, LOD):
            raise TableauEvalError("LOD used directly in an aggregate context")
        raise TableauEvalError(f"Cannot aggregate {type(node).__name__}")

    # ------------------------------------------------------- worksheet --
    def is_measure(self, ref: FieldRef):
        if ref.derivation in AGG_DERIVATIONS:
            return True
        if ref.derivation in DATE_PARTS:
            return False
        return self.is_agg_calc(ref.field)

    def dim_vector(self, ref: FieldRef):
        base = self.field_vector(ref.field)
        if ref.derivation in DATE_PARTS:
            out = []
            for v in base:
                d = _as_dt(v)
                if d is None:
                    out.append(None)
                    continue
                out.append({"yr": d.year, "qr": (d.month - 1) // 3 + 1, "mn": d.month, "dy": d.day,
                            "wk": _date_part("week", d)}.get(ref.derivation)
                           if ref.derivation in ("yr", "qr", "mn", "dy", "wk") else
                           _trunc({"tyr": "year", "tqr": "quarter", "tmn": "month", "my": "month",
                                   "twk": "week", "tdy": "day"}[ref.derivation], d))
            return out
        return base

    def run_worksheet(self, ws: Worksheet) -> pd.DataFrame:
        shelf = ws.rows + ws.cols + [r for v in ws.encodings.values() for r in v]
        dims, meas = [], []
        for r in shelf:
            (meas if self.is_measure(r) else dims).append(r)
        dims = _uniq(dims)
        meas = _uniq(meas)
        # 0. data source filters (incl. user filters when an identity is set), 1. context filters
        self.context = self.datasource_mask()
        for f in ws.filters:
            if f.is_context:
                self.context &= self._dim_filter_mask(f)
        self._vec = {k: v for k, v in self._vec.items() if k[-1] == 0} if not self.context.all() else self._vec
        mask = self.context.copy()
        # 2. Top N (computed on the context, before dimension filters)
        for f in ws.filters:
            if f.kind == "top" and not f.is_context:
                mask &= self._top_mask(f)
        # 3. dimension filters
        for f in ws.filters:
            if f.is_context or f.kind == "top":
                continue
            if self.is_measure(f.field):
                continue
            mask &= self._dim_filter_mask(f)
        idx_all = np.nonzero(mask)[0]
        # 4. group by view dims
        dvecs = [self.dim_vector(d) for d in dims]
        cells = {}
        for i in idx_all:
            cells.setdefault(tuple(v[i] for v in dvecs), []).append(i)
        cells = {k: np.array(v) for k, v in cells.items()}
        view = _View(self, ws, dims, cells, dvecs, idx_all)
        # 5. measure filters
        for f in ws.filters:
            if self.is_measure(f.field) and f.kind == "quantitative":
                keep = {}
                for k, ix in cells.items():
                    view.current = k
                    v = self.measure_value(f.field, ix, view)
                    ok = True
                    if v is None:
                        ok = f.include_null
                    else:
                        if f.min is not None and v < float(f.min) - 1e-9:
                            ok = False
                        if f.max is not None and v > float(f.max) + 1e-9:
                            ok = False
                    if ok:
                        keep[k] = ix
                cells = keep
                view.cells = cells
        # 6. measures (+ table calcs on the remaining cells)
        rows = []
        for k in sorted(cells, key=lambda t: tuple(_skey(x) for x in t)):
            view.current = k
            row = list(k)
            for m in meas:
                view.pill = m
                row.append(_nz(self.measure_value(m, cells[k], view)))
            rows.append(row)
        return pd.DataFrame(rows, columns=[d.key for d in dims] + [m.key for m in meas])

    def datasource_mask(self):
        m = np.ones(self.n, dtype=bool)
        for f in self.ds.datasource_filters:
            cf = self.ds.calculations.get(f.field.field)
            if cf is not None and cf.extra.get("security") and self.user is None:
                continue
            m &= self._dim_filter_mask(f)
        return m

    def visible_records(self) -> int:
        return int(self.datasource_mask().sum())

    def measure_value(self, ref: FieldRef, idx, view):
        if ref.derivation in ("usr", "none") or (ref.derivation not in AGG_OF and self.is_agg_calc(ref.field)):
            ast = self._calc_ast(ref.field)
            if ast is None:
                raise TableauEvalError(f"Unknown measure [{ref.field}]")
            return self.agg(ast, idx, view)
        fn = AGG_OF.get(ref.derivation, "SUM")
        ast = self._calc_ast(ref.field)
        if isinstance(ast, LOD) and ast.kind in ("INCLUDE", "EXCLUDE"):
            return view.lod_measure(ast, fn, idx)
        if ast is not None and is_aggregate(ast, self._calc_ast):
            return self.agg(ast, idx, view)
        vec = self.field_vector(ref.field)
        idx = self.native_rows(Field(ref.field), idx)
        return _aggregate(fn, [vec[i] for i in idx])

    def _member_match(self, vec, members):
        norm = {_mnorm(m) for m in members}
        return np.array([_mnorm(v) in norm for v in vec], dtype=bool)

    def _dim_filter_mask(self, f: Filter):
        vec = self.dim_vector(f.field)
        if f.kind == "categorical":
            m = self._member_match(vec, f.members)
            return ~m if f.exclude else m
        if f.kind == "categorical-all":
            return np.ones(self.n, dtype=bool)
        if f.kind == "quantitative":
            out = np.ones(self.n, dtype=bool)
            for i, v in enumerate(vec):
                if v is None:
                    out[i] = f.include_null
                    continue
                lo, hi = f.min, f.max
                if isinstance(v, datetime):
                    lo, hi = (_as_dt(lo) if lo is not None else None), (_as_dt(hi) if hi is not None else None)
                if lo is not None and v < lo:
                    out[i] = False
                if hi is not None and v > hi:
                    out[i] = False
            return out
        if f.kind == "relative-date":
            anchor = _as_dt(f.raw) if f.raw else datetime(self.today.year, self.today.month, self.today.day)
            p = f.period_type or "day"
            start = _date_add(_trunc(p, anchor), p, f.first_period or 0)
            end = _date_add(_trunc(p, anchor), p, (f.last_period or 0) + 1)
            return np.array([(v is not None and start <= _as_dt(v) < end) or (v is None and f.include_null)
                             for v in vec], dtype=bool)
        raise TableauEvalError(f"Filter type {f.kind} not supported")

    def _top_mask(self, f: Filter):
        vec = self.dim_vector(f.field)
        n = f.top_n
        if f.top_param:
            n = int(float(self.params.get(f.top_param, 10)))
        ast = parse_formula(f.top_by or "COUNT([Number of Records])")
        groups = {}
        for i in np.nonzero(self.context)[0]:
            groups.setdefault(vec[i], []).append(i)
        scores = {k: self.agg(ast, np.array(ix)) for k, ix in groups.items()}
        desc = f.top_direction == "top"
        ranked = sorted(scores, key=lambda k: (scores[k] is None, -(scores[k] or 0) if desc else (scores[k] or 0)))
        top = set(ranked[:n])
        return np.array([v in top for v in vec], dtype=bool)


class _View:
    """View-level state for table calculations and INCLUDE/EXCLUDE LODs."""

    def __init__(self, eng: TableauEngine, ws: Worksheet, dims, cells, dvecs, idx_all):
        self.eng, self.ws, self.dims, self.cells, self.dvecs = eng, ws, dims, cells, dvecs
        self.idx_all = idx_all
        self.current = None
        self.pill = None
        self._cache = {}

    def _addressing(self):
        tc = (self.pill.table_calc if self.pill is not None else None) or {}
        ot = (tc.get("ordering_type") or "").lower()
        rows = [d for d in self.dims if any(d.key == r.key for r in self.ws.rows)]
        cols = [d for d in self.dims if any(d.key == r.key for r in self.ws.cols)]
        if ot == "rows":
            return rows
        if ot == "columns":
            return cols
        if ot == "field" and tc.get("ordering_fields"):
            sel = [d for d in self.dims if any(d.field in f for f in tc["ordering_fields"])]
            if sel:
                return sel
        return cols or rows

    def partition(self):
        addr = self._addressing()
        ai = [self.dims.index(a) for a in addr]
        pi = [i for i in range(len(self.dims)) if i not in ai]
        cur = self.current
        pkey = tuple(cur[i] for i in pi)
        members = [k for k in self.cells if tuple(k[i] for i in pi) == pkey]
        members.sort(key=lambda k: tuple(_skey(k[i]) for i in ai))
        return members

    def value_in(self, node, cell):
        ck = (id(node), cell, id(self.pill))
        if ck not in self._cache:
            saved = self.current
            self.current = cell
            self._cache[ck] = self.eng.agg(node, self.cells[cell], self)
            self.current = saved
        return self._cache[ck]

    def table_calc(self, node: Func):
        name = node.name
        part = self.partition()
        pos = part.index(self.current)
        inner = node.args[0] if node.args else None
        if name == "INDEX":
            return pos + 1
        if name == "SIZE":
            return len(part)
        if name == "FIRST":
            return -pos
        if name == "LAST":
            return len(part) - 1 - pos
        if name == "TOTAL":
            idx = np.concatenate([self.cells[c] for c in part])
            return self.eng.agg(inner, idx, None)
        vals = [self.value_in(inner, c) for c in part]
        if name.startswith("RUNNING_"):
            seen = [v for v in vals[:pos + 1] if v is not None]
            if not seen:
                return None
            return _aggregate({"RUNNING_SUM": "SUM", "RUNNING_AVG": "AVG", "RUNNING_MIN": "MIN",
                               "RUNNING_MAX": "MAX", "RUNNING_COUNT": "COUNT"}[name], seen)
        if name.startswith("WINDOW_"):
            if len(node.args) > 1:
                s = int(self.eng.agg(node.args[1], self.cells[self.current], None))
                e = int(self.eng.agg(node.args[2], self.cells[self.current], None)) if len(node.args) > 2 else 0
                window = vals[max(0, pos + s):pos + e + 1]
            else:
                window = vals
            return _aggregate({"WINDOW_SUM": "SUM", "WINDOW_AVG": "AVG", "WINDOW_MIN": "MIN",
                               "WINDOW_MAX": "MAX", "WINDOW_COUNT": "COUNT", "WINDOW_MEDIAN": "MEDIAN"}[name],
                              [v for v in window if v is not None])
        if name.startswith("RANK"):
            v = vals[pos]
            if v is None:
                return None
            asc = len(node.args) > 1 and isinstance(node.args[1], Str) and node.args[1].value.lower() == "asc"
            others = [x for x in vals if x is not None]
            if name == "RANK_DENSE":
                better = {x for x in others if (x < v if asc else x > v)}
                return 1 + len(better)
            if name == "RANK_PERCENTILE":
                less = sum(1 for x in others if x < v)
                return less / (len(others) - 1) if len(others) > 1 else 1.0
            return 1 + sum(1 for x in others if (x < v if asc else x > v))
        if name == "LOOKUP":
            off = int(self.eng.agg(node.args[1], self.cells[self.current], None)) if len(node.args) > 1 else 0
            j = pos + off
            return vals[j] if 0 <= j < len(vals) else None
        if name == "PREVIOUS_VALUE":
            return vals[pos - 1] if pos > 0 else None
        raise TableauEvalError(f"Table calculation {name} not supported")

    def lod_measure(self, ast: LOD, outer_fn: str, idx):
        dim_vecs = [self.eng.row_vector(d) for d in ast.dims]
        if ast.kind == "INCLUDE":
            groups = {}
            for i in idx:
                groups.setdefault(tuple(v[i] for v in dim_vecs), []).append(i)
            vals = [self.eng.agg(ast.expr, np.array(ix)) for ix in groups.values()]
            return _aggregate(outer_fn, vals)
        # EXCLUDE: compute at view grain minus the excluded dimensions
        excl = {d.name for d in ast.dims if isinstance(d, Field)}
        keep_i = [i for i, d in enumerate(self.dims) if d.field not in excl]
        cur = self.current
        sel = [i for i in self.idx_all if all(self.dvecs[k][i] == cur[k] for k in keep_i)]
        return _aggregate(outer_fn, [self.eng.agg(ast.expr, np.array(sel))])


# ------------------------------------------------------------- helpers --
def _uniq(refs):
    out, seen = [], set()
    for r in refs:
        if r.key not in seen:
            seen.add(r.key)
            out.append(r)
    return out


def _skey(v):
    if v is None:
        return (0, "")
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, datetime):
        return (2, v.timestamp())
    return (3, str(v))


def _eq(a, b):
    if isinstance(a, datetime) or isinstance(b, datetime):
        return _as_dt(a) == _as_dt(b)
    return a == b


def _mnorm(v):
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", float(v))
    if isinstance(v, datetime):
        return ("d", v.date().isoformat())
    if isinstance(v, str) and len(v) == 10 and v[4:5] == "-" and v[7:8] == "-":
        return ("d", v)
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return ("b", v.lower() == "true")
    return ("s", v)


def _aggregate(fn, vals, p=None):
    nn = [v for v in vals if v is not None]
    if fn == "COUNT":
        return len(nn)
    if fn == "COUNTD":
        return len(set(nn))
    if fn == "ATTR":
        s = set(nn)
        return next(iter(s)) if len(s) == 1 else ("*" if s else None)
    if not nn:
        return None
    if fn == "SUM":
        return sum(nn)
    if fn == "AVG":
        return sum(nn) / len(nn)
    if fn == "MIN":
        return min(nn)
    if fn == "MAX":
        return max(nn)
    if fn == "MEDIAN":
        return float(np.median(nn))
    if fn in ("STDEV", "VAR"):
        if len(nn) < 2:
            return None
        v = float(np.var(nn, ddof=1))
        return math.sqrt(v) if fn == "STDEV" else v
    if fn in ("STDEVP", "VARP"):
        v = float(np.var(nn))
        return math.sqrt(v) if fn == "STDEVP" else v
    if fn == "PERCENTILE":
        return float(np.percentile(nn, float(p) * 100))
    raise TableauEvalError(f"Aggregate {fn} not supported")


def _date_part(part, d: datetime):
    if part == "year":
        return d.year
    if part == "quarter":
        return (d.month - 1) // 3 + 1
    if part == "month":
        return d.month
    if part == "day":
        return d.day
    if part == "weekday":
        return d.isoweekday() % 7 + 1
    if part == "week":
        jan1 = datetime(d.year, 1, 1)
        return ((d - jan1).days + jan1.isoweekday() % 7) // 7 + 1
    if part == "dayofyear":
        return d.timetuple().tm_yday
    if part == "hour":
        return d.hour
    if part == "minute":
        return d.minute
    if part == "second":
        return d.second
    if part == "iso-week":
        return d.isocalendar()[1]
    raise TableauEvalError(f"Date part {part}")


def _trunc(part, d: datetime):
    if part == "year":
        return datetime(d.year, 1, 1)
    if part == "quarter":
        return datetime(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    if part == "month":
        return datetime(d.year, d.month, 1)
    if part == "week":
        dd = datetime(d.year, d.month, d.day)
        return dd - timedelta(days=d.isoweekday() % 7)
    if part == "day":
        return datetime(d.year, d.month, d.day)
    raise TableauEvalError(f"DATETRUNC unit {part}")


def _date_add(d: datetime, part, k: int):
    import calendar
    if part in ("year", "quarter", "month"):
        months = k * {"year": 12, "quarter": 3, "month": 1}[part]
        y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
        return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))
    if part == "week":
        return d + timedelta(days=7 * k)
    if part == "day":
        return d + timedelta(days=k)
    if part == "hour":
        return d + timedelta(hours=k)
    if part == "minute":
        return d + timedelta(minutes=k)
    raise TableauEvalError(f"DATEADD unit {part}")


def _date_diff(part, a: datetime, b: datetime):
    if part == "day":
        return (b.date() - a.date()).days
    if part == "month":
        return (b.year - a.year) * 12 + b.month - a.month
    if part == "year":
        return b.year - a.year
    if part == "quarter":
        return (b.year - a.year) * 4 + (b.month - 1) // 3 - (a.month - 1) // 3
    if part == "week":
        return ((_trunc("week", b) - _trunc("week", a)).days) // 7
    if part == "hour":
        return int((b - a).total_seconds() // 3600)
    raise TableauEvalError(f"DATEDIFF unit {part}")
