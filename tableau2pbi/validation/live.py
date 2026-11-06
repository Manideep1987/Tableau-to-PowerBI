"""Live validation against Tableau Server / Cloud and the Power BI service.

* Tableau: REST API sign-in with a Personal Access Token, then
  ``GET /api/{v}/sites/{site}/views/{view}/data`` (CSV of the view's underlying
  summary data; ``vf_<field>=value`` applies filters).
* Power BI: ``POST /v1.0/myorg[/groups/{g}]/datasets/{id}/executeQueries`` with the
  DAX query the validator generates for each converted visual.

Both results are normalised and compared with :func:`compare_exports`.
"""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
from typing import Optional

import pandas as pd

from ..converter import ConversionResult
from ..model import FieldRef
from .compare import compare_exports


class LiveError(Exception):
    pass


def _http(method, url, headers=None, body=None, timeout=60):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise LiveError(f"{method} {url} -> HTTP {e.code}: {e.read()[:300]!r}") from e


class TableauServerClient:
    def __init__(self, server: str, site_content_url: str, token_name: str, token_secret: str,
                 api_version: str = "3.22"):
        self.server = server.rstrip("/")
        self.site = site_content_url
        self.token_name, self.token_secret = token_name, token_secret
        self.v = api_version
        self.token = None
        self.site_id = None

    @property
    def base(self):
        return f"{self.server}/api/{self.v}"

    def _h(self):
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["X-Tableau-Auth"] = self.token
        return h

    def sign_in(self):
        body = {"credentials": {"personalAccessTokenName": self.token_name,
                                "personalAccessTokenSecret": self.token_secret,
                                "site": {"contentUrl": self.site}}}
        _, raw = _http("POST", f"{self.base}/auth/signin", self._h(), body)
        cred = json.loads(raw)["credentials"]
        self.token, self.site_id = cred["token"], cred["site"]["id"]
        return self

    def sign_out(self):
        if self.token:
            try:
                _http("POST", f"{self.base}/auth/signout", self._h(), b"")
            finally:
                self.token = None

    def find_view(self, workbook: str, view: str) -> str:
        q = urllib.parse.quote(f"name:eq:{view}")
        _, raw = _http("GET", f"{self.base}/sites/{self.site_id}/views?filter={q}&pageSize=1000", self._h())
        views = json.loads(raw).get("views", {}).get("view", [])
        for v in views:
            if not workbook or v.get("workbook", {}).get("name") in (workbook, None) or len(views) == 1:
                return v["id"]
        raise LiveError(f"View '{view}' not found in workbook '{workbook}'")

    def view_data(self, view_id: str, filters: Optional[dict] = None) -> pd.DataFrame:
        qs = "&".join(f"vf_{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}" for k, v in (filters or {}).items())
        url = f"{self.base}/sites/{self.site_id}/views/{view_id}/data" + (f"?{qs}" if qs else "")
        _, raw = _http("GET", url, self._h())
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        return unpivot_measure_values(df)


class PowerBIClient:
    def __init__(self, access_token: str, base_url: str = "https://api.powerbi.com/v1.0/myorg"):
        self.token = access_token
        self.base = base_url.rstrip("/")

    def execute_query(self, dataset_id: str, dax: str, group_id: Optional[str] = None) -> pd.DataFrame:
        path = f"/groups/{group_id}" if group_id else ""
        url = f"{self.base}{path}/datasets/{dataset_id}/executeQueries"
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        _, raw = _http("POST", url, {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, body)
        res = json.loads(raw)
        try:
            rows = res["results"][0]["tables"][0]["rows"]
        except (KeyError, IndexError) as e:
            raise LiveError(f"Unexpected executeQueries response: {str(res)[:300]}") from e
        df = pd.DataFrame(rows)
        df.columns = [_pbi_col(c) for c in df.columns]
        return df


def _pbi_col(c: str) -> str:
    m = re.match(r"^(?:'?[^\[']*'?)?\[(.+)\]$", c)
    return m.group(1) if m else c


def unpivot_measure_values(df: pd.DataFrame) -> pd.DataFrame:
    """Tableau view data can be long format (Measure Names / Measure Values) -> wide."""
    cols = {c.lower(): c for c in df.columns}
    if "measure names" in cols and "measure values" in cols:
        mn, mv = cols["measure names"], cols["measure values"]
        keys = [c for c in df.columns if c not in (mn, mv)]
        wide = df.pivot_table(index=keys, columns=mn, values=mv, aggfunc="first").reset_index() if keys else \
            df.set_index(mn)[[mv]].T.reset_index(drop=True)
        wide.columns.name = None
        return wide
    return df


def tableau_caption(res: ConversionResult, ref: FieldRef) -> str:
    ds = res.workbook.datasource(ref.datasource)
    name = ref.field
    if ds:
        if ref.field in ds.columns and ds.columns[ref.field].caption:
            name = ds.columns[ref.field].caption
        elif ref.field in ds.calculations:
            name = ds.calculations[ref.field].caption
    prefix = {"yr": "Year of ", "qr": "Quarter of ", "mn": "Month of ", "tmn": "Month of ", "tqr": "Quarter of ",
              "tyr": "Year of ", "dy": "Day of ", "tdy": "Day of ", "wk": "Week of ", "twk": "Week of ",
              "ctd": "Distinct count of ", "cnt": "Count of ", "avg": "Average of ", "min": "Minimum of ",
              "max": "Maximum of ", "med": "Median of "}.get(ref.derivation, "")
    return prefix + name


def live_validate(res: ConversionResult, tableau: TableauServerClient, powerbi: PowerBIClient,
                  dataset_id: str, group_id: Optional[str] = None, views: Optional[dict] = None,
                  abs_tol=0.01, rel_tol=1e-6) -> list:
    """Compare every worksheet's published view with the converted visual's DAX query."""
    from .runner import dax_query_text
    out = []
    for ws in res.workbook.worksheets:
        visual = res.worksheet_visuals.get(ws.name)
        if visual is None or not visual.measures:
            continue
        refs = [r for r in ws.rows + ws.cols + [x for v in ws.encodings.values() for x in v]]
        dims = [r for r in refs if (ws.name, r.key) in res.field_map and isinstance(res.field_map[(ws.name, r.key)], tuple)]
        meas = [r for r in refs if (ws.name, r.key) in res.field_map and isinstance(res.field_map[(ws.name, r.key)], str)]
        dax = dax_query_text(res, ws, [d.key for d in dims], [m.key for m in meas], visual)
        try:
            view_id = (views or {}).get(ws.name) or tableau.find_view(res.workbook.name, ws.name)
            tdf = tableau.view_data(view_id)
            pdf = powerbi.execute_query(dataset_id, dax, group_id)
        except LiveError as e:
            out.append({"worksheet": ws.name, "status": "ERROR", "detail": str(e), "dax": dax})
            continue
        # align column names: Power BI columns -> Tableau captions
        rename = {}
        for d in dims:
            rename[res.field_map[(ws.name, d.key)][1]] = tableau_caption(res, d)
        for m in meas:
            rename[res.field_map[(ws.name, m.key)]] = tableau_caption(res, m)
        pdf = pdf.rename(columns=rename)
        keys = [tableau_caption(res, d) for d in dims]
        pairs = [(tableau_caption(res, m), tableau_caption(res, m)) for m in meas
                 if tableau_caption(res, m).lower() in {c.lower() for c in tdf.columns}]
        cmp = compare_exports(tdf, pdf, keys, pairs, abs_tol, rel_tol)
        out.append({"worksheet": ws.name, "status": cmp["status"], "summary": cmp["summary"], "dax": dax,
                    "rows": cmp["rows"][:50]})
    return out
