"""Human-in-the-loop providers. Approvals are bound to the *content hash* of what was reviewed."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    task_id: str
    kind: str              # design | release
    subject: str           # artifact name
    version: int
    content_hash: str
    summary: str
    risk: str              # low | medium | high
    details: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    approver: str
    reason: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    category: str
    default: Optional[str]
    blocking: bool


class Approver:
    def decide(self, request: ApprovalRequest) -> ApprovalDecision:  # pragma: no cover - interface
        raise NotImplementedError

    def clarify(self, questions: List[Question]) -> Dict[str, str]:  # pragma: no cover - interface
        raise NotImplementedError


class ScriptedApprover(Approver):
    """Deterministic approver for demos/tests: a decision (or callable) per approval kind."""

    def __init__(self, decisions: Optional[Dict[str, Union[ApprovalDecision, Callable[[ApprovalRequest], ApprovalDecision]]]] = None,
                 answers: Optional[Dict[str, str]] = None, name: str = "scripted-reviewer"):
        self.decisions = decisions or {}
        self.answers = answers or {}
        self.name = name
        self.requests: List[ApprovalRequest] = []
        self.asked: List[Question] = []

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        d = self.decisions.get(request.kind)
        if callable(d):
            return d(request)
        return d or ApprovalDecision(True, self.name, "approved by scripted reviewer")

    def clarify(self, questions: List[Question]) -> Dict[str, str]:
        self.asked.extend(questions)
        return {q.id: self.answers[q.id] for q in questions if q.id in self.answers}


class InteractiveApprover(Approver):
    """Prompts on the terminal. A lock keeps prompts from interleaving when tasks run in parallel."""

    _lock = threading.Lock()

    def __init__(self, name: str = "human"):
        self.name = name

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        with self._lock:
            print(f"\n=== APPROVAL REQUIRED: {request.kind} ({request.risk} risk) ===")
            print(request.summary)
            for line in request.details:
                print(f"  - {line}")
            print(f"  subject: {request.subject}@v{request.version} sha256:{request.content_hash[:12]}")
            answer = input("Approve? [y/N] ").strip().lower()
            reason = input("Reason: ").strip() or "(none given)"
        return ApprovalDecision(answer in {"y", "yes"}, self.name, reason)

    def clarify(self, questions: List[Question]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        with self._lock:
            print("\n=== CLARIFICATION NEEDED ===")
            for q in questions:
                hint = f" [default: {q.default}]" if q.default else ""
                ans = input(f"{q.id} ({'BLOCKING' if q.blocking else 'optional'}) {q.text}{hint}\n> ").strip()
                if ans:
                    out[q.id] = ans
        return out
