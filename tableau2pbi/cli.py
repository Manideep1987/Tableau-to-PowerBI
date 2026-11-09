"""Command line interface.

    python -m tableau2pbi convert  workbook.twbx -o out/ [--sqlite data.db]   # PBIP + reports
    python -m tableau2pbi validate workbook.twbx --sqlite data.db             # number validation
    python -m tableau2pbi translate "{FIXED [Region] : SUM([Sales])}"
    python -m tableau2pbi compare tableau.csv powerbi.csv
    python -m tableau2pbi serve --port 8000                                    # web UI
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import pandas as pd


def _today(args):
    return date.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None


def _profile(wb, args):
    """With source data available, correct 'contains-null' metadata before converting."""
    from .validation.runner import DataBinding, apply_keys, apply_nullability, key_findings, nullability_findings
    if not (getattr(args, "sqlite", None) or getattr(args, "csv_dir", None) or wb.packaged_files):
        return
    binding = DataBinding(args.sqlite, args.csv_dir, packaged=wb.packaged_files)
    found = nullability_findings(wb, binding)
    if apply_nullability(wb, found):
        print("Data profiling: treating as nullable (metadata said NOT NULL): " +
              ", ".join(f"{f['column']} ({f['nulls']} NULLs)" for f in found))
    mm = apply_keys(wb, key_findings(wb, binding))
    if mm:
        print("Data profiling: many-to-many relationships (key not unique on either side): " + ", ".join(mm))


def _convert(args):
    from .converter import convert
    from .parser import read_workbook
    from .pbip.writer import write_pbip
    from .report import conversion_json, dax_mapping_csv, migration_report_html, validation_report_html
    from .validation.runner import DataBinding, validate
    wb = read_workbook(args.workbook)
    _profile(wb, args)
    res = convert(wb, args.joins)
    os.makedirs(args.out, exist_ok=True)
    write_pbip(res, args.out)
    rep = None
    if args.sqlite or args.csv_dir or args.validate:
        rep = validate(res, DataBinding(args.sqlite, args.csv_dir, packaged=wb.packaged_files),
                       today=_today(args)).to_dict()
    from .ir import build_ir, lineage_csv
    from .validation.gates import results_csv, verdict
    if rep:
        rep["verdict"] = verdict(rep["gates"])
    files = {"MIGRATION_REPORT.html": migration_report_html(res, rep), "DAX_MAPPING.csv": dax_mapping_csv(res),
             "migration_report.json": json.dumps({**conversion_json(res), "validation": rep}, indent=2, default=str),
             "lineage.csv": lineage_csv(res, rep), "ir.json": json.dumps(build_ir(res, rep), indent=2, default=str),
             ".gitignore": "*.pbix\n.pbi/localSettings.json\n.pbi/cache.abf\n"}
    if rep:
        files["VALIDATION_REPORT.html"] = validation_report_html(rep)
        files["validation.json"] = json.dumps(rep, indent=2, default=str)
        files["validation_results.csv"] = results_csv(rep)
    for name, text in files.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    st = res.stats
    print(f"Converted {wb.name}: {st['tables']} tables, {st['measures']} measures, {st['calc_columns']} calculated "
          f"columns, {st['pages']} pages, {st['visuals']} visuals -> {args.out}")
    if rep:
        print("Validation:", rep["summary"])
        print("Gates:", ", ".join(f"{g['gate']} {g['name']}={g['status']}" for g in rep["gates"]))
        print("Verdict:", rep["verdict"])
        return 0 if rep["summary"]["overall"] != "FAIL" else 2
    return 0


def _validate(args):
    from .converter import convert
    from .parser import read_workbook
    from .report import validation_report_html
    from .validation.runner import DataBinding, validate
    wb = read_workbook(args.workbook)
    _profile(wb, args)
    res = convert(wb, args.joins)
    rep = validate(res, DataBinding(args.sqlite, args.csv_dir, packaged=wb.packaged_files), today=_today(args),
                   abs_tol=args.abs_tol, rel_tol=args.rel_tol).to_dict()
    for c in rep["checks"]:
        print(f"{c['status']:5} {c['layer']:12} {c['name']} | {c['detail']}")
    print(rep["summary"])
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(validation_report_html(rep))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, default=str)
    return 0 if rep["summary"]["overall"] != "FAIL" else 2


def translate_formula(formula: str):
    from .catalog import DemoResolver
    from .formula.ast import LOD, TABLE_CALCS, is_aggregate
    from .formula.dax import DaxTranslator
    from .formula.parser import parse_formula
    ast = parse_formula(formula)
    r = DemoResolver()
    tr = DaxTranslator(r)
    if isinstance(ast, LOD) and ast.kind in ("INCLUDE", "EXCLUDE"):
        return tr.lod_measure(ast, "sum")
    agg = is_aggregate(ast, r.calc) or any(getattr(n, "name", "") in TABLE_CALCS for n in ast.walk())
    return tr.translate(ast, "agg" if agg else "row", addressing=[("Products", "Category")])


def _translate(args):
    res = translate_formula(args.formula)
    print(res.expression)
    for w in res.warnings:
        print("  note:", w, file=sys.stderr)
    return 0


def _compare(args):
    from .validation.compare import compare_exports
    t = pd.read_csv(args.tableau, dtype=str, keep_default_na=False)
    p = pd.read_csv(args.powerbi, dtype=str, keep_default_na=False)
    out = compare_exports(t, p, args.keys.split(",") if args.keys else None, None, args.abs_tol)
    print(json.dumps(out["summary"], indent=2))
    print("Status:", out["status"])
    return 0 if out["status"] != "FAIL" else 2


def _live(args):
    """Compare the published Tableau views with the published Power BI dataset (read-only)."""
    from .converter import convert
    from .parser import read_workbook
    from .validation.live import PowerBIClient, TableauServerClient, live_validate
    secret = os.environ.get(args.pat_secret_env)
    token = os.environ.get(args.pbi_token_env)
    if not secret or not token:
        print(f"Set the environment variables {args.pat_secret_env} (Tableau PAT secret) and "
              f"{args.pbi_token_env} (Azure AD access token for the Power BI REST API).", file=sys.stderr)
        return 2
    res = convert(read_workbook(args.workbook))
    tab = TableauServerClient(args.tableau_server, args.site, args.pat_name, secret, args.api_version).sign_in()
    try:
        out = live_validate(res, tab, PowerBIClient(token), args.dataset, args.workspace, abs_tol=args.abs_tol)
    finally:
        tab.sign_out()
    for r in out:
        s = r.get("summary", {})
        print(f"{r['status']:5} {r['worksheet']} | {s.get('matched_cells', '-')}/{s.get('total_cells', '-')} cells "
              f"{r.get('detail', '')}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
    return 0 if all(r["status"] == "PASS" for r in out) else 2


def _serve(args):
    from .web.app import main as serve
    serve(args.host, args.port)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tableau2pbi", description="Tableau to Power BI migration toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="convert a .twb/.twbx into a PBIP project")
    c.add_argument("workbook")
    c.add_argument("-o", "--out", default="out")
    c.add_argument("--joins", choices=["flatten", "star"], default="flatten")
    c.add_argument("--sqlite")
    c.add_argument("--csv-dir")
    c.add_argument("--validate", action="store_true", help="validate using data packaged in the .twbx")
    c.add_argument("--as-of", help="'today' for relative date filters (YYYY-MM-DD)")
    c.set_defaults(fn=_convert)
    v = sub.add_parser("validate", help="validate numbers between Tableau and the converted model")
    v.add_argument("workbook")
    v.add_argument("--sqlite")
    v.add_argument("--csv-dir")
    v.add_argument("--joins", choices=["flatten", "star"], default="flatten")
    v.add_argument("--as-of")
    v.add_argument("--abs-tol", type=float, default=0.01)
    v.add_argument("--rel-tol", type=float, default=1e-6)
    v.add_argument("--html")
    v.add_argument("--json")
    v.set_defaults(fn=_validate)
    t = sub.add_parser("translate", help="translate a Tableau formula to DAX")
    t.add_argument("formula")
    t.set_defaults(fn=_translate)
    k = sub.add_parser("compare", help="compare a Tableau export with a Power BI export")
    k.add_argument("tableau")
    k.add_argument("powerbi")
    k.add_argument("--keys")
    k.add_argument("--abs-tol", type=float, default=0.01)
    k.set_defaults(fn=_compare)
    lv = sub.add_parser("live", help="compare published Tableau views with the published Power BI dataset")
    lv.add_argument("workbook")
    lv.add_argument("--tableau-server", required=True, help="e.g. https://tableau.contoso.com")
    lv.add_argument("--site", default="", help="Tableau site content URL ('' for Default)")
    lv.add_argument("--pat-name", required=True, help="Tableau personal access token name")
    lv.add_argument("--pat-secret-env", default="TABLEAU_PAT_SECRET", help="env var holding the PAT secret")
    lv.add_argument("--pbi-token-env", default="POWERBI_TOKEN", help="env var holding the Power BI access token")
    lv.add_argument("--dataset", required=True, help="Power BI dataset (semantic model) id")
    lv.add_argument("--workspace", help="Power BI workspace (group) id")
    lv.add_argument("--api-version", default="3.22")
    lv.add_argument("--abs-tol", type=float, default=0.01)
    lv.add_argument("--json")
    lv.set_defaults(fn=_live)
    s = sub.add_parser("serve", help="start the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=_serve)
    args = ap.parse_args(argv)
    return args.fn(args)
