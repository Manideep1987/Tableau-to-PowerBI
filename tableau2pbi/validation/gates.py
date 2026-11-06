"""Quality gates, readiness score and the validation results CSV.

Gates (build guide section 10): 1 data contract -> 2 numeric/KPI -> 3 interactions ->
4 security -> 5 business UAT. A failed gate BLOCKS every later gate: later results are
still shown but cannot be accepted until the earlier contract is fixed.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

GATES = [(1, "Data contract"), (2, "Numeric / KPI parity"), (3, "Interactions"), (4, "Security (RLS)"),
         (5, "Business UAT")]
NATIVE_VISUALS = {"clusteredBarChart", "clusteredColumnChart", "stackedBarChart", "stackedColumnChart", "lineChart",
                  "areaChart", "pieChart", "scatterChart", "tableEx", "pivotTable", "card", "multiRowCard", "map",
                  "filledMap", "treemap", "slicer", "textbox", "lineClusteredColumnComboChart"}


def _gate_status(checks):
    st = [c["status"] for c in checks]
    if not st:
        return "N/A"
    if "FAIL" in st or "ERROR" in st:
        return "FAIL"
    if all(s in ("N/A", "SKIP") for s in st):
        return "N/A"
    if "WARN" in st:
        return "WARN"
    if "PENDING" in st:
        return "PENDING"
    return "PASS"


def compute_gates(rep: dict, signoff: dict | None) -> list:
    out, blocked_by = [], None
    for g, name in GATES:
        checks = [c for c in rep["checks"] if c.get("gate") == g]
        if g == 5 and signoff:
            decision = signoff.get("decision")
            status = {"approved": "PASS", "approved-with-redesign": "WARN", "rejected": "FAIL"}.get(decision, "PENDING")
        else:
            status = _gate_status(checks)
        auto = [c for c in checks if c["status"] not in ("PENDING",)]
        human = [c["name"] for c in checks if c["status"] == "PENDING" and not (g == 5 and signoff)]
        entry = {"gate": g, "name": name, "status": status, "automated_checks": len(auto),
                 "passed": sum(c["status"] == "PASS" for c in auto),
                 "failed": sum(c["status"] in ("FAIL", "ERROR") for c in auto),
                 "warnings": sum(c["status"] == "WARN" for c in auto), "pending_human_checks": human}
        if g == 5 and signoff:
            entry["signoff"] = signoff
        if blocked_by is not None:
            entry["blocked_by"] = blocked_by
            entry["status_if_unblocked"] = status
            entry["status"] = "BLOCKED"
        elif status == "FAIL":
            blocked_by = g
        out.append(entry)
    return out


def verdict(gates: list) -> str:
    st = {g["gate"]: g["status"] for g in gates}
    if any(s in ("FAIL", "BLOCKED") for s in st.values()):
        first = min(g for g, s in st.items() if s in ("FAIL", "BLOCKED"))
        return f"Not ready: gate {first} ({dict(GATES)[first]}) failed"
    if any(s in ("PENDING", "WARN") for g, s in st.items() if g < 5):
        warn = any(s == "WARN" for g, s in st.items() if g < 5)
        return f"Automated gates passed{' with warnings' if warn else ''}; human checks pending"
    if st.get(5) in ("PENDING", None):
        return "Ready for business UAT sign-off"
    return "Signed off" if st.get(5) in ("PASS", "WARN") else "Not ready"


def readiness(res, rep: dict) -> dict:
    """Readiness score card (build guide section 13): measured, not claimed."""
    wb = res.workbook
    checks = rep["checks"]
    verified = set()
    for c in checks:
        if c["status"] != "PASS":
            continue
        d = c.get("data") or {}
        for v in (d.get("measure_map") or {}).values():
            if v:
                verified.add(v)
        for v in (d.get("dimension_map") or {}).values():
            if v:
                verified.add(tuple(v)[1] if isinstance(v, (list, tuple)) else v)
        for r in d.get("register", []):
            if r.get("status") == "PASS":
                verified.add(r["measure"])
    # objects used by a verified measure (directly or through other measures) are verified too
    import re as _re
    exprs = {m.name: m.expression for m in res.model.all_measures()}
    exprs.update({c.name: c.expression for t in res.model.tables for c in t.columns if c.expression})
    frontier = list(verified)
    while frontier:
        obj = frontier.pop()
        for ref in _re.findall(r"'[^']+'\[([^\]]+)\]|(?<!')\[([^\]]+)\]", exprs.get(obj, "") or ""):
            name = ref[0] or ref[1]
            if name and name not in verified:
                verified.add(name)
                frontier.append(name)
    sec_ok = any(c["layer"] == "Security" and c["name"].startswith("Identity") and c["status"] == "PASS" for c in checks)
    calcs = [m for m in res.calc_mappings if m.kind not in ("base-measure", "date-part")]
    ver = [m for m in calcs if m.pbi_object in verified or (m.kind == "rls-role" and sec_ok)]
    numeric = [c for c in checks if c["gate"] == 2 and c["status"] not in ("SKIP", "PENDING", "N/A")]
    ident = [c for c in checks if c["layer"] == "Security" and c["name"].startswith("Identity")]
    visuals = [v for v in res.worksheet_visuals.values()]
    native = [v for v in visuals if v.visual_type in NATIVE_VISUALS]
    review = sum(1 for m in calcs if m.classification == "REVIEW")
    unsupported = sum(1 for m in calcs if m.classification == "UNSUPPORTED")
    manual_actions = sum(1 for a in wb.actions if a.type not in ("filter", "highlight"))
    warn_issues = sum(1 for i in res.issues if i["severity"] == "warning")
    failed_checks = sum(1 for c in checks if c["status"] in ("FAIL", "ERROR"))
    warn_checks = sum(1 for c in checks if c["status"] == "WARN")
    minutes = 45 * unsupported + 10 * review + 5 * warn_issues + 20 * manual_actions + 30 * failed_checks + 15 * warn_checks \
        + 2 * len(wb.unknown_nodes)

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else None
    metrics = [
        {"metric": "Parse completeness", "value": wb.parse_stats.get("completeness"),
         "detail": f"{wb.parse_stats.get('known_elements', 0)} known / {wb.parse_stats.get('unknown_elements', 0)} unknown XML elements (unknown preserved for review)"},
        {"metric": "Calculation pass rate", "value": pct(len(ver), len(calcs)),
         "detail": f"{len(ver)} of {len(calcs)} calculations numerically verified in a passing check "
                   f"({len(calcs) - review - unsupported} PASS, {review} REVIEW, {unsupported} UNSUPPORTED by rule)"},
        {"metric": "Visual openability", "value": None,
         "detail": f"NOT RUN: requires Power BI Desktop on Windows. {len(native)}/{len(visuals)} worksheets map to native visuals."},
        {"metric": "Numeric parity", "value": pct(sum(c["status"] == "PASS" for c in numeric), len(numeric)),
         "detail": f"{sum(c['status'] == 'PASS' for c in numeric)} of {len(numeric)} visual, scenario and KPI checks pass"},
        {"metric": "Security acceptance", "value": pct(sum(c["status"] == "PASS" for c in ident), len(ident)) if ident else None,
         "detail": (f"{sum(c['status'] == 'PASS' for c in ident)} of {len(ident)} simulated identities match; live identity test pending"
                    if ident else "No row-level security in the workbook")},
        {"metric": "Manual effort (estimate)", "value": minutes, "unit": "minutes",
         "detail": "45/unsupported calc, 10/review item, 5/warning, 20/manual action, 30/failed and 15/warned check"},
    ]
    scored = [m["value"] for m in metrics[:5] if isinstance(m["value"], (int, float))]
    return {"metrics": metrics, "index": round(sum(scored) / len(scored), 1) if scored else None,
            "verified_objects": sorted(str(v) for v in verified)}


ROOT_CAUSE = {"NULL_VS_ZERO": "Null handling", "NULL_MISMATCH": "Null handling",
              "MISSING_IN_POWERBI": "Row set difference (filter / join / relationship)",
              "MISSING_IN_TABLEAU": "Row set difference (filter / join / relationship)",
              "EXTRA_BLANK_ROW": "Blank member (unmatched key)", "VALUE_MISMATCH": "Calculation logic / filter context"}
CSV_COLUMNS = ["gate", "check", "scenario", "metric", "power_bi_object", "tableau_value", "powerbi_value", "delta",
               "tolerance", "root_cause_category", "status", "owner", "approval_notes"]


def results_rows(rep: dict) -> list:
    rows = []
    tol = rep.get("tolerance", {}).get("absolute", 0.01)
    for c in rep["checks"]:
        d = c.get("data") or {}
        known = "KNOWN LIMITATION" in c.get("detail", "")
        if "register" in d:
            for r in d["register"]:
                rows.append({"gate": c["gate"], "check": c["name"], "scenario": r["scenario"], "metric": r["metric"],
                             "power_bi_object": r["measure"], "tableau_value": r["tableau"], "powerbi_value": r["powerbi"],
                             "delta": r["delta"], "tolerance": r["tolerance"],
                             "root_cause_category": "" if r["status"] == "PASS" else (r.get("note") or "Calculation logic / filter context"),
                             "status": r["status"], "owner": "BI migration engineer", "approval_notes": ""})
        elif "rows" in d and "summary" in d:
            labels, mmap = d.get("labels", {}), d.get("measure_map", {})
            bad = {}
            for r in d["rows"]:
                for v in r.get("values", []):
                    if v.get("status", "MATCH") != "MATCH" or r["status"] in ("MISSING_IN_POWERBI", "MISSING_IN_TABLEAU"):
                        bad.setdefault(v["measure"], []).append((r, v))
            for m, t in d["summary"].get("totals", {}).items():
                st = "FAIL" if bad.get(m) and c["status"] != "WARN" else ("WARN" if bad.get(m) else "PASS")
                first = bad.get(m, [(None, None)])[0]
                cause = "" if st == "PASS" else ("Known limitation: static calculated column" if known else
                                                 ROOT_CAUSE.get((first[1] or {}).get("status") or (first[0] or {}).get("status"),
                                                                "Calculation logic / filter context"))
                rows.append({"gate": c["gate"], "check": c["name"], "scenario": "Total of all rows",
                             "metric": labels.get(m, m), "power_bi_object": mmap.get(m), "tableau_value": t["tableau"],
                             "powerbi_value": t["powerbi"], "delta": t["diff"], "tolerance": tol,
                             "root_cause_category": cause, "status": st, "owner": "BI migration engineer",
                             "approval_notes": "Accepted known limitation" if known and st != "PASS" else ""})
                for r, v in bad.get(m, [])[:20]:
                    status = "WARN" if known else "FAIL"
                    rows.append({"gate": c["gate"], "check": c["name"], "scenario": " | ".join(r["key"]),
                                 "metric": labels.get(m, m), "power_bi_object": mmap.get(m), "tableau_value": v.get("tableau"),
                                 "powerbi_value": v.get("powerbi"), "delta": v.get("diff"), "tolerance": tol,
                                 "root_cause_category": "Known limitation: static calculated column" if known else
                                 ROOT_CAUSE.get(v.get("status") if v.get("status") != "MATCH" else r["status"], ""),
                                 "status": status, "owner": "BI migration engineer", "approval_notes": ""})
        elif c["layer"] == "Security" and "columns" in d:
            for r in d["columns"]:
                rows.append({"gate": 4, "check": c["name"], "scenario": f"user = {r['user']}", "metric": r["metric"],
                             "power_bi_object": "", "tableau_value": r["tableau"], "powerbi_value": r["powerbi"],
                             "delta": None, "tolerance": 0, "root_cause_category": "" if r["status"] == "PASS" else "Security filter",
                             "status": r["status"], "owner": "Security owner", "approval_notes": "Simulated identity"})
        else:
            rows.append({"gate": c["gate"], "check": c["name"], "scenario": "", "metric": c["layer"], "power_bi_object": "",
                         "tableau_value": "", "powerbi_value": "", "delta": "", "tolerance": "",
                         "root_cause_category": "" if c["status"] in ("PASS", "N/A") else c["detail"][:160],
                         "status": c["status"], "owner": "Business owner" if c["gate"] == 5 else "BI migration engineer",
                         "approval_notes": ""})
    return rows


def results_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in results_rows(rep):
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})
    return buf.getvalue()


def make_signoff(approver: str, decision: str, notes: str = "") -> dict:
    if decision not in ("approved", "approved-with-redesign", "rejected"):
        raise ValueError("decision must be approved, approved-with-redesign or rejected")
    if not approver.strip():
        raise ValueError("approver is required")
    return {"approver": approver.strip(), "decision": decision, "notes": notes.strip(),
            "recorded_at": datetime.now().isoformat(timespec="seconds")}
