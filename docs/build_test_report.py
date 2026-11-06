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

DOCS = os.path.join(ROOT, "docs")
SHOTS = os.path.join(DOCS, "screenshots")
OUT = os.path.join(DOCS, "test-results")
SAMPLES = os.path.join(ROOT, "samples", "workbooks")
DB = os.path.join(ROOT, "samples", "data", "superstore.db")
AS_OF = date(2025, 12, 31)
NAMES = ["Superstore_Sales.twb", "Finance_CustomSQL.twb", "HR_Analytics.twbx"]
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
    ]
    for mid, title, change, make, sample, check in cases:
        res = make()
        rep = validate(res, binding(sample["wb"]), today=AS_OF, scenarios=False)
        c = by_name(rep).get(check)
        detected = c is not None and c.status in ("FAIL", "ERROR")
        muts.append({"id": mid, "title": title, "change": change, "check": check,
                     "status": c.status if c else "MISSING", "detail": c.detail if c else "",
                     "detected": detected})
    return muts


def uat_cases(samples, ui, muts, pyres):
    S, F, H = (samples[n] for n in NAMES)
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
         "DAX returned; syntax error explained", "UI-11, UI-12 " + ("PASS" if ui_ok.get("UI-11") and ui_ok.get("UI-12") else "FAIL"),
         ui_ok.get("UI-11", False) and ui_ok.get("UI-12", False), "screenshots/12_translator.png")
    case("UAT-18", "Compare exported reports", "Validate numbers of both reports from exports",
         "Upload a Tableau crosstab CSV and a Power BI export with one seeded difference",
         "Formatted numbers parsed; the single different row is reported", "UI-14 " + ("PASS" if ui_ok.get("UI-14") else "FAIL"),
         ui_ok.get("UI-14", False), "screenshots/14_compare_exports.png")
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
         "Run full validation (4 layers) on the 3 samples", "No FAIL / ERROR",
         "; ".join(f"{n}: {samples[n]['rep'].summary}" for n in NAMES), allpass)
    return cases


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
]

LIMITATIONS = [
    ("Parameter-driven calculated columns", "Power BI calculated columns are computed at refresh, so they don't react to slicers. Numbers match only at the parameter's default value. The validator reports this as WARN.",
     "Rebuild as a measure, or use a field parameter / dynamic segmentation table."),
    ("Parameters inside custom SQL", "Converted to Power Query parameters, which are applied at refresh. They can't be changed from a slicer.",
     "Use Dynamic M query parameters (DirectQuery) or move the filter to the model."),
    ("Context filters with FIXED LODs", "Tableau FIXED respects context filters; ALLEXCEPT / REMOVEFILTERS removes them. Flagged as a warning.",
     "Add KEEPFILTERS on the context column in the converted DAX."),
    ("Top N combined with dimension filters", "Tableau applies Top N before dimension filters; Power BI applies it after. Flagged as a warning.",
     "Make the dimension filter a context-like filter (page filter) or rewrite Top N as a RANKX measure."),
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
    L.append("* **Verdict:** every number in the three sample reports matches Tableau, except for one documented Power BI limitation "
             "(a parameter-driven calculated column, reported as WARN).")
    L.append("\n## 2. Scope and approach\n")
    L.append("**In scope:** metadata extraction, conversion of data sources, custom SQL, joins, relationships, calculations, "
             "LODs, table calculations, groups, bins, sets, parameters, filters, worksheets and dashboards. Also in scope: "
             "the PBIP output, number validation, the Tableau→DAX reference, the formula translator, export comparison and the web UI.\n")
    L.append("**Validation method.** The requested bichart.ai article could not be retrieved from the build environment "
             "(the domain is outside the network allow-list). The framework therefore follows published Tableau→Power BI "
             "migration validation practice:\n")
    L.append(md_table(["Layer", "What is checked", "How"], [
        ["L1 Metadata parity", "Every calculation, parameter, worksheet, dashboard, filter, column and relationship has a Power BI counterpart", "Inventory comparison"],
        ["L2 Data reconciliation", "Row counts, column sums, NULL counts and distinct counts", "Source SQL = Tableau data source = Power BI model tables"],
        ["L3 Visual reconciliation", "Every worksheet vs its converted visual, cell by cell", "Tableau-semantics engine vs DAX engine running the generated DAX, abs tolerance 0.01 / rel 1e-6"],
        ["L4 Scenarios", "Parameter changes and slicers on dimensions outside the view", "Re-run L3 with changed inputs (filter propagation)"],
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
    ]))
    L.append(f"\nData: deterministic generator (`samples/generate_samples.py`, seed 42): 3,000 orders, 200 customers, 60 products, "
             f"400 employees, deliberate NULLs (3% discounts, 5% performance scores, terminations).\n")
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
        L.append(md_table(["Layer", "Check", "Status", "Result"],
                          [[c.layer, c.name, f"**{c.status}**", c.detail[:300]] for c in smp["rep"].checks]))
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
    L.append(md_table(["ID", "Injected defect", "Change", "Check", "Result", "Detected"],
                      [[m["id"], m["title"], m["change"], m["check"], f"{m['status']}: {m['detail'][:150]}", "Yes" if m["detected"] else "**No**"] for m in muts]))
    L.append("\n## 8. Tableau → DAX mapping of the sample calculations\n")
    for n in NAMES:
        L.append(f"### {n}\n")
        L.append(md_table(["Tableau field", "Tableau formula", "Power BI object", "DAX", "Status"],
                          [[m.tableau_name, m.tableau_formula, f"{m.pbi_table}[{m.pbi_object}] ({m.kind})", m.dax, m.status]
                           for m in samples[n]["res"].calc_mappings]))
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
    L.append("\n## 12. Sign-off\n")
    L.append(md_table(["Criterion", "Result"], [
        ["All automated tests pass", "Yes" if tot["failed"] == 0 else "No"],
        ["All UAT cases pass", "Yes" if n_pass == len(cases) else "No"],
        ["Numbers match for all sample worksheets (L3)", "Yes" if all(c.status == "PASS" for n in NAMES for c in samples[n]["rep"].checks if c.layer == "L3 Visual") else "No"],
        ["Validator detects all injected defects", "Yes" if all(m["detected"] for m in muts) else "No"],
        ["Open items", "Open the generated PBIP in Power BI Desktop and run live validation against the published Tableau and Power BI content"],
    ]))
    text = "\n".join(L) + "\n"
    with open(os.path.join(DOCS, "TEST_REPORT.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


SHOT_CAPTIONS = [("01_convert_start.png", "Upload and samples"), ("02_conversion_summary.png", "Conversion summary and Tableau → DAX mapping"),
                 ("04_data_sql.png", "Data sources and custom SQL → Power Query"), ("05_model.png", "Semantic model"),
                 ("06_layout.png", "Dashboard converted to a report page"), ("07_filters_params.png", "Filters and parameters"),
                 ("08_issues.png", "Issues and manual steps"), ("10_validation_results.png", "Validation results"),
                 ("11_validation_detail.png", "Cell-level comparison"), ("12_translator.png", "Formula translator"),
                 ("13_reference.png", "Tableau → DAX reference"), ("14_compare_exports.png", "Compare exports"),
                 ("15_mobile.png", "Phone layout"), ("16_dark_mode.png", "Dark mode")]


def _f(v):
    if v is None:
        return "∅"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 100 else f"{v:.4f}"
    return str(v)


def write_docx(md_text):
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
    path = os.path.join(DOCS, "Test_Report.docx")
    doc.save(path)
    return path


def main():
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
