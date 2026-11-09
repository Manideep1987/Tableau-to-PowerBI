"""Tableau connections / custom SQL -> Power Query (M) source expressions."""
from __future__ import annotations

import re
from typing import Optional

from .model import Connection, Relation

SQL_PARAM_RE = re.compile(r"<\[?Parameters\]?\.\[([^\]]+)\]>|<Parameters\.([^>]+)>")


def m_str(s: str) -> str:
    return '"' + (s or "").replace('"', '""') + '"'


def m_ident(s: str) -> str:
    return '#"' + s.replace('"', '""') + '"'


def split_table(table: Optional[str]):
    """'[dbo].[Orders]' -> ('dbo', 'Orders');  '[Orders$]' -> (None, 'Orders$')"""
    if not table:
        return None, None
    parts = re.findall(r"\[((?:[^\]]|\]\])+)\]", table) or [table]
    parts = [p.replace("]]", "]") for p in parts]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None, parts[0]


def source_step(conn: Connection, server_param: Optional[str] = None,
                db_param: Optional[str] = None) -> tuple[str, list[str]]:
    """Return (M expression for the database/source step, notes)."""
    notes = []
    srv = server_param or m_str(conn.server or "")
    db = db_param or m_str(conn.dbname or "")
    cls = conn.cls
    if cls in ("sqlserver", "azure_sql_dw", "azure_sqldb"):
        return f"Sql.Database({srv}, {db})", notes
    if cls == "postgres":
        return f"PostgreSQL.Database({srv}, {db})", notes
    if cls == "mysql":
        return f"MySQL.Database({srv}, {db})", notes
    if cls == "oracle":
        return f"Oracle.Database({srv})", notes
    if cls == "redshift":
        return f"AmazonRedshift.Database({srv}, {db})", notes
    if cls == "teradata":
        return f"Teradata.Database({srv})", notes
    if cls == "snowflake":
        wh = m_str(conn.warehouse or "")
        return f"Snowflake.Databases({srv}, {wh})", notes
    if cls in ("bigquery", "google-bigquery"):
        return "GoogleBigQuery.Database()", notes
    if cls in ("databricks", "spark"):
        return f"Databricks.Catalogs({srv}, {m_str(conn.extra.get('http-path', ''))})", notes
    if cls == "sqlite":
        notes.append("SQLite is reached through ODBC in Power BI; install an SQLite ODBC driver.")
        return f"Odbc.DataSource(\"driver={{SQLite3 ODBC Driver}};database=\" & {db})", notes
    if cls == "excel-direct":
        return f"Excel.Workbook(File.Contents({db_param or m_str(conn.filename or '')}), null, true)", notes
    if cls == "textscan":
        return f"File.Contents({db_param or m_str(conn.filename or '')})", notes
    if cls == "hyper" or cls == "dataengine":
        notes.append("Tableau extract (.hyper) cannot be read by Power BI; re-point to the original source.")
        return "#table({}, {})", notes
    notes.append(f"Connector '{cls}' has no direct mapping; generic ODBC used.")
    return f"Odbc.DataSource({m_str('dsn=' + (conn.server or cls))})", notes


def sql_to_m_expr(sql: str, param_lookup=None) -> tuple[str, list]:
    """Turn custom SQL into an M text expression; Tableau '<Parameters.X>' tokens become
    concatenated Power Query parameters."""
    used, parts, pos = [], [], 0
    for m in SQL_PARAM_RE.finditer(sql):
        name = m.group(1) or m.group(2)
        p = param_lookup(name) if param_lookup else None
        if p is None:
            continue
        parts.append(m_str(sql[pos:m.start()]))
        if p.datatype == "string":
            parts.append(f"\"'\" & {m_ident(p.caption)} & \"'\"")
        else:
            parts.append(f"Text.From({m_ident(p.caption)})")
        used.append(p)
        pos = m.end()
    parts.append(m_str(sql[pos:]))
    return " & ".join(x for x in parts if x != '""') or '""', used


def table_query(conn: Connection, rel: Relation, columns: list, param_lookup=None,
                server_param=None, db_param=None) -> tuple[str, list[str]]:
    """Full ``let ... in`` partition expression for one logical table.

    ``columns`` is a list of (source_name, target_name, datatype)."""
    src, notes = source_step(conn, server_param, db_param)
    steps = [f"Source = {src}"]
    if rel.type == "text" and rel.sql:
        sql_m, used = sql_to_m_expr(rel.sql, param_lookup)
        if used:
            notes.append("Custom SQL parameters converted to Power Query parameters: "
                         + ", ".join(p.caption for p in used))
        steps.append(f"Data = Value.NativeQuery(Source, {sql_m}, null, [EnableFolding = true])")
        notes.append("Custom SQL is sent as a native query (query folding enabled).")
    elif conn.cls == "excel-direct":
        _, sheet = split_table(rel.table)
        sheet = (sheet or rel.name).rstrip("$")
        steps.append(f"Sheet = Source{{[Item = {m_str(sheet)}, Kind = \"Sheet\"]}}[Data]")
        steps.append("Data = Table.PromoteHeaders(Sheet, [PromoteAllScalars = true])")
    elif conn.cls == "textscan":
        steps.append("Csv = Csv.Document(Source, [Delimiter = \",\", Encoding = 65001, QuoteStyle = QuoteStyle.Csv])")
        steps.append("Data = Table.PromoteHeaders(Csv, [PromoteAllScalars = true])")
    else:
        schema, table = split_table(rel.table)
        if conn.cls == "snowflake":
            steps.append(f"Db = Source{{[Name = {m_str(conn.dbname or '')}, Kind = \"Database\"]}}[Data]")
            steps.append(f"Sch = Db{{[Name = {m_str(schema or 'PUBLIC')}, Kind = \"Schema\"]}}[Data]")
            steps.append(f"Data = Sch{{[Name = {m_str(table)}, Kind = \"Table\"]}}[Data]")
        elif schema:
            steps.append(f"Data = Source{{[Schema = {m_str(schema)}, Item = {m_str(table)}]}}[Data]")
        else:
            steps.append(f"Data = Source{{[Item = {m_str(table)}]}}[Data]")
    last = "Data"
    renames = [(s, t) for s, t, _ in columns if s and s != t]
    if columns:
        keep = ", ".join(m_str(s) for s, _, _ in columns if s)
        steps.append(f"Selected = Table.SelectColumns({last}, {{{keep}}}, MissingField.UseNull)")
        last = "Selected"
    if renames:
        pairs = ", ".join(f"{{{m_str(s)}, {m_str(t)}}}" for s, t in renames)
        steps.append(f"Renamed = Table.RenameColumns({last}, {{{pairs}}})")
        last = "Renamed"
    types = {"integer": "Int64.Type", "real": "type number", "string": "type text",
             "date": "type date", "datetime": "type datetime", "boolean": "type logical"}
    if columns:
        pairs = ", ".join(f"{{{m_str(t)}, {types.get(dt, 'type any')}}}" for _, t, dt in columns)
        steps.append(f"Typed = Table.TransformColumnTypes({last}, {{{pairs}}})")
        last = "Typed"
    body = ",\n    ".join(steps)
    return f"let\n    {body}\nin\n    {last}", notes


def lint_sql(sql: str) -> list[str]:
    """Best-practice checks for custom SQL moved into Power BI."""
    issues = []
    s = re.sub(r"\s+", " ", sql or "").strip()
    u = s.upper()
    if re.search(r"SELECT\s+\*", u):
        issues.append("SELECT * - list columns explicitly so schema changes do not break refresh.")
    if " ORDER BY " in f" {u} ":
        issues.append("ORDER BY inside a source query is ignored by Power BI and prevents folding in some sources.")
    if SQL_PARAM_RE.search(sql or ""):
        issues.append("Tableau parameters in SQL were converted to Power Query parameters (static at refresh).")
    if re.search(r"\bTOP\s+\d+|\bLIMIT\s+\d+", u):
        issues.append("Row-limiting clause (TOP/LIMIT) found - confirm it is intended for production.")
    if u.count("SELECT") > 3:
        issues.append("Complex SQL (nested selects) - consider a database view for maintainability.")
    return issues
