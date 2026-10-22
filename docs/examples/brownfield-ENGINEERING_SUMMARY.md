# Engineering summary - run `brownfield-013417-7d63`

**Scenario:** brownfield  |  **Status:** COMPLETED  |  agentic-url-shortener/runs/brownfield-013417-7d63/workspace`

> Add optional expiry to short links: callers can pass ttl_seconds (60 seconds to 1 year); after expiry the redirect returns 410 Gone and clicks are no longer counted. Existing links must keep working and existing databases must be migrated without downtime.

## 1. Requirement understanding

Mode: **brownfield**. Capabilities: expiry.

| ID | Requirement | Acceptance criteria |
|---|---|---|
| F-EXP | Links may carry an optional TTL; expired links stop redirecting | ttl_seconds in [60, 31536000] yields expires_at = created_at + ttl; redirect after expiry returns 410 and records no click; ttl outside the range returns 422; databases created before this release keep working (idempotent migration) |

**Ambiguities and how they were closed**

- none

**Assumptions**

- Single-node deployment with SQLite is acceptable for the prototype
- 302 (not 301) redirects so every click is observable and links stay revocable

**Out of scope**

- Authentication / per-user link ownership
- Multi-region replication
- Link deletion UI

## 2. Codebase impact (brownfield)

Risk: **high** - schema change on existing data

| Module | Why |
|---|---|
| `shortener/api.py` | expiry: change set edits this module |
| `shortener/repository.py` | expiry: change set edits this module |
| `shortener/service.py` | expiry: change set edits this module |
| `shortener/validation.py` | expiry: change set edits this module |
| `tests/test_api.py` (transitive) | imports shortener/api.py - regression surface |
| `tests/test_service.py` (transitive) | imports shortener/repository.py, shortener/service.py - regression surface |
| `tests/test_validation.py` (transitive) | imports shortener/validation.py - regression surface |

Routes touched: /healthz, /api/v1/urls, /api/v1/urls/{code}/stats, /{code}  
Tables: {"shortener/repository.py": ["links", "clicks"]}  
Existing tests re-run: tests/test_api.py, tests/test_service.py, tests/test_validation.py

## 3. Task decomposition and orchestration

Source units are ordered by the import graph of the produced code; test units wait for all source units so they are authored against the final contract. Units within a level run in parallel.

| Level | Work units (run in parallel) |
|---|---|
| 0 | `impl:shortener/repository.py`, `impl:shortener/validation.py` |
| 1 | `impl:shortener/service.py` |
| 2 | `impl:shortener/api.py` |
| 3 | `impl:tests/test_expiry.py` |

```mermaid
flowchart LR
  req_analyze["req.analyze"]
  req_clarify{{"req.clarify"}}
  design_explore["design.explore"]
  impact_analyze["impact.analyze"]
  design_architect["design.architect"]
  approve_design{{"approve.design"}}
  plan_decompose["plan.decompose"]
  impl_sync["impl.sync"]
  test_unit["test.unit"]
  scan_security["scan.security"]
  docs_update["docs.update"]
  review_gate["review.gate"]
  release_readiness["release.readiness"]
  approve_release{{"approve.release"}}
  release_finalize["release.finalize"]
  impl_shortener_api_py["impl:shortener/api.py"]
  impl_shortener_repository_py["impl:shortener/repository.py"]
  impl_shortener_service_py["impl:shortener/service.py"]
  impl_shortener_validation_py["impl:shortener/validation.py"]
  impl_tests_test_expiry_py["impl:tests/test_expiry.py"]
  req_analyze --> req_clarify
  req_analyze --> design_explore
  req_clarify --> impact_analyze
  req_clarify --> design_architect
  design_explore --> design_architect
  impact_analyze --> design_architect
  design_architect --> approve_design
  approve_design --> plan_decompose
  impl_shortener_api_py --> impl_sync
  impl_shortener_repository_py --> impl_sync
  impl_shortener_service_py --> impl_sync
  impl_shortener_validation_py --> impl_sync
  impl_tests_test_expiry_py --> impl_sync
  plan_decompose --> impl_sync
  impl_sync --> test_unit
  impl_sync --> scan_security
  impl_sync --> docs_update
  test_unit --> review_gate
  scan_security --> review_gate
  docs_update --> review_gate
  review_gate --> release_readiness
  release_readiness --> approve_release
  approve_release --> release_finalize
  plan_decompose --> impl_shortener_api_py
  impl_shortener_repository_py --> impl_shortener_api_py
  impl_shortener_service_py --> impl_shortener_api_py
  impl_shortener_validation_py --> impl_shortener_api_py
  plan_decompose --> impl_shortener_repository_py
  plan_decompose --> impl_shortener_service_py
  impl_shortener_repository_py --> impl_shortener_service_py
  impl_shortener_validation_py --> impl_shortener_service_py
  plan_decompose --> impl_shortener_validation_py
  plan_decompose --> impl_tests_test_expiry_py
  impl_shortener_api_py --> impl_tests_test_expiry_py
  impl_shortener_repository_py --> impl_tests_test_expiry_py
  impl_shortener_service_py --> impl_tests_test_expiry_py
  impl_shortener_validation_py --> impl_tests_test_expiry_py
  style approve_design fill:#d4f4dd
  style approve_release fill:#d4f4dd
  style design_architect fill:#d4f4dd
  style design_explore fill:#d4f4dd
  style docs_update fill:#d4f4dd
  style impact_analyze fill:#d4f4dd
  style impl_sync fill:#d4f4dd
  style impl_shortener_api_py fill:#d4f4dd
  style impl_shortener_repository_py fill:#d4f4dd
  style impl_shortener_service_py fill:#d4f4dd
  style impl_shortener_validation_py fill:#d4f4dd
  style impl_tests_test_expiry_py fill:#d4f4dd
  style plan_decompose fill:#d4f4dd
  style release_finalize fill:#d4f4dd
  style release_readiness fill:#d4f4dd
  style req_analyze fill:#d4f4dd
  style req_clarify fill:#d4f4dd
  style review_gate fill:#d4f4dd
  style scan_security fill:#d4f4dd
  style test_unit fill:#d4f4dd
```

## 4. Design decisions and trade-offs

| ID | Decision | Rationale | Alternatives rejected |
|---|---|---|---|
| D-EXP-1 | Store absolute `expires_at` (REAL, nullable) on `links` | Absolute timestamps make reads a single comparison and survive TTL policy changes | store ttl + created_at (needs arithmetic on every read); background reaper deleting rows (loses analytics, needs scheduler) |
| D-EXP-2 | Lazy expiry check at resolve time; rows are kept | No scheduler to operate; analytics history preserved; 410 is more informative than 404 | periodic purge job |
| D-EXP-3 | Additive, idempotent migration (`ALTER TABLE ... ADD COLUMN`) executed on startup | Backward compatible: existing rows get NULL (= never expires); old binaries ignore the column | offline migration tool; new table + backfill |

**API changes:** POST /api/v1/urls: optional `ttl_seconds` (60..31536000); response gains `expires_at` (nullable); GET /{code}: new 410 Gone response for expired links

## 5. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Schema change on a live database | low | high | Additive nullable column, idempotent migration, covered by a legacy-database test; rollback = ignore column |
| Clock skew between nodes makes links expire early/late | medium | low | Single clock source per deployment; document NTP requirement |

## 6. Artifacts produced

| File | Action | + | - |
|---|---|---|---|
| `shortener/api.py` | modify | 6 | 1 |
| `shortener/repository.py` | modify | 9 | 4 |
| `shortener/service.py` | modify | 12 | 4 |
| `shortener/validation.py` | modify | 10 | 0 |
| `tests/test_expiry.py` | create | 66 | 0 |
| `README.md` | docs | | |
| `CHANGELOG.md` | docs | | |
| `docs/DESIGN.md` | docs | | |
| `docs/openapi.json` | docs | | |

## 7. Validation

- **Tests:** 44 passed, 0 failed, 0 errors (4 test files)
- **Policy scan:** 17 files, 0 violation(s); rules: SEC-001 secrets, SEC-002 dangerous calls, SEC-003 SQL injection, SEC-004 required controls, CMP-001 raw PII at rest
- **Independent review:** 0 blocker(s), 0 warning(s)

## 8. Governance trail

| Approval | Mode | Approver | Decision | Reason |
|---|---|---|---|---|
| design | human | eng-lead (scripted) | APPROVED | Additive migration and API change reviewed; rollback plan acceptable |
| release | human | eng-lead (scripted) | APPROVED | Checklist green, rollout/rollback plan acceptable |

Decision lineage of the design:

```
design@v1  <- task design.architect  sha256:ddd8f92b340be8e9
  design_draft@v1  <- task design.explore  sha256:f1f650d4b0ab9565
    spec@v1  <- task req.analyze  sha256:4f827dd1a536bf75
  impact@v1  <- task impact.analyze  sha256:f9089e0b34641435
    spec@v1  (expanded above)
  spec@v1  (expanded above)
```

## 9. Reliability metrics

| Metric | Value |
|---|---|
| Task success rate | 100% (20/20) |
| Completed-then-rolled-back tasks | 0% of tasks (work done, then undone by safe-stop) |
| First-pass success rate | 100% |
| Attempts / retries / fallbacks | 20 / 0 / 0 |
| Policy blocks / gate failures | 0 / 0 |
| Replans / rollbacks / safe-stops | 0 / 0 / 0 |
| Approvals requested / denied | 2 / 0 |
| MTTR | n/a (no failures recovered) |
| End-to-end latency | 1.146s (human wait 0.051s, automation 1.096s) |

## 10. Release readiness

- [x] automated tests green - 44 passed across 4 test files
- [x] policy scan clean - 17 files, 0 violation(s)
- [x] documentation regenerated - 4 endpoints in OpenAPI + README
- [x] independent review has no blockers - 5 files, +103/-9
- [x] design approval recorded - human by eng-lead (scripted)
- [x] audit log hash-chain intact - sha256 chain verified up to this point

**Rollout**

1. Deploy to staging and run the smoke tests (create -> redirect -> stats).
1. Start the new build; additive migration runs automatically at startup (idempotent).
1. Watch error rate, p95 redirect latency and /healthz for 30 minutes before full traffic.

**Rollback**

- Redeploy the previous build; no data restore required.
- Schema change is additive (new nullable column / new table): the previous build ignores it, so rollback loses no data.

Recommended version bump: `minor`

## 11. Limitations of this prototype

- Agents are deterministic/rule-based; the LLM seam (orchestrator/llm.py) augments requirement analysis only and is not exercised in CI.
- The implementer applies a curated catalog of change sets; it does not synthesise arbitrary code. Unknown requirements stop at the requirements gate.
- Runs are persisted (state, artifacts, hash-chained audit log) but an interrupted run cannot be resumed; it must be restarted.
- Rollback restores the workspace via undo journals; it does not undo external side effects (none are performed by design).
- Human denial of an approval safe-stops the run; it does not (yet) route feedback back into a redesign loop.
- SQLite + in-process rate limiting + hashed visitors are single-node prototype choices; see design risks for the scale-out path.
- Test quality is measured by pass/fail and policy scans, not by mutation testing or coverage thresholds.
