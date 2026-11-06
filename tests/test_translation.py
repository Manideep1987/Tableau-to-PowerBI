"""Tableau formula parser and DAX translator."""
import pytest

from tableau2pbi.catalog import DemoResolver, catalog
from tableau2pbi.formula.ast import LOD, BinOp, Case, Field, Func, If, In, Num
from tableau2pbi.formula.dax import DaxTranslator, TranslationError
from tableau2pbi.formula.parser import FormulaError, parse_formula


# ------------------------------------------------------------------ parser
def test_parse_lod():
    n = parse_formula("{ FIXED [Region], [Category] : SUM([Sales]) }")
    assert isinstance(n, LOD) and n.kind == "FIXED" and [d.name for d in n.dims] == ["Region", "Category"]
    assert parse_formula("{SUM([Sales])}").dims == []


def test_parse_if_case_in_and_comments():
    n = parse_formula("// header\nIF [a] > 1 THEN 'x' /* c */ ELSEIF [a] = 1 THEN 'y' ELSE 'z' END")
    assert isinstance(n, If) and len(n.branches) == 2
    assert isinstance(parse_formula("CASE [r] WHEN 'E' THEN 1 END"), Case)
    assert isinstance(parse_formula("[r] IN ('a', 'b')"), In)
    assert parse_formula("ELSE_IF_DUMMY(1)").name == "ELSE_IF_DUMMY"


def test_parse_precedence_and_params():
    n = parse_formula("1 + 2 * 3 ^ 2")
    assert isinstance(n, BinOp) and n.op == "+" and n.right.op == "*" and n.right.right.op == "^"
    p = parse_formula("[Parameters].[Growth Rate]")
    assert isinstance(p, Field) and p.is_parameter and p.name == "Growth Rate"
    assert parse_formula("[Field with ]] bracket]").name == "Field with ] bracket"
    assert parse_formula("-5").value == -5
    assert parse_formula("[a] == [b]").op == "="
    assert parse_formula("[a] != [b]").op == "<>"


@pytest.mark.parametrize("bad", ["SUM([Sales]", "IF [a] THEN 1", "[open", "'unterminated", "1 +", "{FIXED [a] SUM([b])}"])
def test_parse_errors(bad):
    with pytest.raises(FormulaError):
        parse_formula(bad)


# -------------------------------------------------------------- translator
R = DemoResolver()


def dax(formula, mode=None, addressing=None):
    ast = parse_formula(formula)
    from tableau2pbi.formula.ast import is_aggregate
    mode = mode or ("agg" if is_aggregate(ast, R.calc) else "row")
    return DaxTranslator(R).translate(ast, mode, host="Orders", addressing=addressing)


CASES = [
    ("SUM([Sales])", "SUM('Orders'[Sales])"),
    ("AVG([Discount])", "AVERAGE('Orders'[Discount])"),
    ("COUNTD([Order ID])", "DISTINCTCOUNTNOBLANK('Orders'[Order ID])"),
    ("COUNT([Order ID])", "COUNTA('Orders'[Order ID])"),
    ("COUNT([Quantity])", "COUNT('Orders'[Quantity])"),
    ("COUNTD([Customer Name])", "CALCULATE(DISTINCTCOUNTNOBLANK('Customers'[Customer Name]), 'Orders')"),
    ("SUM([Profit]) / SUM([Sales])", "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"),
    ("MEDIAN([Sales])", "MEDIAN('Orders'[Sales])"),
    ("ATTR([Region])", "SELECTEDVALUE('Customers'[Region], \"*\")"),
    ("SUM([Sales] * [Quantity])", "SUMX('Orders', ('Orders'[Sales] * 'Orders'[Quantity]))"),
    ("IIF([Sales] > 100, 'Big', 'Small')", "IF(('Orders'[Sales] > 100), \"Big\", \"Small\")"),
    ("ZN([Discount])", "COALESCE('Orders'[Discount], 0)"),
    ("IFNULL([Discount], 0.5)", "COALESCE('Orders'[Discount], 0.5)"),
    ("ISNULL([Discount])", "ISBLANK('Orders'[Discount])"),
    ("[Region] IN ('East', 'West')", "RELATED('Customers'[Region]) IN {\"East\", \"West\"}"),
    ("UPPER([Region]) + '-' + [Order ID]", "UPPER(RELATED('Customers'[Region])) & \"-\" & 'Orders'[Order ID]"),
    ("INT([Profit])", "TRUNC('Orders'[Profit])"),
    ("[Quantity] % 3", "MOD('Orders'[Quantity], 3)"),
    ("DATEPART('month', [Order Date])", "MONTH('Orders'[Order Date])"),
    ("DATETRUNC('year', [Order Date])", "DATE(YEAR('Orders'[Order Date]), 1, 1)"),
    ("DATEADD('month', 2, [Order Date])", "EDATE('Orders'[Order Date], 2)"),
    ("DATEDIFF('day', [Order Date], [Ship Date])", "DATEDIFF('Orders'[Order Date], 'Orders'[Ship Date], DAY)"),
    ("[Order Date] >= #2024-03-01#", "('Orders'[Order Date] >= DATE(2024, 3, 1))"),
    ("FIND([Region], 'a')", "FIND(\"a\", RELATED('Customers'[Region]), 1, 0)"),
    ("REPLACE([Region], 'E', 'e')", "SUBSTITUTE(RELATED('Customers'[Region]), \"E\", \"e\")"),
    ("MID([Order ID], 2)", "MID('Orders'[Order ID], 2, LEN('Orders'[Order ID]))"),
    ("{FIXED : SUM([Sales])}", "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS())"),
    ("{FIXED [Region] : SUM([Sales])}",
     "(VAR __lod1 = RELATED('Customers'[Region]) RETURN CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS(), 'Customers'[Region] = __lod1))"),
    ("SUM([Sales]) * [Parameters].[Target]", "(SUM('Orders'[Sales]) * [Target Value])"),
    ("USERNAME()", "USERPRINCIPALNAME()"),
]


@pytest.mark.parametrize("formula,expected", CASES)
def test_translation(formula, expected):
    assert dax(formula).expression == expected


def test_null_guards():
    # Tableau: NULL + 1 is NULL and NULL > 0 is NULL; DAX would return 1 / FALSE without guards
    r = dax("[Discount] + 1")
    assert r.expression == "IF(ISBLANK('Orders'[Discount]), BLANK(), ('Orders'[Discount] + 1))"
    assert dax("[Discount] > 0.1").expression == "IF(ISBLANK('Orders'[Discount]), BLANK(), ('Orders'[Discount] > 0.1))"
    # non-nullable columns stay simple
    assert dax("[Sales] + 1").expression == "('Orders'[Sales] + 1)"
    # ZN removes the guard
    assert dax("ZN([Discount]) + 1").expression == "(COALESCE('Orders'[Discount], 0) + 1)"


def test_if_elseif_becomes_switch_true():
    r = dax("IF [Sales] > 10 THEN 'a' ELSEIF [Sales] > 5 THEN 'b' ELSE 'c' END")
    assert r.expression.startswith("SWITCH(TRUE(),") and r.expression.count('"') == 6


def test_table_calcs_use_addressing():
    a = [("Products", "Category")]
    assert dax("RUNNING_SUM(SUM([Sales]))", "agg", a).expression == (
        "CALCULATE(SUM('Orders'[Sales]), WINDOW(1, ABS, 0, REL, ALLSELECTED('Products'[Category]), "
        "ORDERBY('Products'[Category])))")
    assert dax("RANK(SUM([Sales]))", "agg", a).expression == (
        "RANKX(ALLSELECTED('Products'[Category]), CALCULATE(SUM('Orders'[Sales])), , DESC, SKIP)")
    assert "OFFSET(-1" in dax("LOOKUP(SUM([Sales]), -1)", "agg", a).expression
    assert dax("INDEX()", "agg", a).expression.startswith("ROWNUMBER(")
    r = dax("RANK(SUM([Sales]))", "agg", None)
    assert any("table calculation" in w for w in r.warnings)


def test_include_exclude_measures():
    tr = DaxTranslator(R)
    inc = tr.lod_measure(parse_formula("{INCLUDE [Customer Name] : SUM([Sales])}"), "avg")
    assert inc.expression == "AVERAGEX(SUMMARIZE('Orders', 'Customers'[Customer Name]), CALCULATE(SUM('Orders'[Sales])))"
    exc = tr.lod_measure(parse_formula("{EXCLUDE [Region] : SUM([Sales])}"), "sum")
    assert exc.expression == "CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Customers'[Region]))"


def test_translation_errors():
    with pytest.raises(TranslationError):
        dax("[Unknown Field] + 1")
    with pytest.raises(TranslationError):
        DaxTranslator(R).translate("SUM([Sales])", "row", host="Orders")   # aggregate in a calc column


def test_unknown_function_is_flagged_not_crashing():
    r = dax("RAWSQL_REAL('x')", "agg")
    assert r.confidence == "low" and r.status == "Manual review"


def test_catalog_is_complete_and_generated():
    c = catalog()
    assert len(c["functions"]) >= 70
    assert len(c["structural"]) >= 30
    cats = {f["category"] for f in c["functions"]}
    assert {"Aggregation", "Logical", "String", "Number", "Date", "LOD", "Table calc", "Parameter"} <= cats
    assert all(f["dax"] and "TODO" not in f["dax"] for f in c["functions"])
