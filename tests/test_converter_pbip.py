"""Converted semantic model, report pages and the PBIP files."""
import json
import re

import pytest

from tableau2pbi.converter import convert
from tableau2pbi.pbip.writer import build_files
from tableau2pbi.validation.dax_engine import parse_dax


def test_star_schema_model(conversions):
    m = conversions["Superstore_Sales.twb"].model
    kinds = {t.name: t.kind for t in m.tables}
    assert kinds["Orders"] == "fact" and kinds["Customers"] == "dim" and kinds["Products"] == "dim"
    assert kinds["_Measures"] == "measures" and kinds["Date"] == "date"
    assert sum(1 for k in kinds.values() if k == "parameter") == 4
    rels = {(r.from_table, r.from_column, r.to_table, r.to_column) for r in m.relationships}
    assert ("Orders", "Customer ID", "Customers", "Customer ID") in rels
    assert ("Orders", "Product ID", "Products", "Product ID") in rels
    assert all(r.cross_filter == "oneDirection" for r in m.relationships)


def test_columns_renamed_and_typed(conversions):
    t = conversions["Superstore_Sales.twb"].model.table("Customers")
    assert [c.name for c in t.columns if not c.expression][:3] == ["Customer ID", "Customer Name", "Segment"]
    assert 'Table.RenameColumns' in t.m_expression and '{"customer_name", "Customer Name"}' in t.m_expression
    assert t.hierarchies == [("Geography", ["Region", "State"])]
    o = conversions["Superstore_Sales.twb"].model.table("Orders")
    assert o.column("Sales").datatype == "double" and o.column("Sales").summarize_by == "sum"
    assert o.column("Order Date").datatype == "dateTime"
    assert o.column("Discount").format_string == "0%"


def test_every_calculation_mapped(conversions):
    for name, res in conversions.items():
        assert res.stats["manual"] == 0, name
        for m in res.calc_mappings:
            assert m.dax, (name, m.tableau_name)


def test_all_generated_dax_parses(conversions):
    for name, res in conversions.items():
        for t in res.model.tables:
            for mm in t.measures:
                parse_dax(mm.expression)
            for c in t.columns:
                if c.expression:
                    parse_dax(c.expression)
            if t.dax_expression and t.kind != "date":
                parse_dax(t.dax_expression)


@pytest.mark.parametrize("ws,visual", [
    ("Sales by Region", "stackedBarChart"), ("Monthly Trend", "lineChart"), ("Category Profitability", "clusteredBarChart"),
    ("Segment Matrix", "pivotTable"), ("Top Customers", "stackedBarChart"), ("Sales Share", "pieChart"),
    ("Profit vs Sales", "scatterChart"), ("KPI Summary", "multiRowCard"), ("Running Sales by Quarter", "lineChart"),
    ("State Map", "filledMap"), ("Sub-Category Rank", "tableEx"), ("Sales Distribution", "clusteredBarChart"),
    ("Region Codes", None),
])
def test_visual_mapping(conversions, ws, visual):
    if visual is None:     # Finance sample: dimension on rows + measure on columns -> horizontal bars
        assert conversions["Finance_CustomSQL.twb"].worksheet_visuals[ws].visual_type == "clusteredBarChart"
        return
    assert conversions["Superstore_Sales.twb"].worksheet_visuals[ws].visual_type == visual


def test_filters_converted(conversions):
    v = conversions["Superstore_Sales.twb"].worksheet_visuals
    assert [f.kind for f in v["Top Customers"].filters] == ["topn"]
    assert v["Top Customers"].filters[0].top_n == 10 and v["Top Customers"].filters[0].by_measure == "Sum of Sales"
    seg = v["Segment Matrix"].filters[0]
    assert seg.kind == "categorical" and seg.values == ["East", "West", "Central"] and seg.column == "Region"
    assert v["Discount Analysis"].filters[0].exclude is True
    assert v["Region Share"].filters[0].kind == "measure-range"
    assert v["Monthly Trend"].filters[0].column == "Order Date Year"


def test_pages_and_slicers(conversions):
    res = conversions["Superstore_Sales.twb"]
    pages = {p.display_name: p for p in res.pages}
    exe = pages["Executive Overview"]
    types = [v.visual_type for v in exe.visuals]
    assert types.count("slicer") == 2 and "textbox" in types
    assert all(0 <= v.x <= 1280 and 0 <= v.y <= 720 for v in exe.visuals)
    # sheets not on a dashboard get their own page
    on_dashboards = {z.name for d in res.workbook.dashboards for z in d.zones if z.type == "worksheet"}
    assert len(on_dashboards) == 9
    assert len(res.pages) == 2 + (16 - 9)


def test_custom_sql_to_m(conversions):
    res = conversions["Finance_CustomSQL.twb"]
    t = res.model.table("Finance Orders")
    assert t.kind == "single" and t.join_plan and t.join_plan["joins"][0]["type"] == "inner"
    assert 'Value.NativeQuery' in t.m_expression and 'Text.From(#"Min Quantity")' in t.m_expression
    assert "Table.Join(T0" in t.m_expression and "JoinKind.Inner" in t.m_expression
    names = [e[0] for e in res.model.expressions]
    assert "Min Quantity" in names and "Finance Orders Server" in names
    assert res.sql_reviews and res.sql_reviews[0]["findings"]


def test_star_join_strategy(workbooks):
    res = convert(workbooks["Finance_CustomSQL.twb"], "star")
    assert {t.name for t in res.model.tables} >= {"Finance Orders SQL", "Targets"}
    assert any(r.to_table == "Targets" for r in res.model.relationships)
    assert any("INNER join" in i["message"] for i in res.issues)


def test_pbip_files_are_well_formed(conversions):
    for name, res in conversions.items():
        files = build_files(res)
        base = res.workbook.name
        assert f"{base}.pbip" in files
        for p, text in files.items():
            if p.endswith((".json", ".pbip", ".pbir", ".pbism", ".platform")):
                json.loads(text)
            if p.endswith(".tmdl") and "/tables/" in p:
                assert text.startswith("table "), p
                assert "partition " in text, p
        pbir = json.loads(files[f"{base}.Report/definition.pbir"])
        assert pbir["datasetReference"]["byPath"]["path"] == f"../{base}.SemanticModel"
        pages = json.loads(files[f"{base}.Report/definition/pages/pages.json"])
        assert len(pages["pageOrder"]) == len(res.pages)


def test_report_references_exist_in_model(conversions):
    """Every Entity/Property used by a visual or filter must exist in the semantic model."""
    for name, res in conversions.items():
        objects = set()
        for t in res.model.tables:
            objects |= {(t.name, c.name) for c in t.columns} | {(t.name, m.name) for m in t.measures}
        for p, text in build_files(res).items():
            if not p.endswith("visual.json"):
                continue
            blob = json.loads(text)

            def walk(x):
                if isinstance(x, dict):
                    for k in ("Column", "Measure"):
                        if k in x and "Entity" in x[k]["Expression"]["SourceRef"]:
                            ent = x[k]["Expression"]["SourceRef"]["Entity"]
                            assert (ent, x[k]["Property"]) in objects, (name, p, ent, x[k]["Property"])
                    for v in x.values():
                        walk(v)
                elif isinstance(x, list):
                    for v in x:
                        walk(v)
            walk(blob)


def test_tmdl_measure_block_syntax(conversions):
    files = build_files(conversions["Superstore_Sales.twb"])
    text = files["Superstore_Sales.SemanticModel/definition/tables/_Measures.tmdl"]
    assert re.search(r"\tmeasure 'Profit Ratio' = DIVIDE\(", text)
    assert "formatString: 0.0%" in text
    rel = files["Superstore_Sales.SemanticModel/definition/relationships.tmdl"]
    assert "fromColumn: Orders.'Customer ID'" in rel and "toColumn: Customers.'Customer ID'" in rel
