"""JSON serialisation of a conversion and self-contained HTML reports."""
from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime

from .converter import ConversionResult


def conversion_json(res: ConversionResult) -> dict:
    wb = res.workbook
    ds_out = []
    for ds in wb.datasources:
        ds_out.append({
            "name": ds.name, "caption": ds.caption, "model_style": ds.model_style, "is_extract": ds.is_extract,
            "connections": [{"name": c.name, "class": c.cls, "server": c.server, "database": c.dbname,
                             "schema": c.schema, "file": c.filename} for c in ds.connections],
            "tables": [{"name": t.name, "type": t.relation.type, "table": t.relation.table,
                        "sql": t.relation.sql} for t in ds.tables],
            "relationships": [r.__dict__ for r in ds.relationships],
            "columns": [{"name": n, "table": c.table, "remote": c.remote_name, "datatype": c.datatype,
                         "role": c.role, "nullable": c.contains_null, "hidden": c.hidden,
                         "caption": c.caption, "description": c.description} for n, c in ds.columns.items()],
            "hierarchies": [{"name": h.name, "levels": h.levels} for h in ds.hierarchies],
        })
    tables = []
    for t in res.model.tables:
        tables.append({
            "name": t.name, "kind": t.kind, "m": t.m_expression, "dax_source": t.dax_expression,
            "columns": [{"name": c.name, "datatype": c.datatype, "expression": c.expression,
                         "format": c.format_string, "hidden": c.hidden, "summarize_by": c.summarize_by,
                         "tableau": c.tableau_ref} for c in t.columns],
            "measures": [{"name": m.name, "expression": m.expression, "format": m.format_string,
                          "folder": m.folder, "tableau": m.tableau_ref} for m in t.measures],
            "hierarchies": [{"name": h, "levels": lv} for h, lv in t.hierarchies]})
    worksheets = []
    for ws in wb.worksheets:
        v = res.worksheet_visuals.get(ws.name)
        worksheets.append({
            "name": ws.name, "mark": ws.mark,
            "rows": [r.key for r in ws.rows], "cols": [r.key for r in ws.cols],
            "encodings": {k: [r.key for r in vs] for k, vs in ws.encodings.items()},
            "filters": [{"kind": f.kind, "field": f.field.key, "members": [str(m) for m in f.members],
                         "exclude": f.exclude, "min": _s(f.min), "max": _s(f.max), "top_n": f.top_n,
                         "top_by": f.top_by, "context": f.is_context} for f in ws.filters],
            "visual": _visual(v) if v else None})
    pages = [{"name": p.name, "display_name": p.display_name, "width": p.width, "height": p.height,
              "source": p.source, "visuals": [_visual(v) for v in p.visuals]} for p in res.pages]
    params = [{"name": p.caption, "internal": p.internal_name, "datatype": p.datatype,
               "value": _s(p.current_value), "domain": p.domain_type, "min": _s(p.min), "max": _s(p.max),
               "step": _s(p.step), "members": [_s(m[0]) for m in p.members]} for p in wb.parameters.values()]
    return {
        "workbook": {"name": wb.name, "version": wb.version, "build": wb.source_build,
                     "packaged_files": sorted(wb.packaged_files)},
        "stats": res.stats, "datasources": ds_out,
        "model": {"tables": tables,
                  "relationships": [r.__dict__ for r in res.model.relationships],
                  "expressions": [{"name": n, "expression": e} for n, e, _ in res.model.expressions]},
        "calculations": [m.__dict__ for m in res.calc_mappings],
        "parameters": params, "worksheets": worksheets, "pages": pages,
        "dashboards": [{"name": d.name, "width": d.width, "height": d.height,
                        "zones": [z.__dict__ for z in d.zones]} for d in wb.dashboards],
        "actions": [a.__dict__ for a in wb.actions],
        "issues": res.issues, "sql": [{k: v for k, v in s.items()} for s in res.sql_reviews],
    }


def _s(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _visual(v):
    return {"name": v.name, "type": v.visual_type, "worksheet": v.worksheet, "title": v.title,
            "x": round(v.x), "y": round(v.y), "w": round(v.w), "h": round(v.h), "text": v.text,
            "roles": {k: [{"kind": a, "table": b, "name": c} for a, b, c in items] for k, items in v.roles.items()},
            "filters": [{k: _s(x) if not isinstance(x, list) else [_s(i) for i in x] for k, x in f.__dict__.items()}
                        for f in v.filters],
            "notes": v.notes, "sort": v.sort and {k: (list(x) if isinstance(x, tuple) else x) for k, x in v.sort.items()}}


def dax_mapping_csv(res: ConversionResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Data source", "Tableau field", "Tableau formula", "Kind", "Power BI table", "Power BI object",
                "DAX", "Status", "Notes"])
    for m in res.calc_mappings:
        w.writerow([m.datasource, m.tableau_name, m.tableau_formula, m.kind, m.pbi_table, m.pbi_object, m.dax,
                    m.status, " | ".join(m.warnings)])
    return buf.getvalue()


# ------------------------------------------------------------------ HTML --
CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1d2433;--muted:#5b6475;--line:#e3e6ec;--accent:#2457d6;
--pass:#137a3f;--pass-bg:#e5f5ec;--warn:#8a5a00;--warn-bg:#fff3d6;--fail:#b42318;--fail-bg:#fde8e6;--info:#2457d6;--info-bg:#e8efff}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1a1f29;--ink:#e8ebf1;--muted:#a3abba;--line:#2c3342;
--accent:#7aa2ff;--pass:#6fd39b;--pass-bg:#17321f;--warn:#f1c45b;--warn-bg:#3a2e12;--fail:#ff8a80;--fail-bg:#3d1a18;--info:#7aa2ff;--info-bg:#1b2747}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:32px 16px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.card b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:8px 0}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
code,pre{font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}pre{white-space:pre-wrap;word-break:break-word;margin:0}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.PASS,.Converted{background:var(--pass-bg);color:var(--pass)}.WARN,.Converted.with.notes,.warning{background:var(--warn-bg);color:var(--warn)}
.FAIL,.ERROR,.Failed,.Manual.review,.error{background:var(--fail-bg);color:var(--fail)}.SKIP,.info{background:var(--info-bg);color:var(--info)}
.wrap{overflow-x:auto}
"""


def _pill(status):
    cls = html.escape(str(status)).replace(" ", ".")
    return f'<span class="pill {cls}">{html.escape(str(status))}</span>'


def migration_report_html(res: ConversionResult, validation: dict | None = None) -> str:
    e = html.escape
    st = res.stats
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Migration Report</title><style>{CSS}</style></head><body><main>",
           f"<h1>Migration report: {e(res.workbook.name)}</h1>",
           f"<div class='muted'>Generated {datetime.now():%Y-%m-%d %H:%M} · Tableau version {e(str(res.workbook.version))}</div>",
           "<div class='cards'>"]
    for label, key in (("Data sources", "datasources"), ("Tables", "tables"), ("Calculations", "calculations"),
                       ("Parameters", "parameters"), ("Worksheets", "worksheets"), ("Dashboards", "dashboards"),
                       ("Measures", "measures"), ("Calc columns", "calc_columns"), ("Pages", "pages")):
        out.append(f"<div class='card'><span class='muted'>{label}</span><b>{st[key]}</b></div>")
    conv = st["converted"] + st["converted_with_notes"]
    total = conv + st["manual"]
    out.append(f"<div class='card'><span class='muted'>Auto-converted</span><b>{round(100 * conv / total) if total else 100}%</b></div></div>")
    if validation:
        s = validation["summary"]
        out.append(f"<h2>Validation {_pill(s['overall'])}</h2><p>{s['PASS']} passed · {s['WARN']} warnings · "
                   f"{s['FAIL']} failed · {s['ERROR']} errors · {s['SKIP']} skipped (pass rate {s['pass_rate']}%).</p>")
    out.append("<h2>Calculations: Tableau → DAX</h2><div class='wrap'><table><tr><th>Field</th><th>Tableau</th><th>Power BI</th><th>DAX</th><th>Status</th></tr>")
    for m in res.calc_mappings:
        notes = "".join(f"<div class='muted'>• {e(w)}</div>" for w in m.warnings)
        out.append(f"<tr><td><b>{e(m.tableau_name)}</b><div class='muted'>{e(m.kind)}</div></td><td><pre>{e(m.tableau_formula)}</pre></td>"
                   f"<td>{e(m.pbi_table)}<br><b>{e(m.pbi_object)}</b></td><td><pre>{e(m.dax)}</pre>{notes}</td><td>{_pill(m.status)}</td></tr>")
    out.append("</table></div><h2>Semantic model</h2><table><tr><th>Table</th><th>Kind</th><th>Columns</th><th>Measures</th></tr>")
    for t in res.model.tables:
        out.append(f"<tr><td><b>{e(t.name)}</b></td><td>{e(t.kind)}</td><td>{len(t.columns)}</td><td>{len(t.measures)}</td></tr>")
    out.append("</table><table><tr><th>From (many)</th><th>To (one)</th><th>Direction</th><th>Tableau</th></tr>")
    for r in res.model.relationships:
        out.append(f"<tr><td>{e(r.from_table)}[{e(r.from_column)}]</td><td>{e(r.to_table)}[{e(r.to_column)}]</td>"
                   f"<td>{e(r.cross_filter)}</td><td>{e(r.tableau_join)}</td></tr>")
    out.append("</table>")
    if res.sql_reviews:
        out.append("<h2>Custom SQL</h2>")
        for s in res.sql_reviews:
            finds = "".join(f"<li>{e(f)}</li>" for f in s["findings"]) or "<li>No issues found</li>"
            out.append(f"<h3>{e(s['table'])}</h3><table><tr><th>Tableau SQL</th><th>Power Query (M)</th></tr><tr><td><pre>{e(s['sql'])}</pre></td>"
                       f"<td><pre>{e(s['m'] or '')}</pre></td></tr></table><ul>{finds}</ul>")
    out.append("<h2>Worksheets → visuals</h2><table><tr><th>Worksheet</th><th>Mark</th><th>Visual</th><th>Fields</th><th>Filters</th></tr>")
    for ws in res.workbook.worksheets:
        v = res.worksheet_visuals.get(ws.name)
        fields = "; ".join(f"{k}: " + ", ".join(n for _, _, n in it) for k, it in (v.roles.items() if v else []))
        flt = ", ".join(f.kind for f in (v.filters if v else []))
        out.append(f"<tr><td><b>{e(ws.name)}</b></td><td>{e(ws.mark)}</td><td>{e(v.visual_type if v else '-')}</td>"
                   f"<td>{e(fields)}</td><td>{e(flt)}</td></tr>")
    out.append("</table><h2>Dashboards → pages</h2><table><tr><th>Page</th><th>Source</th><th>Visuals</th></tr>")
    for p in res.pages:
        out.append(f"<tr><td><b>{e(p.display_name)}</b></td><td>{e(p.source)}</td><td>"
                   + e(", ".join(f"{v.visual_type}{' (' + v.worksheet + ')' if v.worksheet else ''}" for v in p.visuals)) + "</td></tr>")
    out.append("</table><h2>Issues and manual steps</h2><table><tr><th>Severity</th><th>Area</th><th>Object</th><th>Message</th></tr>")
    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(res.issues, key=lambda x: order.get(x["severity"], 3)):
        out.append(f"<tr><td>{_pill(i['severity'])}</td><td>{e(i['area'])}</td><td>{e(str(i['object']))}</td><td>{e(i['message'])}</td></tr>")
    out.append("</table></main></body></html>")
    return "".join(out)


def validation_report_html(rep: dict) -> str:
    e = html.escape
    s = rep["summary"]
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Validation Report</title><style>{CSS}</style></head><body><main>",
           f"<h1>Validation report: {e(rep['workbook'])} {_pill(s['overall'])}</h1>",
           f"<div class='muted'>{e(rep['started'])} · {rep['duration_s']}s · data: {e(rep['binding'])} · tolerance abs {rep['tolerance']['absolute']} / rel {rep['tolerance']['relative']}</div>",
           "<div class='cards'>"]
    for k in ("PASS", "WARN", "FAIL", "ERROR", "SKIP"):
        out.append(f"<div class='card'><span class='muted'>{k}</span><b>{s[k]}</b></div>")
    out.append(f"<div class='card'><span class='muted'>Pass rate</span><b>{s['pass_rate']}%</b></div></div>")
    out.append("<table><tr><th>Layer</th><th>Check</th><th>Status</th><th>Detail</th></tr>")
    for c in rep["checks"]:
        out.append(f"<tr><td>{e(c['layer'])}</td><td>{e(c['name'])}</td><td>{_pill(c['status'])}</td><td>{e(c['detail'])}</td></tr>")
    out.append("</table>")
    for c in rep["checks"]:
        d = c.get("data") or {}
        if "rows" not in d:
            continue
        out.append(f"<h2>{e(c['name'])} {_pill(c['status'])}</h2>")
        tot = d["summary"].get("totals", {})
        if tot:
            out.append("<table><tr><th>Measure</th><th>Tableau total</th><th>Power BI total</th><th>Difference</th></tr>")
            for m, v in tot.items():
                out.append(f"<tr><td>{e(m)} → {e(str(d.get('measure_map', {}).get(m)))}</td><td>{v['tableau']:,.4f}</td>"
                           f"<td>{v['powerbi']:,.4f}</td><td>{v['diff']:,.6f}</td></tr>")
            out.append("</table>")
        out.append("<div class='wrap'><table><tr><th>Key</th>" + "".join(f"<th>{e(m)}<br>Tableau / Power BI</th>" for m in d["measures"]) + "<th>Status</th></tr>")
        for r in d["rows"][:40]:
            cells = "".join(f"<td>{_fmt(v.get('tableau'))} / {_fmt(v.get('powerbi'))}</td>" for v in r["values"])
            out.append(f"<tr><td>{e(' | '.join(r['key']))}</td>{cells}<td>{_pill('PASS' if r['status'] == 'MATCH' else 'FAIL')} {e(r['status'])}</td></tr>")
        out.append("</table></div>")
        if d.get("dax_query"):
            out.append(f"<details><summary>DAX query for live validation</summary><pre>{e(d['dax_query'])}</pre></details>")
    out.append("</main></body></html>")
    return "".join(out)


def _fmt(v):
    if v is None:
        return "∅"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return html.escape(str(v))
