"""Parent selection.

dgm:    the DGM rule, weight = sigmoid(10 * (score - 0.5)) / (1 + n_children)
random: uniform over admitted agents (the control arm; selection pressure removed,
        mutation and evaluation unchanged)
"""

from __future__ import annotations

import math
import random

from .archive import Archive, AgentRecord


def dgm_weight(score: float, n_children: int, k: float = 10.0, mid: float = 0.5) -> float:
    return 1.0 / (1.0 + math.exp(-k * (score - mid))) / (1.0 + n_children)


def select_parent(archive: Archive, arm: str, rng: random.Random) -> AgentRecord:
    pool = archive.admitted()
    if not pool:
        raise ValueError("empty archive")
    if arm == "random":
        return rng.choice(pool)
    if arm == "dgm":
        weights = [dgm_weight(r.score, archive.children_count(r.id)) for r in pool]
        return rng.choices(pool, weights=weights, k=1)[0]
    raise ValueError(f"unknown arm {arm!r}")
