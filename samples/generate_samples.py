"""Generate sample data and Tableau workbooks covering the conversion feature matrix.

Outputs (deterministic, seed 42):
  samples/data/superstore.db          SQLite star schema (Orders, Customers, Products, Targets)
  samples/data/csv/*.csv              CSV copies + HR employees file
  samples/workbooks/Superstore_Sales.twb   relationships model, 15 calcs, LOD, table calcs,
                                           groups/sets/bins, 4 params, 12 sheets, 2 dashboards
  samples/workbooks/Finance_CustomSQL.twb  custom SQL with SQL parameter + legacy inner join
  samples/workbooks/HR_Analytics.twbx      packaged workbook with CSV (textscan) source
"""
from __future__ import annotations

import csv
import io
import os
import random
import sqlite3
import zipfile
from datetime import date, timedelta
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
WB = os.path.join(HERE, "workbooks")

REGIONS = {
    "East": ["New York", "Pennsylvania", "Massachusetts", "New Jersey"],
    "West": ["California", "Washington", "Oregon", "Arizona"],
    "Central": ["Texas", "Illinois", "Michigan", "Minnesota"],
    "South": ["Florida", "Georgia", "Virginia", "Tennessee"],
}
CATEGORIES = {
    "Furniture": ["Chairs", "Tables", "Bookcases", "Furnishings"],
    "Office Supplies": ["Binders", "Paper", "Storage", "Art"],
    "Technology": ["Phones", "Machines", "Accessories", "Copiers"],
}
SEGMENTS = ["Consumer", "Corporate", "Home Office"]
SHIP_MODES = ["Standard Class", "Second Class", "First Class", "Same Day"]
FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew",
         "Sam", "Robin", "Kai", "Rowan", "Emery", "Sky", "Reese", "Parker", "Hayden", "Blake"]
LAST = ["Smith", "Lee", "Patel", "Garcia", "Kim", "Brown", "Nguyen", "Lopez", "Clark", "Young"]


def build_data(seed=42):
    rnd = random.Random(seed)
    customers = []
    for i in range(1, 201):
        region = rnd.choice(list(REGIONS))
        customers.append({
            "customer_id": f"C{i:04d}",
            "customer_name": f"{FIRST[i % 20]} {LAST[(i * 7) % 10]} {i}",
            "segment": rnd.choice(SEGMENTS),
            "region": region,
            "state": rnd.choice(REGIONS[region]),
        })
    products = []
    pid = 1
    for cat, subs in CATEGORIES.items():
        for sub in subs:
            for k in range(5):
                base = {"Furniture": 180, "Office Supplies": 25, "Technology": 320}[cat]
                products.append({"product_id": f"P{pid:04d}", "product_name": f"{sub} Model {k + 1}",
                                 "category": cat, "sub_category": sub,
                                 "unit_price": round(base * rnd.uniform(0.4, 2.2), 2)})
                pid += 1
    orders = []
    start = date(2023, 1, 1)
    for i in range(1, 3001):
        c = rnd.choice(customers)
        p = rnd.choice(products)
        od = start + timedelta(days=rnd.randint(0, 365 * 3 - 1))
        ship_days = rnd.choice([0, 1, 2, 3, 4, 5, 6, 7])
        qty = rnd.randint(1, 9)
        disc = rnd.choice([0, 0, 0, 0.1, 0.15, 0.2, 0.3, 0.45])
        if rnd.random() < 0.03:
            disc_val = None                      # deliberate NULLs (null-semantics tests)
        else:
            disc_val = disc
        sales = round(p["unit_price"] * qty * (1 - (disc_val or 0)), 2)
        margin = rnd.uniform(-0.25, 0.4) - (disc_val or 0) * 0.6
        orders.append({
            "row_id": i, "order_id": f"O{100000 + (i // 2)}", "order_date": od.isoformat(),
            "ship_date": (od + timedelta(days=ship_days)).isoformat(),
            "ship_mode": SHIP_MODES[min(ship_days // 2, 3)] if ship_days else "Same Day",
            "customer_id": c["customer_id"], "product_id": p["product_id"],
            "sales": sales, "quantity": qty, "discount": disc_val,
            "profit": round(sales * margin, 2),
        })
    targets = [{"region": r, "sales_target": t} for r, t in
               zip(REGIONS, [380000, 420000, 360000, 300000])]
    employees = []
    depts = ["Engineering", "Sales", "Finance", "HR", "Operations", "Marketing"]
    for i in range(1, 401):
        hire = date(2015, 1, 1) + timedelta(days=rnd.randint(0, 3600))
        term = None
        if rnd.random() < 0.22:
            term = hire + timedelta(days=rnd.randint(120, 2400))
            if term > date(2025, 12, 31):
                term = None
        dept = rnd.choice(depts)
        lvl = rnd.choice(["L1", "L2", "L3", "L4", "L5"])
        salary = round({"L1": 52000, "L2": 68000, "L3": 88000, "L4": 115000, "L5": 150000}[lvl]
                       * rnd.uniform(0.85, 1.25) * (1.15 if dept == "Engineering" else 1), 0)
        employees.append({
            "employee_id": f"E{i:04d}", "employee_name": f"{FIRST[i % 20]} {LAST[i % 10]}",
            "department": dept, "job_level": lvl, "gender": rnd.choice(["F", "M", "X"]),
            "location": rnd.choice(["Austin", "Boston", "Denver", "Seattle"]),
            "hire_date": hire.isoformat(), "termination_date": term.isoformat() if term else None,
            "salary": salary,
            "performance_score": None if rnd.random() < 0.05 else rnd.choice([1, 2, 3, 3, 4, 4, 5]),
        })
    return customers, products, orders, targets, employees


def write_db(customers, products, orders, targets, employees):
    os.makedirs(os.path.join(DATA, "csv"), exist_ok=True)
    path = os.path.join(DATA, "superstore.db")
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    for name, rows in (("Customers", customers), ("Products", products), ("Orders", orders),
                       ("Targets", targets)):
        cols = list(rows[0])
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    con.close()
    for name, rows in (("Customers", customers), ("Products", products), ("Orders", orders),
                       ("Targets", targets), ("employees", employees)):
        with open(os.path.join(DATA, "csv", f"{name}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if v is None else v) for k, v in r.items()})


# ----------------------------------------------------------- TWB builders --
def a(s):
    return escape(str(s), quote=True)


def meta(remote, local, parent, local_type, remote_type, agg, contains_null=False):
    return (f"<metadata-record class='column'><remote-name>{remote}</remote-name>"
            f"<remote-type>{remote_type}</remote-type><local-name>[{a(local)}]</local-name>"
            f"<parent-name>[{a(parent)}]</parent-name><remote-alias>{remote}</remote-alias>"
            f"<local-type>{local_type}</local-type><aggregation>{agg}</aggregation>"
            f"<contains-null>{'true' if contains_null else 'false'}</contains-null></metadata-record>")


RT = {"string": 129, "integer": 20, "real": 5, "date": 7, "datetime": 135}


def column(name, datatype, role, typ, caption=None, extra="", inner=""):
    cap = f" caption='{a(caption)}'" if caption else ""
    return (f"<column{cap} datatype='{datatype}' name='[{a(name)}]' role='{role}' type='{typ}'{extra}>"
            f"{inner}</column>")


def calc(internal, caption, formula, datatype="real", role="measure", typ="quantitative", fmt=None):
    f = f" default-format='{a(fmt)}'" if fmt else ""
    return (f"<column caption='{a(caption)}' datatype='{datatype}' name='[{internal}]' role='{role}' "
            f"type='{typ}'{f}><calculation class='tableau' formula='{a(formula)}' /></column>")


def ws(name, ds, rows, cols, mark="Automatic", encodings="", filters="", deps="", sorts="", title=None):
    t = (f"<layout-options><title><formatted-text><run>{a(title)}</run></formatted-text></title></layout-options>"
         if title else "")
    return f"""
    <worksheet name='{a(name)}'>
      {t}
      <table>
        <view>
          <datasources><datasource caption='{a(ds[1])}' name='{ds[0]}' /></datasources>
          <datasource-dependencies datasource='{ds[0]}'>{deps}</datasource-dependencies>
          {filters}
          {sorts}
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view><breakdown value='auto' /></view>
            <mark class='{mark}' />
            <encodings>{encodings}</encodings>
          </pane>
        </panes>
        <rows>{a(rows)}</rows>
        <cols>{a(cols)}</cols>
      </table>
    </worksheet>"""


def cat_filter(ds, inst, members, exclude=False):
    mem = "".join(f"<groupfilter function='member' level='[{a(inst)}]' member='{a(m)}' />" for m in members)
    if exclude:
        body = (f"<groupfilter function='except' user:ui-domain='database' user:ui-enumeration='exclusive' "
                f"user:ui-marker='enumerate'><groupfilter function='level-members' level='[{a(inst)}]' />"
                f"<groupfilter function='union'>{mem}</groupfilter></groupfilter>")
    elif len(members) == 1:
        body = mem.replace("/>", "user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />")
    else:
        body = (f"<groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' "
                f"user:ui-marker='enumerate'>{mem}</groupfilter>")
    return f"<filter class='categorical' column='[{ds}].[{a(inst)}]'>{body}</filter>"


def top_filter(ds, inst, count, expression):
    return (f"<filter class='categorical' column='[{ds}].[{a(inst)}]'>"
            f"<groupfilter count='{a(count)}' end='top' function='end' units='records' user:ui-marker='end' "
            f"user:ui-top-by-field='true'><groupfilter direction='DESC' expression='{a(expression)}' "
            f"function='order' user:ui-marker='order'><groupfilter function='level-members' "
            f"level='[{a(inst)}]' user:ui-enumeration='all' user:ui-marker='enumerate' /></groupfilter>"
            f"</groupfilter></filter>")


def qfilter(ds, inst, mn=None, mx=None):
    inner = (f"<min>{a(mn)}</min>" if mn is not None else "") + (f"<max>{a(mx)}</max>" if mx is not None else "")
    rng = "in-range" if mn is not None and mx is not None else ("atleast" if mn is not None else "atmost")
    return (f"<filter class='quantitative' column='[{ds}].[{a(inst)}]' included-values='{rng}'>"
            f"{inner}</filter>")


def zone(zid, x, y, w, h, name=None, ztype=None, param=None, text=None, mode=None):
    attrs = f"id='{zid}' x='{x}' y='{y}' w='{w}' h='{h}'"
    if name:
        attrs += f" name='{a(name)}'"
    if ztype:
        attrs += f" type-v2='{ztype}'"
    if param:
        attrs += f" param='{a(param)}'"
    if mode:
        attrs += f" mode='{mode}'"
    inner = f"<formatted-text><run>{a(text)}</run></formatted-text>" if text else ""
    return f"<zone {attrs}>{inner}</zone>"


HEADER = ("<?xml version='1.0' encoding='utf-8' ?>\n"
          "<workbook original-version='18.1' source-build='2024.2.0 (20242.24.0716.1944)' "
          "source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>\n")


def superstore_twb():
    DS = "federated.1x2k9wq0ab3k7l1e8cm4h0qz5lzt"
    D = (DS, "Superstore")
    params = f"""
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column caption='Sales Target' datatype='real' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='250000.0'>
        <calculation class='tableau' formula='250000.0' />
        <range granularity='50000.0' max='500000.0' min='50000.0' />
      </column>
      <column caption='Top N Customers' datatype='integer' name='[Parameter 2]' param-domain-type='list' role='measure' type='quantitative' value='10'>
        <calculation class='tableau' formula='10' />
        <members><member value='5' /><member value='10' /><member value='15' /><member value='20' /></members>
      </column>
      <column caption='Discount Threshold' datatype='real' name='[Parameter 3]' param-domain-type='range' role='measure' type='quantitative' value='0.2'>
        <calculation class='tableau' formula='0.2' />
        <range granularity='0.05' max='0.5' min='0.0' />
      </column>
      <column caption='Metric Selector' datatype='string' name='[Parameter 4]' param-domain-type='list' role='dimension' type='nominal' value='&quot;Sales&quot;'>
        <calculation class='tableau' formula='&quot;Sales&quot;' />
        <members><member value='&quot;Sales&quot;' /><member value='&quot;Profit&quot;' /><member value='&quot;Quantity&quot;' /></members>
      </column>
    </datasource>"""
    md = "".join([
        meta("row_id", "Row ID", "Orders", "integer", RT["integer"], "Sum"),
        meta("order_id", "Order ID", "Orders", "string", RT["string"], "Count"),
        meta("order_date", "Order Date", "Orders", "date", RT["date"], "Year"),
        meta("ship_date", "Ship Date", "Orders", "date", RT["date"], "Year"),
        meta("ship_mode", "Ship Mode", "Orders", "string", RT["string"], "Count"),
        meta("customer_id", "Customer ID", "Orders", "string", RT["string"], "Count"),
        meta("product_id", "Product ID", "Orders", "string", RT["string"], "Count"),
        meta("sales", "Sales", "Orders", "real", RT["real"], "Sum"),
        meta("quantity", "Quantity", "Orders", "integer", RT["integer"], "Sum"),
        meta("discount", "Discount", "Orders", "real", RT["real"], "Avg", contains_null=True),
        meta("profit", "Profit", "Orders", "real", RT["real"], "Sum"),
        meta("customer_id", "Customer ID (Customers)", "Customers", "string", RT["string"], "Count"),
        meta("customer_name", "Customer Name", "Customers", "string", RT["string"], "Count"),
        meta("segment", "Segment", "Customers", "string", RT["string"], "Count"),
        meta("region", "Region", "Customers", "string", RT["string"], "Count"),
        meta("state", "State", "Customers", "string", RT["string"], "Count"),
        meta("product_id", "Product ID (Products)", "Products", "string", RT["string"], "Count"),
        meta("product_name", "Product Name", "Products", "string", RT["string"], "Count"),
        meta("category", "Category", "Products", "string", RT["string"], "Count"),
        meta("sub_category", "Sub-Category", "Products", "string", RT["string"], "Count"),
        meta("unit_price", "Unit Price", "Products", "real", RT["real"], "Avg"),
    ])
    calcs = "".join([
        calc("Calculation_0001", "Profit Ratio", "SUM([Profit]) / SUM([Sales])", fmt="p0.0%"),
        calc("Calculation_0002", "Profit Status",
             "IF [Profit] > 0 THEN 'Profitable'\nELSEIF [Profit] = 0 THEN 'Break-even'\nELSE 'Loss'\nEND",
             "string", "dimension", "nominal"),
        calc("Calculation_0003", "Discount Band",
             "IIF(ZN([Discount]) >= [Parameters].[Parameter 3], 'High Discount', 'Standard')",
             "string", "dimension", "nominal"),
        calc("Calculation_0004", "Days to Ship", "DATEDIFF('day', [Order Date], [Ship Date])",
             "integer", "measure", "quantitative"),
        calc("Calculation_0005", "Customer Lifetime Sales", "{FIXED [Customer Name] : SUM([Sales])}"),
        calc("Calculation_0006", "Region Sales", "{ FIXED [Region] : SUM([Sales]) }"),
        calc("Calculation_0007", "Sales vs Target", "SUM([Sales]) - [Parameters].[Parameter 1]", fmt="c\"$\"#,##0"),
        calc("Calculation_0008", "Running Sales", "RUNNING_SUM(SUM([Sales]))"),
        calc("Calculation_0009", "Sales Rank", "RANK(SUM([Sales]))", "integer"),
        calc("Calculation_0010", "Order Count", "COUNTD([Order ID])", "integer"),
        calc("Calculation_0011", "Avg Sales per Customer", "SUM([Sales]) / COUNTD([Customer Name])"),
        calc("Calculation_0012", "Selected Metric",
             "CASE [Parameters].[Parameter 4]\nWHEN 'Sales' THEN SUM([Sales])\nWHEN 'Profit' THEN SUM([Profit])\n"
             "ELSE SUM([Quantity])\nEND"),
        calc("Calculation_0013", "Net Sales", "[Sales] * (1 - [Discount])"),
        calc("Calculation_0014", "Customer Tier",
             "IF [Customer Lifetime Sales] >= 25000 THEN 'Platinum' ELSEIF [Customer Lifetime Sales] >= 12000 "
             "THEN 'Gold' ELSE 'Silver' END", "string", "dimension", "nominal"),
        calc("Calculation_0015", "Order Month Name", "DATENAME('month', [Order Date])", "string", "dimension", "nominal"),
        calc("Calculation_0016", "Product Code", "UPPER(LEFT([Sub-Category], 3)) + '-' + RIGHT([Product ID], 4)",
             "string", "dimension", "nominal"),
        calc("Calculation_0017", "Avg Customer Sales (INCLUDE)", "{INCLUDE [Customer Name] : SUM([Sales])}"),
        calc("Calculation_0018", "Share of Region", "SUM([Sales]) / SUM([Region Sales])", fmt="p1"),
        calc("Calculation_0019", "Large Order", "[Quantity] >= 7 AND [Sales] > 500", "boolean", "dimension", "nominal"),
        calc("Calculation_0020", "Percent of Total Sales", "SUM([Sales]) / TOTAL(SUM([Sales]))", fmt="p1"),
        calc("Calculation_0021", "Ship Quarter Start", "DATETRUNC('quarter', [Ship Date])", "date", "dimension", "ordinal"),
        calc("Calculation_0022", "Profit per Unit", "ZN(SUM([Profit])) / SUM([Quantity])"),
    ])
    groups = """
      <column caption='Region Group' datatype='string' name='[Region (group)]' role='dimension' type='nominal'>
        <calculation class='categorical-bin' column='[Region]' new-bin='true'>
          <bin default-name='false' value='&quot;Coastal&quot;'><value>&quot;East&quot;</value><value>&quot;West&quot;</value></bin>
          <bin default-name='false' value='&quot;Inland&quot;'><value>&quot;Central&quot;</value><value>&quot;South&quot;</value></bin>
        </calculation>
      </column>
      <column caption='Sales (bin)' datatype='integer' name='[Sales (bin)]' role='dimension' type='ordinal'>
        <calculation class='bin' decimals='0' formula='[Sales]' peg='0' size='500' />
      </column>
      <group caption='Key States' name='[Key States]' name-style='unqualified' user:ui-builder='identity-set'>
        <groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate'>
          <groupfilter function='member' level='[State]' member='&quot;California&quot;' />
          <groupfilter function='member' level='[State]' member='&quot;New York&quot;' />
          <groupfilter function='member' level='[State]' member='&quot;Texas&quot;' />
        </groupfilter>
      </group>
      <group caption='Top 20 Customers' name='[Top Customers]' name-style='unqualified' user:ui-builder='filter-group'>
        <groupfilter count='20' end='top' function='end' units='records' user:ui-marker='end' user:ui-top-by-field='true'>
          <groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>
            <groupfilter function='level-members' level='[Customer Name]' user:ui-enumeration='all' user:ui-marker='enumerate' />
          </groupfilter>
        </groupfilter>
      </group>"""
    cols = "".join([
        column("Region", "string", "dimension", "nominal"),
        column("State", "string", "dimension", "nominal", extra=" semantic-role='[State].[Name]'"),
        column("Sales", "real", "measure", "quantitative", extra=" default-format='c&quot;$&quot;#,##0.00'"),
        column("Profit", "real", "measure", "quantitative", extra=" default-format='c&quot;$&quot;#,##0.00'"),
        column("Discount", "real", "measure", "quantitative", extra=" default-format='p0%'",
               inner="<desc><formatted-text><run>Discount rate applied to the order line</run></formatted-text></desc>"),
        column("Row ID", "integer", "dimension", "ordinal", extra=" hidden='true'"),
    ])
    datasource = f"""
    <datasource caption='Superstore' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='sql-prod-01' name='sqlserver.0hb5l3o1x9m1rc'>
            <connection authentication='sqlserver' class='sqlserver' dbname='Superstore' odbc-native-protocol='' one-time-sql='' port='1433' schema='dbo' server='sql-prod-01.corp.local' username='bi_reader' />
          </named-connection>
        </named-connections>
        <_.fcp.ObjectModelEncapsulateLegacy.false...relation type='collection'>
          <relation connection='sqlserver.0hb5l3o1x9m1rc' name='Orders' table='[dbo].[Orders]' type='table' />
          <relation connection='sqlserver.0hb5l3o1x9m1rc' name='Customers' table='[dbo].[Customers]' type='table' />
          <relation connection='sqlserver.0hb5l3o1x9m1rc' name='Products' table='[dbo].[Products]' type='table' />
        </_.fcp.ObjectModelEncapsulateLegacy.false...relation>
        <metadata-records>{md}</metadata-records>
      </connection>
      <aliases enabled='yes' />
      {cols}
      {calcs}
      {groups}
      <drill-paths>
        <drill-path name='Geography'><field>[Region]</field><field>[State]</field></drill-path>
        <drill-path name='Product Hierarchy'><field>[Category]</field><field>[Sub-Category]</field><field>[Product Name]</field></drill-path>
      </drill-paths>
      <folder name='KPIs' role='measures'>
        <folder-item name='[Calculation_0001]' type='field' />
        <folder-item name='[Calculation_0010]' type='field' />
      </folder>
      <_.fcp.ObjectModelTableType.true...column caption='Orders' datatype='table' name='[__tableau_internal_object_id__].[Orders_A1]' role='measure' type='quantitative' />
      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>
          <object caption='Orders' id='Orders_A1'><properties context=''><relation connection='sqlserver.0hb5l3o1x9m1rc' name='Orders' table='[dbo].[Orders]' type='table' /></properties></object>
          <object caption='Customers' id='Customers_B2'><properties context=''><relation connection='sqlserver.0hb5l3o1x9m1rc' name='Customers' table='[dbo].[Customers]' type='table' /></properties></object>
          <object caption='Products' id='Products_C3'><properties context=''><relation connection='sqlserver.0hb5l3o1x9m1rc' name='Products' table='[dbo].[Products]' type='table' /></properties></object>
        </objects>
        <relationships>
          <relationship>
            <expression op='='><expression op='[Customer ID]' /><expression op='[Customer ID (Customers)]' /></expression>
            <first-end-point object-id='Orders_A1' /><second-end-point object-id='Customers_B2' />
          </relationship>
          <relationship>
            <expression op='='><expression op='[Product ID]' /><expression op='[Product ID (Products)]' /></expression>
            <first-end-point object-id='Orders_A1' /><second-end-point object-id='Products_C3' />
          </relationship>
        </relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>"""

    def r(inst):
        return f"[{DS}].[{inst}]"

    sheets = "".join([
        ws("Sales by Region", D, r("none:Region:nk"), r("sum:Sales:qk"), "Bar",
           encodings=f"<color column='{r('none:Category:nk')}' />",
           sorts=f"<computed-sort column='{r('none:Region:nk')}' direction='DESC' using='{r('sum:Sales:qk')}' />",
           title="Sales by Region and Category"),
        ws("Monthly Trend", D, f"({r('sum:Sales:qk')} + {r('sum:Profit:qk')})", r("tmn:Order Date:qk"), "Line",
           filters=qfilter(DS, "yr:Order Date:ok", 2024, 2025)),
        ws("Category Profitability", D, r("none:Category:nk"), f"({r('sum:Profit:qk')} + {r('usr:Calculation_0001:qk')})",
           "Bar", encodings=f"<label column='{r('usr:Calculation_0010:qk')}' />"),
        ws("Segment Matrix", D, f"({r('none:Segment:nk')} / {r('none:Calculation_0002:nk')})", r("yr:Order Date:ok"),
           "Text", encodings=f"<text column='{r('sum:Sales:qk')}' />",
           filters=cat_filter(DS, "none:Region:nk", ['"East"', '"West"', '"Central"'])),
        ws("Top Customers", D, r("none:Customer Name:nk"), r("sum:Sales:qk"), "Bar",
           filters=top_filter(DS, "none:Customer Name:nk", "10", "SUM([Sales])"),
           encodings=f"<color column='{r('none:Calculation_0014:nk')}' />"),
        ws("Sales Share", D, "", "", "Pie",
           encodings=f"<color column='{r('none:Category:nk')}' /><size column='{r('sum:Sales:qk')}' />"),
        ws("Profit vs Sales", D, r("sum:Profit:qk"), r("sum:Sales:qk"), "Circle",
           encodings=f"<lod column='{r('none:Sub-Category:nk')}' />"),
        ws("KPI Summary", D, "", "", "Text",
           encodings=(f"<text column='{r('sum:Sales:qk')}' /><text column='{r('usr:Calculation_0001:qk')}' />"
                      f"<text column='{r('usr:Calculation_0010:qk')}' /><text column='{r('usr:Calculation_0007:qk')}' />")),
        ws("Running Sales by Quarter", D, f"({r('sum:Sales:qk')} + {r('usr:Calculation_0008:qk')})", r("tqr:Order Date:qk"),
           "Line", deps=f"<column-instance column='[Calculation_0008]' derivation='User' name='[usr:Calculation_0008:qk]' pivot='key' type='quantitative'><table-calc ordering-type='Rows' /></column-instance>"),
        ws("State Map", D, "", "", "Multipolygon",
           encodings=f"<lod column='{r('none:State:nk')}' /><color column='{r('sum:Sales:qk')}' />"),
        ws("Sub-Category Rank", D, r("none:Sub-Category:nk"),
           f"({r('sum:Sales:qk')} + {r('usr:Calculation_0009:qk')} + {r('usr:Calculation_0020:qk')})", "Text",
           encodings=f"<text column='{r('sum:Sales:qk')}' />",
           deps=f"<column-instance column='[Calculation_0009]' derivation='User' name='[usr:Calculation_0009:qk]' pivot='key' type='quantitative'><table-calc ordering-type='Columns' /></column-instance>"),
        ws("Customer Tiers", D, f"({r('none:Calculation_0014:nk')} / {r('none:Region (group):nk')})",
           f"({r('ctd:Customer Name:qk')} + {r('avg:Calculation_0005:qk')} + {r('avg:Calculation_0017:qk')})", "Text",
           encodings=f"<text column='{r('ctd:Customer Name:qk')}' />"),
        ws("Discount Analysis", D, f"({r('none:Calculation_0003:nk')} / {r('none:Ship Mode:nk')})",
           f"({r('sum:Calculation_0013:qk')} + {r('avg:Discount:qk')} + {r('avg:Calculation_0004:qk')})", "Text",
           encodings=f"<text column='{r('sum:Calculation_0013:qk')}' />",
           filters=cat_filter(DS, "none:Ship Mode:nk", ['"Same Day"'], exclude=True)),
        ws("Region Share", D, f"({r('none:Region:nk')} / {r('none:Category:nk')})",
           f"({r('usr:Calculation_0018:qk')} + {r('sum:Calculation_0006:qk')} + {r('usr:Calculation_0011:qk')})", "Text",
           encodings=f"<text column='{r('usr:Calculation_0018:qk')}' />",
           filters=qfilter(DS, "sum:Sales:qk", 1000, None)),
        ws("Sales Distribution", D, r("none:Sales (bin):ok"), r("cnt:Order ID:qk"), "Bar",
           filters=qfilter(DS, "none:Sales (bin):ok", None, 3000)),
        ws("Key States Selected Metric", D, f"({r('none:Key States:nk')} / {r('none:Calculation_0016:nk')})",
           f"({r('usr:Calculation_0012:qk')} + {r('usr:Calculation_0022:qk')})", "Text",
           encodings=f"<text column='{r('usr:Calculation_0012:qk')}' />",
           filters=cat_filter(DS, "none:Category:nk", ['"Technology"'])),
    ])
    dashboards = f"""
    <dashboards>
      <dashboard name='Executive Overview'>
        <size maxheight='800' maxwidth='1400' minheight='800' minwidth='1400' />
        <zones>
          <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
            {zone(2, 0, 0, 100000, 8000, ztype='text', text='Executive Sales Overview')}
            {zone(3, 0, 8000, 100000, 16000, name='KPI Summary')}
            {zone(4, 0, 24000, 50000, 38000, name='Sales by Region')}
            {zone(5, 50000, 24000, 50000, 38000, name='Monthly Trend')}
            {zone(6, 0, 62000, 50000, 38000, name='Category Profitability')}
            {zone(7, 50000, 62000, 34000, 38000, name='Sales Share')}
            {zone(8, 84000, 62000, 16000, 19000, name='Sales by Region', ztype='filter', param=f'[{DS}].[none:Segment:nk]', mode='checkdropdown')}
            {zone(9, 84000, 81000, 16000, 19000, ztype='paramctrl', param='[Parameters].[Parameter 1]', mode='slider')}
          </zone>
        </zones>
      </dashboard>
      <dashboard name='Customer Insights'>
        <size maxheight='768' maxwidth='1366' minheight='768' minwidth='1366' />
        <zones>
          <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
            {zone(2, 0, 0, 60000, 50000, name='Top Customers')}
            {zone(3, 60000, 0, 40000, 50000, name='Customer Tiers')}
            {zone(4, 0, 50000, 60000, 50000, name='Profit vs Sales')}
            {zone(5, 60000, 50000, 40000, 50000, name='State Map')}
            {zone(6, 0, 0, 0, 0, ztype='bitmap', name='logo.png')}
          </zone>
        </zones>
      </dashboard>
    </dashboards>"""
    actions = f"""
    <actions>
      <action caption='Filter by Region' name='[Action1]'>
        <activation auto-clear='true' type='on-select' />
        <source dashboard='Executive Overview' type='sheet' worksheet='Sales by Region' />
        <command command='tsc:tsl-filter'><param name='special-fields' value='all' /><param name='target' value='Executive Overview' /></command>
      </action>
      <action caption='Highlight Category' name='[Action2]'>
        <activation type='on-hover' />
        <source dashboard='Executive Overview' type='sheet' worksheet='Category Profitability' />
        <command command='tsc:brush'><param name='field-captions' value='Category' /></command>
      </action>
      <action caption='Open CRM' name='[Action3]'>
        <activation type='on-menu' />
        <source dashboard='Customer Insights' type='sheet' worksheet='Top Customers' />
        <command command='tsc:browser'><param name='url' value='https://crm.example.com/customer/&lt;Customer Name&gt;' /></command>
      </action>
    </actions>"""
    return (HEADER + f"  <datasources>{params}{datasource}\n  </datasources>\n  <worksheets>{sheets}\n  </worksheets>"
            + dashboards + actions + "\n</workbook>\n")


def finance_twb():
    DS = "federated.0fin7qz2x1lb9e0a8m3k"
    D = (DS, "Finance Orders")
    sql = ("SELECT o.order_id, o.order_date, o.sales, o.profit, o.quantity, c.region, c.segment\n"
           "FROM Orders o\nINNER JOIN Customers c ON c.customer_id = o.customer_id\n"
           "WHERE o.quantity &gt;= &lt;Parameters.Min Quantity&gt;")
    params = """
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <column caption='Min Quantity' datatype='integer' name='[Min Quantity]' param-domain-type='range' role='measure' type='quantitative' value='2'>
        <calculation class='tableau' formula='2' />
        <range granularity='1' max='9' min='1' />
      </column>
      <column caption='Growth Rate' datatype='real' name='[Growth Rate]' param-domain-type='any' role='measure' type='quantitative' value='0.08'>
        <calculation class='tableau' formula='0.08' />
      </column>
    </datasource>"""
    md = "".join([
        meta("order_id", "order_id", "Custom SQL Query", "string", RT["string"], "Count"),
        meta("order_date", "order_date", "Custom SQL Query", "date", RT["date"], "Year"),
        meta("sales", "sales", "Custom SQL Query", "real", RT["real"], "Sum"),
        meta("profit", "profit", "Custom SQL Query", "real", RT["real"], "Sum"),
        meta("quantity", "quantity", "Custom SQL Query", "integer", RT["integer"], "Sum"),
        meta("region", "region", "Custom SQL Query", "string", RT["string"], "Count"),
        meta("segment", "segment", "Custom SQL Query", "string", RT["string"], "Count"),
        meta("region", "region (Targets)", "Targets", "string", RT["string"], "Count"),
        meta("sales_target", "sales_target", "Targets", "real", RT["real"], "Sum"),
    ])
    calcs = "".join([
        calc("Calculation_F01", "Target Attainment", "SUM([sales]) / SUM([sales_target])", fmt="p0.0%"),
        calc("Calculation_F02", "Projected Sales", "SUM([sales]) * (1 + [Parameters].[Growth Rate])"),
        calc("Calculation_F03", "Margin %", "IF SUM([sales]) = 0 THEN NULL ELSE SUM([profit]) / SUM([sales]) END",
             fmt="p1"),
        calc("Calculation_F04", "Fiscal Year",
             "IF DATEPART('month', [order_date]) >= 7 THEN YEAR([order_date]) + 1 ELSE YEAR([order_date]) END",
             "integer", "dimension", "ordinal"),
        calc("Calculation_F05", "Sales YoY Prior", "LOOKUP(SUM([sales]), -1)"),
        calc("Calculation_F06", "Order Size", "CASE [quantity] WHEN 1 THEN 'Single' WHEN 2 THEN 'Pair' ELSE 'Bulk' END",
             "string", "dimension", "nominal"),
        calc("Calculation_F07", "Loss Orders", "SUM(IF [profit] < 0 THEN 1 ELSE 0 END)", "integer"),
        calc("Calculation_F08", "Region Code", "UPPER(LEFT([region], 1)) + STR(LEN([region]))", "string",
             "dimension", "nominal"),
        calc("Calculation_F09", "Avg Monthly Sales", "WINDOW_AVG(SUM([sales]))"),
        calc("Calculation_F10", "Max Order Value", "MAX([sales])"),
    ])
    datasource = f"""
    <datasource caption='Finance Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='pg-finance' name='postgres.1abc'>
            <connection authentication='username-password' class='postgres' dbname='finance' port='5432' server='pg-finance.corp.local' username='fin_reader' sslmode='require' />
          </named-connection>
        </named-connections>
        <relation join='inner' type='join'>
          <clause type='join'>
            <expression op='='>
              <expression op='[Custom SQL Query].[region]' />
              <expression op='[Targets].[region]' />
            </expression>
          </clause>
          <relation connection='postgres.1abc' name='Custom SQL Query' type='text'>{sql}</relation>
          <relation connection='postgres.1abc' name='Targets' table='[public].[Targets]' type='table' />
        </relation>
        <metadata-records>{md}</metadata-records>
      </connection>
      {calcs}
      <extract count='-1' enabled='true' units='records'><connection class='hyper' dbname='Data/Extracts/finance.hyper' /></extract>
    </datasource>"""

    def r(inst):
        return f"[{DS}].[{inst}]"
    sheets = "".join([
        ws("Attainment by Region", D, r("none:region:nk"),
           f"({r('sum:sales:qk')} + {r('sum:sales_target:qk')} + {r('usr:Calculation_F01:qk')})", "Text",
           encodings=f"<text column='{r('usr:Calculation_F01:qk')}' />"),
        ws("Fiscal Year Sales", D, r("none:Calculation_F04:ok"),
           f"({r('sum:sales:qk')} + {r('usr:Calculation_F02:qk')} + {r('usr:Calculation_F05:qk')} + {r('usr:Calculation_F09:qk')})",
           "Text", encodings=f"<text column='{r('sum:sales:qk')}' />",
           deps=f"<column-instance column='[Calculation_F05]' derivation='User' name='[usr:Calculation_F05:qk]' pivot='key' type='quantitative'><table-calc ordering-type='Columns' /></column-instance>"),
        ws("Order Size Mix", D, f"({r('none:Calculation_F06:nk')} / {r('none:segment:nk')})",
           f"({r('cnt:order_id:qk')} + {r('usr:Calculation_F03:qk')} + {r('usr:Calculation_F07:qk')} + {r('usr:Calculation_F10:qk')})",
           "Text", encodings=f"<text column='{r('cnt:order_id:qk')}' />",
           filters=cat_filter(DS, "none:segment:nk", ['"Consumer"', '"Corporate"'])),
        ws("Region Codes", D, r("none:Calculation_F08:nk"), r("sum:profit:qk"), "Bar"),
    ])
    dashboards = f"""
    <dashboards>
      <dashboard name='Finance Summary'>
        <size maxheight='720' maxwidth='1280' minheight='720' minwidth='1280' />
        <zones><zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          {zone(2, 0, 0, 50000, 50000, name='Attainment by Region')}
          {zone(3, 50000, 0, 50000, 50000, name='Fiscal Year Sales')}
          {zone(4, 0, 50000, 100000, 50000, name='Order Size Mix')}
        </zone></zones>
      </dashboard>
    </dashboards>"""
    return (HEADER + f"  <datasources>{params}{datasource}\n  </datasources>\n  <worksheets>{sheets}\n  </worksheets>"
            + dashboards + "\n</workbook>\n")


def hr_twb():
    DS = "federated.hr01csvsource"
    D = (DS, "Employees")
    md = "".join([
        meta("employee_id", "Employee ID", "employees.csv", "string", RT["string"], "Count"),
        meta("employee_name", "Employee Name", "employees.csv", "string", RT["string"], "Count"),
        meta("department", "Department", "employees.csv", "string", RT["string"], "Count"),
        meta("job_level", "Job Level", "employees.csv", "string", RT["string"], "Count"),
        meta("gender", "Gender", "employees.csv", "string", RT["string"], "Count"),
        meta("location", "Location", "employees.csv", "string", RT["string"], "Count"),
        meta("hire_date", "Hire Date", "employees.csv", "date", RT["date"], "Year"),
        meta("termination_date", "Termination Date", "employees.csv", "date", RT["date"], "Year", True),
        meta("salary", "Salary", "employees.csv", "real", RT["real"], "Sum"),
        meta("performance_score", "Performance Score", "employees.csv", "integer", RT["integer"], "Avg", True),
    ])
    calcs = "".join([
        calc("Calculation_H01", "Headcount", "COUNTD([Employee ID])", "integer"),
        calc("Calculation_H02", "Is Active", "ISNULL([Termination Date])", "boolean", "dimension", "nominal"),
        calc("Calculation_H03", "Attrition Rate",
             "SUM(IF NOT ISNULL([Termination Date]) THEN 1 ELSE 0 END) / COUNTD([Employee ID])", fmt="p1"),
        calc("Calculation_H04", "Tenure Years",
             "DATEDIFF('day', [Hire Date], IFNULL([Termination Date], #2025-12-31#)) / 365.25"),
        calc("Calculation_H05", "Tenure Band",
             "IF [Tenure Years] < 2 THEN '0-2 yrs' ELSEIF [Tenure Years] < 5 THEN '2-5 yrs' ELSE '5+ yrs' END",
             "string", "dimension", "nominal"),
        calc("Calculation_H06", "Dept Avg Salary", "{FIXED [Department] : AVG([Salary])}"),
        calc("Calculation_H07", "Salary vs Dept", "AVG([Salary]) - AVG([Dept Avg Salary])"),
        calc("Calculation_H08", "High Performer", "[Performance Score] >= 4", "boolean", "dimension", "nominal"),
        calc("Calculation_H09", "Hire Year", "YEAR([Hire Date])", "integer", "dimension", "ordinal"),
        calc("Calculation_H10", "Median Salary", "MEDIAN([Salary])"),
        calc("Calculation_H11", "Hires Running Total", "RUNNING_SUM(COUNTD([Employee ID]))", "integer"),
    ])
    datasource = f"""
    <datasource caption='Employees' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='employees' name='textscan.0hr'>
            <connection class='textscan' directory='Data/hr' filename='employees.csv' password='' server='' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.0hr' name='employees.csv' table='[employees#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
            <column datatype='string' name='employee_id' ordinal='0' />
          </columns>
        </relation>
        <metadata-records>{md}</metadata-records>
      </connection>
      {calcs}
    </datasource>"""

    def r(inst):
        return f"[{DS}].[{inst}]"
    sheets = "".join([
        ws("Headcount by Department", D, r("none:Department:nk"),
           f"({r('usr:Calculation_H01:qk')} + {r('usr:Calculation_H03:qk')} + {r('usr:Calculation_H10:qk')})", "Text",
           encodings=f"<text column='{r('usr:Calculation_H01:qk')}' />"),
        ws("Tenure Distribution", D, f"({r('none:Calculation_H05:nk')} / {r('none:Calculation_H02:nk')})",
           f"({r('usr:Calculation_H01:qk')} + {r('avg:Calculation_H04:qk')})", "Text",
           encodings=f"<text column='{r('usr:Calculation_H01:qk')}' />"),
        ws("Salary Benchmark", D, f"({r('none:Department:nk')} / {r('none:Job Level:nk')})",
           f"({r('avg:Salary:qk')} + {r('usr:Calculation_H07:qk')} + {r('avg:Performance Score:qk')})", "Text",
           encodings=f"<text column='{r('avg:Salary:qk')}' />",
           filters=cat_filter(DS, "none:Location:nk", ['"Austin"', '"Seattle"'])),
        ws("Hiring Trend", D, f"({r('usr:Calculation_H01:qk')} + {r('usr:Calculation_H11:qk')})", r("none:Calculation_H09:ok"),
           "Line", deps=f"<column-instance column='[Calculation_H11]' derivation='User' name='[usr:Calculation_H11:qk]' pivot='key' type='quantitative'><table-calc ordering-type='Rows' /></column-instance>"),
        ws("Performance Mix", D, r("none:Calculation_H08:nk"), r("cnt:Employee ID:qk"), "Bar",
           filters=cat_filter(DS, "none:Department:nk", ['"HR"'], exclude=True)),
    ])
    dashboards = f"""
    <dashboards>
      <dashboard name='HR Overview'>
        <size maxheight='800' maxwidth='1200' minheight='800' minwidth='1200' />
        <zones><zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          {zone(2, 0, 0, 50000, 50000, name='Headcount by Department')}
          {zone(3, 50000, 0, 50000, 50000, name='Hiring Trend')}
          {zone(4, 0, 50000, 50000, 50000, name='Salary Benchmark')}
          {zone(5, 50000, 50000, 50000, 50000, name='Tenure Distribution')}
          {zone(6, 80000, 0, 20000, 10000, ztype='filter', param=f'[{DS}].[none:Department:nk]', name='Headcount by Department')}
        </zone></zones>
      </dashboard>
    </dashboards>"""
    return (HEADER + f"  <datasources>{datasource}\n  </datasources>\n  <worksheets>{sheets}\n  </worksheets>"
            + dashboards + "\n</workbook>\n")


def main():
    os.makedirs(WB, exist_ok=True)
    data = build_data()
    write_db(*data)
    with open(os.path.join(WB, "Superstore_Sales.twb"), "w", encoding="utf-8") as f:
        f.write(superstore_twb())
    with open(os.path.join(WB, "Finance_CustomSQL.twb"), "w", encoding="utf-8") as f:
        f.write(finance_twb())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("HR_Analytics.twb", hr_twb())
        z.write(os.path.join(DATA, "csv", "employees.csv"), "Data/hr/employees.csv")
    with open(os.path.join(WB, "HR_Analytics.twbx"), "wb") as f:
        f.write(buf.getvalue())
    # finance showcase (Invoice Payment Health, procure-to-pay)
    import sys
    sys.path.insert(0, HERE)
    import invoice_health
    inv = invoice_health.build()
    con = sqlite3.connect(os.path.join(DATA, "superstore.db"))
    for name, rows in inv.items():
        cols = list(rows[0])
        con.execute(f"DROP TABLE IF EXISTS {name}")
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in rows])
        with open(os.path.join(DATA, "csv", f"{name}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    con.commit()
    con.close()
    with open(os.path.join(WB, "Invoice_Health.twb"), "w", encoding="utf-8") as f:
        f.write(invoice_health.twb(sys.modules[__name__]))
    print("Samples written to", WB)


if __name__ == "__main__":
    main()
