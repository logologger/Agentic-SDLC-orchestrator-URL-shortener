"""Reliability metrics derived purely from the audit log (so they can be re-computed and audited)."""
from __future__ import annotations

import statistics
import time
from typing import Any, Dict, List, Optional

from .store import Store


def compute(store: Store, run_id: str, in_flight_ok: bool = False) -> Dict[str, Any]:
    """in_flight_ok: count RUNNING tasks as succeeded (used by the summary written by the last task)."""
    ev = store.events(run_id)
    kinds: Dict[str, List[Dict[str, Any]]] = {}
    for e in ev:
        kinds.setdefault(e["kind"], []).append(e)
    states = {t: ("SUCCEEDED" if in_flight_ok and s == "RUNNING" else s) for t, (s, _a) in store.tasks(run_id).items()}
    start = kinds["RUN_STARTED"][0]["ts"] if "RUN_STARTED" in kinds else (ev[0]["ts"] if ev else time.time())
    end = kinds["RUN_FINISHED"][-1]["ts"] if "RUN_FINISHED" in kinds else (ev[-1]["ts"] if ev else start)

    # per-task timeline
    first_fail: Dict[str, float] = {}
    for e in kinds.get("ATTEMPT_FAILED", []) + kinds.get("POLICY_BLOCKED", []):
        first_fail.setdefault(e["task_id"], e["ts"])
    started: Dict[str, Dict[str, Any]] = {}
    for e in kinds.get("TASK_STARTED", []):
        started[e["task_id"]] = e
    succeeded_ev = {e["task_id"]: e for e in kinds.get("TASK_SUCCEEDED", [])}
    recoveries = [succeeded_ev[t]["ts"] - ts for t, ts in first_fail.items() if t in succeeded_ev and succeeded_ev[t]["ts"] >= ts]
    human_s = sum(succeeded_ev[t]["ts"] - s["ts"] for t, s in started.items() if s["payload"].get("human") and t in succeeded_ev)
    first_try = [t for t, e in succeeded_ev.items() if e["payload"]["attempts"] == 1 and not e["payload"].get("used_fallback")]

    if in_flight_ok:
        first_try += [t for t, st in store.tasks(run_id).items() if st[0] == "RUNNING"]
    total = len(states)
    ok = sum(1 for s in states.values() if s == "SUCCEEDED")
    retries = sum(1 for e in kinds.get("ATTEMPT_FAILED", []) if e["payload"].get("will_retry"))
    duration = end - start
    return {
        "run_id": run_id,
        "tasks_total": total, "tasks_succeeded": ok,
        "success_rate": round(ok / total, 3) if total else 0.0,
        "executed_ok_rate": round(sum(1 for x in states.values() if x in ("SUCCEEDED", "ROLLED_BACK")) / total, 3) if total else 0.0,
        "first_pass_rate": round(len(first_try) / total, 3) if total else 0.0,
        "attempts": len(kinds.get("ATTEMPT_STARTED", [])),
        "retries": retries,
        "retry_rate": round(retries / max(1, len(kinds.get("ATTEMPT_STARTED", []))), 3),
        "fallbacks": len(kinds.get("FALLBACK_ENGAGED", [])),
        "policy_blocks": len(kinds.get("POLICY_BLOCKED", [])),
        "gate_failures": len(kinds.get("GATE_FAILED", [])),
        "replans": len(kinds.get("REPLAN", [])),
        "rollbacks": len(kinds.get("ROLLBACK", [])) + len(kinds.get("REPLAN", [])),
        "safe_stops": len(kinds.get("SAFE_STOP", [])),
        "approvals_requested": len(kinds.get("APPROVAL_REQUESTED", [])),
        "approvals_denied": sum(1 for e in kinds.get("APPROVAL_DECIDED", []) if not e["payload"]["approved"]),
        "mttr_s": round(statistics.mean(recoveries), 4) if recoveries else None,
        "recovered_tasks": len(recoveries),
        "latency_total_s": round(duration, 3),
        "latency_human_wait_s": round(human_s, 3),
        "latency_automation_s": round(max(0.0, duration - human_s), 3),
        "status": store.run_row(run_id).get("status", "?"),
    }


def compute_all(store: Store) -> Dict[str, Any]:
    runs = [compute(store, r) for r in store.run_ids()]
    if not runs:
        return {"runs": 0}
    mttrs = [r["mttr_s"] for r in runs if r["mttr_s"] is not None]
    n = sum(r["tasks_total"] for r in runs)
    return {
        "runs": len(runs),
        "run_success_rate": round(sum(1 for r in runs if r["status"] == "COMPLETED") / len(runs), 3),
        "task_success_rate": round(sum(r["tasks_succeeded"] for r in runs) / n, 3) if n else 0.0,
        "retries": sum(r["retries"] for r in runs), "fallbacks": sum(r["fallbacks"] for r in runs),
        "rollbacks": sum(r["rollbacks"] for r in runs), "replans": sum(r["replans"] for r in runs),
        "mttr_s": round(statistics.mean(mttrs), 4) if mttrs else None,
        "latency_avg_s": round(statistics.mean(r["latency_total_s"] for r in runs), 3),
        "latency_max_s": max(r["latency_total_s"] for r in runs),
    }


def render(m: Dict[str, Any]) -> str:
    pct = lambda x: f"{x * 100:.0f}%"  # noqa: E731
    mttr: Optional[float] = m["mttr_s"]
    return "\n".join([
        f"| Task success rate | {pct(m['success_rate'])} ({m['tasks_succeeded']}/{m['tasks_total']}) |",
        f"| Completed-then-rolled-back tasks | {pct(m['executed_ok_rate'] - m['success_rate'])} of tasks (work done, then undone by safe-stop) |",
        f"| First-pass success rate | {pct(m['first_pass_rate'])} |",
        f"| Attempts / retries / fallbacks | {m['attempts']} / {m['retries']} / {m['fallbacks']} |",
        f"| Policy blocks / gate failures | {m['policy_blocks']} / {m['gate_failures']} |",
        f"| Replans / rollbacks / safe-stops | {m['replans']} / {m['rollbacks']} / {m['safe_stops']} |",
        f"| Approvals requested / denied | {m['approvals_requested']} / {m['approvals_denied']} |",
        f"| MTTR | {f'{mttr:.3f}s over {m['recovered_tasks']} recovered task(s)' if mttr is not None else 'n/a (no failures recovered)'} |",
        f"| End-to-end latency | {m['latency_total_s']}s (human wait {m['latency_human_wait_s']}s, automation {m['latency_automation_s']}s) |",
    ])
