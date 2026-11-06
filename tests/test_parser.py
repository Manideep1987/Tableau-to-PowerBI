"""TWB/TWBX metadata reader."""
import io
import zipfile

import pytest

from tableau2pbi.parser import parse_field_ref, parse_member, parse_shelf, read_workbook


def test_superstore_inventory(workbooks):
    wb = workbooks["Superstore_Sales.twb"]
    assert wb.version == "18.1"
    assert len(wb.datasources) == 1
    ds = wb.datasources[0]
    assert ds.caption == "Superstore"
    assert ds.model_style == "relationships"
    assert [t.name for t in ds.tables] == ["Orders", "Customers", "Products"]
    assert len(ds.relationships) == 2
    assert len(ds.columns) == 21
    assert len(ds.calculations) == 26            # 22 formulas + group + bin + 2 sets
    kinds = sorted(c.kind for c in ds.calculations.values())
    assert kinds.count("group") == 1 and kinds.count("bin") == 1 and kinds.count("set") == 2
    assert len(wb.parameters) == 4
    assert len(wb.worksheets) == 16
    assert len(wb.dashboards) == 2
    assert len(wb.actions) == 3
    assert {a.type for a in wb.actions} == {"filter", "highlight", "url"}


def test_connection_metadata(workbooks):
    c = workbooks["Superstore_Sales.twb"].datasources[0].connections[0]
    assert c.cls == "sqlserver" and c.server == "sql-prod-01.corp.local" and c.dbname == "Superstore"


def test_column_metadata(workbooks):
    ds = workbooks["Superstore_Sales.twb"].datasources[0]
    disc = ds.columns["Discount"]
    assert disc.table == "Orders" and disc.remote_name == "discount" and disc.datatype == "real"
    assert disc.contains_null is True
    assert disc.description == "Discount rate applied to the order line"
    assert ds.columns["Customer ID (Customers)"].table == "Customers"
    assert ds.columns["State"].semantic_role == "[State].[Name]"
    assert ds.columns["Row ID"].hidden is True
    assert [h.name for h in ds.hierarchies] == ["Geography", "Product Hierarchy"]
    assert ds.calculations["Calculation_0001"].folder == "KPIs"


def test_calculations_and_parameters(workbooks):
    wb = workbooks["Superstore_Sales.twb"]
    ds = wb.datasources[0]
    pr = ds.calculations["Calculation_0001"]
    assert pr.caption == "Profit Ratio" and pr.formula == "SUM([Profit]) / SUM([Sales])"
    assert pr.default_format == "p0.0%"
    grp = ds.calculations["Region (group)"]
    assert grp.extra["source"] == "Region"
    assert grp.extra["bins"][0] == {"label": "Coastal", "values": ["East", "West"]}
    bin_ = ds.calculations["Sales (bin)"]
    assert bin_.extra["size"] == "500" and bin_.extra["source"] == "Sales"
    top = ds.calculations["Top Customers"]
    assert top.extra["set_type"] == "top" and top.extra["count"] == "20"
    p = wb.parameters["Parameter 1"]
    assert p.caption == "Sales Target" and p.current_value == 250000.0
    assert (p.min, p.max, p.step) == (50000.0, 500000.0, 50000.0)
    assert [m[0] for m in wb.parameters["Parameter 4"].members] == ["Sales", "Profit", "Quantity"]


def test_worksheet_shelves_and_filters(workbooks):
    wb = workbooks["Superstore_Sales.twb"]
    ws = wb.worksheet("Sales by Region")
    assert [r.key for r in ws.rows] == ["none:Region:nk"]
    assert [r.key for r in ws.cols] == ["sum:Sales:qk"]
    assert ws.mark == "Bar"
    assert ws.encodings["color"][0].field == "Category"
    assert ws.sorts and ws.sorts[0]["direction"] == "DESC"
    top = wb.worksheet("Top Customers").filters[0]
    assert top.kind == "top" and top.top_n == 10 and top.top_by == "SUM([Sales])"
    seg = wb.worksheet("Segment Matrix").filters[0]
    assert seg.kind == "categorical" and seg.members == ["East", "West", "Central"]
    disc = wb.worksheet("Discount Analysis").filters[0]
    assert disc.exclude is True and disc.members == ["Same Day"]
    rng = wb.worksheet("Region Share").filters[0]
    assert rng.kind == "quantitative" and rng.min == 1000 and rng.max is None
    rs = wb.worksheet("Running Sales by Quarter")
    tc = [r for r in rs.rows if r.table_calc]
    assert tc and tc[0].table_calc["ordering_type"] == "Columns"


def test_dashboard_zones(workbooks):
    db = workbooks["Superstore_Sales.twb"].dashboards[0]
    assert db.name == "Executive Overview" and (db.width, db.height) == (1400, 800)
    types = [z.type for z in db.zones]
    assert types.count("worksheet") == 5 and "filter" in types and "paramctrl" in types and "text" in types


def test_custom_sql_and_legacy_join(workbooks):
    ds = workbooks["Finance_CustomSQL.twb"].datasources[0]
    assert ds.model_style == "joins"
    assert ds.is_extract is True
    sql = next(t for t in ds.tables if t.relation.type == "text").relation.sql
    assert "<Parameters.Min Quantity>" in sql and "INNER JOIN" in sql
    r = ds.relationships[0]
    assert (r.left_table, r.left_column, r.right_table, r.right_column, r.join_type) == \
        ("Custom SQL Query", "region", "Targets", "region", "inner")


def test_twbx_packaged_files(workbooks):
    wb = workbooks["HR_Analytics.twbx"]
    assert "Data/hr/employees.csv" in wb.packaged_files
    assert wb.datasources[0].connections[0].cls == "textscan"
    assert wb.datasources[0].connections[0].filename == "Data/hr/employees.csv"


def test_field_ref_parsing():
    r = parse_field_ref("[federated.abc].[sum:Sales:qk]")
    assert (r.datasource, r.field, r.derivation, r.pill_type) == ("federated.abc", "Sales", "sum", "qk")
    r = parse_field_ref("[ds].[none:Sub-Category:nk]")
    assert r.field == "Sub-Category"
    r = parse_field_ref("[ds].[usr:Calculation_1:qk:2]")
    assert r.field == "Calculation_1" and r.derivation == "usr"
    refs = parse_shelf("([ds].[none:A:nk] / [ds].[none:B:nk]) + [ds].[:Measure Names]")
    assert [x.field for x in refs] == ["A", "B"]


@pytest.mark.parametrize("raw,expected", [('"East"', "East"), ("2024", 2024), ("1.5", 1.5),
                                          ("#2024-01-01#", "2024-01-01"), ("%null%", None), ("true", True),
                                          ('"He said ""hi"""', 'He said "hi"')])
def test_member_literals(raw, expected):
    assert parse_member(raw) == expected


def test_twbx_from_bytes_and_recover_from_bad_xml():
    twb = b"<?xml version='1.0'?><workbook version='18.1'><datasources/><worksheets><worksheet name='A'><table><rows/><cols/></table></worksheet></worksheets>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Book.twb", twb)          # missing closing tag -> recover mode
        z.writestr("Data/x.csv", "a,b\n1,2\n")
    wb = read_workbook(data=buf.getvalue(), filename="Book.twbx")
    assert [w.name for w in wb.worksheets] == ["A"]
    assert "Data/x.csv" in wb.packaged_files


def test_twbx_without_twb_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        read_workbook(data=buf.getvalue(), filename="bad.twbx")
