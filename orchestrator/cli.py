"""Command line: run scenarios, inspect traces, lineage, metrics and audit integrity."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import graph, metrics, pipeline, reports
from .approvals import InteractiveApprover
from .engine import Fault
from .llm import FakeLLM
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scenarios.definitions import CHAOS, CORE, SCENARIOS  # noqa: E402

_SHOW = {"TASK_STARTED", "TASK_SUCCEEDED", "TASK_FAILED", "REPLAN", "POLICY_BLOCKED", "FALLBACK_ENGAGED", "GRAPH_EXPANDED", "SAFE_STOP",
         "ROLLBACK", "APPROVAL_DECIDED", "ATTEMPT_FAILED", "GATE_FAILED", "CLARIFICATION_REQUESTED"}


def _printer(quiet: bool):
    t0: List[float] = []

    def on_event(e):
        if quiet or e["kind"] not in _SHOW:
            return
        t0.append(e["ts"]) if not t0 else None
        p = e["payload"]
        detail = {"TASK_SUCCEEDED": lambda: p.get("notes", ""), "TASK_FAILED": lambda: p.get("error", ""),
                  "REPLAN": lambda: p["reason"], "POLICY_BLOCKED": lambda: "; ".join(p["violations"]),
                  "FALLBACK_ENGAGED": lambda: f"-> {p['agent']}", "GRAPH_EXPANDED": lambda: f"+{len(p['added'])} tasks",
                  "SAFE_STOP": lambda: p["reason"], "ROLLBACK": lambda: f"{p['files_restored']} file(s) restored",
                  "APPROVAL_DECIDED": lambda: f"{p['approval']} {'APPROVED' if p['approved'] else 'DENIED'} by {p['approver']} ({p['mode']})",
                  "ATTEMPT_FAILED": lambda: p["error"], "GATE_FAILED": lambda: p["reason"],
                  "TASK_STARTED": lambda: p["agent"], "CLARIFICATION_REQUESTED": lambda: f"blocking={p['blocking']}"}[e["kind"]]()
        print(f"  +{e['ts'] - t0[0]:5.2f}s {e['kind']:<24} {e['task_id']:<34} {str(detail)[:110]}", flush=True)
    return on_event


def _parse_faults(items: List[str]) -> Dict[str, List[Fault]]:
    out: Dict[str, List[Fault]] = {}
    for item in items or []:
        task, _, spec = item.partition("=")
        kind, _, times = spec.partition(":")
        out.setdefault(task, []).append(Fault(kind, int(times or 1)))
    return out


def run_scenario(name: str, runs_dir: Path, store: Store, args, seeds: Dict[str, Path]) -> pipeline.PipelineRun:
    sc = SCENARIOS[name]
    print(f"\n{'=' * 100}\n{sc.title}\n{'=' * 100}\n> {sc.requirement}\n")
    faults = {**sc.faults, **_parse_faults(args.inject)}
    approver = InteractiveApprover() if args.interactive else sc.approver()
    llm = FakeLLM('{"ambiguities": []}') if args.llm == "fake" else None
    if args.llm == "anthropic":
        from .llm import AnthropicLLM
        llm = AnthropicLLM()
    seed = seeds.get("greenfield") if sc.seed == "greenfield" else None
    if sc.seed == "greenfield" and seed is None:
        print("(no greenfield workspace yet - seeding from the catalog baseline)")
        from .catalog.features import base_files
        seed = runs_dir / "_baseline"
        for p, c in base_files().items():
            (seed / p).parent.mkdir(parents=True, exist_ok=True)
            (seed / p).write_text(c)
    r = pipeline.run(sc.requirement, name, runs_dir, approver, seed=seed, faults=faults, llm=llm, store=store, on_event=_printer(args.quiet))
    m = metrics.compute(store, r.run_id)
    ok, bad = store.verify_chain(r.run_id)
    print(f"\n-> {r.result.status.value}{' (' + r.result.halt_reason + ')' if r.result.halt_reason else ''}  run_id={r.run_id}")
    print(f"   success {m['tasks_succeeded']}/{m['tasks_total']} tasks | retries {m['retries']} | fallbacks {m['fallbacks']} | replans {m['replans']} | "
          f"rollbacks {m['rollbacks']} | latency {m['latency_total_s']}s | audit chain {'OK' if ok else f'BROKEN at {bad}'}")
    if r.result.status.value == "COMPLETED":
        print(f"   summary:   {r.run_dir / 'ENGINEERING_SUMMARY.md'}\n   workspace: {r.workspace}")
    if r.result.status.value != sc.expect:
        print(f"   !! expected {sc.expect}")
    return r


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestrator", description="Agentic SDLC orchestrator for the URL shortener")
    ap.add_argument("--runs-dir", default=str(ROOT / "runs"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a scenario: " + ", ".join(SCENARIOS) + ", core (the 3 required), chaos (all drills), all")
    r.add_argument("scenario")
    r.add_argument("--interactive", action="store_true", help="you are the human approver")
    r.add_argument("--inject", action="append", metavar="TASK=KIND[:N]", help="fault injection: transient[:n] | fail_always | poison")
    r.add_argument("--llm", choices=["none", "fake", "anthropic"], default="none")
    r.add_argument("--quiet", action="store_true")
    sub.add_parser("trace", help="event timeline of a run").add_argument("run_id")
    sub.add_parser("verify", help="verify the audit hash-chain of a run").add_argument("run_id")
    sub.add_parser("runs", help="list recorded runs with metrics")
    lin = sub.add_parser("lineage", help="decision lineage of an artifact (e.g. design, spec, test_report)")
    lin.add_argument("run_id")
    lin.add_argument("artifact")
    return _dispatch(ap.parse_args(argv))


def _dispatch(args) -> int:
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = Store(str(runs_dir / "orchestrator.db"))
    if args.cmd == "run":
        names = {"core": CORE, "chaos": CHAOS, "all": CORE + CHAOS}.get(args.scenario, [args.scenario])
        unknown = [n for n in names if n not in SCENARIOS]
        if unknown:
            print(f"unknown scenario {unknown}; choose from {list(SCENARIOS)} or core/chaos/all")
            return 2
        seeds: Dict[str, Path] = {}
        failed = 0
        for n in names:
            run = run_scenario(n, runs_dir, store, args, seeds)
            if n == "greenfield" and run.result.status.value == "COMPLETED":
                seeds["greenfield"] = run.workspace
            failed += run.result.status.value != SCENARIOS[n].expect
        print(f"\nAggregate over all recorded runs: {metrics.compute_all(store)}")
        return 1 if failed else 0
    if args.cmd == "trace":
        print(reports.trace(store, args.run_id))
    elif args.cmd == "verify":
        ok, bad = store.verify_chain(args.run_id)
        print("audit chain OK" if ok else f"audit chain BROKEN at event #{bad}")
        return 0 if ok else 1
    elif args.cmd == "lineage":
        print(reports.lineage(store, args.run_id, args.artifact))
    elif args.cmd == "runs":
        for rid in store.run_ids():
            m = metrics.compute(store, rid)
            print(f"{rid:<28} {m['status']:<10} tasks {m['tasks_succeeded']}/{m['tasks_total']}  retries {m['retries']}  replans {m['replans']}  {m['latency_total_s']}s")
    return 0
