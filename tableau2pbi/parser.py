"""Tableau .twb / .twbx metadata reader.

Reads the complete workbook XML: connections, physical/logical tables, custom SQL,
joins and relationships, column metadata, calculated fields, groups, sets, bins,
parameters, hierarchies, folders, aliases, worksheets (shelves, marks, encodings,
filters, sorts), dashboards (zones, layout, quick filters, parameter controls)
and actions.
"""
from __future__ import annotations

import html
import io
import os
import re
import zipfile
from typing import Optional

from lxml import etree

from .model import (Action, Column, CalculatedField, Connection, Dashboard, DashboardZone,
                    Datasource, FieldRef, Filter, Hierarchy, LogicalTable, Parameter,
                    Relation, Relationship, Workbook, Worksheet)

FIELD_REF_RE = re.compile(r"\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]")


# ---------------------------------------------------------------- helpers --
def _local(tag) -> str:
    """Strip namespaces and Tableau's ``_.fcp.Feature.true...`` prefixes from a tag."""
    if not isinstance(tag, str):
        return ""
    tag = tag.split("}")[-1]
    if "..." in tag:
        tag = tag.split("...")[-1]
    return tag


def _fcp_enabled(tag: str) -> Optional[bool]:
    """For feature-flag tags (``_.fcp.X.true...relation``) return the flag value."""
    if isinstance(tag, str) and tag.startswith("_.fcp.") and "..." in tag:
        flag = tag.split("...")[0].rsplit(".", 1)[-1]
        return flag == "true"
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _iter(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            yield c


def _attr(el, name, default=None):
    if el is None:
        return default
    for k, v in el.attrib.items():
        if _local(k) == name or k.split("}")[-1] == name:
            return v
    return default


def strip_brackets(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s.replace("]]", "]")


def parse_member(raw: str):
    """Convert a Tableau member literal ('"East"', '2024', '#2024-01-01#') to python."""
    if raw is None:
        return None
    raw = html.unescape(raw)
    if raw == "%null%" or raw.lower() == "null":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('""', '"')
    if len(raw) >= 2 and raw[0] == raw[-1] == "#":
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        f = float(raw)
        return int(f) if f.is_integer() and "." not in raw else f
    except ValueError:
        return raw


def parse_field_ref(text: str) -> Optional[FieldRef]:
    m = FIELD_REF_RE.search(text or "")
    if not m:
        return None
    ds, inner = m.group(1), m.group(2)
    return parse_instance_name(ds, inner)


def parse_instance_name(ds: str, inner: str) -> Optional[FieldRef]:
    inner = inner.replace("]]", "]")
    if inner.startswith(":"):          # [:Measure Names], [:Measure Values]
        return FieldRef(ds, inner, "special", "nk")
    parts = inner.split(":")
    if len(parts) >= 3:
        if parts[-1].isdigit() and len(parts) >= 4:
            parts = parts[:-1]
        return FieldRef(ds, ":".join(parts[1:-1]), parts[0], parts[-1])
    return FieldRef(ds, inner, "none", "nk")


def parse_shelf(text: Optional[str]) -> list[FieldRef]:
    if not text:
        return []
    out = []
    for m in FIELD_REF_RE.finditer(text):
        ref = parse_instance_name(m.group(1), m.group(2))
        if ref and ref.derivation != "special":
            out.append(ref)
    return out


DATATYPE_FROM_REMOTE = {"3": "integer", "5": "real", "7": "date", "135": "datetime",
                        "11": "boolean", "129": "string", "130": "string", "20": "integer",
                        "131": "real"}


# ------------------------------------------------------------- the reader --
class TableauReader:
    def __init__(self, path: Optional[str] = None, data: Optional[bytes] = None,
                 filename: Optional[str] = None):
        self.path = path
        self.filename = filename or (os.path.basename(path) if path else "workbook.twb")
        self.data = data if data is not None else open(path, "rb").read()
        self.packaged: dict[str, bytes] = {}

    def _load_xml(self) -> bytes:
        if self.filename.lower().endswith(".twbx") or self.data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(self.data)) as z:
                twb_name = None
                for n in z.namelist():
                    if n.lower().endswith(".twb") and "/" not in n.strip("/"):
                        twb_name = n
                for n in z.namelist():
                    if n.lower().endswith(".twb") and twb_name is None:
                        twb_name = n
                    elif not n.endswith("/") and not n.lower().endswith(".twb"):
                        self.packaged[n] = z.read(n)
                if not twb_name:
                    raise ValueError("No .twb file found inside the .twbx package")
                return z.read(twb_name)
        return self.data

    def read(self) -> Workbook:
        xml = self._load_xml()
        parser = etree.XMLParser(recover=True, huge_tree=True, remove_comments=True)
        root = etree.fromstring(xml, parser)
        name = os.path.splitext(self.filename)[0]
        wb = Workbook(name=name, version=root.get("version"),
                      source_build=root.get("source-build"))
        wb.packaged_files = self.packaged

        ds_root = _child(root, "datasources")
        if ds_root is not None:
            for ds_el in _children(ds_root, "datasource"):
                if ds_el.get("name") == "Parameters":
                    self._read_parameters(ds_el, wb)
                else:
                    wb.datasources.append(self._read_datasource(ds_el, wb))

        ws_root = _child(root, "worksheets")
        if ws_root is not None:
            for ws_el in _children(ws_root, "worksheet"):
                wb.worksheets.append(self._read_worksheet(ws_el, wb))

        db_root = _child(root, "dashboards")
        if db_root is not None:
            for db_el in _children(db_root, "dashboard"):
                if db_el.get("type") == "storyboard":
                    wb.warnings.append(f"Story '{db_el.get('name')}' is converted as a dashboard page.")
                wb.dashboards.append(self._read_dashboard(db_el))

        act_root = _child(root, "actions")
        if act_root is not None:
            for a in _children(act_root, "action"):
                wb.actions.append(self._read_action(a))
        return wb

    # ------------------------------------------------------------ params --
    def _read_parameters(self, ds_el, wb: Workbook):
        for col in _children(ds_el, "column"):
            internal = strip_brackets(col.get("name"))
            dt = col.get("datatype", "string")
            calc = _child(col, "calculation")
            formula = calc.get("formula") if calc is not None else None
            value = parse_member(col.get("value")) if col.get("value") is not None else (
                parse_member(formula) if formula else None)
            p = Parameter(internal_name=internal, caption=col.get("caption") or internal,
                          datatype=dt, current_value=_coerce(value, dt),
                          domain_type=col.get("param-domain-type", "any"), formula=formula)
            rng = _child(col, "range")
            if rng is not None:
                p.min = _coerce(parse_member(rng.get("min")), dt) if rng.get("min") else None
                p.max = _coerce(parse_member(rng.get("max")), dt) if rng.get("max") else None
                p.step = _coerce(parse_member(rng.get("granularity")), dt) if rng.get("granularity") else None
            members = _child(col, "members")
            aliases = {}
            al = _child(col, "aliases")
            if al is not None:
                for a in _children(al, "alias"):
                    aliases[parse_member(a.get("key"))] = a.get("value")
            if members is not None:
                for m in _children(members, "member"):
                    v = _coerce(parse_member(m.get("value")), dt)
                    p.members.append((v, m.get("alias") or aliases.get(v)))
            wb.parameters[internal] = p

    # -------------------------------------------------------- datasource --
    def _read_datasource(self, ds_el, wb: Workbook) -> Datasource:
        ds = Datasource(name=ds_el.get("name"), caption=ds_el.get("caption") or ds_el.get("name"))
        conn_el = _child(ds_el, "connection")
        named = {}
        if conn_el is not None:
            if conn_el.get("class") == "federated":
                nc_root = _child(conn_el, "named-connections")
                if nc_root is not None:
                    for nc in _children(nc_root, "named-connection"):
                        inner = _child(nc, "connection")
                        if inner is not None:
                            c = self._read_connection(nc.get("name"), inner)
                            ds.connections.append(c)
                            named[c.name] = c
            else:
                c = self._read_connection(conn_el.get("class"), conn_el)
                ds.connections.append(c)
                named[c.name] = c

            # Physical / logical structure. Prefer the object-graph (2020.2+ relationships).
            graph = None
            for el in conn_el.iter():
                if _local(el.tag) == "object-graph":
                    graph = el
                    break
            if graph is None:
                for el in ds_el:
                    if _local(el.tag) == "object-graph":
                        graph = el
            if graph is not None and len(list(_iter(graph, "object"))) > 0:
                self._read_object_graph(graph, ds)
            else:
                rel_el = self._top_relation(conn_el)
                if rel_el is not None:
                    self._read_legacy_relation(rel_el, ds)

            # metadata records -> physical columns
            for rec in _iter(conn_el, "metadata-record"):
                if rec.get("class") != "column":
                    continue
                local = strip_brackets(_text(rec, "local-name"))
                if not local:
                    continue
                parent = strip_brackets(_text(rec, "parent-name"))
                remote_type = _text(rec, "remote-type")
                dt = _text(rec, "local-type") or DATATYPE_FROM_REMOTE.get(remote_type or "", "string")
                agg = _text(rec, "aggregation")
                col = Column(name=local, table=parent, remote_name=_text(rec, "remote-name"),
                             datatype=dt, aggregation=agg,
                             contains_null=(_text(rec, "contains-null") or "").lower() == "true")
                col.role = "measure" if dt in ("integer", "real") and agg not in (None, "Count", "Year") \
                    and not local.lower().endswith("id") else "dimension"
                col.type = "quantitative" if col.role == "measure" else "nominal"
                ds.columns[local] = col
            if any(True for _ in _iter(ds_el, "extract")):
                ex = next(_iter(ds_el, "extract"))
                ds.is_extract = ex.get("enabled", "true") == "true"

        # <column> definitions (captions, roles, calcs, groups, bins)
        for col_el in ds_el:
            if _local(col_el.tag) != "column":
                continue
            self._read_column(col_el, ds)
        for grp in _children(ds_el, "group"):
            self._read_group(grp, ds)
        dp_root = _child(ds_el, "drill-paths")
        if dp_root is not None:
            for dp in _children(dp_root, "drill-path"):
                levels = [strip_brackets(f.text) for f in _children(dp, "field")]
                ds.hierarchies.append(Hierarchy(dp.get("name"), levels))
        for folder in _iter(ds_el, "folder"):
            fname = folder.get("name")
            for item in _children(folder, "folder-item"):
                fld = strip_brackets(item.get("name"))
                if fld in ds.columns:
                    ds.columns[fld].folder = fname
                if fld in ds.calculations:
                    ds.calculations[fld].folder = fname
        for f in _children(ds_el, "filter"):
            flt = self._read_filter(f, ds.name)
            if flt:
                ds.datasource_filters.append(flt)
        return ds

    def _top_relation(self, conn_el):
        cands = [c for c in conn_el if _local(c.tag) == "relation"]
        if not cands:
            return None
        # Tableau writes both a legacy-encapsulated and a plain relation; prefer the
        # variant whose feature flag is false (the physical join tree).
        for c in cands:
            if _fcp_enabled(c.tag) is False:
                return c
        return cands[0]

    def _read_connection(self, name, el) -> Connection:
        cls = el.get("class", "unknown")
        c = Connection(name=name or cls, cls=cls, server=el.get("server"),
                       dbname=el.get("dbname"), schema=el.get("schema"),
                       username=el.get("username"), port=el.get("port"),
                       filename=el.get("filename") or el.get("directory"),
                       warehouse=el.get("warehouse"))
        if cls == "textscan" and el.get("directory") and el.get("filename"):
            c.filename = el.get("directory").rstrip("/") + "/" + el.get("filename")
        for k in ("authentication", "service", "project", "catalog", "http-path", "odbc-connect-string-extras",
                  "role", "dataset", "sslmode"):
            if el.get(k):
                c.extra[k] = el.get(k)
        return c

    def _relation_from_el(self, el) -> Relation:
        rtype = el.get("type", "table")
        rel = Relation(name=el.get("name") or "Table", type=rtype, connection=el.get("connection"),
                       table=el.get("table"))
        if rtype == "text":
            rel.sql = html.unescape("".join(el.itertext())).strip()
        return rel

    def _read_object_graph(self, graph, ds: Datasource):
        ds.model_style = "relationships"
        id_to_name = {}
        objs = next(_iter(graph, "objects"), None)
        for obj in (_children(objs, "object") if objs is not None else []):
            caption = obj.get("caption") or obj.get("id")
            id_to_name[obj.get("id")] = caption
            props = _child(obj, "properties")
            rel_el = None
            if props is not None:
                rel_el = next((c for c in props if _local(c.tag) == "relation"), None)
            rel = self._relation_from_el(rel_el) if rel_el is not None else Relation(caption, "table")
            if rel.type == "join":
                # a logical table built from a physical join: keep join tree
                rel = self._legacy_tree(rel_el)
            ds.tables.append(LogicalTable(caption, rel))
        rels = next(_iter(graph, "relationships"), None)
        for r in (_children(rels, "relationship") if rels is not None else []):
            first = _child(r, "first-end-point")
            second = _child(r, "second-end-point")
            expr = _child(r, "expression")
            pairs = _expr_pairs(expr)
            for lf, rf in pairs:
                ds.relationships.append(Relationship(
                    left_table=id_to_name.get(first.get("object-id"), first.get("object-id")),
                    left_column=strip_brackets(lf),
                    right_table=id_to_name.get(second.get("object-id"), second.get("object-id")),
                    right_column=strip_brackets(rf), join_type="relationship"))

    def _legacy_tree(self, el) -> Relation:
        rel = self._relation_from_el(el)
        if rel.type == "join":
            rel.join_type = el.get("join", "inner")
            clause = _child(el, "clause")
            if clause is not None:
                rel.clauses = _expr_pairs(_child(clause, "expression"))
            rel.children = [self._legacy_tree(c) for c in el if _local(c.tag) == "relation"]
        elif rel.type == "union":
            rel.children = [self._legacy_tree(c) for c in el if _local(c.tag) == "relation"]
        return rel

    def _read_legacy_relation(self, el, ds: Datasource):
        tree = self._legacy_tree(el)
        if tree.type in ("join",):
            ds.model_style = "joins"
            leaves = []

            def walk(r: Relation):
                if r.type == "join":
                    for ch in r.children:
                        walk(ch)
                    for lf, rf in r.clauses:
                        lt, lc = _split_qualified(lf)
                        rt, rc = _split_qualified(rf)
                        ds.relationships.append(Relationship(lt, lc, rt, rc, join_type=r.join_type))
                else:
                    leaves.append(r)
            walk(tree)
            for leaf in leaves:
                ds.tables.append(LogicalTable(leaf.name, leaf))
        else:
            ds.model_style = "single"
            ds.tables.append(LogicalTable(tree.name, tree))

    def _read_column(self, col_el, ds: Datasource):
        name = strip_brackets(col_el.get("name"))
        if not name or col_el.get("datatype") == "table" or name.startswith("__tableau_internal_object_id__"):
            return
        calc = _child(col_el, "calculation")
        desc = None
        d = _child(col_el, "desc")
        if d is not None:
            desc = " ".join(t.strip() for t in d.itertext() if t.strip())
        caption = col_el.get("caption")
        common = dict(datatype=col_el.get("datatype", "string"), role=col_el.get("role", "dimension"),
                      type=col_el.get("type", "nominal"))
        aliases_el = _child(col_el, "aliases")
        if aliases_el is not None:
            ds.aliases[name] = {parse_member(a.get("key")): a.get("value") for a in _children(aliases_el, "alias")}
        if calc is not None:
            cls = calc.get("class", "tableau")
            cf = CalculatedField(internal_name=name, caption=caption or name,
                                 formula=calc.get("formula") or "", hidden=col_el.get("hidden") == "true",
                                 description=desc, default_format=col_el.get("default-format"), **common)
            if cls == "categorical-bin":
                cf.kind = "group"
                cf.extra["source"] = strip_brackets(calc.get("column"))
                bins = []
                for b in _children(calc, "bin"):
                    label = parse_member(b.get("value"))
                    vals = [parse_member(v.text) for v in _children(b, "value")]
                    bins.append({"label": label, "values": vals})
                cf.extra["bins"] = bins
                cf.extra["other"] = calc.get("other")   # None => keep ungrouped values
            elif cls == "bin":
                cf.kind = "bin"
                cf.extra.update(source=strip_brackets(calc.get("formula")),
                                size=calc.get("size"), peg=calc.get("peg", "0"))
            elif not cf.formula and cls != "tableau":
                return
            ds.calculations[name] = cf
            return
        col = ds.columns.get(name)
        if col is None:
            col = Column(name=name, table=None, remote_name=None)
            ds.columns[name] = col
        col.caption = caption or col.caption
        col.datatype = common["datatype"] or col.datatype
        col.role = common["role"] or col.role
        col.type = common["type"] or col.type
        col.hidden = col_el.get("hidden") == "true"
        col.description = desc
        col.default_format = col_el.get("default-format")
        col.semantic_role = col_el.get("semantic-role")
        if col_el.get("aggregation"):
            col.aggregation = col_el.get("aggregation")

    def _read_group(self, grp, ds: Datasource):
        name = strip_brackets(grp.get("name"))
        caption = grp.get("caption") or name
        gf = _child(grp, "groupfilter")
        cf = CalculatedField(internal_name=name, caption=caption, formula="", datatype="boolean",
                             role="dimension", type="nominal", kind="set")
        if gf is None:
            return
        func = gf.get("function")
        level = None
        members = []
        for g in gf.iter():
            if _local(g.tag) != "groupfilter":
                continue
            if g.get("level"):
                level = level or g.get("level")
            if g.get("function") == "member":
                members.append(parse_member(g.get("member")))
        ref = parse_instance_name(ds.name, strip_brackets(level)) if level else None
        cf.extra["source"] = ref.field if ref else None
        if func in ("union", "member"):
            cf.extra["set_type"] = "members"
            cf.extra["members"] = members
        elif func == "end":
            order = next((g for g in gf.iter() if _local(g.tag) == "groupfilter" and g.get("function") == "order"), None)
            cf.extra.update(set_type="top", count=gf.get("count"), end=gf.get("end", "top"),
                            expression=order.get("expression") if order is not None else None,
                            direction=order.get("direction", "DESC") if order is not None else "DESC")
        elif func == "filter":
            cf.extra.update(set_type="condition", expression=gf.get("expression"))
        else:
            cf.extra["set_type"] = func
            cf.extra["members"] = members
        ds.calculations[name] = cf

    # --------------------------------------------------------- worksheet --
    def _read_worksheet(self, ws_el, wb: Workbook) -> Worksheet:
        ws = Worksheet(name=ws_el.get("name"))
        table = _child(ws_el, "table")
        if table is None:
            return ws
        view = _child(table, "view")
        tc_by_instance = {}
        if view is not None:
            dss = _child(view, "datasources")
            if dss is not None:
                ws.datasources = [d.get("name") for d in _children(dss, "datasource")]
            for dep in _children(view, "datasource-dependencies"):
                for ci in _children(dep, "column-instance"):
                    tc = _child(ci, "table-calc")
                    if tc is not None:
                        order_fields = [strip_brackets(o.get("field")) for o in _children(tc, "order")]
                        tc_by_instance[(dep.get("datasource"), strip_brackets(ci.get("name")))] = {
                            "ordering_type": tc.get("ordering-type", "Rows"),
                            "ordering_fields": order_fields, "type": tc.get("type")}
            for f in _children(view, "filter"):
                flt = self._read_filter(f, None)
                if flt:
                    ws.filters.append(flt)
            for s in _iter(view, "sort"):
                ref = parse_field_ref(s.get("column", ""))
                using = parse_field_ref(s.get("using", "")) if s.get("using") else None
                if ref:
                    ws.sorts.append({"field": ref, "direction": s.get("direction", "ASC"),
                                     "using": using, "class": s.get("class")})
        ws.rows = parse_shelf(_text(table, "rows"))
        ws.cols = parse_shelf(_text(table, "cols"))
        for ref in ws.rows + ws.cols:
            tc = tc_by_instance.get((ref.datasource, f"{ref.derivation}:{ref.field}:{ref.pill_type}"))
            if tc:
                ref.table_calc = tc
        panes = _child(table, "panes")
        if panes is not None:
            for pane in _children(panes, "pane"):
                mark = _child(pane, "mark")
                if mark is not None and mark.get("class"):
                    if ws.mark == "Automatic" or pane.get("id") in (None, "0"):
                        ws.mark = mark.get("class")
                enc = _child(pane, "encodings")
                if enc is not None:
                    for e in enc:
                        refs = parse_shelf(e.get("column", ""))
                        if refs:
                            ws.encodings.setdefault(_local(e.tag), []).extend(refs)
                if pane.get("y-axis-name") or pane.get("x-axis-name"):
                    pass
            if len(_children(panes, "pane")) > 2:
                ws.dual_axis = True
        title = None
        for t in _iter(ws_el, "title"):
            title = " ".join(x.strip() for x in t.itertext() if x.strip())
            break
        ws.title = title
        return ws

    def _read_filter(self, f, ds_name) -> Optional[Filter]:
        cls = f.get("class")
        ref = parse_field_ref(f.get("column", ""))
        if ref is None:
            return None
        flt = Filter(kind=cls or "categorical", datasource=ref.datasource, field=ref,
                     is_context=f.get("context") == "true")
        if cls == "categorical":
            gf = _child(f, "groupfilter")
            if gf is not None:
                func = gf.get("function")
                if func == "end":
                    order = next((g for g in gf.iter() if _local(g.tag) == "groupfilter"
                                  and g.get("function") == "order"), None)
                    flt.kind = "top"
                    count = gf.get("count", "10")
                    if count.startswith("["):
                        ref_p = parse_field_ref(count)
                        flt.top_param = ref_p.field if ref_p else strip_brackets(count)
                        flt.top_n = None
                    else:
                        flt.top_n = int(float(count))
                    flt.top_direction = gf.get("end", "top")
                    flt.top_by = order.get("expression") if order is not None else None
                elif func == "except":
                    flt.exclude = True
                    flt.members = [parse_member(g.get("member")) for g in gf.iter()
                                   if _local(g.tag) == "groupfilter" and g.get("function") == "member"]
                elif func in ("union", "member"):
                    flt.members = [parse_member(g.get("member")) for g in gf.iter()
                                   if _local(g.tag) == "groupfilter" and g.get("function") == "member"]
                elif func == "level-members":
                    flt.members = []          # all values
                    flt.kind = "categorical-all"
                elif func == "filter":
                    flt.kind = "condition"
                    flt.top_by = gf.get("expression")
                else:
                    flt.raw = etree.tostring(gf).decode()
        elif cls == "quantitative":
            mn, mx = _text(f, "min"), _text(f, "max")
            flt.min = parse_member(mn) if mn is not None else None
            flt.max = parse_member(mx) if mx is not None else None
            flt.include_null = f.get("included-values") in ("all", "in-range-or-null")
        elif cls == "relative-date":
            flt.period_type = f.get("period-type", "day")
            flt.first_period = int(f.get("first-period", "0"))
            flt.last_period = int(f.get("last-period", "0"))
            flt.include_null = f.get("include-null") == "true"
            if f.get("anchor"):
                flt.raw = parse_member(f.get("anchor"))
        return flt

    # --------------------------------------------------------- dashboard --
    def _read_dashboard(self, el) -> Dashboard:
        db = Dashboard(name=el.get("name"))
        size = _child(el, "size")
        if size is not None:
            db.width = int(size.get("maxwidth") or size.get("minwidth") or 1200)
            db.height = int(size.get("maxheight") or size.get("minheight") or 800)
        zones = _child(el, "zones")

        def walk(z, depth=0):
            ztype = z.get("type") or z.get("type-v2")
            name = z.get("name")
            if ztype is None and name:
                ztype = "worksheet"
            if ztype in ("layout-basic", "layout-flow"):
                ztype = "layout"
            text = None
            ft = _child(z, "formatted-text")
            if ft is not None:
                text = " ".join(t.strip() for t in ft.itertext() if t.strip())
            dz = DashboardZone(id=z.get("id"), type=ztype or "layout", name=name,
                               x=float(z.get("x", 0)), y=float(z.get("y", 0)),
                               w=float(z.get("w", 0)), h=float(z.get("h", 0)),
                               param=z.get("param"), text=text, mode=z.get("mode"))
            if dz.type != "layout":
                db.zones.append(dz)
            for c in z:
                if _local(c.tag) == "zone":
                    walk(c, depth + 1)
        if zones is not None:
            for z in _children(zones, "zone"):
                walk(z)
        return db

    def _read_action(self, a) -> Action:
        cmd = _child(a, "command")
        command = cmd.get("command", "") if cmd is not None else ""
        kind = {"tsc:tsl-filter": "filter", "tsc:brush": "highlight", "tsc:browser": "url",
                "tsc:goto-sheet": "goto-sheet", "tsc:change-parameter": "parameter",
                "tsc:edit-set": "set"}.get(command, command or "unknown")
        src = _child(a, "source")
        act = _child(a, "activation")
        out = Action(name=a.get("caption") or strip_brackets(a.get("name")), type=kind,
                     source_sheet=(src.get("worksheet") or src.get("dashboard")) if src is not None else None,
                     activation=act.get("type", "on-select") if act is not None else "on-select")
        if cmd is not None:
            for p in _children(cmd, "param"):
                if p.get("name") in ("target", "worksheet"):
                    out.target = p.get("value")
                if p.get("name") == "url":
                    out.url = p.get("value")
                if p.get("name") in ("field-captions", "target-fields"):
                    out.fields = [x.strip() for x in (p.get("value") or "").split(",") if x.strip()]
        return out


# ---------------------------------------------------------------- utils --
def _text(el, name) -> Optional[str]:
    c = _child(el, name)
    if c is None:
        return None
    return c.text


def _expr_pairs(expr) -> list:
    """Extract (left, right) operand pairs from an equality join expression tree."""
    out = []
    if expr is None:
        return out
    op = expr.get("op")
    subs = _children(expr, "expression")
    if op in ("=", "==") and len(subs) == 2:
        out.append((subs[0].get("op"), subs[1].get("op")))
    elif op and op.upper() == "AND":
        for s in subs:
            out += _expr_pairs(s)
    return out


def _split_qualified(text: str):
    """'[Orders].[customer_id]' -> ('Orders', 'customer_id')"""
    m = FIELD_REF_RE.search(text or "")
    if m:
        return m.group(1), m.group(2)
    return None, strip_brackets(text)


def _coerce(v, dt):
    if v is None:
        return None
    try:
        if dt == "integer":
            return int(float(v))
        if dt == "real":
            return float(v)
        if dt == "boolean":
            return v if isinstance(v, bool) else str(v).lower() == "true"
    except (TypeError, ValueError):
        return v
    return v


def read_workbook(path: Optional[str] = None, data: Optional[bytes] = None,
                  filename: Optional[str] = None) -> Workbook:
    return TableauReader(path, data, filename).read()
