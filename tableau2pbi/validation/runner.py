"""Validation orchestrator: proves the converted Power BI report shows the same numbers.

Validation layers (in line with Tableau -> Power BI migration practice):

L1  Metadata parity        every Tableau object has a Power BI counterpart
L2  Data reconciliation    row counts, numeric totals, null and distinct counts per
                           table: source -> Tableau data source -> Power BI model
L3  Visual reconciliation  every worksheet is executed with Tableau semantics and
                           its converted visual is executed with DAX semantics;
                           cell-by-cell comparison with tolerance
L4  Scenario tests         parameter values and slicer filters are changed and the
                           comparison is repeated (filter-propagation checks)
"""
from __future__ import annotations

import copy
import io
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from ..converter import ConversionResult, PbiFilter, PbiVisual
from ..mcode import SQL_PARAM_RE, split_table
from ..model import Datasource, FieldRef, Filter, Workbook
from .compare import compare_frames
from .dax_engine import ColF, DaxEngine, _norm
from .tableau_engine import TableauEngine


# ============================================================ binding ==
class DataBinding:
    """Where to read the source data for validation.

    * ``sqlite``   path to a SQLite database holding the physical tables
    * ``csv_dir``  folder with ``<table>.csv`` files
    * packaged CSV / Excel files inside a .twbx are used automatically
    * ``frames``   explicit {table name: DataFrame}
    """

    def __init__(self, sqlite: Optional[str] = None, csv_dir: Optional[str] = None,
                 frames: Optional[dict] = None, packaged: Optional[dict] = None):
        self.sqlite = sqlite
        self.csv_dir = csv_dir
        self.frames = frames or {}
        self.packaged = packaged or {}

    def describe(self):
        parts = []
        if self.sqlite:
            parts.append(f"SQLite: {os.path.basename(self.sqlite)}")
        if self.csv_dir:
            parts.append(f"CSV folder: {self.csv_dir}")
        if self.packaged:
            parts.append(f"Packaged files: {', '.join(sorted(self.packaged))}")
        if self.frames:
            parts.append(f"Uploaded tables: {', '.join(sorted(self.frames))}")
        return "; ".join(parts) or "none"

    def _read_csv_bytes(self, data: bytes):
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)

    def physical(self, rel, conn, params: dict) -> pd.DataFrame:
        """Raw physical rows (remote column names) for a Tableau relation."""
        if rel is None:
            raise ValueError("No source relation")
        if rel.type == "text":
            if not self.sqlite:
                raise ValueError("Custom SQL validation needs a SQL database binding (sqlite)")
            sql = _substitute(rel.sql, params)
            with sqlite3.connect(self.sqlite) as con:
                return pd.read_sql_query(sql, con)
        _, table = split_table(rel.table)
        table = table or rel.name
        for key in (rel.name, table):
            if key in self.frames:
                return self.frames[key].copy()
        if conn is not None and conn.cls == "textscan":
            fn = (conn.filename or "").replace("\\", "/")
            for name, data in self.packaged.items():
                if name.replace("\\", "/").endswith(fn) or os.path.basename(name) == os.path.basename(fn):
                    return self._read_csv_bytes(data)
        if conn is not None and conn.cls == "excel-direct":
            fn = os.path.basename(conn.filename or "")
            for name, data in self.packaged.items():
                if os.path.basename(name) == fn:
                    return pd.read_excel(io.BytesIO(data), sheet_name=table.rstrip("$"), dtype=str)
        if self.csv_dir:
            for cand in (table, table.replace("#", "."), rel.name):
                for c in (cand, cand + ".csv"):
                    p = os.path.join(self.csv_dir, c)
                    if os.path.isfile(p):
                        return pd.read_csv(p, dtype=str, keep_default_na=False)
        if self.sqlite:
            with sqlite3.connect(self.sqlite) as con:
                return pd.read_sql_query(f'SELECT * FROM "{table}"', con)
        raise ValueError(f"No data bound for table '{table}'")


def _substitute(sql: str, params: dict) -> str:
    def repl(m):
        name = m.group(1) or m.group(2)
        v = params.get(name)
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return str(v)
    return SQL_PARAM_RE.sub(repl, sql)


def _coerce(series: pd.Series, datatype: str) -> pd.Series:
    s = series.replace({"": None})
    if datatype in ("date", "datetime", "dateTime"):
        return pd.to_datetime(s, errors="coerce")
    if datatype in ("real", "double"):
        return pd.to_numeric(s, errors="coerce").astype(float)
    if datatype in ("integer", "int64"):
        v = pd.to_numeric(s, errors="coerce")
        return v.astype(object).where(v.notna(), None).map(lambda x: int(x) if x is not None else None)
    if datatype == "boolean":
        return s.map(lambda x: None if x is None else str(x).lower() in ("true", "1"))
    return s.astype(object).where(s.notna(), None).map(lambda x: None if x is None else str(x))


# ===================================================== tableau records ==
def tableau_records(wb: Workbook, ds: Datasource, binding: DataBinding) -> pd.DataFrame:
    params = {p.caption: p.current_value for p in wb.parameters.values()}
    params.update({p.internal_name: p.current_value for p in wb.parameters.values()})
    conn_by = {c.name: c for c in ds.connections}
    frames = {}
    for lt in ds.tables:
        raw = binding.physical(lt.relation, conn_by.get(lt.relation.connection) or
                               (ds.connections[0] if ds.connections else None), params)
        cols = {}
        for cname, col in ds.columns.items():
            if col.table != lt.name:
                continue
            src = col.remote_name or cname
            if src in raw.columns:
                cols[cname] = _coerce(raw[src], col.datatype)
            else:
                cols[cname] = pd.Series([None] * len(raw), dtype=object)
        frames[lt.name] = pd.DataFrame(cols)
    if len(ds.tables) == 1:
        return frames[ds.tables[0].name].reset_index(drop=True)
    if ds.model_style == "joins":
        joined = frames[ds.tables[0].name]
        done = {ds.tables[0].name}
        for r in ds.relationships:
            lc = _local(ds, r.left_table, r.left_column)
            rc = _local(ds, r.right_table, r.right_column)
            other = r.right_table if r.left_table in done else r.left_table
            if r.left_table not in done:
                lc, rc = rc, lc
            how = {"inner": "inner", "left": "left", "right": "right", "full": "outer"}.get(r.join_type, "inner")
            joined = joined.merge(frames[other], how=how, left_on=lc, right_on=rc)
            done.add(other)
        return joined.reset_index(drop=True)
    # relationships: records are the root (first) object, related tables attached
    root = ds.tables[0].name
    joined = frames[root]
    done = {root}
    pending = list(ds.relationships)
    while pending:
        progressed = False
        for r in list(pending):
            if r.left_table in done and r.right_table not in done:
                a, b, t = r.left_column, r.right_column, r.right_table
            elif r.right_table in done and r.left_table not in done:
                a, b, t = r.right_column, r.left_column, r.left_table
            else:
                if r.left_table in done and r.right_table in done:
                    pending.remove(r)
                continue
            joined = joined.merge(frames[t], how="left", left_on=a, right_on=b)
            done.add(t)
            pending.remove(r)
            progressed = True
        if not progressed:
            break
    return joined.reset_index(drop=True)


def _local(ds, table, column):
    for cname, c in ds.columns.items():
        if c.table == table and (cname == column or c.remote_name == column or cname == f"{column} ({table})"):
            return cname
    return column


# ======================================================== PBI model data ==
def model_tables(res: ConversionResult, binding: DataBinding) -> dict:
    wb = res.workbook
    params = {p.caption: p.current_value for p in wb.parameters.values()}
    params.update({p.internal_name: p.current_value for p in wb.parameters.values()})
    out = {}
    date_values = []
    for t in res.model.tables:
        if t.kind in ("fact", "dim", "single"):
            if t.join_plan:
                parts = []
                for leaf in t.join_plan["leaves"]:
                    raw = binding.physical(leaf["relation"], leaf["conn"], params)
                    parts.append(_shape(raw, leaf["columns"]))
                df = parts[0]
                names = [l["name"] for l in t.join_plan["leaves"]]
                for j in t.join_plan["joins"]:
                    right = parts[names.index(j["right_leaf"])]
                    how = {"inner": "inner", "left": "left", "right": "right", "full": "outer"}.get(j["type"], "inner")
                    df = df.merge(right, how=how, left_on=j["left_column"], right_on=j["right_column"])
            else:
                raw = binding.physical(t.source_relation, t.source_conn, params)
                df = _shape(raw, t.source_spec or [])
            for c in t.columns:
                if c.expression is None and c.datatype == "dateTime" and c.name in df.columns:
                    date_values += [v for v in df[c.name].dropna().tolist()]
            out[t.name] = df.reset_index(drop=True)
        elif t.kind == "parameter":
            out[t.name] = pd.DataFrame({t.columns[0].name: t.param_values})
        elif t.kind == "measures":
            out[t.name] = pd.DataFrame({"Measures": [None]})
    date_t = res.model.table("Date")
    if date_t is not None and date_values:
        lo, hi = min(date_values), max(date_values)
        days = pd.date_range(datetime(lo.year, 1, 1), datetime(hi.year, 12, 31), freq="D")
        out["Date"] = pd.DataFrame({"Date": days, "Year": days.year, "Quarter": ["Q" + str(q) for q in days.quarter],
                                    "Month Number": days.month, "Month": days.strftime("%b"),
                                    "Month Start": days.to_period("M").to_timestamp()})
    return out


def _shape(raw: pd.DataFrame, spec) -> pd.DataFrame:
    cols = {}
    for src, target, dt in spec:
        if src in raw.columns:
            cols[target] = _coerce(raw[src], dt)
        else:
            cols[target] = pd.Series([None] * len(raw), dtype=object)
    return pd.DataFrame(cols)


def build_dax_engine(res: ConversionResult, binding: DataBinding, today: date) -> DaxEngine:
    tables = model_tables(res, binding)
    rels = [(r.from_table, r.from_column, r.to_table, r.to_column) for r in res.model.relationships if r.active]
    measures = {m.name: m.expression for m in res.model.all_measures()}
    calc = {}
    for t in res.model.tables:
        cc = [(c.name, c.expression) for c in t.columns if c.expression]
        if cc:
            calc[t.name] = cc
    return DaxEngine(tables, rels, measures, calc, today=today)


# ============================================================ results ==
@dataclass
class Check:
    layer: str
    name: str
    status: str            # PASS | WARN | FAIL | ERROR | SKIP
    detail: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class ValidationReport:
    workbook: str
    binding: str
    started: str
    duration_s: float = 0
    tolerance: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    engine_errors: list = field(default_factory=list)

    @property
    def summary(self):
        out = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        out["total"] = len(self.checks)
        executed = out["total"] - out["SKIP"]
        out["pass_rate"] = round(100.0 * out["PASS"] / executed, 1) if executed else 0.0
        out["overall"] = "FAIL" if out["FAIL"] or out["ERROR"] else ("WARN" if out["WARN"] else "PASS")
        return out

    def to_dict(self):
        return {"workbook": self.workbook, "binding": self.binding, "started": self.started,
                "duration_s": self.duration_s, "tolerance": self.tolerance, "summary": self.summary,
                "checks": [asdict(c) for c in self.checks], "engine_errors": self.engine_errors}


# ========================================================== validator ==
class Validator:
    def __init__(self, res: ConversionResult, binding: DataBinding, today: Optional[date] = None,
                 abs_tol: float = 0.01, rel_tol: float = 1e-6, scenarios: bool = True):
        self.res = res
        self.wb = res.workbook
        self.binding = binding
        self.today = today or date.today()
        self.abs_tol, self.rel_tol = abs_tol, rel_tol
        self.scenarios = scenarios
        self.report = ValidationReport(self.wb.name, binding.describe(),
                                       datetime.now().isoformat(timespec="seconds"),
                                       tolerance={"absolute": abs_tol, "relative": rel_tol})
        self.tab_engines = {}
        self.tab_records = {}

    def add(self, *a, **kw):
        self.report.checks.append(Check(*a, **kw))

    # ---------------------------------------------------------------- run --
    def run(self) -> ValidationReport:
        t0 = time.time()
        self.metadata_parity()
        try:
            self.dax = build_dax_engine(self.res, self.binding, self.today)
        except Exception as e:
            self.add("L2 Data", "Load Power BI model data", "ERROR", f"{type(e).__name__}: {e}")
            self.report.duration_s = round(time.time() - t0, 2)
            return self.report
        for ds in self.wb.datasources:
            try:
                recs = tableau_records(self.wb, ds, self.binding)
                self.tab_records[ds.name] = recs
                self.tab_engines[ds.name] = TableauEngine(self.wb, ds, recs, self.today)
            except Exception as e:
                self.add("L2 Data", f"Load Tableau data source '{ds.caption}'", "ERROR", f"{type(e).__name__}: {e}")
        self.data_reconciliation()
        for ws in self.wb.worksheets:
            self.visual_check(ws)
        if self.scenarios:
            self.parameter_scenarios()
            self.slice_scenarios()
        self.report.engine_errors = list(dict.fromkeys(self.dax.errors))[:50]
        self.report.duration_s = round(time.time() - t0, 2)
        return self.report

    # ------------------------------------------------------------ layer 1 --
    def metadata_parity(self):
        res, wb = self.res, self.wb
        calcs = sum(len(d.calculations) for d in wb.datasources)
        mapped = [m for m in res.calc_mappings if m.kind not in ("base-measure", "date-part", "parameter")]
        ok = [m for m in mapped if m.status in ("Converted", "Converted with notes")]
        self.add("L1 Metadata", "Calculated fields converted", "PASS" if len(ok) == calcs else
                 ("WARN" if len(ok) >= calcs * 0.9 else "FAIL"),
                 f"{len(ok)}/{calcs} Tableau calculations (incl. groups, bins, sets) have a DAX equivalent",
                 {"expected": calcs, "actual": len(ok),
                  "manual": [m.tableau_name for m in mapped if m.status not in ("Converted", "Converted with notes")]})
        ptables = [t for t in res.model.tables if t.kind == "parameter"]
        self.add("L1 Metadata", "Parameters converted", "PASS" if len(ptables) == len(wb.parameters) else "FAIL",
                 f"{len(ptables)}/{len(wb.parameters)} parameters -> parameter tables + value measures")
        missing = [w.name for w in wb.worksheets if w.name not in res.worksheet_visuals]
        self.add("L1 Metadata", "Worksheets converted to visuals", "PASS" if not missing else "FAIL",
                 f"{len(wb.worksheets) - len(missing)}/{len(wb.worksheets)} worksheets have a visual",
                 {"missing": missing})
        pages = {p.display_name for p in res.pages}
        dmiss = [d.name for d in wb.dashboards if d.name not in pages]
        self.add("L1 Metadata", "Dashboards converted to pages", "PASS" if not dmiss else "FAIL",
                 f"{len(wb.dashboards) - len(dmiss)}/{len(wb.dashboards)} dashboards -> report pages",
                 {"missing": dmiss})
        nf = sum(len([f for f in w.filters if f.kind != "categorical-all"]) for w in wb.worksheets)
        cf = sum(len(v.filters) for v in res.worksheet_visuals.values())
        self.add("L1 Metadata", "Worksheet filters converted", "PASS" if cf >= nf else "WARN",
                 f"{cf}/{nf} worksheet filters -> visual-level filters")
        cols = sum(len(d.columns) for d in wb.datasources)
        pcols = sum(len([c for c in t.columns if c.expression is None]) for t in res.model.tables
                    if t.kind in ("fact", "dim", "single"))
        self.add("L1 Metadata", "Columns mapped", "PASS" if pcols >= cols else "WARN",
                 f"{pcols}/{cols} source columns in the semantic model")
        rels = sum(len(d.relationships) for d in wb.datasources if d.model_style == "relationships")
        prels = len([r for r in res.model.relationships if r.to_table != "Date"])
        self.add("L1 Metadata", "Relationships converted", "PASS" if prels >= rels else "WARN",
                 f"{prels}/{rels} Tableau relationships -> model relationships (joins flattened: "
                 f"{sum(1 for d in wb.datasources if d.model_style == 'joins')})")

    # ------------------------------------------------------------ layer 2 --
    def data_reconciliation(self):
        for ds in self.wb.datasources:
            recs = self.tab_records.get(ds.name)
            if recs is None:
                continue
            fact = None
            for t in self.res.model.tables:
                if t.datasource == ds.name and t.kind in ("fact", "single"):
                    fact = t
            if fact is None:
                continue
            pbi = self.dax.base.get(fact.name)
            n_t, n_p = len(recs), len(pbi) if pbi is not None else 0
            self.add("L2 Data", f"Row count - {ds.caption}", "PASS" if n_t == n_p else "FAIL",
                     f"Tableau data source rows {n_t:,} vs Power BI '{fact.name}' rows {n_p:,}",
                     {"tableau": n_t, "powerbi": n_p})
            prof = []
            status = "PASS"
            for cname, col in ds.columns.items():
                tgt = self.res.field_map.get(("__col__", cname))
                key = None
                for (dsn, tc), (pt, pc) in _col_lookup(self.res, ds).items():
                    if tc == cname:
                        key = f"{pt}[{pc}]"
                        ptable = pt
                if key is None or cname not in recs.columns:
                    continue
                pdf = self.dax.base.get(ptable)
                if pdf is None or key not in pdf.columns:
                    continue
                tv = recs[cname]
                pv = pdf[key]
                if ptable != fact.name:
                    continue
                row = {"column": cname, "tableau_nulls": int(tv.isna().sum()), "powerbi_nulls": int(pv.isna().sum()),
                       "tableau_distinct": int(tv.nunique(dropna=True)), "powerbi_distinct": int(pv.nunique(dropna=True))}
                if col.datatype in ("integer", "real"):
                    row["tableau_sum"] = round(float(pd.to_numeric(tv, errors="coerce").sum()), 4)
                    row["powerbi_sum"] = round(float(pd.to_numeric(pv, errors="coerce").sum()), 4)
                ok = (row["tableau_nulls"] == row["powerbi_nulls"] and row["tableau_distinct"] == row["powerbi_distinct"]
                      and abs(row.get("tableau_sum", 0) - row.get("powerbi_sum", 0)) <= self.abs_tol)
                row["status"] = "PASS" if ok else "FAIL"
                if not ok:
                    status = "FAIL"
                prof.append(row)
            self.add("L2 Data", f"Column profile - {ds.caption}", status,
                     f"{len(prof)} columns profiled (sum / null count / distinct count)", {"columns": prof})
            if self.binding.sqlite and ds.model_style != "joins" and not any(
                    c.cls in ("textscan", "excel-direct") for c in ds.connections):
                try:
                    self._sql_truth(ds, fact)
                except sqlite3.Error as e:
                    self.add("L2 Data", f"SQL ground truth - {ds.caption}", "SKIP", f"Source not queryable: {e}")

    def _sql_truth(self, ds, fact):
        """Third, independent source of truth: aggregate the physical table with SQL."""
        lt = next((t for t in ds.tables if t.relation.type == "table" and
                   self.res.model.table(fact.name) and fact.source_relation is t.relation), None)
        if lt is None:
            return
        _, table = split_table(lt.relation.table)
        rows = []
        status = "PASS"
        with sqlite3.connect(self.binding.sqlite) as con:
            for cname, col in ds.columns.items():
                if col.table != lt.name or col.datatype not in ("integer", "real") or not col.remote_name:
                    continue
                sql_sum = con.execute(f'SELECT SUM("{col.remote_name}") FROM "{table}"').fetchone()[0] or 0
                key = None
                for (_, tc), (pt, pc) in _col_lookup(self.res, ds).items():
                    if tc == cname and pt == fact.name:
                        key = f"{pt}[{pc}]"
                if key is None:
                    continue
                pbi_sum = float(pd.to_numeric(self.dax.base[fact.name][key], errors="coerce").sum())
                tab_sum = float(pd.to_numeric(self.tab_records[ds.name][cname], errors="coerce").sum())
                ok = abs(sql_sum - pbi_sum) <= self.abs_tol and abs(sql_sum - tab_sum) <= self.abs_tol
                status = status if ok else "FAIL"
                rows.append({"column": cname, "sql": round(sql_sum, 4), "tableau": round(tab_sum, 4),
                             "powerbi": round(pbi_sum, 4), "status": "PASS" if ok else "FAIL"})
        if rows:
            self.add("L2 Data", f"SQL ground truth - {table}", status,
                     f"SUM() of {len(rows)} numeric columns: source SQL = Tableau = Power BI", {"columns": rows})

    # ------------------------------------------------------------ layer 3 --
    def _pbi_query(self, ws, tab_cols, dims_keys, meas_keys, visual: PbiVisual, extra_filters=(), param_filters=()):
        dims, measures, missing = [], [], []
        roles_cols = set(visual.group_columns)
        roles_meas = set(visual.measures)
        for k in dims_keys:
            tgt = self.res.field_map.get((ws.name, k))
            if tgt is None:
                missing.append(k)
                continue
            dims.append(tuple(tgt))
            if tuple(tgt) not in roles_cols:
                missing.append(f"{k} (not on visual)")
        for k in meas_keys:
            m = self.res.field_map.get((ws.name, k))
            if m is None:
                missing.append(k)
                continue
            measures.append(m)
            if m not in roles_meas:
                missing.append(f"{k} (not on visual)")
        shadow, value_filters = self._pbi_filters(visual.filters)
        shadow = shadow + list(extra_filters) + list(param_filters)
        df = self.dax.query(dims, measures, shadow, value_filters)
        df.columns = list(dims_keys[:len(dims)]) + list(meas_keys[:len(measures)]) if len(df.columns) == len(dims) + len(measures) else df.columns
        return df, missing

    def _pbi_filters(self, filters):
        shadow, value_filters, topn = [], [], []
        for f in filters:
            if f.kind in ("categorical", "range", "relative-date"):
                key = f"{f.table}[{f.column}]"
                dom = self.dax.domain(key)
                if f.kind == "categorical":
                    vals = {_mn(v) for v in f.values}
                    sel = [v for v in dom if (_mn(v) in vals) != f.exclude]
                elif f.kind == "range":
                    sel = [v for v in dom if _in_range(v, f.min, f.max, f.include_null)]
                else:
                    start, end = _relative_window(self.today, f.period_type, f.first_period, f.last_period)
                    sel = [v for v in dom if v is not None and start <= _dt(v) < end]
                shadow.append(ColF(key, frozenset(_norm(v) for v in sel)))
            elif f.kind == "measure-range":
                value_filters.append((f.measure, _num(f.min), _num(f.max), f.include_null))
            elif f.kind == "topn":
                topn.append(f)
        for f in topn:   # Power BI evaluates Top N within the other filters of the visual
            key = f"{f.table}[{f.column}]"
            self.dax.shadow = tuple(shadow)
            scores = []
            for v in self.dax.domain(key):
                cf = tuple(shadow) + (ColF(key, frozenset([_norm(v)])),)
                from .dax_engine import Ctx
                s = self.dax._measure(f.by_measure, Ctx(cf))
                if s is not None:
                    scores.append((s, v))
            scores.sort(key=lambda x: -x[0] if f.top_direction == "top" else x[0])
            keep = [v for _, v in scores[:f.top_n]]
            shadow.append(ColF(key, frozenset(_norm(v) for v in keep)))
            self.dax._eval_cache.clear()
        return shadow, value_filters

    def visual_check(self, ws, label=None, tab_ws=None, extra_pbi=(), param_override=None, layer="L3 Visual",
                     param_filters=()):
        eng = None
        for dsn in ws.datasources:
            if dsn in self.tab_engines:
                eng = self.tab_engines[dsn]
        name = label or f"Worksheet '{ws.name}'"
        if eng is None:
            self.add(layer, name, "SKIP", "Data source not loaded")
            return None
        visual = self.res.worksheet_visuals.get(ws.name)
        try:
            saved = dict(eng.params)
            if param_override:
                eng.params.update(param_override)
                eng._vec.clear()
            tab = eng.run_worksheet(tab_ws or ws)
        except Exception as e:
            self.add(layer, name, "ERROR", f"Tableau engine: {type(e).__name__}: {e}")
            return None
        finally:
            if param_override:
                eng.params = saved
                eng._vec.clear()
        dims_keys = [c for c in tab.columns if not eng.is_measure(_ref(c))]
        meas_keys = [c for c in tab.columns if eng.is_measure(_ref(c))]
        if not meas_keys:
            self.add(layer, name, "SKIP", "Worksheet has no measures to compare")
            return None
        try:
            pbi, missing = self._pbi_query(ws, list(tab.columns), dims_keys, meas_keys, visual, extra_pbi,
                                           param_filters)
        except Exception as e:
            self.add(layer, name, "ERROR", f"Power BI engine: {type(e).__name__}: {e}")
            return None
        cmp = compare_frames(tab, pbi, dims_keys, meas_keys, self.abs_tol, self.rel_tol)
        status = cmp["status"]
        detail = (f"{visual.visual_type if visual else '?'} | {cmp['summary']['tableau_rows']} vs "
                  f"{cmp['summary']['powerbi_rows']} rows | {cmp['summary']['matched_cells']}/"
                  f"{cmp['summary']['total_cells']} cells match")
        if missing:
            status = "FAIL" if status == "PASS" else status
            detail += f" | unmapped: {', '.join(missing)}"
        data = {"visual_type": visual.visual_type if visual else None, "dimensions": dims_keys,
                "measures": meas_keys,
                "measure_map": {k: self.res.field_map.get((ws.name, k)) for k in meas_keys},
                "dimension_map": {k: self.res.field_map.get((ws.name, k)) for k in dims_keys},
                "dax_query": dax_query_text(self.res, ws, dims_keys, meas_keys, visual),
                **cmp}
        self.add(layer, name, status, detail, data)
        return status

    # ------------------------------------------------------------ layer 4 --
    def parameter_scenarios(self):
        for p in self.wb.parameters.values():
            alt = None
            if p.members:
                alt = next((v for v, _ in p.members if v != p.current_value), None)
            elif p.domain_type == "range" and p.min is not None and p.max is not None:
                step = p.step or 1
                cand = p.current_value + step if p.current_value is not None else p.min
                alt = cand if cand <= p.max else p.min
            if alt is None:
                continue
            if any(re.search(r"<\[?Parameters\]?\.\[?" + re.escape(p.caption), lt.relation.sql or "")
                   for ds in self.wb.datasources for lt in ds.tables):
                self.add("L4 Scenario", f"Parameter '{p.caption}' = {alt}", "SKIP",
                         "Parameter is used in custom SQL: Power BI applies it at refresh (Power Query "
                         "parameter); re-run validation after changing it in both tools.")
                continue
            users = self._sheets_using_param(p)
            if not users:
                continue
            ptab = self.res.model.table(p.caption)
            pf = [ColF(f"{ptab.name}[{ptab.columns[0].name}]", frozenset([_norm(alt)]))] if ptab else []
            static_cols = [m.tableau_name for m in self.res.calc_mappings
                           if m.kind == "column" and any("uses parameter" in w and p.caption in w for w in m.warnings)]
            for ws in users:
                st = self.visual_check(ws, label=f"'{ws.name}' with {p.caption} = {alt}",
                                       param_override={p.internal_name: alt, p.caption: alt},
                                       layer="L4 Scenario", param_filters=pf)
                if st == "FAIL" and static_cols:
                    c = self.report.checks[-1]
                    c.status = "WARN"
                    c.detail += (f" | KNOWN LIMITATION: {', '.join(static_cols)} is a calculated column driven by "
                                 f"'{p.caption}'. Power BI calculated columns do not react to slicers, so numbers only "
                                 f"match at the default value. Fix: rebuild as a measure or a field parameter.")

    def _sheets_using_param(self, p):
        out = []
        for ws in self.wb.worksheets:
            ds = self.wb.datasource(ws.datasources[0]) if ws.datasources else None
            if ds is None:
                continue
            names = set()
            for r in ws.all_fields() + [f.field for f in ws.filters]:
                names.add(r.field)
            frontier = list(names)
            seen = set()
            hit = False
            while frontier:
                n = frontier.pop()
                if n in seen:
                    continue
                seen.add(n)
                cf = ds.calculations.get(n)
                if cf is None:
                    continue
                txt = cf.formula + " " + json.dumps(cf.extra, default=str)
                if f"[Parameters].[{p.internal_name}]" in txt or f"[Parameters].[{p.caption}]" in txt:
                    hit = True
                    break
                frontier += re.findall(r"\[([^\[\]]+)\]", cf.formula)
            if hit:
                out.append(ws)
        return out

    def slice_scenarios(self, max_sheets: int = 6):
        """Apply a slicer on a dimension that is not in the view (filter propagation)."""
        done = 0
        for ws in self.wb.worksheets:
            if done >= max_sheets:
                break
            ds = self.wb.datasource(ws.datasources[0]) if ws.datasources else None
            eng = self.tab_engines.get(ds.name) if ds else None
            if eng is None:
                continue
            in_view = {r.field for r in ws.all_fields()} | {f.field.field for f in ws.filters}
            cand = None
            for cname, col in ds.columns.items():
                if col.datatype != "string" or cname in in_view or col.hidden or col.table is None:
                    continue
                vals = [v for v in eng.cols.get(cname, []) if v is not None]
                distinct = sorted(set(vals))
                if 2 <= len(distinct) <= 8:
                    cand = (cname, distinct[0])
                    break
            if cand is None:
                continue
            cname, val = cand
            tgt = None
            for (_, tc), (pt, pc) in _col_lookup(self.res, ds).items():
                if tc == cname:
                    tgt = (pt, pc)
            if tgt is None:
                continue
            tws = copy.deepcopy(ws)
            tws.filters.append(Filter("categorical", ds.name, FieldRef(ds.name, cname, "none", "nk"),
                                      members=[val]))
            st = self.visual_check(ws, label=f"'{ws.name}' sliced by {cname} = {val}", tab_ws=tws,
                                   extra_pbi=[ColF(f"{tgt[0]}[{tgt[1]}]", frozenset([val]))], layer="L4 Scenario")
            if st:
                done += 1


# ============================================================ helpers ==
def _col_lookup(res: ConversionResult, ds):
    out = {}
    for t in res.model.tables:
        if t.datasource != ds.name:
            continue
        for c in t.columns:
            if c.expression is None and c.tableau_ref:
                out[(ds.name, c.tableau_ref)] = (t.name, c.name)
    return out


def _ref(key: str) -> FieldRef:
    parts = key.split(":")
    return FieldRef("", ":".join(parts[1:-1]), parts[0], parts[-1])


def _mn(v):
    v = _norm(v)
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower()
    return v


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return pd.Timestamp(str(v)).to_pydatetime()


def _in_range(v, lo, hi, include_null):
    if v is None:
        return include_null
    if isinstance(v, (datetime, date, pd.Timestamp)):
        v = _dt(v)
        lo = _dt(lo) if lo is not None else None
        hi = _dt(hi) if hi is not None else None
    else:
        lo, hi = _num(lo) if lo is not None else None, _num(hi) if hi is not None else None
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def _relative_window(today, period, first, last):
    period = period or "day"
    t = datetime(today.year, today.month, today.day)
    def trunc(d):
        if period == "year":
            return datetime(d.year, 1, 1)
        if period == "quarter":
            return datetime(d.year, (d.month - 1) // 3 * 3 + 1, 1)
        if period == "month":
            return datetime(d.year, d.month, 1)
        if period == "week":
            return d - timedelta(days=d.isoweekday() % 7)
        return d

    def add(d, k):
        if period in ("year", "quarter", "month"):
            months = k * {"year": 12, "quarter": 3, "month": 1}[period]
            y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
            return d.replace(year=y, month=m)
        return d + timedelta(days=k * (7 if period == "week" else 1))
    base = trunc(t)
    return add(base, first or 0), add(base, (last or 0) + 1)


def dax_query_text(res: ConversionResult, ws, dims_keys, meas_keys, visual: Optional[PbiVisual]) -> str:
    """DAX query to run against the published dataset (DAX Studio / executeQueries)."""
    cols = []
    for k in dims_keys:
        t = res.field_map.get((ws.name, k))
        if t:
            cols.append(f"'{t[0]}'[{t[1]}]")
    filters = []
    for f in (visual.filters if visual else []):
        if f.kind == "categorical":
            vals = ", ".join(_dax_lit(v) for v in f.values)
            expr = f"TREATAS({{{vals}}}, '{f.table}'[{f.column}])"
            if f.exclude:
                expr = f"FILTER(ALL('{f.table}'[{f.column}]), NOT('{f.table}'[{f.column}] IN {{{vals}}}))"
            filters.append(f"KEEPFILTERS({expr})")
        elif f.kind == "range":
            conds = []
            if f.min is not None:
                conds.append(f"'{f.table}'[{f.column}] >= {_dax_lit(f.min)}")
            if f.max is not None:
                conds.append(f"'{f.table}'[{f.column}] <= {_dax_lit(f.max)}")
            filters.append(f"KEEPFILTERS(FILTER(ALL('{f.table}'[{f.column}]), {' && '.join(conds)}))")
        elif f.kind == "topn":
            filters.append(f"KEEPFILTERS(TOPN({f.top_n}, ALL('{f.table}'[{f.column}]), [{f.by_measure}]))")
    meas = []
    for k in meas_keys:
        m = res.field_map.get((ws.name, k))
        if m:
            meas.append(f'"{m}", [{m}]')
    args = ",\n    ".join(cols + filters + meas)
    q = f"EVALUATE\nSUMMARIZECOLUMNS(\n    {args}\n)"
    vf = [f for f in (visual.filters if visual else []) if f.kind == "measure-range"]
    if vf:
        conds = []
        for f in vf:
            if f.min is not None:
                conds.append(f"[{f.measure}] >= {f.min}")
            if f.max is not None:
                conds.append(f"[{f.measure}] <= {f.max}")
        q = f"EVALUATE\nFILTER(\n  SUMMARIZECOLUMNS(\n    {args}\n  ),\n  {' && '.join(conds)}\n)"
    if cols:
        q += "\nORDER BY " + ", ".join(cols)
    return q


def _dax_lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        y, m, d = v.split("-")
        return f"DATE({int(y)}, {int(m)}, {int(d)})"
    return '"' + str(v).replace('"', '""') + '"'


def validate(res: ConversionResult, binding: DataBinding, today: Optional[date] = None, **kw) -> ValidationReport:
    return Validator(res, binding, today, **kw).run()
