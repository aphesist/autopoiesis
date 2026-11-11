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


def sign_flip_test(values: Sequence[float]) -> float:
    """Exact two-sided sign-flip (one-sample permutation) test of mean == 0.

    With n lineages the smallest attainable p is 2 / 2**n (n=6 -> 0.031).
    """
    x = np.asarray(values, dtype=float)
    n = len(x)
    if n == 0:
        return 1.0
    if n > 20:
        raise ValueError("use a Monte Carlo version for n > 20")
    obs = abs(x.mean())
    signs = np.array(list(itertools.product([1, -1], repeat=n)))
    means = np.abs((signs * x).mean(axis=1))
    return float(np.mean(means >= obs - 1e-12))


def permutation_test(a: Sequence[float], b: Sequence[float]) -> float:
    """Exact two-sided test of mean(a) == mean(b) over all relabellings (6+6 -> 924)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    pooled = np.concatenate([a, b])
    n, na = len(pooled), len(a)
    obs = abs(a.mean() - b.mean())
    total = pooled.sum()
    hits = count = 0
    for idx in itertools.combinations(range(n), na):
        sa = pooled[list(idx)].sum()
        d = abs(sa / na - (total - sa) / (n - na))
        hits += d >= obs - 1e-12
        count += 1
    return hits / count


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
