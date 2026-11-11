import random

import numpy as np
import pytest

from autopoiesis import stats
from autopoiesis.archive import AgentRecord, Archive
from autopoiesis.selection import dgm_weight, select_parent


def test_wilson_known_values():
    lo, hi = stats.wilson(6, 12)
    assert lo == pytest.approx(0.2538, abs=1e-3) and hi == pytest.approx(0.7462, abs=1e-3)
    lo, hi = stats.wilson(0, 12)
    assert lo == 0.0 and hi == pytest.approx(0.2425, abs=1e-3)


def test_sign_flip_minimum_p_for_six_lineages():
    assert stats.sign_flip_test([1, 2, 3, 4, 5, 6]) == pytest.approx(2 / 64)
    assert stats.sign_flip_test([1, -1, 1, -1, 1, -1]) == pytest.approx(1.0)


def test_permutation_test_extremes():
    assert stats.permutation_test([1] * 6, [0] * 6) == pytest.approx(2 / 924)
    assert stats.permutation_test([0, 1] * 3, [1, 0] * 3) == pytest.approx(1.0)


def test_goodhart_index_cancels_split_difficulty():
    # Agent improves equally on S and H: no Goodhart gap even though H is harder.
    g = stats.goodhart_index([0.8] * 10, [0.6] * 10, [0.5] * 10, [0.3] * 10)
    assert g == pytest.approx(0.0)


def test_winners_curse_is_positive_when_nothing_is_overfit():
    p = np.full((40, 100), 0.6)  # 40 identical agents
    gaps = stats.winners_curse_null(p, reps=500, seed=1)
    assert gaps.mean() > 0.05


def test_holm_monotone():
    adj = stats.holm([0.01, 0.04, 0.03])
    assert adj == pytest.approx([0.03, 0.06, 0.06])


def test_kaplan_meier_simple():
    curve = stats.kaplan_meier([2, 4, 20, 20], [True, True, False, False])
    assert curve == [(0.0, 1.0), (2, 0.75), (4, 0.5)]


def test_two_level_bootstrap_contains_mean():
    est, lo, hi = stats.two_level_bootstrap([[0, 1, 1], [1, 1, 1], [0, 0, 1]], b=2000)
    assert lo <= est <= hi


def test_dgm_weight_prefers_good_and_unexplored():
    assert dgm_weight(0.8, 0) > dgm_weight(0.4, 0)
    assert dgm_weight(0.8, 0) > dgm_weight(0.8, 3)


def test_select_parent_arms(tmp_path):
    arc = Archive(tmp_path / "a.jsonl")
    arc.put(AgentRecord("good", None, "l", "dgm", 0, "x", screen_proxy=0.9))
    arc.put(AgentRecord("bad", None, "l", "dgm", 0, "x", screen_proxy=0.1))
    arc.put(AgentRecord("dead", None, "l", "dgm", 0, "x", screen_proxy=1.0, admitted=False))
    rng = random.Random(0)
    picks = [select_parent(arc, "dgm", rng).id for _ in range(500)]
    assert picks.count("good") > 450 and "dead" not in picks
    picks = [select_parent(arc, "random", rng).id for _ in range(500)]
    assert 180 < picks.count("bad") < 320


def test_archive_roundtrip(tmp_path):
    arc = Archive(tmp_path / "a.jsonl")
    r = AgentRecord("c1", "p", "l", "dgm", 1, "x", screen_proxy=0.5)
    arc.put(r)
    r.full_proxy = 0.7
    arc.put(r)
    again = Archive(tmp_path / "a.jsonl")
    assert again.records["c1"].score == 0.7


def test_mcnemar_exact_known_values():
    # b=1, c=9: n=10, P(X<=1) = 11/1024, two-sided p = 22/1024.
    assert stats.mcnemar_exact(1, 9) == pytest.approx(22 / 1024)
    assert stats.mcnemar_exact(9, 1) == pytest.approx(22 / 1024)
    assert stats.mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert stats.mcnemar_exact(3, 3) == 1.0 and stats.mcnemar_exact(0, 0) == 1.0


def test_two_level_bootstrap_dg_stratified():
    s = [[1.0, 0.0, 1.0, 1.0], [0.5, 0.5, 0.5, 0.5]]
    h = [[0.0, 0.0], [0.5, 0.5]]
    est, lo, hi = stats.two_level_bootstrap_dg(s, h, b=2000)
    assert est == pytest.approx(((0.75 - 0.0) + (0.5 - 0.5)) / 2)
    assert lo <= est <= hi
    est, lo, hi = stats.two_level_bootstrap_dg([[0.3] * 5] * 3, [[0.1] * 4] * 3, b=200)
    assert est == lo == hi == pytest.approx(0.2)
