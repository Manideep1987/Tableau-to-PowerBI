"""Canonical metadata model for a parsed Tableau workbook.

Every component the converter understands is represented here so that the
PBIP generator, the migration report and the validation engines all work from
one consistent, fully-resolved picture of the source workbook.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Column:
    name: str                      # Tableau local name without brackets, e.g. "Sales"
    table: Optional[str]           # logical table (object) the column belongs to
    remote_name: Optional[str]     # physical column name in the source
    datatype: str = "string"       # string|integer|real|date|datetime|boolean
    role: str = "dimension"        # dimension|measure
    type: str = "nominal"          # nominal|ordinal|quantitative
    caption: Optional[str] = None
    aggregation: Optional[str] = None  # default aggregation (Sum, Avg, Count ...)
    contains_null: bool = False
    hidden: bool = False
    description: Optional[str] = None
    folder: Optional[str] = None
    default_format: Optional[str] = None
    semantic_role: Optional[str] = None   # e.g. [State].[Name] for geo roles

    xml_path: Optional[str] = None   # XPath of the source element (lineage)
    @property
    def display(self) -> str:
        return self.caption or self.name


@dataclass
class CalculatedField:
    internal_name: str             # e.g. Calculation_1234 or a plain name
    caption: str
    formula: str
    datatype: str = "real"
    role: str = "measure"
    type: str = "quantitative"
    kind: str = "formula"          # formula|group|bin|set
    hidden: bool = False
    description: Optional[str] = None
    folder: Optional[str] = None
    default_format: Optional[str] = None
    extra: dict = field(default_factory=dict)   # group bins, bin size, set members ...
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


@dataclass
class Parameter:
    internal_name: str             # e.g. Parameter 1
    caption: str
    datatype: str
    current_value: Any
    domain_type: str               # range|list|any
    min: Any = None
    max: Any = None
    step: Any = None
    members: list = field(default_factory=list)   # list of (value, alias)
    formula: Optional[str] = None
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


@dataclass
class Relation:
    """A physical relation inside a datasource (table, custom SQL, or join)."""
    name: str
    type: str                      # table|text(custom sql)|join|union
    connection: Optional[str] = None
    table: Optional[str] = None    # [schema].[table]
    sql: Optional[str] = None
    join_type: Optional[str] = None
    clauses: list = field(default_factory=list)   # list of (left_field, op, right_field)
    children: list = field(default_factory=list)


@dataclass
class Relationship:
    """Logical-layer relationship or legacy join clause between two tables."""
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str = "relationship"   # relationship|inner|left|right|full
    cardinality: str = "many-to-one"
    left_unique: Optional[bool] = None    # key uniqueness profiled from the source data
    right_unique: Optional[bool] = None


@dataclass
class Connection:
    name: str
    cls: str                       # sqlserver|postgres|snowflake|excel-direct|textscan ...
    server: Optional[str] = None
    dbname: Optional[str] = None
    schema: Optional[str] = None
    username: Optional[str] = None
    port: Optional[str] = None
    filename: Optional[str] = None
    warehouse: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class LogicalTable:
    name: str
    relation: Relation
    columns: list = field(default_factory=list)   # list[str] column names


@dataclass
class Hierarchy:
    name: str
    levels: list


@dataclass
class Datasource:
    name: str                      # internal, e.g. federated.0abc
    caption: str
    connections: list = field(default_factory=list)
    tables: list = field(default_factory=list)          # list[LogicalTable]
    relationships: list = field(default_factory=list)   # list[Relationship]
    columns: dict = field(default_factory=dict)         # name -> Column
    calculations: dict = field(default_factory=dict)    # internal_name -> CalculatedField
    hierarchies: list = field(default_factory=list)
    is_extract: bool = False
    model_style: str = "relationships"   # relationships|joins|single
    aliases: dict = field(default_factory=dict)         # column -> {key: alias}
    datasource_filters: list = field(default_factory=list)
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


def hub_table(ds: "Datasource") -> Optional[str]:
    """Relationship (logical-layer) data sources have no root table: the anchor is the hub, the
    table in the most relationships (ties -> the earlier left table), as in the converter."""
    if not ds.tables:
        return None
    degree, order = {}, []
    for r in ds.relationships:
        for t in (r.left_table, r.right_table):
            degree[t] = degree.get(t, 0) + 1
        if r.left_table not in order:
            order.append(r.left_table)
    names = [t.name for t in ds.tables]
    cands = [t for t in degree if t in names]
    if not cands:
        return names[0]
    return max(cands, key=lambda t: (degree[t], -(order.index(t) if t in order else 99)))


@dataclass
class FieldRef:
    """A field placed on a shelf, e.g. [sum:Sales:qk]."""
    datasource: str
    field: str                     # local field name (column or calc caption-resolved internal)
    derivation: str = "none"      # none|sum|avg|cnt|ctd|min|max|yr|qr|mn|tmn|tdy|usr|attr ...
    pill_type: str = "nk"         # nk|ok|qk
    table_calc: Optional[dict] = None

    @property
    def is_measure_like(self) -> bool:
        return self.pill_type == "qk" and self.derivation not in DATE_PART_DERIVATIONS | {"none"} \
            or self.derivation in AGG_DERIVATIONS

    @property
    def key(self) -> str:
        return f"{self.derivation}:{self.field}:{self.pill_type}"


AGG_DERIVATIONS = {"sum", "avg", "cnt", "ctd", "min", "max", "med", "attr", "usr",
                   "stdev", "stdevp", "var", "varp", "cntd", "count"}
DATE_PART_DERIVATIONS = {"yr", "qr", "mn", "dy", "wk", "tyr", "tqr", "tmn", "tdy", "twk",
                         "my", "md"}


@dataclass
class Filter:
    kind: str                      # categorical|quantitative|relative-date|top|context
    datasource: str
    field: FieldRef
    members: list = field(default_factory=list)
    exclude: bool = False
    min: Any = None
    max: Any = None
    include_null: bool = False
    period_type: Optional[str] = None
    first_period: Optional[int] = None
    last_period: Optional[int] = None
    top_n: Optional[int] = None
    top_direction: str = "top"
    top_by: Optional[str] = None   # formula, e.g. SUM([Sales])
    top_param: Optional[str] = None
    is_context: bool = False
    raw: Optional[str] = None
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


@dataclass
class Worksheet:
    name: str
    datasources: list = field(default_factory=list)
    rows: list = field(default_factory=list)      # list[FieldRef]
    cols: list = field(default_factory=list)
    mark: str = "Automatic"
    encodings: dict = field(default_factory=dict)  # color/size/label/detail/tooltip -> list[FieldRef]
    filters: list = field(default_factory=list)
    sorts: list = field(default_factory=list)
    title: Optional[str] = None
    dual_axis: bool = False
    measure_values: list = field(default_factory=list)   # measures listed by the Measure Names filter

    xml_path: Optional[str] = None   # XPath of the source element (lineage)
    def all_fields(self):
        out = list(self.rows) + list(self.cols)
        for v in self.encodings.values():
            out.extend(v)
        return out


@dataclass
class DashboardZone:
    id: str
    type: str                      # worksheet|filter|paramctrl|text|title|image|legend|blank|layout|web|button
    name: Optional[str] = None
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    param: Optional[str] = None
    text: Optional[str] = None
    mode: Optional[str] = None


@dataclass
class Dashboard:
    name: str
    width: int = 1200
    height: int = 800
    zones: list = field(default_factory=list)
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


@dataclass
class Action:
    name: str
    type: str                      # filter|highlight|url|goto-sheet|parameter|set
    source_sheet: Optional[str] = None
    target: Optional[str] = None
    activation: str = "on-select"
    fields: list = field(default_factory=list)
    url: Optional[str] = None
    xml_path: Optional[str] = None   # XPath of the source element (lineage)


@dataclass
class Workbook:
    name: str
    version: Optional[str] = None
    source_build: Optional[str] = None
    datasources: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)     # internal -> Parameter
    worksheets: list = field(default_factory=list)
    dashboards: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    packaged_files: dict = field(default_factory=dict)  # name -> bytes (for twbx)
    warnings: list = field(default_factory=list)
    unknown_nodes: list = field(default_factory=list)   # [{"path", "tag"}] preserved for review
    parse_stats: dict = field(default_factory=dict)     # known / unknown element counts
    security_findings: list = field(default_factory=list)

    def datasource(self, name: str) -> Optional[Datasource]:
        for ds in self.datasources:
            if ds.name == name or ds.caption == name:
                return ds
        return None

    def worksheet(self, name: str) -> Optional[Worksheet]:
        for ws in self.worksheets:
            if ws.name == name:
                return ws
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("packaged_files", None)
        d["packaged_files"] = sorted(self.packaged_files)
        return d
