"""Build data/lcb/tasks.jsonl, data/lcb/splits.json and data/lcb/dropped.json from
LiveCodeBench release_v6 (HF dataset livecodebench/code_generation_lite, test.jsonl to
test6.jsonl: 1055 problems, May 2023 to April 2025).

    python scripts/make_splits_lcb.py --config configs/pilot-lcb.json [--raw-dir DIR]

Without --raw-dir the six files (about 4.5 GB) are downloaded to data/lcb/raw/
(git-ignored). The dataset is treated as data only: private tests are decoded as LCB's
official loader does (JSON, else base64 -> zlib -> pickle -> JSON), with an unpickler
that refuses every global, so the pickle can yield nothing but plain values.

Each kept problem becomes a Task (src/autopoiesis/tasks.py):
  visible  the public tests (shown to the agent)
  base     the public tests plus K private tests chosen per problem with the seed; the
           flawed selection evaluator scores these, and the K hidden ones carry canaries
  plus     the remaining private tests; clean = base + plus = every test
Kinds: "func" for LeetCode (functional tests; Solution().<func_name>(*args), each input
line parsed as JSON as LCB's harness does) and "stdio" for AtCoder and Codeforces.

Problems are dropped, with the reason recorded, if their difficulty is not selected, they
have fewer than K + 1 private tests, their tests total more than MAX_TASK_BYTES, or their
functional tests do not parse as JSON. Split sizes come from the config's "splits" block
(B, the re-rank split, takes the rest); every split is stratified by kind and difficulty.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import pickle
import random
import sys
import urllib.request
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autopoiesis.config import Config  # noqa: E402
from autopoiesis.evaluators.data import canary_case, informative  # noqa: E402
from autopoiesis.harness._norm import encode  # noqa: E402
from autopoiesis.tasks import SPLIT_NAMES, Task, make_stratified_splits  # noqa: E402

RELEASE = "release_v6"
FILES = ["test.jsonl", "test2.jsonl", "test3.jsonl", "test4.jsonl", "test5.jsonl", "test6.jsonl"]
URL = "https://huggingface.co/datasets/livecodebench/code_generation_lite/resolve/main/"
K_PRIVATE_WEAK = 3  # hidden private tests in the weak (selection) set
MAX_TASK_BYTES = 2_000_000  # inputs + outputs of all tests


class _PlainUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"refusing global {module}.{name}")


def decode_private(raw: str) -> list:
    """LCB's loader: json.loads, else json.loads(pickle.loads(zlib.decompress(b64decode)))."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        blob = zlib.decompress(base64.b64decode(raw.encode("utf-8")))
        return json.loads(_PlainUnpickler(io.BytesIO(blob)).load())


def ensure_raw(raw_dir: Path) -> list[Path]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in FILES:
        p = raw_dir / name
        if not p.exists():
            print(f"downloading {name} ...", file=sys.stderr)
            tmp = p.with_suffix(".part")
            urllib.request.urlretrieve(URL + name, tmp)
            tmp.rename(p)
        paths.append(p)
    return paths


def _func_case(t: dict):
    args = [json.loads(line) for line in t["input"].split("\n")]
    return [encode(args), json.loads(t["output"])]


def build_task(row: dict, seed: int) -> Task | str:
    """A Task, or the reason the problem is dropped."""
    public = json.loads(row["public_test_cases"])
    private = decode_private(row["private_test_cases"])
    tests = public + private
    types = {t["testtype"] for t in tests}
    if len(private) < K_PRIVATE_WEAK + 1:
        return f"only {len(private)} private tests (need {K_PRIVATE_WEAK + 1})"
    size = sum(len(t["input"]) + len(t["output"]) for t in tests)
    if size > MAX_TASK_BYTES:
        return f"tests too large ({size} bytes)"
    if types == {"stdin"}:
        kind, entry = "stdio", ""
        case = lambda t: [t["input"], t["output"]]  # noqa: E731
    elif types == {"functional"}:
        kind = "func"
        entry = json.loads(row["metadata"] or "{}").get("func_name", "")
        if not entry or "class Solution" not in row["starter_code"]:
            return "functional task without class Solution or func_name"
        case = _func_case
    else:
        return f"mixed test types {sorted(types)}"
    try:
        pub = [case(t) for t in public]
        priv = [case(t) for t in private]
    except (json.JSONDecodeError, TypeError) as e:
        return f"test does not parse: {type(e).__name__}"
    pick = sorted(random.Random(f"{seed}|{row['question_id']}").sample(range(len(priv)),
                                                                        K_PRIVATE_WEAK))
    weak = [priv[i] for i in pick]
    rest = [c for i, c in enumerate(priv) if i not in set(pick)]
    return Task(f"lcb/{row['question_id']}", row["question_content"].strip(), entry,
                pub, pub + weak, rest, kind, row["platform"], row["difficulty"],
                row["contest_date"], row["starter_code"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "configs" / "pilot-lcb.json"))
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--difficulties", default="medium,hard")
    ap.add_argument("--raw-dir", help="directory holding test.jsonl ... test6.jsonl")
    ap.add_argument("--out", default=str(ROOT / "data" / "lcb"))
    args = ap.parse_args()
    cfg = Config.load(args.config)
    keep = sorted(d.strip() for d in args.difficulties.split(",") if d.strip())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir) if args.raw_dir else out / "raw"
    paths = [raw_dir / n for n in FILES] if args.raw_dir else ensure_raw(raw_dir)

    tasks: dict[str, Task] = {}
    dropped: dict[str, str] = {}
    n_raw = 0
    for p in paths:
        with p.open() as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                n_raw += 1
                tid = f"lcb/{row['question_id']}"
                if tid in tasks or tid in dropped:
                    raise SystemExit(f"duplicate question_id {row['question_id']}")
                if row["difficulty"] not in keep:
                    dropped[tid] = f"difficulty {row['difficulty']} not selected"
                    continue
                t = build_task(row, args.seed)
                if isinstance(t, str):
                    dropped[tid] = t
                else:
                    tasks[tid] = t
    rows = [tasks[t] for t in sorted(tasks)]
    with (out / "tasks.jsonl").open("w") as f:
        for t in rows:
            f.write(json.dumps({"id": t.id, "prompt": t.prompt, "entry_point": t.entry_point,
                                "visible": t.visible, "base": t.base, "plus": t.plus,
                                "kind": t.kind, "platform": t.platform,
                                "difficulty": t.difficulty, "contest_date": t.contest_date,
                                "starter_code": t.starter_code}) + "\n")

    s = cfg.splits
    sizes = {"screen": s.screen, "full": s.full, "heldout": s.heldout, "decoy": s.decoy}
    sizes["rerank"] = len(rows) - sum(sizes.values())
    if sizes["rerank"] < 0:
        raise SystemExit(f"only {len(rows)} usable tasks; shrink the splits")
    strata = {t.id: f"{t.kind}/{t.difficulty}" for t in rows}
    eligible = {t.id for t in rows if informative(t.kind, t.base[canary_case(t)][1])}
    splits = make_stratified_splits(strata, sizes, s.canaries, args.seed, eligible)
    splits.update({"pool": "lcb", "release": RELEASE, "difficulties": keep,
                   "k_private_weak": K_PRIVATE_WEAK, "max_task_bytes": MAX_TASK_BYTES,
                   "stratified_by": ["kind", "difficulty"]})
    (out / "splits.json").write_text(json.dumps(splits, indent=1))
    (out / "dropped.json").write_text(json.dumps(dict(sorted(dropped.items())), indent=1))

    def mix(ids):
        return dict(sorted(Counter(strata[t] for t in ids).items()))

    reasons = Counter(r.split(" (")[0] if r.startswith("tests too large") else
                      ("too few private tests" if r.startswith("only") else r)
                      for r in dropped.values())
    print(json.dumps({"raw": n_raw, "usable": len(rows), "dropped": len(dropped),
                      "drop_reasons": dict(reasons), "usable_by_stratum": mix(tasks),
                      "canary_eligible": len(eligible),
                      "splits": {k: {"n": len(splits[k]), "mix": mix(splits[k])}
                                 for k in (*SPLIT_NAMES, "canary")}}, indent=1))


if __name__ == "__main__":
    main()
