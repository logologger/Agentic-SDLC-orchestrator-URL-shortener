"""Governance agents: approval gates, barrier, release readiness."""
from __future__ import annotations

import time

from ..approvals import ApprovalRequest
from ..models import AgentResult, ApprovalDenied, AutonomyProfile
from ..policy import PolicyEngine
from .base import Agent, TaskContext


class BarrierAgent(Agent):
    """Synchronisation point for parallel branches (the planner rewires its dependencies at run time)."""
    name = "barrier"
    profile = AutonomyProfile(name="barrier")

    def run(self, ctx: TaskContext) -> AgentResult:
        return AgentResult(notes="all parallel work units joined")


class ApprovalGateAgent(Agent):
    """Change-control checkpoint. Low-risk designs may be auto-approved *by policy* (and that is logged);
    high-impact designs and every release need a person. Approval is bound to the artifact's content hash."""
    name = "approval_gate"
    profile = AutonomyProfile(name="approver")

    def run(self, ctx: TaskContext) -> AgentResult:
        kind, subject = ctx.params["kind"], ctx.params["subject"]
        content, meta = ctx.read(subject), ctx.artifact_meta(subject)
        if kind == "design":
            high = bool(content["high_impact"])
            reasons = content["high_impact_reasons"]
            summary = f"Design for {', '.join(content['features'])}: {len(content['files'])} file(s)"
            details = reasons + content["api_changes"]
        else:
            high = True
            reasons = [] if content["ready"] else ["readiness checks failing"]
            summary = f"Release readiness: {'READY' if content['ready'] else 'NOT READY'} ({content['recommended_version']})"
            details = [f"{'ok ' if c['ok'] else 'FAIL'} {c['item']}: {c['evidence']}" for c in content["checks"]]
        risk = "high" if high else "low"
        mode, approver, reason, wait = "human", "", "", 0.0
        if not PolicyEngine.requires_human(kind, high):
            mode, approver, reason = "policy-auto", "policy:low-risk-auto-approval", "no schema/API/PII impact; policy permits auto-approval"
            ctx.log("APPROVAL_DECIDED", approval=kind, approved=True, mode=mode, approver=approver, reason=reason, subject=subject, version=meta["version"])
        else:
            req = ApprovalRequest(ctx.run_id, ctx.task.id, kind, subject, meta["version"], meta["hash"], summary, risk, details)
            ctx.log("APPROVAL_REQUESTED", approval=kind, subject=subject, version=meta["version"], hash=meta["hash"][:16], risk=risk, details=details[:8])
            t0 = time.time()
            decision = ctx.approver.decide(req)
            wait = time.time() - t0
            approver, reason = decision.approver, decision.reason
            ctx.log("APPROVAL_DECIDED", approval=kind, approved=decision.approved, mode=mode, approver=approver, reason=reason,
                    subject=subject, version=meta["version"], hash=meta["hash"][:16], wait_s=round(wait, 3))
            if not decision.approved:
                raise ApprovalDenied(f"{kind} approval denied by {approver}: {reason}")
        return AgentResult({f"approval.{kind}": {"kind": kind, "approved": True, "approver": approver, "reason": reason, "mode": mode,
                                                 "subject": subject, "subject_version": meta["version"], "subject_hash": meta["hash"]}})


class ReleaseCheckAgent(Agent):
    name = "release_check"
    profile = AutonomyProfile(name="release")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec, design = ctx.read("spec"), ctx.read("design")
        tests, scan, docs, review = ctx.read("test_report"), ctx.read("scan_report"), ctx.read("docs_report"), ctx.read("review")
        approval = ctx.read("approval.design", required=False)
        chain_ok, _ = ctx.store.verify_chain(ctx.run_id)
        checks = [
            {"item": "automated tests green", "ok": tests["failed"] == 0 and tests["errors"] == 0 and tests["passed"] > 0,
             "evidence": f"{tests['passed']} passed across {len(tests['test_files'])} test files"},
            {"item": "policy scan clean", "ok": not [v for v in scan["violations"] if v["severity"] == "high"],
             "evidence": f"{scan['files_scanned']} files, {len(scan['violations'])} violation(s)"},
            {"item": "documentation regenerated", "ok": bool(docs["endpoints"]), "evidence": f"{len(docs['endpoints'])} endpoints in OpenAPI + README"},
            {"item": "independent review has no blockers", "ok": not review["blockers"],
             "evidence": f"{review['totals']['files']} files, +{review['totals']['added']}/-{review['totals']['removed']}"},
            {"item": "design approval recorded", "ok": bool(approval and approval["approved"]),
             "evidence": f"{approval['mode']} by {approval['approver']}" if approval else "missing"},
            {"item": "audit log hash-chain intact", "ok": chain_ok, "evidence": "sha256 chain verified up to this point"},
        ]
        rollback = ["Redeploy the previous build; no data restore required."]
        if design["schema_change"]:
            rollback.append("Schema change is additive (new nullable column / new table): the previous build ignores it, so rollback loses no data.")
        if "unique_visitors" in design["features"]:
            rollback.append("If privacy review objects: unset the feature (revert) and DROP TABLE visitors; no raw personal data exists to purge.")
        rollout = ["Deploy to staging and run the smoke tests (create -> redirect -> stats).",
                   "Start the new build; additive migration runs automatically at startup (idempotent).",
                   "Watch error rate, p95 redirect latency and /healthz for 30 minutes before full traffic."]
        residual = [r for r in design["risks"] if r["likelihood"] == "high" or r["impact"] == "high"]
        ready = all(c["ok"] for c in checks)
        version = "1.0.0" if "core" in design["features"] else ("minor" if design["api_changes"] else "patch")
        return AgentResult({"release_readiness": {"ready": ready, "checks": checks, "rollout_plan": rollout, "rollback_plan": rollback,
                                                  "residual_risks": residual, "recommended_version": version}},
                           notes="READY" if ready else "NOT READY")
