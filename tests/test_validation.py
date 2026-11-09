"""End-to-end number validation, including negative (mutation) tests proving the
validator detects real defects."""
import copy

import pandas as pd
import pytest

from tableau2pbi.converter import convert
from tableau2pbi.validation.compare import compare_exports, compare_frames, to_number
from tableau2pbi.validation.runner import validate

from conftest import AS_OF, binding_for


def _by_name(rep):
    return {c.name: c for c in rep.checks}


@pytest.mark.parametrize("name,expected_pass", [("Superstore_Sales.twb", 49), ("Finance_CustomSQL.twb", 27),
                                                ("HR_Analytics.twbx", 29), ("Invoice_Health.twb", 44)])
def test_samples_validate(validations, name, expected_pass):
    s = validations[name].summary
    assert s["FAIL"] == 0 and s["ERROR"] == 0, [(c.name, c.detail) for c in validations[name].checks if c.status in ("FAIL", "ERROR")]
    assert s["PASS"] == expected_pass


def test_every_worksheet_is_validated(validations, workbooks):
    for name, rep in validations.items():
        l3 = [c for c in rep.checks if c.layer == "L3 Visual"]
        assert len(l3) == len(workbooks[name].worksheets)
        assert all(c.status == "PASS" for c in l3), name


def test_known_limitation_reported_as_warning(validations):
    warn = [c for c in validations["Superstore_Sales.twb"].checks if c.status == "WARN"]
    assert {c.name for c in warn} == {"'Discount Analysis' with Discount Threshold = 0.25", "Dashboard actions -> interactions"}
    lim = next(c for c in warn if "KNOWN LIMITATION" in c.detail)
    assert "Discount Band" in lim.detail


def test_sql_ground_truth_and_profiles(validations):
    checks = _by_name(validations["Superstore_Sales.twb"])
    assert checks["SQL ground truth - Orders"].status == "PASS"
    assert checks["Row count - Superstore"].data == {"tableau": 3000, "powerbi": 3000}
    prof = checks["Column profile - Superstore"].data["columns"]
    disc = next(c for c in prof if c["column"] == "Discount")
    assert disc["tableau_nulls"] == disc["powerbi_nulls"] > 0


# ----------------------------------------------------------- negative tests
def _mutated(res, measure, new_expr):
    r = copy.deepcopy(res)
    r.model.measure(measure).expression = new_expr
    return r


def test_detects_wrong_measure(conversions, workbooks):
    res = _mutated(conversions["Superstore_Sales.twb"], "Profit Ratio",
                   "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Quantity]))")
    rep = validate(res, binding_for(workbooks["Superstore_Sales.twb"]), today=AS_OF, scenarios=False)
    c = _by_name(rep)["Worksheet 'Category Profitability'"]
    assert c.status == "FAIL" and c.data["summary"]["mismatched_rows"] == 3


def test_detects_missing_null_guard(conversions, workbooks):
    res = copy.deepcopy(conversions["Superstore_Sales.twb"])
    col = res.model.table("Orders").column("Net Sales")
    col.expression = "('Orders'[Sales] * (1 - 'Orders'[Discount]))"      # naive translation
    rep = validate(res, binding_for(workbooks["Superstore_Sales.twb"]), today=AS_OF, scenarios=False)
    assert _by_name(rep)["Worksheet 'Discount Analysis'"].status == "FAIL"


def test_detects_dropped_filter(conversions, workbooks):
    res = copy.deepcopy(conversions["Superstore_Sales.twb"])
    res.worksheet_visuals["Segment Matrix"].filters = []
    rep = validate(res, binding_for(workbooks["Superstore_Sales.twb"]), today=AS_OF, scenarios=False)
    c = _by_name(rep)["Worksheet 'Segment Matrix'"]
    # Region is not in the view: dropping its filter changes every cell value
    assert c.status == "FAIL" and c.data["summary"]["mismatched_rows"] > 0


def test_detects_wrong_lod(conversions, workbooks):
    res = copy.deepcopy(conversions["Superstore_Sales.twb"])
    col = res.model.table("Orders").column("Region Sales")
    col.expression = "CALCULATE(SUM('Orders'[Sales]))"                   # context transition: row value, not region
    rep = validate(res, binding_for(workbooks["Superstore_Sales.twb"]), today=AS_OF, scenarios=False)
    assert _by_name(rep)["Worksheet 'Region Share'"].status == "FAIL"


def test_detects_join_inflation_with_star_schema(workbooks):
    """Star schema removes Tableau's join duplication -> SUM(sales_target) differs."""
    res = convert(workbooks["Finance_CustomSQL.twb"], "star")
    rep = validate(res, binding_for(workbooks["Finance_CustomSQL.twb"]), today=AS_OF, scenarios=False)
    c = _by_name(rep)["Worksheet 'Attainment by Region'"]
    assert c.status == "FAIL"
    bad = {v["measure"] for r in c.data["rows"] for v in r["values"] if v.get("status") != "MATCH"}
    assert "sum:sales_target:qk" in bad and "sum:sales:qk" not in bad


def test_detects_wrong_relationship_direction(conversions, workbooks):
    res = copy.deepcopy(conversions["Superstore_Sales.twb"])
    res.model.relationships = [r for r in res.model.relationships if r.to_table != "Customers"]
    rep = validate(res, binding_for(workbooks["Superstore_Sales.twb"]), today=AS_OF, scenarios=False)
    assert _by_name(rep)["Worksheet 'Sales by Region'"].status == "FAIL"


# -------------------------------------------------------------- comparator
def test_compare_frames_statuses():
    t = pd.DataFrame({"k": ["a", "b", "c", "d"], "m": [1.0, 2.0, None, 4.0]})
    p = pd.DataFrame({"k": ["a", "b", "c", "e"], "m": [1.004, 2.5, 0.0, 9.0]})
    out = compare_frames(t, p, ["k"], ["m"], abs_tol=0.01)
    st = {tuple(r["key"]): r["status"] for r in out["rows"]}
    assert st[("a",)] == "MATCH" and st[("b",)] == "MISMATCH" and st[("d",)] == "MISSING_IN_POWERBI"
    assert st[("e",)] == "MISSING_IN_TABLEAU" and out["status"] == "FAIL"
    c = next(r for r in out["rows"] if r["key"] == ["c"])
    assert c["values"][0]["status"] == "NULL_VS_ZERO"


@pytest.mark.parametrize("raw,val", [("$1,234.50", 1234.5), ("(12)", -12.0), ("45%", 0.45), ("1.2K", 1200.0),
                                     ("", None), ("-", None), ("abc", "abc"), (3, 3.0)])
def test_number_parsing(raw, val):
    assert to_number(raw) == val


def test_compare_exports_auto_keys(tmp_path):
    t = pd.read_csv("tests/fixtures/tableau_export.csv", dtype=str, keep_default_na=False)
    p = pd.read_csv("tests/fixtures/powerbi_export.csv", dtype=str, keep_default_na=False)
    out = compare_exports(t, p)
    assert out["matched_columns"]["keys"] == ["region", "category"]
    assert out["summary"]["mismatched_rows"] == 1 and out["status"] == "FAIL"
    bad = [r for r in out["rows"] if r["status"] != "MATCH"][0]
    assert bad["key"] == ["West", "Technology"]
