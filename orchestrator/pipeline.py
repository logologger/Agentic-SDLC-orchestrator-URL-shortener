"""The SDLC pipeline definition and the run() entry point used by the CLI, scenarios and tests."""
from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import metrics, reports
from .agents.base import Agent, TaskContext
from .agents.delivery import DocsAgent, ImplementerAgent, ReviewerAgent, SafeImplementer, ScannerAgent, TesterAgent
from .agents.design import ArchitectAgent, ExplorerAgent, ImpactAgent, PlannerAgent
from .agents.governance import ApprovalGateAgent, BarrierAgent, ReleaseCheckAgent
from .agents.requirements import ClarifierAgent, LLMRequirementsAgent, RequirementsAgent
from .approvals import Approver
from .engine import Engine, Fault, RunResult
from .models import AgentResult, AutonomyProfile, EngineConfig, TaskSpec
from .policy import PolicyEngine
from .store import Store
from .toolbox import Workspace


class FinalizerAgent(Agent):
    """Platform-owned: writes the engineering summary into the run directory (outside the agent sandbox)."""
    name = "finalizer"
    profile = AutonomyProfile(name="finalizer")

    def __init__(self, engine_ref: Callable[[], Engine]):
        self._engine = engine_ref

    def run(self, ctx: TaskContext) -> AgentResult:
        eng = self._engine()
        text = reports.engineering_summary(ctx.store, ctx.run_id, eng.run_dir, eng.tasks, eng.ws.root)
        path = eng.run_dir / "ENGINEERING_SUMMARY.md"
        path.write_text(text)
        return AgentResult({"final_summary": {"path": str(path), "bytes": len(text)}}, notes=str(path))


def build_tasks(requirement: str, brownfield: bool, use_llm: bool) -> List[TaskSpec]:
    """The explicit dependency graph (entry/exit gates, human checkpoints, parallel branches, sync points)."""
    T = TaskSpec
    tasks = [
        T("req.analyze", "requirements_llm" if use_llm else "requirements", params={"requirement": requirement}, fallback="requirements" if use_llm else None,
          gate_out=["acceptance_criteria"], phase="requirements", description="normalise requirement, detect ambiguity"),
        T("req.clarify", "clarifier", deps=["req.analyze"], gate_out=["acceptance_criteria", "spec_resolved"], human=True, phase="requirements",
          description="ask humans / record assumptions; re-issue spec"),
        T("design.explore", "explorer", deps=["req.analyze"], phase="design", description="speculative footprint estimate (may be re-planned)"),
    ]
    arch_deps = ["req.clarify", "design.explore"]
    if brownfield:
        tasks.append(T("impact.analyze", "impact", deps=["req.clarify"], phase="design", description="AST import graph, routes, tables"))
        arch_deps.append("impact.analyze")
    tasks += [
        T("design.architect", "architect", deps=arch_deps, gate_in=["spec_resolved"], gate_out=["design_complete"], phase="design",
          description="decisions, API/schema changes, risks"),
        T("approve.design", "approval_gate", deps=["design.architect"], params={"kind": "design", "subject": "design"}, human=True,
          phase="governance", description="change-control checkpoint (human if high impact)"),
        T("plan.decompose", "planner", deps=["approve.design"], gate_in=["design_approved"], gate_out=["plan_valid"], phase="plan",
          description="work breakdown; expands graph with impl units"),
        T("impl.sync", "barrier", deps=["plan.decompose"], phase="implement", description="join of parallel implementation units"),
        T("test.unit", "tester", deps=["impl.sync"], gate_out=["unit_tests_green"], phase="validate", description="run the generated test-suite"),
        T("scan.security", "scanner", deps=["impl.sync"], gate_out=["scan_clean"], phase="validate", description="policy + security scan"),
        T("docs.update", "docs", deps=["impl.sync"], side_effects=True, phase="document", description="README, OpenAPI, CHANGELOG, design log"),
        T("review.gate", "reviewer", deps=["test.unit", "scan.security", "docs.update"], gate_in=["tests_green_in"], gate_out=["review_passed"],
          phase="review", description="independent review / change-control rules"),
        T("release.readiness", "release_check", deps=["review.gate"], phase="release", description="checklist, rollout and rollback plan"),
        T("approve.release", "approval_gate", deps=["release.readiness"], params={"kind": "release", "subject": "release_readiness"}, human=True,
          phase="governance", description="release approval (always human)"),
        T("release.finalize", "finalizer", deps=["approve.release"], gate_in=["release_approved"], phase="release",
          description="engineering summary"),
    ]
    return tasks


@dataclass
class PipelineRun:
    result: RunResult
    engine: Engine
    store: Store
    workspace: Path
    run_dir: Path

    @property
    def run_id(self) -> str:
        return self.result.run_id


def run(requirement: str, scenario: str, runs_dir: Path, approver: Approver, seed: Optional[Path] = None,
        faults: Optional[Dict[str, List[Fault]]] = None, llm: Any = None, config: Optional[EngineConfig] = None,
        store: Optional[Store] = None, on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        run_id: Optional[str] = None) -> PipelineRun:
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = store or Store(str(runs_dir / "orchestrator.db"))
    run_id = run_id or f"{scenario}-{time.strftime('%H%M%S')}-{secrets.token_hex(2)}"
    run_dir = runs_dir / run_id
    ws_dir = run_dir / "workspace"
    if seed:
        shutil.copytree(seed, ws_dir, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.db"))
    ws = Workspace(ws_dir)
    brownfield = (ws.root / "shortener" / "api.py").is_file()

    holder: Dict[str, Engine] = {}
    agents: Dict[str, Agent] = {a.name: a for a in (
        RequirementsAgent(), LLMRequirementsAgent(), ClarifierAgent(), ExplorerAgent(), ImpactAgent(), ArchitectAgent(), ApprovalGateAgent(),
        PlannerAgent(), BarrierAgent(), ImplementerAgent(), SafeImplementer(), TesterAgent(), ScannerAgent(), DocsAgent(), ReviewerAgent(),
        ReleaseCheckAgent(), FinalizerAgent(lambda: holder["e"]))}
    store.create_run(run_id, scenario, requirement)
    engine = Engine(store, run_id, ws, run_dir, agents, approver, PolicyEngine(), config, faults, llm, on_event)
    holder["e"] = engine
    for t in build_tasks(requirement, brownfield, use_llm=llm is not None):
        engine.add_task(t)
    result = engine.run()
    final = metrics.compute(store, run_id)
    (run_dir / "metrics.json").write_text(json.dumps(final, indent=2))
    return PipelineRun(result, engine, store, ws.root, run_dir)
