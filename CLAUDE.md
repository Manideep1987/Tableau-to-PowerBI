# CLAUDE.md: working on the Tableau → Power BI Migrator

## What this is
A local, deterministic Tableau (.twb/.twbx) → Power BI (PBIP: TMDL + PBIR) converter with independent number
validation. The architecture follows `docs/` and the build guide: parse → canonical IR → rule-based mappings →
optional AI suggestions (reviewed) → Power BI builders → quality gates.

| Engine | Code |
|---|---|
| UI | `tableau2pbi/web/static/` (dependency-free HTML/JS/CSS) |
| API | `tableau2pbi/web/app.py` (FastAPI, in-memory jobs, audit trail) |
| Conversion | `parser.py` (secure TWB/TWBX reader) → `model.py` → `ir.py` (IR v0.1.0 + schema) → `converter.py` |
| Rules | `formula/parser.py` (Tableau AST) → `formula/dax.py` (DAX rules), `mcode.py` (Power Query) |
| AI assistant | `ai_assist.py` (opt-in, redacted, Claude via the Anthropic SDK, output always REVIEW) |
| Power BI builders | `pbip/writer.py` (TMDL tables/relationships/roles, PBIR pages/visuals/filters) |
| Quality gates | `validation/` (Tableau engine, DAX engine, comparator, gates, KPI register, identity matrix, live clients) |

## Rules
- Never invent source metadata. Everything comes from the XML; unknown nodes are preserved in
  `Workbook.unknown_nodes` with their XPath and flagged. Never call an LLM for XML parsing.
- Deterministic rules first. Every translation records `strategy`, `confidence` and a PASS / REVIEW / UNSUPPORTED
  classification. AI output is a candidate only. It must parse (`validation/dax_engine.parse_dax`), reference
  existing objects (`ai_assist.check_dax`) and be applied as an override by a reviewer.
- "Valid DAX" is not "same semantics". Every rule change needs a numeric test that runs both engines
  (`tests/test_validation.py`, `tests/test_guide_features.py`). Keep the Tableau engine and the DAX engine
  independent. Never share evaluation code between them.
- Never mark validation complete without running it. Human checks (click paths, real identities/SSO, business
  sign-off) stay `PENDING`. A failed gate blocks later gates.
- Security: keep the parser limits (`parser.LIMITS`, entity rejection), never export credentials, redact
  identifiers before any AI call, and treat RLS as a hard gate. No publish/delete operations against Tableau or
  Power BI. Live clients are read-only (`validation/live.py`).
- Outputs stay editable and open: PBIP, `migration_report.json`, `lineage.csv`, `ir.json`, `validation_results.csv`.

## Commands
```bash
pip install -r requirements.txt
python samples/generate_samples.py              # deterministic sample data + 4 workbooks
python -m pytest -q                             # must pass before every commit
python -m tableau2pbi serve --port 8000         # UI
python -m tableau2pbi convert samples/workbooks/Invoice_Health.twb -o out/inv --sqlite samples/data/superstore.db --as-of 2025-12-31
python docs/build_test_report.py                # full campaign: pytest + Playwright UI + UAT + defect injection -> docs/
```

## Conventions
- Python 3.11+, standard library + lxml/pandas/numpy/fastapi. No new runtime dependency without a reason.
- DAX: `'Table'[Column]`, `[Measure]`, `DIVIDE` for division, `DISTINCTCOUNTNOBLANK` for COUNTD, explicit measures
  in `_Measures`, parameters as tables + `<name> Value` measures, roles with `USERPRINCIPALNAME()`.
- New Tableau construct: add the parser support, the rule in `formula/dax.py` or `converter.py`, a catalogue entry in
  `catalog.py`, a sample usage in `samples/`, and a validation test that proves the numbers.
- Small, incremental commits. Update `docs/TEST_REPORT.md` with `python docs/build_test_report.py`.
