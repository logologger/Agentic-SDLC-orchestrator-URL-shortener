"""End-to-end: the three required scenarios and the chaos drills, with assertions on governance behaviour."""
import json
import shutil
import subprocess
import sys

import pytest

from orchestrator import metrics, pipeline, reports
from orchestrator.approvals import ScriptedApprover
from orchestrator.catalog.features import base_files
from orchestrator.models import EngineConfig
from orchestrator.store import Store
from scenarios.definitions import SCENARIOS

CFG = EngineConfig(retry_backoff_s=0.0)


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("runs")


@pytest.fixture(scope="module")
def baseline(runs_dir):
    d = runs_dir / "_baseline"
    for p, c in base_files().items():
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_text(c)
    return d


@pytest.fixture(scope="module")
def store(runs_dir):
    return Store(str(runs_dir / "orchestrator.db"))


@pytest.fixture(scope="module")
def results(runs_dir, baseline, store):
    """Run every scenario once (greenfield first; the others start from its workspace)."""
    out = {}
    for name, sc in SCENARIOS.items():
        seed = out["greenfield"].workspace if sc.seed == "greenfield" else None
        out[name] = pipeline.run(sc.requirement, name, runs_dir, sc.approver(), seed=seed, faults=sc.faults, store=store, config=CFG)
    return out


def states(r, state):
    return sorted(t for t, s in r.result.states.items() if s == state)


def kinds(r, kind):
    return [e for e in r.store.events(r.run_id) if e["kind"] == kind]


def artifact(r, name):
    return r.store.get_artifact(r.run_id, name)[0]


def test_every_scenario_ends_as_expected(results):
    for name, r in results.items():
        assert r.result.status.value == SCENARIOS[name].expect, (name, r.result.halt_reason)


# ---- greenfield ---------------------------------------------------------------------------------
def test_greenfield_builds_a_working_service_with_tests_docs_and_summary(results):
    r = results["greenfield"]
    assert artifact(r, "test_report")["passed"] >= 30 and artifact(r, "scan_report")["violations"] == []
    for f in ("shortener/api.py", "tests/test_api.py", "README.md", "CHANGELOG.md", "docs/openapi.json", "docs/DESIGN.md"):
        assert (r.workspace / f).is_file(), f
    assert "/api/v1/urls" in json.loads((r.workspace / "docs/openapi.json").read_text())["paths"]
    summary = (r.run_dir / "ENGINEERING_SUMMARY.md").read_text()
    for heading in ("Requirement understanding", "Task decomposition", "Design decisions", "Risks", "Validation", "Governance trail",
                    "Reliability metrics", "Release readiness", "Limitations"):
        assert heading in summary
    assert "```mermaid" in summary


def test_greenfield_runs_validation_branches_in_parallel_and_joins_at_review(results):
    r = results["greenfield"]
    starts = {e["task_id"]: e["ts"] for e in kinds(r, "TASK_STARTED")}
    done = {e["task_id"]: e["ts"] for e in kinds(r, "TASK_SUCCEEDED")}
    assert max(starts[t] for t in ("test.unit", "scan.security", "docs.update")) < min(done[t] for t in ("test.unit", "docs.update"))
    assert starts["review.gate"] >= max(done[t] for t in ("test.unit", "scan.security", "docs.update"))      # sync point
    plan = artifact(r, "plan")
    assert len(plan["parallel_levels"]) >= 3 and len(plan["parallel_levels"][0]) >= 3                      # real DAG, not a chain


def test_generated_service_actually_serves_traffic(results, tmp_path):
    """Black-box check outside the agents' own tests: import the generated app and exercise it."""
    code = ("from fastapi.testclient import TestClient; from shortener.api import create_app\n"
            "c = TestClient(create_app(':memory:'))\n"
            "code = c.post('/api/v1/urls', json={'url': 'https://example.com/x'}).json()['code']\n"
            "r = c.get('/' + code, follow_redirects=False)\n"
            "assert r.status_code == 302 and r.headers['location'] == 'https://example.com/x'\n"
            "assert c.get(f'/api/v1/urls/{code}/stats').json()['total_clicks'] == 1\nprint('OK')")
    out = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=results["greenfield"].workspace, capture_output=True, text=True)
    assert out.stdout.strip() == "OK", out.stderr


# ---- brownfield ---------------------------------------------------------------------------------
def test_brownfield_reasons_about_impact_and_requires_human_approval_for_schema_change(results):
    r = results["brownfield"]
    impact = artifact(r, "impact")
    direct = {d["module"] for d in impact["direct"]}
    assert {"shortener/repository.py", "shortener/service.py", "shortener/api.py", "shortener/validation.py"} <= direct
    assert impact["risk"] == "high" and "shortener/codegen.py" not in direct
    assert {"tests/test_api.py", "tests/test_service.py"} <= set(impact["existing_tests_to_rerun"])
    assert "shortener/api.py" in {t["module"] for t in impact["transitive"]} | direct          # callers surface as regression risk
    approvals = reports.approvals(r.store, r.run_id)
    assert [a["approval"] for a in approvals] == ["design", "release"] and all(a["mode"] == "human" for a in approvals)
    assert artifact(r, "approval.design")["subject_hash"] == r.store.get_artifact(r.run_id, "design")[2]     # bound to reviewed content


def test_brownfield_changes_are_surgical_and_tested(results):
    r = results["brownfield"]
    changed = {c["path"] for c in artifact(r, "review")["manifest"]}
    assert changed == {"shortener/validation.py", "shortener/repository.py", "shortener/service.py", "shortener/api.py", "tests/test_expiry.py"}
    base_tests = artifact(results["greenfield"], "test_report")["passed"]
    assert artifact(r, "test_report")["passed"] > base_tests                                   # old tests still pass + new ones
    assert "ttl_seconds" in (r.workspace / "docs/openapi.json").read_text()
    assert "Run " + r.run_id in (r.workspace / "CHANGELOG.md").read_text()


# ---- ambiguous ----------------------------------------------------------------------------------
def test_ambiguous_requirement_is_clarified_replanned_and_privacy_safe(results):
    r = results["ambiguous"]
    spec = artifact(r, "spec")
    by_id = {a["id"]: a for a in spec["ambiguities"]}
    assert by_id["AMB-2"]["blocking"] and by_id["AMB-2"]["resolution"]["source"] == "human"        # PII decision is never assumed
    assert by_id["AMB-1"]["resolution"]["source"] == "assumed-default" and spec["privacy"]["mode"] == "hashed"
    assert r.store.latest_version(r.run_id, "spec") == 2
    replans = kinds(r, "REPLAN")
    assert [e["task_id"] for e in replans] == ["design.explore"]                                 # only the speculative branch is redone
    db_sql = "\n".join((r.workspace / p).read_text() for p in ("shortener/repository.py", "shortener/privacy.py"))
    assert "ip_address" not in db_sql and "user_agent TEXT" not in db_sql
    assert any("assumption" in w for w in artifact(r, "review")["warnings"])                     # assumptions surfaced to reviewer


def test_decision_lineage_links_design_back_to_clarified_spec(results):
    r = results["ambiguous"]
    tree = reports.lineage(r.store, r.run_id, "design")
    assert "design@v1  <- task design.architect" in tree and "spec@v2  <- task req.clarify" in tree and "spec@v1  <- task req.analyze" in tree


# ---- chaos --------------------------------------------------------------------------------------
def test_transient_faults_are_retried_and_recovery_time_is_measured(results):
    r = results["chaos-retry"]
    m = metrics.compute(r.store, r.run_id)
    assert r.result.status.value == "COMPLETED" and m["retries"] == 3 and m["mttr_s"] is not None and m["recovered_tasks"] == 2


def test_hardcoded_secret_is_blocked_and_fallback_agent_recovers(results):
    r = results["chaos-policy"]
    assert kinds(r, "POLICY_BLOCKED")[0]["payload"]["violations"][0].startswith("SEC-001")
    assert kinds(r, "FALLBACK_ENGAGED")[0]["payload"]["agent"] == "implementer_safe"
    assert "sk-live" not in (r.workspace / "shortener/api.py").read_text() and artifact(r, "scan_report")["violations"] == []


def test_permanent_failure_restores_workspace_exactly(results, runs_dir):
    r = results["chaos-halt"]
    assert not states(r, "SUCCEEDED") and "test.unit" in states(r, "FAILED") and states(r, "ROLLED_BACK")
    seed = results["greenfield"].workspace
    for p in seed.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".json", ".txt"}:
            rel = p.relative_to(seed)
            assert (r.workspace / rel).read_text() == p.read_text(), f"{rel} differs after rollback"
    extra = {p.relative_to(r.workspace).as_posix() for p in r.workspace.rglob("*.py")} - {p.relative_to(seed).as_posix() for p in seed.rglob("*.py")}
    assert not extra, f"files left behind after rollback: {extra}"
    assert metrics.compute(r.store, r.run_id)["safe_stops"] == 1


def test_denied_approval_halts_before_any_code_is_written(results):
    r = results["chaos-denied"]
    assert states(r, "FAILED") == ["approve.design"] and "plan.decompose" in states(r, "ABORTED")
    assert not kinds(r, "GRAPH_EXPANDED") and metrics.compute(r.store, r.run_id)["approvals_denied"] == 1


def test_raw_pii_answer_is_blocked_by_compliance_policy(results):
    r = results["chaos-pii"]
    assert "req.clarify" in states(r, "FAILED")
    assert any("CMP-002" in v for e in kinds(r, "POLICY_BLOCKED") for v in e["payload"]["violations"])
    assert "design.architect" in states(r, "ABORTED")


def test_all_audit_chains_verify_and_metrics_aggregate(results, store):
    for r in results.values():
        assert store.verify_chain(r.run_id) == (True, None)
    agg = metrics.compute_all(store)
    assert agg["runs"] >= len(results) and 0 < agg["run_success_rate"] < 1 and agg["rollbacks"] > 0 and agg["replans"] >= 1
