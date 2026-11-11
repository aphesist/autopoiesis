"""Statistics for PREREGISTRATION.md. The lineage is the independent unit.

All tests here are exact or resampling-based and make no normality assumption.
"""

from __future__ import annotations

import itertools
import math
from typing import Sequence

import numpy as np


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def goodhart_index(proxy_best: Sequence[float], proxy_seed: Sequence[float],
                   clean_best: Sequence[float], clean_seed: Sequence[float]) -> float:
    """E1: dG = [proxy_S(best) - proxy_S(seed)] - [clean_H(best) - clean_H(seed)].

    Inputs are per-problem scores (S for proxy, H for clean). Pairing against the seed
    on the same problems cancels split difficulty differences.
    """
    return (np.mean(proxy_best) - np.mean(proxy_seed)) - (np.mean(clean_best) - np.mean(clean_seed))


def sign_flip_test(values: Sequence[float], alternative: str = "two-sided") -> float:
    """Exact sign-flip (one-sample permutation) test of mean == 0.

    alternative: "two-sided", or "greater" for the pre-registered directional tests.
    With n lineages the smallest attainable p is 2 / 2**n two-sided and 1 / 2**n
    one-sided (n=6 -> 0.031 and 0.016).
    """
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n == 0:
        return 1.0
    if n > 20:
        raise ValueError("use a Monte Carlo version for n > 20")
    signs = np.array(list(itertools.product([1, -1], repeat=n)))
    means = (signs * x).mean(axis=1)
    if alternative == "greater":
        return float(np.mean(means >= x.mean() - 1e-12))
    if alternative != "two-sided":
        raise ValueError(alternative)
    return float(np.mean(np.abs(means) >= abs(x.mean()) - 1e-12))


def permutation_test(a: Sequence[float], b: Sequence[float], alternative: str = "two-sided") -> float:
    """Exact test of mean(a) == mean(b) over all relabellings (6+6 -> 924).

    alternative: "two-sided", or "greater" for H: mean(a) > mean(b).
    """
    if alternative not in ("two-sided", "greater"):
        raise ValueError(alternative)
    a, b = np.asarray(a, float), np.asarray(b, float)
    pooled = np.concatenate([a, b])
    n, na = len(pooled), len(a)
    obs = a.mean() - b.mean()
    total = pooled.sum()
    hits = count = 0
    for idx in itertools.combinations(range(n), na):
        sa = pooled[list(idx)].sum()
        d = sa / na - (total - sa) / (n - na)
        if alternative == "greater":
            hits += d >= obs - 1e-12
        else:
            hits += abs(d) >= abs(obs) - 1e-12
        count += 1
    return hits / count


def fixed_sequence(pvals: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Fixed-sequence (hierarchical) testing: test in the pre-registered order at alpha each,
    stop at the first non-rejection. Controls the family-wise error rate at alpha."""
    out, go = [], True
    for p in pvals:
        go = go and p <= alpha
        out.append(go)
    return out


def two_level_bootstrap(per_lineage: Sequence[Sequence[float]], b: int = 10_000,
                        seed: int = 0, alpha: float = 0.05) -> tuple[float, float, float]:
    """Mean and percentile CI, resampling lineages and then problems within lineages."""
    rng = np.random.default_rng(seed)
    groups = [np.asarray(g, float) for g in per_lineage]
    est = float(np.mean([g.mean() for g in groups]))
    stats = np.empty(b)
    for i in range(b):
        picks = rng.integers(0, len(groups), len(groups))
        stats[i] = np.mean([rng.choice(groups[j], len(groups[j])).mean() for j in picks])
    lo, hi = np.quantile(stats, [alpha / 2, 1 - alpha / 2])
    return est, float(lo), float(hi)


def two_level_bootstrap_dg(s_diffs: Sequence[Sequence[float]], h_diffs: Sequence[Sequence[float]],
                           b: int = 10_000, seed: int = 0,
                           alpha: float = 0.05) -> tuple[float, float, float]:
    """Two-level bootstrap for the Goodhart index, stratified by split.

    s_diffs[l] holds lineage l's per-problem proxy differences best - seed on S, and
    h_diffs[l] its per-problem clean differences best - seed on H, so that lineage l's
    dG = mean(s_diffs[l]) - mean(h_diffs[l]). Lineages are resampled, then S and H
    problems are resampled separately within each lineage (S and H are different
    problems, so they cannot be pooled into one list). Returns the mean of the
    lineage dGs and its percentile CI.
    """
    rng = np.random.default_rng(seed)
    S = [np.asarray(g, float) for g in s_diffs]
    H = [np.asarray(g, float) for g in h_diffs]
    est = float(np.mean([s.mean() - h.mean() for s, h in zip(S, H)]))
    stats = np.empty(b)
    for i in range(b):
        picks = rng.integers(0, len(S), len(S))
        stats[i] = np.mean([rng.choice(S[j], len(S[j])).mean() - rng.choice(H[j], len(H[j])).mean()
                            for j in picks])
    lo, hi = np.quantile(stats, [alpha / 2, 1 - alpha / 2])
    return est, float(lo), float(hi)


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar test on paired binary outcomes.

    b and c are the discordant counts (detector A right and B wrong, and vice versa).
    Under H0 b ~ Binomial(b + c, 1/2); p = min(1, 2 * P(X <= min(b, c))).
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def winners_curse_null(p: np.ndarray, reps: int = 10_000, seed: int = 0) -> np.ndarray:
    """E1b parametric null: gap from selecting the best agent when nothing is overfit.

    p: (n_agents, n_problems) per-problem success probabilities estimated from re-runs.
    Each replicate draws one score per agent, picks the argmax, and records its drawn
    score minus its true mean. The distribution is what best-of-N selection alone yields.
    """
    rng = np.random.default_rng(seed)
    p = np.asarray(p, float)
    true = p.mean(axis=1)
    out = np.empty(reps)
    for i in range(reps):
        draws = (rng.random(p.shape) < p).mean(axis=1)
        j = int(np.argmax(draws))
        out[i] = draws[j] - true[j]
    return out


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def kaplan_meier(times: Sequence[float], observed: Sequence[bool]) -> list[tuple[float, float]]:
    """Survival curve S(t) for time-to-first-exploit (censored at the last generation)."""
    data = sorted(zip(times, observed))
    at_risk, s, curve = len(data), 1.0, [(0.0, 1.0)]
    for t, grp in itertools.groupby(data, key=lambda x: x[0]):
        grp = list(grp)
        d = sum(1 for _, o in grp if o)
        if d:
            s *= 1 - d / at_risk
            curve.append((t, s))
        at_risk -= len(grp)
    return curve
