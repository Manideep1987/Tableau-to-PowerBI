"""Tableau workbook -> Power BI semantic model + report pages.

The converter produces an in-memory target model (``PbiModel`` / ``PbiPage``) that is
then serialised to a PBIP project (TMDL + PBIR) by :mod:`tableau2pbi.pbip.writer`, and
executed by the validation engines.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .formula.ast import LOD, TABLE_CALCS, Field as FField, Func, contains, is_aggregate
from .formula.dax import (CalcInfo, ColInfo, DaxResult, DaxTranslator, ParamInfo, Resolver,
                          TranslationError, dax_literal, dax_str, q_col, q_measure, q_table)
from .formula.parser import FormulaError, parse_formula
from .mcode import lint_sql, table_query
from .model import (AGG_DERIVATIONS, CalculatedField, Column, Datasource, FieldRef, Filter,
                    Parameter, Workbook, Worksheet)

PBI_TYPES = {"integer": "int64", "real": "double", "string": "string", "date": "dateTime",
             "datetime": "dateTime", "boolean": "boolean", "null": "string"}

AGG_LABEL = {"sum": "Sum", "avg": "Average", "min": "Min", "max": "Max", "cnt": "Count",
             "ctd": "Distinct Count", "med": "Median", "attr": "Attr", "count": "Count",
             "cntd": "Distinct Count", "stdev": "Std Dev", "stdevp": "Std Dev (Pop)",
             "var": "Variance", "varp": "Variance (Pop)"}
AGG_FUNC = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "cnt": "COUNT", "count": "COUNT",
            "ctd": "COUNTD", "cntd": "COUNTD", "med": "MEDIAN", "attr": "ATTR", "stdev": "STDEV",
            "stdevp": "STDEVP", "var": "VAR", "varp": "VARP"}

DATE_PART = {  # derivation -> (suffix, tableau formula template, datatype)
    "yr": ("Year", "DATEPART('year', [{f}])", "integer"),
    "qr": ("Quarter", "DATEPART('quarter', [{f}])", "integer"),
    "mn": ("Month", "DATEPART('month', [{f}])", "integer"),
    "dy": ("Day", "DATEPART('day', [{f}])", "integer"),
    "wk": ("Week", "DATEPART('week', [{f}])", "integer"),
    "tyr": ("Year Start", "DATETRUNC('year', [{f}])", "date"),
    "tqr": ("Quarter Start", "DATETRUNC('quarter', [{f}])", "date"),
    "tmn": ("Month Start", "DATETRUNC('month', [{f}])", "date"),
    "my": ("Month Start", "DATETRUNC('month', [{f}])", "date"),
    "twk": ("Week Start", "DATETRUNC('week', [{f}])", "date"),
    "tdy": ("Date", "DATETRUNC('day', [{f}])", "date"),
}


# ------------------------------------------------------------ target model --
@dataclass
class PbiColumn:
    name: str
    datatype: str
    source_column: Optional[str] = None
    expression: Optional[str] = None          # calculated column DAX
    summarize_by: str = "none"
    hidden: bool = False
    format_string: Optional[str] = None
    description: Optional[str] = None
    folder: Optional[str] = None
    tableau_ref: Optional[str] = None
    nullable: bool = False


@dataclass
class PbiMeasure:
    name: str
    expression: str
    format_string: Optional[str] = None
    description: Optional[str] = None
    folder: Optional[str] = None
    tableau_ref: Optional[str] = None
    datatype: str = "real"


@dataclass
class PbiTable:
    name: str
    kind: str                                  # fact|dim|parameter|measures|date|single
    columns: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    m_expression: Optional[str] = None
    dax_expression: Optional[str] = None       # calculated table source
    hierarchies: list = field(default_factory=list)
    hidden: bool = False
    source_table: Optional[str] = None         # physical table for validation binding
    source_sql: Optional[str] = None
    datasource: Optional[str] = None
    param_values: Optional[list] = None
    join_plan: Optional[dict] = None
    source_relation: object = None
    source_conn: object = None
    source_spec: Optional[list] = None
    param_default: object = None

    def column(self, name) -> Optional[PbiColumn]:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class PbiRelationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cross_filter: str = "oneDirection"
    active: bool = True
    tableau_join: str = "relationship"


@dataclass
class PbiModel:
    name: str
    tables: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    expressions: list = field(default_factory=list)   # (name, M expression, description)
    roles: list = field(default_factory=list)         # list[PbiRole]

    def table(self, name) -> Optional[PbiTable]:
        return next((t for t in self.tables if t.name == name), None)

    def measure(self, name) -> Optional[PbiMeasure]:
        for t in self.tables:
            for m in t.measures:
                if m.name == name:
                    return m
        return None

    def all_measures(self):
        return [m for t in self.tables for m in t.measures]


@dataclass
class PbiFilter:
    kind: str            # categorical|range|relative-date|topn|measure-range
    table: Optional[str] = None
    column: Optional[str] = None
    measure: Optional[str] = None
    values: list = field(default_factory=list)
    exclude: bool = False
    min: object = None
    max: object = None
    include_null: bool = False
    top_n: Optional[int] = None
    top_direction: str = "top"
    by_measure: Optional[str] = None
    period_type: Optional[str] = None
    first_period: Optional[int] = None
    last_period: Optional[int] = None
    note: Optional[str] = None


@dataclass
class PbiVisual:
    name: str
    visual_type: str
    worksheet: Optional[str] = None
    title: Optional[str] = None
    roles: dict = field(default_factory=dict)          # role -> list of ("column"|"measure", table, name)
    filters: list = field(default_factory=list)
    x: float = 0
    y: float = 0
    w: float = 400
    h: float = 300
    text: Optional[str] = None
    sort: Optional[dict] = None
    notes: list = field(default_factory=list)

    @property
    def group_columns(self):
        out = []
        for role, items in self.roles.items():
            for kind, t, n in items:
                if kind == "column" and (t, n) not in out:
                    out.append((t, n))
        return out

    @property
    def measures(self):
        out = []
        for role, items in self.roles.items():
            for kind, t, n in items:
                if kind == "measure" and n not in out:
                    out.append(n)
        return out


@dataclass
class PbiPage:
    name: str
    display_name: str
    width: int = 1280
    height: int = 720
    visuals: list = field(default_factory=list)
    filters: list = field(default_factory=list)
    source: str = "dashboard"          # dashboard|worksheet


@dataclass
class CalcMapping:
    datasource: str
    tableau_name: str
    tableau_formula: str
    kind: str                           # measure|column|lod-measure|group|bin|set|base-measure|date-part|parameter
    pbi_object: str
    pbi_table: str
    dax: str
    status: str
    warnings: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    xml_path: Optional[str] = None
    strategy: str = ""                  # deterministic rule (or override / ai) that produced the DAX
    confidence: str = "high"           # high | medium | low
    tableau_id: str = ""
    overridden: bool = False
    original_dax: Optional[str] = None
    override_note: Optional[str] = None

    @property
    def classification(self) -> str:
        """PASS / REVIEW / UNSUPPORTED (build guide, translation workflow step 05)."""
        if self.status in ("Failed",) or (self.status == "Manual review" and self.confidence == "low"):
            return "UNSUPPORTED"
        if self.kind == "rls-role":
            return "REVIEW"            # security always needs live-identity acceptance
        if self.status == "Converted" and not self.overridden:
            return "PASS"
        return "REVIEW"


STRATEGY = {"measure": "rule:aggregate-measure", "column": "rule:row-level-calculated-column",
            "group": "rule:group-switch-in", "bin": "rule:bin-floor", "set": "rule:set-membership",
            "base-measure": "rule:explicit-base-measure", "date-part": "rule:date-part-column",
            "parameter": "rule:parameter-table+selectedvalue", "rls-role": "rule:user-filter-to-rls-role"}


@dataclass
class PbiRole:
    name: str
    table_permissions: dict                 # table -> DAX filter expression
    tableau_ref: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ConversionResult:
    workbook: Workbook
    model: PbiModel
    pages: list
    calc_mappings: list
    issues: list                        # dict(severity, area, object, message)
    worksheet_visuals: dict             # worksheet name -> PbiVisual
    field_map: dict                     # (ds, tableau field) -> (table, column) or measure
    sql_reviews: list

    @property
    def stats(self):
        st = Counter(m.status for m in self.calc_mappings if m.kind not in ("base-measure", "date-part"))
        return {
            "datasources": len(self.workbook.datasources),
            "tables": len([t for t in self.model.tables if t.kind in ("fact", "dim", "single")]),
            "columns": sum(len([c for c in t.columns if c.expression is None]) for t in self.model.tables
                           if t.kind in ("fact", "dim", "single")),
            "relationships": len(self.model.relationships),
            "calculations": sum(len(ds.calculations) for ds in self.workbook.datasources),
            "parameters": len(self.workbook.parameters),
            "worksheets": len(self.workbook.worksheets),
            "dashboards": len(self.workbook.dashboards),
            "filters": sum(len(w.filters) for w in self.workbook.worksheets),
            "measures": len(self.model.all_measures()),
            "calc_columns": sum(len([c for c in t.columns if c.expression]) for t in self.model.tables),
            "pages": len(self.pages),
            "visuals": sum(len(p.visuals) for p in self.pages),
            "converted": st.get("Converted", 0),
            "converted_with_notes": st.get("Converted with notes", 0),
            "manual": st.get("Manual review", 0) + st.get("Failed", 0),
        }


def tidy(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("[", "(").replace("]", ")")).strip()


def tableau_format_to_pbi(fmt: Optional[str]) -> Optional[str]:
    if not fmt:
        return None
    if fmt.startswith("p"):
        dec = fmt.count("0", fmt.find(".")) if "." in fmt else 0
        return "0" + ("." + "0" * dec if dec else "") + "%"
    if fmt.startswith("c"):
        dec = 2 if ".00" in fmt else 0
        return "\\$#,0" + ("." + "0" * dec if dec else "") + ";(\\$#,0" + ("." + "0" * dec if dec else "") + ");\\$#,0"
    if fmt.startswith("n"):
        dec = len(fmt.split(".")[1].rstrip(";").split(";")[0]) if "." in fmt else 0
        return "#,0" + ("." + "0" * dec if dec else "")
    return None


# ------------------------------------------------------------- resolver --
class _DsResolver(Resolver):
    def __init__(self, conv: "Converter", ds: Datasource):
        self.conv = conv
        self.ds = ds
        self.fact_table = conv.fact_of[ds.name]
        self.measure_table = conv.measure_table

    def column(self, name):
        key = self.conv.col_lookup.get((self.ds.name, name))
        if key is None:
            # match by caption
            for cname, col in self.ds.columns.items():
                if col.caption == name:
                    key = self.conv.col_lookup.get((self.ds.name, cname))
                    break
        if key is None:
            return None
        t, c = key
        pc = self.conv.model.table(t).column(c)
        return ColInfo(t, c, self.conv.tableau_type(pc.datatype), pc.nullable)

    def calc(self, name):
        info = self.conv.calc_info.get((self.ds.name, name))
        if info is None:
            for cf in self.ds.calculations.values():
                if cf.caption == name:
                    return self.conv.calc_info.get((self.ds.name, cf.internal_name))
        return info

    def parameter(self, name):
        p = self.conv.wb.parameters.get(name)
        if p is None:
            p = next((x for x in self.conv.wb.parameters.values() if x.caption == name), None)
        if p is None:
            return None
        return ParamInfo(f"{p.caption} Value", p.caption, p.caption, p.datatype, p.current_value)

    def is_one_side(self, host, other):
        return self.conv.reachable_one_side(host, other)


# ------------------------------------------------------------ converter --
class Converter:
    def __init__(self, wb: Workbook, join_strategy: str = "flatten"):
        """``join_strategy`` for legacy (physical) joins: ``flatten`` keeps Tableau's
        joined-table semantics (numbers match); ``star`` builds related tables."""
        self.join_strategy = join_strategy
        self.flattened: set = set()
        self.wb = wb
        self.model = PbiModel(wb.name)
        self.measure_table = "_Measures"
        self.issues: list[dict] = []
        self.calc_mappings: list[CalcMapping] = []
        self.col_lookup: dict = {}       # (ds, tableau col name) -> (table, col)
        self.calc_info: dict = {}        # (ds, internal) -> CalcInfo
        self.calc_ast: dict = {}
        self.fact_of: dict = {}
        self.many_to_one: dict = defaultdict(set)   # table -> set(one-side tables)
        self.addressing: dict = {}       # (ds, calc) -> addressing [(t, c)]
        self.sql_reviews: list = []
        self.field_map: dict = {}
        self.lod_measures: set = set()

    # -------------------------------------------------------------- api --
    def convert(self) -> ConversionResult:
        self.model.tables.append(PbiTable(self.measure_table, "measures",
                                          columns=[PbiColumn("Measures", "string", hidden=True,
                                                             expression=None)],
                                          dax_expression='ROW("Measures", BLANK())'))
        for ds in self.wb.datasources:
            self._tables(ds)
        for ds in self.wb.datasources:
            self._relationships(ds)
        self._parameters()
        for ds in self.wb.datasources:
            self._classify_calcs(ds)
        self._date_parts_and_addressing()
        for ds in self.wb.datasources:
            self._translate_calcs(ds)
        self._date_table()
        visuals = {ws.name: self._visual(ws) for ws in self.wb.worksheets}
        pages = self._pages(visuals)
        self._actions()
        for m in self.calc_mappings:
            if not m.strategy:
                m.strategy = STRATEGY.get(m.kind, "rule")
            if m.status == "Failed":
                m.confidence = "low"
        for w in self.wb.warnings:
            self.issue("info", "Workbook", self.wb.name, w)
        return ConversionResult(self.wb, self.model, pages, self.calc_mappings, self.issues,
                                visuals, self.field_map, self.sql_reviews)

    def issue(self, severity, area, obj, message):
        self.issues.append({"severity": severity, "area": area, "object": obj, "message": message})

    @staticmethod
    def tableau_type(pbi_type):
        return {"int64": "integer", "double": "real", "string": "string", "dateTime": "date",
                "boolean": "boolean"}.get(pbi_type, "string")

    # ----------------------------------------------------------- tables --
    def _table_name(self, ds: Datasource, name: str) -> str:
        base = tidy(name)
        if base.lower() in ("custom sql query",):
            base = f"{ds.caption} SQL"
        existing = {t.name for t in self.model.tables}
        if base in existing:
            base = f"{base} ({ds.caption})"
        return base

    def _local_col(self, ds, table, column):
        for cname, c in ds.columns.items():
            if c.table == table and (cname == column or c.remote_name == column or
                                     cname == f"{column} ({table})"):
                return cname
        return column if column in ds.columns else None

    def _flat_table(self, ds: Datasource):
        """Legacy physical joins -> one Power Query merged table (Tableau semantics)."""
        conn_by_name = {c.name: c for c in ds.connections}
        default_conn = ds.connections[0] if ds.connections else None
        pname = self._table_name(ds, ds.caption)
        tbl = PbiTable(pname, "single", datasource=ds.name)
        server_p = db_p = None
        if default_conn and default_conn.server:
            server_p = self._m_param(f"{ds.caption} Server", default_conn.server)
        if default_conn and default_conn.dbname:
            db_p = self._m_param(f"{ds.caption} Database", default_conn.dbname)
        steps, leaves = [], []
        for i, lt in enumerate(ds.tables):
            spec = []
            for cname, col in ds.columns.items():
                if col.table != lt.name:
                    continue
                summarize = "none"
                if col.role == "measure" and col.datatype in ("integer", "real"):
                    summarize = {"Sum": "sum", "Avg": "average", "Min": "min", "Max": "max"}.get(
                        col.aggregation or "Sum", "sum")
                tbl.columns.append(PbiColumn(cname, PBI_TYPES.get(col.datatype, "string"), source_column=cname,
                                             summarize_by=summarize, hidden=col.hidden,
                                             format_string=tableau_format_to_pbi(col.default_format),
                                             description=col.description, folder=col.folder,
                                             tableau_ref=cname, nullable=col.contains_null))
                spec.append((col.remote_name or cname, cname, col.datatype))
                self.col_lookup[(ds.name, cname)] = (pname, cname)
                if col.caption:
                    self.col_lookup.setdefault((ds.name, col.caption), (pname, cname))
            rel = lt.relation
            conn = conn_by_name.get(rel.connection) or default_conn
            m, notes = table_query(conn, rel, spec, self._param_lookup,
                                   server_p and f'#"{server_p}"', db_p and f'#"{db_p}"')
            for n in notes:
                self.issue("info", "Power Query", pname, n)
            steps.append(f"T{i} = " + m.replace("\n", "\n    "))
            leaves.append({"name": lt.name, "source_table": (re.findall(r"\[([^\]]+)\]", rel.table or "")[-1:]
                                                             or [lt.name])[0] if rel.type == "table" else None,
                           "sql": rel.sql, "columns": spec, "relation": rel, "conn": conn})
            if rel.sql:
                self.sql_reviews.append({"table": pname, "datasource": ds.caption, "sql": rel.sql, "m": m,
                                         "findings": lint_sql(rel.sql)})
        joins = []
        index = {lt.name: i for i, lt in enumerate(ds.tables)}
        current = "T0"
        joined = {ds.tables[0].name} if ds.tables else set()
        kinds = {"inner": "JoinKind.Inner", "left": "JoinKind.LeftOuter", "right": "JoinKind.RightOuter",
                 "full": "JoinKind.FullOuter"}
        for k, r in enumerate(ds.relationships):
            lc = self._local_col(ds, r.left_table, r.left_column)
            rc = self._local_col(ds, r.right_table, r.right_column)
            other = r.right_table if r.left_table in joined else r.left_table
            if r.left_table not in joined:
                lc, rc = rc, lc
            joins.append({"left_column": lc, "right_leaf": other, "right_column": rc,
                          "type": r.join_type or "inner"})
            steps.append(f"J{k} = Table.Join({current}, {{\"{lc}\"}}, T{index.get(other, 0)}, {{\"{rc}\"}}, "
                         f"{kinds.get(r.join_type, 'JoinKind.Inner')})")
            current = f"J{k}"
            joined.add(other)
        tbl.m_expression = "let\n    " + ",\n    ".join(steps) + f"\nin\n    {current}"
        tbl.join_plan = {"leaves": leaves, "joins": joins}
        self.model.tables.append(tbl)
        self.fact_of[ds.name] = pname
        self.flattened.add(ds.name)
        self.issue("info", "Data source", ds.caption,
                   f"Physical join of {len(ds.tables)} tables flattened into '{pname}' with Table.Join to keep "
                   f"Tableau join semantics (row duplication included). Consider refactoring to a star schema.")

    def _tables(self, ds: Datasource):
        if ds.model_style == "joins" and self.join_strategy == "flatten":
            return self._flat_table(ds)
        conn_by_name = {c.name: c for c in ds.connections}
        default_conn = ds.connections[0] if ds.connections else None
        table_names = {}
        for lt in ds.tables:
            table_names[lt.name] = self._table_name(ds, lt.name)
        # assign columns without a table
        if len(ds.tables) == 1:
            for col in ds.columns.values():
                if col.table is None or col.table not in table_names:
                    col.table = ds.tables[0].name
        server_p = db_p = None
        if default_conn and default_conn.server:
            server_p = self._m_param(f"{ds.caption} Server", default_conn.server)
        if default_conn and default_conn.dbname:
            db_p = self._m_param(f"{ds.caption} Database", default_conn.dbname)
        for lt in ds.tables:
            pname = table_names[lt.name]
            tbl = PbiTable(pname, "single" if len(ds.tables) == 1 else "dim", datasource=ds.name)
            cols_spec = []
            for cname, col in ds.columns.items():
                if col.table != lt.name:
                    continue
                pcol = re.sub(r"\s\(" + re.escape(lt.name) + r"\)$", "", cname)
                pcol = re.sub(r"\s\(" + re.escape(pname) + r"\)$", "", pcol)
                if tbl.column(pcol):
                    pcol = cname
                summarize = "none"
                if col.role == "measure" and col.datatype in ("integer", "real"):
                    summarize = {"Sum": "sum", "Avg": "average", "Min": "min", "Max": "max",
                                 "Count": "count", "CountD": "distinctCount"}.get(col.aggregation or "Sum", "sum")
                pc = PbiColumn(pcol, PBI_TYPES.get(col.datatype, "string"),
                               source_column=pcol, summarize_by=summarize, hidden=col.hidden,
                               format_string=tableau_format_to_pbi(col.default_format),
                               description=col.description, folder=col.folder,
                               tableau_ref=cname, nullable=col.contains_null)
                tbl.columns.append(pc)
                cols_spec.append((col.remote_name or cname, pcol, col.datatype))
                self.col_lookup[(ds.name, cname)] = (pname, pcol)
                if col.caption:
                    self.col_lookup.setdefault((ds.name, col.caption), (pname, pcol))
            rel = lt.relation
            conn = conn_by_name.get(rel.connection) or default_conn
            tbl.source_table = (rel.table and re.findall(r"\[([^\]]+)\]", rel.table)[-1:] or [lt.name])[0] \
                if rel.type == "table" else None
            tbl.source_sql = rel.sql
            tbl.source_relation, tbl.source_conn, tbl.source_spec = rel, conn, cols_spec
            if rel.type == "join":
                self.issue("warning", "Data source", lt.name,
                           "Logical table built from a physical join; converted using the first leaf table.")
            if conn is not None:
                try:
                    m, notes = table_query(conn, rel, cols_spec, self._param_lookup,
                                           server_p and f'#"{server_p}"', db_p and f'#"{db_p}"')
                except Exception as e:      # pragma: no cover - defensive
                    m, notes = "#table({}, {})", [f"Power Query generation failed: {e}"]
                tbl.m_expression = m
                for n in notes:
                    self.issue("info", "Power Query", pname, n)
            if rel.sql:
                review = lint_sql(rel.sql)
                self.sql_reviews.append({"table": pname, "datasource": ds.caption, "sql": rel.sql,
                                         "m": tbl.m_expression, "findings": review})
            self.model.tables.append(tbl)
            for h in ds.hierarchies:
                levels = [self.col_lookup.get((ds.name, lv)) for lv in h.levels]
                if levels and all(l and l[0] == pname for l in levels):
                    tbl.hierarchies.append((h.name, [l[1] for l in levels]))
                elif levels and all(levels) and h.levels and levels[0][0] == pname:
                    self.issue("info", "Hierarchy", h.name,
                               "Hierarchy spans several tables; created on the first table only.")
        if ds.is_extract:
            self.issue("info", "Data source", ds.caption,
                       "Data source uses a Tableau extract; Power BI imports from the live source instead.")
        for f in ds.datasource_filters:
            cf = ds.calculations.get(f.field.field)
            if cf is not None and cf.extra.get("security"):
                continue        # user filter -> RLS role (see _translate_calcs)
            self.issue("warning", "Data source", ds.caption,
                       f"Data source filter on [{f.field.field}] must be applied in Power Query or RLS.")

    def _m_param(self, name, value):
        if not any(e[0] == name for e in self.model.expressions):
            self.model.expressions.append(
                (name, f'"{value}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
                 "Converted from Tableau connection"))
        return name

    def _param_lookup(self, name):
        p = self.wb.parameters.get(name) or next(
            (x for x in self.wb.parameters.values() if x.caption == name), None)
        if p is not None:
            mtype = {"integer": "Number", "real": "Number", "date": "Date"}.get(p.datatype, "Text")
            val = p.current_value
            lit = f'"{val}"' if mtype == "Text" else (f"#date({str(val)[:4]}, {int(str(val)[5:7])}, {int(str(val)[8:10])})"
                                                      if mtype == "Date" else str(val))
            if not any(e[0] == p.caption for e in self.model.expressions):
                self.model.expressions.append(
                    (p.caption, f'{lit} meta [IsParameterQuery=true, Type="{mtype}", IsParameterQueryRequired=true]',
                     f"Power Query parameter from Tableau parameter used in custom SQL"))
        return p

    # ---------------------------------------------------- relationships --
    def _relationships(self, ds: Datasource):
        if ds.name in self.flattened:
            return
        rels = ds.relationships
        if not rels:
            self.fact_of[ds.name] = self.col_lookup.get(
                (ds.name, next(iter(ds.columns), "")), (self._table_name_for(ds, 0), None))[0]
            t = self.model.table(self.fact_of[ds.name])
            if t is not None:
                t.kind = "fact" if len(ds.tables) > 1 else "single"
            return
        degree = Counter()
        resolved = []
        for r in rels:
            lt, lc = self._rel_end(ds, r.left_table, r.left_column)
            rt, rc = self._rel_end(ds, r.right_table, r.right_column)
            if not (lt and rt and lc and rc):
                self.issue("warning", "Relationship", f"{r.left_table}-{r.right_table}",
                           "Relationship columns could not be resolved; create it manually.")
                continue
            resolved.append((lt, lc, rt, rc, r.join_type))
            degree[lt] += 1
            degree[rt] += 1
        if not resolved:
            self.fact_of[ds.name] = self._table_name_for(ds, 0)
            return
        # The fact is the table with the most relationships (hub of the star); ties -> left table.
        fact = max(degree, key=lambda t: (degree[t], -[x[0] for x in resolved].index(t)
                                          if t in [x[0] for x in resolved] else -99))
        self.fact_of[ds.name] = fact
        for lt, lc, rt, rc, jt in resolved:
            # many side: fact side / the side closer to the fact
            if rt == fact:
                lt, lc, rt, rc = rt, rc, lt, lc
            self.model.relationships.append(PbiRelationship(lt, lc, rt, rc, tableau_join=jt))
            self.many_to_one[lt].add(rt)
            if jt in ("inner",):
                self.issue("warning", "Relationship", f"{lt} -> {rt}",
                           "Tableau INNER join: fact rows without a matching dimension row are dropped in "
                           "Tableau but kept (blank member) in Power BI. Validation row counts confirm impact.")
            elif jt in ("full", "right"):
                self.issue("warning", "Relationship", f"{lt} -> {rt}",
                           f"Tableau {jt.upper()} join has no relationship equivalent; review model.")
        for t in self.model.tables:
            if t.datasource == ds.name and t.kind != "measures":
                t.kind = "fact" if t.name == fact else "dim"

    def _table_name_for(self, ds, i):
        for t in self.model.tables:
            if t.datasource == ds.name:
                return t.name
        return None

    def _rel_end(self, ds, table, column):
        for cname, c in ds.columns.items():
            if c.table == table and (cname == column or c.remote_name == column or
                                     cname == f"{column} ({table})"):
                return self.col_lookup.get((ds.name, cname), (None, None))
        col = ds.columns.get(column)
        if col is not None:
            return self.col_lookup.get((ds.name, column), (None, None))
        return None, None

    def reachable_one_side(self, host, other):
        seen, stack = set(), [host]
        while stack:
            t = stack.pop()
            for o in self.many_to_one.get(t, ()):
                if o == other:
                    return True
                if o not in seen:
                    seen.add(o)
                    stack.append(o)
        return False

    # ------------------------------------------------------- parameters --
    def _parameters(self):
        for p in self.wb.parameters.values():
            col = p.caption
            if p.domain_type == "range" and p.min is not None and p.max is not None:
                step = p.step or 1
                src = (f"SELECTCOLUMNS(GENERATESERIES({dax_literal(p.min)}, {dax_literal(p.max)}, "
                       f"{dax_literal(step)}), {dax_str(col)}, [Value])")
                values = []
                v = p.min
                while v <= p.max + 1e-9 and len(values) < 10000:
                    values.append(round(v, 10) if isinstance(v, float) else v)
                    v = v + step
            elif p.members:
                values = [m[0] for m in p.members]
                rows = ", ".join("{" + dax_literal(v) + "}" for v in values)
                src = f"SELECTCOLUMNS({{{', '.join(dax_literal(v) for v in values)}}}, {dax_str(col)}, [Value])"
            else:
                values = [p.current_value]
                src = f"SELECTCOLUMNS({{{dax_literal(p.current_value)}}}, {dax_str(col)}, [Value])"
            t = PbiTable(p.caption, "parameter", dax_expression=src, param_values=values,
                         param_default=p.current_value)
            t.columns.append(PbiColumn(col, PBI_TYPES.get(p.datatype, "string"), source_column=f"[{col}]",
                                       tableau_ref=f"[Parameters].[{p.internal_name}]"))
            dax = f"SELECTEDVALUE({q_col(p.caption, col)}, {dax_literal(p.current_value)})"
            t.measures.append(PbiMeasure(f"{p.caption} Value", dax,
                                         description=f"Selected value of Tableau parameter '{p.caption}'",
                                         tableau_ref=p.internal_name,
                                         datatype=p.datatype))
            self.model.tables.append(t)
            kind = "What-if (range)" if p.domain_type == "range" else ("List" if p.members else "Any value")
            self.calc_mappings.append(CalcMapping(
                "Parameters", p.caption, p.formula or str(p.current_value), "parameter",
                f"{p.caption} Value", p.caption, f"{p.caption} = {src}\n\n[{p.caption} Value] = {dax}",
                "Converted", [f"{kind} parameter -> disconnected table + slicer + value measure"]))
            if p.domain_type == "any" and not p.members:
                self.issue("info", "Parameter", p.caption,
                           "Free-entry parameter converted to a single-value table; add values or use a field parameter.")

    # ----------------------------------------------------- calculations --
    def _classify_calcs(self, ds: Datasource):
        fact = self.fact_of.get(ds.name)
        lookup = {}
        for name, cf in ds.calculations.items():
            if cf.kind == "formula":
                try:
                    lookup[name] = parse_formula(cf.formula)
                    lookup.setdefault(cf.caption, lookup[name])
                except FormulaError as e:
                    self.issue("error", "Calculation", cf.caption, f"Formula could not be parsed: {e}")
        self.calc_ast[ds.name] = lookup

        def lk(n):
            return lookup.get(n)

        for name, cf in ds.calculations.items():
            ast = lookup.get(name)
            if cf.kind == "formula" and ast is None:
                continue
            if cf.kind in ("group", "bin"):
                src = cf.extra.get("source")
                t = self.col_lookup.get((ds.name, src), (fact, None))[0]
                self.calc_info[(ds.name, name)] = CalcInfo("column", t, tidy(cf.caption),
                                                           "real" if cf.kind == "bin" else "string")
                continue
            if cf.kind == "set":
                src = cf.extra.get("source")
                t = self.col_lookup.get((ds.name, src), (fact, None))[0]
                self.calc_info[(ds.name, name)] = CalcInfo("column", t, tidy(cf.caption), "boolean")
                continue
            agg = is_aggregate(ast, lk)
            kind = "measure" if agg else "column"
            if cf.extra.get("security_filter") and not agg:
                self.calc_info[(ds.name, name)] = CalcInfo("security", self._host_for(ds, ast, lookup),
                                                           tidy(cf.caption), "boolean", ast)
                continue
            if isinstance(ast, LOD) and ast.kind in ("INCLUDE", "EXCLUDE"):
                self.lod_measures.add((ds.name, name))
                agg, kind = True, "measure"
            host = self.measure_table if agg else self._host_for(ds, ast, lookup)
            dt = cf.datatype or "real"
            nullable = False
            self.calc_info[(ds.name, name)] = CalcInfo(kind, host, tidy(cf.caption), dt, ast, nullable)
        # nullability after all infos exist
        for name, info in list(self.calc_info.items()):
            if name[0] != ds.name or info.ast is None or info.kind != "column":
                continue
            tr = DaxTranslator(_DsResolver(self, ds))
            try:
                info.nullable = tr.may_be_null(info.ast)
            except Exception:
                info.nullable = False

    def _host_for(self, ds, ast, lookup, _seen=None):
        _seen = _seen or set()
        fact = self.fact_of.get(ds.name)
        tables = set()
        for n in ast.walk():
            if isinstance(n, LOD):
                tables.add(fact)
            if isinstance(n, FField) and not n.is_parameter:
                if n.name in lookup and n.name not in _seen:
                    tables.add(self._host_for(ds, lookup[n.name], lookup, _seen | {n.name}))
                    continue
                key = self.col_lookup.get((ds.name, n.name))
                if key:
                    tables.add(key[0])
                else:
                    info = self.calc_info.get((ds.name, n.name))
                    if info:
                        tables.add(info.table)
        tables.discard(None)
        if len(tables) == 1:
            return tables.pop()
        return fact

    def _translate_calcs(self, ds: Datasource):
        resolver = _DsResolver(self, ds)
        order = self._dependency_order(ds)
        for name in order:
            cf = ds.calculations[name]
            info = self.calc_info.get((ds.name, name))
            if info is None:
                self.calc_mappings.append(CalcMapping(ds.caption, cf.caption, cf.formula, "measure", "",
                                                      "", "", "Failed", ["Formula could not be parsed"]))
                continue
            try:
                if cf.kind == "group":
                    res = self._group_dax(ds, cf, resolver)
                elif cf.kind == "bin":
                    res = self._bin_dax(ds, cf, resolver)
                elif cf.kind == "set":
                    res = self._set_dax(ds, cf, resolver)
                elif info.kind == "security":
                    res = DaxTranslator(resolver).translate(info.ast, "row", host=info.table)
                else:
                    tr = DaxTranslator(resolver)
                    addressing = self.addressing.get((ds.name, name))
                    if (ds.name, name) in self.lod_measures:
                        res = tr.lod_measure(info.ast, "sum")
                    else:
                        res = tr.translate(info.ast, "agg" if info.kind == "measure" else "row",
                                       host=info.table if info.kind == "column" else None,
                                       addressing=addressing)
            except (TranslationError, FormulaError, KeyError, IndexError) as e:
                self.calc_mappings.append(CalcMapping(ds.caption, cf.caption, cf.formula, info.kind,
                                                      info.name, info.table, "", "Failed", [str(e)]))
                self.issue("error", "Calculation", cf.caption, f"Not converted: {e}")
                continue
            fmt = tableau_format_to_pbi(cf.default_format)
            if info.kind == "security":
                expr = res.expression
                if expr.startswith("(") and expr.endswith(")"):
                    expr = expr[1:-1]
                self.model.roles.append(PbiRole(info.name, {info.table: expr}, cf.internal_name,
                                                f"Converted from Tableau user filter {cf.formula!r}"))
                self.calc_mappings.append(CalcMapping(
                    ds.caption, cf.caption, cf.formula, "rls-role", info.name, info.table, expr, res.status,
                    res.warnings + ["Tableau USERNAME() data source filter -> Power BI RLS role (USERPRINCIPALNAME). "
                                    "Assign Entra users/groups to the role in the service and test with real identities."],
                    sorted(res.functions), cf.xml_path, STRATEGY["rls-role"], res.confidence, cf.internal_name))
                self.issue("warning", "Security", info.name,
                           f"Row-level security role '{info.name}' created on '{info.table}'. Security parity "
                           f"is a hard gate: test with permitted and forbidden real users before go-live.")
                continue
            if info.kind == "measure":
                tbl = self.model.table(self.measure_table)
                tbl.measures.append(PbiMeasure(info.name, res.expression, fmt, cf.description,
                                               cf.folder, cf.internal_name, info.datatype))
            else:
                tbl = self.model.table(info.table)
                tbl.columns.append(PbiColumn(info.name, PBI_TYPES.get(info.datatype, "string"),
                                             expression=res.expression, format_string=fmt,
                                             description=cf.description, folder=cf.folder,
                                             tableau_ref=cf.internal_name, nullable=info.nullable,
                                             hidden=cf.hidden))
            formula = cf.formula or self._describe_special(cf)
            self.calc_mappings.append(CalcMapping(
                ds.caption, cf.caption, formula,
                cf.kind if cf.kind != "formula" else info.kind, info.name, info.table,
                res.expression, res.status, res.warnings, sorted(res.functions), cf.xml_path,
                ("rule:fixed-lod-column" if isinstance(info.ast, LOD) and info.ast.kind == "FIXED" else
                 "rule:include-exclude-lod-measure" if (ds.name, name) in self.lod_measures else
                 "rule:table-calc-window" if info.ast is not None and contains(
                     info.ast, lambda n: isinstance(n, Func) and n.name in TABLE_CALCS) else
                 STRATEGY.get(cf.kind if cf.kind != "formula" else info.kind, "rule")),
                res.confidence, cf.internal_name))
            for w in res.warnings:
                self.issue("warning" if res.confidence != "low" else "error", "Calculation", cf.caption, w)

    def _describe_special(self, cf: CalculatedField):
        if cf.kind == "group":
            return "GROUP " + "; ".join(f"{b['label']} = {b['values']}" for b in cf.extra.get("bins", []))
        if cf.kind == "bin":
            return f"BIN [{cf.extra.get('source')}] size {cf.extra.get('size')}"
        if cf.kind == "set":
            e = cf.extra
            if e.get("set_type") == "top":
                return f"SET top {e.get('count')} [{e.get('source')}] by {e.get('expression')}"
            return f"SET [{e.get('source')}] IN {e.get('members')}"
        return ""

    def _dependency_order(self, ds):
        lookup = self.calc_ast.get(ds.name, {})
        names = list(ds.calculations)
        cap_to_int = {cf.caption: n for n, cf in ds.calculations.items()}
        deps = {}
        for n in names:
            ast = lookup.get(n)
            d = set()
            if ast is not None:
                for nd in ast.walk():
                    if isinstance(nd, FField):
                        ref = nd.name if nd.name in ds.calculations else cap_to_int.get(nd.name)
                        if ref and ref != n:
                            d.add(ref)
            src = ds.calculations[n].extra.get("source")
            if src in ds.calculations:
                d.add(src)
            deps[n] = d
        out, seen = [], set()

        def visit(n, stack=()):
            if n in seen or n in stack:
                return
            for d in deps.get(n, ()):
                visit(d, stack + (n,))
            seen.add(n)
            out.append(n)
        for n in names:
            visit(n)
        return out

    def _src_ref(self, ds, src, resolver, host):
        info = resolver.calc(src)
        if info is not None:
            return q_col(info.table, info.name) if info.table == host else f"RELATED({q_col(info.table, info.name)})", info.datatype
        col = resolver.column(src)
        if col is None:
            raise TranslationError(f"Unknown source field [{src}]")
        ref = q_col(col.table, col.column)
        return (ref if col.table == host else f"RELATED({ref})"), col.datatype

    def _group_dax(self, ds, cf, resolver):
        info = self.calc_info[(ds.name, cf.internal_name)]
        ref, dt = self._src_ref(ds, cf.extra["source"], resolver, info.table)
        parts = []
        for b in cf.extra.get("bins", []):
            vals = ", ".join(dax_literal(v) for v in b["values"])
            parts.append(f"{ref} IN {{{vals}}}, {dax_literal(b['label'])}")
        other = cf.extra.get("other")
        tail = dax_literal(other) if other else (ref if dt == "string" else f"CONVERT({ref}, STRING)")
        expr = "SWITCH(TRUE(),\n        " + ",\n        ".join(parts) + f",\n        {tail}\n    )"
        return DaxResult(expr, "row", "string", [], {"SWITCH", "IN"}, "high", info.table)

    def _bin_dax(self, ds, cf, resolver):
        info = self.calc_info[(ds.name, cf.internal_name)]
        ref, _ = self._src_ref(ds, cf.extra["source"], resolver, info.table)
        size = cf.extra.get("size") or "1"
        warnings = []
        m = re.match(r"\[Parameters\]\.\[(.+)\]", size)
        if m:
            p = resolver.parameter(m.group(1))
            size = q_measure(p.measure)
            warnings.append("Bin size driven by a parameter is static in a calculated column.")
        expr = f"IF(ISBLANK({ref}), BLANK(), FLOOR({ref}, {size}))"
        return DaxResult(expr, "row", "real", warnings, {"FLOOR"}, "high", info.table)

    def _set_dax(self, ds, cf, resolver):
        info = self.calc_info[(ds.name, cf.internal_name)]
        e = cf.extra
        ref, dt = self._src_ref(ds, e["source"], resolver, info.table)
        if e.get("set_type") == "members":
            vals = ", ".join(dax_literal(v) for v in e.get("members", []))
            return DaxResult(f"{ref} IN {{{vals}}}", "row", "boolean", [], {"IN"}, "high", info.table)
        if e.get("set_type") == "top":
            tr = DaxTranslator(resolver)
            m = tr.translate(e.get("expression") or "SUM([Number of Records])", "agg")
            count = e.get("count") or "10"
            pm = re.match(r"\[Parameters\]\.\[(.+)\]", count)
            if pm:
                count = q_measure(resolver.parameter(pm.group(1)).measure)
            order = "DESC" if e.get("end", "top") == "top" else "ASC"
            col = resolver.column(e["source"])
            expr = (f"RANKX(ALL({q_table(col.table)}), CALCULATE({m.expression}), , {order}, SKIP)"
                    f" <= {count}")
            return DaxResult(expr, "row", "boolean", ["Top-N set evaluated at refresh (static); ranks rows of "
                                                      f"{q_table(col.table)} (one row per [{col.column}] assumed)."] + m.warnings,
                             {"RANKX", "ALL", "CALCULATE"} | m.functions, "medium", info.table)
        raise TranslationError(f"Set type '{e.get('set_type')}' is not supported automatically")

    # --------------------------------------- date parts & table calc addr --
    def _date_parts_and_addressing(self):
        for ws in self.wb.worksheets:
            for ref in ws.all_fields() + [f.field for f in ws.filters]:
                if ref.derivation in DATE_PART:
                    self._date_part_column(ref)
        for ws in self.wb.worksheets:
            for ref in ws.rows + ws.cols + [r for v in ws.encodings.values() for r in v]:
                ds = self.wb.datasource(ref.datasource)
                if ds is None:
                    continue
                ast = self.calc_ast.get(ds.name, {}).get(ref.field)
                if ast is None or not contains(ast, lambda n: isinstance(n, Func) and n.name in TABLE_CALCS):
                    continue
                if (ds.name, ref.field) in self.addressing:
                    continue
                addr_refs = self._addressing_refs(ws, ref)
                self.addressing[(ds.name, ref.field)] = [x for x in (self.dim_target(r) for r in addr_refs) if x]
                self.addressing[(ds.name, ref.field + "@ws")] = ws.name

    def _addressing_refs(self, ws: Worksheet, ref: FieldRef):
        def dims(shelf):
            return [r for r in shelf if not self.is_measure_ref(r)]
        tc = ref.table_calc or {}
        ot = (tc.get("ordering_type") or "").lower()
        if ot == "field" and tc.get("ordering_fields"):
            out = []
            for f in tc["ordering_fields"]:
                for r in ws.rows + ws.cols:
                    if f"{r.derivation}:{r.field}:{r.pill_type}" == f.split("].[")[-1] or r.field in f:
                        out.append(r)
            if out:
                return out
        if ot == "rows":
            return dims(ws.rows)
        if ot == "columns":
            return dims(ws.cols)
        return dims(ws.cols) or dims(ws.rows)

    def _date_part_column(self, ref: FieldRef):
        ds = self.wb.datasource(ref.datasource)
        if ds is None:
            return None
        key = self.col_lookup.get((ds.name, ref.field))
        if key is None:
            info = self.calc_info.get((ds.name, ref.field))
            if info is None or info.kind != "column":
                return None
            key = (info.table, info.name)
        t, c = key
        suffix, tmpl, dt = DATE_PART[ref.derivation]
        name = f"{c} {suffix}"
        tbl = self.model.table(t)
        if tbl.column(name) is None:
            col_ref = q_col(t, c)
            dax = {"yr": f"YEAR({col_ref})", "qr": f"QUARTER({col_ref})", "mn": f"MONTH({col_ref})",
                   "dy": f"DAY({col_ref})", "wk": f"WEEKNUM({col_ref}, 1)",
                   "tyr": f"DATE(YEAR({col_ref}), 1, 1)",
                   "tqr": f"DATE(YEAR({col_ref}), (QUARTER({col_ref}) - 1) * 3 + 1, 1)",
                   "tmn": f"DATE(YEAR({col_ref}), MONTH({col_ref}), 1)",
                   "my": f"DATE(YEAR({col_ref}), MONTH({col_ref}), 1)",
                   "twk": f"({col_ref} - WEEKDAY({col_ref}, 1) + 1)",
                   "tdy": f"TRUNC({col_ref})"}[ref.derivation]
            src_col = tbl.column(c)
            if src_col is not None and src_col.nullable:
                dax = f"IF(ISBLANK({col_ref}), BLANK(), {dax})"
            tbl.columns.append(PbiColumn(name, PBI_TYPES[dt], expression=dax,
                                         tableau_ref=f"{ref.derivation}:{ref.field}",
                                         folder="Date parts", nullable=bool(src_col and src_col.nullable)))
            self.calc_mappings.append(CalcMapping(ds.caption, f"{ref.derivation.upper()}([{ref.field}])",
                                                  tmpl.format(f=ref.field), "date-part", name, t, dax,
                                                  "Converted", [], []))
        return t, name

    def _date_table(self):
        """Best practice: a marked date table related to the main fact date column."""
        for ds in self.wb.datasources:
            fact = self.model.table(self.fact_of.get(ds.name) or "")
            if fact is None:
                continue
            date_cols = [c for c in fact.columns if c.datatype == "dateTime" and c.expression is None]
            if not date_cols:
                continue
            if self.model.table("Date") is not None:
                return
            date_dims = [r.to_table for r in self.model.relationships if r.from_table == fact.name and any(
                c.datatype == "dateTime" and c.expression is None for c in self.model.table(r.to_table).columns)]
            if date_dims:
                self.issue("info", "Model", date_dims[0],
                           f"'{date_dims[0]}' is used as the date dimension: mark it as a date table in Power BI.")
                return
            dc = date_cols[0]
            expr = (f"ADDCOLUMNS(CALENDARAUTO(), \"Year\", YEAR([Date]), \"Quarter\", \"Q\" & QUARTER([Date]), "
                    f"\"Month Number\", MONTH([Date]), \"Month\", FORMAT([Date], \"MMM\"), "
                    f"\"Month Start\", DATE(YEAR([Date]), MONTH([Date]), 1))")
            t = PbiTable("Date", "date", dax_expression=expr)
            t.columns = [PbiColumn("Date", "dateTime", source_column="[Date]"),
                         PbiColumn("Year", "int64", source_column="[Year]"),
                         PbiColumn("Quarter", "string", source_column="[Quarter]"),
                         PbiColumn("Month Number", "int64", source_column="[Month Number]", hidden=True),
                         PbiColumn("Month", "string", source_column="[Month]"),
                         PbiColumn("Month Start", "dateTime", source_column="[Month Start]")]
            t.hierarchies.append(("Calendar", ["Year", "Quarter", "Month"]))
            self.model.tables.append(t)
            self.model.relationships.append(PbiRelationship(fact.name, dc.name, "Date", "Date"))
            self.issue("info", "Model", "Date",
                       f"Added a Date table (CALENDARAUTO) related to {fact.name}[{dc.name}] for time intelligence.")
            return

    # --------------------------------------------------- field mapping --
    def is_measure_ref(self, ref: FieldRef) -> bool:
        if ref.derivation in AGG_DERIVATIONS:
            return True
        if ref.derivation in DATE_PART:
            return False
        ds = self.wb.datasource(ref.datasource)
        if ds is None:
            return False
        info = self.calc_info.get((ds.name, ref.field))
        if info is not None and info.kind == "measure":
            return True
        return False

    def dim_target(self, ref: FieldRef):
        ds = self.wb.datasource(ref.datasource)
        if ds is None:
            return None
        if ref.derivation in DATE_PART:
            return self._date_part_column(ref)
        key = self.col_lookup.get((ds.name, ref.field))
        if key:
            return key
        info = self.calc_info.get((ds.name, ref.field))
        if info is not None and info.kind == "column":
            return info.table, info.name
        return None

    def measure_target(self, ref: FieldRef) -> Optional[str]:
        """Return (creating when needed) the PBI measure for an aggregated pill."""
        ds = self.wb.datasource(ref.datasource)
        if ds is None:
            return None
        info = self.calc_info.get((ds.name, ref.field))
        deriv = ref.derivation if ref.derivation in AGG_FUNC else "sum"
        is_lod_measure = (ds.name, ref.field) in self.lod_measures
        if info is not None and info.kind == "measure" and (not is_lod_measure or deriv == "sum"
                                                             or ref.derivation in ("usr", "none")):
            return info.name
        if info is not None:
            label = info.name
        else:
            col = ds.columns.get(ref.field)
            label = (col.caption if col and col.caption else ref.field)
        name = f"{AGG_LABEL.get(deriv, deriv.title())} of {tidy(label)}"
        if self.model.measure(name):
            return name
        resolver = _DsResolver(self, ds)
        tr = DaxTranslator(resolver)
        formula = f"{AGG_FUNC[deriv]}([{ref.field}])"
        ast = self.calc_ast.get(ds.name, {}).get(ref.field)
        try:
            if ast is not None and isinstance(ast, LOD) and ast.kind != "FIXED":
                # INCLUDE/EXCLUDE are view-dependent: aggregate over the LOD grain.
                # FIXED results are row-level values (replicated per record), so they
                # are materialised as calculated columns and aggregated normally.
                res = tr.lod_measure(ast, deriv)
                formula = f"{AGG_FUNC[deriv]}({ds.calculations[ref.field].formula})"
            else:
                res = tr.translate(formula, "agg")
        except TranslationError as e:
            self.issue("error", "Measure", name, str(e))
            return None
        fmt = None
        col = ds.columns.get(ref.field)
        if col is not None and deriv not in ("cnt", "ctd", "count", "cntd"):
            fmt = tableau_format_to_pbi(col.default_format)
        self.model.table(self.measure_table).measures.append(
            PbiMeasure(name, res.expression, fmt, f"Tableau {AGG_FUNC[deriv]}([{ref.field}])",
                       "Base measures", f"{deriv}:{ref.field}", res.datatype))
        self.calc_mappings.append(CalcMapping(ds.caption, name, formula, "base-measure", name,
                                              self.measure_table, res.expression, res.status,
                                              res.warnings, sorted(res.functions)))
        return name

    def measure_for_formula(self, ds: Datasource, formula: str) -> Optional[str]:
        """Measure for an ad-hoc aggregate formula such as a Top-N 'by' expression."""
        try:
            ast = parse_formula(formula)
        except FormulaError:
            return None
        if isinstance(ast, Func) and len(ast.args) == 1 and isinstance(ast.args[0], FField):
            inv = {v: k for k, v in AGG_FUNC.items()}
            deriv = inv.get(ast.name)
            if deriv:
                return self.measure_target(FieldRef(ds.name, ast.args[0].name, deriv, "qk"))
        if isinstance(ast, FField):
            info = self.calc_info.get((ds.name, ast.name))
            if info and info.kind == "measure":
                return info.name
        name = f"Top N by {tidy(formula)}"
        if not self.model.measure(name):
            res = DaxTranslator(_DsResolver(self, ds)).translate(ast, "agg")
            self.model.table(self.measure_table).measures.append(
                PbiMeasure(name, res.expression, None, f"Tableau {formula}", "Base measures"))
        return name

    # --------------------------------------------------------- visuals --
    def _visual(self, ws: Worksheet) -> PbiVisual:
        row_dims = [r for r in ws.rows if not self.is_measure_ref(r)]
        col_dims = [r for r in ws.cols if not self.is_measure_ref(r)]
        row_meas = [r for r in ws.rows if self.is_measure_ref(r)]
        col_meas = [r for r in ws.cols if self.is_measure_ref(r)]
        enc = ws.encodings
        color = enc.get("color", [])
        size = enc.get("size", [])
        text = enc.get("text", []) + enc.get("label", [])
        detail = enc.get("lod", []) + enc.get("detail", [])
        tooltip = enc.get("tooltip", [])
        mark = (ws.mark or "Automatic").lower()

        def cols(refs):
            out = []
            for r in refs:
                tgt = self.dim_target(r)
                if tgt:
                    out.append(("column", tgt[0], tgt[1]))
                    self.field_map[(ws.name, r.key)] = tgt
                else:
                    self.issue("warning", "Visual", ws.name, f"Field [{r.field}] could not be mapped.")
            return out

        def meas(refs):
            out = []
            for r in refs:
                m = self.measure_target(r)
                if m:
                    out.append(("measure", self.measure_table if not self._param_measure(m) else self._param_measure(m), m))
                    self.field_map[(ws.name, r.key)] = m
            return out

        color_dims = [r for r in color if not self.is_measure_ref(r)]
        color_meas = [r for r in color if self.is_measure_ref(r)]
        text_meas = [r for r in text if self.is_measure_ref(r)]
        text_dims = [r for r in text if not self.is_measure_ref(r)]
        geo = any((self._column_obj(r) and (self._column_obj(r).semantic_role or "").startswith("[State]")
                   or (self._column_obj(r) and (self._column_obj(r).semantic_role or "").startswith("[Country]")))
                  for r in row_dims + col_dims + detail + color_dims)
        has_date_axis = any(r.derivation in DATE_PART or self._is_date(r) for r in col_dims + row_dims)
        v = PbiVisual(name=_vid(ws.name), visual_type="tableEx", worksheet=ws.name,
                      title=ws.title or ws.name)
        all_meas = row_meas + col_meas
        if mark in ("map", "multipolygon") or (geo and mark in ("automatic", "circle", "shape")) :
            v.visual_type = "filledMap" if mark in ("multipolygon", "map") and color_meas else "map"
            loc = [r for r in row_dims + col_dims + detail if self._column_obj(r) and self._column_obj(r).semantic_role]
            v.roles["Category"] = cols(loc or detail)
            if color_meas or size:
                v.roles["Size" if v.visual_type == "map" else "Color"] = meas(color_meas or size)
            v.notes.append("Tableau map converted to a Power BI map visual; verify geocoding (data category).")
        elif mark == "pie":
            v.visual_type = "pieChart"
            v.roles["Category"] = cols(color_dims or row_dims + col_dims)
            v.roles["Y"] = meas(size or all_meas or text_meas)
        elif mark in ("text",) or (mark == "automatic" and not all_meas and text_meas):
            if not row_dims and not col_dims:
                v.visual_type = "card" if len(text_meas) == 1 else "multiRowCard"
                v.roles["Values"] = meas(text_meas)
            elif row_dims and col_dims:
                v.visual_type = "pivotTable"
                v.roles["Rows"] = cols(row_dims)
                v.roles["Columns"] = cols(col_dims)
                v.roles["Values"] = meas(_dedupe(text_meas + all_meas))
            else:
                v.visual_type = "tableEx"
                v.roles["Values"] = cols(row_dims + col_dims) + meas(_dedupe(all_meas + text_meas))
        elif row_meas and col_meas and (mark in ("circle", "shape", "automatic", "square")):
            v.visual_type = "scatterChart"
            v.roles["Category"] = cols(detail + color_dims + row_dims + col_dims)
            v.roles["X"] = meas(col_meas[:1])
            v.roles["Y"] = meas(row_meas[:1])
            if size:
                v.roles["Size"] = meas([s for s in size if self.is_measure_ref(s)])
        elif mark in ("line", "area") or (mark == "automatic" and has_date_axis and all_meas):
            stacked = mark == "area"
            v.visual_type = "areaChart" if stacked else "lineChart"
            v.roles["Category"] = cols(col_dims + row_dims)
            v.roles["Y"] = meas(all_meas)
            if color_dims:
                v.roles["Series"] = cols(color_dims)
            if ws.dual_axis and len(all_meas) > 1:
                v.visual_type = "lineClusteredColumnComboChart"
                v.roles["Y"] = meas(all_meas[:1])
                v.roles["Y2"] = meas(all_meas[1:])
        elif mark in ("square",) and row_dims and col_dims:
            v.visual_type = "pivotTable"
            v.roles["Rows"] = cols(row_dims)
            v.roles["Columns"] = cols(col_dims)
            v.roles["Values"] = meas(color_meas or text_meas or all_meas)
            v.notes.append("Highlight table converted to a matrix; apply conditional formatting (background colour).")
        elif mark == "square" and not row_dims and not col_dims:
            v.visual_type = "treemap"
            v.roles["Group"] = cols(color_dims + detail + text_dims)
            v.roles["Values"] = meas(size or color_meas or text_meas)
        elif all_meas and (row_dims or col_dims or color_dims):
            horizontal = bool(row_dims) and bool(col_meas) and not row_meas
            stacked = bool(color_dims)
            if horizontal:
                v.visual_type = "stackedBarChart" if stacked else "clusteredBarChart"
                v.roles["Category"] = cols(row_dims)
                v.roles["Y"] = meas(col_meas)
            else:
                v.visual_type = "stackedColumnChart" if stacked else "clusteredColumnChart"
                v.roles["Category"] = cols(col_dims or row_dims)
                v.roles["Y"] = meas(row_meas or col_meas)
            if color_dims:
                v.roles["Series"] = cols(color_dims)
            if mark == "gantt bar" or mark == "ganttbar":
                v.notes.append("Gantt marks have no native visual; converted to a bar chart.")
        elif all_meas or text_meas:
            v.visual_type = "card" if len(all_meas + text_meas) == 1 else "multiRowCard"
            v.roles["Values"] = meas(all_meas + text_meas)
        else:
            v.visual_type = "tableEx"
            v.roles["Values"] = cols(row_dims + col_dims + color_dims + detail)
        # measures used on label / tooltip / size marks are kept as tooltips so that the
        # numbers shown in Tableau remain available (and are validated) in Power BI
        placed = set(v.measures)
        extra = [r for r in tooltip + text + size + color if self.is_measure_ref(r)]
        tm = [x for x in meas(_dedupe(extra)) if x[2] not in placed]
        if tm:
            v.roles["Tooltips"] = v.roles.get("Tooltips", []) + tm
        v.filters = [f for f in (self._filter(ws, fl) for fl in ws.filters) if f is not None]
        for s in ws.sorts:
            if s.get("using") is not None and self.is_measure_ref(s["using"]):
                m = self.measure_target(s["using"])
                if m:
                    v.sort = {"measure": m, "direction": s.get("direction", "DESC")}
            elif s.get("field") is not None and v.sort is None:
                tgt = self.dim_target(s["field"])
                if tgt:
                    v.sort = {"column": tgt, "direction": s.get("direction", "ASC")}
        if ws.dual_axis and v.visual_type not in ("lineClusteredColumnComboChart",):
            v.notes.append("Dual axis chart: review secondary axis in Power BI.")
        return v

    def _param_measure(self, name):
        for t in self.model.tables:
            if t.kind == "parameter" and any(m.name == name for m in t.measures):
                return t.name
        return None

    def _column_obj(self, ref: FieldRef) -> Optional[Column]:
        ds = self.wb.datasource(ref.datasource)
        return ds.columns.get(ref.field) if ds else None

    def _is_date(self, ref: FieldRef):
        c = self._column_obj(ref)
        return bool(c and c.datatype in ("date", "datetime"))

    def _filter(self, ws: Worksheet, fl: Filter) -> Optional[PbiFilter]:
        ref = fl.field
        ds = self.wb.datasource(ref.datasource)
        if fl.is_context:
            self.issue("warning", "Filter", ws.name,
                       f"Context filter on [{ref.field}] - FIXED LODs in Tableau respect it, Power BI ALLEXCEPT "
                       f"does not. Verify numbers in validation.")
        if fl.kind == "top":
            tgt = self.dim_target(ref)
            by = self.measure_for_formula(ds, fl.top_by or "SUM([Number of Records])") if ds else None
            n = fl.top_n
            if fl.top_param:
                p = self.wb.parameters.get(fl.top_param)
                n = int(p.current_value) if p else 10
                self.issue("info", "Filter", ws.name,
                           f"Top N bound to parameter '{p.caption if p else fl.top_param}' converted with its "
                           f"current value ({n}); Power BI Top N filters cannot be parameter-driven.")
            others = [f for f in ws.filters if f is not fl and f.kind in ("categorical",) and not f.is_context]
            if others:
                self.issue("warning", "Filter", ws.name,
                           "Top N filter combined with dimension filters: Tableau computes Top N before "
                           "dimension filters (unless they are context filters); Power BI computes it after.")
            if tgt is None:
                return None
            return PbiFilter("topn", tgt[0], tgt[1], top_n=n, top_direction=fl.top_direction, by_measure=by)
        if fl.kind == "categorical-all":
            return None
        if fl.kind == "condition":
            self.issue("warning", "Filter", ws.name, f"Condition filter on [{ref.field}] by {fl.top_by} needs manual setup.")
            return None
        if self.is_measure_ref(ref):
            m = self.measure_target(ref)
            return PbiFilter("measure-range", measure=m, min=fl.min, max=fl.max, include_null=fl.include_null)
        tgt = self.dim_target(ref)
        if tgt is None:
            self.issue("warning", "Filter", ws.name, f"Filter field [{ref.field}] could not be mapped.")
            return None
        if fl.kind == "categorical":
            return PbiFilter("categorical", tgt[0], tgt[1], values=list(fl.members), exclude=fl.exclude)
        if fl.kind == "quantitative":
            return PbiFilter("range", tgt[0], tgt[1], min=fl.min, max=fl.max, include_null=fl.include_null)
        if fl.kind == "relative-date":
            return PbiFilter("relative-date", tgt[0], tgt[1], period_type=fl.period_type,
                             first_period=fl.first_period, last_period=fl.last_period)
        return None

    # ----------------------------------------------------------- pages --
    def _pages(self, visuals: dict) -> list:
        pages = []
        used = set()
        W, H = 1280, 720
        for db in self.wb.dashboards:
            page = PbiPage(_vid("page " + db.name), db.name, W, H)
            sx = W / 100000.0
            sy = H / 100000.0
            # Tableau zone coordinates are in 1/100000 of the dashboard size.
            max_x = max((z.x + z.w for z in db.zones), default=100000) or 100000
            max_y = max((z.y + z.h for z in db.zones), default=100000) or 100000
            if max_x > 100000 or max_y > 100000:       # absolute pixel coordinates
                sx, sy = W / max(max_x, 1), H / max(max_y, 1)
            for z in db.zones:
                x, y, w, h = z.x * sx, z.y * sy, max(z.w * sx, 40), max(z.h * sy, 30)
                if z.type == "worksheet" and z.name in visuals:
                    base = visuals[z.name]
                    v = PbiVisual(**{**base.__dict__, "x": x, "y": y, "w": w, "h": h,
                                     "name": _vid(db.name + z.name)})
                    page.visuals.append(v)
                    used.add(z.name)
                elif z.type == "filter" and z.param:
                    ref = None
                    from .parser import parse_field_ref
                    ref = parse_field_ref(z.param)
                    tgt = self.dim_target(ref) if ref else None
                    if tgt:
                        page.visuals.append(PbiVisual(_vid(db.name + z.id + "slicer"), "slicer",
                                                      title=tgt[1], roles={"Values": [("column", tgt[0], tgt[1])]},
                                                      x=x, y=y, w=w, h=h))
                elif z.type == "paramctrl" and z.param:
                    pname = z.param.split("].[")[-1].strip("[]")
                    p = self.wb.parameters.get(pname)
                    if p:
                        page.visuals.append(PbiVisual(_vid(db.name + z.id + "param"), "slicer", title=p.caption,
                                                      roles={"Values": [("column", p.caption, p.caption)]},
                                                      x=x, y=y, w=w, h=h,
                                                      notes=["Parameter control -> single-select slicer"]))
                elif z.type in ("text", "title") and (z.text or z.type == "title"):
                    page.visuals.append(PbiVisual(_vid(db.name + z.id + "text"), "textbox",
                                                  text=z.text or db.name, x=x, y=y, w=w, h=h))
                elif z.type in ("bitmap", "image", "web", "extension"):
                    self.issue("info", "Dashboard", db.name,
                               f"{z.type} object '{z.name or z.id}' needs to be recreated manually.")
            pages.append(page)
        for ws in self.wb.worksheets:
            if ws.name in used:
                continue
            page = PbiPage(_vid("page " + ws.name), ws.name, W, H, source="worksheet")
            v = visuals[ws.name]
            page.visuals.append(PbiVisual(**{**v.__dict__, "x": 20, "y": 20, "w": W - 40, "h": H - 40}))
            pages.append(page)
        return pages

    def _actions(self):
        for a in self.wb.actions:
            if a.type == "filter":
                self.issue("info", "Action", a.name,
                           "Filter action -> Power BI cross-filter interaction (default). "
                           "Use 'Edit interactions' to match source/target sheets.")
            elif a.type == "highlight":
                self.issue("info", "Action", a.name, "Highlight action -> cross-highlight interaction (default).")
            elif a.type == "url":
                self.issue("warning", "Action", a.name, f"URL action -> add a button/URL column ({a.url}).")
            elif a.type == "goto-sheet":
                self.issue("warning", "Action", a.name, "Go-to-sheet action -> page navigation button or drillthrough.")
            elif a.type == "parameter":
                self.issue("warning", "Action", a.name, "Parameter action -> slicer selection / field parameter.")
            else:
                self.issue("warning", "Action", a.name, f"Action type '{a.type}' needs manual recreation.")


def _dedupe(refs):
    out, seen = [], set()
    for r in refs:
        if r.key not in seen:
            seen.add(r.key)
            out.append(r)
    return out


def _vid(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def convert(wb: Workbook, join_strategy: str = "flatten") -> ConversionResult:
    return Converter(wb, join_strategy).convert()


def apply_override(res: ConversionResult, pbi_object: str, dax: str, note: str = "", source: str = "manual") -> CalcMapping:
    """Replace the DAX of a measure / calculated column / RLS role after review.

    The expression must parse and reference existing model objects. The original DAX is
    kept for lineage; the mapping becomes REVIEW until validation is re-run."""
    from .ai_assist import check_dax
    check = check_dax(res, dax)
    if not check["parse_ok"] or not check["references_ok"]:
        raise ValueError("; ".join(check["problems"]))
    target = None
    for t in res.model.tables:
        for m in t.measures:
            if m.name == pbi_object:
                target = m
        for c in t.columns:
            if c.name == pbi_object and c.expression:
                target = c
    role = next((r for r in res.model.roles if r.name == pbi_object), None)
    if target is None and role is None:
        raise KeyError(f"No measure, calculated column or role named '{pbi_object}'")
    if target is not None:
        target.expression = dax
    else:
        table = next(iter(role.table_permissions))
        role.table_permissions[table] = dax
    hit = None
    for m in res.calc_mappings:
        if m.pbi_object == pbi_object:
            if not m.overridden:
                m.original_dax = m.dax
            m.dax, m.overridden, m.override_note = dax, True, note or None
            m.strategy = "ai:suggestion-reviewed" if source == "ai" else "override:manual"
            hit = m
    if hit is None:
        raise KeyError(f"'{pbi_object}' has no Tableau mapping")
    return hit
