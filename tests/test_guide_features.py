"""Features required by the build guide: secure ingestion, canonical IR, lineage, RLS,
quality gates, KPI register, validation CSV, readiness, overrides, opt-in AI assistance."""
import copy
import csv
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tableau2pbi.ai_assist import AIUnavailable, check_dax, model_context, redact, suggest
from tableau2pbi.converter import apply_override, convert
from tableau2pbi.ir import build_ir, feature_inventory, lineage_csv, validate_ir
from tableau2pbi.parser import UnsafeWorkbookError, read_workbook
from tableau2pbi.pbip.writer import build_files
from tableau2pbi.validation.gates import CSV_COLUMNS, compute_gates, make_signoff, results_csv, verdict
from tableau2pbi.validation.runner import validate
from tableau2pbi.web.app import app

from conftest import AS_OF, binding_for

INV = "Invoice_Health.twb"


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


TWB = b"<?xml version='1.0'?><workbook version='18.1'><datasources/><worksheets/></workbook>"


# ------------------------------------------------------------- ingestion
def test_rejects_path_traversal():
    with pytest.raises(UnsafeWorkbookError, match="path traversal"):
        read_workbook(data=_zip([("Book.twb", TWB), ("../../evil.csv", "x")]), filename="b.twbx")


def test_rejects_zip_bomb():
    with pytest.raises(UnsafeWorkbookError, match="zip bomb"):
        read_workbook(data=_zip([("Book.twb", TWB), ("Data/big.csv", b"0" * (30 * 2**20))]), filename="b.twbx")


def test_rejects_xml_entities():
    xxe = b"<?xml version='1.0'?><!DOCTYPE w [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><workbook>&x;</workbook>"
    with pytest.raises(UnsafeWorkbookError, match="entity"):
        read_workbook(data=xxe, filename="x.twb")


def test_unknown_nodes_preserved_with_xpath():
    twb = (b"<?xml version='1.0'?><workbook version='18.1'><datasources/><worksheets><worksheet name='A'>"
           b"<table><rows/><cols/></table><shiny-new-feature mode='x'/></worksheet></worksheets></workbook>")
    wb = read_workbook(data=twb, filename="u.twb")
    assert wb.unknown_nodes == [{"path": "/workbook/worksheets/worksheet/shiny-new-feature", "tag": "shiny-new-feature",
                                 "attributes": {"mode": "x"}}]
    assert wb.parse_stats["unknown_elements"] == 1 and wb.parse_stats["completeness"] < 100


def test_xml_paths_and_blend_detection(workbooks):
    wb = workbooks[INV]
    ds = wb.datasources[0]
    assert ds.calculations["Calculation_I13"].xml_path.startswith("/workbook/datasources/datasource[2]/column")
    assert wb.worksheets[0].xml_path == "/workbook/worksheets/worksheet[1]"
    assert wb.security_findings[0]["applied_as"] == "data source filter"


# ----------------------------------------------------------- IR / lineage
def test_ir_schema_valid_and_deterministic(conversions):
    for name, res in conversions.items():
        ir = build_ir(res)
        assert validate_ir(ir) == [], name
        assert json.dumps(build_ir(res), sort_keys=True) == json.dumps(ir, sort_keys=True)
    ir = build_ir(conversions[INV])
    otp = next(c for c in ir["calculations"] if c["name"] == "On-time %")
    assert otp["deps"] == ["c_on_time_paid_amount", "c_paid_amount"] and otp["expression_ast"]["node"] == "BinOp"
    assert ir["security"]["requires_identity_review"] is True


def test_ir_schema_rejects_bad_documents(conversions):
    ir = build_ir(conversions[INV])
    ir["calculations"][0]["classification"] = "MAYBE"
    del ir["workbook"]["id"]
    assert len(validate_ir(ir)) == 2


def test_lineage_and_inventory(conversions):
    rows = list(csv.DictReader(io.StringIO(lineage_csv(conversions[INV]))))
    kinds = {r["tableau_type"] for r in rows}
    assert {"column", "measure", "rls-role", "worksheet", "base-measure"} <= kinds
    lod = next(r for r in rows if r["tableau_name"] == "Vendor Fixed Amount")
    assert lod["strategy"] == "rule:fixed-lod-column" and lod["xml_path"].startswith("/workbook/")
    inv = {(r["category"], r["item"]): r for r in feature_inventory(conversions[INV])}
    assert inv[("Security", "User filters / USERNAME()")]["status"] == "review"
    assert inv[("Calculations", "LOD expressions")]["count"] == 1


def test_classification(conversions):
    cls = {m.tableau_name: m.classification for m in conversions[INV].calc_mappings}
    assert cls["Total Invoice Amount"] == "PASS"
    assert cls["Manager Security"] == "REVIEW"          # security always needs live acceptance
    assert cls["Vendor Share of Region"] == "REVIEW"    # TOTAL() note


# ------------------------------------------------------------------- RLS
def test_user_filter_becomes_rls_role(conversions):
    res = conversions[INV]
    assert [r.name for r in res.model.roles] == ["Manager Security"]
    assert res.model.roles[0].table_permissions == {
        "DimEmployeeSecurity": "'DimEmployeeSecurity'[Manager UPN] = USERPRINCIPALNAME()"}
    files = build_files(res)
    assert "ref role 'Manager Security'" in files["Invoice_Health.SemanticModel/definition/model.tmdl"]
    role = files["Invoice_Health.SemanticModel/definition/roles/Manager Security.tmdl"]
    assert "tablePermission DimEmployeeSecurity = 'DimEmployeeSecurity'[Manager UPN] = USERPRINCIPALNAME()" in role
    # not materialised as a (meaningless) calculated column
    assert res.model.table("DimEmployeeSecurity").column("Manager Security") is None


def test_identity_matrix(validations):
    rep = validations[INV]
    ident = [c for c in rep.checks if c.name.startswith("Identity")]
    assert len(ident) == 4 and all(c.status == "PASS" for c in ident)
    forbidden = next(c for c in ident if "forbidden" in c.name)
    assert forbidden.data["columns"][0]["tableau"] == forbidden.data["columns"][0]["powerbi"] == 0
    counts = [c.data["columns"][0]["tableau"] for c in ident if "permitted" in c.name]
    assert sum(counts) == 1500     # the three managers partition all invoices


def test_rls_mutation_detected(conversions, workbooks):
    res = copy.deepcopy(conversions[INV])
    res.model.roles[0].table_permissions["DimEmployeeSecurity"] = "'DimEmployeeSecurity'[Email UPN] = USERPRINCIPALNAME()"
    rep = validate(res, binding_for(workbooks[INV]), today=AS_OF, scenarios=False).to_dict()
    ident = [c for c in rep["checks"] if c["name"].startswith("Identity") and "permitted" in c["name"]]
    assert ident and all(c["status"] == "FAIL" for c in ident)
    assert rep["gates"][3]["status"] == "FAIL"


# ----------------------------------------------------------------- gates
def test_gates_and_pending_human_checks(validations):
    g = {x["gate"]: x for x in validations[INV].gates}
    assert g[1]["status"] == "PASS" and g[2]["status"] == "PASS"
    assert g[3]["status"] == "PENDING" and g[4]["status"] == "PENDING" and g[5]["status"] == "PENDING"
    assert verdict(validations[INV].gates) == "Automated gates passed; human checks pending"
    assert g[4]["pending_human_checks"] == ["Live identity test (real users, groups, SSO)"]


def test_failed_gate_blocks_later_gates(conversions, workbooks):
    res = copy.deepcopy(conversions[INV])
    res.model.measure("Total Invoice Amount").expression = "SUM('FactInvoices'[Invoice Amount]) * 1.01"
    rep = validate(res, binding_for(workbooks[INV]), today=AS_OF, scenarios=False).to_dict()
    st = [g["status"] for g in rep["gates"]]
    assert st[1] == "FAIL" and st[2:] == ["BLOCKED", "BLOCKED", "BLOCKED"]
    assert rep["gates"][2]["blocked_by"] == 2
    assert verdict(rep["gates"]).startswith("Not ready: gate 2")


def test_signoff_drives_gate5(validations):
    rep = validations[INV].to_dict()
    so = make_signoff("Priya Shah", "approved", "KPIs reviewed")
    assert compute_gates(rep, so)[4]["status"] == "PASS"
    assert compute_gates(rep, make_signoff("P", "rejected"))[4]["status"] == "FAIL"
    with pytest.raises(ValueError):
        make_signoff("", "approved")
    with pytest.raises(ValueError):
        make_signoff("P", "maybe")


def test_kpi_register_scenarios(validations):
    reg = validations[INV].kpi_register
    scen = {r["scenario"] for r in reg}
    assert "All data" in scen and "Parameter Status Selector = Late invoices" in scen
    assert any(s.startswith("Region = ") for s in scen) and any(s.startswith("Vendor Category = ") for s in scen)
    assert all(r["status"] == "PASS" for r in reg)
    tot = next(r for r in reg if r["scenario"] == "All data" and r["metric"] == "Total Invoice Amount")
    assert tot["tableau"] == tot["powerbi"] and tot["delta"] == 0


def test_validation_results_csv(validations):
    rows = list(csv.DictReader(io.StringIO(results_csv(validations["Superstore_Sales.twb"].to_dict()))))
    assert list(rows[0].keys()) == CSV_COLUMNS
    lim = [r for r in rows if r["root_cause_category"] == "Known limitation: static calculated column"]
    assert lim and all(r["status"] == "WARN" for r in lim)
    assert any(r["gate"] == "5" and r["status"] == "PENDING" for r in rows)


def test_readiness(validations):
    m = {x["metric"]: x for x in validations[INV].readiness["metrics"]}
    assert m["Parse completeness"]["value"] == 100.0
    assert m["Numeric parity"]["value"] == 100.0
    assert m["Visual openability"]["value"] is None and "NOT RUN" in m["Visual openability"]["detail"]
    assert m["Calculation pass rate"]["value"] >= 90
    assert m["Security acceptance"]["value"] == 100.0


# -------------------------------------------------------------- overrides
def test_override_applies_and_is_traced(conversions):
    res = copy.deepcopy(conversions[INV])
    m = apply_override(res, "On-time %", "DIVIDE([On Time Paid Amount], [Paid Amount], 0)", "zero not blank")
    assert m.overridden and m.original_dax == "DIVIDE([On Time Paid Amount], [Paid Amount])"
    assert m.classification == "REVIEW" and res.model.measure("On-time %").expression.endswith(", 0)")
    with pytest.raises(ValueError, match="unknown column"):
        apply_override(res, "On-time %", "SUM('FactInvoices'[Nope])")
    with pytest.raises(ValueError, match="DAX syntax"):
        apply_override(res, "On-time %", "SUM(")
    with pytest.raises(KeyError):
        apply_override(res, "No Such Measure", "1")


def test_api_override_signoff_history_artifacts():
    c = TestClient(app)
    job = c.post("/api/convert", data={"sample": INV}).json()
    jid = job["job_id"]
    r = c.post(f"/api/jobs/{jid}/override", json={"object": "On-time %", "dax": "DIVIDE([On Time Paid Amount], [Nope])"})
    assert r.status_code == 400 and "unknown measure" in r.json()["detail"]
    assert c.post(f"/api/jobs/{jid}/signoff", json={"approver": "x", "decision": "approved"}).status_code == 400
    v1 = c.post(f"/api/jobs/{jid}/validate", data={"use_sample_data": "true", "as_of": "2025-12-31"}).json()
    assert v1["verdict"] == "Automated gates passed; human checks pending"
    r = c.post(f"/api/jobs/{jid}/override", json={"object": "Total Invoice Amount",
                                                   "dax": "SUM('FactInvoices'[Invoice Amount]) * 2"})
    assert r.status_code == 200
    v2 = c.post(f"/api/jobs/{jid}/validate", data={"use_sample_data": "true", "as_of": "2025-12-31"}).json()
    assert v2["gates"][1]["status"] == "FAIL" and len(v2["history"]) == 2
    so = c.post(f"/api/jobs/{jid}/signoff", json={"approver": "Priya", "decision": "rejected", "notes": "x2 total"}).json()
    assert so["gates"][4]["status"] == "BLOCKED" and so["gates"][4]["status_if_unblocked"] == "FAIL"
    lineage = c.get(f"/api/jobs/{jid}/artifact/lineage.csv").text
    assert "override:manual" in lineage
    manifest = c.get(f"/api/jobs/{jid}/artifact/migration_report.json").json()
    assert [a["event"] for a in manifest["audit"]] == ["converted", "validated", "override", "validated", "signoff"]
    assert c.get(f"/api/jobs/{jid}/artifact/uat_signoff.json").json()["decision"] == "rejected"
    assert c.get(f"/api/jobs/{jid}/artifact/nope.txt").status_code == 404


# -------------------------------------------------------------------- AI
class FakeClient:
    def __init__(self, payload, stop="end_turn"):
        self.payload, self.stop, self.calls = payload, stop, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_ai_suggestion_is_validated_and_review_only(conversions):
    res = conversions[INV]
    m = next(x for x in res.calc_mappings if x.tableau_name == "Manager Security")
    fake = FakeClient({"dax": "'DimEmployeeSecurity'[Manager UPN] = USERPRINCIPALNAME()", "object_type": "measure",
                       "explanation": "UPN comparison", "confidence": "medium", "test_scenarios": ["manager A"]})
    out = suggest(res, m, client=fake)
    assert out["status"] == "REVIEW" and out["parse_ok"] and out["references_ok"]
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    prompt = call["messages"][0]["content"]
    assert "@contoso.com" not in prompt and "findw-sql.contoso.local" not in prompt   # redacted context
    bad = suggest(res, m, client=FakeClient({"dax": "[Ghost] + 1", "object_type": "measure", "explanation": "",
                                             "confidence": "low", "test_scenarios": []}))
    assert bad["references_ok"] is False and "unknown measure [Ghost]" in bad["problems"]
    with pytest.raises(AIUnavailable):
        suggest(res, m, client=FakeClient({}, stop="refusal"))


def test_ai_is_opt_in(monkeypatch, conversions):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(AIUnavailable, match="opt-in"):
        suggest(conversions[INV], conversions[INV].calc_mappings[3])
    c = TestClient(app)
    jid = c.post("/api/convert", data={"sample": INV}).json()["job_id"]
    r = c.post(f"/api/jobs/{jid}/ai-suggest", json={"object": "On-time %"})
    assert r.status_code == 400 and "opt-in" in r.json()["detail"]
    assert c.get("/api/health").json()["ai_available"] is False


def test_redaction_and_dax_check(conversions):
    assert redact("x anna@contoso.com at sql-prod-01.corp.local") == "x <email> at <host>"
    ctx = model_context(conversions[INV])
    assert "FactInvoices" in ctx and "@" not in ctx
    assert check_dax(conversions[INV], "VAR x = 1 RETURN [Total Invoice Amount] + x")["references_ok"]
