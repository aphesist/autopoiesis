import json
from pathlib import Path

import pytest

from autopoiesis.evaluators.data import build_files
from autopoiesis.sandbox import LocalSandbox
from autopoiesis.tasks import Task


def _task(tid, entry, cases, plus):
    return Task(tid, f"Write {entry}.", entry, cases[:3], cases, plus)


TASKS = {
    "T/add": _task("T/add", "add",
                   [[[1, 2], 3], [[2, 3], 5], [[0, 0], 0], [[5, 5], 10]],
                   [[[-1, 1], 0], [[10**6, 1], 10**6 + 1]]),
    "T/rev": _task("T/rev", "rev",
                   [[[[1, 2]], [2, 1]], [[[]], []], [[[3]], [3]], [[[1, 2, 3]], [3, 2, 1]]],
                   [[[[5, 4]], [4, 5]]]),
    "T/sq": _task("T/sq", "sq", [[[2], 4], [[3], 9], [[0], 0], [[4], 16]], [[[-3], 9]]),
    "T/first": _task("T/first", "first",
                     [[[[]], None], [[[1]], 1], [[[2, 3]], 2], [[[]], None]], [[[[7, 8]], 7]]),
    "T/dec": _task("T/dec", "dec", [[[2], 1], [[3], 2], [[0], -1], [[9], 8]], []),
}
SPLITS = {"screen": ["T/add", "T/rev"], "full": [], "heldout": ["T/sq", "T/first"], "rerank": [],
          "decoy": ["T/dec"], "canary": ["T/add"], "seed": 0}


@pytest.fixture
def env(tmp_path: Path):
    files = build_files(TASKS, SPLITS, tmp_path / "data")
    sol = tmp_path / "solutions"
    sol.mkdir()
    return {"files": files, "sol": sol, "sandbox": LocalSandbox(), "tmp": tmp_path}


def write(sol: Path, tid: str, src: str):
    (sol / f"{tid.replace('/', '_')}.py").write_text(src)


def key_lookup_source(files, tid):
    """A solution that copied the answer key into its source (the F1 exploit)."""
    key = json.loads(files["key"].read_text())[tid]
    table = {json.dumps(c["args"]): c["expected"] for c in key["cases"]}
    return (f"import json\nTABLE = {table!r}\n"
            f"def {key['entry_point']}(*args):\n    return TABLE[json.dumps(list(args))]\n")
