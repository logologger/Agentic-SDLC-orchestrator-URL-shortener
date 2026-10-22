# Testing approach, limitations and trade-offs

## Three layers of validation

| Layer | What is tested | Where | How many |
|---|---|---|---|
| **A. The generated service** (the product) | validation, SSRF guard, service logic, rate limiter, HTTP contract, expiry + migration, privacy-preserving counting | `orchestrator/catalog/base/tests`, `features.py::new_files` — executed **by the pipeline itself** (`test.unit`) in each workspace | 34 (greenfield) → 44 (+expiry) → 47 (+expiry +visitors) |
| **B. The orchestrator** (the differentiator) | engine semantics on synthetic graphs, store/audit chain, policy rules, toolbox confinement | `tests/test_engine.py`, `tests/test_foundations.py` | 14 + 26 |
| **C. End to end** | the 3 scenarios + 5 chaos drills, asserting *governance behaviour*, not just success | `tests/test_scenarios.py`, `tests/test_agents.py` | 14 + 10 |

Run: `make test` (64 tests, ~12 s). `make all` also runs the scenarios.

## What the tests actually prove (selected)

* **Parallelism and synchronisation** — a gauge shows two sibling tasks overlap and the join waits for both; in the real run the validation branches overlap and `review.gate` starts only after all three finish.
* **Bounded retries** — a task that always fails transiently is attempted exactly `1 + max_retries` times, then the run halts.
* **Policy failures are not retried** — a hard-coded secret is blocked *before* the write, costs exactly 2 attempts (primary + fallback) even with `max_retries=5`.
* **Rollback is exact** — after a permanent failure the workspace is compared file-by-file with the seed (content and set of files).
* **Re-planning** — a producer republishing an artifact causes the consumer to re-run and its stale output to be replaced; two mutually-feeding agents are stopped by the replan budget instead of looping.
* **Graph safety** — cyclic expansion proposed by an agent is rejected before commit.
* **Audit integrity** — editing or deleting any event is detected by `verify_chain`.
* **Human oversight** — approvals are bound to artifact hashes; a denied approval writes nothing; an unanswered blocking question halts instead of assuming; "store raw IPs" is blocked by compliance policy.
* **Black-box check of the output** — a test boots the *generated* app in a subprocess and exercises create → redirect → stats; separately it was launched with `uvicorn` and driven with `curl` (including the SSRF rejection of `169.254.169.254`).
* **Mutation check** — disabling `rollback_on_halt` made 2 tests fail (engine-level and end-to-end), i.e. the safety tests are sensitive to the behaviour they claim to protect.

## Limitations (honest list)

1. **Agents are not general code generators.** The implementer applies a curated catalog (baseline + `expiry` + `unique_visitors`). Requirements outside the catalog stop at the requirements gate. The *orchestration* generalises; the *capability set* does not.
2. **Requirement NLP is rule-based** (keyword/regex). It can miss ambiguities or over-trigger. The optional LLM seam (`--llm anthropic`) augments detection with strictly validated JSON and falls back to the rules, but it was **not exercised against a live model** here (no network in CI); only `FakeLLM` paths are tested.
3. **No resume.** State and artifacts are persisted and auditable, but an interrupted run is restarted, not resumed (dynamically-added tasks would need to be persisted as specs).
4. **Test quality ≠ test adequacy.** We gate on pass/fail, policy scan and "code change must ship with tests (CHG-002)". No coverage threshold or mutation testing of the generated service.
5. **Scale/perf.** SQLite, in-process rate limiter, single-process thread pool. The service has no load test; the NFR "p95 < 100 ms" in the ambiguous scenario is an *assumption*, recorded as such, not a measurement.
6. **DNS rebinding / open-redirect abuse** are documented risks, not mitigated (the shortener never fetches targets, so server-side SSRF is not reachable).
7. **Approval denial = safe-stop**; there is no redesign loop with reviewer feedback.
8. **Scripted humans.** Demos use `ScriptedApprover`; `--interactive` uses a real terminal prompt but is not covered by automated tests.
9. **Audit chain is tamper-*evident*, not tamper-*proof*:** someone able to rewrite the whole database and recompute hashes can forge a chain. Anchor the head hash externally in production.
10. **Clock/ordering:** MTTR and latency use wall-clock timestamps from one host.

## Trade-offs worth defending

* **Determinism over cleverness.** Reproducible agents let every guardrail and recovery path be asserted in tests. An LLM would raise capability and lower reproducibility; that is why it is a seam with strict validation + fallback, not the core.
* **Fail loudly over heal silently.** Anchor mismatch, policy hit, stale approval, denied approval → stop and restore, because the alternative (guessing) is worse in a change-controlled environment.
* **Engine-enforced autonomy over agent-side discipline.** Boundaries live in `Toolbox`/`PolicyEngine`, so even a buggy or compromised agent is contained (see `chaos-policy`).
* **Speculation costs a little work** (`design.explore` re-runs) but hides human wait time and demonstrates bounded re-planning.
* **One process, many threads.** Easy to reason about and observe; the cost is horizontal scalability, which would need a work queue and a shared store.
