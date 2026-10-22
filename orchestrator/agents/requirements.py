"""Requirement understanding: normalise free text into a spec, surface ambiguity, resolve with humans."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..approvals import Question
from ..llm import AMBIGUITY_SYSTEM, parse_ambiguities
from ..models import AgentError, AgentResult, AutonomyProfile, PolicyViolation, Violation
from .base import Agent, TaskContext

_FEATURE_PATTERNS = {
    "expiry": re.compile(r"\b(expir\w*|ttl|temporary|temporarily|time[- ]limited|self[- ]destruct\w*|short[- ]lived)\b", re.I),
    "unique_visitors": re.compile(r"\b(unique (visitors?|clickers?)|distinct visitors?|who (is |are )?(click\w*|visit\w*|open\w*)|"
                                  r"track(ing)? (the )?(users?|visitors?|people|clickers?)|visitor identity)\b", re.I),
}
_CORE = re.compile(r"\b(url|link)[- ]?shorten\w*|\bshort(en)? (links?|urls?)\b", re.I)
_DURATION = re.compile(r"\b\d+\s*(second|sec|minute|min|hour|day|week|month|year)s?\b|\bttl[_ ]seconds\b", re.I)
_VAGUE_NFR = re.compile(r"\b(fast|quick(ly)?|scalable|high[- ]traffic|robust|reliable|secure|performant)\b", re.I)
_NUMBERS_NEAR = re.compile(r"\b(p9\d|ms|req(uest)?s?/s|per (second|minute)|99\.\d+%)", re.I)

_CORE_REQUIREMENTS = [
    ("F-1", "Create a short link for a valid http(s) URL; optionally with a custom alias",
     ["201 with a 7-character code and short_url", "non-http(s), private-network and over-long URLs return 422",
      "alias already in use returns 409", "reserved or malformed alias returns 422"]),
    ("F-2", "Redirect a short code to its long URL", ["known code returns 302 with Location header", "unknown code returns 404"]),
    ("F-3", "Per-link click analytics", ["each redirect increments total_clicks", "stats expose total_clicks and last_clicked_at",
                                         "unknown link stats return 404"]),
    ("F-4", "Rate-limit link creation to protect the service", ["more than 10 creations/minute/client returns 429 with Retry-After"]),
    ("F-5", "Operational health endpoint", ["GET /healthz returns 200 when the database answers, 503 otherwise"]),
]
_FEATURE_REQUIREMENTS = {
    "expiry": ("F-EXP", "Links may carry an optional TTL; expired links stop redirecting",
               ["ttl_seconds in [60, 31536000] yields expires_at = created_at + ttl", "redirect after expiry returns 410 and records no click",
                "ttl outside the range returns 422", "databases created before this release keep working (idempotent migration)"]),
    "unique_visitors": ("F-UV", "Report distinct visitors per link without storing personal data",
                        ["stats include unique_visitors", "repeat visits by the same client on the same day count once",
                         "no raw IP address or user-agent is persisted"]),
}


def _detect_ambiguities(text: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if _FEATURE_PATTERNS["expiry"].search(text) and not _DURATION.search(text):
        found.append({"question": "How long may a temporary link live (allowed range), and who chooses the lifetime?",
                      "category": "functional", "blocking": False,
                      "default": "Optional per-link ttl_seconds (60 s to 365 d); links without a ttl never expire"})
    if _FEATURE_PATTERNS["unique_visitors"].search(text):
        found.append({"question": "'Know who is clicking' - do you need to identify individuals (personal data) or only count distinct "
                                  "visitors? What retention period and legal basis apply?",
                      "category": "compliance", "blocking": True, "default": None})
    m = _VAGUE_NFR.search(text)
    if m and not _NUMBERS_NEAR.search(text):
        found.append({"question": f"'{m.group(0)}' is not measurable. What latency / throughput / availability targets apply?",
                      "category": "nfr", "blocking": False,
                      "default": "p95 redirect < 100 ms at 100 req/s on one node; 99.9% availability; OWASP ASVS L1 controls"})
    return found


class RequirementsAgent(Agent):
    name = "requirements"
    profile = AutonomyProfile(name="analyst")

    def run(self, ctx: TaskContext) -> AgentResult:
        text: str = ctx.params["requirement"]
        brownfield = ctx.toolbox.exists("shortener/api.py")
        features = [n for n, pat in _FEATURE_PATTERNS.items() if pat.search(text)]
        if not brownfield:
            features.insert(0, "core")
        elif _CORE.search(text) and not features:
            raise AgentError("requirement describes the existing core service; nothing new to build")
        if not features:
            raise AgentError("could not map the requirement to any known capability; needs a human to refine it")
        functional = [{"id": i, "text": t, "acceptance": a} for i, t, a in (_CORE_REQUIREMENTS if "core" in features else [])]
        functional += [{"id": i, "text": t, "acceptance": a} for i, t, a in (_FEATURE_REQUIREMENTS[f] for f in features if f in _FEATURE_REQUIREMENTS)]
        ambiguities = [{"id": f"AMB-{n}", "resolution": None, **a} for n, a in enumerate(self.ambiguities(ctx, text), 1)]
        spec = {
            "requirement": text, "mode": "brownfield" if brownfield else "greenfield", "features": features,
            "summary": ("Extend the existing URL shortener with: " if brownfield else "Build a URL shortener with: ") + ", ".join(features),
            "functional": functional,
            "non_functional": ["Input validation on all public endpoints", "No secrets or personal data in source or logs",
                               "Additive, reversible schema changes only", "Every change ships with automated tests"],
            "ambiguities": ambiguities,
            "assumptions": ["Single-node deployment with SQLite is acceptable for the prototype",
                            "302 (not 301) redirects so every click is observable and links stay revocable"],
            "out_of_scope": ["Authentication / per-user link ownership", "Multi-region replication", "Link deletion UI"],
            "privacy": {"mode": "none"}, "status": "draft",
        }
        return AgentResult({"spec": spec}, notes=f"{len(features)} feature(s), {len(ambiguities)} ambiguity(ies)")

    def ambiguities(self, ctx: TaskContext, text: str) -> List[Dict[str, Any]]:
        return _detect_ambiguities(text)


class LLMRequirementsAgent(RequirementsAgent):
    """Rule-based analysis plus LLM-suggested extra ambiguities. Any LLM problem => AgentError => fallback agent."""
    name = "requirements_llm"

    def ambiguities(self, ctx: TaskContext, text: str) -> List[Dict[str, Any]]:
        if ctx.llm is None:
            raise AgentError("no LLM configured")
        base = _detect_ambiguities(text)
        try:
            extra = parse_ambiguities(ctx.llm.complete(AMBIGUITY_SYSTEM, text))
        except ValueError as exc:
            raise AgentError(str(exc))
        known = {a["question"] for a in base}
        return base + [a for a in extra if a["question"] not in known]


class ClarifierAgent(Agent):
    """Human-in-the-loop: asks open questions, records answers/assumptions, re-issues the spec as a new version."""
    name = "clarifier"
    profile = AutonomyProfile(name="analyst")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec = ctx.read("spec")
        open_q = [a for a in spec["ambiguities"] if not a.get("resolution")]
        if not open_q:
            return AgentResult(notes="no ambiguity; spec unchanged")
        questions = [Question(a["id"], a["question"], a["category"], a["default"], a["blocking"]) for a in open_q]
        ctx.log("CLARIFICATION_REQUESTED", questions=[q.id for q in questions], blocking=[q.id for q in questions if q.blocking])
        answers = ctx.approver.clarify(questions)
        who = getattr(ctx.approver, "name", "human")
        decisions, assumptions = [], list(spec["assumptions"])
        for a in spec["ambiguities"]:
            if a.get("resolution"):
                continue
            if a["id"] in answers:
                a["resolution"] = {"answer": answers[a["id"]], "source": "human", "by": who}
                decisions.append({"decision": f"{a['id']} resolved by human", "answer": answers[a["id"]], "by": who})
                self._apply(spec, a, answers[a["id"]])
            elif not a["blocking"] and a["default"]:
                a["resolution"] = {"answer": a["default"], "source": "assumed-default", "by": "agent"}
                assumptions.append(f"{a['id']}: {a['default']} (assumed; no answer given)")
                decisions.append({"decision": f"{a['id']} defaulted", "answer": a["default"], "by": "agent"})
        spec["assumptions"], spec["status"] = assumptions, "clarified"
        return AgentResult({"spec": spec}, decisions=decisions,
                           notes=f"{sum(1 for a in spec['ambiguities'] if a['resolution'])}/{len(spec['ambiguities'])} resolved")

    @staticmethod
    def _apply(spec: Dict[str, Any], amb: Dict[str, Any], answer: str) -> None:
        if amb["category"] != "compliance":
            return
        stores_raw = re.search(r"\b(raw|plain|full|store|log|keep|save)\b.{0,30}\b(ip|address|user.?agent|e-?mail)\b", answer, re.I)
        protects = re.search(r"\b(hash\w*|salted|no raw|not (store|keep|log)|without storing|anonymi[sz]\w*|aggregate\w*)\b", answer, re.I)
        if stores_raw and not protects:
            raise PolicyViolation([Violation("CMP-002", "compliance", "high",
                                             "answer requires storing raw personal data; needs DPO sign-off outside this pipeline", amb["id"])])
        spec["privacy"] = {"mode": "hashed", "basis": answer[:200]}
