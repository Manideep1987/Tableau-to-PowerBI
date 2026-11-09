"""Serialise a ConversionResult into a Power BI Project (PBIP) folder / zip.

Layout::

    <Name>.pbip
    <Name>.SemanticModel/definition.pbism
    <Name>.SemanticModel/definition/{database,model,relationships,expressions}.tmdl
    <Name>.SemanticModel/definition/tables/<Table>.tmdl
    <Name>.Report/definition.pbir
    <Name>.Report/definition/{report,version}.json
    <Name>.Report/definition/pages/pages.json
    <Name>.Report/definition/pages/<page>/page.json
    <Name>.Report/definition/pages/<page>/visuals/<visual>/visual.json
"""
from __future__ import annotations

import io
import json
import os
import re
import uuid
import zipfile

from ..converter import ConversionResult, PbiFilter, PbiTable, PbiVisual

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def _guid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def tmdl_name(n: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n):
        return n
    return "'" + n.replace("'", "''") + "'"


def _expr_block(expr: str, indent: str) -> str:
    expr = expr.strip()
    if "\n" not in expr:
        return " " + expr
    lines = expr.split("\n")
    body = "\n".join(indent + "\t\t" + ln.rstrip() for ln in lines)
    return " ```\n" + body + "\n" + indent + "\t\t```"


def _desc(text, indent):
    if not text:
        return ""
    return "".join(f"{indent}/// {ln}\n" for ln in str(text).splitlines())


def table_tmdl(t: PbiTable, model_name: str) -> str:
    out = [f"table {tmdl_name(t.name)}", f"\tlineageTag: {_guid(model_name + t.name)}"]
    if t.hidden or t.kind == "measures":
        pass
    out.append("")
    for m in t.measures:
        d = _desc(m.description, "\t")
        out.append(f"{d}\tmeasure {tmdl_name(m.name)} ={_expr_block(m.expression, '')}")
        if m.format_string:
            out.append(f"\t\tformatString: {m.format_string}")
        if m.folder:
            out.append(f"\t\tdisplayFolder: {m.folder}")
        out.append(f"\t\tlineageTag: {_guid(model_name + t.name + 'm' + m.name)}")
        if m.tableau_ref:
            out.append(f"\n\t\tannotation TableauSource = {m.tableau_ref}")
        out.append("")
    for c in t.columns:
        d = _desc(c.description, "\t")
        if c.expression:
            out.append(f"{d}\tcolumn {tmdl_name(c.name)} ={_expr_block(c.expression, '')}")
        else:
            out.append(f"{d}\tcolumn {tmdl_name(c.name)}")
        out.append(f"\t\tdataType: {c.datatype}")
        if c.format_string:
            out.append(f"\t\tformatString: {c.format_string}")
        if c.hidden or (t.kind == "measures"):
            out.append("\t\tisHidden")
        if c.folder:
            out.append(f"\t\tdisplayFolder: {c.folder}")
        out.append(f"\t\tlineageTag: {_guid(model_name + t.name + 'c' + c.name)}")
        out.append(f"\t\tsummarizeBy: {c.summarize_by}")
        if not c.expression:
            src = c.source_column or c.name
            if t.dax_expression:
                out.append("\t\tisNameInferred")
            out.append(f"\t\tsourceColumn: {src}")
        if c.tableau_ref:
            out.append(f"\n\t\tannotation TableauSource = {c.tableau_ref}")
        out.append("")
    for hname, levels in t.hierarchies:
        out.append(f"\thierarchy {tmdl_name(hname)}")
        out.append(f"\t\tlineageTag: {_guid(model_name + t.name + 'h' + hname)}")
        out.append("")
        for lv in levels:
            out.append(f"\t\tlevel {tmdl_name(lv)}")
            out.append(f"\t\t\tlineageTag: {_guid(model_name + t.name + 'l' + hname + lv)}")
            out.append(f"\t\t\tcolumn: {tmdl_name(lv)}")
            out.append("")
    if t.dax_expression:
        out.append(f"\tpartition {tmdl_name(t.name)} = calculated")
        out.append("\t\tmode: import")
        out.append(f"\t\tsource ={_expr_block(t.dax_expression, chr(9)) if chr(10) in t.dax_expression else ' ' + t.dax_expression}")
    elif t.m_expression:
        out.append(f"\tpartition {tmdl_name(t.name)} = m")
        out.append("\t\tmode: import")
        out.append("\t\tsource =")
        for ln in t.m_expression.split("\n"):
            out.append("\t\t\t\t" + ln)
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    if t.kind == "parameter":
        out.append("\n\tannotation TableauParameter = true")
    return "\n".join(out) + "\n"


def model_tmdl(res: ConversionResult) -> str:
    lines = ["model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tsourceQueryCulture: en-US", "\tdataAccessOptions", "\t\tlegacyRedirects",
             "\t\treturnErrorValuesAsNull", "",
             "annotation PBI_QueryOrder = " + json.dumps([t.name for t in res.model.tables if t.m_expression]),
             "", "annotation __PBI_TimeIntelligenceEnabled = 0", "",
             f"annotation ConvertedFrom = Tableau workbook {res.workbook.name}", ""]
    for t in res.model.tables:
        lines.append(f"ref table {tmdl_name(t.name)}")
    for r in res.model.roles:
        lines.append(f"ref role {tmdl_name(r.name)}")
    return "\n".join(lines) + "\n"


def role_tmdl(role) -> str:
    out = [_desc(role.description, "") + f"role {tmdl_name(role.name)}", "\tmodelPermission: read", ""]
    for table, expr in role.table_permissions.items():
        out.append(f"\ttablePermission {tmdl_name(table)} = {expr}")
        out.append("")
    if role.tableau_ref:
        out.append(f"\tannotation TableauSource = {role.tableau_ref}")
    return "\n".join(out) + "\n"


def relationships_tmdl(res: ConversionResult) -> str:
    out = []
    for r in res.model.relationships:
        out.append(f"relationship {_guid(r.from_table + r.from_column + r.to_table + r.to_column)}")
        if not r.active:
            out.append("\tisActive: false")
        if r.cross_filter != "oneDirection":
            out.append(f"\tcrossFilteringBehavior: {r.cross_filter}")
        if getattr(r, "cardinality", "many-to-one") == "many-to-many":
            out.append("\ttoCardinality: many")         # 'to' table filters 'from' (single direction)
        out.append(f"\tfromColumn: {tmdl_name(r.from_table)}.{tmdl_name(r.from_column)}")
        out.append(f"\ttoColumn: {tmdl_name(r.to_table)}.{tmdl_name(r.to_column)}")
        out.append("")
    return "\n".join(out) + "\n"


def expressions_tmdl(res: ConversionResult) -> str:
    out = []
    for name, expr, desc in res.model.expressions:
        out.append(_desc(desc, "") + f"expression {tmdl_name(name)} = {expr}")
        out.append(f"\tlineageTag: {_guid('expr' + name)}")
        out.append("")
        out.append("\tannotation PBI_ResultType = " + ("Number" if 'Type="Number"' in expr else "Text"))
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- report --
def _col(table, column, source=None):
    ref = {"Source": source} if source else {"Entity": table}
    return {"Column": {"Expression": {"SourceRef": ref}, "Property": column}}


def _meas(table, name, source=None):
    ref = {"Source": source} if source else {"Entity": table}
    return {"Measure": {"Expression": {"SourceRef": ref}, "Property": name}}


def _lit(v):
    if v is None:
        return {"Literal": {"Value": "null"}}
    if isinstance(v, bool):
        return {"Literal": {"Value": "true" if v else "false"}}
    if isinstance(v, int):
        return {"Literal": {"Value": f"{v}L"}}
    if isinstance(v, float):
        return {"Literal": {"Value": f"{v}D"}}
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return {"Literal": {"Value": f"datetime'{s}T00:00:00'"}}
    return {"Literal": {"Value": "'" + s.replace("'", "''") + "'"}}


def filter_json(f: PbiFilter, idx: int, measure_table: str) -> dict:
    name = f"Filter{idx}"
    if f.kind == "categorical":
        cond = {"In": {"Expressions": [_col(f.table, f.column, "t")], "Values": [[_lit(v)] for v in f.values]}}
        if f.exclude:
            cond = {"Not": {"Expression": cond}}
        return {"name": name, "field": _col(f.table, f.column), "type": "Categorical",
                "filter": {"Version": 2, "From": [{"Name": "t", "Entity": f.table, "Type": 0}],
                           "Where": [{"Condition": cond}]}}
    if f.kind in ("range", "measure-range"):
        is_m = f.kind == "measure-range"
        target = _meas(measure_table, f.measure, "t") if is_m else _col(f.table, f.column, "t")
        conds = []
        if f.min is not None:
            conds.append({"Comparison": {"ComparisonKind": 2, "Left": target, "Right": _lit(f.min)}})
        if f.max is not None:
            conds.append({"Comparison": {"ComparisonKind": 4, "Left": target, "Right": _lit(f.max)}})
        cond = conds[0] if len(conds) == 1 else {"And": {"Left": conds[0], "Right": conds[1]}}
        entity = measure_table if is_m else f.table
        return {"name": name, "field": _meas(measure_table, f.measure) if is_m else _col(f.table, f.column),
                "type": "Advanced",
                "filter": {"Version": 2, "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                           "Where": [{"Condition": cond}]}}
    if f.kind == "topn":
        direction = 2 if f.top_direction == "top" else 1
        sub = {"Version": 2, "From": [{"Name": "t", "Entity": f.table, "Type": 0},
                                      {"Name": "m", "Entity": measure_table, "Type": 0}],
               "Select": [{**_col(f.table, f.column, "t"), "Name": "field"}],
               "OrderBy": [{"Direction": direction, "Expression": _meas(measure_table, f.by_measure, "m")}],
               "Top": f.top_n}
        return {"name": name, "field": _col(f.table, f.column), "type": "TopN",
                "filter": {"Version": 2,
                           "From": [{"Name": "subquery", "Expression": {"Subquery": {"Query": sub}}, "Type": 2},
                                    {"Name": "t", "Entity": f.table, "Type": 0}],
                           "Where": [{"Condition": {"In": {"Expressions": [_col(f.table, f.column, "t")],
                                                           "Table": {"SourceRef": {"Source": "subquery"}}}}}]}}
    if f.kind == "relative-date":
        unit = {"day": 0, "week": 1, "month": 2, "year": 3, "quarter": 2}.get(f.period_type or "day", 0)
        amount = (f.first_period or 0) * (3 if f.period_type == "quarter" else 1)
        return {"name": name, "field": _col(f.table, f.column), "type": "RelativeDate",
                "filter": {"Version": 2, "From": [{"Name": "t", "Entity": f.table, "Type": 0}],
                           "Where": [{"Condition": {"Between": {
                               "Expression": _col(f.table, f.column, "t"),
                               "LowerBound": {"DateSpan": {"Expression": {"DateAdd": {
                                   "Expression": {"Now": {}}, "Amount": amount, "TimeUnit": unit}}, "TimeUnit": unit}},
                               "UpperBound": {"DateSpan": {"Expression": {"Now": {}}, "TimeUnit": unit}}}}}]}}
    return {"name": name, "type": "Advanced"}


def visual_json(v: PbiVisual, z: int, measure_table: str) -> dict:
    container = {"$schema": f"{SCHEMA}/visualContainer/1.3.0/schema.json", "name": v.name,
                 "position": {"x": round(v.x, 2), "y": round(v.y, 2), "z": z * 1000,
                              "width": round(v.w, 2), "height": round(v.h, 2), "tabOrder": z * 1000}}
    if v.visual_type == "textbox":
        container["visual"] = {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": [
            {"textRuns": [{"value": v.text or "", "textStyle": {"fontWeight": "bold", "fontSize": "16pt"}}]}]}}]},
            "drillFilterOtherVisuals": True}
        return container
    query_state = {}
    for role, items in v.roles.items():
        if not items:
            continue
        projs = []
        for kind, table, name in items:
            field = _col(table, name) if kind == "column" else _meas(table, name)
            projs.append({"field": field, "queryRef": f"{table}.{name}", "nativeQueryRef": name})
        if role == "Category" and v.visual_type in ("lineChart", "areaChart", "lineClusteredColumnComboChart"):
            projs[0]["active"] = True
        query_state[role] = {"projections": projs}
    visual = {"visualType": v.visual_type, "query": {"queryState": query_state}, "drillFilterOtherVisuals": True}
    if v.sort:
        if "measure" in v.sort:
            visual["query"]["sortDefinition"] = {"sort": [{"field": _meas(measure_table, v.sort["measure"]),
                                                          "direction": "Descending" if v.sort["direction"].upper().startswith("D") else "Ascending"}],
                                                 "isDefaultSort": False}
        elif "column" in v.sort:
            t, c = v.sort["column"]
            visual["query"]["sortDefinition"] = {"sort": [{"field": _col(t, c),
                                                          "direction": "Descending" if v.sort["direction"].upper().startswith("D") else "Ascending"}]}
    if v.visual_type == "slicer":
        single = any("Parameter" in n for n in v.notes)
        visual["objects"] = {"data": [{"properties": {"mode": {"expr": {"Literal": {"Value": "'Dropdown'" if single else "'Basic'"}}}}}],
                             "selection": [{"properties": {"singleSelect": {"expr": {"Literal": {"Value": "true" if single else "false"}}}}}]}
    if v.title:
        visual["visualContainerObjects"] = {"title": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": "'" + v.title.replace("'", "''") + "'"}}}}}]}
    container["visual"] = visual
    if v.filters:
        container["filterConfig"] = {"filters": [filter_json(f, i, measure_table) for i, f in enumerate(v.filters)]}
    if v.notes:
        container["annotations"] = [{"name": "ConversionNote", "value": " | ".join(v.notes)}]
    return container


def build_files(res: ConversionResult) -> dict:
    """Return {relative path: text} for the whole PBIP project."""
    name = re.sub(r"[^\w\- ]", "_", res.workbook.name).strip() or "Report"
    sm, rp = f"{name}.SemanticModel", f"{name}.Report"
    files = {}
    files[f"{name}.pbip"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": rp}}], "settings": {"enableAutoRecovery": True}}, indent=2)
    files[f"{sm}/definition.pbism"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}}, indent=2)
    files[f"{sm}/.platform"] = json.dumps({"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                                           "metadata": {"type": "SemanticModel", "displayName": name},
                                           "config": {"version": "2.0", "logicalId": _guid(sm)}}, indent=2)
    files[f"{sm}/definition/database.tmdl"] = "database\n\tcompatibilityLevel: 1601\n"
    files[f"{sm}/definition/model.tmdl"] = model_tmdl(res)
    files[f"{sm}/definition/relationships.tmdl"] = relationships_tmdl(res)
    if res.model.expressions:
        files[f"{sm}/definition/expressions.tmdl"] = expressions_tmdl(res)
    for t in res.model.tables:
        fname = re.sub(r"[^\w\- ()]", "_", t.name)
        files[f"{sm}/definition/tables/{fname}.tmdl"] = table_tmdl(t, name)
    files[f"{sm}/definition/cultures/en-US.tmdl"] = "cultureInfo en-US\n"
    for role in res.model.roles:
        files[f"{sm}/definition/roles/{re.sub(r'[^\w\- ()]', '_', role.name)}.tmdl"] = role_tmdl(role)

    files[f"{rp}/definition.pbir"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{sm}"}}}, indent=2)
    files[f"{rp}/.platform"] = json.dumps({"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                                           "metadata": {"type": "Report", "displayName": name},
                                           "config": {"version": "2.0", "logicalId": _guid(rp)}}, indent=2)
    files[f"{rp}/definition/version.json"] = json.dumps({
        "$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"}, indent=2)
    files[f"{rp}/definition/report.json"] = json.dumps({
        "$schema": f"{SCHEMA}/report/1.2.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": "5.59",
                                          "type": "SharedResources"}},
        "layoutOptimization": "None",
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "allowChangeFilterTypes": True, "useEnhancedTooltips": True},
        "annotations": [{"name": "ConvertedFrom", "value": f"Tableau: {res.workbook.name}"}]}, indent=2)
    files[f"{rp}/definition/pages/pages.json"] = json.dumps({
        "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.name for p in res.pages], "activePageName": res.pages[0].name if res.pages else None}, indent=2)
    for p in res.pages:
        files[f"{rp}/definition/pages/{p.name}/page.json"] = json.dumps({
            "$schema": f"{SCHEMA}/page/1.3.0/schema.json", "name": p.name, "displayName": p.display_name,
            "displayOption": "FitToPage", "height": p.height, "width": p.width}, indent=2)
        for i, v in enumerate(p.visuals):
            files[f"{rp}/definition/pages/{p.name}/visuals/{v.name}/visual.json"] = json.dumps(
                visual_json(v, i, "_Measures"), indent=2)
    return files


def write_pbip(res: ConversionResult, out_dir: str) -> str:
    files = build_files(res)
    for rel, text in files.items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return out_dir


def pbip_zip(res: ConversionResult, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, text in build_files(res).items():
            z.writestr(rel, text)
        for rel, text in (extra or {}).items():
            z.writestr(rel, text)
    return buf.getvalue()
