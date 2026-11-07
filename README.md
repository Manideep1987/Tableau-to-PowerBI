# Tableau → Power BI Migrator

Convert Tableau workbooks (`.twb` / `.twbx`) into Power BI projects (`.pbip`), then **prove the numbers match**.

The design follows the supplied research and build guide (*Build a Tableau to Power BI Conversion Tool*). It is an
auditable converter, not a screenshot redrawer. Tableau parsing is deterministic and feeds a typed intermediate
representation. Rule-based mappings translate it, AI assists only where rules can't, and Microsoft-native
PBIP/TMDL/PBIR files are generated. Independent validation then runs through five quality gates. Processing is
local. Nothing is published.

| Engine (guide section 5) | Implementation |
|---|---|
| Web UI | Upload → Inventory → Mapping → Convert → Validate → Export (`tableau2pbi/web/static`) |
| Application API | FastAPI jobs, overrides, sign-off, artifacts, audit trail (`web/app.py`) |
| Conversion engine | Secure TWB/TWBX reader → canonical IR v0.1.0 with JSON Schema (`parser.py`, `ir.py`) |
| Rules | Tableau AST → DAX / Power Query rules with PASS / REVIEW / UNSUPPORTED (`formula/`, `converter.py`, `mcode.py`) |
| AI assistant | Opt-in Claude suggestions for unsupported expressions: redacted, parsed, reference-checked, review only (`ai_assist.py`) |
| Power BI builders | TMDL semantic model incl. RLS roles, PBIR pages and visuals (`pbip/writer.py`) |
| Quality gates | 1 Data → 2 KPI/numeric → 3 Interactions → 4 Security → 5 Business UAT (`validation/`) |

* **Reads all the workbook metadata:** connections, custom SQL, physical joins, relationships (logical layer), columns
  (types, roles, default aggregation, NULL flags, formats, descriptions, folders, hidden), calculated fields, groups,
  bins, sets, parameters, hierarchies, worksheets (shelves, marks, encodings, filters, sorts), dashboards (zones, quick
  filters, parameter controls, text) and actions.
* **Converts to a best-practice Power BI model:** a star schema with single-direction many-to-one relationships, a
  dedicated `_Measures` table with explicit measures, a marked Date table, Power Query parameters for server and
  database, parameter tables plus value measures, and date-part columns.
* **Translates every calculation to DAX.** This covers row-level calculations (calculated columns), aggregates
  (measures), FIXED / INCLUDE / EXCLUDE LODs, table calculations (WINDOW / OFFSET / ROWNUMBER / RANKX over
  ALLSELECTED), and NULL semantics.
* **Rebuilds dashboards** as report pages (PBIR) with mapped visuals, visual-level filters, slicers and text boxes.
* **Validates the numbers** in four layers: metadata parity, data reconciliation, visual-by-visual cell comparison,
  and parameter/slicer scenarios. Validation runs offline on source data, or live against Tableau Server and the
  Power BI service.
* **Tableau → DAX reference** and an interactive **formula translator**.

![Validation center](docs/screenshots/11_validation_gates.png)

**Step-by-step instructions: [docs/USER_GUIDE.md](docs/USER_GUIDE.md)** (also as Word: docs/User_Guide.docx).

## Quick start

```bash
pip install -r requirements.txt
python samples/generate_samples.py          # sample data + 4 sample workbooks (already committed)
python -m tableau2pbi serve --port 8000     # open http://127.0.0.1:8000
```

In the UI: **Convert**, then review the result, then **Validate numbers**, then **Download PBIP**. Open the `.pbip` in
Power BI Desktop (enable *Power BI Project (.pbip) save option* and *Store reports using enhanced metadata format (PBIR)*
in Options > Preview features). Then set the data source credentials and refresh.

### Command line

Samples: **Invoice_Health.twb** is the guide's finance showcase (procure-to-pay, on-time %, vendor FIXED LOD,
USERNAME() row-level security). There are also Superstore_Sales.twb, Finance_CustomSQL.twb and HR_Analytics.twbx.

Optional AI suggestions: set `ANTHROPIC_API_KEY` on the server. The UI then shows *Suggest with AI* for each
calculation. Only the formula and the model's table and column names are sent, with e-mails and hosts redacted. The
suggestion must parse and reference existing objects, and a reviewer has to apply it as an override.

```bash
# convert + validate against a copy of the source tables
python -m tableau2pbi convert samples/workbooks/Invoice_Health.twb -o out/invoice \
       --sqlite samples/data/superstore.db --as-of 2025-12-31

# validation only (exit code 2 when numbers differ)
python -m tableau2pbi validate samples/workbooks/HR_Analytics.twbx --validate --html report.html

python -m tableau2pbi translate "{FIXED [Customer Name] : MIN([Order Date])}"
python -m tableau2pbi compare tableau_crosstab.csv powerbi_export.csv --keys Region,Category
```

Already-converted samples are in [`samples/output/`](samples/output). Each folder holds the PBIP project, the migration
report, the validation report and the DAX mapping.

## How validation works

A failed gate **blocks** every later gate. Human checks stay **PENDING** until a person performs them; they are never
simulated as passed.

| Gate | Automated checks | Human / live checks |
|---|---|---|
| 1 Data contract | Metadata parity; row counts, column sums, NULL / distinct counts; source SQL = Tableau = Power BI | Same warehouse view and business date |
| 2 Numeric / KPI | Every worksheet vs its visual cell by cell; parameter scenarios; **KPI regression register** (critical KPIs x canonical filter scenarios) | Approve ambiguous definitions |
| 3 Interactions | PBIR field references resolve; quick filters → slicers; actions → interactions; sort; slicer propagation scenarios | Click paths, tooltips, drillthrough |
| 4 Security | `USERNAME()` user filter → RLS role lint; **simulated identity matrix** (each manager + a forbidden user) | Real users, groups, SSO |
| 5 Business UAT | Decision log, `uat_signoff.json` | Business owner sign-off |

Outputs: `validation_results.csv` (gate, scenario, metric, Tableau value, Power BI value, delta, tolerance,
root-cause category, status, owner, approval notes), `lineage.csv` (every Power BI object traced back to its Tableau
XPath, rule, confidence and evidence), `ir.json`, `migration_report.json`, and a readiness score:
- Parse completeness.
- Calculation pass rate.
- Visual openability.
- Numeric parity.
- Security acceptance.
- Manual effort estimate.

The **Tableau engine** (`validation/tableau_engine.py`) applies Tableau semantics:
- Record-level FIXED LODs.
- View-grain INCLUDE / EXCLUDE.
- The Tableau filter order (context → Top N → dimension → measure → table calc).
- Table-calc addressing and partitioning.
- NULL propagation.

The **DAX engine** (`validation/dax_engine.py`) parses and executes the *generated DAX* with:
- Filter context and row context.
- Context transition.
- CALCULATE modifiers.
- Relationship propagation.
- BLANK arithmetic.
- Window functions.

The two engines are independent implementations, so agreement between them is evidence the translation is right.

**Live mode** (`validation/live.py`) compares the published reports:
- Tableau side: REST API sign-in with a Personal Access Token, then each view's data.
- Power BI side: the generated DAX query per visual, run through `executeQueries`.

Two other comparison paths: the **Compare exports** screen compares a Tableau crosstab with a Power BI *Export data*
file, and every validated visual shows its DAX query for DAX Studio.

## Project layout

```
tableau2pbi/
  parser.py            TWB/TWBX metadata reader
  model.py             metadata model
  formula/             Tableau formula parser + DAX translator
  converter.py         semantic model, visuals, pages, filters, parameters
  mcode.py             connections + custom SQL -> Power Query (M)
  pbip/writer.py       PBIP: TMDL semantic model + PBIR report
  validation/          Tableau engine, DAX engine, comparator, orchestrator, live clients
  catalog.py           Tableau -> DAX reference (generated by the translator)
  report.py            JSON + HTML migration / validation reports
  web/                 FastAPI app + single-page UI (no external dependencies)
samples/               data generator, sample workbooks, converted outputs
tests/                 131 automated tests + Playwright UI walkthrough
docs/                  test report (Markdown + Word), screenshots, test evidence
```

## Testing

```bash
python -m pytest -q                  # 163 tests incl. 100 fixed-seed differential fuzz cases and hostile-input tests
python tests/fuzz/sweep.py           # 2,000 random calculations/worksheets: Tableau engine vs DAX engine
python docs/build_test_report.py     # full campaign: pytest + browser UI walkthrough + 39 UAT cases + defect injection
```

Results are in [docs/TEST_REPORT.md](docs/TEST_REPORT.md) and [docs/Test_Report.docx](docs/Test_Report.docx).

## Conversion rules (summary)

| Tableau | Power BI |
|---|---|
| Relationships | Star schema; hub table = fact; many-to-one, single direction |
| Physical joins | `Table.Join` in Power Query (keeps Tableau numbers); optional star schema (`--joins star`) |
| Custom SQL | `Value.NativeQuery(..., [EnableFolding=true])`; `<Parameters.X>` → Power Query parameter |
| Row-level calc | Calculated column (`RELATED` for dimension columns, NULL-safe operators) |
| Aggregate calc | Measure in `_Measures` (`DIVIDE`, iterators for expressions) |
| `{FIXED dims : agg}` | Calculated column: `CALCULATE(agg, REMOVEFILTERS(), dim = current)` |
| `{INCLUDE / EXCLUDE}` | Measure: `AGGX(SUMMARIZE(fact, dims), CALCULATE(agg))` / `CALCULATE(agg, REMOVEFILTERS(dims))` |
| Table calcs | `WINDOW`, `OFFSET`, `ROWNUMBER`, `RANKX` over `ALLSELECTED(addressing)` |
| COUNTD | `DISTINCTCOUNTNOBLANK`; dimension-table columns cross-filtered through the fact |
| Group / bin / set | `SWITCH(TRUE(), col IN {...})` / `FLOOR(col, size)` / `col IN {...}` or `RANKX(...) <= N` |
| Parameter | Disconnected table (`GENERATESERIES` / list) + slicer + `SELECTEDVALUE` measure |
| Filters | Basic / Advanced / TopN / RelativeDate visual filters; measure filters on aggregated values |
| Dashboard | Report page (zones scaled to 1280×720), quick filters and parameter controls become slicers |
| `USERNAME()` user filter | TMDL role with `USERPRINCIPALNAME()` table permission (tested per identity) |

Known limitations are listed in the [test report](docs/TEST_REPORT.md#10-known-limitations-and-recommendations).
