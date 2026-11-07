"""Tableau -> DAX equivalents reference.

Every example is produced by the real translator against a demo star schema
(Orders fact, Customers / Products dimensions), so the catalogue always reflects
what the converter actually generates.
"""
from __future__ import annotations

from functools import lru_cache

from .formula.ast import is_aggregate
from .formula.dax import CalcInfo, ColInfo, DaxTranslator, ParamInfo, Resolver
from .formula.parser import parse_formula


class DemoResolver(Resolver):
    fact_table = "Orders"
    measure_table = "_Measures"
    COLS = {
        "Sales": ("Orders", "real", False), "Profit": ("Orders", "real", False),
        "Quantity": ("Orders", "integer", False), "Discount": ("Orders", "real", True),
        "Order Date": ("Orders", "date", False), "Ship Date": ("Orders", "date", False),
        "Order ID": ("Orders", "string", False), "Customer ID": ("Orders", "string", False),
        "Customer Name": ("Customers", "string", False), "Region": ("Customers", "string", False),
        "Segment": ("Customers", "string", False), "State": ("Customers", "string", False),
        "Category": ("Products", "string", False), "Sub-Category": ("Products", "string", False),
        "Product Name": ("Products", "string", False),
    }
    CALCS = {"Profit Ratio": CalcInfo("measure", "_Measures", "Profit Ratio", "real"),
             "Profit Status": CalcInfo("column", "Orders", "Profit Status", "string")}

    def column(self, name):
        c = self.COLS.get(name)
        return ColInfo(c[0], name, c[1], c[2]) if c else None

    def calc(self, name):
        return self.CALCS.get(name)

    def parameter(self, name):
        return ParamInfo(f"{name} Value", name, name, "real", 0)

    def is_one_side(self, host, other):
        return host == "Orders" and other in ("Customers", "Products")


ENTRIES = [
    # category, tableau formula, notes
    ("Aggregation", "SUM([Sales])", "Explicit measure; implicit measures are avoided (best practice)."),
    ("Aggregation", "AVG([Discount])", "Both ignore NULL/BLANK values."),
    ("Aggregation", "MIN([Order Date])", ""),
    ("Aggregation", "MAX([Sales])", ""),
    ("Aggregation", "COUNT([Order ID])", "COUNT counts non-null values; text columns use COUNTA."),
    ("Aggregation", "COUNTD([Order ID])", "COUNTD ignores NULL -> DISTINCTCOUNTNOBLANK (DISTINCTCOUNT counts BLANK)."),
    ("Aggregation", "COUNTD([Customer Name])", "Dimension-table column: filtered through the fact table to keep Tableau relationship semantics."),
    ("Aggregation", "MEDIAN([Sales])", ""),
    ("Aggregation", "ATTR([Region])", "ATTR returns '*' for several values; SELECTEDVALUE with '*' fallback."),
    ("Aggregation", "STDEV([Sales])", "Sample standard deviation."),
    ("Aggregation", "PERCENTILE([Sales], 0.9)", ""),
    ("Aggregation", "SUM([Sales] * (1 - [Discount]))", "Aggregate of an expression -> iterator (SUMX) with row context."),
    ("Aggregation", "SUM([Profit]) / SUM([Sales])", "Division -> DIVIDE (Tableau returns NULL on divide by zero)."),
    ("Logical", "IF [Profit] > 0 THEN 'Profit' ELSEIF [Profit] = 0 THEN 'Even' ELSE 'Loss' END", "ELSEIF chains -> SWITCH(TRUE(), ...)."),
    ("Logical", "IIF([Sales] > 1000, 'Large', 'Small')", ""),
    ("Logical", "CASE [Region] WHEN 'East' THEN 1 WHEN 'West' THEN 2 ELSE 0 END", ""),
    ("Logical", "IFNULL([Discount], 0)", ""),
    ("Logical", "ZN([Discount])", ""),
    ("Logical", "ISNULL([Discount])", ""),
    ("Logical", "[Region] IN ('East', 'West')", ""),
    ("Logical", "[Quantity] >= 5 AND NOT [Sales] < 100", ""),
    ("Logical", "[Discount] > 0.2", "NULL comparison is NULL in Tableau; the DAX keeps BLANK instead of FALSE."),
    ("String", "LEFT([Customer Name], 3)", ""),
    ("String", "RIGHT([Order ID], 4)", ""),
    ("String", "MID([Customer Name], 2, 5)", ""),
    ("String", "LEN([Customer Name])", ""),
    ("String", "UPPER([Region]) + '-' + LOWER([Segment])", "String '+' -> '&'."),
    ("String", "TRIM([Customer Name])", "DAX TRIM also removes repeated inner spaces."),
    ("String", "REPLACE([Region], 'East', 'E')", "REPLACE -> SUBSTITUTE."),
    ("String", "CONTAINS([Customer Name], 'Smith')", "CONTAINSSTRING is case-insensitive."),
    ("String", "STARTSWITH([Order ID], 'O1')", ""),
    ("String", "FIND([Customer Name], ' ')", "Argument order is swapped in DAX."),
    ("String", "SPLIT([Customer Name], ' ', 2)", "SPLIT -> PATHITEM over a substituted path."),
    ("String", "STR([Quantity])", ""),
    ("Number", "ROUND([Sales], 1)", ""),
    ("Number", "INT([Profit])", "Tableau INT truncates toward zero -> TRUNC (DAX INT rounds down)."),
    ("Number", "ABS([Profit])", ""),
    ("Number", "CEILING([Sales])", ""),
    ("Number", "POWER([Quantity], 2)", ""),
    ("Number", "SQRT([Sales])", ""),
    ("Number", "[Quantity] % 2", "% -> MOD."),
    ("Number", "DIV([Quantity], 2)", "DIV -> QUOTIENT."),
    ("Date", "DATEPART('year', [Order Date])", ""),
    ("Date", "DATEPART('quarter', [Order Date])", ""),
    ("Date", "DATEPART('weekday', [Order Date])", "Sunday = 1 in both."),
    ("Date", "DATENAME('month', [Order Date])", "FORMAT output follows the model culture."),
    ("Date", "DATETRUNC('month', [Order Date])", ""),
    ("Date", "DATETRUNC('quarter', [Order Date])", ""),
    ("Date", "DATETRUNC('week', [Order Date])", "Week starts on Sunday (Tableau default for US locale)."),
    ("Date", "DATEADD('month', 3, [Order Date])", "Scalar date shift -> EDATE (DAX DATEADD is a time-intelligence table function)."),
    ("Date", "DATEADD('day', 7, [Order Date])", ""),
    ("Date", "DATEDIFF('day', [Order Date], [Ship Date])", ""),
    ("Date", "DATEDIFF('month', [Order Date], TODAY())", ""),
    ("Date", "MAKEDATE(2024, 12, 31)", ""),
    ("Date", "[Order Date] >= #2024-01-01#", "Date literal -> DATE()."),
    ("Date", "YEAR([Order Date])", ""),
    ("LOD", "{FIXED [Customer Name] : SUM([Sales])}", "Row-level value attached to every record -> calculated column that removes all filters and keeps the LOD dimensions."),
    ("LOD", "{FIXED : SUM([Sales])}", "Table-scoped LOD (grand total)."),
    ("LOD", "SUM([Sales]) / SUM({FIXED [Region] : SUM([Sales])})", "LOD inside an aggregate -> evaluated per row inside SUMX."),
    ("LOD", "{INCLUDE [Customer Name] : SUM([Sales])}", "Aggregated over the view grain + included dims (SUMX/AVERAGEX over SUMMARIZE)."),
    ("LOD", "{EXCLUDE [Region] : SUM([Sales])}", "ALLSELECTED on the excluded dimension: removes it from the grain, keeps filters on it (Tableau applies dimension filters first)."),
    ("Table calc", "RUNNING_SUM(SUM([Sales]))", "Addressing = worksheet dimension; WINDOW(1, ABS, 0, REL) over ALLSELECTED."),
    ("Table calc", "RUNNING_AVG(SUM([Sales]))", ""),
    ("Table calc", "WINDOW_SUM(SUM([Sales]))", ""),
    ("Table calc", "WINDOW_AVG(SUM([Sales]))", ""),
    ("Table calc", "WINDOW_AVG(SUM([Sales]), -2, 0)", "Moving window -> WINDOW(-2, REL, 0, REL)."),
    ("Table calc", "TOTAL(SUM([Sales]))", ""),
    ("Table calc", "SUM([Sales]) / TOTAL(SUM([Sales]))", "Percent of total."),
    ("Table calc", "RANK(SUM([Sales]))", "Competition ranking (1,2,2,4) -> RANKX ... SKIP."),
    ("Table calc", "RANK_DENSE(SUM([Sales]))", ""),
    ("Table calc", "INDEX()", "-> ROWNUMBER."),
    ("Table calc", "LOOKUP(SUM([Sales]), -1)", "-> OFFSET(-1)."),
    ("Table calc", "SIZE()", ""),
    ("Table calc", "RANK_PERCENTILE(SUM([Sales]))", ""),
    ("Parameter", "SUM([Sales]) * [Parameters].[Growth Rate]", "Parameter -> disconnected table + slicer + SELECTEDVALUE measure."),
    ("Security", "USERNAME()", "Use in row-level security roles (USERPRINCIPALNAME)."),
]

STRUCTURAL = [
    ("Data model", "Relationships (logical layer)", "Star schema relationships (many-to-one, single direction); the hub table becomes the fact."),
    ("Data model", "Physical joins", "Flattened with Table.Join in Power Query (keeps Tableau row duplication) or, optionally, a star schema."),
    ("Data model", "Custom SQL", "Value.NativeQuery with query folding; <Parameters.X> -> Power Query parameters."),
    ("Data model", "Extract (.hyper)", "Import mode from the original source (extracts are not readable by Power BI)."),
    ("Data model", "Data source filter", "Power Query filter step or RLS role (flagged for review)."),
    ("Data model", "Hierarchy (drill path)", "TMDL hierarchy on the table."),
    ("Data model", "Folders / descriptions / default formats", "displayFolder / description / formatString."),
    ("Data model", "Date fields", "Date-part columns per pill (Year, Quarter, Month start ...) + a marked Date table (CALENDARAUTO)."),
    ("Calculations", "Group", "SWITCH(TRUE(), col IN {...}, label, ...) calculated column."),
    ("Calculations", "Bin", "FLOOR(col, size) calculated column."),
    ("Calculations", "Set (members)", "col IN {...} boolean calculated column."),
    ("Calculations", "Set (Top N)", "RANKX(ALL(dim), CALCULATE(measure)) <= N calculated column."),
    ("Parameters", "Range parameter", "GENERATESERIES table (what-if parameter) + value measure."),
    ("Parameters", "List parameter", "Table constructor + single-select slicer."),
    ("Filters", "Categorical (include / exclude)", "Visual-level Basic filter (In / Not In)."),
    ("Filters", "Quantitative on dimension", "Advanced filter (>=, <=)."),
    ("Filters", "Quantitative on measure", "Visual-level measure filter (applied after aggregation, like Tableau)."),
    ("Filters", "Top N", "TopN visual filter by measure. Tableau applies Top N before dimension filters (unless context)."),
    ("Filters", "Relative date", "Relative date filter."),
    ("Filters", "Context filter", "Flagged: FIXED LODs in Tableau respect context filters, ALLEXCEPT does not."),
    ("Dashboards", "Dashboard", "Report page (zones scaled to 1280x720)."),
    ("Dashboards", "Worksheet zone", "Visual mapped from the mark type and shelves."),
    ("Dashboards", "Quick filter / parameter control", "Slicer."),
    ("Dashboards", "Text / title", "Text box."),
    ("Dashboards", "Filter / highlight action", "Cross-filter / cross-highlight interactions."),
    ("Dashboards", "URL / go-to-sheet / parameter action", "Button, drillthrough or slicer (manual step)."),
    ("Visuals", "Bar (dims on rows)", "clusteredBarChart / stackedBarChart (with colour dimension)."),
    ("Visuals", "Bar (dims on columns)", "clusteredColumnChart / stackedColumnChart."),
    ("Visuals", "Line / Area", "lineChart / areaChart; dual axis -> combo chart."),
    ("Visuals", "Text table / crosstab", "tableEx / pivotTable (matrix)."),
    ("Visuals", "Pie", "pieChart."),
    ("Visuals", "Scatter (measure x measure)", "scatterChart."),
    ("Visuals", "Map / filled map", "map / filledMap."),
    ("Visuals", "Highlight table / heat map", "Matrix with conditional formatting."),
    ("Visuals", "KPI (measures only)", "card / multiRowCard."),
]


@lru_cache(maxsize=1)
def catalog() -> dict:
    r = DemoResolver()
    out = []
    for cat, formula, notes in ENTRIES:
        ast = parse_formula(formula)
        agg = is_aggregate(ast, r.calc) or cat == "Table calc"
        tr = DaxTranslator(r)
        addressing = [("Products", "Category")] if cat == "Table calc" else None
        from .formula.ast import LOD
        if isinstance(ast, LOD) and ast.kind in ("INCLUDE", "EXCLUDE"):
            res, agg = tr.lod_measure(ast, "sum"), True
        else:
            res = tr.translate(ast, "agg" if agg else "row", host="Orders", addressing=addressing)
        out.append({"category": cat, "tableau": formula, "dax": res.expression,
                    "object": "Measure" if agg else "Calculated column",
                    "notes": "; ".join(filter(None, [notes] + res.warnings)), "status": res.status})
    return {"functions": out,
            "structural": [{"area": a, "tableau": t, "powerbi": p} for a, t, p in STRUCTURAL],
            "schema": "Demo schema: Orders (fact: Sales, Profit, Quantity, Discount*, Order Date, Ship Date, Order ID) "
                      "-> Customers (Customer Name, Region, Segment, State), Products (Category, Sub-Category). "
                      "* nullable. Table calculations address Products[Category]."}
