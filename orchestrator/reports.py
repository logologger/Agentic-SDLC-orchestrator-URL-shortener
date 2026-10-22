"""Human-readable views over the audit log: trace, lineage, and the final engineering summary."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import graph, metrics
from .models import TaskSpec
from .store import Store

def esc(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


LIMITATIONS = [
    "Agents are deterministic/rule-based; the LLM seam (orchestrator/llm.py) augments requirement analysis only and is not exercised in CI.",
    "The implementer applies a curated catalog of change sets; it does not synthesise arbitrary code. Unknown requirements stop at the requirements gate.",
    "Runs are persisted (state, artifacts, hash-chained audit log) but an interrupted run cannot be resumed; it must be restarted.",
    "Rollback restores the workspace via undo journals; it does not undo external side effects (none are performed by design).",
    "Human denial of an approval safe-stops the run; it does not (yet) route feedback back into a redesign loop.",
    "SQLite + in-process rate limiting + hashed visitors are single-node prototype choices; see design risks for the scale-out path.",
    "Test quality is measured by pass/fail and policy scans, not by mutation testing or coverage thresholds.",
]


def trace(store: Store, run_id: str, limit: int = 0) -> str:
    ev = store.events(run_id)
    if not ev:
        return "(no events)"
    t0 = ev[0]["ts"]
    lines = []
    for e in ev:
        p = e["payload"]
        detail = ", ".join(f"{k}={str(v)[:70]}" for k, v in p.items() if k not in {"inputs", "tasks", "details", "questions"})
        lines.append(f"+{e['ts'] - t0:6.2f}s {e['kind']:<19} {e['task_id']:<34} {detail}")
    return "\n".join(lines[-limit:] if limit else lines)


def lineage(store: Store, run_id: str, artifact: str, version: int = 0) -> str:
    """Decision lineage: which task produced an artifact version and which artifact versions it consumed (recursively)."""
    published = {(e["payload"]["artifact"], e["payload"]["version"]): e for e in store.events(run_id) if e["kind"] == "ARTIFACT_PUBLISHED"
                 and e["payload"]["new_version"]}
    version = version or store.latest_version(run_id, artifact)
    out: List[str] = []

    def walk(name: str, ver: int, depth: int, seen: set) -> None:
        e = published.get((name, ver))
        pad = "  " * depth
        if not e:
            out.append(f"{pad}{name}@v{ver}  (produced outside the audit window)")
            return
        out.append(f"{pad}{name}@v{ver}  <- task {e['task_id']}  sha256:{e['payload']['hash']}")
        for n, v in sorted(e["payload"]["inputs"].items()):
            if (n, v) in seen:
                out.append(f"{pad}  {n}@v{v}  (expanded above)")
            else:
                seen.add((n, v))
                walk(n, v, depth + 1, seen)

    walk(artifact, version, 0, {(artifact, version)})
    return "\n".join(out)


def approvals(store: Store, run_id: str) -> List[Dict[str, Any]]:
    return [{"task": e["task_id"], **e["payload"]} for e in store.events(run_id) if e["kind"] == "APPROVAL_DECIDED"]


def engineering_summary(store: Store, run_id: str, run_dir: Path, tasks: Dict[str, TaskSpec], workspace: Path) -> str:
    art = lambda n: (store.get_artifact(run_id, n) or (None,))[0]  # noqa: E731
    spec, design, plan, impact = art("spec"), art("design"), art("plan"), art("impact")
    tests, scan, review, docs, rel = art("test_report"), art("scan_report"), art("review"), art("docs_report"), art("release_readiness")
    states = {t: ('SUCCEEDED' if s == 'RUNNING' else s) for t, (s, _a) in store.tasks(run_id).items()}
    m = metrics.compute(store, run_id, in_flight_ok=True)
    row = store.run_row(run_id)
    ev = store.events(run_id)
    L: List[str] = [f"# Engineering summary - run `{run_id}`", "",
                    f"**Scenario:** {row['scenario']}  |  **Status:** {'COMPLETED' if row['status'] == 'RUNNING' else row['status']}  |  **Workspace:** `{workspace}`", "",
                    f"> {spec['requirement']}", "", "## 1. Requirement understanding", "",
                    f"Mode: **{spec['mode']}**. Capabilities: {', '.join(spec['features'])}.", "",
                    "| ID | Requirement | Acceptance criteria |", "|---|---|---|"]
    L += [f"| {r['id']} | {r['text']} | {'; '.join(r['acceptance'])} |" for r in spec["functional"]]
    L += ["", "**Ambiguities and how they were closed**", ""]
    L += [f"- `{a['id']}` ({a['category']}, {'blocking' if a['blocking'] else 'non-blocking'}): {a['question']}  \n  -> "
          f"{(a['resolution'] or {}).get('source', 'UNRESOLVED')}: {(a['resolution'] or {}).get('answer', '')}" for a in spec["ambiguities"]] or ["- none"]
    L += ["", "**Assumptions**", ""] + [f"- {a}" for a in spec["assumptions"]]
    L += ["", "**Out of scope**", ""] + [f"- {a}" for a in spec["out_of_scope"]]
    if impact:
        L += ["", "## 2. Codebase impact (brownfield)", "", f"Risk: **{impact['risk']}** - {impact['risk_reason']}", "",
              "| Module | Why |", "|---|---|"] + [f"| `{d['module']}` | {'; '.join(d['why'])} |" for d in impact["direct"]]
        L += [f"| `{t['module']}` (transitive) | imports {', '.join(t['via'])} - regression surface |" for t in impact["transitive"]]
        L += ["", f"Routes touched: {', '.join(r['route'] for r in impact['routes_touched']) or 'none'}  ",
              f"Tables: {json.dumps(impact['tables'])}  ", f"Existing tests re-run: {', '.join(impact['existing_tests_to_rerun'])}"]
    L += ["", "## 3. Task decomposition and orchestration", "", plan["rationale"], "", "| Level | Work units (run in parallel) |", "|---|---|"]
    L += [f"| {n} | {', '.join(f'`{i}`' for i in lvl)} |" for n, lvl in enumerate(plan["parallel_levels"])]
    L += ["", "```mermaid", graph.to_mermaid(tasks, states), "```", "",
          "## 4. Design decisions and trade-offs", "", "| ID | Decision | Rationale | Alternatives rejected |", "|---|---|---|---|"]
    L += [f"| {d['id']} | {esc(d['decision'])} | {esc(d['rationale'])} | {esc('; '.join(d['alternatives']))} |" for d in design["decisions"]]
    L += ["", "**API changes:** " + ("; ".join(design["api_changes"]) or "none (new service)"), "", "## 5. Risks", "",
          "| Risk | Likelihood | Impact | Mitigation |", "|---|---|---|---|"]
    L += [f"| {esc(r['risk'])} | {r['likelihood']} | {r['impact']} | {esc(r['mitigation'])} |" for r in design["risks"]]
    L += ["", "## 6. Artifacts produced", "", "| File | Action | + | - |", "|---|---|---|---|"]
    L += [f"| `{c['path']}` | {c['action']} | {c['added']} | {c['removed']} |" for c in review["manifest"]]
    L += [f"| `{f}` | docs | | |" for f in docs["files"]]
    L += ["", "## 7. Validation", "",
          f"- **Tests:** {tests['passed']} passed, {tests['failed']} failed, {tests['errors']} errors ({len(tests['test_files'])} test files)",
          f"- **Policy scan:** {scan['files_scanned']} files, {len(scan['violations'])} violation(s); rules: {', '.join(scan['rules'])}",
          f"- **Independent review:** {len(review['blockers'])} blocker(s), {len(review['warnings'])} warning(s)"]
    L += [f"  - warning: {w}" for w in review["warnings"]]
    L += ["", "## 8. Governance trail", "", "| Approval | Mode | Approver | Decision | Reason |", "|---|---|---|---|---|"]
    L += [f"| {a['approval']} | {a['mode']} | {a['approver']} | {'APPROVED' if a['approved'] else 'DENIED'} | {a['reason']} |" for a in approvals(store, run_id)]
    for kind, title in (("DECISION", "Recorded decisions"), ("REPLAN", "Re-plans"), ("POLICY_BLOCKED", "Policy blocks"),
                        ("FALLBACK_ENGAGED", "Fallbacks"), ("ROLLBACK", "Rollbacks")):
        rows = [e for e in ev if e["kind"] == kind]
        if rows:
            L += ["", f"**{title}**", ""] + [f"- `{e['task_id']}` {json.dumps(e['payload'])[:220]}" for e in rows]
    L += ["", "Decision lineage of the design:", "", "```", lineage(store, run_id, "design"), "```", "",
          "## 9. Reliability metrics", "", "| Metric | Value |", "|---|---|", metrics.render(m), "",
          "## 10. Release readiness", ""]
    L += [f"- [{'x' if c['ok'] else ' '}] {c['item']} - {c['evidence']}" for c in rel["checks"]]
    L += ["", "**Rollout**", ""] + [f"1. {s}" for s in rel["rollout_plan"]] + ["", "**Rollback**", ""] + [f"- {s}" for s in rel["rollback_plan"]]
    L += ["", f"Recommended version bump: `{rel['recommended_version']}`", "", "## 11. Limitations of this prototype", ""]
    L += [f"- {x}" for x in LIMITATIONS]
    return "\n".join(L) + "\n"
