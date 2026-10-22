# Scenarios

Each scenario runs the **same governed pipeline** (see ARCHITECTURE §2); what differs is the requirement, the state of the codebase and where
humans are pulled in. Every figure below comes from a real run — the full per-run reports are in [`docs/examples/`](examples).

`python -m orchestrator run core` runs all three in order; brownfield and ambiguous start from the *greenfield output*.

---

## 1. Greenfield — "Build a URL shortener from scratch"

> Build a URL shortener service. Users create short links for long URLs, optionally with a custom alias, and are redirected with HTTP 302.
> Each link has click analytics. Link creation is rate limited to 10 requests per minute per client. Expose a health endpoint.
> Reject non-http(s) and private-network targets.

**Understanding.** `RequirementsAgent` normalises this into 5 functional requirements (F-1…F-5), each with machine-checkable acceptance criteria,
NFRs, assumptions (302 not 301; single-node SQLite), out-of-scope items. No ambiguity → `req.clarify` is a no-op and the spec stays at v1.

**Decomposition.** Architect → 12 files, 5 core decisions (D-CORE-1…5), 4 risks. The planner derives dependencies from the import graph of the
code to be written and expands the graph with 12 `impl:` tasks in four parallel levels:

| Level | Work units |
|---|---|
| 0 | `conftest.py`, `requirements.txt`, `shortener/__init__`, `codegen`, `ratelimit`, `repository`, `validation` (7 in parallel) |
| 1 | `service` (imports codegen, repository, validation) |
| 2 | `api` (imports service, ratelimit, repository, validation) |
| 3 | `test_api`, `test_service`, `test_validation` (3 in parallel) |

**Orchestration highlights.** Design approval is *auto-approved by policy* (no schema/API/PII impact — logged as `policy-auto`); the release is always
a human decision. After `impl.sync`, `test.unit`, `scan.security` and `docs.update` run concurrently and join at `review.gate`.

**Validation.** 34 tests pass; 12 files scanned, 0 violations; OpenAPI exported from the *running app* (`docs/openapi.json`); an independent
black-box test boots the generated app and exercises create → redirect → stats; the service was also launched with `uvicorn` and driven with `curl`.
26/26 tasks succeeded, 0 retries.

---

## 2. Brownfield — "Add link expiry to the running service"

> Add optional expiry to short links: callers can pass `ttl_seconds` (60 seconds to 1 year); after expiry the redirect returns 410 Gone and clicks
> are no longer counted. Existing links must keep working and existing databases must be migrated without downtime.

**Codebase reasoning.** `ImpactAgent` parses the live workspace (AST), builds the import graph and reports:

* direct: `api.py`, `repository.py`, `service.py`, `validation.py` (+ why each);
* **not** touched: `codegen.py`, `ratelimit.py` — the change is surgical;
* transitive regression surface: `tests/test_api.py`, `test_service.py`, `test_validation.py` (they import changed modules → re-run);
* routes touched, tables (`links`, `clicks`) and **risk = high** (schema change on existing data).

**Governance.** Because the design changes a schema *and* a public API, `approve.design` requires a human. The approval request lists the reasons
and is bound to the design's sha256; if the design were re-issued, the approval would be stale and `plan.decompose`'s entry gate would refuse to start.

**Execution.** 4 modify-units + 1 new test file, 37 added / 9 removed source lines. Edits are *anchor-based*: each must match exactly once or the unit fails
(→ rollback). The migration is additive and idempotent (`ALTER TABLE … ADD COLUMN expires_at` guarded by `PRAGMA table_info`); a legacy-database
test proves old databases open and old rows never expire.

**Validation.** 44 tests (34 existing + 10 new) pass — existing behaviour is protected. README, OpenAPI (`ttl_seconds`, `expires_at`, 410) and
CHANGELOG are regenerated; the reviewer confirms code changes are accompanied by tests (CHG-002). The release checklist includes an explicit rollback plan:
the column is nullable and ignored by the previous build, so rollback loses no data.

---

## 3. Ambiguous — "Make links temporary and let us know who is clicking them, and keep it fast."

**Understanding.** Three ambiguities are detected and classified:

| ID | Question | Class | Outcome |
|---|---|---|---|
| AMB-1 | "temporary": lifetime and who chooses it? | functional, non-blocking | **assumed default** (optional `ttl_seconds`, 60 s–365 d) — flagged to the reviewer |
| AMB-2 | "who is clicking": identify individuals or count visitors? retention? legal basis? | **compliance, blocking** | **human answer required**; never defaulted |
| AMB-3 | "fast": not measurable | NFR, non-blocking | assumed (p95 < 100 ms @ 100 rps, 99.9%) and recorded |

The scripted human answers AMB-2 with *"count distinct visitors only… salted hashes… never store raw IP or user-agent"*, which becomes a recorded
decision (`D-PRIV-1`) and `spec.privacy.mode = hashed`.

**Non-linear execution and re-planning.** While the human is answering, `design.explore` speculatively drafts the file footprint from spec **v1**. When
`req.clarify` publishes spec **v2**, the engine detects that `design.explore` consumed a superseded input, rolls it back and re-runs it
(`REPLAN … upstream artifact spec changed`). Only that branch is redone; nothing downstream had started. Lineage shows the whole chain:

```
design@v1  <- task design.architect
  design_draft@v1  <- task design.explore
    spec@v1  <- task req.analyze
  impact@v1  <- task impact.analyze
    spec@v2  <- task req.clarify
```

**Outcome.** Two features (`expiry`, `unique_visitors`) → 7 files; `unique_visitors` stores only an HMAC of (day, ip, user-agent) — the test suite asserts
that neither the raw IP nor the user-agent appears in the SQLite dump. 47 tests pass; the reviewer warns that AMB-1 and AMB-3 were closed by assumption, not by a human.

---

## 4. Chaos drills (same pipeline, injected failures)

`python -m orchestrator run chaos` — each is also an automated test.

| Drill | Injection | Observed behaviour |
|---|---|---|
| `chaos-retry` | transient failure ×2 on `impl:service.py`, ×1 on `test.unit` | 3 retries, run COMPLETES, MTTR recorded |
| `chaos-policy` | primary implementer appends a hard-coded API key to `api.py` | `POLICY_BLOCKED SEC-001` before the write, **no futile retries**, `implementer_safe` fallback takes over, scan clean |
| `chaos-halt` | permanent failure in `test.unit` after all code was written | `SAFE_STOP`; in-flight tasks drain; every completed task rolled back in reverse order; workspace byte-identical to the seed |
| `chaos-denied` | reviewer denies the design (privacy not signed off) | run HALTED before any code is written; 0 files touched |
| `chaos-pii` | human answers "store the full IP address of every visitor" | `CMP-002` blocks it at clarification; run HALTED |

Try your own: `python -m orchestrator run brownfield --inject 'impl:shortener/service.py=transient:3'` (more transient failures than the retry budget: 3 failed attempts, then the fallback agent takes over — 2 retries + 1 fallback in the metrics).
