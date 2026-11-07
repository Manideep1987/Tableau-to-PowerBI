"""Differential fuzzing: random Tableau calculations and worksheets, both engines, compare.

For each batch a fresh copy of a sample workbook gets N random calculated fields (row-level,
aggregate, LOD, table calculations) and N random worksheets (random dimensions, measures and
filters). The batch is converted, then every random worksheet is executed by the Tableau
engine and its converted visual by the DAX engine; any cell difference is reported with the
formula, worksheet and the first mismatching rows.

    python tests/fuzz/differential_fuzz.py --cases 400 --seed 1
"""
from __future__ import annotations

import argparse
import copy
import os
import random
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tableau2pbi.converter import convert  # noqa: E402
from tableau2pbi.model import CalculatedField, FieldRef, Filter, Worksheet  # noqa: E402
from tableau2pbi.parser import read_workbook  # noqa: E402
from tableau2pbi.validation.runner import DataBinding, Validator, build_dax_engine, tableau_records  # noqa: E402
from tableau2pbi.validation.tableau_engine import TableauEngine  # noqa: E402

DB = os.path.join(ROOT, "samples", "data", "superstore.db")
AS_OF = date(2025, 12, 31)

SCHEMAS = {
    "Superstore_Sales.twb": {
        "num": ["Sales", "Profit", "Quantity", "Discount", "Unit Price"],
        "str": ["Region", "Segment", "Category", "Sub-Category", "Ship Mode", "State"],
        "date": ["Order Date", "Ship Date"],
        "lits": {"Region": ["East", "West", "Central", "South"], "Segment": ["Consumer", "Corporate", "Home Office"],
                 "Category": ["Furniture", "Technology", "Office Supplies"], "Ship Mode": ["Same Day", "First Class",
                                                                                          "Second Class", "Standard Class"],
                 "Sub-Category": ["Chairs", "Phones", "Paper", "Binders"], "State": ["Texas", "California", "Florida"]},
        "ids": ["Order ID", "Customer Name", "Product Name"],
    },
    "Invoice_Health.twb": {
        "num": ["Invoice Amount"],
        "str": ["Region", "Vendor Category", "Status", "Fiscal Period", "Month"],
        "date": ["Invoice Date", "Due Date", "Payment Date"],
        "lits": {"Region": ["North", "South", "East", "West"], "Vendor Category": ["IT", "Facilities", "Logistics"],
                 "Status": ["Paid", "Open", "Disputed"], "Fiscal Period": ["FY25 P01", "FY25 P06"],
                 "Month": ["2024-03", "2025-01"]},
        "ids": ["Invoice ID", "Vendor Name", "Employee Name"],
    },
    "HR_Analytics.twbx": {
        "num": ["Salary", "Performance Score"],
        "str": ["Department", "Job Level", "Gender", "Location"],
        "date": ["Hire Date", "Termination Date"],
        "lits": {"Department": ["Engineering", "Sales", "HR"], "Job Level": ["L1", "L3", "L5"], "Gender": ["F", "M"],
                 "Location": ["Austin", "Boston"]},
        "ids": ["Employee ID", "Employee Name"],
    },
}


class Gen:
    def __init__(self, rnd: random.Random, schema: dict):
        self.r, self.s = rnd, schema

    def pick(self, xs):
        return self.r.choice(xs)

    def f(self, name):
        return f"[{name}]"

    # ------------------------------------------------------------ row-level
    def num(self, d=0):
        r = self.r.random()
        if d > 2 or r < 0.3:
            return self.f(self.pick(self.s["num"])) if self.r.random() < 0.75 else self.pick(["1", "2", "0.5", "10", "100", "-3"])
        ops = [
            lambda: f"({self.num(d + 1)} {self.pick(['+', '-', '*'])} {self.num(d + 1)})",
            lambda: f"({self.num(d + 1)} / {self.pick(['2', '4', self.num(d + 1)])})",
            lambda: f"ZN({self.num(d + 1)})",
            lambda: f"ABS({self.num(d + 1)})",
            lambda: f"ROUND({self.num(d + 1)}, {self.pick(['0', '1', '2'])})",
            lambda: f"IIF({self.bool(d + 1)}, {self.num(d + 1)}, {self.num(d + 1)})",
            lambda: f"IF {self.bool(d + 1)} THEN {self.num(d + 1)} ELSEIF {self.bool(d + 1)} THEN {self.num(d + 1)} ELSE {self.num(d + 1)} END",
            lambda: f"IF {self.bool(d + 1)} THEN {self.num(d + 1)} END",
            lambda: f"DATEDIFF('{self.pick(['day', 'month', 'year', 'quarter'])}', {self.date(d + 1)}, {self.date(d + 1)})",
            lambda: f"DATEPART('{self.pick(['year', 'month', 'quarter', 'day', 'weekday'])}', {self.date(d + 1)})",
            lambda: f"LEN({self.str(d + 1)})",
            lambda: f"INT({self.num(d + 1)})",
            lambda: f"IFNULL({self.num(d + 1)}, {self.pick(['0', '-1'])})",
            lambda: f"MAX({self.num(d + 1)}, {self.num(d + 1)})",
            lambda: self.case_num(d),
        ]
        return self.pick(ops)()

    def case_num(self, d):
        col = self.pick([c for c in self.s["str"] if c in self.s["lits"]])
        whens = " ".join(f"WHEN '{v}' THEN {self.num(d + 1)}" for v in self.r.sample(self.s["lits"][col], min(2, len(self.s["lits"][col]))))
        return f"CASE [{col}] {whens} ELSE {self.num(d + 1)} END"

    def str(self, d=0):
        r = self.r.random()
        if d > 2 or r < 0.4:
            return self.f(self.pick(self.s["str"]))
        ops = [
            lambda: f"UPPER({self.str(d + 1)})",
            lambda: f"LEFT({self.str(d + 1)}, {self.pick(['1', '3'])})",
            lambda: f"{self.str(d + 1)} + '-' + {self.str(d + 1)}",
            lambda: f"IIF({self.bool(d + 1)}, {self.str(d + 1)}, '{self.pick(['Other', 'n/a'])}')",
            lambda: f"IF {self.bool(d + 1)} THEN 'Yes' ELSE 'No' END",
            lambda: f"STR(DATEPART('year', {self.date(d + 1)}))",
            lambda: f"REPLACE({self.str(d + 1)}, 'e', 'E')",
            lambda: f"TRIM({self.str(d + 1)})",
        ]
        return self.pick(ops)()

    def bool(self, d=0):
        ops = [
            lambda: f"{self.num(d + 1)} {self.pick(['>', '<', '>=', '<=', '=', '<>'])} {self.pick(['0', '1', '50', '0.2', '500'])}",
            lambda: self.str_eq(),
            lambda: f"({self.bool(d + 2)} {self.pick(['AND', 'OR'])} {self.bool(d + 2)})" if d < 2 else self.str_eq(),
            lambda: f"NOT {self.str_eq()}",
            lambda: f"ISNULL({self.f(self.pick(self.s['num'] + self.s['date']))})",
            lambda: f"{self.date(d + 1)} >= #2024-07-01#",
            lambda: f"STARTSWITH({self.f(self.pick(self.s['str']))}, '{self.pick(['S', 'E', 'C', 'F'])}')",
        ]
        return self.pick(ops)()

    def str_eq(self):
        col = self.pick([c for c in self.s["str"] if c in self.s["lits"]])
        vals = self.s["lits"][col]
        if self.r.random() < 0.5:
            return f"[{col}] = '{self.pick(vals)}'"
        return f"[{col}] IN ({', '.join(repr(v) for v in self.r.sample(vals, min(2, len(vals))))})"

    def date(self, d=0):
        r = self.r.random()
        if d > 2 or r < 0.6:
            return self.f(self.pick(self.s["date"]))
        return self.pick([
            lambda: f"DATETRUNC('{self.pick(['month', 'quarter', 'year'])}', {self.date(d + 1)})",
            lambda: f"DATEADD('{self.pick(['day', 'month'])}', {self.pick(['3', '-1', '12'])}, {self.date(d + 1)})",
        ])()

    # ------------------------------------------------------------ aggregate
    def agg(self, d=0, top=False):
        base = [
            lambda: f"{self.pick(['SUM', 'AVG', 'MIN', 'MAX'])}({self.num(1)})",
            lambda: f"SUM({self.f(self.pick(self.s['num']))})",
            lambda: f"COUNTD({self.f(self.pick(self.s['ids'] + self.s['str']))})",
            lambda: f"COUNT({self.f(self.pick(self.s['ids']))})",
            lambda: f"MEDIAN({self.f(self.pick(self.s['num']))})",
        ]
        if d > 1 or self.r.random() < 0.45:
            return self.pick(base)()
        return self.pick([
            lambda: f"{self.agg(d + 1)} / {self.agg(d + 1)}",
            lambda: f"({self.agg(d + 1)} {self.pick(['+', '-', '*'])} {self.agg(d + 1)})",
            lambda: f"ZN({self.agg(d + 1)})",
            lambda: f"IIF({self.agg(d + 1)} > {self.pick(['0', '1000'])}, {self.agg(d + 1)}, {self.agg(d + 1)})",
        ] + ([lambda: f"IF {self.agg(d + 1)} > {self.pick(['0', '100'])} THEN 'High' ELSE 'Low' END"] if top else []))()

    def lod_row(self):
        dims = self.r.sample(self.s["str"], self.r.choice([0, 1, 1, 2]))
        inner = self.pick([f"SUM({self.f(self.pick(self.s['num']))})", f"AVG({self.f(self.pick(self.s['num']))})",
                           f"COUNTD({self.f(self.pick(self.s['ids']))})", f"MAX({self.f(self.pick(self.s['num']))})"])
        return "{FIXED " + ", ".join(self.f(x) for x in dims) + " : " + inner + "}"

    def lod_view(self):
        dim = self.pick(self.s["str"] + self.s["ids"][:1])
        kind = self.pick(["INCLUDE", "EXCLUDE"])
        return "{" + kind + " " + self.f(dim) + " : " + self.pick(
            [f"SUM({self.f(self.pick(self.s['num']))})", f"AVG({self.f(self.pick(self.s['num']))})"]) + "}"

    def table_calc(self):
        a = f"SUM({self.f(self.pick(self.s['num']))})"
        return self.pick([f"RUNNING_SUM({a})", f"RANK({a})", f"WINDOW_AVG({a})", f"TOTAL({a})", f"{a} / TOTAL({a})",
                          f"LOOKUP({a}, -1)", f"RANK_DENSE({a})", f"INDEX()", f"WINDOW_SUM({a})", f"RUNNING_AVG({a})"])


def make_batch(wb, ds, rnd, gen: Gen, n, schema):
    """Add n calculations and n worksheets; returns case descriptors."""
    cases = []
    dims_pool = [("col", c) for c in schema["str"]] + [("yr", c) for c in schema["date"]]
    for i in range(n):
        kind = rnd.choices(["row_num", "row_str", "row_bool", "agg", "lod_row", "lod_view", "table_calc"],
                           [3, 2, 1, 4, 2, 1, 2])[0]
        formula = {"row_num": lambda: gen.num(), "row_str": lambda: gen.str(), "row_bool": lambda: gen.bool(),
                   "agg": lambda: gen.agg(top=True), "lod_row": gen.lod_row, "lod_view": gen.lod_view,
                   "table_calc": gen.table_calc}[kind]()
        name = f"Fuzz_{i:03d}"
        dtype = {"row_num": "real", "row_str": "string", "row_bool": "boolean", "agg": "real", "lod_row": "real",
                 "lod_view": "real", "table_calc": "real"}[kind]
        if kind == "agg" and "'High'" in formula:
            dtype = "string"
        role = "dimension" if dtype in ("string", "boolean") else "measure"
        ds.calculations[name] = CalculatedField(name, f"Fuzz {i:03d}", formula, dtype, role,
                                                "nominal" if role == "dimension" else "quantitative")
        # worksheet
        ndims = rnd.choice([0, 1, 1, 2, 2]) if kind != "table_calc" else rnd.choice([1, 1, 2])
        dims = []
        for k, c in rnd.sample(dims_pool, ndims):
            dims.append(FieldRef(ds.name, c, "none" if k == "col" else "yr", "nk" if k == "col" else "ok"))
        meas = []
        if kind in ("row_str", "row_bool"):
            dims.insert(0, FieldRef(ds.name, name, "none", "nk"))
        elif kind in ("row_num", "lod_row"):
            meas.append(FieldRef(ds.name, name, rnd.choice(["sum", "avg", "min", "max"]), "qk"))
        elif kind == "lod_view":
            meas.append(FieldRef(ds.name, name, rnd.choice(["sum", "avg"]), "qk"))
        else:
            meas.append(FieldRef(ds.name, name, "usr", "qk" if dtype != "string" else "nk"))
        meas.append(FieldRef(ds.name, rnd.choice(schema["num"]), rnd.choice(["sum", "avg", "cnt"]), "qk"))
        if dtype == "string" and kind == "agg":
            meas = meas[1:]
            text_meas = [FieldRef(ds.name, name, "usr", "nk")]
        else:
            text_meas = []
        filters = []
        for _ in range(rnd.choice([0, 0, 1, 2])):
            fk = rnd.choice(["cat", "cat_ex", "measure", "top"])
            col = rnd.choice([c for c in schema["str"] if c in schema["lits"]])
            ref = FieldRef(ds.name, col, "none", "nk")
            if fk == "cat":
                filters.append(Filter("categorical", ds.name, ref, members=rnd.sample(schema["lits"][col], 1 + rnd.random().__round__())))
            elif fk == "cat_ex":
                filters.append(Filter("categorical", ds.name, ref, members=[rnd.choice(schema["lits"][col])], exclude=True))
            elif fk == "measure" and dims and kind != "table_calc":
                filters.append(Filter("quantitative", ds.name, FieldRef(ds.name, schema["num"][0], "sum", "qk"),
                                      min=rnd.choice([0, 1000, 10000])))
            elif fk == "top" and dims and not any(f.kind == "categorical" for f in filters) and dims[0].derivation == "none" \
                    and kind != "table_calc":
                filters.append(Filter("top", ds.name, dims[0], top_n=rnd.choice([2, 3]), top_by=f"SUM([{schema['num'][0]}])"))
        rows = dims[:1]
        cols = dims[1:] + meas
        enc = {"text": text_meas} if text_meas else {}
        ws = Worksheet(f"Fuzz sheet {i:03d}", [ds.name], rows=rows, cols=cols, mark="Text", encodings=enc, filters=filters)
        wb.worksheets.append(ws)
        cases.append({"calc": name, "kind": kind, "formula": formula, "worksheet": ws.name,
                      "dims": [d.key for d in dims], "filters": [f.kind + ("-ex" if f.exclude else "") for f in filters]})
    return cases


def run(cases_total=200, seed=1, batch=25, names=None, verbose=True):
    names = names or list(SCHEMAS)
    rnd = random.Random(seed)
    results = {"cases": 0, "pass": 0, "fail": [], "unsupported": [], "error": []}
    base = {n: read_workbook(os.path.join(ROOT, "samples", "workbooks", n)) for n in names}
    t0 = time.time()
    b = 0
    while results["cases"] < cases_total:
        name = names[b % len(names)]
        b += 1
        wb = copy.deepcopy(base[name])
        wb.worksheets, wb.dashboards, wb.actions = [], [], []
        ds = wb.datasources[0]
        gen = Gen(rnd, SCHEMAS[name])
        n = min(batch, cases_total - results["cases"])
        cases = make_batch(wb, ds, rnd, gen, n, SCHEMAS[name])
        res = convert(wb)
        status = {m.tableau_name: m for m in res.calc_mappings}
        binding = DataBinding(sqlite=DB, packaged=wb.packaged_files)
        v = Validator(res, binding, AS_OF, scenarios=False)
        v.dax = build_dax_engine(res, binding, AS_OF)
        recs = tableau_records(wb, ds, binding)
        v.tab_records[ds.name] = recs
        v.tab_engines[ds.name] = TableauEngine(wb, ds, recs, AS_OF)
        for c in cases:
            results["cases"] += 1
            m = status.get(f"Fuzz {c['calc'][5:]}")
            c["workbook"] = name
            c["dax"] = m.dax if m else None
            if m is None or m.status in ("Failed",) or m.classification == "UNSUPPORTED":
                c["reason"] = (m.warnings if m else ["no mapping"])
                results["unsupported"].append(c)
                continue
            ws = wb.worksheet(c["worksheet"])
            st = v.visual_check(ws)
            chk = v.report.checks[-1]
            c["detail"] = chk.detail
            if st == "PASS":
                results["pass"] += 1
            elif st in ("FAIL", "WARN"):
                c["rows"] = [r for r in chk.data.get("rows", []) if r["status"] != "MATCH"][:3]
                results["fail"].append(c)
            else:
                results["error"].append(c)
        if verbose:
            print(f"  batch {b} ({name}): {results['cases']} cases, {len(results['fail'])} mismatches, "
                  f"{len(results['error'])} errors, {len(results['unsupported'])} unsupported, {time.time() - t0:.0f}s",
                  flush=True)
        for e in v.dax.errors[:3]:
            if verbose:
                print("    engine:", e)
    results["seconds"] = round(time.time() - t0, 1)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--batch", type=int, default=25)
    a = ap.parse_args()
    r = run(a.cases, a.seed, a.batch)
    print(f"\n{r['pass']}/{r['cases']} PASS, {len(r['fail'])} mismatches, {len(r['error'])} errors, "
          f"{len(r['unsupported'])} unsupported in {r['seconds']}s")
    for c in (r["fail"] + r["error"])[:40]:
        print(f"\n[{c['workbook']}] {c['kind']}  {c['formula']}\n  dims={c['dims']} filters={c['filters']}\n  DAX: {c['dax']}\n  {c['detail']}")
        for row in c.get("rows", []):
            print("   ", row["status"], row["key"], [(v["measure"], v["tableau"], v["powerbi"]) for v in row["values"]
                                                   if v.get("status", "x") != "MATCH"][:2])
    for c in r["unsupported"][:10]:
        print(f"\nUNSUPPORTED [{c['workbook']}] {c['formula']}: {c['reason']}")
    return 0 if not r["fail"] and not r["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
