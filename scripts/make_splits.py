"""Build data/tasks.jsonl and data/splits.json from EvalPlus MBPP+.

    pip install -e '.[data]'
    python scripts/make_splits.py --seed 20261111

Expected outputs are computed by running each task's canonical solution on its base
and plus inputs. Tasks whose canonical outputs cannot be normalised to JSON, or that
raise, time out or exceed MAX_TASK_BYTES, are dropped and listed in data/dropped.json.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, TimeoutError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autopoiesis.config import Splits  # noqa: E402
from autopoiesis.harness._norm import encode, norm  # noqa: E402
from autopoiesis.tasks import make_splits  # noqa: E402

N_VISIBLE = 1  # MBPP+ prompts show exactly one assert
MAX_TASK_BYTES = 2_000_000  # a few tasks have explosive outputs (Mbpp/255: 1.3 GB)


def _expected(task: dict) -> dict:
    ns: dict = {}
    exec(task["canonical_solution"], ns)
    fn = ns[task["entry_point"]]

    def run(inputs):
        out = []
        for args in inputs:
            v = norm(fn(*copy.deepcopy(args)))
            if isinstance(v, dict) and any(k.startswith("__") for k in v):
                raise TypeError(f"non-JSON output {v}")
            json.dumps(v)
            out.append([encode(args), v])
        return out

    return {"base": run(task["base_input"]), "plus": run(task["plus_input"])}


def main() -> None:
    from evalplus.data import get_mbpp_plus

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--out", default=str(ROOT / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    raw = get_mbpp_plus()
    rows, dropped = [], {}
    # Two passes in fresh interpreters (different hash seeds): drop non-deterministic tasks,
    # e.g. canonical solutions that return list(set(...)).
    passes = []
    for _ in range(2):
        with ProcessPoolExecutor(mp_context=mp.get_context("spawn")) as pool:
            futs = {tid: pool.submit(_expected, t) for tid, t in raw.items()}
            res = {}
            for tid, fut in futs.items():
                try:
                    res[tid] = fut.result(timeout=120)
                except Exception as e:  # noqa: BLE001
                    res[tid] = e
            passes.append(res)
    for tid in sorted(raw):
        exp, again = passes[0][tid], passes[1][tid]
        if isinstance(exp, TimeoutError):
            dropped[tid] = "timeout"
            continue
        if isinstance(exp, Exception):
            dropped[tid] = f"{type(exp).__name__}: {str(exp)[:120]}"
            continue
        if exp != again:
            dropped[tid] = "non-deterministic canonical output"
            continue
        if len(exp["base"]) <= N_VISIBLE:
            dropped[tid] = "no hidden base case"
            continue
        t = raw[tid]
        size = len(json.dumps(exp))
        if size > MAX_TASK_BYTES:
            dropped[tid] = f"outputs too large ({size} bytes)"
            continue
        rows.append({"id": tid, "prompt": t["prompt"].strip().strip('"').strip(),
                     "entry_point": t["entry_point"], "visible": exp["base"][:N_VISIBLE],
                     "base": exp["base"], "plus": exp["plus"]})


    with (out / "tasks.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    s = Splits()
    sizes = {"screen": s.screen, "full": s.full, "heldout": s.heldout, "decoy": s.decoy}
    sizes["rerank"] = len(rows) - sum(sizes.values())
    if sizes["rerank"] < 0:
        raise SystemExit(f"only {len(rows)} usable tasks; shrink the splits")
    def informative(v) -> bool:
        return not (isinstance(v, bool) or v is None or (isinstance(v, (list, dict, str)) and not v))

    eligible = {r["id"] for r in rows if informative(r["base"][-1][1])}
    splits = make_splits([r["id"] for r in rows], sizes, s.canaries, args.seed, eligible)
    (out / "splits.json").write_text(json.dumps(splits, indent=1))
    (out / "dropped.json").write_text(json.dumps(dropped, indent=1))
    print(json.dumps({"usable": len(rows), "dropped": len(dropped),
                      "sizes": {k: len(v) for k, v in splits.items() if isinstance(v, list)}}))


if __name__ == "__main__":
    main()
