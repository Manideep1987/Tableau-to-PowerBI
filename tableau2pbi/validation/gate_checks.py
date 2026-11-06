"""Gate 2-4 checks added on top of the visual reconciliation.

* KPI regression register (gate 2): critical KPIs x canonical filter scenarios, Tableau
  value vs Power BI value, delta and tolerance (the build guide's regression register).
* Interactions (gate 3): report field references, quick filters -> slicers, actions ->
  interactions, sort order, filter direction, plus slicer scenarios run by the runner.
* Security (gate 4): RLS role lint and a simulated identity matrix (Tableau USERNAME()
  filter vs Power BI role with USERPRINCIPALNAME()) per user, including a forbidden user.

Checks that need a person (click paths, real-identity / SSO tests, business sign-off)
are recorded as PENDING - they are never simulated as passed.
"""
from __future__ import annotations

import json
import re

from ..formula.ast import TABLE_CALCS, Field as FField, Func, contains
from ..model import FieldRef, Filter, Worksheet
from .dax_engine import ColF, DaxError, _norm, parse_dax

KEYWORDS = ("region", "category", "segment", "department", "status", "country", "type", "class", "level")
WINDOW_DAX = re.compile(r"\b(WINDOW|OFFSET|ROWNUMBER|RANKX|ALLSELECTED)\s*\(")


class GateChecks:
    # ------------------------------------------------------------ helpers --
    def _engine_for(self, ws):
        for dsn in ws.datasources:
            if dsn in self.tab_engines:
                return self.tab_engines[dsn], self.wb.datasource(dsn)
        return None, None

    def _fact(self, ds):
        for t in self.res.model.tables:
            if t.datasource == ds.name and t.kind in ("fact", "single"):
                return t.name
        return None

    def kpi_candidates(self, limit=6):
        visuals = self.res.worksheet_visuals
        cards = [w for w in self.wb.worksheets if visuals.get(w.name) and visuals[w.name].visual_type in ("card", "multiRowCard")]
        order = cards + [w for w in self.wb.worksheets if w not in cards]
        out, seen = [], set()
        for ws in order:
            eng, ds = self._engine_for(ws)
            if eng is None:
                continue
            for r in ws.rows + ws.cols + [x for v in ws.encodings.values() for x in v]:
                if not eng.is_measure(r):
                    continue
                m = self.res.field_map.get((ws.name, r.key))
                if not isinstance(m, str) or m in seen:
                    continue
                pm = self.res.model.measure(m)
                if pm is None or WINDOW_DAX.search(pm.expression):
                    continue
                ast = eng._calc_ast(r.field)
                if ast is not None and contains(ast, lambda n: isinstance(n, Func) and n.name in TABLE_CALCS):
                    continue
                out.append((ws, r, m))
                seen.add(m)
            if len(out) >= limit and ws not in cards:
                break
        return out[:max(limit, len([1 for w, _, _ in out if w in cards]))]

    def canonical_dimensions(self, ds, eng, n=2):
        fact = self._fact(ds)
        scored = []
        lookup = self._col_targets(ds)
        for cname, col in ds.columns.items():
            if col.datatype != "string" or col.hidden or cname not in lookup:
                continue
            vals = sorted({v for v in eng.cols.get(cname, []) if v is not None})
            if not 2 <= len(vals) <= 8:
                continue
            name = (col.caption or cname).lower()
            score = (2 if any(k in name for k in KEYWORDS) else 0) + (1 if lookup[cname][0] != fact else 0)
            scored.append((score, cname, vals))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(c, v) for _, c, v in scored[:n]]

    def _col_targets(self, ds):
        out = {}
        for t in self.res.model.tables:
            if t.datasource != ds.name:
                continue
            for c in t.columns:
                if c.expression is None and c.tableau_ref:
                    out[c.tableau_ref] = (t.name, c.name)
        return out

    def _kpi_value(self, eng, ws, ref, filters, params=None):
        saved = dict(eng.params)
        try:
            if params:
                eng.params.update(params)
                eng._vec.clear()
            w2 = Worksheet(f"KPI {ref.key}", list(ws.datasources), cols=[ref], filters=list(filters))
            df = eng.run_worksheet(w2)
            return df[ref.key].iloc[0] if len(df) else None
        finally:
            if params:
                eng.params = saved
                eng._vec.clear()

    def _pbi_value(self, dax, measure, filters):
        df = dax.query([], [measure], list(filters))
        return df[measure].iloc[0] if len(df) else None

    def _cmp(self, a, b):
        from .compare import _cell_status, to_number
        a, b = to_number(a), to_number(b)
        st = _cell_status(a, b, self.abs_tol, self.rel_tol)
        delta = (b - a) if isinstance(a, float) and isinstance(b, float) else None
        tol = max(self.abs_tol, self.rel_tol * max(abs(a or 0) if isinstance(a, float) else 0,
                                                  abs(b or 0) if isinstance(b, float) else 0))
        return a, b, delta, tol, st

    # ------------------------------------------------------------- gate 2 --
    def kpi_checks(self):
        kpis = self.kpi_candidates()
        if not kpis:
            self.add("KPI register", "KPI regression register", "SKIP", "No aggregate KPIs found")
            return
        from .runner import pill_label
        ds0 = self._engine_for(kpis[0][0])[1]
        eng0 = self._engine_for(kpis[0][0])[0]
        scenarios = [("All data", [], [], {}, [])]
        targets = self._col_targets(ds0)
        for cname, vals in self.canonical_dimensions(ds0, eng0):
            cap = ds0.columns[cname].caption or cname
            t, c = targets[cname]
            for v in vals[:2]:
                scenarios.append((f"{cap} = {v}", [Filter("categorical", ds0.name, FieldRef(ds0.name, cname, "none", "nk"),
                                                          members=[v])], [ColF(f"{t}[{c}]", frozenset([v]))], {}, []))
        sql_params = {p.caption for d in self.wb.datasources for lt in d.tables for p in self.wb.parameters.values()
                      if lt.relation.sql and p.caption in lt.relation.sql}
        for p in self.wb.parameters.values():
            if p.caption in sql_params:
                continue
            alt = next((v for v, _ in p.members if v != p.current_value), None)
            if alt is None and p.domain_type == "range" and p.min is not None:
                alt = p.current_value + (p.step or 1) if p.current_value is not None and p.max is not None and \
                    p.current_value + (p.step or 1) <= p.max else p.min
            if alt is None:
                continue
            ptab = self.res.model.table(p.caption)
            static = [m.tableau_name for m in self.res.calc_mappings
                      if m.kind == "column" and any("uses parameter" in w and p.caption in w for w in m.warnings)]
            scenarios.append((f"Parameter {p.caption} = {alt}", [],
                              [ColF(f"{ptab.name}[{ptab.columns[0].name}]", frozenset([_norm(alt)]))] if ptab else [],
                              {p.internal_name: alt, p.caption: alt}, static))
        for name, tf, pf, params, static in scenarios:
            rows, worst = [], "PASS"
            for ws, ref, m in kpis:
                eng, _ = self._engine_for(ws)
                try:
                    tv = self._kpi_value(eng, ws, ref, tf, params)
                    pv = self._pbi_value(self.dax, m, pf)
                except Exception as e:      # recorded, not hidden
                    rows.append({"scenario": name, "metric": pill_label(self.wb, ws, ref.key), "measure": m,
                                 "tableau": None, "powerbi": None, "delta": None, "tolerance": None,
                                 "status": "ERROR", "note": f"{type(e).__name__}: {e}"})
                    worst = "ERROR"
                    continue
                a, b, delta, tol, st = self._cmp(tv, pv)
                status = "PASS" if st == "MATCH" else ("WARN" if st == "NULL_VS_ZERO" else "FAIL")
                note = ""
                if status == "FAIL" and static:
                    status, note = "WARN", f"Known limitation: {', '.join(static)} is a static calculated column"
                rows.append({"scenario": name, "metric": pill_label(self.wb, ws, ref.key), "measure": m, "tableau": a,
                             "powerbi": b, "delta": delta, "tolerance": tol, "status": status, "note": note})
                order = ["PASS", "WARN", "FAIL", "ERROR"]
                worst = max(worst, status, key=order.index)
            self.report.kpi_register.extend(rows)
            ok = sum(r["status"] == "PASS" for r in rows)
            self.add("KPI register", f"KPI scenario: {name}", worst, f"{ok}/{len(rows)} KPIs within tolerance",
                     {"register": rows})

    # ------------------------------------------------------------- gate 3 --
    def interaction_checks(self):
        from ..pbip.writer import build_files
        res = self.res
        objects = set()
        for t in res.model.tables:
            objects |= {(t.name, c.name) for c in t.columns} | {(t.name, m.name) for m in t.measures}
        missing, refs = [], 0
        for p, text in build_files(res).items():
            if not p.endswith("visual.json"):
                continue

            def walk(x):
                nonlocal refs
                if isinstance(x, dict):
                    for k in ("Column", "Measure"):
                        if k in x and "Entity" in x[k].get("Expression", {}).get("SourceRef", {}):
                            refs += 1
                            key = (x[k]["Expression"]["SourceRef"]["Entity"], x[k]["Property"])
                            if key not in objects:
                                missing.append(f"{key[0]}[{key[1]}] in {p.split('/visuals/')[-1]}")
                    for v in x.values():
                        walk(v)
                elif isinstance(x, list):
                    for v in x:
                        walk(v)
            walk(json.loads(text))
        self.add("Interactions", "Report field references (PBIR)", "FAIL" if missing else "PASS",
                 f"{refs - len(missing)}/{refs} visual and filter field references resolve to model objects",
                 {"missing": missing[:50]})
        rows, bad = [], 0
        pages = {p.display_name: p for p in res.pages}
        for db in self.wb.dashboards:
            page = pages.get(db.name)
            slicers = [v for v in (page.visuals if page else []) if v.visual_type == "slicer"]
            want = [z for z in db.zones if z.type in ("filter", "paramctrl")]
            ok = len(slicers) >= len(want)
            bad += 0 if ok else 1
            rows.append({"dashboard": db.name, "quick_filters_and_parameter_controls": len(want),
                         "slicers": len(slicers), "status": "PASS" if ok else "FAIL"})
        if rows:
            self.add("Interactions", "Quick filters and parameter controls -> slicers", "FAIL" if bad else "PASS",
                     f"{sum(r['slicers'] for r in rows)} slicers for {sum(r['quick_filters_and_parameter_controls'] for r in rows)} "
                     f"dashboard controls", {"columns": rows})
        acts = []
        for a in self.wb.actions:
            auto = a.type in ("filter", "highlight")
            acts.append({"action": a.name, "type": a.type, "source": a.source_sheet,
                         "power_bi": "default cross-filter" if a.type == "filter" else (
                             "default cross-highlight" if a.type == "highlight" else "manual: button / drillthrough / bookmark"),
                         "status": "PASS" if auto else "WARN"})
        if acts:
            manual = [a for a in acts if a["status"] != "PASS"]
            self.add("Interactions", "Dashboard actions -> interactions", "WARN" if manual else "PASS",
                     f"{len(acts) - len(manual)}/{len(acts)} actions map to native interactions; {len(manual)} need a manual step",
                     {"columns": acts})
        sorted_ws = [w for w in self.wb.worksheets if w.sorts]
        if sorted_ws:
            lost = [w.name for w in sorted_ws if not res.worksheet_visuals[w.name].sort]
            self.add("Interactions", "Sort order", "WARN" if lost else "PASS",
                     f"{len(sorted_ws) - len(lost)}/{len(sorted_ws)} sorted worksheets keep their sort", {"missing": lost})
        bidir = [r for r in res.model.relationships if r.cross_filter != "oneDirection"]
        self.add("Interactions", "Filter propagation design", "WARN" if bidir else "PASS",
                 "All relationships many-to-one, single direction (no ambiguous paths)" if not bidir else
                 f"{len(bidir)} bidirectional relationship(s): review for ambiguity and performance")
        self.add("Interactions", "Click paths, slicer defaults, tooltips, drillthrough", "PENDING",
                 "Human check in Power BI Desktop: click each visual and slicer and compare with the Tableau dashboard.")

    # ------------------------------------------------------------- gate 4 --
    def security_checks(self):
        res, wb = self.res, self.wb
        roles = res.model.roles
        if not roles and not wb.security_findings:
            self.add("Security", "User filters / USERNAME()", "N/A",
                     "No Tableau user filters or user functions found in the workbook.")
            self.add("Security", "Confirm no row-level security requirement", "PENDING",
                     "Business owner confirms the published Tableau content had no server-side permissions "
                     "or user filters that must be reproduced.")
            return
        objects = {(t.name, c.name) for t in res.model.tables for c in t.columns}
        for role in roles:
            problems = []
            for table, expr in role.table_permissions.items():
                try:
                    ast = parse_dax(expr)
                except DaxError as e:
                    problems.append(f"DAX does not parse: {e}")
                    continue
                for t, c in re.findall(r"'([^']+)'\[([^\]]+)\]", expr):
                    if (t, c) not in objects:
                        problems.append(f"unknown column '{t}'[{c}]")
                if "USERPRINCIPALNAME" not in expr.upper():
                    problems.append("filter does not use USERPRINCIPALNAME()")
                if not any(r.to_table == table or r.from_table == table for r in res.model.relationships):
                    problems.append(f"'{table}' has no relationship, the role would not filter the fact table")
            self.add("Security", f"RLS role lint: {role.name}", "FAIL" if problems else "PASS",
                     "; ".join(problems) or f"{len(role.table_permissions)} table permission(s) parse, reference "
                                              f"model columns and propagate to the fact table",
                     {"role": role.name, "permissions": role.table_permissions})
        for f in wb.security_findings:
            if f["applied_as"] != "data source filter":
                self.add("Security", f"User function in calculation '{f['calculation']}'", "WARN",
                         "USERNAME() used inside a calculation (not as a user filter): converted as DAX, review the "
                         "behaviour per user.")
        for role in roles:
            self._identity_matrix(role)
        self.add("Security", "Live identity test (real users, groups, SSO)", "PENDING",
                 "Assign Entra ID users/groups to each role in the Power BI service and test permitted and forbidden "
                 "users with 'View as'. Simulation does not replace this test.")

    def _identity_matrix(self, role):
        ds = next((d for d in self.wb.datasources for cf in d.calculations.values() if cf.internal_name == role.tableau_ref), None)
        if ds is None or ds.name not in self.tab_engines:
            return
        cf = ds.calculations[role.tableau_ref]
        base_eng = self.tab_engines[ds.name]
        ast = base_eng._calc_ast(cf.internal_name)
        users = []
        for n in ast.walk():
            if isinstance(n, FField) and not n.is_parameter and n.name in base_eng.cols:
                users = sorted({v for v in base_eng.cols[n.name] if v is not None})[:3]
                break
        users = users + ["no.access@contoso.com"]
        fact = self._fact(ds)
        kpis = [(w, r, m) for w, r, m in self.kpi_candidates(4)]
        from .runner import pill_label
        from .tableau_engine import TableauEngine
        recs = self.tab_records[ds.name]
        for user in users:
            teng = TableauEngine(self.wb, ds, recs, self.today, user=user)
            sec = self.dax.with_rls(role.table_permissions, user)
            rows = []
            t_rows = teng.visible_records()
            p_rows = len(sec.base[fact]) if fact in sec.base else None
            rows.append({"user": user, "metric": f"Visible {fact} rows", "tableau": t_rows, "powerbi": p_rows,
                         "status": "PASS" if t_rows == p_rows else "FAIL"})
            for ws, ref, m in kpis:
                try:
                    tv = self._kpi_value(teng, ws, ref, [])
                    pv = self._pbi_value(sec, m, [])
                    a, b, delta, tol, st = self._cmp(tv, pv)
                    status = "PASS" if st in ("MATCH",) or (a in (None, 0) and b in (None, 0)) else "FAIL"
                except Exception as e:
                    a = b = None
                    status = "ERROR"
                rows.append({"user": user, "metric": pill_label(self.wb, ws, ref.key), "tableau": a, "powerbi": b,
                             "status": status})
            self.report.identity_matrix.extend(rows)
            ok = all(r["status"] == "PASS" for r in rows)
            label = "forbidden user" if user.startswith("no.access") else "permitted user"
            self.add("Security", f"Identity {user} ({label})", "PASS" if ok else "FAIL",
                     f"Tableau USERNAME() filter: {t_rows} rows · Power BI role '{role.name}': {p_rows} rows · "
                     f"{sum(r['status'] == 'PASS' for r in rows)}/{len(rows)} values match (simulated identity)",
                     {"columns": rows})
