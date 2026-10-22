"""Named entry/exit gates. A gate returns None when it passes, or a human-readable reason when it blocks."""
from __future__ import annotations

from typing import Callable, Dict, Optional

from .agents.base import TaskContext
from .graph import GraphError, validate
from .models import AgentResult, TaskSpec

Gate = Callable[[TaskContext, Optional[AgentResult]], Optional[str]]


def _produced(ctx: TaskContext, result: Optional[AgentResult], name: str):
    if result is not None and name in result.artifacts:
        return result.artifacts[name]
    return ctx.read(name, required=False)


# ---- entry gates ---------------------------------------------------------------------------------
def spec_resolved(ctx, result):
    spec = _produced(ctx, result, "spec")
    if spec is None:
        return "no requirement spec available"
    open_blocking = [a["id"] for a in spec["ambiguities"] if a["blocking"] and not a.get("resolution")]
    return f"blocking ambiguities unresolved: {open_blocking}" if open_blocking else None


def design_approved(ctx, _):
    ctx.read("design", required=False)
    approval, meta = ctx.read("approval.design", required=False), ctx.artifact_meta("design")
    if not approval or not approval["approved"]:
        return "design has not been approved"
    if meta is None or approval["subject_hash"] != meta["hash"]:
        return "approval refers to a different version of the design (stale approval)"
    return None


def tests_green_in(ctx, _):
    rep = ctx.read("test_report", required=False)
    if not rep or rep["failed"] or rep["errors"] or not rep["passed"]:
        return "test report is missing or not green"
    return None


def release_approved(ctx, _):
    ctx.read("release_readiness", required=False)
    approval, meta = ctx.read("approval.release", required=False), ctx.artifact_meta("release_readiness")
    if not approval or not approval["approved"]:
        return "release has not been approved"
    if meta is None or approval["subject_hash"] != meta["hash"]:
        return "release approval is stale"
    return None


# ---- exit gates ----------------------------------------------------------------------------------
def acceptance_criteria(ctx, result):
    spec = _produced(ctx, result, "spec")
    missing = [r["id"] for r in spec["functional"] if not r.get("acceptance")]
    return f"requirements without acceptance criteria: {missing}" if missing else None


def design_complete(ctx, result):
    d = _produced(ctx, result, "design")
    for key in ("features", "files", "decisions", "risks", "api_changes"):
        if not d.get(key) and key != "api_changes":
            return f"design is missing `{key}`"
    return None


def plan_valid(ctx, result):
    plan, design = _produced(ctx, result, "plan"), ctx.read("design")
    planned = {w["file"] for w in plan["work_items"]}
    if planned != set(design["files"]):
        return f"plan does not cover the design exactly: {sorted(planned ^ set(design['files']))}"
    try:  # dependencies named in the plan must form a DAG
        validate({w["id"]: TaskSpec(w["id"], "x", [d for d in w["depends_on"]]) for w in plan["work_items"]})
    except GraphError as exc:
        return str(exc)
    return None


def unit_tests_green(ctx, result):
    rep = _produced(ctx, result, "test_report")
    if rep["failed"] or rep["errors"] or not rep["passed"]:
        return f"tests not green: {rep['passed']} passed, {rep['failed']} failed, {rep['errors']} errors"
    return None


def scan_clean(ctx, result):
    rep = _produced(ctx, result, "scan_report")
    high = [v for v in rep["violations"] if v["severity"] == "high"]
    return f"{len(high)} high-severity policy violations: {high[0]['rule']} {high[0]['message']}" if high else None


def review_passed(ctx, result):
    rev = _produced(ctx, result, "review")
    return "; ".join(rev["blockers"]) if rev["blockers"] else None


GATES: Dict[str, Gate] = {g.__name__: g for g in (
    spec_resolved, design_approved, tests_green_in, release_approved,
    acceptance_criteria, design_complete, plan_valid, unit_tests_green, scan_clean, review_passed)}
