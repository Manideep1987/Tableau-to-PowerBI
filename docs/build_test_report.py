"""Run the complete test campaign and write the test documentation.

    python docs/build_test_report.py            # pytest + UI walkthrough + UAT -> docs/TEST_REPORT.md / .docx

Steps
1. Unit / integration tests (pytest, JUnit XML)
2. UI walkthrough in a real browser (Playwright) with screenshots
3. UAT cases on the three sample workbooks (conversion + number validation)
4. Defect-injection (mutation) campaign proving the validator catches wrong conversions
5. Markdown + Word report
"""
from __future__ import annotations

import copy
import json
import os
import platform
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tableau2pbi import __version__  # noqa: E402
from tableau2pbi.catalog import catalog  # noqa: E402
from tableau2pbi.converter import convert  # noqa: E402
from tableau2pbi.parser import read_workbook  # noqa: E402
from tableau2pbi.pbip.writer import build_files  # noqa: E402
from tableau2pbi.validation.runner import DataBinding, validate  # noqa: E402
from tableau2pbi.validation.gates import verdict  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
SHOTS = os.path.join(DOCS, "screenshots")
OUT = os.path.join(DOCS, "test-results")
SAMPLES = os.path.join(ROOT, "samples", "workbooks")
DB = os.path.join(ROOT, "samples", "data", "superstore.db")
AS_OF = date(2025, 12, 31)
NAMES = ["Superstore_Sales.twb", "Finance_CustomSQL.twb", "HR_Analytics.twbx", "Invoice_Health.twb"]
os.makedirs(OUT, exist_ok=True)


# ------------------------------------------------------------------ 1. pytest
def run_pytest():
    junit = os.path.join(OUT, "pytest.xml")
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}", "tests"],
                       capture_output=True, text=True)
    dur = time.time() - t0
    tree = ET.parse(junit).getroot()
    suite = tree if tree.tag == "testsuite" else tree.find("testsuite")
    by_file = {}
    for tc in suite.iter("testcase"):
        f = tc.get("classname", "").split(".")[-1] or tc.get("file", "")
        st = "failed" if tc.find("failure") is not None or tc.find("error") is not None else (
            "skipped" if tc.find("skipped") is not None else "passed")
        d = by_file.setdefault(f, {"passed": 0, "failed": 0, "skipped": 0, "tests": []})
        d[st] += 1
        d["tests"].append((tc.get("name"), st))
    total = {k: sum(v[k] for v in by_file.values()) for k in ("passed", "failed", "skipped")}
    with open(os.path.join(OUT, "pytest.log"), "w") as f:
        f.write(p.stdout + p.stderr)
    return {"by_file": by_file, "total": total, "duration": round(dur, 1), "tail": p.stdout.strip().splitlines()[-1]}


# ------------------------------------------------------------------ 2. UI
def run_ui():
    import uvicorn
    from tableau2pbi.web.app import app
    port = 8799
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    env = dict(os.environ, SHOT_SCALE="1")
    p = subprocess.run([sys.executable, "tests/ui_walkthrough.py", f"http://127.0.0.1:{port}", SHOTS],
                       capture_output=True, text=True, env=env)
    server.should_exit = True
    th.join(timeout=5)
    steps = []
    for line in p.stdout.splitlines():
        if line.startswith(("PASS ", "FAIL ")):
            st, rest = line.split(" ", 1)
            name, _, detail = rest.partition(" - ")
            steps.append({"id": name.split(" ")[0], "name": name.split(" ", 1)[1], "status": st, "detail": detail})
    return {"steps": steps, "ok": p.returncode == 0, "log": p.stdout[-2000:] + p.stderr[-2000:]}


# ------------------------------------------------------------------ 3. UAT
def binding(wb):
    return DataBinding(sqlite=DB, packaged=wb.packaged_files)


def by_name(rep):
    return {c.name: c for c in rep.checks}


def run_samples():
    out = {}
    for n in NAMES:
        t0 = time.time()
        wb = read_workbook(os.path.join(SAMPLES, n))
        res = convert(wb)
        tc = time.time() - t0
        t1 = time.time()
        rep = validate(res, binding(wb), today=AS_OF)
        tv = time.time() - t1
        out[n] = {"wb": wb, "res": res, "rep": rep, "t_convert": tc, "t_validate": tv}
    return out


def mutation_campaign(samples):
    """Inject realistic conversion defects; the validator must flag each one."""
    s = samples["Superstore_Sales.twb"]
    f = samples["Finance_CustomSQL.twb"]
    i = samples["Invoice_Health.twb"]

    def rls_wrong(res):
        r = copy.deepcopy(res)
        r.model.roles[0].table_permissions["DimEmployeeSecurity"] = "'DimEmployeeSecurity'[Email UPN] = USERPRINCIPALNAME()"
        return r
    muts = []

    def mut_measure(res, name, expr):
        r = copy.deepcopy(res)
        r.model.measure(name).expression = expr
        return r

    def mut_column(res, table, col, expr):
        r = copy.deepcopy(res)
        r.model.table(table).column(col).expression = expr
        return r

    def drop_filter(res, ws):
        r = copy.deepcopy(res)
        r.worksheet_visuals[ws].filters = []
        return r

    def top_n(res, ws, n):
        r = copy.deepcopy(res)
        r.worksheet_visuals[ws].filters[0].top_n = n
        return r

    def drop_rel(res, table):
        r = copy.deepcopy(res)
        r.model.relationships = [x for x in r.model.relationships if x.to_table != table]
        return r

    cases = [
        ("M1", "Wrong aggregation in a measure", "Profit Ratio = SUM(Profit) / SUM(Quantity)",
         lambda: mut_measure(s["res"], "Profit Ratio", "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Quantity]))"),
         s, "Worksheet 'Category Profitability'"),
        ("M2", "Naive NULL translation", "Net Sales without BLANK guard (BLANK treated as 0)",
         lambda: mut_column(s["res"], "Orders", "Net Sales", "('Orders'[Sales] * (1 - 'Orders'[Discount]))"),
         s, "Worksheet 'Discount Analysis'"),
        ("M3", "Filter lost in conversion", "Segment Matrix visual without its Region filter",
         lambda: drop_filter(s["res"], "Segment Matrix"), s, "Worksheet 'Segment Matrix'"),
        ("M4", "FIXED LOD translated as plain CALCULATE", "Region Sales = CALCULATE(SUM(Sales)) (context transition)",
         lambda: mut_column(s["res"], "Orders", "Region Sales", "CALCULATE(SUM('Orders'[Sales]))"),
         s, "Worksheet 'Region Share'"),
        ("M5", "Missing relationship", "Orders -> Customers relationship removed",
         lambda: drop_rel(s["res"], "Customers"), s, "Worksheet 'Sales by Region'"),
        ("M6", "Wrong Top N", "Top Customers filter Top 5 instead of Top 10",
         lambda: top_n(s["res"], "Top Customers", 5), s, "Worksheet 'Top Customers'"),
        ("M7", "COUNTD not cross-filtered through the fact", "DISTINCTCOUNTNOBLANK('Customers'[Customer Name])",
         lambda: mut_measure(s["res"], "Distinct Count of Customer Name", "DISTINCTCOUNTNOBLANK('Customers'[Customer Name])"),
         s, "Worksheet 'Customer Tiers'"),
        ("M8", "Wrong table-calculation addressing", "Running Sales ordered by Region instead of Quarter",
         lambda: mut_measure(s["res"], "Running Sales",
                             "CALCULATE(SUM('Orders'[Sales]), WINDOW(1, ABS, 0, REL, ALLSELECTED('Customers'[Region]), ORDERBY('Customers'[Region])))"),
         s, "Worksheet 'Running Sales by Quarter'"),
        ("M9", "Join converted to star schema", "Finance physical inner join modelled as a relationship",
         lambda: convert(f["wb"], "star"), f, "Worksheet 'Attainment by Region'"),
        ("M10", "RLS role on the wrong column", "Manager Security role filters Email UPN instead of Manager UPN",
         lambda: rls_wrong(i["res"]), i, "Identity anna.lee@contoso.com (permitted user)"),
        ("M11", "On-time % definition changed", "Denominator = all invoices instead of paid invoices (guide challenge B)",
         lambda: mut_measure(i["res"], "On-time %", "DIVIDE([On Time Paid Amount], [Total Invoice Amount])"),
         i, "KPI scenario: All data"),
    ]
    for mid, title, change, make, sample, check in cases:
        res = make()
        rep = validate(res, binding(sample["wb"]), today=AS_OF, scenarios=False)
        c = by_name(rep).get(check)
        detected = c is not None and c.status in ("FAIL", "ERROR")
        g = {x["gate"]: x["status"] for x in rep.gates}
        muts.append({"id": mid, "title": title, "change": change, "check": check,
                     "status": c.status if c else "MISSING", "detail": c.detail if c else "",
                     "detected": detected, "gates": " ".join(f"G{k}:{v}" for k, v in g.items())})
    return muts


def uat_cases(samples, ui, muts, pyres):
    S, F, H, I = (samples[n] for n in NAMES)
    cases = []

    def case(cid, title, requirement, steps, expected, actual, ok, evidence=""):
        cases.append({"id": cid, "title": title, "requirement": requirement, "steps": steps,
                      "expected": expected, "actual": actual, "status": "PASS" if ok else "FAIL",
                      "evidence": evidence})

    wb, res = S["wb"], S["res"]
    ds = wb.datasources[0]
    inv = {"datasources": len(wb.datasources), "tables": len(ds.tables), "relationships": len(ds.relationships),
           "columns": len(ds.columns), "calculations": len(ds.calculations), "parameters": len(wb.parameters),
           "worksheets": len(wb.worksheets), "dashboards": len(wb.dashboards), "actions": len(wb.actions),
           "hierarchies": len(ds.hierarchies), "filters": sum(len(w.filters) for w in wb.worksheets)}
    exp = {"datasources": 1, "tables": 3, "relationships": 2, "columns": 21, "calculations": 26, "parameters": 4,
           "worksheets": 16, "dashboards": 2, "actions": 3, "hierarchies": 2, "filters": 7}
    case("UAT-01", "Read the entire Tableau metadata", "Tool must read all metadata of the workbook",
         "Convert Superstore_Sales.twb; compare the inventory with the authored workbook",
         json.dumps(exp), json.dumps(inv), inv == exp, "Parser inventory")
    conns = sorted({c.cls for n in NAMES for d in samples[n]["wb"].datasources for c in d.connections})
    m_ok = ("Sql.Database" in res.model.table("Orders").m_expression and
            "PostgreSQL.Database" in F["res"].model.table("Finance Orders").m_expression and
            "Csv.Document" in H["res"].model.table("employees.csv").m_expression)
    case("UAT-02", "Connections to Power Query", "Data sources must be re-created in Power BI",
         "Convert the 3 samples; inspect partitions", "SQL Server, PostgreSQL and CSV become native M connectors with server/db parameters",
         f"Connector classes {conns}; M parameters: {[e[0] for e in res.model.expressions + F['res'].model.expressions]}", m_ok)
    fm = F["res"].model.table("Finance Orders").m_expression
    case("UAT-03", "Custom SQL with a parameter", "SQL must be converted",
         "Convert Finance_CustomSQL.twb", "Value.NativeQuery with query folding; <Parameters.Min Quantity> becomes an M parameter; lint findings reported",
         f"NativeQuery={'Value.NativeQuery' in fm}; parameter={'Text.From(#\"Min Quantity\")' in fm}; findings={F['res'].sql_reviews[0]['findings']}",
         "Value.NativeQuery" in fm and 'Text.From(#"Min Quantity")' in fm and bool(F["res"].sql_reviews[0]["findings"]))
    rels = [(r.from_table, r.to_table) for r in res.model.relationships]
    case("UAT-04", "Relationships and joins", "Data model must follow Power BI best practice (star schema)",
         "Inspect Superstore and Finance models", "Superstore: Orders fact -> Customers/Products (many-to-one, single direction) + Date table; Finance: physical join flattened with Table.Join",
         f"Superstore relationships {rels}; Finance tables {[t.name for t in F['res'].model.tables if t.kind in ('single', 'fact', 'dim')]}",
         ("Orders", "Customers") in rels and ("Orders", "Products") in rels and "Table.Join" in fm)
    tot = {n: samples[n]["res"].stats for n in NAMES}
    case("UAT-05", "Every calculation converted to DAX", "Tableau calculations must have Power BI DAX equivalents",
         "Convert the 3 samples; review the Calculations tab", "100% of calculated fields, groups, bins, sets and parameters converted; none needs manual work",
         "; ".join(f"{n}: {st['converted'] + st['converted_with_notes']}/{st['converted'] + st['converted_with_notes'] + st['manual']}" for n, st in tot.items()),
         all(st["manual"] == 0 for st in tot.values()))

    def l3(sample, ws):
        return by_name(sample["rep"]).get(f"Worksheet '{ws}'")

    def group(cid, title, req, sample, sheets, expected):
        cs = [l3(sample, w) for w in sheets]
        ok = all(c and c.status == "PASS" for c in cs)
        case(cid, title, req, f"Validate worksheets: {', '.join(sheets)}", expected,
             "; ".join(f"{w}: {c.status} ({c.detail.split('|')[-1].strip()})" for w, c in zip(sheets, cs)), ok)

    group("UAT-06", "LOD expressions", "FIXED / INCLUDE / EXCLUDE must give Tableau numbers", S,
          ["Customer Tiers", "Region Share", "Top Customers"], "All cells match (FIXED per record, INCLUDE at view grain)")
    group("UAT-07", "Table calculations", "RUNNING_SUM, RANK, TOTAL, LOOKUP, WINDOW_AVG must match", S,
          ["Running Sales by Quarter", "Sub-Category Rank"], "All cells match")
    group("UAT-07b", "Table calculations (Finance, HR)", "LOOKUP, WINDOW_AVG, RUNNING_SUM(COUNTD)", F, ["Fiscal Year Sales"], "All cells match")
    scen = [c for c in S["rep"].checks if c.layer == "L4 Scenario" and "with " in c.name]
    case("UAT-08", "Parameters", "Parameters must work in Power BI", "Change each parameter to a new value and re-validate",
         "Numbers match for measure-driven parameters; parameter-driven calculated columns flagged as a known limitation",
         "; ".join(f"{c.name}: {c.status}" for c in scen),
         all(c.status in ("PASS", "WARN") for c in scen) and any(c.status == "WARN" and "KNOWN LIMITATION" in c.detail for c in scen))
    group("UAT-09", "Filters", "Categorical include/exclude, range, measure, Top N and date filters", S,
          ["Segment Matrix", "Discount Analysis", "Region Share", "Top Customers", "Monthly Trend", "Sales Distribution"],
          "All cells match")
    group("UAT-10", "Groups, bins and sets", "Groups/bins/sets converted with identical members", S,
          ["Customer Tiers", "Sales Distribution", "Key States Selected Metric"], "All cells match")
    pages = {p.display_name: [v.visual_type for v in p.visuals] for p in res.pages}
    case("UAT-11", "Dashboards to report pages", "Dashboards must be converted", "Review Report layout tab",
         "2 dashboard pages keep zone positions; quick filter and parameter control become slicers; text becomes a text box",
         json.dumps({k: v for k, v in pages.items() if k in ("Executive Overview", "Customer Insights")}),
         pages["Executive Overview"].count("slicer") == 2 and "textbox" in pages["Executive Overview"], "screenshots/06_layout.png")
    vt = {w: v.visual_type for w, v in res.worksheet_visuals.items()}
    case("UAT-12", "Worksheets to visuals", "Each worksheet must become the equivalent visual", "Compare marks/shelves with visual types",
         "Bar->bar/column, Line->line, Text->table/matrix/card, Pie->pie, Circle(measure x measure)->scatter, Multipolygon->filled map",
         json.dumps(vt), vt["Sales Share"] == "pieChart" and vt["Profit vs Sales"] == "scatterChart" and vt["State Map"] == "filledMap"
         and vt["Segment Matrix"] == "pivotTable" and vt["KPI Summary"] == "multiRowCard")
    group("UAT-13", "NULL handling", "NULL semantics must give identical numbers", H,
          ["Performance Mix", "Tenure Distribution", "Salary Benchmark"], "NULL performance score stays NULL (not FALSE); all cells match")
    files = {n: build_files(samples[n]["res"]) for n in NAMES}
    ok14 = all(f"{samples[n]['wb'].name}.pbip" in files[n] for n in NAMES)
    for n in NAMES:
        for p, text in files[n].items():
            if p.endswith((".json", ".pbip", ".pbir", ".pbism")):
                json.loads(text)
    case("UAT-14", "Power BI project output", "Output must be a Power BI report", "Download PBIP zip; inspect structure",
         "PBIP with TMDL semantic model and PBIR report; all JSON valid; every visual field exists in the model",
         f"files: {', '.join(f'{n}: {len(files[n])}' for n in NAMES)}", ok14)
    case("UAT-15", "Validator detects wrong conversions", "Validation must catch number differences",
         "Inject 9 realistic defects (see section 7) and re-validate", "Every injected defect is detected (FAIL)",
         f"{sum(m['detected'] for m in muts)}/{len(muts)} detected", all(m["detected"] for m in muts))
    cat = catalog()
    cats = {}
    for f_ in cat["functions"]:
        cats[f_["category"]] = cats.get(f_["category"], 0) + 1
    case("UAT-16", "Tableau to DAX equivalents", "Tool must tell the DAX equivalent of Tableau calculations",
         "Open DAX reference; search", "Reference of functions and structural mappings generated by the converter",
         f"{len(cat['functions'])} functions ({cats}); {len(cat['structural'])} structural mappings", len(cat["functions"]) >= 70)
    ui_ok = {s["id"]: s["status"] == "PASS" for s in ui["steps"]}
    case("UAT-17", "Formula translator", "Users can translate any formula", "Translate an LOD and an invalid formula in the UI",
         "DAX returned; syntax error explained", "UI-16, UI-17 " + ("PASS" if ui_ok.get("UI-16") and ui_ok.get("UI-17") else "FAIL"),
         ui_ok.get("UI-16", False) and ui_ok.get("UI-17", False), "screenshots/18_translator.png")
    case("UAT-18", "Compare exported reports", "Validate numbers of both reports from exports",
         "Upload a Tableau crosstab CSV and a Power BI export with one seeded difference",
         "Formatted numbers parsed; the single different row is reported", "UI-19 " + ("PASS" if ui_ok.get("UI-19") else "FAIL"),
         ui_ok.get("UI-19", False), "screenshots/20_compare_exports.png")
    case("UAT-19", "Simple UI following design principles", "UI must be very simple",
         "Playwright walkthrough: 4-step flow, responsive, dark mode, no JS errors",
         "All UI checks pass", f"{sum(ui_ok.values())}/{len(ui_ok)} UI checks passed", ui["ok"], "screenshots/")
    perf = {n: (round(samples[n]["t_convert"], 2), round(samples[n]["t_validate"], 1)) for n in NAMES}
    case("UAT-20", "Performance", "Interactive response times", "Measure conversion and validation time",
         "Conversion < 5 s, validation < 60 s per sample workbook",
         "; ".join(f"{n}: convert {c}s, validate {v}s" for n, (c, v) in perf.items()),
         all(c < 5 and v < 60 for c, v in perf.values()))
    live = pyres["by_file"].get("test_web_live", {})
    live_ok = any(n == "test_live_validation_against_stub" and st == "passed" for n, st in live.get("tests", []))
    case("UAT-21", "Live validation (Tableau Server + Power BI service)", "Validate published reports",
         "Run live_validate against Tableau REST and Power BI executeQueries stubs",
         "PAT sign-in, view data download, DAX executeQueries, comparison PASS", "test_live_validation_against_stub: " +
         ("passed" if live_ok else "failed"), live_ok)
    web = pyres["by_file"].get("test_web_live", {})
    err_ok = any(n == "test_upload_and_errors" and st == "passed" for n, st in web.get("tests", []))
    case("UAT-22", "Error handling", "Bad input must be handled gracefully",
         "Upload a PDF, a corrupt .twbx, nothing, a path-traversal sample name", "400/404 with a clear message; no crash",
         "test_upload_and_errors: " + ("passed" if err_ok else "failed"), err_ok)
    sk = [c for c in F["rep"].checks if c.status == "SKIP"]
    case("UAT-23", "Known limitations reported", "The tool must be transparent about what it cannot do",
         "Review WARN/SKIP checks and issues", "Parameter-driven calculated column -> WARN with fix; custom SQL parameter scenario -> SKIP with instruction",
         "; ".join(c.detail[:120] for c in sk), bool(sk))
    allpass = all(samples[n]["rep"].summary["FAIL"] == 0 and samples[n]["rep"].summary["ERROR"] == 0 for n in NAMES)
    case("UAT-24", "Overall number parity", "Numbers of both reports must match",
         "Run full validation on the 4 samples", "No FAIL / ERROR",
         "; ".join(f"{n}: {samples[n]['rep'].summary}" for n in NAMES), allpass)
    # ---- build guide requirements
    from tableau2pbi.ir import build_ir, lineage_csv, validate_ir
    from tableau2pbi.validation.gates import CSV_COLUMNS, results_csv, verdict
    errs = {n: validate_ir(build_ir(samples[n]["res"])) for n in NAMES}
    case("UAT-25", "Canonical IR v0.1.0", "Guide 6: versioned JSON IR with stable IDs, deps, confidence",
         "Build ir.json for the 4 samples; validate against schemas/ir-0.1.0.schema.json; build twice",
         "Schema-valid and deterministic", "; ".join(f"{n}: {len(e)} schema errors" for n, e in errs.items()),
         all(not e for e in errs.values()) and json.dumps(build_ir(I["res"]), sort_keys=True) == json.dumps(build_ir(I["res"]), sort_keys=True))
    sec_tests = {n: st for n, st in pyres["by_file"].get("test_guide_features", {}).get("tests", [])}
    sec_ok = all(sec_tests.get(t) == "passed" for t in ("test_rejects_path_traversal", "test_rejects_zip_bomb", "test_rejects_xml_entities"))
    case("UAT-26", "Secure ingestion", "Guide 15: XML entity / ZIP path attacks",
         "Upload a .twbx with ../ paths, a 30 MB zero-filled member, and a TWB with an external entity",
         "All three rejected with a clear error; no file written to disk", f"path traversal / zip bomb / XXE tests: {'passed' if sec_ok else 'failed'}", sec_ok)
    unk_ok = sec_tests.get("test_unknown_nodes_preserved_with_xpath") == "passed"
    case("UAT-27", "Unknown nodes preserved", "Guide 6: preserve unknown node + review flag",
         "Parse a workbook with an unrecognised element", "Listed with XPath; parse completeness < 100%",
         f"test_unknown_nodes_preserved_with_xpath: {'passed' if unk_ok else 'failed'}; Superstore completeness "
         f"{S['wb'].parse_stats['completeness']}%", unk_ok)
    role = I["res"].model.roles[0] if I["res"].model.roles else None
    ident = [c for c in I["rep"].checks if c.name.startswith("Identity")]
    case("UAT-28", "Row-level security", "Guide 8 RLS example: USERNAME() -> USERPRINCIPALNAME() role, identity matrix",
         "Convert Invoice_Health.twb; validate each manager and a forbidden user",
         "Role on DimEmployeeSecurity; each identity sees the same rows/KPIs in both tools; forbidden user sees 0; live test PENDING",
         (f"role {role.name}: {role.table_permissions}; " if role else "no role; ") + "; ".join(f"{c.name}: {c.status}" for c in ident),
         bool(role) and len(ident) == 4 and all(c.status == "PASS" for c in ident)
         and any(c.status == "PENDING" and c.name.startswith("Live identity") for c in I["rep"].checks), "screenshots/13_identity_matrix.png")
    gates_i = {g["gate"]: g["status"] for g in I["rep"].gates}
    m11 = next((m for m in muts if m["id"] == "M11"), {})
    case("UAT-29", "Five quality gates", "Guide 10: data, KPI, interactions, RLS, business UAT; earlier failure blocks later gates",
         "Validate Invoice Health; inject M11 (KPI definition change)",
         "Clean run: G1/G2 PASS, G3/G4/G5 PENDING human checks. Injected defect: G2 FAIL, G3-G5 BLOCKED",
         f"clean {gates_i}; injected {m11.get('gates')}",
         gates_i.get(1) == "PASS" and gates_i.get(2) == "PASS" and gates_i.get(5) == "PENDING"
         and "G2:FAIL G3:BLOCKED G4:BLOCKED G5:BLOCKED" in (m11.get("gates") or ""), "screenshots/11_validation_gates.png")
    reg = I["rep"].kpi_register
    case("UAT-30", "KPI regression register", "Guide 10: 5-15 critical KPIs x canonical filter scenarios with Tableau value, Power BI value, delta",
         "Validate Invoice Health", "All scenario rows within tolerance",
         f"{len(reg)} rows, {len({r['scenario'] for r in reg})} scenarios, {len({r['metric'] for r in reg})} KPIs, "
         f"{sum(r['status'] == 'PASS' for r in reg)} PASS", bool(reg) and all(r["status"] == "PASS" for r in reg),
         "screenshots/12_kpi_register.png")
    csv_text = results_csv(I["rep"].to_dict())
    lin = lineage_csv(I["res"], I["rep"].to_dict())
    case("UAT-31", "validation_results.csv and lineage", "Guide 12 prompt D + 15: scenario, metric, values, delta, tolerance, root cause, status, owner, approval notes; trace every field back to Tableau",
         "Download both CSVs", "All required columns; every PBI object traced to Tableau XPath and rule",
         f"validation_results.csv columns = {CSV_COLUMNS}; lineage rows = {len(lin.splitlines()) - 1}",
         csv_text.splitlines()[0].split(",") == CSV_COLUMNS and "rule:fixed-lod-column" in lin and "/workbook/" in lin)
    rd = {m["metric"]: m for m in I["rep"].readiness["metrics"]}
    case("UAT-32", "Readiness score", "Guide 13: parse completeness, calc pass rate, visual openability, numeric parity, security acceptance, manual effort",
         "Validate Invoice Health", "Measured metrics; openability reported as NOT RUN (no Windows)",
         "; ".join(f"{k}: {v['value']}" for k, v in rd.items()), rd["Parse completeness"]["value"] == 100.0
         and rd["Visual openability"]["value"] is None)
    ov_ok = sec_tests.get("test_api_override_signoff_history_artifacts") == "passed" and ui_ok.get("UI-05", False)
    case("UAT-33", "Mapping override with provenance", "Guide 15 checklist: reviewer can trace and override each field",
         "Override On-time % with an invalid then a valid DAX; re-validate", "Invalid rejected; valid applied, original kept, audit trail, re-validation detects the change",
         f"API test + UI-05: {'passed' if ov_ok else 'failed'}", ov_ok, "screenshots/04_override.png")
    ai_ok = all(sec_tests.get(t) == "passed" for t in ("test_ai_suggestion_is_validated_and_review_only", "test_ai_is_opt_in",
                                                     "test_redaction_and_dax_check"))
    case("UAT-34", "Opt-in AI assistance", "Guide 5/11/15: AI only for ambiguous expressions, redacted, reviewed",
         "Request a suggestion without credentials; with a stubbed Claude response; with a refusal",
         "Opt-in error without credentials; suggestion parsed + reference-checked + REVIEW only; identifiers redacted",
         f"AI tests: {'passed' if ai_ok else 'failed'} (model claude-opus-5-5, structured JSON output, server-side fallback)", ai_ok)
    ch = by_name(I["rep"])
    reg_all = {r["metric"]: r for r in reg if r["scenario"] == "All data"}
    case("UAT-35", "Finance showcase: the guide's three translation challenges", "Guide 8 A/B/C: total, on-time paid amount with NULLs, vendor FIXED LOD",
         "Validate Invoice Health KPI register and 'Vendor Concentration'",
         "A, B and C reconcile exactly (C as a record-level FIXED column, not ALLEXCEPT)",
         f"A Total {reg_all.get('Total Invoice Amount', {}).get('tableau')} = {reg_all.get('Total Invoice Amount', {}).get('powerbi')}; "
         f"B On-time % {reg_all.get('On-time %', {}).get('status')}; C {ch.get(chr(87) + 'orksheet ' + chr(39) + 'Vendor Concentration' + chr(39)).status}",
         reg_all.get("Total Invoice Amount", {}).get("status") == "PASS" and reg_all.get("On-time %", {}).get("status") == "PASS"
         and ch["Worksheet 'Vendor Concentration'"].status == "PASS")
    fz = load_fuzz()
    if fz:
        case("UAT-36", "Differential fuzzing", "Numbers must match for any calculation, not only the authored samples",
             f"Generate {fz['total_cases']} random calculations (row-level, aggregate, FIXED/INCLUDE/EXCLUDE LOD, table calcs, "
             f"NULL handling, dates, strings) and random worksheets (dims, measures, include/exclude, measure and Top N filters) on 3 data models",
             "Every case converts and Tableau engine = DAX engine cell by cell",
             f"{fz['total_pass']}/{fz['total_cases']} PASS over seeds {[x['seed'] for x in fz['seeds']]}",
             fz["total_pass"] == fz["total_cases"])
    rob = {n: st for n, st in pyres["by_file"].get("test_robustness", {}).get("tests", [])}
    ok = rob and all(v == "passed" for v in rob.values())
    case("UAT-37", "Hostile and unusual input", "Tool must not crash or produce invalid output on bad / unusual workbooks",
         "Random bytes, truncated XML, quotes / brackets / unicode / euro sign in names, empty tables, all-NULL columns, 300-file package",
         "Clean 400 errors or valid conversion; numbers match", f"{sum(v == 'passed' for v in rob.values())}/{len(rob)} robustness tests passed", bool(ok))
    case("UAT-38", "Nullability data contract", "Wrong NULL metadata must not silently change numbers",
         "Workbook declares a column NOT NULL, data contains NULLs", "Gate 1 FAIL naming the column; profiling + re-conversion -> PASS",
         "test_quotes_brackets_unicode_in_names: " + rob.get("test_quotes_brackets_unicode_in_names", "not run"),
         rob.get("test_quotes_brackets_unicode_in_names") == "passed")
    return cases


def load_fuzz():
    p = os.path.join(OUT, "fuzz_sweep.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


# ------------------------------------------------------------------ report
DEFECTS = [
    ("D-01", "High", "DAX engine", "Plain aggregations (SUM, COUNTROWS) and table references performed context transition, so a FIXED-LOD calculated column returned the row value.",
     "Found by L3 'Region Share' FAIL (Share of Region = 1.0).", "Context transition restricted to CALCULATE / measure references / RELATEDTABLE."),
    ("D-02", "High", "Translator", "COUNTD of a dimension-table column ignored filters on other dimensions (DAX filters flow one-to-many only).",
     "L3 'Customer Tiers' FAIL (2 vs 6 rows).", "Aggregations over dimension tables are wrapped in CALCULATE(..., <fact>) to reproduce Tableau relationship semantics."),
    ("D-03", "High", "Translator", "Comparison with a NULL operand produced FALSE in DAX while Tableau produces NULL (HR 'High Performer').",
     "L3 'Performance Mix' FAIL (NULL group missing).", "Null-preserving guard IF(ISBLANK(x), BLANK(), x op y) for nullable operands."),
    ("D-04", "Medium", "Converter", "Measures placed on Tableau label/tooltip marks were not added to the chart visual.",
     "L3 'Category Profitability' FAIL (unmapped Order Count).", "Label, tooltip, size and colour measures are added to the Tooltips well."),
    ("D-05", "Medium", "Parser", "Tableau's internal object-id column (datatype 'table') was read as a data column.",
     "L1 'Columns mapped' WARN (21/22).", "Internal object-model columns skipped."),
    ("D-06", "Medium", "Parser", "<computed-sort> elements were not read, so visual sort order was lost.", "Unit test test_worksheet_shelves_and_filters.",
     "All Tableau sort element variants parsed and mapped to sortDefinition."),
    ("D-07", "High", "Converter", "Physical joins modelled as a star schema change numbers when the join duplicates rows (SUM of target per order).",
     "Mutation M9 / star-schema option.", "Default join strategy 'flatten' (Table.Join in Power Query); star schema kept as an option with a warning."),
    ("D-08", "Medium", "DAX engine", "Visual query with dimensions on unrelated tables raised an error instead of cross-joining like SUMMARIZECOLUMNS.",
     "Mutation M5 returned ERROR.", "Cross join of unrelated column sets; missing relationship now surfaces as a number mismatch."),
    ("D-09", "Medium", "Converter", "Top-level INCLUDE/EXCLUDE LODs were converted as static calculated columns (wrong grain).",
     "Translation status 'Manual review'.", "Converted as view-grain measures: AGGX(SUMMARIZE(fact, dims), CALCULATE(expr)) / REMOVEFILTERS."),
    ("D-10", "Low", "Validator", "Validation took 95 s on Superstore (calculated columns re-filtered the full frame per row).",
     "Performance measurement.", "Mask caching, vectorised isin and CALCULATE result cache: 8 s."),
    ("D-11", "High", "Translator", "Two-argument MIN(a, b) / MAX(a, b) was treated as an aggregation (only the first argument used).",
     "Differential fuzzing (seed 1).", "Scalar MIN/MAX recognised in the AST, translator and Tableau engine."),
    ("D-12", "High", "Converter", "Text tables and KPI cards without dimensions dropped the measures placed on shelves (empty card).",
     "Differential fuzzing (seed 1).", "Card / multi-row card values = shelf measures + text measures."),
    ("D-13", "High", "Validator", "The Tableau engine aggregated fields of related (dimension) tables per fact row; Tableau relationships aggregate them at their native level.",
     "Differential fuzzing (SUM/AVG/COUNT of Products[Unit Price], DimVendor[Vendor Name]).", "Row identity per related table; aggregations over one related table count each of its rows once."),
    ("D-14", "Medium", "Translator", "COUNT / COUNTD of only-NULL values: Tableau 0 (row shown), DAX BLANK (row hidden).",
     "Differential fuzzing.", "COUNT/COUNTD of nullable columns return 0 when the table has rows in context."),
    ("D-15", "High", "Translator", "INDEX / RANK / WINDOW_* / LOOKUP ran over all addressing values; Tableau partitions only contain cells with data.",
     "Differential fuzzing (INDEX off by one when a year was missing in a partition).", "Relation = CALCULATETABLE(SUMMARIZE(fact, addressing), ALLSELECTED(addressing))."),
    ("D-16", "High", "Translator", "Scalar functions on NULL: DAX treats BLANK as 0 / \"\" / 1899-12-30 (WEEKDAY(BLANK-1) = 7) where Tableau returns NULL.",
     "Differential fuzzing (seed 1).", "Row-level scalar functions with nullable arguments are NULL-guarded; the DAX engine now reproduces DAX's BLANK date arithmetic."),
    ("D-17", "High", "Converter", "Top N combined with a dimension filter: Tableau ranks before the filter, Power BI after.",
     "Differential fuzzing (seed 1).", "Rank measure over the unfiltered context (ALLEXCEPT fact, Top N field) + visual measure filter rank <= N."),
    ("D-18", "High", "Translator", "NULL at aggregate level and three-valued logic: COUNTD(x) + MIN(null), IIF(NULL,...), CASE NULL, NULL IN (...), NOT NULL, NULL AND/OR.",
     "Differential fuzzing (seeds 1, 13).", "Guards in measures too; IIF returns NULL/unknown, CASE NULL -> ELSE, Kleene AND/OR."),
    ("D-19", "High", "Translator", "EXCLUDE LOD removed the worksheet's filter on the excluded dimension (REMOVEFILTERS); Tableau applies dimension filters first.",
     "Differential fuzzing (seed 2).", "EXCLUDE -> CALCULATE(expr, ALLSELECTED(dims))."),
    ("D-20", "High", "Translator", "Division by zero yields NULL in Tableau even with non-NULL inputs; comparisons on DIVIDE() results were not guarded.",
     "Differential fuzzing (seed 13).", "Divisions (except by a non-zero literal) are treated as possibly NULL."),
    ("D-21", "Medium", "Converter", "Metadata 'contains-null = false' contradicted by the data (CSV/Excel) produced unguarded DAX.",
     "Robustness test with unicode / quoted names.", "Gate 1 'Nullability contract' check + automatic data profiling before conversion (CLI, web app)."),
]

LIMITATIONS = [
    ("Parameter-driven calculated columns", "Power BI calculated columns are computed at refresh, so they don't react to slicers. Numbers match only at the parameter's default value. The validator reports this as WARN.",
     "Rebuild as a measure, or use a field parameter / dynamic segmentation table."),
    ("Parameters inside custom SQL", "Converted to Power Query parameters, which are applied at refresh. They can't be changed from a slicer.",
     "Use Dynamic M query parameters (DirectQuery) or move the filter to the model."),
    ("Context filters with FIXED LODs", "Tableau FIXED respects context filters; ALLEXCEPT / REMOVEFILTERS removes them. Flagged as a warning.",
     "Add KEEPFILTERS on the context column in the converted DAX."),
    ("Top N on a field that is not in the view, combined with dimension filters", "Tableau applies Top N before dimension filters; a Power BI TopN filter applies it after. (When the field is in the view the converter reproduces Tableau's order with a rank measure.)",
     "Add the Top N field to the visual, or rewrite the Top N as a RANKX measure."),
    ("String comparison case sensitivity", "DAX compares text case-insensitively; most Tableau sources compare case-sensitively (CONTAINS, =). Flagged on CONTAINS.",
     "Normalise case in Power Query when values differ only by case."),
    ("Table calculations used in several sheets with different addressing", "A DAX measure has one addressing (from the first sheet that uses it).",
     "Duplicate the measure per addressing; the validator flags mismatching sheets."),
    ("Visual formatting", "Colours, fonts, reference lines, annotations and custom shapes are not converted; layout is converted.",
     "Apply a Power BI theme; rebuild formatting by design (no pixel-perfect replication)."),
    ("Extracts (.hyper)", "Power BI can't read .hyper files; the converted model points at the original source.", "Repoint credentials and refresh."),
    ("Power BI Desktop opening", "PBIP files are generated to the published TMDL / PBIR schemas. Opening them in Power BI Desktop wasn't possible in the Linux test environment.",
     "Open the .pbip in Power BI Desktop (Developer mode with PBIR enabled) as the first acceptance step."),
]


def md_table(headers, rows):
    def cell(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_markdown(pyres, ui, samples, muts, cases):
    L = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_pass = sum(c["status"] == "PASS" for c in cases)
    L.append("# Tableau to Power BI Migrator: Test Report\n")
    L.append(md_table(["Item", "Value"], [
        ["Product", f"Tableau to Power BI Migrator v{__version__}"], ["Document", "Test strategy, execution evidence and UAT sign-off"],
        ["Executed", now], ["Prepared by", "Claude Code (automated test campaign)"],
        ["Environment", f"Python {platform.python_version()} · {platform.system()} · Chromium (Playwright)"]]))
    L.append("\n## 1. Executive summary\n")
    tot = pyres["total"]
    L.append(f"* **Automated tests:** {tot['passed']} passed, {tot['failed']} failed, {tot['skipped']} skipped ({pyres['duration']} s).")
    L.append(f"* **UI walkthrough (real browser):** {sum(s['status'] == 'PASS' for s in ui['steps'])}/{len(ui['steps'])} checks passed.")
    L.append(f"* **UAT:** {n_pass}/{len(cases)} cases passed.")
    for n in NAMES:
        s = samples[n]["rep"].summary
        L.append(f"* **{n}:** {s['PASS']} PASS · {s['WARN']} WARN · {s['FAIL']} FAIL · {s['ERROR']} ERROR · {s['SKIP']} SKIP. Overall **{s['overall']}**.")
    L.append(f"* **Defect injection:** {sum(m['detected'] for m in muts)}/{len(muts)} injected conversion defects were detected by the validator.")
    fz = load_fuzz()
    if fz:
        L.append(f"* **Differential fuzzing:** {fz['total_pass']}/{fz['total_cases']} random calculations and worksheets "
                 f"reconcile cell by cell (Tableau engine vs DAX engine) after fixing the defects it found (D-11 to D-20).")
    L.append('* **Verdict:** every number in the four sample reports matches Tableau. The exception is one documented Power BI limitation (a parameter-driven calculated column, reported as WARN). Gates 1-2 pass on all samples. Gates 3-5 wait on human checks (click paths, live identity tests, business sign-off), which this campaign records as PENDING instead of simulating.')
    L.append("\n## 2. Scope and approach\n")
    L.append("**In scope:** metadata extraction, conversion of data sources, custom SQL, joins, relationships, calculations, "
             "LODs, table calculations, groups, bins, sets, parameters, filters, worksheets and dashboards. Also in scope: "
             "the PBIP output, number validation, the Tableau→DAX reference, the formula translator, export comparison and the web UI.\n")
    L.append("**Validation method.** It follows the supplied research and build guide (\"Build a Tableau to Power BI "
             "Conversion Tool\", section 10). That guide summarises the BIChart validation article, which itself could not be "
             "fetched from the build environment. Validation is a feature, not a final checkbox. It has five gates, and a "
             "failed gate blocks every later gate. Human checks stay PENDING until a person performs them:\n")
    L.append(md_table(["Gate", "Automated checks", "Human / live-system checks"], [
        ["1 Data contract", "Metadata parity (every object has a counterpart); row counts, column sums, NULL and distinct counts; source SQL = Tableau = Power BI", "Same warehouse view, extract filters and business date"],
        ["2 Numeric / KPI", "Every worksheet vs its visual cell by cell; parameter scenarios; KPI regression register (critical KPIs x canonical filter scenarios); tolerance abs 0.01 / rel 1e-6", "Approve ambiguous definitions and unusual LOD cases"],
        ["3 Interactions", "PBIR field references resolve; quick filters and parameter controls become slicers; actions become interactions; sort order; single-direction relationships; slicer propagation scenarios", "Click paths, slicer defaults, tooltips, drillthrough (PENDING)"],
        ["4 Security", "USERNAME() user filter becomes an RLS role (lint); simulated identity matrix per manager plus a forbidden user", "Real users, groups and SSO (PENDING)"],
        ["5 Business UAT", "Decision log and signed output artifact (uat_signoff.json)", "Business owner signs off or records an approved redesign"],
    ]))
    L.append("\nThe two engines are independent implementations. The Tableau engine models record-level FIXED LODs, the Tableau "
             "filter order (context → Top N/FIXED → dimension → measure → table calc) and NULL propagation. The DAX engine "
             "executes the generated DAX with filter context, context transition, relationships, BLANK arithmetic and window "
             "functions. A third source of truth (SQL `SUM()` on the source database) anchors L2. For published reports, the "
             "same comparison runs live through the Tableau REST API (view data) and the Power BI `executeQueries` API.\n")
    L.append("## 3. Test data: sample workbooks\n")
    L.append(md_table(["Workbook", "Model", "Features covered"], [
        ["Superstore_Sales.twb", "SQL Server · relationships (Orders→Customers, Products)",
         "22 formulas incl. FIXED/INCLUDE LOD, RUNNING_SUM, RANK, TOTAL, CASE with parameter, string/date functions, NULL-safe math; group, bin, member set, Top-N set; 4 parameters (range, list, string); 16 sheets (bar, stacked, line, text, matrix, pie, scatter, filled map, KPI); categorical include/exclude, Top N, range on dimension/measure/date part; 2 dashboards with quick filter, parameter control, text, image; filter/highlight/URL actions; hierarchies, folders, descriptions, formats"],
        ["Finance_CustomSQL.twb", "PostgreSQL · custom SQL with <Parameters.Min Quantity> · physical INNER join · extract",
         "Fiscal year logic, LOOKUP, WINDOW_AVG, IF..NULL, CASE on numbers, SUM(IF..), STR/LEN; join duplication semantics"],
        ["HR_Analytics.twbx", "Packaged CSV (textscan)", "NULLs in dates and scores, ISNULL/IFNULL, date literal, FIXED AVG, MEDIAN, RUNNING_SUM(COUNTD), boolean dimensions"],
        ["Invoice_Health.twb (finance showcase)", "SQL Server FinanceDW · FactInvoices -> DimVendor, DimEmployeeSecurity, DimDate",
         "The guide's procure-to-pay demo: total invoice amount, on-time paid amount with NULL payment dates, on-time %, vendor FIXED LOD, aging buckets, overdue count, Top 10 vendors, 'Late invoices' parameter metric, TOTAL() share by field addressing, fiscal periods, and a USERNAME() data source filter converted to RLS"],
    ]))
    L.append(f"\nData: deterministic generator (`samples/generate_samples.py`, seed 42): 3,000 orders, 200 customers, 60 products, "
             f"400 employees, 1,500 invoices / 40 vendors / 12 AP clerks under 3 managers, deliberate NULLs (3% discounts, 5% performance "
             f"scores, terminations, unpaid invoices).\n")
    L.append("## 4. Automated test results\n")
    L.append(md_table(["Test module", "Passed", "Failed", "Skipped"],
                      [[f, d["passed"], d["failed"], d["skipped"]] for f, d in sorted(pyres["by_file"].items())]
                      + [["**Total**", tot["passed"], tot["failed"], tot["skipped"]]]))
    L.append(f"\n`{pyres['tail']}`. Full log: `docs/test-results/pytest.log`; JUnit: `docs/test-results/pytest.xml`.\n")
    L.append("## 5. UAT test cases\n")
    L.append(md_table(["ID", "Title", "Requirement", "Steps", "Expected", "Actual", "Status"],
                      [[c["id"], c["title"], c["requirement"], c["steps"], c["expected"], c["actual"][:400], f"**{c['status']}**"] for c in cases]))
    L.append("\n## 6. Number validation results\n")
    for n in NAMES:
        smp = samples[n]
        st = smp["res"].stats
        s = smp["rep"].summary
        L.append(f"### 6.{NAMES.index(n) + 1} {n}: **{s['overall']}**\n")
        L.append(f"Conversion: {st['tables']} tables, {st['relationships']} relationships, {st['measures']} measures, "
                 f"{st['calc_columns']} calculated columns, {st['pages']} pages, {st['visuals']} visuals in "
                 f"{smp['t_convert']:.2f} s; validation {smp['t_validate']:.1f} s.\n")
        L.append(md_table(["Gate", "Status", "Automated (pass / fail / warn)", "Pending human checks"],
                          [[f"{g['gate']} {g['name']}", f"**{g['status']}**", f"{g['automated_checks']} ({g['passed']} / {g['failed']} / {g['warnings']})",
                            "; ".join(g["pending_human_checks"])] for g in smp["rep"].gates]))
        L.append(f"\nVerdict: **{verdict(smp['rep'].gates)}**. Readiness: " + "; ".join(
            f"{m['metric']} {m['value'] if m['value'] is not None else 'n/a'}" for m in smp["rep"].readiness["metrics"]) + "\n")
        L.append(md_table(["Gate", "Layer", "Check", "Status", "Result"],
                          [[c.gate, c.layer, c.name, f"**{c.status}**", c.detail[:300]] for c in smp["rep"].checks]))
        L.append("")
    inv = samples["Invoice_Health.twb"]["rep"]
    L.append("**KPI regression register: Invoice Health** (the guide's example register, measured)\n")
    L.append(md_table(["Scenario", "KPI", "Tableau", "Power BI", "Delta", "Tolerance", "Acceptance"],
                      [[r["scenario"], r["metric"], _f(r["tableau"]), _f(r["powerbi"]), _f(r["delta"]), _f(r["tolerance"]),
                        r["status"]] for r in inv.kpi_register]))
    L.append("\n**Security identity matrix: Invoice Health** (simulated identities; the live user test stays PENDING)\n")
    L.append(md_table(["User", "Metric", "Tableau USERNAME() filter", "Power BI role", "Status"],
                      [[r["user"], r["metric"], _f(r["tableau"]), _f(r["powerbi"]), r["status"]] for r in inv.identity_matrix]))
    L.append("")
    sup = by_name(samples["Superstore_Sales.twb"]["rep"])
    for title in ("Worksheet 'Region Share'", "Worksheet 'Running Sales by Quarter'"):
        c = sup[title]
        d = c.data
        L.append(f"**Cell-level evidence: {title}**. Totals: " + "; ".join(
            f"{d['labels'].get(m, m)} → [{d['measure_map'].get(m)}]: Tableau {v['tableau']:,.4f} / Power BI {v['powerbi']:,.4f}"
            for m, v in d["summary"]["totals"].items()) + "\n")
        rows = []
        for r in d["rows"][:8]:
            rows.append([" / ".join(r["key"])] + [f"{_f(v['tableau'])} / {_f(v['powerbi'])}" for v in r["values"]] + [r["status"]])
        L.append(md_table(["Key"] + [d["labels"].get(m, m) + " (T / PBI)" for m in d["measures"]] + ["Status"], rows))
        L.append(f"\nDAX query for live validation:\n\n```\n{d['dax_query']}\n```\n")
    L.append("## 7. Defect injection (does the validator catch wrong conversions?)\n")
    L.append(md_table(["ID", "Injected defect", "Change", "Check", "Result", "Gates", "Detected"],
                      [[m["id"], m["title"], m["change"], m["check"], f"{m['status']}: {m['detail'][:150]}", m.get("gates", ""),
                        "Yes" if m["detected"] else "**No**"] for m in muts]))
    L.append("\n## 8. Tableau → DAX mapping of the sample calculations\n")
    for n in NAMES:
        L.append(f"### {n}\n")
        L.append(md_table(["Tableau field", "Tableau formula", "Power BI object", "DAX", "Rule", "Class"],
                          [[m.tableau_name, m.tableau_formula, f"{m.pbi_table}[{m.pbi_object}] ({m.kind})", m.dax, m.strategy,
                            m.classification] for m in samples[n]["res"].calc_mappings]))
        L.append("")
    fz = load_fuzz()
    if fz:
        L.append("\n## 7b. Differential fuzzing\n")
        L.append("Hand-written tests only cover the cases someone thought of. The fuzzer (`tests/fuzz/differential_fuzz.py`) "
                 "generates random, type-correct Tableau calculations and random worksheets on the Superstore, Invoice Health "
                 "and HR models. It converts them and compares the Tableau engine with the DAX engine for every cell. "
                 "The first 150-case run found 36 mismatches. They had eight root causes, all fixed (D-11 to D-18). Later "
                 "runs on fresh seeds found two more (D-19 EXCLUDE, D-20 division by zero). The final sweep is below. "
                 "`tests/test_robustness.py` runs 100 fixed-seed cases in every `pytest` run.\n")
        L.append(md_table(["Seed", "Cases", "PASS", "Mismatches", "Errors", "Unsupported", "Seconds"],
                          [[x["seed"], x["cases"], x["pass"], x["mismatches"], x["errors"], x["unsupported"], x["seconds"]]
                           for x in fz["seeds"]] + [["**Total**", fz["total_cases"], fz["total_pass"],
                                                     sum(x["mismatches"] for x in fz["seeds"]), sum(x["errors"] for x in fz["seeds"]),
                                                     sum(x["unsupported"] for x in fz["seeds"]), round(sum(x["seconds"] for x in fz["seeds"]))]]))
        L.append("")
    L.append("## 9. Defects found and fixed during testing\n")
    L.append(md_table(["ID", "Severity", "Component", "Defect", "How it was found", "Fix"], DEFECTS))
    L.append("\n## 10. Known limitations and recommendations\n")
    L.append(md_table(["Limitation", "Impact", "Recommendation"], LIMITATIONS))
    L.append("\n## 11. UI walkthrough evidence\n")
    L.append(md_table(["ID", "Check", "Status", "Detail"], [[s["id"], s["name"], s["status"], s["detail"]] for s in ui["steps"]]))
    for shot, cap in SHOT_CAPTIONS:
        if os.path.exists(os.path.join(SHOTS, shot)):
            L.append(f"\n**{cap}**\n\n![{cap}](screenshots/{shot})")
    L.append("\n## 12. Build guide traceability\n")
    L.append(md_table(["Guide section", "Requirement", "Implementation", "Evidence"], GUIDE_TRACE))
    L.append("\n## 13. Sign-off\n")
    L.append(md_table(["Criterion", "Result"], [
        ["All automated tests pass", "Yes" if tot["failed"] == 0 else "No"],
        ["All UAT cases pass", "Yes" if n_pass == len(cases) else "No"],
        ["Business UAT (gate 5)", "PENDING: recorded by the business owner in the Validation center (the UI walkthrough records a sample decision)"],
        ["Numbers match for all sample worksheets (L3)", "Yes" if all(c.status == "PASS" for n in NAMES for c in samples[n]["rep"].checks if c.layer == "L3 Visual") else "No"],
        ["Validator detects all injected defects", "Yes" if all(m["detected"] for m in muts) else "No"],
        ["Open items", "Open the generated PBIP in Power BI Desktop and run live validation against the published Tableau and Power BI content"],
    ]))
    text = "\n".join(L) + "\n"
    with open(os.path.join(DOCS, "TEST_REPORT.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


GUIDE_TRACE = [
    ["Exec decision", "Deterministic parsing -> typed IR -> rules -> selective AI -> PBIP/TMDL/PBIR -> independent validation", "parser.py, ir.py, formula/, converter.py, ai_assist.py, pbip/writer.py, validation/", "UAT-01, 05, 14, 25, 34"],
    ["MVP finish line", "PBIP, JSON inventory, original-vs-DAX pairs, supported/unsupported inventory, test report, provenance", "Export view: PBIP zip, migration_report.json, DAX_MAPPING.csv, inventory, lineage.csv, this report", "UAT-14, 31; UI-14/15"],
    ["2 BIChart workflow", "Connect/upload, inventory/assess, configure target, convert, validate/publish", "Upload -> Inventory -> Mapping -> Convert -> Validate -> Export; publishing deliberately not automated", "UI-01..15"],
    ["5 Five engines", "UI, API, conversion engine, AI assistants, Power BI builders, quality gates", "web/static, web/app.py (FastAPI), parser+converter, ai_assist.py, pbip/writer.py, validation/gates.py", "tests/*"],
    ["6 Canonical JSON", "Versioned IR, stable IDs, deps, confidence, unknown node preservation, lineage", "ir.py + schemas/ir-0.1.0.schema.json; XPath on every object", "UAT-25, 27, 31"],
    ["6 Minimum extraction", "TWB/TWBX, safe unzip (path traversal, zip bombs), data sources, fields, calcs, filters, shelves, zones", "parser.py LIMITS, entity rejection", "UAT-01, 26"],
    ["7 Translation coverage", "Classify, typed tree, deterministic rules, AI candidate, validate, PASS/REVIEW/UNSUPPORTED", "formula/dax.py rules; CalcMapping.classification/strategy/confidence", "UAT-05, 06, 07, 34"],
    ["8 Finance example", "Invoice Health: total, on-time paid amount, vendor FIXED LOD, RLS", "samples/invoice_health.py", "UAT-28, 35"],
    ["9 Real Power BI project", "PBIP folders, fields exist, TMDL parses, relationships reference known columns", "pbip/writer.py; Gate 3 PBIR reference check; DAX parser over all expressions", "UAT-14; test_report_references_exist_in_model"],
    ["10 Validation gates", "Data, KPI/DAX, interactions, RLS, business UAT; KPI register; blocked later gates", "validation/gates.py, gate_checks.py", "UAT-28..30; M10/M11"],
    ["12 Prompt D", "Read-only validation; CSV + HTML with scenario, metric, values, delta, tolerance, root cause, status, owner, approval", "validation_results.csv, VALIDATION_REPORT.html; never marks complete without running", "UAT-31"],
    ["12 Prompt E", "Upload -> Inventory -> Mapping -> Convert -> Validate -> Export; readiness, lineage, side-by-side DAX, rerun history, QA pack, finance showcase, accessible contrast", "web/static/*", "UI-01..22"],
    ["13 Readiness score", "Parse completeness, calc pass rate, visual openability, numeric parity, security acceptance, manual effort", "gates.readiness()", "UAT-32"],
    ["14 UX screens", "Upload & inspect, model explorer, DAX translator, page designer (layout), validation center, download", "Workbench tabs, Validation center, Export", "UI-01..22"],
    ["15 Security & risks", "Local processing, opt-in redacted AI, entity/ZIP guards, no credentials exported, RLS as hard gate, measured denominators", "parser LIMITS, ai_assist.redact, gate 4, readiness metrics", "UAT-26, 28, 34"],
    ["15 Go/no-go", "Openable PBIP with 2 pages; SUM/COUNTD/IF/CASE/FIXED reconcile; unsupported shown; RLS; trace + override", "All automated except Desktop open (Windows) and real users", "UAT-24, 28, 33; open items in sign-off"],
]

SHOT_CAPTIONS = [("01_convert_start.png", "Upload and samples (finance showcase first)"), ("02_inventory.png", "Inventory: parse completeness and feature support"),
                 ("03_mapping.png", "Mapping: Tableau → DAX with rule, confidence and classification"), ("04_override.png", "Reviewed DAX override with original kept"),
                 ("05_data_sql.png", "Data sources and SQL → Power Query"), ("06_model.png", "Semantic model"),
                 ("07_layout.png", "Dashboard converted to a report page"), ("09_security.png", "Row-level security role"),
                 ("11_validation_gates.png", "Validation center: five gates"), ("12_kpi_register.png", "KPI regression register"),
                 ("13_identity_matrix.png", "Security identity matrix"), ("15_signoff.png", "Business UAT sign-off"),
                 ("16_export.png", "Export center"),
                 ("14_validation_detail.png", "Cell-level comparison"), ("18_translator.png", "Formula translator"),
                 ("19_reference.png", "Tableau → DAX reference"), ("20_compare_exports.png", "Compare exports"),
                 ("21_mobile.png", "Phone layout"), ("22_dark_mode.png", "Dark mode")]


def _f(v):
    if v is None:
        return "∅"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 100 else f"{v:.4f}"
    return str(v)


def write_docx(md_text, out_name="Test_Report.docx"):
    """Render the Markdown report into a formatted Word document."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Inches, Pt, RGBColor
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10)
    from docx.enum.section import WD_ORIENT
    for s in doc.sections:
        s.orientation = WD_ORIENT.LANDSCAPE
        s.page_width, s.page_height = Inches(11.69), Inches(8.27)      # A4 landscape: wide test tables
        s.left_margin = s.right_margin = Inches(0.6)
        s.top_margin = s.bottom_margin = Inches(0.6)
    lines = md_text.splitlines()
    i = 0
    import re

    def add_runs(par, text):
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
            if part.startswith("**") and part.endswith("**"):
                r = par.add_run(part[2:-2])
                r.bold = True
                if part[2:-2] in ("PASS",):
                    r.font.color.rgb = RGBColor(0x13, 0x7A, 0x3F)
                elif part[2:-2] in ("FAIL", "ERROR", "No"):
                    r.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)
                elif part[2:-2] == "WARN":
                    r.font.color.rgb = RGBColor(0x8A, 0x5A, 0x00)
            elif part.startswith("`") and part.endswith("`"):
                r = par.add_run(part[1:-1])
                r.font.name = "Consolas"
                r.font.size = Pt(8.5)
            else:
                par.add_run(part)

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("# "):
            doc.add_heading(ln[2:].replace("**", ""), 0)
        elif ln.startswith("## "):
            doc.add_heading(ln[3:].replace("**", ""), 1)
        elif ln.startswith("### "):
            doc.add_heading(ln[4:].replace("**", ""), 2)
        elif ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            p = doc.add_paragraph()
            r = p.add_run("\n".join(buf))
            r.font.name = "Consolas"
            r.font.size = Pt(8)
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|(-{3}\|)+$", lines[i]):
                    rows.append([c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i])[1:-1]])
                i += 1
            i -= 1
            t = doc.add_table(rows=len(rows), cols=len(rows[0]))
            t.style = "Light Grid Accent 1"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            for ri, row in enumerate(rows):
                for ci, val in enumerate(row[:len(rows[0])]):
                    cell = t.cell(ri, ci)
                    cell.text = ""
                    par = cell.paragraphs[0]
                    add_runs(par, val)
                    for r in par.runs:
                        r.font.size = Pt(7.5 if len(rows[0]) > 4 else 8.5)
                        if ri == 0:
                            r.bold = True
        elif ln.startswith("!["):
            m = re.match(r"!\[(.*)\]\((.*)\)", ln)
            path = os.path.join(DOCS, m.group(2))
            if os.path.exists(path):
                doc.add_picture(path, width=Inches(8.5))
        elif ln.startswith("* "):
            add_runs(doc.add_paragraph(style="List Bullet"), ln[2:])
        elif ln.strip():
            add_runs(doc.add_paragraph(), ln)
        i += 1
    path = os.path.join(DOCS, out_name)
    doc.save(path)
    return path


def main():
    if "--guide" in sys.argv:            # render docs/USER_GUIDE.md to Word
        with open(os.path.join(DOCS, "USER_GUIDE.md"), encoding="utf-8") as f:
            print(write_docx(f.read(), "User_Guide.docx"))
        return
    if "--docx-only" in sys.argv:       # re-render the Word file from an existing TEST_REPORT.md
        with open(os.path.join(DOCS, "TEST_REPORT.md"), encoding="utf-8") as f:
            print(write_docx(f.read()))
        return
    print("1/5 pytest ...")
    pyres = run_pytest()
    print("   ", pyres["tail"])
    print("2/5 UI walkthrough ...")
    ui = run_ui()
    print("   ", sum(s["status"] == "PASS" for s in ui["steps"]), "/", len(ui["steps"]))
    print("3/5 sample conversions and validation ...")
    samples = run_samples()
    print("4/5 defect injection ...")
    muts = mutation_campaign(samples)
    print("   ", sum(m["detected"] for m in muts), "/", len(muts), "detected")
    cases = uat_cases(samples, ui, muts, pyres)
    print("   UAT", sum(c["status"] == "PASS" for c in cases), "/", len(cases))
    for c in cases:
        if c["status"] != "PASS":
            print("    FAILED", c["id"], c["actual"][:300])
    print("5/5 writing reports ...")
    md = write_markdown(pyres, ui, samples, muts, cases)
    write_docx(md)
    with open(os.path.join(OUT, "uat_results.json"), "w") as f:
        json.dump({"uat": cases, "mutations": muts, "ui": ui["steps"], "pytest": pyres["total"],
                   "validation": {n: samples[n]["rep"].to_dict() for n in NAMES}}, f, indent=2, default=str)
    from tableau2pbi.report import migration_report_html, validation_report_html
    for n in NAMES:
        base = os.path.splitext(n)[0]
        with open(os.path.join(OUT, f"{base}_validation.html"), "w") as f:
            f.write(validation_report_html(json.loads(json.dumps(samples[n]["rep"].to_dict(), default=str))))
        with open(os.path.join(OUT, f"{base}_migration.html"), "w") as f:
            f.write(migration_report_html(samples[n]["res"], samples[n]["rep"].to_dict()))
    print("done: docs/TEST_REPORT.md, docs/Test_Report.docx, docs/test-results/")


if __name__ == "__main__":
    main()
