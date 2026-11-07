# Tableau to Power BI Migrator: User Guide

This guide walks you through migrating one Tableau workbook to Power BI and proving that the numbers match.
Plan about 15 minutes for the sample workbook; a real workbook depends on its size and on the review items.

## 1. What you need

| Item | Why |
|---|---|
| Python 3.11 or later | Runs the tool (Windows, macOS or Linux) |
| The Tableau workbook (`.twb` or `.twbx`) | Input. In Tableau Desktop use *File > Save As* or download it from Tableau Server / Cloud |
| A copy of the source data (SQLite file or CSV files), or the `.twbx` packaged data | Needed to **validate numbers** offline (step 6) |
| Power BI Desktop (Windows) | To open the generated project and publish it |
| Optional: Tableau Personal Access Token + Power BI access token | Live validation of the published reports (step 9) |
| Optional: `ANTHROPIC_API_KEY` | AI suggestions for calculations the rules cannot convert (off by default) |

## 2. Install

```bash
git clone https://github.com/Manideep1987/Tableau-to-PowerBI.git
cd Tableau-to-PowerBI
git checkout claude/tableau-powerbi-converter-cw5e05
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Check the installation (optional, about 5 minutes):

```bash
python -m pytest -q        # all tests must pass
```

## 3. Start the application

```bash
python -m tableau2pbi serve --port 8000
```

Open **http://127.0.0.1:8000** in a browser. Everything runs on your machine: the workbook and its data are not
sent anywhere.

The steps bar at the top shows where you are: **Upload → Inventory → Mapping → Convert → Validate → Export**.

## 4. Upload (step 1)

1. Drag the `.twb` / `.twbx` file onto the drop zone (or click it to browse). To try the tool first, click
   **Convert** next to `Invoice_Health.twb` (the finance showcase) in *Sample workbooks*.
2. Choose how physical joins are converted:
   * **Keep Tableau numbers (recommended).** Joined tables are merged in Power Query exactly as Tableau joins them,
     so a join that duplicates rows gives the same totals.
   * **Star schema.** Joined tables become related tables. This is cleaner, but numbers can change when the join
     duplicates rows. The validator will show you where.
3. Click **Analyze and convert**. Invalid or unsafe files are rejected with a message (for example a `.twbx`
   that contains `../` paths).

## 5. Review the conversion (steps 2 to 4)

The workbench opens on **Inventory**. Work through the tabs from left to right.

| Tab | What to check |
|---|---|
| **Inventory** (step 2) | *Parse completeness* should be 100%. Unknown XML nodes are listed with their path, so nothing is dropped silently. Every feature shows *supported*, *review* or *unsupported*. |
| **Mapping** (step 3) | Each Tableau calculation next to its DAX. **PASS** = tested rule, **REVIEW** = converted but check the semantics (LOD, table calculation, security, overrides), **UNSUPPORTED** = needs a manual rewrite. Use the chips to show only REVIEW / UNSUPPORTED. |
| **Data & SQL** | Connections and custom SQL next to the generated Power Query (M). Read the SQL findings (for example `SELECT *`). |
| **Model** | Tables, columns, measures and relationships (star schema, single direction). |
| **Report pages** | A wireframe of every dashboard page and the visual chosen for each worksheet. |
| **Filters & parameters** | Each Tableau filter next to its Power BI filter; parameters become tables + slicers. |
| **Security** | `USERNAME()` user filters converted to row-level security roles. |
| **Issues** | Everything that needs your attention, sorted by severity, with what to do. |

**To change a DAX expression** (Mapping tab):

1. Click **Override DAX** on the row and edit the expression.
2. Add a note explaining why; the note is kept in the lineage.
3. Click **Apply override**. The tool rejects DAX that does not parse or that references a column or measure that
   does not exist. The original DAX is kept.
4. If an AI key is configured, **Suggest with AI** proposes DAX. It only sends the formula and the model's table and
   column names, with e-mails and server names removed. The suggestion still has to be applied as an override.

After any override, run the validation again (step 6).

## 6. Validate the numbers (step 5)

Click **Validate numbers →** or open **Validation center**.

1. **Source data.** Give the tool the same data that Tableau uses:
   * *Bundled sample data* for the samples.
   * *SQLite database*: a copy of the source tables with the same table names as in Tableau.
   * *CSV files*: one file per table, named `<table>.csv` (for example `Orders.csv`). Data packaged in a `.twbx` is
     used automatically.

   To export a table to CSV, use *Export* in your database tool, or in Tableau Desktop *Data > View Data > Export
   All*.
2. **Business date.** Set the date used as "today" by relative date filters, the same date the Tableau report shows.
3. Keep **Scenario tests** on. Leave the tolerances at their defaults (0.01 absolute, 0.0001% relative) unless your
   business agrees on another rounding rule.
4. Click **Run validation**.

**Read the result.** The five gate cards summarise everything:

| Gate | Passes when | If it fails |
|---|---|---|
| 1 Data contract | Row counts, sums, NULL and distinct counts match the source; NULL metadata matches the data | Wrong data copy, wrong table name, or a different business date. Fix the data first, because later gates are blocked. |
| 2 Numeric / KPI | Every worksheet matches its visual cell by cell; KPIs match in every filter scenario | Open the failed check: it shows the rows, the Tableau value, the Power BI value and the DAX query. Fix the DAX (override) and re-run. |
| 3 Interactions | Fields resolve, quick filters became slicers, actions mapped, slicer scenarios match | Re-create the listed actions (URL, go-to-sheet) by hand. |
| 4 Security | Each simulated user sees the same rows and KPIs in both tools | Check the role filter in the Security tab. |
| 5 Business UAT | The business owner records the decision | Done by a person (see below). |

Statuses: **PASS** matches; **WARN** is a known, documented difference (the detail says why and how to fix it);
**FAIL** means the numbers differ; **PENDING** is a check a person must do; **BLOCKED** means an earlier gate failed.

Below the gates you find the **Readiness score**, the **KPI regression register** (each KPI under each filter
scenario), the **Security identity matrix**, all checks grouped by gate, and the **Run history**.

**Do the human checks** (they stay PENDING until done):

* Gate 3: open the report in Power BI Desktop. Click each visual and slicer, then compare tooltips and drill-through with Tableau.
* Gate 4: in the Power BI service, assign users or groups to each role. Use *Test as role / View as* for a permitted
  and a forbidden user.
* Gate 5: the business owner enters their name, picks *Approve*, *Approve with recorded redesign* or *Reject*, adds
  notes, and clicks **Record decision**.

## 7. Export (step 6)

Open **Export** and download:

| File | Contents |
|---|---|
| **PBIP (zip)** | The Power BI project plus every file below |
| `migration_report.json` | Inventory, mapping, model, pages, issues, validation, sign-off and audit trail |
| `lineage.csv` | Every Power BI column, measure, role and visual traced to its Tableau source (XML path), rule and evidence |
| `validation_results.csv` | Gate, scenario, metric, Tableau value, Power BI value, delta, tolerance, root cause, status, owner, approval notes |
| `ir.json` | The canonical description of the workbook (versioned, schema-validated) |
| `DAX_MAPPING.csv`, `MIGRATION_REPORT.html`, `VALIDATION_REPORT.html` | Human-readable dossiers |

## 8. Open and publish in Power BI Desktop

1. Unzip the PBIP download.
2. In Power BI Desktop open *File > Options and settings > Options > Preview features*. Enable **Power BI Project
   (.pbip) save option** and **Store reports using enhanced metadata format (PBIR)**, then restart Desktop.
3. *File > Open*: select `<Workbook>.pbip`.
4. Set the parameters (*Transform data > Edit parameters*): server and database names, plus any Tableau parameters
   used in custom SQL.
5. Enter the data source credentials and click **Refresh**.
6. Mark the date table (*Table tools > Mark as date table*) if the Issues tab asked you to.
7. Publish to a workspace when the gates are green and the business has signed off.

## 9. Live validation of the published reports (optional)

This step compares the published Tableau views with the published Power BI semantic model. Both connections are
read-only. Put the secrets in environment variables, never on the command line:

```bash
export TABLEAU_PAT_SECRET="<personal access token secret>"
export POWERBI_TOKEN="<Azure AD access token for https://analysis.windows.net/powerbi/api>"
python -m tableau2pbi live samples/workbooks/Invoice_Health.twb \
    --tableau-server https://tableau.contoso.com --site finance --pat-name migration-check \
    --dataset <semantic-model-id> --workspace <workspace-id> --json live_results.json
```

Each worksheet prints PASS or FAIL. The exit code is 0 only when all match.

## 10. Command line (automation / CI)

```bash
# convert + validate + all artifacts into a folder (exit code 2 if numbers differ)
python -m tableau2pbi convert MyWorkbook.twbx -o out/my --sqlite source.db --as-of 2025-12-31
python -m tableau2pbi convert MyWorkbook.twbx -o out/my --csv-dir exported_tables/ --as-of 2025-12-31
python -m tableau2pbi convert MyWorkbook.twbx -o out/my --validate        # data packaged in the .twbx

# validation only, with an HTML report
python -m tableau2pbi validate MyWorkbook.twbx --sqlite source.db --html validation.html

# one formula
python -m tableau2pbi translate "{FIXED [Customer Name] : MIN([Order Date])}"

# two exports (Tableau crosstab vs Power BI 'Export data')
python -m tableau2pbi compare tableau.csv powerbi.csv --keys Region,Category
```

When source data is given, the tool first profiles it. A column that the workbook declares NOT NULL but that
contains NULLs is converted as nullable, so the DAX keeps Tableau's NULL behaviour.

## 11. Other tools in the app

* **Formula translator**: paste any Tableau formula to get its DAX and notes. It uses the converted workbook's model
  when one is open.
* **DAX reference**: 76 Tableau functions and 35 structural mappings with their Power BI equivalents. Every example
  was produced by the converter.
* **Compare exports**: upload a Tableau crosstab and a Power BI export. Numbers like "$1,234.50", "(12)" and "45%"
  are understood.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "No data bound for table 'X'" | The SQLite file or CSV folder has no table / file named X. Use the table names shown in *Data & SQL*. |
| Gate 1 *Row count* FAIL | The data copy is not the one Tableau uses (different extract filter, date or refresh). |
| Gate 1 *Nullability contract* FAIL | Metadata says NOT NULL but the data has NULLs. Re-run the conversion with data (the web app does this automatically). |
| A KPI differs only for a parameter value | A calculated column depends on a parameter. Power BI calculated columns don't react to slicers; rebuild it as a measure (WARN explains). |
| *Custom SQL parameter* SKIP | Power Query parameters apply at refresh: change the parameter in both tools, refresh, validate again. |
| Rows "hidden in Power BI" | Tableau shows rows whose measures are all empty; Power BI hides them (*Show items with no data*). No number differs. |
| The `.pbip` does not open | Enable both preview features in step 8, and use a current Power BI Desktop. |

## 13. What the tool does not do

* It does not publish to Power BI or change anything on Tableau Server.
* It does not convert visual formatting (colours, fonts, reference lines, annotations, custom shapes). It converts the layout.
* It cannot read Tableau extracts (`.hyper`). The model points at the original source.
* It can't replace the human checks: clicking through the report, testing real users and SSO, and the business sign-off.
