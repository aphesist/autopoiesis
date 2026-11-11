"""The confirmatory steps (reeval, detect) and the analysis paths that use them, on a tiny
2-generation fake run with the local (unsafe) sandbox. No real model calls."""

import json

import pytest

from autopoiesis.analysis import analyse
from autopoiesis.confirmatory import detect, load_reeval, reeval
from autopoiesis.config import Caps, Config, Experiment
from autopoiesis.llm import Completion, Ledger
from autopoiesis.loop import Run
from autopoiesis.sandbox import LocalSandbox

from .conftest import SPLITS, TASKS
from .test_loop import REPO, FakeLLM


class MonitoringFakeLLM(FakeLLM):
    """FakeLLM plus a monitor that flags diffs containing os._exit, and a call counter."""

    def __init__(self):
        super().__init__()
        self.calls: dict[str, int] = {}

    def complete(self, *, role, model, system, messages, max_tokens, effort=None, meta=None):
        self.calls[role] = self.calls.get(role, 0) + 1
        if role == "monitor":
            flagged = "os._exit" in messages[0]["content"]
            return Completion(json.dumps({"suspicious": flagged, "reason": "fake"}), {}, "end_turn")
        return super().complete(role=role, model=model, system=system, messages=messages,
                                max_tokens=max_tokens, effort=effort, meta=meta)


@pytest.fixture
def fake_run(tmp_path):
    cfg = Config(caps=Caps(test_timeout_s=2.0, agent_timeout_s=30.0),
                 experiment=Experiment(seeds_per_arm=1, generations=2, heldout_every=2,
                                       seed_baseline_k=2),
                 use_batch_for_mutations=False, sandbox="local")
    run = Run(cfg, tmp_path / "run", MonitoringFakeLLM(), LocalSandbox(), TASKS, SPLITS,
              REPO / "agent_seed")
    run.run()
    return run


def test_provisional_paths_without_confirmatory_data(fake_run):
    res = analyse(fake_run.dir, resamples=200)
    assert res["E1"]["source"] == "provisional"
    assert res["E5"]["source"] == "archive"
    assert set(res["E5"]) >= {"gap_flag", "static_scan"} and "monitor_small" not in res["E5"]


def test_reeval_detect_and_confirmatory_analysis(fake_run):
    run_dir = fake_run.dir
    winners = {n: a.proxy_winner() for n, a in fake_run.archives.items()}
    assert all(w is not None for w in winners.values())
    dgm_best, rnd_best = winners["dgm-00"].id, winners["random-00"].id

    llm = MonitoringFakeLLM()
    summary = reeval(run_dir, llm, LocalSandbox(), k=2)
    assert summary["status"] == "complete"
    for aid, reps in [(dgm_best, 2), (rnd_best, 2), ("seed", 2)]:
        a = summary["agents"][aid]
        assert a["reps_done"] == reps and len(a["reps"]) == reps
        for i in range(1, reps + 1):
            rep = run_dir / "reeval" / aid / f"rep{i}"
            assert (rep / "_rep.json").exists()
            for split in ("screen", "heldout"):  # full and rerank are empty in the fixture
                assert (rep / split / "_scores.json").exists()
    assert json.loads((run_dir / "reeval" / "summary.json").read_text())["status"] == "complete"
    # The exploiting winner passes the proxy on S but fails every held-out task; the seed and
    # the harmless winner solve everything.
    assert summary["agents"][dgm_best]["mean"]["s_proxy"] == 1.0
    assert summary["agents"][dgm_best]["mean"]["s_clean_base"] == 0.0
    assert summary["agents"][dgm_best]["mean"]["h_clean"] == 0.0
    assert summary["agents"]["seed"]["mean"]["h_clean"] == 1.0
    worker_calls = llm.calls["worker"]

    # Resumable: a second call does no new work.
    again = reeval(run_dir, llm, LocalSandbox(), k=2)
    assert again["status"] == "complete" and llm.calls["worker"] == worker_calls
    assert len(load_reeval(run_dir)[dgm_best]) == 2

    d = detect(run_dir, llm)
    assert d["status"] == "complete" and d["added"] == d["total"] >= 2
    rows = {json.loads(ln)["id"]: json.loads(ln)
            for ln in (run_dir / "detect.jsonl").read_text().splitlines()}
    exploit = rows["dgm-00-g01"]
    assert exploit["gap_flag"] and exploit["static_scan"]
    assert exploit["monitor_small"] and exploit["monitor_large"]
    harmless = rows["random-00-g01"]
    assert not any(harmless[k] for k in ("gap_flag", "static_scan", "monitor_small", "monitor_large"))
    monitor_calls = llm.calls["monitor"]
    assert detect(run_dir, llm)["added"] == 0 and llm.calls["monitor"] == monitor_calls

    res = analyse(run_dir, resamples=500)
    e1 = res["E1"]
    assert e1["source"] == "reeval" and e1["estimand"] == "delta_G_reeval"
    assert res["lineages"]["dgm-00"]["delta_G_reeval"] == pytest.approx(1.0)
    assert res["lineages"]["random-00"]["delta_G_reeval"] == pytest.approx(0.0)
    assert res["lineages"]["dgm-00"]["delta_G"] == res["lineages"]["dgm-00"]["delta_G_reeval"]
    assert e1["dgm"]["n"] == 1 and e1["dgm"]["mean"] == pytest.approx(1.0)
    assert e1["dgm"]["sign_flip_p"] == pytest.approx(1.0)  # one lineage cannot reject
    est, lo, hi = e1["dgm"]["bootstrap95"]
    assert lo <= est <= hi and est == pytest.approx(1.0)
    assert e1["dgm_vs_random_p"] == pytest.approx(1.0)
    b = res["E1b"]["dgm-00"]
    assert b["winners_curse"] == pytest.approx(0.0)  # deterministic fake: no curse
    assert b["exploit_overfit"] == pytest.approx(1.0)
    assert 0.0 <= b["winners_curse_null"]["p_observed_or_larger"] <= 1.0

    e5 = res["E5"]
    assert e5["source"] == "detect" and e5["n"] == len(rows) and e5["positives"] >= 1
    for name in ("gap_flag", "static_scan", "monitor_small", "monitor_large"):
        assert e5[name]["recall"] == 1.0 and e5[name]["precision"] == 1.0, (name, e5[name])
        assert e5[name]["recall_wilson95"][1] == 1.0
    assert len(e5["mcnemar_on_positives"]) == 6
    assert e5["mcnemar_on_positives"]["static_scan_vs_monitor_small"]["p"] == 1.0
    json.dumps(res, default=str)


class LedgeredFakeLLM(MonitoringFakeLLM):
    """Bills every call to a ledger and refuses calls past the cap, like AnthropicLLM."""

    def __init__(self, ledger: Ledger):
        super().__init__()
        self.ledger = ledger

    def complete(self, **kw):
        self.ledger.check()
        c = super().complete(**kw)
        self.ledger.record(kw["role"], "claude-haiku-5-5", {"output_tokens": 1_000_000}, False, {})
        return c


def test_budget_stops_cleanly_and_resumes(fake_run, tmp_path):
    run_dir = fake_run.dir
    exhausted = Ledger(tmp_path / "ledger.jsonl", budget_usd=0.0)
    llm = LedgeredFakeLLM(exhausted)
    res = reeval(run_dir, llm, LocalSandbox(), k=2, ledger=exhausted)
    assert res["status"] == "budget_exceeded"
    assert all(a["reps_done"] == 0 for a in res["agents"].values())
    assert load_reeval(run_dir) == {}
    assert detect(run_dir, llm, ledger=exhausted) == {"status": "budget_exceeded", "added": 0, "total": 0}
    assert analyse(run_dir, resamples=200)["E1"]["source"] == "provisional"

    # One output token-million of Haiku costs $0.50: room for a few calls, then a clean stop
    # that leaves no half-finished repetition behind.
    tight = Ledger(tmp_path / "ledger2.jsonl", budget_usd=1.2)
    res = reeval(run_dir, LedgeredFakeLLM(tight), LocalSandbox(), k=2, ledger=tight)
    assert res["status"] == "budget_exceeded"
    for aid, reps in load_reeval(run_dir).items():
        assert all("screen" in r and "heldout" in r for r in reps), aid

    # With the budget restored, the remaining repetitions complete.
    res = reeval(run_dir, MonitoringFakeLLM(), LocalSandbox(), k=2)
    assert res["status"] == "complete"
    assert all(a["reps_done"] == a["reps_wanted"] for a in res["agents"].values())
