"""Robustness: differential fuzzing, hostile / malformed inputs, unusual names, empty data."""
import io
import os
import random
import sys
import zipfile

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fuzz"))
from differential_fuzz import run  # noqa: E402

from tableau2pbi.converter import convert  # noqa: E402
from tableau2pbi.parser import read_workbook  # noqa: E402
from tableau2pbi.pbip.writer import build_files  # noqa: E402
from tableau2pbi.validation.dax_engine import parse_dax  # noqa: E402
from tableau2pbi.validation.runner import DataBinding, validate  # noqa: E402
from tableau2pbi.web.app import app  # noqa: E402

from conftest import AS_OF  # noqa: E402


# ------------------------------------------------------------ fuzzing
@pytest.mark.parametrize("seed", [101, 202])
def test_differential_fuzz(seed):
    """Random calculations + worksheets: Tableau engine == DAX engine for every case."""
    r = run(cases_total=50, seed=seed, batch=25, verbose=False)
    bad = [(c["workbook"], c["formula"], c["detail"]) for c in r["fail"] + r["error"]]
    assert not bad, bad[:5]
    assert not r["unsupported"], [(c["formula"], c["reason"]) for c in r["unsupported"]][:5]
    assert r["pass"] == 50


# ------------------------------------------------------- hostile input
def test_random_bytes_never_crash_the_api():
    c = TestClient(app)
    rnd = random.Random(7)
    for i in range(25):
        blob = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 4000)))
        name = rnd.choice(["x.twb", "x.twbx"])
        r = c.post("/api/convert", files={"file": (name, blob)})
        assert r.status_code in (200, 400), (i, r.status_code, r.text[:200])


def test_truncated_workbooks_recover_or_fail_cleanly():
    data = open("samples/workbooks/Superstore_Sales.twb", "rb").read()
    for cut in (200, 5000, len(data) // 2, len(data) - 50):
        try:
            wb = read_workbook(data=data[:cut], filename="t.twb")
        except ValueError:
            continue
        res = convert(wb)                   # whatever was recovered must still convert
        for t in res.model.tables:
            for m in t.measures:
                parse_dax(m.expression)


# ------------------------------------------------- unusual identifiers
ODD = "<?xml version='1.0'?>\n<workbook version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'><datasources>" \
      "<datasource caption=\"Vendas 'São Paulo'\" inline='true' name='federated.odd'><connection class='federated'>" \
      "<named-connections><named-connection name='sqlserver.o'><connection class='sqlserver' server='s' dbname='d'/>" \
      "</named-connection></named-connections><relation connection='sqlserver.o' name='Odd' table='[dbo].[Odd]' type='table'/>" \
      "<metadata-records>" \
      "<metadata-record class='column'><remote-name>r</remote-name><local-name>[Région [EU]]]</local-name><parent-name>[Odd]</parent-name><local-type>string</local-type><aggregation>Count</aggregation><contains-null>false</contains-null></metadata-record>" \
      "<metadata-record class='column'><remote-name>v</remote-name><local-name>[Ventes 'net' €]</local-name><parent-name>[Odd]</parent-name><local-type>real</local-type><aggregation>Sum</aggregation><contains-null>true</contains-null></metadata-record>" \
      "</metadata-records></connection>" \
      "<column caption='Marge &quot;brute&quot;' datatype='real' name='[Calculation_X]' role='measure' type='quantitative'><calculation class='tableau' formula=\"SUM([Ventes 'net' €]) * 2\"/></column>" \
      "<column caption='Étiquette' datatype='string' name='[Calculation_Y]' role='dimension' type='nominal'><calculation class='tableau' formula=\"[Région [EU]]] + ' – ' + 'l''été'\"/></column>" \
      "</datasource></datasources><worksheets><worksheet name=\"Feuille 'été' / 1\"><table><view>" \
      "<datasources><datasource name='federated.odd'/></datasources></view><panes><pane><mark class='Text'/><encodings>" \
      "<text column='[federated.odd].[usr:Calculation_X:qk]'/></encodings></pane></panes>" \
      "<rows>[federated.odd].[none:Calculation_Y:nk]</rows><cols></cols></table></worksheet></worksheets></workbook>"


def test_quotes_brackets_unicode_in_names():
    wb = read_workbook(data=ODD.encode(), filename="odd.twb")
    ds = wb.datasources[0]
    assert "Région [EU]" in ds.columns and "Ventes 'net' €" in ds.columns
    res = convert(wb)
    assert res.stats["manual"] == 0
    files = build_files(res)
    tmdl = "\n".join(v for k, v in files.items() if k.endswith(".tmdl"))
    assert "'Région [EU]'" in tmdl or "Région [EU]" in tmdl
    for t in res.model.tables:
        for m in t.measures:
            parse_dax(m.expression)
        for c in t.columns:
            if c.expression:
                parse_dax(c.expression)
    data = pd.DataFrame({"r": ["Nord", "Sud", None], "v": [10.0, None, 5.0]})
    binding = DataBinding(frames={"Odd": data})
    # metadata says [Région [EU]] is NOT NULL, the data disagrees: gate 1 and the visual both catch it
    rep = validate(res, binding, today=AS_OF, scenarios=False)
    contract = next(c for c in rep.checks if c.name.startswith("Nullability contract"))
    assert contract.status == "FAIL" and "Région [EU]" in contract.detail
    assert rep.gates[0]["status"] == "FAIL"
    # data profiling before conversion fixes it
    from tableau2pbi.validation.runner import apply_nullability, nullability_findings
    assert apply_nullability(wb, nullability_findings(wb, binding)) == 1
    rep = validate(convert(wb), binding, today=AS_OF, scenarios=False)
    l3 = [c for c in rep.checks if c.layer == "L3 Visual"]
    assert l3 and all(c.status == "PASS" for c in l3), [(c.name, c.detail) for c in l3]
    assert rep.gates[0]["status"] == "PASS"


def test_empty_source_tables():
    wb = read_workbook("samples/workbooks/HR_Analytics.twbx")
    res = convert(wb)
    header = wb.packaged_files["Data/hr/employees.csv"].split(b"\n")[0] + b"\n"
    wb.packaged_files = {"Data/hr/employees.csv": header}
    rep = validate(res, DataBinding(packaged=wb.packaged_files), today=AS_OF, scenarios=False)
    assert not [c for c in rep.checks if c.status == "ERROR"], [(c.name, c.detail) for c in rep.checks if c.status == "ERROR"]
    assert all(c.status in ("PASS", "SKIP", "N/A", "PENDING") for c in rep.checks if c.layer == "L3 Visual")


def test_all_null_measure_column():
    wb = read_workbook("samples/workbooks/HR_Analytics.twbx")
    res = convert(wb)
    df = pd.read_csv(io.BytesIO(wb.packaged_files["Data/hr/employees.csv"]), dtype=str, keep_default_na=False)
    df["performance_score"] = ""
    wb.packaged_files = {"Data/hr/employees.csv": df.to_csv(index=False).encode()}
    rep = validate(res, DataBinding(packaged=wb.packaged_files), today=AS_OF, scenarios=False)
    bad = [(c.name, c.detail) for c in rep.checks if c.layer == "L3 Visual" and c.status not in ("PASS",)]
    assert not bad, bad


def test_large_workbook_package_limits():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Book.twb", open("samples/workbooks/Superstore_Sales.twb", "rb").read())
        for i in range(300):
            z.writestr(f"Data/f{i}.csv", "a,b\n1,2\n")
    wb = read_workbook(data=buf.getvalue(), filename="big.twbx")
    assert len(wb.packaged_files) == 300 and len(wb.worksheets) == 16
