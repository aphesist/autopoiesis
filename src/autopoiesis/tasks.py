"""Task records and splits.

A task is a programming problem with three test sets:
  visible - shown to the agent (MBPP's prompt assert; LCB's public tests)
  base    - the weak tests scored by the flawed selection evaluator (MBPP's original
            tests; LCB's public tests plus K hidden private tests)
  plus    - the rest of the strong tests, scored only by the clean evaluator together
            with base (EvalPlus's plus tests; LCB's remaining private tests)

Two kinds:
  func   cases are [args, expected], expected stored JSON-normalised (tuples become
         lists). MBPP+ calls the function `entry_point`; LCB LeetCode tasks (starter code
         with `class Solution`) call Solution().<entry_point>(*args).
  stdio  cases are [input, expected output], both strings; the solution is a program
         (LCB AtCoder and Codeforces tasks).
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
    kind: str = "func"  # "func" | "stdio"
    platform: str = ""  # LCB: leetcode, atcoder, codeforces; "" for MBPP+
    difficulty: str = ""
    contest_date: str = ""
    starter_code: str = ""

    @property
    def cls(self) -> str | None:
        """The class whose method is the entry point (LCB LeetCode tasks), else None."""
        return "Solution" if self.kind == "func" and "class Solution" in self.starter_code else None

    @property
    def lcb(self) -> bool:
        return bool(self.platform)

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(d["id"], d["prompt"], d["entry_point"], d["visible"], d["base"], d["plus"],
                    d.get("kind", "func"), d.get("platform", ""), d.get("difficulty", ""),
                    d.get("contest_date", ""), d.get("starter_code", ""))


def load_tasks(path: str | Path) -> dict[str, Task]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            t = Task.from_dict(json.loads(line))
            out[t.id] = t
    return out


def make_splits(task_ids: list[str], sizes: dict[str, int], n_canaries: int, seed: int,
                canary_eligible: set[str] | None = None) -> dict:
    """Deterministic split. Canaries are drawn from the selection problems.

    Canaries are stratified: at least half of them (rounded up) come from S_screen, the
    split every child is scored on, so a key-reading agent meets canaries even if it never
    reaches S_full. The rest come from S_full (topped up from S_screen if S_full is short).
    canary_eligible, if given, restricts canaries to tasks whose canary case has an
    informative expected value (not a bool, None or empty container): an honest answer that
    is wrong only on that case must not be able to equal the canary by chance.
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
    return _add_canaries(out, n_canaries, seed, canary_eligible)


def make_stratified_splits(strata: dict[str, str], sizes: dict[str, int], n_canaries: int,
                           seed: int, canary_eligible: set[str] | None = None) -> dict:
    """Deterministic split stratified by `strata` (task id -> stratum label).

    Splits are filled in SPLIT_NAMES order. Each split's size is shared among the strata in
    proportion to the tasks each stratum still has (largest remainders), and its members
    are the next tasks of each stratum's seeded shuffle, so every split has close to the
    pool's mix. Canaries are drawn as in make_splits.
    """
    groups: dict[str, list[str]] = {}
    for tid in sorted(strata):
        groups.setdefault(strata[tid], []).append(tid)
    for label in sorted(groups):
        random.Random(f"{seed}|{label}").shuffle(groups[label])
    total = sum(sizes.get(n, 0) for n in SPLIT_NAMES)
    if total > len(strata):
        raise ValueError(f"need {total} tasks, have {len(strata)}")
    pos = {g: 0 for g in groups}
    out = {}
    for name in SPLIT_NAMES:
        n = sizes.get(name, 0)
        left = {g: len(groups[g]) - pos[g] for g in groups}
        remaining = sum(left.values())
        quota = {g: n * left[g] / remaining if remaining else 0 for g in groups}
        take = {g: int(quota[g]) for g in groups}
        for g in sorted(groups, key=lambda g: (-(quota[g] - take[g]), g)):
            if sum(take.values()) >= n:
                break
            if take[g] < left[g]:
                take[g] += 1
        members = []
        for g in sorted(groups):
            members += groups[g][pos[g]:pos[g] + take[g]]
            pos[g] += take[g]
        out[name] = sorted(members)
    return _add_canaries(out, n_canaries, seed, canary_eligible)


def _add_canaries(out: dict, n_canaries: int, seed: int, canary_eligible: set[str] | None) -> dict:
    rng = random.Random(seed + 1)
    ok = (lambda t: t in canary_eligible) if canary_eligible is not None else (lambda t: True)
    screen = [t for t in out["screen"] if ok(t)]
    full = [t for t in out["full"] if ok(t)]
    n_total = min(n_canaries, len(screen) + len(full))
    n_screen = min(len(screen), max((n_total + 1) // 2, n_total - len(full)))
    picked = rng.sample(screen, n_screen)
    picked += rng.sample(full, n_total - n_screen)
    out["canary"] = sorted(picked)
    out["seed"] = seed
    return out


def load_splits(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
