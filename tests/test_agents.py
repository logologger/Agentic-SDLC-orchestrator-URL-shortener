import json

import pytest

from orchestrator import cli, metrics, pipeline
from orchestrator.approvals import ScriptedApprover
from orchestrator.catalog.features import FEATURES, EditError, Edit, apply_edits, base_files
from orchestrator.llm import FakeLLM, parse_ambiguities
from orchestrator.models import EngineConfig
from orchestrator.store import Store
from scenarios.definitions import AMBIGUOUS_REQ, BROWNFIELD_REQ, GREENFIELD_REQ, PRIVACY_ANSWER, reviewer

CFG = EngineConfig(retry_backoff_s=0.0)


def materialise(path, features=()):
    files = base_files()
    for name in features:
        for p, edits in FEATURES[name].edits.items():
            files[p] = apply_edits(p, files[p], edits)
        files.update(FEATURES[name].new_files)
    for p, c in files.items():
        (path / p).parent.mkdir(parents=True, exist_ok=True)
        (path / p).write_text(c)
    return path


def run(tmp_path, req, seed=None, approver=None, **kw):
    return pipeline.run(req, "t", tmp_path / "runs", approver or reviewer(), seed=seed, config=CFG, **kw)


# ---- catalog -----------------------------------------------------------------------------------
def test_feature_edits_commute():
    def build(order):
        files = base_files()
        for name in order:
            for p, edits in FEATURES[name].edits.items():
                files[p] = apply_edits(p, files[p], edits)
        return files
    assert build(["expiry", "unique_visitors"]) == build(["unique_visitors", "expiry"])


def test_ambiguous_or_missing_anchor_fails_loudly():
    with pytest.raises(EditError, match="matched 0 times"):
        apply_edits("x.py", "a = 1\n", [Edit("b = 2\n", "")])
    with pytest.raises(EditError, match="matched 2 times"):
        apply_edits("x.py", "a = 1\na = 1\n", [Edit("a = 1\n", "")])


# ---- requirements --------------------------------------------------------------------------------
def test_unmappable_requirement_stops_at_the_first_gate_instead_of_guessing(tmp_path):
    r = run(tmp_path, "Please make the app blockchain-powered with AI", seed=materialise(tmp_path / "seed"))
    assert r.result.status.value == "HALTED" and r.result.states["req.analyze"] == "FAILED"
    assert not r.workspace.joinpath("blockchain").exists()


def test_vague_nfr_and_temporary_without_duration_are_detected():
    from orchestrator.agents.requirements import _detect_ambiguities
    cats = {a["category"] for a in _detect_ambiguities(AMBIGUOUS_REQ)}
    assert cats == {"functional", "compliance", "nfr"}
    assert _detect_ambiguities(BROWNFIELD_REQ) == [] and _detect_ambiguities(GREENFIELD_REQ) == []
    assert _detect_ambiguities("Links expire after 7 days") == []        # explicit duration => not ambiguous


def test_unanswered_blocking_question_halts_instead_of_assuming(tmp_path):
    silent = ScriptedApprover(answers={})        # human never answers the privacy question
    r = run(tmp_path, AMBIGUOUS_REQ, seed=materialise(tmp_path / "seed"), approver=silent)
    assert r.result.states["req.clarify"] == "FAILED" and "AMB-2" in r.result.halt_reason
    assert r.result.states["design.architect"] == "ABORTED"


def test_rerunning_a_delivered_feature_is_a_safe_noop(tmp_path):
    r = run(tmp_path, BROWNFIELD_REQ, seed=materialise(tmp_path / "seed", ["expiry"]))
    assert r.result.status.value == "HALTED" and "already exist" in r.result.halt_reason
    assert (r.workspace / "shortener/service.py").read_text() == (tmp_path / "seed/shortener/service.py").read_text()


# ---- LLM seam ---------------------------------------------------------------------------------------
def test_llm_output_is_validated_strictly():
    ok = '{"ambiguities": [{"question": "Which regions?", "category": "nfr", "blocking": false, "default": "one"}]}'
    assert parse_ambiguities("noise " + ok + " trailing")[0]["question"] == "Which regions?"
    for bad in ("not json", '{"ambiguities": [{"question": "", "category": "nfr", "blocking": false}]}',
                '{"ambiguities": [{"question": "q", "category": "weird", "blocking": false}]}', '{"nope": []}'):
        with pytest.raises(ValueError):
            parse_ambiguities(bad)


def test_llm_suggestions_are_merged_and_garbage_falls_back_to_rules(tmp_path):
    seed = materialise(tmp_path / "seed")
    good = FakeLLM('{"ambiguities": [{"question": "Should links be case sensitive?", "category": "functional", "blocking": false, "default": "yes"}]}')
    r = run(tmp_path / "a", BROWNFIELD_REQ, seed=seed, llm=good)
    spec = r.store.get_artifact(r.run_id, "spec")[0]
    assert r.result.status.value == "COMPLETED" and any("case sensitive" in a["question"] for a in spec["ambiguities"])
    assert metrics.compute(r.store, r.run_id)["fallbacks"] == 0

    bad = FakeLLM("I am sorry, I cannot help with that")
    r = run(tmp_path / "b", BROWNFIELD_REQ, seed=seed, llm=bad)
    assert r.result.status.value == "COMPLETED" and metrics.compute(r.store, r.run_id)["fallbacks"] == 1     # rule-based agent took over


# ---- CLI ------------------------------------------------------------------------------------------------
def test_cli_runs_scenario_and_inspection_commands(tmp_path, capsys):
    runs = str(tmp_path / "runs")
    assert cli.main(["--runs-dir", runs, "run", "greenfield", "--quiet"]) == 0
    store = Store(runs + "/orchestrator.db")
    run_id = store.run_ids()[0]
    for cmd, needle in ((["verify", run_id], "audit chain OK"), (["trace", run_id], "RUN_FINISHED"),
                        (["lineage", run_id, "design"], "design@v1"), (["runs"], run_id)):
        assert cli.main(["--runs-dir", runs, *cmd]) == 0
        assert needle in capsys.readouterr().out
    assert cli.main(["--runs-dir", runs, "run", "nonsense"]) == 2


def test_cli_fault_injection_flag(tmp_path, capsys):
    runs = str(tmp_path / "runs")
    assert cli.main(["--runs-dir", runs, "run", "greenfield", "--quiet", "--inject", "test.unit=transient:1"]) == 0
    assert "retries 1" in capsys.readouterr().out
