"""The three required scenarios plus chaos drills. Each is just data: a requirement and a scripted human."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from orchestrator.approvals import ApprovalDecision, ApprovalRequest, Approver, ScriptedApprover
from orchestrator.engine import Fault

GREENFIELD_REQ = (
    "Build a URL shortener service. Users create short links for long URLs, optionally with a custom alias, and are redirected "
    "with HTTP 302. Each link has click analytics (total clicks and last click time). Link creation is rate limited to 10 requests "
    "per minute per client. Expose a health endpoint. Reject non-http(s) and private-network targets."
)
BROWNFIELD_REQ = (
    "Add optional expiry to short links: callers can pass ttl_seconds (60 seconds to 1 year); after expiry the redirect returns "
    "410 Gone and clicks are no longer counted. Existing links must keep working and existing databases must be migrated without downtime."
)
AMBIGUOUS_REQ = "Make links temporary and let us know who is clicking them, and keep it fast."

PRIVACY_ANSWER = ("Count distinct visitors per link only - no identification of individuals. Use salted hashes and never "
                  "store raw IP addresses or user-agents; aggregate counts only.")


def reviewer(name: str = "eng-lead (scripted)") -> ScriptedApprover:
    return ScriptedApprover(
        decisions={
            "design": ApprovalDecision(True, name, "Additive migration and API change reviewed; rollback plan acceptable"),
            "release": ApprovalDecision(True, name, "Checklist green, rollout/rollback plan acceptable"),
        },
        answers={"AMB-2": PRIVACY_ANSWER}, name=name)


@dataclass
class Scenario:
    name: str
    title: str
    requirement: str
    seed: str                      # "none" | "greenfield" (seed workspace from the greenfield run)
    approver: Callable[[], Approver] = reviewer
    faults: Dict[str, List[Fault]] = field(default_factory=dict)
    expect: str = "COMPLETED"
    note: str = ""


def denying_reviewer() -> ScriptedApprover:
    a = reviewer("security-reviewer (scripted)")
    a.decisions["design"] = ApprovalDecision(False, a.name, "Privacy review has not signed off on processing visitor data")
    return a


def raw_pii_reviewer() -> ScriptedApprover:
    a = reviewer()
    a.answers = {"AMB-2": "Store the full IP address and user-agent of every visitor so we can see exactly who clicked."}
    return a


SCENARIOS: Dict[str, Scenario] = {s.name: s for s in [
    Scenario("greenfield", "Greenfield: build the URL shortener from scratch", GREENFIELD_REQ, "none"),
    Scenario("brownfield", "Brownfield: add link expiry to the running service (schema + API change)", BROWNFIELD_REQ, "greenfield"),
    Scenario("ambiguous", "Ambiguous: vague, privacy-sensitive request that needs clarification and re-planning", AMBIGUOUS_REQ, "greenfield"),
    # ---- chaos drills (same pipeline, injected faults) ----
    Scenario("chaos-retry", "Chaos: transient tool failure is retried", BROWNFIELD_REQ, "greenfield",
             faults={"impl:shortener/service.py": [Fault("transient", 2)], "test.unit": [Fault("transient", 1)]}),
    Scenario("chaos-policy", "Chaos: agent emits a hard-coded secret -> policy block -> fallback agent", BROWNFIELD_REQ, "greenfield",
             faults={"impl:shortener/api.py": [Fault("poison")]}),
    Scenario("chaos-halt", "Chaos: permanent failure after code was written -> rollback + safe-stop", BROWNFIELD_REQ, "greenfield",
             faults={"test.unit": [Fault("fail_always")]}, expect="HALTED"),
    Scenario("chaos-denied", "Chaos: human denies the high-impact design -> safe-stop", AMBIGUOUS_REQ, "greenfield",
             approver=denying_reviewer, expect="HALTED"),
    Scenario("chaos-pii", "Chaos: human answers 'store raw IPs' -> compliance policy blocks it", AMBIGUOUS_REQ, "greenfield",
             approver=raw_pii_reviewer, expect="HALTED"),
]}
CORE = ["greenfield", "brownfield", "ambiguous"]
CHAOS = [n for n in SCENARIOS if n.startswith("chaos-")]
