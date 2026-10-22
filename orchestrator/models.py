"""Core types shared by the engine, agents and tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"   # succeeded earlier, then undone by safe-stop or replan
    ABORTED = "ABORTED"           # never ran because the run was halted


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    HALTED = "HALTED"             # safe-stop: workspace restored, humans notified


class OrchestratorError(Exception):
    """Base class for orchestration failures."""


class TransientError(OrchestratorError):
    """Retryable failure (timeouts, flaky tools)."""


class AgentError(OrchestratorError):
    """Deterministic agent failure: retrying the same agent will not help."""


class GateFailure(OrchestratorError):
    """An entry or exit gate rejected the task."""


class ApprovalDenied(OrchestratorError):
    """A human (or policy) refused a high-impact action."""


class SafeStop(OrchestratorError):
    """A run-level control (budget, kill switch, replan limit) demands a halt."""


@dataclass(frozen=True)
class Violation:
    rule: str
    category: str      # security | compliance | change-control
    severity: str      # high | medium | low
    message: str
    path: str = ""

    def __str__(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"{self.rule}({self.severity}){loc}: {self.message}"


class PolicyViolation(OrchestratorError):
    def __init__(self, violations: List[Violation]):
        self.violations = violations
        super().__init__("; ".join(str(v) for v in violations))


@dataclass
class TaskSpec:
    id: str
    agent: str
    deps: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    gate_in: List[str] = field(default_factory=list)     # must hold before the agent may start
    gate_out: List[str] = field(default_factory=list)    # must hold before results are accepted
    max_retries: int = 2                                 # retries of the *same* agent on transient errors
    fallback: Optional[str] = None                       # alternative agent when the primary exhausts retries/violates policy
    side_effects: bool = False                           # writes to the workspace => snapshot/undo journal matters
    human: bool = False                                  # blocks on a person; excluded from automation latency
    phase: str = ""
    description: str = ""


@dataclass
class AgentResult:
    artifacts: Dict[str, Any] = field(default_factory=dict)
    expand: List[TaskSpec] = field(default_factory=list)         # dynamic graph expansion (planner)
    add_deps: Dict[str, List[str]] = field(default_factory=dict) # existing task -> extra deps (barrier wiring)
    notes: str = ""
    decisions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AutonomyProfile:
    """What an agent is *allowed* to do, enforced by the Toolbox (not by the agent's good manners)."""
    name: str
    write_globs: tuple = ()
    max_files: int = 8
    max_changed_lines: int = 800
    can_run_tests: bool = False
    can_export_schema: bool = False


@dataclass
class EngineConfig:
    max_workers: int = 4
    retry_backoff_s: float = 0.0
    max_total_attempts: int = 80
    max_replans_per_task: int = 3
    wall_clock_limit_s: float = 300.0
    rollback_on_halt: bool = True
    kill_switch_file: Optional[str] = None
