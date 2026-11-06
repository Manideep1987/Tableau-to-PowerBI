"""Web API (FastAPI TestClient) and live-validation clients against a local stub server."""
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tableau2pbi.validation.live import PowerBIClient, TableauServerClient, live_validate, unpivot_measure_values
from tableau2pbi.web.app import app

client = TestClient(app)


@pytest.fixture(scope="module")
def job():
    r = client.post("/api/convert", data={"sample": "Superstore_Sales.twb", "join_strategy": "flatten"})
    assert r.status_code == 200
    return r.json()


def test_health_and_samples():
    assert client.get("/api/health").json()["status"] == "ok"
    names = [s["name"] for s in client.get("/api/samples").json()]
    assert names == ["Finance_CustomSQL.twb", "HR_Analytics.twbx", "Superstore_Sales.twb"]
    assert "Tableau" in client.get("/").text


def test_convert_payload(job):
    assert job["stats"]["worksheets"] == 16 and job["stats"]["manual"] == 0
    assert len(job["calculations"]) >= 40 and job["pages"] and job["model"]["relationships"]


def test_upload_and_errors():
    with open("samples/workbooks/HR_Analytics.twbx", "rb") as f:
        r = client.post("/api/convert", files={"file": ("HR_Analytics.twbx", f.read())})
    assert r.status_code == 200 and r.json()["workbook"]["packaged_files"] == ["Data/hr/employees.csv"]
    assert client.post("/api/convert", files={"file": ("x.pdf", b"%PDF")}).status_code == 400
    assert client.post("/api/convert", files={"file": ("x.twbx", b"not a zip")}).status_code == 400
    assert client.post("/api/convert", data={}).status_code == 400
    assert client.post("/api/convert", data={"sample": "../../etc/passwd"}).status_code == 404
    assert client.get("/api/jobs/doesnotexist").status_code == 404


def test_downloads(job):
    z = client.get(f"/api/jobs/{job['job_id']}/pbip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "Superstore_Sales.pbip" in names and "MIGRATION_REPORT.html" in names and "DAX_MAPPING.csv" in names
    assert any(n.endswith("visual.json") for n in names)
    assert "Calculations" in client.get(f"/api/jobs/{job['job_id']}/report").text
    assert client.get(f"/api/jobs/{job['job_id']}/mapping.csv").text.startswith("Data source,Tableau field")


def test_validate_endpoint(job):
    assert client.get(f"/api/jobs/{job['job_id']}/validation/report").status_code == 404
    r = client.post(f"/api/jobs/{job['job_id']}/validate", data={"use_sample_data": "true", "as_of": "2025-12-31"})
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["FAIL"] == 0 and s["PASS"] >= 30
    assert "Validation report" in client.get(f"/api/jobs/{job['job_id']}/validation/report").text
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job['job_id']}/pbip").content)).namelist()
    assert "VALIDATION_REPORT.html" in names
    assert client.post(f"/api/jobs/{job['job_id']}/validate", data={"use_sample_data": "true", "as_of": "bad"}).status_code == 400


def test_translate_and_catalog(job):
    r = client.post("/api/translate", json={"formula": "COUNTD([Order ID])"}).json()
    assert r["ok"] and r["dax"] == "DISTINCTCOUNTNOBLANK('Orders'[Order ID])"
    r = client.post("/api/translate", json={"formula": "SUM([Profit]) / SUM([Sales])", "job_id": job["job_id"]}).json()
    assert r["dax"] == "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))" and "Superstore" in r["schema"]
    assert client.post("/api/translate", json={"formula": "SUM("}).json()["ok"] is False
    assert client.post("/api/translate", json={"formula": "[Nope]"}).json()["ok"] is False
    assert client.post("/api/translate", json={"formula": " "}).status_code == 400
    assert len(client.get("/api/catalog").json()["functions"]) >= 70


def test_compare_endpoint():
    with open("tests/fixtures/tableau_export.csv", "rb") as a, open("tests/fixtures/powerbi_export.csv", "rb") as b:
        r = client.post("/api/compare", files={"tableau": ("t.csv", a.read()), "powerbi": ("p.csv", b.read())})
    assert r.status_code == 200 and r.json()["status"] == "FAIL" and r.json()["summary"]["matched_rows"] == 4


# ------------------------------------------------------------- live stubs
def test_unpivot_measure_values():
    df = pd.DataFrame({"Region": ["E", "E"], "Measure Names": ["Sales", "Profit"], "Measure Values": ["1", "2"]})
    w = unpivot_measure_values(df)
    assert list(w.columns) == ["Region", "Profit", "Sales"] and w.iloc[0]["Sales"] == "1"


def test_live_validation_against_stub(conversions, workbooks):
    """Tableau REST + Power BI executeQueries stubs serve results computed by the engines."""
    from conftest import AS_OF, binding_for
    from tableau2pbi.validation.live import tableau_caption
    from tableau2pbi.validation.runner import Validator

    res = conversions["HR_Analytics.twbx"]
    val = Validator(res, binding_for(workbooks["HR_Analytics.twbx"]), AS_OF, scenarios=False)
    val.run()
    ws = res.workbook.worksheet("Headcount by Department")
    eng = list(val.tab_engines.values())[0]
    tab = eng.run_worksheet(ws)
    refs = {r.key: r for r in ws.rows + ws.cols + [x for v in ws.encodings.values() for x in v]}
    csv = tab.rename(columns={k: tableau_caption(res, refs[k]) for k in tab.columns}).to_csv(index=False).encode()
    dims = [(res.field_map[(ws.name, k)]) for k in tab.columns if isinstance(res.field_map.get((ws.name, k)), tuple)]
    meas = [res.field_map[(ws.name, k)] for k in tab.columns if isinstance(res.field_map.get((ws.name, k)), str)]
    pbi = val.dax.query(dims, meas)
    rows = [{**{f"{t}[{c}]": r[f"{t}[{c}]"] for t, c in dims}, **{f"[{m}]": r[m] for m in meas}}
            for r in pbi.to_dict("records")]
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            calls.append(self.path)
            if self.path.endswith("/auth/signin"):
                assert body["credentials"]["personalAccessTokenName"] == "pat"
                return self._send(json.dumps({"credentials": {"token": "T", "site": {"id": "S1"}}}).encode())
            if self.path.endswith("/executeQueries"):
                assert "SUMMARIZECOLUMNS" in body["queries"][0]["query"]
                assert self.headers["Authorization"] == "Bearer AAD"
                return self._send(json.dumps({"results": [{"tables": [{"rows": rows}]}]}, default=str).encode())
            self._send(b"{}")

        def do_GET(self):
            calls.append(self.path)
            assert self.headers["X-Tableau-Auth"] == "T"
            if "/views?" in self.path:
                return self._send(json.dumps({"views": {"view": [{"id": "V1", "name": ws.name,
                                                                  "workbook": {"name": res.workbook.name}}]}}).encode())
            if self.path.endswith("/views/V1/data"):
                return self._send(csv, "text/csv")
            self.send_response(404)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        tc = TableauServerClient(base, "site", "pat", "secret").sign_in()
        pc = PowerBIClient("AAD", base_url=base + "/v1.0/myorg")
        saved = list(res.workbook.worksheets)
        res.workbook.worksheets = [ws]
        try:
            out = live_validate(res, tc, pc, "DS1", views={ws.name: "V1"})
        finally:
            res.workbook.worksheets = saved
    finally:
        srv.shutdown()
    assert out[0]["status"] == "PASS", out[0]
    assert any(c.endswith("/auth/signin") for c in calls) and any(c.endswith("/executeQueries") for c in calls)
