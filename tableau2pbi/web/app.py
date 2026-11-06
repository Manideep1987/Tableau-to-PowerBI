"""FastAPI web application: upload -> convert -> review -> validate -> download."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import uuid
from datetime import date
from typing import List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..catalog import catalog
from ..converter import convert
from ..formula.ast import LOD, is_aggregate
from ..formula.dax import DaxTranslator, TranslationError
from ..formula.parser import FormulaError, parse_formula
from ..parser import read_workbook
from ..pbip.writer import pbip_zip
from ..report import conversion_json, dax_mapping_csv, migration_report_html, validation_report_html
from ..validation.compare import compare_exports
from ..validation.runner import DataBinding, validate

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLES = os.path.join(ROOT, "samples", "workbooks")
SAMPLE_DB = os.path.join(ROOT, "samples", "data", "superstore.db")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_UPLOAD = 200 * 1024 * 1024

app = FastAPI(title="Tableau to Power BI Migrator", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
JOBS: dict = {}


def _job(job_id):
    j = JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Conversion not found - convert the workbook again")
    return j


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__, "jobs": len(JOBS)}


@app.get("/api/samples")
def samples():
    out = []
    desc = {
        "Superstore_Sales.twb": "Relationships model · 26 calcs (LOD, table calcs, groups, sets, bins) · 4 parameters · 16 sheets · 2 dashboards",
        "Finance_CustomSQL.twb": "Custom SQL with a parameter · physical inner join · extract · LOOKUP / WINDOW_AVG",
        "HR_Analytics.twbx": "Packaged workbook with CSV data · NULL handling · FIXED LOD · RUNNING_SUM",
    }
    for f in sorted(os.listdir(SAMPLES)) if os.path.isdir(SAMPLES) else []:
        if f.lower().endswith((".twb", ".twbx")):
            out.append({"name": f, "description": desc.get(f, ""), "size": os.path.getsize(os.path.join(SAMPLES, f))})
    return out


def _do_convert(data: bytes, filename: str, join_strategy: str, sample: Optional[str]):
    t0 = time.time()
    try:
        wb = read_workbook(data=data, filename=filename)
    except Exception as e:
        raise HTTPException(400, f"Could not read the Tableau workbook: {e}")
    from ..converter import Converter
    conv = Converter(wb, join_strategy)
    res = conv.convert()
    job_id = uuid.uuid4().hex[:12]
    payload = conversion_json(res)
    payload["job_id"] = job_id
    payload["filename"] = filename
    payload["sample"] = sample
    payload["duration_s"] = round(time.time() - t0, 3)
    JOBS[job_id] = {"res": res, "conv": conv, "json": payload, "filename": filename, "sample": sample, "validation": None,
                    "join_strategy": join_strategy}
    return payload


@app.post("/api/convert")
async def api_convert(file: Optional[UploadFile] = File(None), sample: Optional[str] = Form(None),
                      join_strategy: str = Form("flatten")):
    if join_strategy not in ("flatten", "star"):
        raise HTTPException(400, "join_strategy must be 'flatten' or 'star'")
    if file is not None and file.filename:
        if not file.filename.lower().endswith((".twb", ".twbx")):
            raise HTTPException(400, "Upload a Tableau workbook (.twb or .twbx)")
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File is larger than 200 MB")
        return _do_convert(data, file.filename, join_strategy, None)
    if sample:
        path = os.path.join(SAMPLES, os.path.basename(sample))
        if not os.path.isfile(path):
            raise HTTPException(404, "Sample not found")
        with open(path, "rb") as f:
            return _do_convert(f.read(), os.path.basename(path), join_strategy, os.path.basename(path))
    raise HTTPException(400, "Provide a workbook file or choose a sample")


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = _job(job_id)
    out = dict(j["json"])
    out["validation"] = j["validation"]
    return out


@app.get("/api/jobs/{job_id}/pbip")
def api_pbip(job_id: str):
    j = _job(job_id)
    res = j["res"]
    extra = {"MIGRATION_REPORT.html": migration_report_html(res, j["validation"]),
             "DAX_MAPPING.csv": dax_mapping_csv(res),
             "conversion.json": json.dumps(j["json"], indent=2, default=str)}
    if j["validation"]:
        extra["VALIDATION_REPORT.html"] = validation_report_html(j["validation"])
        extra["validation.json"] = json.dumps(j["validation"], indent=2, default=str)
    data = pbip_zip(res, extra)
    name = os.path.splitext(j["filename"])[0]
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}_PowerBI.zip"'})


@app.get("/api/jobs/{job_id}/report", response_class=HTMLResponse)
def api_report(job_id: str):
    j = _job(job_id)
    return migration_report_html(j["res"], j["validation"])


@app.get("/api/jobs/{job_id}/mapping.csv")
def api_mapping(job_id: str):
    j = _job(job_id)
    return Response(dax_mapping_csv(j["res"]), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="DAX_MAPPING.csv"'})


@app.post("/api/jobs/{job_id}/validate")
async def api_validate(job_id: str, use_sample_data: bool = Form(False), sqlite: Optional[UploadFile] = File(None),
                       csvs: Optional[List[UploadFile]] = File(None), abs_tol: float = Form(0.01),
                       rel_tol: float = Form(0.000001), as_of: Optional[str] = Form(None),
                       scenarios: bool = Form(True)):
    j = _job(job_id)
    res = j["res"]
    tmp = tempfile.mkdtemp(prefix="t2p_")
    db_path, frames = None, {}
    if sqlite is not None and sqlite.filename:
        db_path = os.path.join(tmp, "source.db")
        with open(db_path, "wb") as f:
            f.write(await sqlite.read())
    elif use_sample_data and os.path.isfile(SAMPLE_DB):
        db_path = SAMPLE_DB
    for up in csvs or []:
        if up.filename:
            name = os.path.splitext(os.path.basename(up.filename))[0]
            frames[name] = pd.read_csv(io.BytesIO(await up.read()), dtype=str, keep_default_na=False)
            frames[os.path.basename(up.filename)] = frames[name]
    binding = DataBinding(sqlite=db_path, frames=frames, packaged=res.workbook.packaged_files)
    if not (db_path or frames or res.workbook.packaged_files):
        raise HTTPException(400, "Provide source data: the sample database, a SQLite file or CSV files")
    try:
        today = date.fromisoformat(as_of) if as_of else date(2025, 12, 31) if j["sample"] else date.today()
    except ValueError:
        raise HTTPException(400, "as_of must be YYYY-MM-DD")
    rep = validate(res, binding, today=today, abs_tol=abs_tol, rel_tol=rel_tol, scenarios=scenarios).to_dict()
    j["validation"] = json.loads(json.dumps(rep, default=str))
    return j["validation"]


@app.get("/api/jobs/{job_id}/validation/report", response_class=HTMLResponse)
def api_validation_report(job_id: str):
    j = _job(job_id)
    if not j["validation"]:
        raise HTTPException(404, "Run the validation first")
    return validation_report_html(j["validation"])


@app.post("/api/translate")
async def api_translate(payload: dict):
    formula = (payload or {}).get("formula", "")
    job_id = (payload or {}).get("job_id")
    if not formula.strip():
        raise HTTPException(400, "Enter a Tableau formula")
    try:
        ast = parse_formula(formula)
    except FormulaError as e:
        return {"ok": False, "error": f"Syntax error: {e}"}
    if job_id and job_id in JOBS:
        from ..converter import _DsResolver
        res = JOBS[job_id]["res"]
        conv = JOBS[job_id]["conv"]
        resolver = _DsResolver(conv, res.workbook.datasources[0])
        schema = f"Workbook: {res.workbook.name}"
    else:
        from ..catalog import DemoResolver
        resolver = DemoResolver()
        schema = "Demo schema (Orders / Customers / Products)"
    tr = DaxTranslator(resolver)
    try:
        if isinstance(ast, LOD) and ast.kind in ("INCLUDE", "EXCLUDE"):
            r = tr.lod_measure(ast, "sum")
        else:
            agg = is_aggregate(ast, resolver.calc) or any(
                getattr(n, "name", "") in __import__("tableau2pbi.formula.ast", fromlist=["TABLE_CALCS"]).TABLE_CALCS
                for n in ast.walk())
            addressing = None
            if not job_id:
                addressing = [("Products", "Category")]
            r = tr.translate(ast, "agg" if agg else "row", addressing=addressing)
    except TranslationError as e:
        return {"ok": False, "error": str(e), "schema": schema}
    return {"ok": True, "dax": r.expression, "object": "Measure" if r.mode == "agg" else "Calculated column",
            "status": r.status, "warnings": r.warnings, "functions": sorted(r.functions), "schema": schema}


@app.get("/api/catalog")
def api_catalog():
    return catalog()


@app.post("/api/compare")
async def api_compare(tableau: UploadFile = File(...), powerbi: UploadFile = File(...),
                      keys: Optional[str] = Form(None), abs_tol: float = Form(0.01)):
    def read(up, data):
        if up.filename.lower().endswith((".xlsx", ".xls")):
            return pd.read_excel(io.BytesIO(data), dtype=str)
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, sep=None, engine="python")
    t = read(tableau, await tableau.read())
    p = read(powerbi, await powerbi.read())
    k = [x.strip() for x in keys.split(",")] if keys else None
    try:
        return json.loads(json.dumps(compare_exports(t, p, k, None, abs_tol), default=str))
    except KeyError as e:
        raise HTTPException(400, f"Column not found in both files: {e}")


def main(host="127.0.0.1", port=8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
