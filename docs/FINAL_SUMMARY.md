# Final engineering summary — Agentic SDLC orchestrator (URL shortener)

## Plan and rationale
The assignment's differentiator is the orchestration layer, so the work was sequenced to make it *provable* rather than merely present:

1. **Product baseline first** — a small but production-minded URL shortener (validation + SSRF guard, CSPRNG codes with bounded collision retry,
   token-bucket rate limit, 302 redirects, click analytics, health) with its own tests, so agents have a real artifact to build, change and validate.
2. **Change sets, not templates** — brownfield behaviour is modelled as anchor-based edits (`expiry`, `unique_visitors`) verified to commute and to pass tests in every order.
3. **Platform core** — models → store (versioned artifacts, hash-chained log) → policy → toolbox (autonomy enforcement) → engine → gates/approvals.
4. **Agents** per SDLC phase, all behind the same `run(ctx)` contract.
5. **Scenarios as data**, then chaos drills, then tests asserting governance behaviour, then docs generated from real runs.

Rationale for the main bets: determinism for testability; enforcement outside the agent; everything as events so metrics/lineage/reports are derivable and auditable.

## Artifacts delivered
* `orchestrator/` — engine, graph, store, policy, toolbox, gates, approvals, metrics, reports, pipeline, CLI, 17 agents (~2.9k LOC incl. catalog)
* `orchestrator/catalog/` — baseline service + brownfield change sets (the product the agents build)
* `scenarios/` — greenfield, brownfield, ambiguous + 5 chaos drills
* `tests/` — 64 tests (engine, foundations, agents, end-to-end)
* `docs/` — architecture, scenarios, testing/limitations, this summary, and 3 real generated `ENGINEERING_SUMMARY.md` examples
* Per run (generated): the service, its tests, `docs/openapi.json`, README, CHANGELOG, design log, engineering summary, metrics.json, audit log

## Validation performed
* 64/64 orchestrator tests pass; generated service suites pass at 34 / 44 / 47 tests across the three scenarios.
* Generated app launched with `uvicorn` and exercised with `curl` (TTL, redirect 302, unique visitors, SSRF rejection).
* Mutation check on the rollback control (2 tests fail when it is disabled).
* All audit chains verify; all chaos drills end in the expected state.

## Reliability metrics (one `make all` pass, 8 runs)
Run success 5/8 by design (3 chaos drills are *expected* to halt); 3 retries, 1 fallback, 2 replans, 24 task rollbacks, MTTR ≈ 0.24 s (wall-clock of
injected-fault recovery), end-to-end latency ≈ 1 s per run with sub-100 ms of human wait (scripted). Per-run values are in each `metrics.json`.

## Key risks and how they are controlled
| Risk | Control |
|---|---|
| Agent writes insecure/non-compliant code | policy pre-scan in `Toolbox.write`, workspace scan, reviewer rules; blocked content never reaches disk |
| Agent exceeds scope | `AutonomyProfile` globs, file/line budgets, single-writer ownership |
| Bad change reaches release | entry/exit gates, independent reviewer, hash-bound human approvals, release checklist with rollback plan |
| Runaway loops / cost | attempt budget, wall-clock budget, replan budget, kill-switch file |
| Half-applied change after failure | per-task undo journals, reverse-order rollback on safe-stop |
| Unreviewable history | hash-chained audit log, lineage and `verify` commands |
| Personal-data creep | CMP-001/002, human-only resolution of blocking compliance questions, hashed visitor ids |

## Assumptions
Single-node prototype deployment; humans are available for the two checkpoints (design when high-impact, release); the catalog's capabilities are the
intended scope; requirement text is English; the workspace is local and exclusively owned by the run.

## Limitations
See `docs/TESTING.md` §Limitations (catalog-bound implementer, rule-based NLP, no resume, no live-LLM validation, SQLite/single-process scale, no redesign loop on denial).

## What I would do next
1. Live-validate `AnthropicLLM` and widen the implementer from catalog edits to LLM-proposed diffs that still pass through the same toolbox/policy/gates.
2. Persist `TaskSpec`s to allow crash-safe **resume**; put tasks on a queue for multi-worker execution.
3. Redesign loop: denied approval → feedback → `design.architect` with reviewer comments (bounded).
4. Coverage + mutation gates; external anchoring of the audit-chain head; per-agent cost/token budgets.
