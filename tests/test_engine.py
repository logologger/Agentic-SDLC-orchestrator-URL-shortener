"""Engine behaviour on synthetic graphs: ordering, parallelism, retries, fallback, rollback, safe-stop, re-planning."""
import os

import pytest

from orchestrator.engine import Fault
from orchestrator.graph import GraphError
from orchestrator.models import AgentError, AgentResult, EngineConfig, RunStatus, TaskSpec, TaskState, TransientError
from tests.helpers import FnAgent, Gauge, events, make_engine

S = TaskState.SUCCEEDED


def noop(name="noop"):
    return FnAgent(name, lambda ctx: None)


def test_dependencies_are_respected_and_parallel_branches_overlap(tmp_path):
    gauge, order = Gauge(), []
    def work(ctx):
        order.append(ctx.task.id)
        gauge(ctx)

    agents = [FnAgent("work", work)]
    tasks = [TaskSpec("a", "work"), TaskSpec("b", "work", ["a"]), TaskSpec("c", "work", ["a"]), TaskSpec("d", "work", ["b", "c"])]
    eng = make_engine(tmp_path, agents, tasks)
    res = eng.run()
    assert res.status == RunStatus.COMPLETED
    assert order[0] == "a" and order[-1] == "d" and set(order[1:3]) == {"b", "c"}
    assert gauge.max == 2   # b and c ran concurrently, d waited for both (synchronisation)


def test_transient_failures_are_retried_with_a_bound(tmp_path):
    calls = []

    def flaky(ctx):
        calls.append(ctx.attempt)
        if len(calls) < 3:
            raise TransientError("boom")

    eng = make_engine(tmp_path, [FnAgent("flaky", flaky)], [TaskSpec("t", "flaky", max_retries=2)])
    assert eng.run().status == RunStatus.COMPLETED and len(calls) == 3
    calls.clear()
    eng = make_engine(tmp_path / "x", [FnAgent("flaky", lambda ctx: (_ for _ in ()).throw(TransientError("always")))],
                      [TaskSpec("t", "flaky", max_retries=1)])
    res = eng.run()
    assert res.status == RunStatus.HALTED and eng.attempts["t"] == 2     # 1 try + 1 retry, then stop


def test_fallback_agent_used_after_deterministic_failure(tmp_path):
    def primary(ctx):
        raise AgentError("cannot")

    eng = make_engine(tmp_path, [FnAgent("p", primary), FnAgent("f", lambda c: AgentResult({"x": 1}))],
                      [TaskSpec("t", "p", fallback="f")])
    assert eng.run().status == RunStatus.COMPLETED
    assert events(eng, "FALLBACK_ENGAGED") and eng.store.get_artifact("r1", "x")[0] == 1


def test_failure_rolls_back_every_write_and_safe_stops(tmp_path):
    def writer(ctx):
        ctx.toolbox.write("shortener/a.py", "x = 1\n")

    def second(ctx):
        ctx.toolbox.write("shortener/b.py", "y = 2\n")
        raise AgentError("late failure")

    eng = make_engine(tmp_path, [FnAgent("w", writer), FnAgent("s", second), noop()],
                      [TaskSpec("a", "w", side_effects=True), TaskSpec("b", "s", ["a"], side_effects=True), TaskSpec("c", "noop", ["b"])])
    res = eng.run()
    ws = tmp_path / "ws" / "shortener"
    assert res.status == RunStatus.HALTED
    assert res.states == {"a": "ROLLED_BACK", "b": "FAILED", "c": "ABORTED"}
    assert not (ws / "a.py").exists() and not (ws / "b.py").exists()      # workspace is exactly as before the run


def test_policy_violation_skips_retries_and_goes_to_fallback(tmp_path):
    def bad(ctx):
        ctx.toolbox.write("shortener/a.py", 'API_KEY = "sk-live-abcdefghijklmnop1234"\n')

    def good(ctx):
        ctx.toolbox.write("shortener/a.py", "x = 1\n")

    eng = make_engine(tmp_path, [FnAgent("bad", bad), FnAgent("good", good)], [TaskSpec("t", "bad", fallback="good", max_retries=5)])
    assert eng.run().status == RunStatus.COMPLETED
    assert eng.attempts["t"] == 2          # no futile retries of a deterministic policy failure
    assert events(eng, "POLICY_BLOCKED")[0]["payload"]["violations"][0].startswith("SEC-001")
    assert (tmp_path / "ws/shortener/a.py").read_text() == "x = 1\n"


def test_entry_and_exit_gates_block_progress(tmp_path):
    eng = make_engine(tmp_path, [noop()], [TaskSpec("t", "noop", gate_in=["tests_green_in"])])
    res = eng.run()
    assert res.status == RunStatus.HALTED and events(eng, "GATE_FAILED")[0]["payload"]["gate"] == "tests_green_in"


def test_attempt_budget_and_kill_switch_stop_the_run(tmp_path):
    eng = make_engine(tmp_path, [noop()], [TaskSpec(f"t{i}", "noop") for i in range(5)], config=EngineConfig(max_total_attempts=2))
    res = eng.run()
    assert res.status == RunStatus.HALTED and "budget" in " ".join(e["payload"]["error"] for e in events(eng, "ATTEMPT_FAILED")) + res.halt_reason

    switch = tmp_path / "STOP"
    switch.write_text("")
    eng = make_engine(tmp_path / "k", [noop()], [TaskSpec("t", "noop")], config=EngineConfig(kill_switch_file=str(switch)))
    res = eng.run()
    assert res.status == RunStatus.HALTED and res.halt_reason == "kill switch engaged" and res.states["t"] == "ABORTED"


def test_upstream_change_invalidates_consumers_and_replans(tmp_path):
    """producer republishes `cfg` v2 after a consumer already used v1 -> consumer (and its dependants) re-run."""
    state = {"consumer_runs": 0}

    def producer1(ctx):
        return AgentResult({"cfg": {"v": 1}})

    def consumer(ctx):
        state["consumer_runs"] += 1
        ctx.toolbox.write("shortener/out.py", f"v = {ctx.read('cfg')['v']}\n")

    def producer2(ctx):
        import time
        time.sleep(0.2)
        return AgentResult({"cfg": {"v": 2}})

    eng = make_engine(tmp_path, [FnAgent("p1", producer1), FnAgent("p2", producer2), FnAgent("c", consumer), noop()], [
        TaskSpec("p1", "p1"), TaskSpec("consume", "c", ["p1"], side_effects=True),
        TaskSpec("p2", "p2", ["p1"]), TaskSpec("after", "noop", ["consume", "p2"])])
    res = eng.run()
    assert res.status == RunStatus.COMPLETED
    assert state["consumer_runs"] == 2 and events(eng, "REPLAN")
    assert (tmp_path / "ws/shortener/out.py").read_text() == "v = 2\n"          # the stale output was replaced, not left behind
    assert eng.store.latest_version("r1", "cfg") == 2


def test_replan_budget_is_bounded(tmp_path):
    """A and B feed each other (x -> y -> x) and never converge; the engine must stop, not loop forever."""
    import itertools
    counter = itertools.count(1)

    def seed(ctx):
        return AgentResult({"x": {"n": 0}})

    def a(ctx):
        ctx.read("x")
        return AgentResult({"y": {"n": next(counter)}})

    def b(ctx):
        ctx.read("y")
        return AgentResult({"x": {"n": next(counter)}})

    eng = make_engine(tmp_path, [FnAgent("seed", seed), FnAgent("a", a), FnAgent("b", b)],
                      [TaskSpec("seed", "seed"), TaskSpec("A", "a", ["seed"]), TaskSpec("B", "b", ["A"])],
                      config=EngineConfig(max_replans_per_task=2))
    res = eng.run()
    assert res.status == RunStatus.HALTED and "replan budget exceeded" in res.halt_reason


def test_graph_expansion_adds_tasks_and_rewires_barrier(tmp_path):
    ran = []

    def planner(ctx):
        units = [TaskSpec(f"u{i}", "unit", [ctx.task.id]) for i in range(3)]
        return AgentResult(expand=units, add_deps={"sync": [u.id for u in units]})

    eng = make_engine(tmp_path, [FnAgent("plan", planner), FnAgent("unit", lambda c: ran.append(c.task.id)), noop()],
                      [TaskSpec("plan", "plan"), TaskSpec("sync", "noop", ["plan"]), TaskSpec("end", "noop", ["sync"])])
    assert eng.run().status == RunStatus.COMPLETED
    assert sorted(ran) == ["u0", "u1", "u2"] and eng.completion_order.index("sync") > eng.completion_order.index("u2")


def test_cyclic_expansion_is_rejected_before_it_is_committed(tmp_path):
    def planner(ctx):
        return AgentResult(expand=[TaskSpec("x", "noop", ["y"]), TaskSpec("y", "noop", ["x"])])

    eng = make_engine(tmp_path, [FnAgent("plan", planner), noop()], [TaskSpec("plan", "plan")])
    res = eng.run()
    assert res.status == RunStatus.HALTED and "x" not in eng.tasks and "y" not in eng.tasks


def test_static_graph_validation():
    from orchestrator import graph
    with pytest.raises(GraphError):
        graph.validate({"a": TaskSpec("a", "x", ["b"]), "b": TaskSpec("b", "x", ["a"])})
    with pytest.raises(GraphError):
        graph.validate({"a": TaskSpec("a", "x", ["ghost"])})
    levels = graph.topological_levels({"a": TaskSpec("a", "x"), "b": TaskSpec("b", "x", ["a"]), "c": TaskSpec("c", "x", ["a"])})
    assert levels == [["a"], ["b", "c"]]


def test_injected_faults_only_hit_the_primary_agent(tmp_path):
    eng = make_engine(tmp_path, [FnAgent("p", lambda c: None), FnAgent("f", lambda c: AgentResult({"via": "fallback"}))],
                      [TaskSpec("t", "p", fallback="f")], faults={"t": [Fault("fail_always")]})
    assert eng.run().status == RunStatus.COMPLETED and eng.store.get_artifact("r1", "via")[0] == "fallback"


def test_state_is_persisted(tmp_path):
    eng = make_engine(tmp_path, [noop()], [TaskSpec("a", "noop"), TaskSpec("b", "noop", ["a"])])
    eng.run()
    assert eng.store.tasks("r1") == {"a": ("SUCCEEDED", 1), "b": ("SUCCEEDED", 1)}
    assert eng.store.verify_chain("r1") == (True, None)
