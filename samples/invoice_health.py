"""Finance showcase: 'Invoice Payment Health' (procure-to-pay) sample.

Star schema, from the build guide (section 8):
  FactInvoices          one row per invoice
  DimVendor             one row per vendor
  DimDate               one row per date (fiscal year starts in April)
  DimEmployeeSecurity   one row per AP employee with the manager's UPN (row-level security)

The workbook exercises the guide's three translation challenges (total, on-time paid
amount with NULL payment dates, vendor-level FIXED LOD), a parameter-driven metric
('Late invoices'), Top N, an aging matrix, and a USERNAME() data source filter that
must become a Power BI RLS role.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

MANAGERS = ["anna.lee@contoso.com", "raj.patel@contoso.com", "li.wong@contoso.com"]
REGIONS = ["North", "South", "East", "West"]
CATEGORIES = ["IT", "Facilities", "Logistics", "Marketing", "Professional Services"]
AS_OF = date(2025, 12, 31)


def build(seed=7):
    rnd = random.Random(seed)
    vendors = [{"vendor_key": f"V{i:03d}", "vendor_name": f"{rnd.choice(['Apex', 'Blue', 'Nordic', 'Summit', 'Vertex', 'Quantum', 'Harbor', 'Pioneer'])} "
                f"{rnd.choice(['Supplies', 'Systems', 'Partners', 'Logistics', 'Services', 'Labs'])} {i}",
                "category": rnd.choice(CATEGORIES), "region": REGIONS[i % 4]} for i in range(1, 41)]
    employees = [{"employee_key": f"E{i:02d}", "employee_name": f"AP Clerk {i}", "email_upn": f"clerk{i:02d}@contoso.com",
                  "manager_upn": MANAGERS[i % 3], "role": "AP Clerk"} for i in range(1, 13)]
    days = [date(2024, 1, 1) + timedelta(days=k) for k in range((date(2025, 12, 31) - date(2024, 1, 1)).days + 1)]
    dates = []
    for d in days:
        fy = d.year + 1 if d.month >= 4 else d.year
        period = (d.month - 4) % 12 + 1
        dates.append({"date_key": int(d.strftime("%Y%m%d")), "date": d.isoformat(), "month": d.strftime("%Y-%m"),
                      "fiscal_period": f"FY{fy % 100:02d} P{period:02d}"})
    invoices = []
    for i in range(1, 1501):
        v = rnd.choice(vendors)
        e = rnd.choice(employees)
        inv = date(2024, 1, 1) + timedelta(days=rnd.randint(0, 700))
        due = inv + timedelta(days=rnd.choice([30, 30, 45, 60]))
        r = rnd.random()
        if r < 0.12 and due > date(2025, 9, 1) or r < 0.04:
            pay, status = None, rnd.choice(["Open", "Open", "Disputed"])
        else:
            pay = due + timedelta(days=rnd.choice([-10, -5, -2, 0, 0, 3, 8, 15, 25, 40]))
            status = "Paid"
        amount = round(rnd.lognormvariate(8.2, 0.9), 2)
        invoices.append({"invoice_id": f"INV{i:05d}", "vendor_key": v["vendor_key"], "employee_key": e["employee_key"],
                         "date_key": int(inv.strftime("%Y%m%d")), "invoice_date": inv.isoformat(),
                         "due_date": due.isoformat(), "payment_date": pay.isoformat() if pay else None,
                         "invoice_amount": amount, "status": status, "paid_flag": 1 if pay else 0})
    return {"FactInvoices": invoices, "DimVendor": vendors, "DimEmployeeSecurity": employees, "DimDate": dates}


def twb(h):
    """Workbook XML. ``h`` is the helper namespace of generate_samples."""
    meta, calc, ws, cat_filter, top_filter, zone, a, RT = (h.meta, h.calc, h.ws, h.cat_filter, h.top_filter,
                                                          h.zone, h.a, h.RT)
    DS = "federated.0inv4health9p2p"
    D = (DS, "Invoice Health")
    cols = [
        ("invoice_id", "Invoice ID", "FactInvoices", "string", "Count", False),
        ("vendor_key", "Vendor Key", "FactInvoices", "string", "Count", False),
        ("employee_key", "Employee Key", "FactInvoices", "string", "Count", False),
        ("date_key", "Date Key", "FactInvoices", "integer", "Count", False),
        ("invoice_date", "Invoice Date", "FactInvoices", "date", "Year", False),
        ("due_date", "Due Date", "FactInvoices", "date", "Year", False),
        ("payment_date", "Payment Date", "FactInvoices", "date", "Year", True),
        ("invoice_amount", "Invoice Amount", "FactInvoices", "real", "Sum", False),
        ("status", "Status", "FactInvoices", "string", "Count", False),
        ("paid_flag", "Paid Flag", "FactInvoices", "boolean", "Count", False),
        ("vendor_key", "Vendor Key (DimVendor)", "DimVendor", "string", "Count", False),
        ("vendor_name", "Vendor Name", "DimVendor", "string", "Count", False),
        ("category", "Vendor Category", "DimVendor", "string", "Count", False),
        ("region", "Region", "DimVendor", "string", "Count", False),
        ("employee_key", "Employee Key (DimEmployeeSecurity)", "DimEmployeeSecurity", "string", "Count", False),
        ("employee_name", "Employee Name", "DimEmployeeSecurity", "string", "Count", False),
        ("email_upn", "Email UPN", "DimEmployeeSecurity", "string", "Count", False),
        ("manager_upn", "Manager UPN", "DimEmployeeSecurity", "string", "Count", False),
        ("role", "Role", "DimEmployeeSecurity", "string", "Count", False),
        ("date_key", "Date Key (DimDate)", "DimDate", "integer", "Count", False),
        ("date", "Calendar Date", "DimDate", "date", "Year", False),
        ("month", "Month", "DimDate", "string", "Count", False),
        ("fiscal_period", "Fiscal Period", "DimDate", "string", "Count", False),
    ]
    md = "".join(meta(r, l, p, t, RT.get(t, 11), agg, n) for r, l, p, t, agg, n in cols)
    calcs = "".join([
        calc("Calculation_I01", "Total Invoice Amount", "SUM([Invoice Amount])", fmt='c"$"#,##0'),
        calc("Calculation_I02", "On Time Paid Amount",
             "SUM(IF [Paid Flag] = TRUE AND [Payment Date] <= [Due Date]\nTHEN [Invoice Amount] ELSE 0 END)", fmt='c"$"#,##0'),
        calc("Calculation_I03", "Paid Amount", "SUM(IF [Paid Flag] THEN [Invoice Amount] END)", fmt='c"$"#,##0'),
        calc("Calculation_I04", "On-time %", "[On Time Paid Amount] / [Paid Amount]", fmt="p0.0%"),
        calc("Calculation_I05", "Days Late", "DATEDIFF('day', [Due Date], [Payment Date])", "integer"),
        calc("Calculation_I06", "Payment Status",
             "IF ISNULL([Payment Date]) THEN 'Unpaid'\nELSEIF [Payment Date] <= [Due Date] THEN 'On time'\nELSE 'Late'\nEND",
             "string", "dimension", "nominal"),
        calc("Calculation_I07", "Aging Bucket",
             "IF ISNULL([Payment Date]) THEN 'Unpaid' ELSEIF [Days Late] <= 0 THEN 'Not late' ELSEIF [Days Late] <= 15 "
             "THEN '1-15 days' ELSEIF [Days Late] <= 30 THEN '16-30 days' ELSE '30+ days' END",
             "string", "dimension", "nominal"),
        calc("Calculation_I08", "Vendor Fixed Amount", "{ FIXED [Vendor Name] : SUM([Invoice Amount]) }"),
        calc("Calculation_I09", "Overdue Invoices",
             "SUM(IF ISNULL([Payment Date]) AND [Due Date] < #2025-12-31# THEN 1 ELSE 0 END)", "integer"),
        calc("Calculation_I10", "Invoice Count", "COUNTD([Invoice ID])", "integer"),
        calc("Calculation_I11", "Avg Days to Pay", "AVG(DATEDIFF('day', [Invoice Date], [Payment Date]))"),
        calc("Calculation_I12", "Selected Metric Amount",
             "CASE [Parameters].[Status Selector]\nWHEN 'Late invoices' THEN SUM(IF [Payment Status] = 'Late' THEN [Invoice Amount] END)\n"
             "WHEN 'On time' THEN [On Time Paid Amount]\nELSE SUM([Invoice Amount])\nEND", fmt='c"$"#,##0'),
        calc("Calculation_I13", "Manager Security", "[Manager UPN] = USERNAME()", "boolean", "dimension", "nominal"),
        calc("Calculation_I14", "Vendor Share of Region", "SUM([Invoice Amount]) / TOTAL(SUM([Invoice Amount]))", fmt="p1"),
    ])
    params = """
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <column caption='Status Selector' datatype='string' name='[Status Selector]' param-domain-type='list' role='dimension' type='nominal' value='&quot;All&quot;'>
        <calculation class='tableau' formula='&quot;All&quot;' />
        <members><member value='&quot;All&quot;' /><member value='&quot;Late invoices&quot;' /><member value='&quot;On time&quot;' /></members>
      </column>
    </datasource>"""
    conn = "sqlserver.0financedw"
    objs = [("FactInvoices", "FI"), ("DimVendor", "DV"), ("DimEmployeeSecurity", "DE"), ("DimDate", "DD")]
    objects = "".join(f"<object caption='{n}' id='{i}_obj'><properties context=''><relation connection='{conn}' name='{n}' "
                      f"table='[dbo].[{n}]' type='table' /></properties></object>" for n, i in objs)

    def rel(a_, b_, oid):
        return (f"<relationship><expression op='='><expression op='[{a_}]' /><expression op='[{b_}]' /></expression>"
                f"<first-end-point object-id='FI_obj' /><second-end-point object-id='{oid}_obj' /></relationship>")
    rels = (rel("Vendor Key", "Vendor Key (DimVendor)", "DV") + rel("Employee Key", "Employee Key (DimEmployeeSecurity)", "DE")
            + rel("Date Key", "Date Key (DimDate)", "DD"))
    ds = f"""
    <datasource caption='Invoice Health' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='FinanceDW' name='{conn}'>
            <connection authentication='sspi' class='sqlserver' dbname='FinanceDW' schema='dbo' server='findw-sql.contoso.local' />
          </named-connection>
        </named-connections>
        <_.fcp.ObjectModelEncapsulateLegacy.false...relation type='collection'>
          {''.join(f"<relation connection='{conn}' name='{n}' table='[dbo].[{n}]' type='table' />" for n, _ in objs)}
        </_.fcp.ObjectModelEncapsulateLegacy.false...relation>
        <metadata-records>{md}</metadata-records>
      </connection>
      <column datatype='real' name='[Invoice Amount]' role='measure' type='quantitative' default-format='c&quot;$&quot;#,##0.00'>
        <desc><formatted-text><run>Invoice value in reporting currency</run></formatted-text></desc>
      </column>
      <column datatype='string' name='[Email UPN]' role='dimension' type='nominal' hidden='true' />
      {calcs}
      <drill-paths><drill-path name='Vendor Hierarchy'><field>[Region]</field><field>[Vendor Category]</field><field>[Vendor Name]</field></drill-path></drill-paths>
      <filter class='categorical' column='[{DS}].[none:Calculation_I13:nk]'>
        <groupfilter function='member' level='[none:Calculation_I13:nk]' member='true' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
      </filter>
      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>{objects}</objects>
        <relationships>{rels}</relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>"""

    def r(inst):
        return f"[{DS}].[{inst}]"
    sheets = "".join([
        ws("KPI Overview", D, "", "", "Text",
           encodings=(f"<text column='{r('usr:Calculation_I01:qk')}' /><text column='{r('usr:Calculation_I04:qk')}' />"
                      f"<text column='{r('usr:Calculation_I09:qk')}' /><text column='{r('usr:Calculation_I10:qk')}' />")),
        ws("Invoice Amount by Vendor", D, r("none:Vendor Name:nk"), r("usr:Calculation_I01:qk"), "Bar",
           filters=top_filter(DS, "none:Vendor Name:nk", "10", "SUM([Invoice Amount])"),
           sorts=f"<computed-sort column='{r('none:Vendor Name:nk')}' direction='DESC' using='{r('usr:Calculation_I01:qk')}' />"),
        ws("Monthly Payment Trend", D, f"({r('usr:Calculation_I01:qk')} + {r('usr:Calculation_I02:qk')})",
           r("tmn:Invoice Date:qk"), "Line"),
        ws("Aging by Region", D, r("none:Region:nk"), r("none:Calculation_I07:nk"), "Text",
           encodings=f"<text column='{r('sum:Invoice Amount:qk')}' />"),
        ws("Vendor Category Performance", D, r("none:Vendor Category:nk"),
           f"({r('usr:Calculation_I01:qk')} + {r('usr:Calculation_I04:qk')} + {r('usr:Calculation_I11:qk')} + {r('avg:Calculation_I05:qk')})",
           "Text", encodings=f"<text column='{r('usr:Calculation_I01:qk')}' />"),
        ws("Payment Status Mix", D, "", "", "Pie",
           encodings=f"<color column='{r('none:Calculation_I06:nk')}' /><size column='{r('sum:Invoice Amount:qk')}' />"),
        ws("Selected Status by Region", D, r("none:Region:nk"), r("usr:Calculation_I12:qk"), "Bar"),
        ws("Vendor Concentration", D, f"({r('none:Region:nk')} / {r('none:Vendor Name:nk')})",
           f"({r('max:Calculation_I08:qk')} + {r('usr:Calculation_I14:qk')})", "Text",
           encodings=f"<text column='{r('max:Calculation_I08:qk')}' />",
           filters=cat_filter(DS, "none:Vendor Category:nk", ['"IT"']),
           deps=f"<column-instance column='[Calculation_I14]' derivation='User' name='[usr:Calculation_I14:qk]' pivot='key' type='quantitative'><table-calc ordering-type='Field'><order field='[{DS}].[none:Vendor Name:nk]' /></table-calc></column-instance>"),
        ws("Fiscal Period South", D, r("none:Fiscal Period:nk"), r("usr:Calculation_I01:qk"), "Text",
           encodings=f"<text column='{r('usr:Calculation_I01:qk')}' />",
           filters=cat_filter(DS, "none:Region:nk", ['"South"'])),
    ])
    dash = f"""
    <dashboards>
      <dashboard name='Invoice Payment Health'>
        <size maxheight='800' maxwidth='1400' minheight='800' minwidth='1400' />
        <zones><zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          {zone(2, 0, 0, 80000, 8000, ztype='text', text='Invoice Payment Health')}
          {zone(3, 0, 8000, 80000, 18000, name='KPI Overview')}
          {zone(4, 0, 26000, 40000, 37000, name='Invoice Amount by Vendor')}
          {zone(5, 40000, 26000, 40000, 37000, name='Monthly Payment Trend')}
          {zone(6, 0, 63000, 40000, 37000, name='Aging by Region')}
          {zone(7, 40000, 63000, 40000, 37000, name='Payment Status Mix')}
          {zone(8, 80000, 0, 20000, 20000, name='Aging by Region', ztype='filter', param=f'[{DS}].[none:Region:nk]', mode='checkdropdown')}
          {zone(9, 80000, 20000, 20000, 20000, ztype='paramctrl', param='[Parameters].[Status Selector]', mode='compact')}
        </zone></zones>
      </dashboard>
    </dashboards>
    <actions>
      <action caption='Filter by Vendor' name='[ActionI1]'>
        <activation auto-clear='true' type='on-select' />
        <source dashboard='Invoice Payment Health' type='sheet' worksheet='Invoice Amount by Vendor' />
        <command command='tsc:tsl-filter'><param name='target' value='Invoice Payment Health' /></command>
      </action>
    </actions>"""
    return (h.HEADER + f"  <datasources>{params}{ds}\n  </datasources>\n  <worksheets>{sheets}\n  </worksheets>"
            + dash + "\n</workbook>\n")
