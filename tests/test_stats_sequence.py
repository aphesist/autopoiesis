import pytest

from autopoiesis import stats


def test_one_sided_minimum_p_for_six_lineages():
    assert stats.sign_flip_test([1, 2, 3, 4, 5, 6], alternative="greater") == pytest.approx(1 / 64)
    assert stats.sign_flip_test([-1, -2, -3, -4, -5, -6], alternative="greater") == pytest.approx(1.0)


def test_one_sided_permutation():
    assert stats.permutation_test([1] * 6, [0] * 6, alternative="greater") == pytest.approx(1 / 924)
    assert stats.permutation_test([0] * 6, [1] * 6, alternative="greater") == pytest.approx(1.0)


def test_fixed_sequence_stops_at_first_failure():
    assert stats.fixed_sequence([0.01, 0.2, 0.001]) == [True, False, False]
    assert stats.fixed_sequence([0.016, 0.03]) == [True, True]
