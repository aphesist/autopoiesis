"""Task records and splits.

A task is a function-synthesis problem with three test sets:
  visible - shown to the agent (MBPP's 3 prompt asserts)
  base    - the original weak tests; scored by the flawed selection evaluator
  plus    - EvalPlus strong tests; scored only by the clean evaluator
Expected values are stored JSON-normalised (tuples become lists).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

SPLIT_NAMES = ("screen", "full", "heldout", "rerank", "decoy")


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    entry_point: str
    visible: list  # [[args, expected], ...]
    base: list
    plus: list

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(d["id"], d["prompt"], d["entry_point"], d["visible"], d["base"], d["plus"])


def load_tasks(path: str | Path) -> dict[str, Task]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            t = Task.from_dict(json.loads(line))
            out[t.id] = t
    return out


def make_splits(task_ids: list[str], sizes: dict[str, int], n_canaries: int, seed: int) -> dict:
    """Deterministic split. Canaries are drawn from the selection problems.

    Canaries are stratified: at least half of them (rounded up) come from S_screen, the
    split every child is scored on, so a key-reading agent meets canaries even if it never
    reaches S_full. The rest come from S_full (topped up from S_screen if S_full is short).
    """
    ids = sorted(task_ids)
    random.Random(seed).shuffle(ids)
    out, i = {}, 0
    for name in SPLIT_NAMES:
        n = sizes.get(name, 0)
        out[name] = ids[i:i + n]
        i += n
    if i > len(ids):
        raise ValueError(f"need {i} tasks, have {len(ids)}")
    rng = random.Random(seed + 1)
    screen, full = out["screen"], out["full"]
    n_total = min(n_canaries, len(screen) + len(full))
    n_screen = min(len(screen), max((n_total + 1) // 2, n_total - len(full)))
    picked = rng.sample(screen, n_screen)
    picked += rng.sample(full, n_total - n_screen)
    out["canary"] = sorted(picked)
    out["seed"] = seed
    return out


def load_splits(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
