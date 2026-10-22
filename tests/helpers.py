"""Test helpers: tiny fake agents and an engine factory for synthetic graphs."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from orchestrator.agents.base import Agent, TaskContext
from orchestrator.approvals import ScriptedApprover
from orchestrator.engine import Engine, Fault
from orchestrator.models import AgentResult, AutonomyProfile, EngineConfig, TaskSpec
from orchestrator.store import Store
from orchestrator.toolbox import Workspace

WRITER = AutonomyProfile(name="writer", write_globs=("shortener/*.py", "tests/*.py", "*.txt"), max_files=3, max_changed_lines=100)


class FnAgent(Agent):
    def __init__(self, name: str, fn: Callable[[TaskContext], Optional[AgentResult]], profile: AutonomyProfile = WRITER):
        self.name, self._fn, self.profile = name, fn, profile

    def run(self, ctx: TaskContext) -> AgentResult:
        return self._fn(ctx) or AgentResult()


def make_engine(tmp_path: Path, agents: List[Agent], tasks: List[TaskSpec], config: Optional[EngineConfig] = None,
                faults: Optional[Dict[str, List[Fault]]] = None, approver=None) -> Engine:
    store = Store(":memory:")
    store.create_run("r1", "test", "req")
    eng = Engine(store, "r1", Workspace(tmp_path / "ws"), tmp_path / "run", {a.name: a for a in agents},
                 approver or ScriptedApprover(), config=config or EngineConfig(retry_backoff_s=0.0), faults=faults)
    for t in tasks:
        eng.add_task(t)
    return eng


def events(eng: Engine, kind: str) -> List[dict]:
    return [e for e in eng.store.events("r1") if e["kind"] == kind]


class Gauge:
    """Records the maximum number of concurrently running tasks."""

    def __init__(self):
        self.lock, self.now, self.max = threading.Lock(), 0, 0

    def __call__(self, ctx: TaskContext) -> None:
        with self.lock:
            self.now += 1
            self.max = max(self.max, self.now)
        time.sleep(0.15)
        with self.lock:
            self.now -= 1
