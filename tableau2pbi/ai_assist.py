"""Optional AI assistance for calculations the deterministic rules cannot convert.

Design rules (build guide sections 5, 11 and 15):

* Opt-in: only used when the user asks for a suggestion AND Anthropic credentials are
  configured (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN), and T2P_AI_DISABLED is not set.
* Minimal, redacted context: the formula and the model's table/column/measure names only.
  No data rows, no connection strings, servers, credentials or e-mail addresses.
* Never trusted: the suggestion is parsed with the DAX parser, every referenced object is
  checked against the model, and it is always returned as REVIEW. It only enters the model
  when a reviewer applies it as an override, after which validation must be re-run.
"""
from __future__ import annotations

import json
import os
import re

MODEL = os.environ.get("T2P_AI_MODEL", "claude-opus-5-5")

SYSTEM = """You are a senior Power BI engineer converting Tableau calculations to DAX.
You receive one Tableau formula and the target Power BI semantic model (table, column and measure names).
Return a DAX expression that reproduces the Tableau result, using only objects that exist in the model.
Rules:
- Aggregate Tableau formulas become measures; row-level formulas become calculated columns on the given host table.
- Use DIVIDE for division, DISTINCTCOUNTNOBLANK for COUNTD, RELATED for columns on the one-side of a relationship.
- Tableau NULL propagates through + and - and comparisons; DAX BLANK does not. Guard nullable operands.
- FIXED LODs ignore dimension filters; table calculations depend on the view's addressing.
- If the formula cannot be reproduced faithfully, say so in the explanation and lower the confidence.
Reference tables as 'Table'[Column] and measures as [Measure]."""

SCHEMA = {
    "type": "object",
    "properties": {
        "dax": {"type": "string", "description": "DAX expression (no 'Name =' prefix)"},
        "object_type": {"type": "string", "enum": ["measure", "calculated column"]},
        "explanation": {"type": "string", "description": "How the DAX reproduces Tableau semantics, incl. caveats"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "test_scenarios": {"type": "array", "items": {"type": "string"},
                           "description": "Filter scenarios that should be validated numerically"},
    },
    "required": ["dax", "object_type", "explanation", "confidence", "test_scenarios"],
    "additionalProperties": False,
}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
HOSTLIKE = re.compile(r"\b[\w-]+(\.[\w-]+){2,}\b")


class AIUnavailable(Exception):
    pass


def available() -> bool:
    if os.environ.get("T2P_AI_DISABLED"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def redact(text: str) -> str:
    text = EMAIL.sub("<email>", text or "")
    return HOSTLIKE.sub("<host>", text)


def model_context(res) -> str:
    lines = []
    for t in res.model.tables:
        if t.kind in ("measures",):
            continue
        cols = ", ".join(f"{c.name} ({c.datatype})" for c in t.columns if not c.hidden)
        lines.append(f"Table '{t.name}' [{t.kind}]: {cols}")
    lines.append("Measures: " + ", ".join(m.name for m in res.model.all_measures()))
    rels = "; ".join(f"'{r.from_table}'[{r.from_column}] -> '{r.to_table}'[{r.to_column}] (many-to-one)"
                     for r in res.model.relationships)
    lines.append("Relationships: " + rels)
    return redact("\n".join(lines))


def suggest(res, mapping, client=None) -> dict:
    """Ask Claude for a DAX candidate for one calculation mapping. Raises AIUnavailable."""
    if client is None:
        if not available():
            raise AIUnavailable("AI assistance is opt-in: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) on the server.")
        import anthropic
        client = anthropic.Anthropic()
    prompt = (f"Target model:\n{model_context(res)}\n\n"
              f"Tableau calculation '{redact(mapping.tableau_name)}' ({mapping.kind}, host table '{mapping.pbi_table}'):\n"
              f"{redact(mapping.tableau_formula)}\n\n"
              f"Current deterministic translation (status {mapping.status}):\n{mapping.dax or '(none)'}\n"
              f"Notes: {'; '.join(mapping.warnings) or 'none'}")
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:      # network / auth / rate limit: surfaced to the reviewer, never hidden
        raise AIUnavailable(f"AI request failed: {type(e).__name__}: {e}") from e
    if response.stop_reason == "refusal":
        raise AIUnavailable("The model declined this request.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AIUnavailable(f"Unexpected AI response: {text[:200]}") from e
    check = check_dax(res, data.get("dax", ""))
    return {**data, **check, "status": "REVIEW", "model": MODEL,
            "note": "AI suggestion: review it, apply it as an override, then re-run validation."}


def check_dax(res, dax: str) -> dict:
    """Parse the DAX and verify every referenced table column / measure exists in the model."""
    from .validation.dax_engine import DaxError, parse_dax
    problems = []
    try:
        parse_dax(dax)
        parse_ok = True
    except DaxError as e:
        parse_ok = False
        problems.append(f"DAX syntax: {e}")
    cols = {(t.name, c.name) for t in res.model.tables for c in t.columns}
    measures = {m.name for m in res.model.all_measures()}
    for t, c in re.findall(r"'([^']+)'\[([^\]]+)\]", dax):
        if (t, c) not in cols:
            problems.append(f"unknown column '{t}'[{c}]")
    bare = re.sub(r"'[^']+'\[[^\]]+\]", "", dax)
    bare = re.sub(r'"[^"]*"', "", bare)
    var_names = set(re.findall(r"\bVAR\s+(\w+)", dax, re.I))
    for m in re.findall(r"\[([^\]]+)\]", bare):
        if m not in measures and m not in var_names and not m.startswith("__"):
            problems.append(f"unknown measure [{m}]")
    return {"parse_ok": parse_ok, "references_ok": not any("unknown" in p for p in problems), "problems": problems}
