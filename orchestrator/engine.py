"""Stateful DAG execution engine.

Coordinator thread (this class) owns all state transitions; worker threads only execute one task's
attempt loop. That keeps the graph consistent while independent branches run in parallel.

Guarantees
  * a task starts only when all dependencies SUCCEEDED and its entry gates pass
  * results are accepted only when policy scans and exit gates pass
  * transient errors are retried (bounded, with backoff); deterministic failures go to the fallback
    agent; if that fails too the task is rolled back and the run safe-stops
  * when an upstream artifact changes after consumers used it, the consumers (and their dependants)
    are rolled back and re-planned, up to a bounded number of times
  * safe-stop (task failure, budget, wall clock, kill switch, replan limit) aborts pending work and,
    by default, restores the workspace to its pre-run state
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import graph
from .agents.base import Agent, TaskContext
from .approvals import Approver
from .gates import GATES
from .models import (AgentError, AgentResult, ApprovalDenied, EngineConfig, GateFailure, OrchestratorError,
                     PolicyViolation, RunStatus, SafeStop, TaskSpec, TaskState, TransientError)
from .policy import PolicyEngine
from .store import Store
from .toolbox import Toolbox, Workspace


@dataclass
class Fault:
    """Chaos hook for demos/tests. Applies to the *primary* agent variant only."""
    kind: str            # transient | fail_always | poison
    times: int = 1       # for transient: how many attempts fail


@dataclass
class Outcome:
    ok: bool
    result: Optional[AgentResult] = None
    error: str = ""
    inputs: Dict[str, int] = field(default_factory=dict)
    toolbox: Optional[Toolbox] = None
    used_fallback: bool = False
    fatal: bool = False          # no point trying anything else (denied approval, budget, halt)
    aborted: bool = False


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    states: Dict[str, str]
    halt_reason: str
    run_dir: Path


class Engine:
    def __init__(self, store: Store, run_id: str, workspace: Workspace, run_dir: Path, agents: Dict[str, Agent],
                 approver: Approver, policy: Optional[PolicyEngine] = None, config: Optional[EngineConfig] = None,
                 faults: Optional[Dict[str, List[Fault]]] = None, llm: Any = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.store, self.run_id, self.ws, self.run_dir = store, run_id, workspace, Path(run_dir)
        self.agents, self.approver, self.llm = agents, approver, llm
        self.policy = policy or PolicyEngine()
        self.config = config or EngineConfig()
        self.faults = faults or {}
        self._fault_counts: Dict[str, int] = {}
        self.on_event = on_event
        self.tasks: Dict[str, TaskSpec] = {}
        self.state: Dict[str, TaskState] = {}
        self.attempts: Dict[str, int] = {}
        self.inputs: Dict[str, Dict[str, int]] = {}
        self.toolboxes: Dict[str, Toolbox] = {}
        self.completion_order: List[str] = []
        self.replans: Dict[str, int] = {}
        self.halt_reason = ""
        self._halt = threading.Event()
        self._total_attempts = 0
        self._attempt_lock = threading.Lock()
        self._started = 0.0

    # ---- graph ----------------------------------------------------------------------------------
    def add_task(self, spec: TaskSpec) -> None:
        if spec.id in self.tasks:
            raise graph.GraphError(f"duplicate task id {spec.id!r}")
        self.tasks[spec.id] = spec
        self._set_state(spec.id, TaskState.PENDING)

    def _set_state(self, tid: str, state: TaskState) -> None:
        self.state[tid] = state
        self.store.set_task(self.run_id, tid, state.value, self.attempts.get(tid, 0))

    def _emit(self, kind: str, tid: str = "", **payload: Any) -> None:
        ev = self.store.log(self.run_id, kind, tid, **payload)
        if self.on_event:
            self.on_event(ev)

    # ---- main loop ------------------------------------------------------------------------------
    def run(self) -> RunResult:
        graph.validate(self.tasks)
        self._started = time.time()
        self._emit("RUN_STARTED", tasks=sorted(self.tasks), max_workers=self.config.max_workers)
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            running: Dict[Future, str] = {}
            while True:
                reason = self._stop_reason()
                if reason and not self._halt.is_set():
                    self._begin_halt(reason)
                if not self._halt.is_set():
                    for tid in self._ready():
                        self._set_state(tid, TaskState.RUNNING)
                        running[pool.submit(self._execute, self.tasks[tid])] = tid
                if not running:
                    break
                done, _ = wait(list(running), timeout=0.2, return_when=FIRST_COMPLETED)
                for fut in done:
                    self._complete(running.pop(fut), fut.result())
        return self._finish()

    def _ready(self) -> List[str]:
        return [t.id for t in self.tasks.values()
                if self.state[t.id] == TaskState.PENDING and all(self.state[d] == TaskState.SUCCEEDED for d in t.deps)]

    def _stop_reason(self) -> str:
        if self.config.kill_switch_file and os.path.exists(self.config.kill_switch_file):
            return "kill switch engaged"
        if time.time() - self._started > self.config.wall_clock_limit_s:
            return "wall-clock budget exceeded"
        return ""

    def _begin_halt(self, reason: str) -> None:
        if not self._halt.is_set():
            self.halt_reason = reason
            self._halt.set()
            self._emit("SAFE_STOP", reason=reason)

    # ---- worker: one task's attempt loop ---------------------------------------------------------
    def _execute(self, spec: TaskSpec) -> Outcome:
        tb: Optional[Toolbox] = None
        inputs: Dict[str, int] = {}
        self._emit("TASK_STARTED", spec.id, agent=spec.agent, phase=spec.phase, human=spec.human, deps=spec.deps)
        variants = [spec.agent] + ([spec.fallback] if spec.fallback else [])
        last_error = "unknown"
        for vi, agent_name in enumerate(variants):
            agent = self.agents[agent_name]
            tb = Toolbox(self.ws, spec.id, agent.profile, self.policy,
                         lambda t: self.state.get(t) in (TaskState.SUCCEEDED, TaskState.FAILED))
            self.toolboxes[spec.id] = tb
            if vi:
                self._emit("FALLBACK_ENGAGED", spec.id, agent=agent_name, after=last_error[:200])
            for attempt in range(1, spec.max_retries + 2):
                if self._halt.is_set():
                    return Outcome(False, error="run halted", aborted=True, fatal=True, toolbox=tb)
                try:
                    self._tick_budget()
                    self.attempts[spec.id] = self.attempts.get(spec.id, 0) + 1
                    self._emit("ATTEMPT_STARTED", spec.id, agent=agent_name, attempt=attempt)
                    ctx = TaskContext(self.run_id, spec, tb, self.store, self.approver, attempt, vi == 0, self.llm, self._emit)
                    self._run_gates(spec.gate_in, ctx, None, "entry")
                    self._maybe_fault(spec, ctx)
                    result = agent.run(ctx)
                    inputs.update(ctx.inputs)
                    self._run_gates(spec.gate_out, ctx, result, "exit")
                    inputs.update(ctx.inputs)
                    return Outcome(True, result, inputs=dict(inputs), toolbox=tb, used_fallback=bool(vi))
                except TransientError as exc:
                    last_error = f"transient: {exc}"
                    self._emit("ATTEMPT_FAILED", spec.id, agent=agent_name, attempt=attempt, retryable=True,
                               will_retry=attempt <= spec.max_retries, error=str(exc)[:300])
                    tb.rollback()
                    time.sleep(self.config.retry_backoff_s * (2 ** (attempt - 1)))
                    continue
                except PolicyViolation as exc:
                    last_error = f"policy: {exc}"
                    self._emit("POLICY_BLOCKED", spec.id, agent=agent_name, violations=[str(v) for v in exc.violations])
                    tb.rollback()
                    break
                except (ApprovalDenied, SafeStop) as exc:
                    tb.rollback()
                    self._emit("ATTEMPT_FAILED", spec.id, agent=agent_name, attempt=attempt, retryable=False, error=str(exc)[:300])
                    return Outcome(False, error=str(exc), fatal=True, toolbox=tb)
                except (AgentError, GateFailure, KeyError, OrchestratorError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    self._emit("ATTEMPT_FAILED", spec.id, agent=agent_name, attempt=attempt, retryable=False, error=str(exc)[:300])
                    tb.rollback()
                    break
                except Exception as exc:  # unexpected bug in an agent: contain it, never crash the engine
                    last_error = f"unexpected {type(exc).__name__}: {exc}"
                    self._emit("ATTEMPT_FAILED", spec.id, agent=agent_name, attempt=attempt, retryable=False, error=last_error[:300])
                    tb.rollback()
                    break
        return Outcome(False, error=last_error, toolbox=tb)

    def _tick_budget(self) -> None:
        with self._attempt_lock:
            self._total_attempts += 1
            if self._total_attempts > self.config.max_total_attempts:
                raise SafeStop("attempt budget exhausted")

    def _run_gates(self, names: List[str], ctx: TaskContext, result: Optional[AgentResult], when: str) -> None:
        for name in names:
            msg = GATES[name](ctx, result)
            self._emit("GATE_FAILED" if msg else "GATE_PASSED", ctx.task.id, gate=name, when=when, reason=msg or "")
            if msg:
                raise GateFailure(f"{when} gate {name}: {msg}")

    def _maybe_fault(self, spec: TaskSpec, ctx: TaskContext) -> None:
        if not ctx.primary:
            return
        for f in self.faults.get(spec.id, []):
            key = f"{spec.id}:{f.kind}"
            if f.kind == "transient" and self._fault_counts.get(key, 0) < f.times:
                self._fault_counts[key] = self._fault_counts.get(key, 0) + 1
                raise TransientError("injected transient fault")
            if f.kind == "fail_always":
                raise AgentError("injected permanent fault")
            if f.kind == "poison":
                ctx.task.params["__poison"] = True

    # ---- coordinator: accept / reject outcomes ----------------------------------------------------
    def _complete(self, tid: str, out: Outcome) -> None:
        spec = self.tasks[tid]
        if not out.ok:
            if out.aborted:
                self._set_state(tid, TaskState.ABORTED)
                return
            self._emit("TASK_FAILED", tid, error=out.error[:400], attempts=self.attempts.get(tid, 0))
            self._set_state(tid, TaskState.FAILED)
            self._begin_halt(f"task {tid} failed: {out.error[:200]}")
            return
        stale = self._stale_reason(spec, out)
        if stale:
            self._reset(tid, f"discarded result: {stale}", rollback_only_self=True)
            return
        res = out.result or AgentResult()
        try:
            self._expand(spec, res)
        except OrchestratorError as exc:
            out.toolbox.rollback() if out.toolbox else None
            self._emit("TASK_FAILED", tid, error=f"graph expansion rejected: {exc}")
            self._set_state(tid, TaskState.FAILED)
            self._begin_halt(f"task {tid} produced an invalid plan: {exc}")
            return
        self.inputs[tid] = out.inputs
        for d in res.decisions:
            self._emit("DECISION", tid, **d)
        changed: List[str] = []
        for name, content in res.artifacts.items():
            version, is_new, h = self.store.put_artifact(self.run_id, name, content, tid)
            self._emit("ARTIFACT_PUBLISHED", tid, artifact=name, version=version, hash=h[:16], new_version=is_new,
                       inputs=dict(out.inputs))
            if is_new and version > 1:
                changed.append(name)
        self.completion_order.append(tid)
        self._set_state(tid, TaskState.SUCCEEDED)
        self._emit("TASK_SUCCEEDED", tid, attempts=self.attempts.get(tid, 0), used_fallback=out.used_fallback,
                   files=out.toolbox.changed_paths if out.toolbox else [], notes=res.notes[:300])
        for name in changed:
            self._invalidate_consumers(name, source=tid)

    def _stale_reason(self, spec: TaskSpec, out: Outcome) -> str:
        for d in spec.deps:
            if self.state[d] != TaskState.SUCCEEDED:
                return f"dependency {d} was reset while this task ran"
        for name, version in out.inputs.items():
            latest = self.store.latest_version(self.run_id, name)
            if latest != version:
                return f"input {name}@v{version} superseded by v{latest}"
        return ""

    def _expand(self, spec: TaskSpec, res: AgentResult) -> None:
        if not res.expand and not res.add_deps:
            return
        trial = dict(self.tasks)
        for t in res.expand:
            if t.id in trial:
                raise graph.GraphError(f"duplicate task id {t.id!r}")
            trial[t.id] = t
        for tid, extra in res.add_deps.items():
            old = trial[tid]
            trial[tid] = TaskSpec(**{**old.__dict__, "deps": sorted(set(old.deps) | set(extra))})
        graph.validate(trial)  # raises before anything is committed
        self.tasks.update(trial)
        for t in res.expand:
            self._set_state(t.id, TaskState.PENDING)
        self._emit("GRAPH_EXPANDED", spec.id, added=[t.id for t in res.expand], rewired=sorted(res.add_deps))

    # ---- re-planning -----------------------------------------------------------------------------------
    def _invalidate_consumers(self, name: str, source: str) -> None:
        stale = [t for t, ins in self.inputs.items()
                 if t != source and name in ins and ins[name] < self.store.latest_version(self.run_id, name)
                 and self.state[t] == TaskState.SUCCEEDED]
        for t in stale:
            if self.state[t] == TaskState.SUCCEEDED:
                self._reset(t, f"upstream artifact {name} changed (task {source})")

    def _reset(self, tid: str, reason: str, rollback_only_self: bool = False) -> None:
        affected = {tid} if rollback_only_self else ({tid} | graph.descendants(self.tasks, tid))
        # in-flight descendants are left alone: their completion-time staleness check discards them
        affected = {t for t in affected if self.state[t] == TaskState.SUCCEEDED or t == tid}
        for t in sorted(affected, key=lambda x: self.completion_order.index(x) if x in self.completion_order else -1, reverse=True):
            self.replans[t] = self.replans.get(t, 0) + 1
            if self.replans[t] > self.config.max_replans_per_task:
                self._begin_halt(f"replan budget exceeded for {t}")
                return
            tb = self.toolboxes.get(t)
            n = tb.rollback() if tb else 0
            if t in self.completion_order:
                self.completion_order.remove(t)
            self.inputs.pop(t, None)
            self._emit("REPLAN", t, reason=reason if t == tid else f"{reason}; dependency {tid} reset", files_restored=n)
            self._set_state(t, TaskState.PENDING)

    # ---- safe-stop / rollback ----------------------------------------------------------------------------
    def _finish(self) -> RunResult:
        if self._halt.is_set():
            for tid, st in list(self.state.items()):
                if st == TaskState.PENDING or st == TaskState.RUNNING:
                    self._set_state(tid, TaskState.ABORTED)
            if self.config.rollback_on_halt:
                for tid in reversed(list(self.completion_order)):
                    tb = self.toolboxes.get(tid)
                    n = tb.rollback() if tb else 0
                    self._set_state(tid, TaskState.ROLLED_BACK)
                    self._emit("ROLLBACK", tid, files_restored=n, reason="safe-stop restores pre-run state")
            status = RunStatus.HALTED
        else:
            status = RunStatus.COMPLETED
        self.store.finish_run(self.run_id, status.value)
        self._emit("RUN_FINISHED", status=status.value, halt_reason=self.halt_reason,
                   duration_s=round(time.time() - self._started, 3))
        return RunResult(self.run_id, status, {t: s.value for t, s in self.state.items()}, self.halt_reason, self.run_dir)
