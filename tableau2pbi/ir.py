"""Canonical intermediate representation (IR v0.1.0): the contract between engines.

The IR is a normalised, versioned JSON document with stable IDs. It records what was read
from the workbook (with the XML source path of every object), the dependency graph, the
conversion decision for every calculation (rule, confidence, PASS/REVIEW/UNSUPPORTED),
unknown nodes preserved for review, and security findings. The same workbook always
produces the same IR (deterministic IDs), so runs can be diffed.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import os
import re

from .converter import ConversionResult
from .formula.ast import Field as FField, LOD, TABLE_CALCS, Func, contains
from .formula.parser import FormulaError, parse_formula

IR_VERSION = "0.1.0"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schemas", f"ir-{IR_VERSION}.schema.json")
SUPPORTED_CONNECTORS = {"sqlserver", "postgres", "mysql", "oracle", "redshift", "snowflake", "bigquery",
                        "google-bigquery", "databricks", "excel-direct", "textscan", "teradata", "azure_sqldb",
                        "azure_sql_dw", "sqlite"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:60] or "x"


def ast_to_json(node):
    if node is None:
        return None
    if isinstance(node, list):
        return [ast_to_json(x) for x in node]
    if isinstance(node, tuple):
        return [ast_to_json(x) for x in node]
    if dataclasses.is_dataclass(node):
        out = {"node": type(node).__name__}
        for f in dataclasses.fields(node):
            out[f.name] = ast_to_json(getattr(node, f.name))
        return out
    return node


def build_ir(res: ConversionResult, validation: dict | None = None) -> dict:
    wb = res.workbook
    ids = {}
    sources, fields, calcs = [], [], []
    for ds in wb.datasources:
        sid = f"src_{slug(ds.caption)}"
        kind = "extract" if ds.is_extract else ("custom_sql" if any(t.relation.type == "text" for t in ds.tables) else
                                               "file" if any(c.cls in ("textscan", "excel-direct") for c in ds.connections) else "sql")
        sources.append({
            "id": sid, "name": ds.caption, "kind": kind, "model_style": ds.model_style, "xml_path": ds.xml_path,
            "connections": [{"class": c.cls, "server": c.server, "database": c.dbname, "schema": c.schema,
                             "file": c.filename, "supported": c.cls in SUPPORTED_CONNECTORS} for c in ds.connections],
            "tables": [{"id": f"tbl_{slug(t.name)}", "name": t.name, "type": t.relation.type, "physical": t.relation.table,
                        "sql": t.relation.sql} for t in ds.tables],
            "relationships": [{"left": f"{r.left_table}.{r.left_column}", "right": f"{r.right_table}.{r.right_column}",
                               "type": r.join_type} for r in ds.relationships],
        })
        for cname, col in ds.columns.items():
            fid = f"f_{slug(col.table or 'x')}_{slug(cname)}"
            ids[(ds.name, cname)] = fid
            if col.caption:
                ids.setdefault((ds.name, col.caption), fid)
            fields.append({"id": fid, "name": col.caption or cname, "source_id": sid, "table": col.table,
                           "remote_name": col.remote_name, "type": col.datatype, "role": col.role,
                           "nullable": col.contains_null, "hidden": col.hidden, "description": col.description,
                           "xml_path": col.xml_path})
        for name, cf in ds.calculations.items():
            ids[(ds.name, name)] = f"c_{slug(cf.caption)}"
            ids.setdefault((ds.name, cf.caption), f"c_{slug(cf.caption)}")
    for p in wb.parameters.values():
        ids[("Parameters", p.internal_name)] = f"p_{slug(p.caption)}"
        ids[("Parameters", p.caption)] = f"p_{slug(p.caption)}"
    maps = {m.tableau_id: m for m in res.calc_mappings if m.tableau_id}
    by_name = {(m.datasource, m.tableau_name): m for m in res.calc_mappings}
    for ds in wb.datasources:
        for name, cf in ds.calculations.items():
            ast = None
            deps = []
            if cf.formula and cf.kind == "formula":
                try:
                    ast = parse_formula(cf.formula)
                except FormulaError:
                    ast = None
            if ast is not None:
                for n in ast.walk():
                    if isinstance(n, FField):
                        key = ("Parameters", n.name) if n.is_parameter else (ds.name, n.name)
                        if key in ids and ids[key] not in deps:
                            deps.append(ids[key])
            elif cf.extra.get("source"):
                src = ids.get((ds.name, cf.extra["source"]))
                deps = [src] if src else []
            m = maps.get(name) or by_name.get((ds.caption, cf.caption))
            category = ("lod" if isinstance(ast, LOD) else
                        "table_calc" if ast is not None and contains(ast, lambda x: isinstance(x, Func) and x.name in TABLE_CALCS) else
                        "security" if cf.extra.get("security") else cf.kind if cf.kind != "formula" else
                        (m.kind if m else "unknown"))
            calcs.append({
                "id": ids[(ds.name, name)], "name": cf.caption, "internal_name": name, "source_id": f"src_{slug(ds.caption)}",
                "category": category, "tableau": cf.formula or (m.tableau_formula if m else ""),
                "expression_ast": ast_to_json(ast), "deps": deps, "xml_path": cf.xml_path,
                "pbi_object": m.pbi_object if m else None, "pbi_table": m.pbi_table if m else None,
                "pbi_dax": m.dax if m else None, "strategy": m.strategy if m else None,
                "confidence": m.confidence if m else "unresolved", "status": m.status if m else "Failed",
                "classification": m.classification if m else "UNSUPPORTED",
                "warnings": m.warnings if m else ["not converted"], "overridden": m.overridden if m else False})
    params = [{"id": f"p_{slug(p.caption)}", "name": p.caption, "type": p.datatype, "current_value": _s(p.current_value),
               "domain": p.domain_type, "members": [_s(v) for v, _ in p.members], "min": _s(p.min), "max": _s(p.max),
               "step": _s(p.step), "xml_path": p.xml_path} for p in wb.parameters.values()]
    worksheets = []
    for ws in wb.worksheets:
        v = res.worksheet_visuals.get(ws.name)
        worksheets.append({
            "id": f"ws_{slug(ws.name)}", "name": ws.name, "mark": ws.mark.lower(), "xml_path": ws.xml_path,
            "rows": [r.key for r in ws.rows], "columns": [r.key for r in ws.cols],
            "encodings": {k: [r.key for r in vs] for k, vs in ws.encodings.items()},
            "filters": [{"kind": f.kind, "field": f.field.key, "context": f.is_context, "xml_path": f.xml_path} for f in ws.filters],
            "target_visual": v.visual_type if v else None})
    dashboards = [{"id": f"db_{slug(d.name)}", "name": d.name, "width": d.width, "height": d.height, "xml_path": d.xml_path,
                   "zones": [{"id": z.id, "type": z.type, "name": z.name, "x": z.x, "y": z.y, "w": z.w, "h": z.h}
                             for d_ in [d] for z in d_.zones]} for d in wb.dashboards]
    ir = {
        "ir_version": IR_VERSION,
        "workbook": {"id": f"wb_{slug(wb.name)}", "name": wb.name, "version": wb.version, "source_build": wb.source_build,
                     "packaged_files": sorted(wb.packaged_files), "parse_stats": wb.parse_stats},
        "sources": sources, "fields": fields, "calculations": calcs, "parameters": params,
        "worksheets": worksheets, "dashboards": dashboards,
        "actions": [{"name": a.name, "type": a.type, "source": a.source_sheet, "xml_path": a.xml_path} for a in wb.actions],
        "security": {"rules": [{"datasource": f["datasource"], "calculation": f["calculation"], "formula": f["formula"],
                                "applied_as": f["applied_as"], "xml_path": f["xml_path"]} for f in wb.security_findings],
                     "roles": [{"name": r.name, "table_permissions": r.table_permissions} for r in res.model.roles],
                     "requires_identity_review": bool(wb.security_findings or res.model.roles)},
        "unknown_nodes": wb.unknown_nodes,
        "warnings": list(wb.warnings) + [i["message"] for i in res.issues if i["severity"] in ("warning", "error")],
        "inventory": feature_inventory(res),
    }
    if validation:
        ir["validation"] = {"summary": validation.get("summary"), "gates": validation.get("gates")}
    return ir


def _s(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def validate_ir(ir: dict) -> list:
    """Validate against the JSON Schema; returns a list of error messages (empty = valid)."""
    import jsonschema
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in v.iter_errors(ir)]


def feature_inventory(res: ConversionResult) -> list:
    """Per-feature inventory with support status (supported / review / unsupported)."""
    wb = res.workbook
    rows = []

    def add(category, item, count, status, note=""):
        if count:
            rows.append({"category": category, "item": item, "count": count, "status": status, "note": note})
    conns = [c for d in wb.datasources for c in d.connections]
    for cls in sorted({c.cls for c in conns}):
        add("Data source", f"Connector: {cls}", sum(c.cls == cls for c in conns),
            "supported" if cls in SUPPORTED_CONNECTORS else ("review" if cls in ("hyper", "dataengine") else "unsupported"),
            "native Power Query connector" if cls in SUPPORTED_CONNECTORS else "re-point to the original source")
    add("Data source", "Custom SQL", sum(1 for d in wb.datasources for t in d.tables if t.relation.type == "text"),
        "supported", "Value.NativeQuery with folding")
    add("Data source", "Relationships (logical layer)", sum(len(d.relationships) for d in wb.datasources
                                                             if d.model_style == "relationships"), "supported", "star schema")
    add("Data source", "Physical joins", sum(len(d.relationships) for d in wb.datasources if d.model_style == "joins"),
        "supported", "flattened with Table.Join (numbers preserved)")
    add("Data source", "Extracts", sum(d.is_extract for d in wb.datasources), "review", "import from the live source")
    add("Data source", "Data source filters (non-security)",
        sum(1 for d in wb.datasources for f in d.datasource_filters
            if not (d.calculations.get(f.field.field) and d.calculations[f.field.field].extra.get("security"))),
        "review", "Power Query filter step")
    blends = [w.name for w in wb.worksheets if len({r.datasource for r in w.all_fields()} - {"Parameters"}) > 1]
    add("Data source", "Data blends", len(blends), "unsupported", ", ".join(blends))
    cls = {}
    for m in res.calc_mappings:
        if m.kind in ("base-measure", "date-part"):
            continue
        cls.setdefault(m.kind, []).append(m.classification)
    for kind, lst in sorted(cls.items()):
        st = "unsupported" if "UNSUPPORTED" in lst else ("review" if "REVIEW" in lst else "supported")
        add("Calculations", {"measure": "Aggregate calculations", "column": "Row-level calculations",
                             "group": "Groups", "bin": "Bins", "set": "Sets", "parameter": "Parameters",
                             "rls-role": "User filters -> RLS roles"}.get(kind, kind), len(lst), st,
            f"{lst.count('PASS')} PASS / {lst.count('REVIEW')} REVIEW / {lst.count('UNSUPPORTED')} UNSUPPORTED")
    lods = tcs = 0
    for d in wb.datasources:
        for cf in d.calculations.values():
            try:
                ast = parse_formula(cf.formula) if cf.formula and cf.kind == "formula" else None
            except FormulaError:
                ast = None
            if ast is None:
                continue
            lods += contains(ast, lambda n: isinstance(n, LOD))
            tcs += contains(ast, lambda n: isinstance(n, Func) and n.name in TABLE_CALCS)
    add("Calculations", "LOD expressions", lods, "review", "context must be proven by validation")
    add("Calculations", "Table calculations", tcs, "review", "addressing from the first worksheet using it")
    marks = {}
    for ws in wb.worksheets:
        marks.setdefault(ws.mark, []).append(res.worksheet_visuals[ws.name].visual_type)
    for mk, vts in sorted(marks.items()):
        add("Worksheets", f"Mark: {mk}", len(vts), "supported", "-> " + ", ".join(sorted(set(vts))))
    add("Worksheets", "Dual axis", sum(w.dual_axis for w in wb.worksheets), "review", "combo chart")
    fk = {}
    for w in wb.worksheets:
        for f in w.filters:
            fk[f.kind + (" (context)" if f.is_context else "")] = fk.get(f.kind + (" (context)" if f.is_context else ""), 0) + 1
    for k, n in sorted(fk.items()):
        add("Filters", k, n, "review" if "context" in k or k == "condition" else "supported")
    zt = {}
    for d in wb.dashboards:
        for z in d.zones:
            zt[z.type] = zt.get(z.type, 0) + 1
    add("Dashboards", "Dashboards", len(wb.dashboards), "supported", "report pages")
    for t, n in sorted(zt.items()):
        add("Dashboards", f"Zone: {t}", n, "unsupported" if t in ("web", "extension") else
            ("review" if t in ("bitmap", "image", "button") else "supported"))
    at = {}
    for a in wb.actions:
        at[a.type] = at.get(a.type, 0) + 1
    for t, n in sorted(at.items()):
        add("Actions", t, n, "supported" if t in ("filter", "highlight") else "review")
    add("Security", "User filters / USERNAME()", len(wb.security_findings), "review",
        "RLS role + live identity test required")
    add("Other", "Unknown XML nodes (preserved)", len(wb.unknown_nodes), "review", "listed with XPath for review")
    return rows


LINEAGE_COLUMNS = ["tableau_type", "tableau_id", "tableau_name", "xml_path", "tableau_expression", "pbi_type",
                   "pbi_table", "pbi_object", "pbi_definition", "strategy", "confidence", "classification",
                   "validation_evidence", "overridden", "override_note"]


def lineage_rows(res: ConversionResult, validation: dict | None = None) -> list:
    evidence = {}
    for c in (validation or {}).get("checks", []):
        d = c.get("data") or {}
        objs = set(v for v in (d.get("measure_map") or {}).values() if v)
        objs |= {v[1] for v in (d.get("dimension_map") or {}).values() if v}
        objs |= {r["measure"] for r in d.get("register", [])}
        for o in objs:
            evidence.setdefault(o, []).append(f"{c['status']}: {c['name']}")
    rows = []
    for t in res.model.tables:
        if t.kind not in ("fact", "dim", "single"):
            continue
        for c in t.columns:
            if c.expression is None:
                rows.append({"tableau_type": "column", "tableau_id": c.tableau_ref, "tableau_name": c.tableau_ref,
                             "xml_path": "", "tableau_expression": "", "pbi_type": "column", "pbi_table": t.name,
                             "pbi_object": c.name, "pbi_definition": f"sourceColumn {c.source_column} ({c.datatype})",
                             "strategy": "rule:source-column", "confidence": "high", "classification": "PASS",
                             "validation_evidence": "; ".join(evidence.get(c.name, [])[:3])})
    for m in res.calc_mappings:
        rows.append({"tableau_type": m.kind, "tableau_id": m.tableau_id, "tableau_name": m.tableau_name,
                     "xml_path": m.xml_path or "", "tableau_expression": m.tableau_formula,
                     "pbi_type": "measure" if m.pbi_table == "_Measures" or m.kind in ("parameter",) else
                     ("role" if m.kind == "rls-role" else "calculated column"),
                     "pbi_table": m.pbi_table, "pbi_object": m.pbi_object, "pbi_definition": m.dax,
                     "strategy": m.strategy, "confidence": m.confidence, "classification": m.classification,
                     "validation_evidence": "; ".join(evidence.get(m.pbi_object, [])[:3]),
                     "overridden": "yes" if m.overridden else "", "override_note": m.override_note or ""})
    for ws in res.workbook.worksheets:
        v = res.worksheet_visuals.get(ws.name)
        rows.append({"tableau_type": "worksheet", "tableau_id": f"ws_{slug(ws.name)}", "tableau_name": ws.name,
                     "xml_path": ws.xml_path or "", "tableau_expression": f"mark={ws.mark}",
                     "pbi_type": "visual", "pbi_table": "", "pbi_object": v.visual_type if v else "",
                     "pbi_definition": "; ".join(f"{k}: " + ", ".join(n for _, _, n in it) for k, it in (v.roles.items() if v else [])),
                     "strategy": "rule:mark-and-shelf-mapping", "confidence": "high", "classification": "PASS",
                     "validation_evidence": next((f"{c['status']}: {c['name']}" for c in (validation or {}).get("checks", [])
                                                  if c["name"] == f"Worksheet '{ws.name}'"), "")})
    return rows


def lineage_csv(res: ConversionResult, validation: dict | None = None) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LINEAGE_COLUMNS)
    w.writeheader()
    for r in lineage_rows(res, validation):
        w.writerow({k: r.get(k, "") for k in LINEAGE_COLUMNS})
    return buf.getvalue()
