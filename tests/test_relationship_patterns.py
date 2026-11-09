"""Patterns found in a real customer workbook (accounts-payable, Excel source, logical relationships).

Synthetic, self-owned data. Each test pins one defect found when converting that workbook:
  * Measure Names / Measure Values (filter members are the value measures; sort on Measure Names)
  * ad-hoc calculations that live only in a worksheet
  * table-calc addressing: ordering-type 'Rows' = Table (across), 'Columns' = Table (down);
    column-instance names with a ':1' suffix; one table calc used along different dimensions
  * SUM(EXCLUDE LOD) and AVG(INCLUDE LOD) inside formulas
  * COUNTD(IF ...) = 0 for marks with only NULLs; LOOKUP beyond the partition edge = NULL
  * date parameters typed as dates
  * a many-to-many relationship (several users per business unit) that must not multiply rows,
    listed first in the XML (the anchor is the hub table, not the first object)
Numbers are also checked against an independent pandas computation, so the two engines cannot
agree on a shared misreading of Tableau semantics.
"""
import random
from datetime import date, timedelta

import pandas as pd
import pytest

from tableau2pbi.converter import convert
from tableau2pbi.parser import read_workbook
from tableau2pbi.pbip.writer import relationships_tmdl
from tableau2pbi.validation.runner import (DataBinding, apply_keys, apply_nullability, key_findings,
                                          nullability_findings, validate)

DS = "federated.0appayments0test"
D = (DS, "AP Payments")
UNITS = ["North Ops", "South Ops", "Central Ops"]
CATS = ["IT", "Facilities", "Logistics"]


def _data(seed=11):
    rnd = random.Random(seed)
    vendors = [{"vendor_id": f"V{i:02d}", "vendor_name": f"Vendor {i:02d}", "category": CATS[i % 3]}
               for i in range(1, 10)]
    access = [{"user_email": f"user{i}@example.com", "unit": UNITS[i % 3]} for i in range(7)]
    bills = []
    for i in range(1, 241):
        v = rnd.choice(vendors)
        due = date(2025, 1, 1) + timedelta(days=rnd.randint(0, 300))
        pay = None if rnd.random() < 0.15 else due + timedelta(days=rnd.choice([-5, -1, 0, 0, 3, 7, 12, 20]))
        bills.append({"bill_id": f"B{i:04d}", "unit": rnd.choice(UNITS), "vendor_id": v["vendor_id"],
                      "due_date": due.isoformat(), "pay_date": pay.isoformat() if pay else None,
                      "amount": round(rnd.uniform(100, 5000), 2)})
    return {"Bills": pd.DataFrame(bills), "Vendors": pd.DataFrame(vendors), "Access": pd.DataFrame(access)}


def _a(s):
    from html import escape
    return escape(str(s), quote=True)


def _r(inst):
    return f"[{DS}].[{inst}]"


def _meta(remote, local, parent, typ, rt, nullable=False):
    return (f"<metadata-record class='column'><remote-name>{remote}</remote-name><remote-type>{rt}</remote-type>"
            f"<local-name>[{_a(local)}]</local-name><parent-name>[{parent}]</parent-name>"
            f"<local-type>{typ}</local-type><contains-null>{'true' if nullable else 'false'}</contains-null>"
            f"</metadata-record>")


def _calc(cid, caption, formula, datatype="real", role="measure", typ="quantitative"):
    return (f"<column caption='{_a(caption)}' datatype='{datatype}' name='[{cid}]' role='{role}' type='{typ}'>"
            f"<calculation class='tableau' formula='{_a(formula)}' /></column>")


def _tc(inst, ordering):
    cid = inst.split(":")[1]
    return (f"<column-instance column='[{cid}]' derivation='User' name='[{inst}]' pivot='key' type='quantitative'>"
            f"<table-calc ordering-type='{ordering}' /></column-instance>")


def _mn_filter(*insts):
    mem = "".join(f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{_a(_r(i))}&quot;' />"
                  for i in insts)
    return (f"<filter class='categorical' column='[{DS}].[:Measure Names]'>"
            f"<groupfilter function='union' user:op='manual'>{mem}</groupfilter></filter>")


def _ws(name, rows, cols, mark="Automatic", enc="", filters="", deps="", sorts=""):
    return f"""
    <worksheet name='{_a(name)}'><table><view>
      <datasources><datasource caption='AP Payments' name='{DS}' /></datasources>
      <datasource-dependencies datasource='{DS}'>{deps}</datasource-dependencies>
      {filters}{sorts}<aggregation value='true' /></view><style />
      <panes><pane><view><breakdown value='auto' /></view><mark class='{mark}' /><encodings>{enc}</encodings></pane></panes>
      <rows>{_a(rows)}</rows><cols>{_a(cols)}</cols></table></worksheet>"""


def _workbook_xml():
    md = "".join([
        _meta("bill_id", "Bill ID", "Bills", "string", 129), _meta("unit", "Unit", "Bills", "string", 129),
        _meta("vendor_id", "Vendor ID", "Bills", "string", 129), _meta("due_date", "Due Date", "Bills", "date", 7),
        _meta("pay_date", "Pay Date", "Bills", "date", 7), _meta("amount", "Amount", "Bills", "real", 5),
        _meta("vendor_id", "Vendor ID (Vendors)", "Vendors", "string", 129),
        _meta("vendor_name", "Vendor Name", "Vendors", "string", 129),
        _meta("category", "Category", "Vendors", "string", 129),
        _meta("user_email", "User Email", "Access", "string", 129),
        _meta("unit", "Unit (Access)", "Access", "string", 129)])
    calcs = "".join([
        _calc("C01", "Is Paid", "IF ISNULL([Pay Date]) THEN 0 ELSE 1 END", "integer"),
        _calc("C02", "On Time Flag", "IF [C01]=1 AND DATEDIFF('day',[Due Date],[Pay Date])<=0 THEN 1 ELSE 0 END",
              "integer"),
        _calc("C03", "On Time %", "SUM([C02])/SUM([C01])"),
        _calc("C04", "Category On-Time %", "{ EXCLUDE [Vendor Name]: SUM([C02])/SUM([C01]) }"),
        _calc("C05", "Variance vs Category", "([C03]-SUM([C04]))*100"),
        _calc("C06", "Days Late", "DATEDIFF('day',[Due Date],[Pay Date])", "integer"),
        _calc("C07", "Avg Days Late per Vendor", "AVG({ INCLUDE [Vendor Name]: AVG([C06]) })"),
        _calc("C08", "Vendor Max Days Late", "{ FIXED [Vendor Name]: MAX([C06]) }", "integer"),
        _calc("C09", "Vendors > 5 Days Late", "COUNTD(IF [C08] > 5 THEN [Vendor Name] END)", "integer"),
        _calc("C10", "Running Amount", "RUNNING_SUM(SUM([Amount]))"),
        _calc("C11", "On Time % MoM", "ZN([C03]) - LOOKUP(ZN([C03]), -1)"),
        _calc("C12", "Vendor On Time %", "{ FIXED [Vendor Name]: SUM([C02])/SUM([C01]) }"),
        _calc("C13", "Amount % of Total", "SUM([Amount])/TOTAL(SUM([Amount]))"),
        _calc("C14", "Days Open", "IF [C01]=0 THEN DATEDIFF('day',[Due Date],[Parameters].[As Of]) END", "integer"),
        _calc("C15", "Viewer Is Admin", "USERNAME() = 'admin@example.com'", "boolean", "dimension", "nominal"),
        _calc("C16", "Admin Flag", "IF [C15] THEN 1 ELSE 0 END", "integer"),
    ])
    conn = "excel-direct.0aptest"
    objs = [("Access", "AC"), ("Bills", "BI"), ("Vendors", "VE")]          # Access first, on purpose
    objects = "".join(f"<object caption='{n}' id='{i}_obj'><properties context=''><relation connection='{conn}' "
                      f"name='{n}' table='[{n}$]' type='table' /></properties></object>" for n, i in objs)

    def rel(a_, b_, oid):
        return (f"<relationship><expression op='='><expression op='[{a_}]' /><expression op='[{b_}]' /></expression>"
                f"<first-end-point object-id='BI_obj' /><second-end-point object-id='{oid}_obj' /></relationship>")
    ds = f"""
    <datasource caption='AP Payments' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections><named-connection caption='ap' name='{conn}'>
          <connection class='excel-direct' filename='Data/ap.xlsx' /></named-connection></named-connections>
        <relation type='collection'>{''.join(f"<relation connection='{conn}' name='{n}' table='[{n}$]' type='table' />" for n, _ in objs)}</relation>
        <metadata-records>{md}</metadata-records>
      </connection>
      {calcs}
      <object-graph><objects>{objects}</objects>
        <relationships>{rel('Unit', 'Unit (Access)', 'AC')}{rel('Vendor ID', 'Vendor ID (Vendors)', 'VE')}</relationships>
      </object-graph>
    </datasource>"""
    params = """
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <column caption='As Of' datatype='date' name='[As Of]' param-domain-type='any' role='measure' type='quantitative' value='#2025-12-31#'>
        <calculation class='tableau' formula='#2025-12-31#' /></column>
    </datasource>"""
    adhoc = ("<column caption='&apos;&apos;' datatype='string' name='[A01]' role='dimension' type='nominal' "
             "user:unnamed='Highlight'><calculation class='tableau' formula='&apos;&apos;' /></column>")
    sheets = "".join([
        _ws("KPI", "", _r(":Measure Names"), enc=f"<text column='{_r('Multiple Values')}' />"
            f"<text column='{_r(':Measure Names')}' />", filters=_mn_filter("sum:Amount:qk", "usr:C03:qk"),
            sorts=f"<manual-sort column='{_r(':Measure Names')}' direction='ASC' />"),
        _ws("Trend", f"({_r('usr:C10:qk')} + {_r('usr:C11:qk')})", _r("tmn:Due Date:ok"), "Line",
            deps=_tc("usr:C10:qk", "Rows") + _tc("usr:C11:qk", "Rows")),
        _ws("By Month", _r("tmn:Due Date:ok"), _r(":Measure Names"), "Text",
            enc=f"<text column='{_r('Multiple Values')}' />",
            filters=_mn_filter("sum:Amount:qk", "usr:C10:qk", "usr:C11:qk"),
            deps=_tc("usr:C10:qk:1", "Columns") + _tc("usr:C11:qk:1", "Columns")),
        _ws("Vendor Variance", f"({_r('none:Category:nk')} / {_r('none:Vendor Name:nk')})", "", "Text",
            enc=f"<text column='{_r('usr:C03:qk')}' /><text column='{_r('usr:C05:qk')}' />"),
        _ws("Category Avg", _r("none:Category:nk"), "", "Text", enc=f"<text column='{_r('usr:C07:qk')}' />"),
        _ws("Vendor Fixed", _r("none:Vendor Name:nk"), "", "Text", enc=f"<text column='{_r('sum:C12:qk')}' />"),
        _ws("Late Vendors by Year", _r("yr:Pay Date:ok"), "", "Text", enc=f"<text column='{_r('usr:C09:qk')}' />"),
        _ws("Share by Category", _r("usr:C13:qk"), _r("none:Category:nk"), "Bar", deps=_tc("usr:C13:qk", "Rows")),
        _ws("Share by Vendor", _r("none:Vendor Name:nk"), "", "Text", enc=f"<text column='{_r('usr:C13:qk')}' />",
            deps=_tc("usr:C13:qk", "Columns")),
        _ws("Highlight", f"({_r('none:Vendor Name:nk')} / {_r('none:A01:nk')})", _r(":Measure Names"), "Square",
            enc=f"<color column='{_r('none:Category:nk')}' /><text column='{_r('Multiple Values')}' />",
            filters=_mn_filter("usr:C03:qk"), deps=adhoc),
        _ws("Open Bills", _r("none:Vendor Name:nk"), "", "Text", enc=f"<text column='{_r('sum:C14:qk')}' />"),
    ])
    return ("<?xml version='1.0' encoding='utf-8' ?>\n<workbook version='18.1' "
            "xmlns:user='http://www.tableausoftware.com/xml/user'>\n"
            f"<datasources>{params}{ds}</datasources><worksheets>{sheets}</worksheets></workbook>\n")


@pytest.fixture(scope="module")
def ap():
    frames = _data()
    wb = read_workbook(data=_workbook_xml().encode(), filename="AP_Payments.twb")
    binding = DataBinding(frames=frames)
    apply_nullability(wb, nullability_findings(wb, binding))      # data profiling, as the CLI does
    mm = apply_keys(wb, key_findings(wb, binding))
    res = convert(wb)
    rep = validate(res, binding, today=date(2025, 12, 31)).to_dict()
    return {"wb": wb, "res": res, "rep": rep, "frames": frames, "mm": mm}


def _check(rep, ws):
    return next(c for c in rep["checks"] if c["name"] == f"Worksheet '{ws}'")


def _cells(rep, ws, measure_key):
    out = {}
    for row in _check(rep, ws)["data"]["rows"]:
        for v in row["values"]:
            if v["measure"] == measure_key:
                out[tuple(row["key"])] = (v["tableau"], v["powerbi"])
    return out


def test_measure_names_become_value_measures(ap):
    kpi = ap["wb"].worksheet("KPI")
    assert [r.key for r in kpi.measure_values] == ["sum:Amount:qk", "usr:C03:qk"]
    assert not kpi.filters and not kpi.sorts                    # Measure Names filter / sort consumed
    v = ap["res"].worksheet_visuals["KPI"]
    assert v.visual_type == "multiRowCard" and len(v.measures) == 2


def test_adhoc_calculation_read_from_worksheet(ap):
    ds = ap["wb"].datasource(DS)
    assert ds.calculations["A01"].extra.get("adhoc") == "Highlight"
    assert _check(ap["rep"], "Highlight")["status"] == "PASS"   # colour dimension kept in the grain


def test_table_calc_ordering_type_and_suffix(ap):
    wb, res = ap["wb"], ap["res"]
    by_month = wb.worksheet("By Month")
    assert all(r.table_calc for r in by_month.measure_values if r.field in ("C10", "C11"))   # ':1' suffix
    dax = "\n".join(m.dax for m in res.calc_mappings)
    assert "ORDERBY(None)" not in dax
    running = next(m for m in res.calc_mappings if m.tableau_name == "Running Amount")
    assert running.status != "Failed" and "Due Date Month Start" in running.dax


def test_running_sum_matches_independent_cumulative_sum(ap):
    """'Rows' = Table (across): months are on Columns, so the running sum runs across the months."""
    b = ap["frames"]["Bills"].copy()
    b["m"] = pd.to_datetime(b["due_date"]).dt.to_period("M").dt.to_timestamp()
    expected = b.groupby("m")["amount"].sum().cumsum()
    cells = _cells(ap["rep"], "Trend", "usr:C10:qk")
    assert len(cells) == len(expected)
    for (k,), (t, p) in cells.items():
        e = expected[pd.Timestamp(k)]
        assert t == pytest.approx(e) and p == pytest.approx(e)


def test_lookup_beyond_edge_is_null(ap):
    first = sorted(_cells(ap["rep"], "By Month", "usr:C11:qk").items())[0]
    assert first[1] == (None, None)


def test_one_table_calc_two_addressings(ap):
    res = ap["res"]
    assert ap["res"].worksheet_visuals["Share by Vendor"].measures != res.worksheet_visuals["Share by Category"].measures
    assert any(m.name.startswith("Amount % of Total (along") for t in res.model.tables for m in t.measures)
    shares = _cells(ap["rep"], "Share by Vendor", "usr:C13:qk")
    assert sum(t for t, _ in shares.values()) == pytest.approx(1.0)
    assert sum(p for _, p in shares.values()) == pytest.approx(1.0)


def test_exclude_inside_sum_matches_independent(ap):
    b = ap["frames"]["Bills"].merge(ap["frames"]["Vendors"], on="vendor_id")
    b["paid"] = b["pay_date"].notna().astype(int)
    b["ontime"] = ((b["paid"] == 1) & (pd.to_datetime(b["pay_date"]) <= pd.to_datetime(b["due_date"]))).astype(int)
    cat = b.groupby("category").apply(lambda g: g.ontime.sum() / g.paid.sum())
    ven = b.groupby(["category", "vendor_name"]).apply(lambda g: g.ontime.sum() / g.paid.sum())
    cells = _cells(ap["rep"], "Vendor Variance", "usr:C05:qk")
    for (c, v), (t, p) in cells.items():
        e = (ven[(c, v)] - cat[c]) * 100
        assert t == pytest.approx(e) and p == pytest.approx(e)


def test_include_inside_avg_and_fixed_sum_not_multiplied(ap):
    assert _check(ap["rep"], "Category Avg")["status"] == "PASS"
    b = ap["frames"]["Bills"].merge(ap["frames"]["Vendors"], on="vendor_id")
    b["paid"] = b["pay_date"].notna().astype(int)
    b["ontime"] = ((b["paid"] == 1) & (pd.to_datetime(b["pay_date"]) <= pd.to_datetime(b["due_date"]))).astype(int)
    # SUM of a vendor-level FIXED = value x bills of the vendor (the many-to-many Access table adds nothing)
    exp = b.groupby("vendor_name").apply(lambda g: g.ontime.sum() / g.paid.sum() * len(g))
    for (v,), (t, p) in _cells(ap["rep"], "Vendor Fixed", "sum:C12:qk").items():
        assert t == pytest.approx(exp[v]) and p == pytest.approx(exp[v])


def test_countd_of_nulls_is_zero(ap):
    cells = _cells(ap["rep"], "Late Vendors by Year", "usr:C09:qk")
    assert any(k[0] in (None, "(null)") for k in cells)
    assert _check(ap["rep"], "Late Vendors by Year")["status"] == "PASS"


def test_date_parameter_is_typed(ap):
    p = ap["res"].model.table("As Of")
    assert "DATE(2025, 12, 31)" in p.dax_expression
    assert _check(ap["rep"], "Open Bills")["status"] == "PASS"


def test_many_to_many_relationship(ap):
    assert ap["mm"] and "Access" in ap["mm"][0]
    tmdl = relationships_tmdl(ap["res"])
    assert "toCardinality: many" in tmdl and "toColumn: Access.Unit" in tmdl
    assert ap["res"].model.table("Bills").kind == "fact"
    row = next(c for c in ap["rep"]["checks"] if c["name"].startswith("Row count"))
    assert row["status"] == "PASS" and row["data"]["tableau"] == 240


def test_all_numbers_match(ap):
    bad = [(c["name"], c["status"], c["detail"]) for c in ap["rep"]["checks"]
           if c["layer"] in ("L2 Data", "L3 Visual") and c["status"] in ("FAIL", "ERROR")]
    assert not bad


def test_user_functions_never_in_calculated_columns(ap):
    """Power BI Desktop refuses to load a model with USERPRINCIPALNAME() in a calculated column
    (PFE_XL_DYNAMIC_SECURITY_FUNCTION_NOT_ALLOWED), also through a dependent calculation."""
    from tableau2pbi.converter import DYNAMIC_SECURITY_DAX
    res = ap["res"]
    for name in ("Viewer Is Admin", "Admin Flag"):
        assert next(m for m in res.calc_mappings if m.tableau_name == name).kind == "measure"
    assert not [c.name for t in res.model.tables for c in t.columns
                if c.expression and DYNAMIC_SECURITY_DAX.search(c.expression)]
    chk = next(c for c in ap["rep"]["checks"] if c["name"] == "Power BI Desktop load rules")
    assert chk["status"] == "PASS"


def test_model_lint_flags_desktop_load_errors(ap):
    import copy
    from tableau2pbi.converter import PbiColumn, PbiMeasure, model_lint
    model = copy.deepcopy(ap["res"].model)
    bills = model.table("Bills")
    bills.columns.append(PbiColumn("Me", "boolean", expression="USERPRINCIPALNAME() = \"x\""))
    bills.columns.append(PbiColumn("Acc", "string", expression="RELATED('Access'[User Email])"))
    model.table("_Measures").measures.append(PbiMeasure("On Time %", "1"))
    found = " | ".join(model_lint(model))
    assert "user function" in found and "many-to-many" in found and "used twice" in found
