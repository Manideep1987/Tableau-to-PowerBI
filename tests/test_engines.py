"""Reference engines: DAX semantics and Tableau semantics on small, hand-checked data."""
from datetime import date, datetime

import pandas as pd
import pytest

from tableau2pbi.model import (CalculatedField, Column, Datasource, FieldRef, Filter, LogicalTable,
                               Relation, Workbook, Worksheet)
from tableau2pbi.validation.dax_engine import ColF, DaxEngine, DaxError, parse_dax
from tableau2pbi.validation.tableau_engine import TableauEngine


# ============================================================== DAX engine
@pytest.fixture()
def eng():
    orders = pd.DataFrame({
        "id": [1, 2, 3, 4, 5, 6],
        "cust": ["A", "A", "B", "B", "C", "C"],
        "sales": [10.0, 20.0, 30.0, 40.0, 50.0, None],
        "disc": [0.1, None, 0.2, 0.0, None, 0.3],
        "d": pd.to_datetime(["2024-01-05", "2024-02-10", "2024-02-11", "2024-03-01", "2024-03-15", "2024-04-01"]),
    })
    cust = pd.DataFrame({"cust": ["A", "B", "C", "D"], "region": ["East", "East", "West", "West"]})
    measures = {"S": "SUM('O'[sales])", "Cnt": "CALCULATE(DISTINCTCOUNTNOBLANK('C'[cust]), 'O')",
                "Rank": "RANKX(ALLSELECTED('C'[region]), CALCULATE(SUM('O'[sales])), , DESC, SKIP)",
                "Run": "CALCULATE(SUM('O'[sales]), WINDOW(1, ABS, 0, REL, ALLSELECTED('C'[region]), ORDERBY('C'[region])))",
                "Prev": "CALCULATE(SUM('O'[sales]), OFFSET(-1, ALLSELECTED('C'[region]), ORDERBY('C'[region])))",
                "Share": "DIVIDE([S], CALCULATE([S], ALLSELECTED('C'[region])))",
                "AllS": "CALCULATE([S], ALL('O'))",
                "KeepRegion": "CALCULATE([S], ALLEXCEPT('O', 'C'[region]))"}
    calc = {"O": [("Fixed Region", "(VAR __k = RELATED('C'[region]) RETURN CALCULATE(SUM('O'[sales]), REMOVEFILTERS(), 'C'[region] = __k))"),
                  ("Net", "('O'[sales] * IF(ISBLANK('O'[disc]), BLANK(), (1 - 'O'[disc])))"),
                  ("Net Unsafe", "('O'[sales] * (1 - 'O'[disc]))")]}
    return DaxEngine({"O": orders, "C": cust}, [("O", "cust", "C", "cust")], measures, calc, today=date(2024, 12, 31))


def ev(e, expr, filters=()):
    return e.evaluate_scalar(expr, tuple(filters))


def test_blank_arithmetic_rules(eng):
    assert ev(eng, "BLANK() + 1") == 1          # BLANK + 1 = 1
    assert ev(eng, "BLANK() * 5") is None        # BLANK * 5 = BLANK
    assert ev(eng, "DIVIDE(1, 0)") is None
    assert ev(eng, "DIVIDE(1, 0, -1)") == -1
    assert ev(eng, "BLANK() = 0") is True        # BLANK equals 0 in DAX
    assert ev(eng, "\"abc\" = \"ABC\"") is True   # case-insensitive comparison
    assert ev(eng, "\"a\" & BLANK()") == "a"
    assert ev(eng, "COALESCE(BLANK(), 3)") == 3


def test_scalar_functions(eng):
    assert ev(eng, "DATE(2024, 14, 1)") == datetime(2025, 2, 1)
    assert ev(eng, "EDATE(DATE(2024, 1, 31), 1)") == datetime(2024, 2, 29)
    assert ev(eng, "DATEDIFF(DATE(2024, 1, 1), DATE(2024, 3, 1), MONTH)") == 2
    assert ev(eng, "WEEKDAY(DATE(2024, 6, 2), 1)") == 1          # Sunday
    assert ev(eng, "TRUNC(-2.7)") == -2 and ev(eng, "INT(-2.7)") == -3
    assert ev(eng, "ROUND(2.5, 0)") == 3 and ev(eng, "ROUND(-2.5, 0)") == -3
    assert ev(eng, "SWITCH(TRUE(), 1 > 2, \"a\", 2 > 1, \"b\", \"c\")") == "b"
    assert ev(eng, "PATHITEM(SUBSTITUTE(\"a b c\", \" \", \"|\"), 2)") == "b"
    assert ev(eng, "FORMAT(DATE(2024, 3, 9), \"MMMM\")") == "March"
    assert ev(eng, "\"x\" IN {\"x\", \"y\"}") is True


def test_aggregation_and_filter_propagation(eng):
    assert ev(eng, "SUM('O'[sales])") == 150
    east = [ColF("C[region]", frozenset(["East"]))]
    assert ev(eng, "SUM('O'[sales])", east) == 100                      # one-side filter reaches the fact
    only_a = [ColF("O[cust]", frozenset(["A"]))]
    assert ev(eng, "DISTINCTCOUNTNOBLANK('C'[cust])", only_a) == 4    # fact filter does NOT reach the dimension
    assert ev(eng, "[Cnt]", only_a) == 1                               # ... unless filtered through the fact
    assert ev(eng, "COUNTROWS('O')", east) == 4
    assert ev(eng, "AVERAGE('O'[disc])") == pytest.approx(0.15)        # blanks ignored
    assert ev(eng, "DISTINCTCOUNT('O'[disc])") == 5                    # 4 values + BLANK
    assert ev(eng, "DISTINCTCOUNTNOBLANK('O'[disc])") == 4


def test_calculate_modifiers(eng):
    f = [ColF("C[region]", frozenset(["East"])), ColF("O[cust]", frozenset(["A"]))]
    assert ev(eng, "[S]", f) == 30
    assert ev(eng, "[AllS]", f) == 150
    assert ev(eng, "[KeepRegion]", f) == 100
    assert ev(eng, "CALCULATE([S], REMOVEFILTERS('O'[cust]))", f) == 100
    assert ev(eng, "CALCULATE([S], 'C'[region] = \"West\")", f) == 0 or ev(eng, "CALCULATE([S], 'C'[region] = \"West\")", f) is None


def test_calculated_columns_and_row_context(eng):
    o = eng.base["O"]
    assert list(o["O[Fixed Region]"]) == [100, 100, 100, 100, 50, 50]
    assert o["O[Net]"].tolist()[1] is None          # guarded: NULL discount -> BLANK
    assert o["O[Net Unsafe]"].tolist()[1] == 20     # unguarded: BLANK treated as 0
    assert ev(eng, "SUMX('O', 'O'[sales] * 2)") == 300
    assert ev(eng, "SUMX(FILTER('O', 'O'[sales] > 25), 'O'[sales])") == 120
    assert ev(eng, "MAXX(VALUES('C'[region]), CALCULATE(SUM('O'[sales])))") == 100


def test_visual_query_with_window_functions(eng):
    df = eng.query([("C", "region")], ["S", "Rank", "Run", "Prev", "Share"])
    rows = {r[0]: tuple(None if (isinstance(x, float) and x != x) else x for x in r[1:])
            for r in df.itertuples(index=False, name=None)}
    assert rows["East"] == (100, 1, 100, None, pytest.approx(2 / 3))
    assert rows["West"] == (50, 2, 150, 100, pytest.approx(1 / 3))
    df2 = eng.query([("C", "region")], ["S"], value_filters=[("S", 60, None, False)])
    assert list(df2["C[region]"]) == ["East"]
    df3 = eng.query([("C", "region")], ["S", "Share"], filters=[ColF("O[cust]", frozenset(["A", "C"]))])
    assert dict(zip(df3["C[region]"], df3["Share"])) == {"East": pytest.approx(30 / 80), "West": pytest.approx(50 / 80)}


def test_dax_parse_errors():
    with pytest.raises(DaxError):
        parse_dax("SUM('O'[sales]")
    with pytest.raises(DaxError):
        parse_dax("VAR x = 1")


# ========================================================== Tableau engine
def _ds():
    ds = Datasource("ds", "DS")
    ds.tables = [LogicalTable("T", Relation("T", "table", table="[T]"))]
    for n, dt, role in (("Region", "string", "dimension"), ("Cat", "string", "dimension"),
                        ("Sales", "real", "measure"), ("Disc", "real", "measure"), ("D", "date", "dimension")):
        ds.columns[n] = Column(n, "T", n, dt, role)
    calcs = {
        "C1": ("Net", "[Sales] * (1 - [Disc])"),
        "C2": ("RegionTotal", "{FIXED [Region] : SUM([Sales])}"),
        "C3": ("Ratio", "SUM([Sales]) / TOTAL(SUM([Sales]))"),
        "C4": ("Run", "RUNNING_SUM(SUM([Sales]))"),
        "C5": ("Rk", "RANK(SUM([Sales]))"),
        "C6": ("Inc", "{INCLUDE [Cat] : SUM([Sales])}"),
        "C7": ("Flag", "[Disc] > 0.1"),
    }
    for k, (cap, f) in calcs.items():
        ds.calculations[k] = CalculatedField(k, cap, f)
    return ds


@pytest.fixture()
def teng():
    df = pd.DataFrame({"Region": ["E", "E", "E", "W", "W"], "Cat": ["a", "b", "b", "a", "a"],
                       "Sales": [10.0, 20.0, 30.0, 5.0, 15.0], "Disc": [0.0, None, 0.5, 0.2, 0.0],
                       "D": pd.to_datetime(["2024-01-01"] * 5)})
    wb = Workbook("t")
    ds = _ds()
    wb.datasources = [ds]
    return TableauEngine(wb, ds, df, today=date(2024, 12, 31)), ds


def ref(f, d="none", t="nk"):
    return FieldRef("ds", f, d, t)


def test_tableau_null_propagation(teng):
    e, _ = teng
    assert e.field_vector("C1") == [10.0, None, 15.0, 4.0, 15.0]
    assert e.field_vector("C7") == [False, None, True, True, False]


def test_tableau_fixed_is_record_level(teng):
    e, _ = teng
    assert e.field_vector("C2") == [60.0, 60.0, 60.0, 20.0, 20.0]
    ws = Worksheet("w", ["ds"], rows=[ref("Region")], cols=[ref("C2", "sum", "qk")])
    out = e.run_worksheet(ws)
    assert dict(zip(out["none:Region:nk"], out["sum:C2:qk"])) == {"E": 180.0, "W": 40.0}   # replicated per record


def test_tableau_table_calcs_and_include(teng):
    e, _ = teng
    ws = Worksheet("w", ["ds"], rows=[ref("Region")],
                   cols=[ref("Sales", "sum", "qk"), ref("C3", "usr", "qk"), ref("C4", "usr", "qk"),
                         ref("C5", "usr", "qk"), ref("C6", "avg", "qk")])
    out = e.run_worksheet(ws).set_index("none:Region:nk")
    assert out.loc["E", "sum:Sales:qk"] == 60 and out.loc["W", "sum:Sales:qk"] == 20
    assert out.loc["E", "usr:C3:qk"] == pytest.approx(0.75)
    assert list(out["usr:C4:qk"]) == [60, 80]
    assert list(out["usr:C5:qk"]) == [1, 2]
    assert out.loc["E", "avg:C6:qk"] == 30 and out.loc["W", "avg:C6:qk"] == 20


def test_tableau_filter_order_top_n_before_dimension_filter(teng):
    e, _ = teng
    top = Filter("top", "ds", ref("Region"), top_n=1, top_by="SUM([Sales])")
    dim = Filter("categorical", "ds", ref("Cat"), members=["a"])
    ws = Worksheet("w", ["ds"], rows=[ref("Region")], cols=[ref("Sales", "sum", "qk")], filters=[top, dim])
    out = e.run_worksheet(ws)
    # Top 1 region by total sales is E (computed before the Cat filter) -> E with Cat=a only
    assert out.values.tolist() == [["E", 10.0]]


def test_tableau_measure_filter_and_exclude(teng):
    e, _ = teng
    f = Filter("quantitative", "ds", ref("Sales", "sum", "qk"), min=25)
    ws = Worksheet("w", ["ds"], rows=[ref("Region")], cols=[ref("Sales", "sum", "qk")], filters=[f])
    assert e.run_worksheet(ws).values.tolist() == [["E", 60.0]]
    ex = Filter("categorical", "ds", ref("Cat"), members=["b"], exclude=True)
    ws = Worksheet("w", ["ds"], rows=[ref("Region")], cols=[ref("Sales", "sum", "qk")], filters=[ex])
    assert e.run_worksheet(ws).values.tolist() == [["E", 10.0], ["W", 20.0]]
