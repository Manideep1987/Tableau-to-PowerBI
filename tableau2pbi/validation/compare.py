"""Cell-by-cell comparison of a Tableau result and a Power BI result."""
from __future__ import annotations

import math
import re
from datetime import date, datetime

import pandas as pd


def norm_key(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "(null)"
    if v is pd.NaT:
        return "(null)"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (pd.Timestamp, datetime)):
        v = pd.Timestamp(v)
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(round(v, 9))
    if hasattr(v, "item"):
        return norm_key(v.item())
    s = str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower()
    return s


def to_number(v):
    """Parse numbers from exports: '$1,234.50', '(12.0)', '12.5%', '1.2K'."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    s = str(v).strip()
    if s in ("", "-", "null", "NULL", "(null)", "None", "nan"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace("€", "").replace("£", "").replace(",", "").replace(" ", "")
    mult = 1.0
    if s.endswith("%"):
        s, mult = s[:-1], 0.01
    elif s[-1:].upper() in ("K", "M", "B") and re.fullmatch(r"-?[\d.]+[KMBkmb]", s):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1]
    try:
        x = float(s) * mult
    except ValueError:
        return s     # non-numeric value (e.g. ATTR text)
    return -x if neg else x


def compare_frames(tab: pd.DataFrame, pbi: pd.DataFrame, keys: list, measures: list,
                   abs_tol: float = 0.01, rel_tol: float = 1e-6, pbi_measures: list | None = None) -> dict:
    pbi_measures = pbi_measures or measures
    t_rows, p_rows = {}, {}
    for r in tab.to_dict("records"):
        t_rows[tuple(norm_key(r.get(k)) for k in keys)] = [to_number(r.get(m)) for m in measures]
    pkeys = [k for k in keys]
    for r in pbi.to_dict("records"):
        p_rows[tuple(norm_key(r.get(k)) for k in pkeys)] = [to_number(r.get(m)) for m in pbi_measures]
    all_keys = list(dict.fromkeys(list(t_rows) + list(p_rows)))
    rows, mism, matched_cells, total_cells, null_zero = [], 0, 0, 0, 0
    missing_p = missing_t = 0
    totals = {m: [0.0, 0.0] for m in measures}
    for k in all_keys:
        tv, pv = t_rows.get(k), p_rows.get(k)
        row = {"key": list(k)}
        if tv is None:
            row["status"] = "MISSING_IN_TABLEAU"
            row["values"] = [{"measure": m, "tableau": None, "powerbi": pv[i]} for i, m in enumerate(measures)]
            missing_t += 1
            if all(v is None or v == 0 for v in pv):
                row["status"] = "EXTRA_BLANK_ROW"
            rows.append(row)
            total_cells += len(measures)
            continue
        if pv is None:
            row["status"] = "MISSING_IN_POWERBI"
            row["values"] = [{"measure": m, "tableau": tv[i], "powerbi": None} for i, m in enumerate(measures)]
            missing_p += 1
            rows.append(row)
            total_cells += len(measures)
            continue
        vals, ok_row = [], True
        for i, m in enumerate(measures):
            a, b = tv[i], pv[i]
            total_cells += 1
            st = _cell_status(a, b, abs_tol, rel_tol)
            if isinstance(a, float):
                totals[m][0] += a
            if isinstance(b, float):
                totals[m][1] += b
            if st == "MATCH":
                matched_cells += 1
            elif st == "NULL_VS_ZERO":
                null_zero += 1
                ok_row = False
            else:
                ok_row = False
            diff = (b - a) if isinstance(a, float) and isinstance(b, float) else None
            vals.append({"measure": m, "tableau": a, "powerbi": b, "diff": diff, "status": st})
        row["values"] = vals
        row["status"] = "MATCH" if ok_row else "MISMATCH"
        if not ok_row:
            mism += 1
        rows.append(row)
    hard = mism - _rows_only_null_zero(rows) + missing_p + (missing_t - sum(1 for r in rows if r["status"] == "EXTRA_BLANK_ROW"))
    if hard == 0 and null_zero == 0 and missing_t == 0:
        status = "PASS"
    elif hard == 0:
        status = "WARN"
    else:
        status = "FAIL"
    summary = {"tableau_rows": len(t_rows), "powerbi_rows": len(p_rows), "matched_rows": sum(1 for r in rows if r["status"] == "MATCH"),
               "mismatched_rows": mism, "missing_in_powerbi": missing_p, "missing_in_tableau": missing_t,
               "matched_cells": matched_cells, "total_cells": total_cells, "null_vs_zero": null_zero,
               "match_rate": round(100.0 * matched_cells / total_cells, 2) if total_cells else 100.0,
               "totals": {m: {"tableau": round(v[0], 6), "powerbi": round(v[1], 6),
                              "diff": round(v[1] - v[0], 6)} for m, v in totals.items()}}
    # keep the payload small: mismatches first, then a sample of matches
    bad = [r for r in rows if r["status"] != "MATCH"]
    good = [r for r in rows if r["status"] == "MATCH"]
    return {"status": status, "summary": summary, "keys": keys, "measures": measures,
            "rows": bad[:200] + good[:max(0, 60 - len(bad))]}


def _rows_only_null_zero(rows):
    n = 0
    for r in rows:
        if r["status"] == "MISMATCH" and all(v["status"] in ("MATCH", "NULL_VS_ZERO") for v in r["values"]):
            n += 1
    return n


def _cell_status(a, b, abs_tol, rel_tol):
    if a is None and b is None:
        return "MATCH"
    if (a is None and b == 0) or (b is None and a == 0):
        return "NULL_VS_ZERO"
    if a is None or b is None:
        return "NULL_MISMATCH"
    if isinstance(a, str) or isinstance(b, str):
        return "MATCH" if str(a).strip().lower() == str(b).strip().lower() else "VALUE_MISMATCH"
    if abs(a - b) <= abs_tol or abs(a - b) <= rel_tol * max(abs(a), abs(b)):
        return "MATCH"
    return "VALUE_MISMATCH"


def compare_exports(tab_df: pd.DataFrame, pbi_df: pd.DataFrame, keys: list | None = None,
                    measure_pairs: list | None = None, abs_tol=0.01, rel_tol=1e-6) -> dict:
    """Compare two exported tables (e.g. Tableau crosstab CSV vs Power BI 'Export data').

    Columns are matched by name (case-insensitive); keys default to the non-numeric
    columns present in both files and measures to the numeric ones."""
    tcols = {c.lower().strip(): c for c in tab_df.columns}
    pcols = {c.lower().strip(): c for c in pbi_df.columns}
    common = [c for c in tcols if c in pcols]
    if keys is None:
        keys = [c for c in common if not _numeric_col(tab_df[tcols[c]])]
    else:
        keys = [k.lower().strip() for k in keys]
    if measure_pairs is None:
        measure_pairs = [(c, c) for c in common if c not in keys]
    t = pd.DataFrame({k: tab_df[tcols[k]] for k in keys})
    p = pd.DataFrame({k: pbi_df[pcols[k]] for k in keys})
    ms = []
    for a, b in measure_pairs:
        a, b = a.lower().strip(), b.lower().strip()
        t[a] = tab_df[tcols[a]]
        p[a] = pbi_df[pcols[b]]
        ms.append(a)
    out = compare_frames(t, p, keys, ms, abs_tol, rel_tol)
    out["matched_columns"] = {"keys": keys, "measures": ms,
                              "unmatched_tableau": [tcols[c] for c in tcols if c not in pcols],
                              "unmatched_powerbi": [pcols[c] for c in pcols if c not in tcols]}
    return out


def _numeric_col(s: pd.Series) -> bool:
    vals = [to_number(v) for v in s.head(50).tolist()]
    vals = [v for v in vals if v is not None]
    return bool(vals) and all(isinstance(v, float) for v in vals)
