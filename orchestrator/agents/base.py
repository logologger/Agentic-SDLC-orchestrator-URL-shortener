"""Agent contract + the per-task context agents receive."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..approvals import Approver
from ..models import AgentResult, AutonomyProfile, TaskSpec
from ..store import Store
from ..toolbox import Toolbox


class TaskContext:
    """Everything an agent may use. Artifact reads are recorded so the engine can build decision lineage
    and detect staleness (a consumed artifact changed => this task's output is invalid)."""

    def __init__(self, run_id: str, task: TaskSpec, toolbox: Toolbox, store: Store, approver: Approver,
                 attempt: int, primary: bool, llm: Any = None, logger: Any = None):
        self.run_id, self.task, self.toolbox, self.store, self.approver = run_id, task, toolbox, store, approver
        self.attempt, self.primary, self.llm = attempt, primary, llm
        self.inputs: Dict[str, int] = {}
        self._logger = logger

    @property
    def params(self) -> Dict[str, Any]:
        return self.task.params

    def read(self, name: str, required: bool = True) -> Any:
        got = self.store.get_artifact(self.run_id, name)
        if got is None:
            if required:
                raise KeyError(f"artifact {name!r} has not been produced yet")
            return None
        self.inputs[name] = got[1]
        return got[0]

    def read_prefix(self, prefix: str) -> Dict[str, Any]:
        return {n: self.read(n) for n in self.store.artifact_names(self.run_id, prefix)}

    def artifact_meta(self, name: str) -> Optional[Dict[str, Any]]:
        got = self.store.get_artifact(self.run_id, name)
        return {"version": got[1], "hash": got[2]} if got else None

    def log(self, kind: str, **payload: Any) -> None:
        if self._logger:                      # engine-provided: also feeds live progress listeners
            self._logger(kind, self.task.id, **payload)
        else:
            self.store.log(self.run_id, kind, self.task.id, **payload)


class Agent:
    name = "agent"
    profile = AutonomyProfile(name="read-only")

    def run(self, ctx: TaskContext) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError
