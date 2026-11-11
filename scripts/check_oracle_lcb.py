"""Oracle check of the LCB pool through the real Docker sandbox (no API calls).

LCB has no canonical solutions, so the check uses an oracle: for every S and H task, a
solution that looks up the expected output of each input and prints (stdio) or returns
(LeetCode) it. It runs through both evaluators exactly as an agent's solutions would.

  1. oracle            must score 100% clean on S and H, pass the selection evaluator on
                       every S task, and trip nothing (it reads no answer key, so not even F1)
  2. perturbed oracle  the same, but wrong on the last strong test of every task: must be
                       0% clean while still passing the weak selection tests
  3. runtime           per-case wall time of the oracle in the trusted runner's own way of
                       running a case (fresh process per stdio input; in-process call for
                       LeetCode), as a floor for the per-case timeout

    python scripts/check_oracle_lcb.py [--config configs/pilot-lcb.json] [--out runs/oracle-lcb]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autopoiesis.config import Config  # noqa: E402
from autopoiesis.evaluators.data import build_files, input_file, perturb_case, safe  # noqa: E402
from autopoiesis.evaluators.scoring import HARNESS_DIR, score_solutions, summarise  # noqa: E402
from autopoiesis.harness._norm import decode  # noqa: E402
from autopoiesis.sandbox import DockerSandbox, Mount  # noqa: E402
from autopoiesis.tasks import load_splits, load_tasks  # noqa: E402

STDIO_ORACLE = """import json, sys
TABLE = json.load(open('/solutions/{stem}.table.json'))
sys.stdout.write(TABLE.get(sys.stdin.buffer.read().decode(), ''))
"""
FUNC_ORACLE = """import json
TABLE = json.load(open('/solutions/{stem}.table.json'))
class Solution:
    def {method}(self, *args):
        return TABLE[json.dumps(list(args))]
"""

# Times each case the way run_cases.py runs it. Mounted at /timing.
TIMER = r"""
import json, os, sys, time
sys.path.insert(0, '/harness')
import run_cases
from _lcb import PRELUDE
from _norm import decode
out = {}
for tid in json.load(open('/timing/ids.json')):
    stem = tid.replace('/', '_')
    data = json.load(open(f'/inputs/inputs-{stem}.json'))[tid]
    sol = f'/solutions/{stem}.py'
    times = []
    if data['kind'] == 'stdio':
        for inp in data['inputs']:
            t0 = time.perf_counter()
            run_cases.run_program(sol, inp, 60.0)
            times.append(time.perf_counter() - t0)
    else:
        t0 = time.perf_counter()
        ns = {'__name__': 'solution'}
        exec(PRELUDE, ns)
        exec(open(sol).read(), ns)
        fn = getattr(ns['Solution'](), data['entry_point'])
        setup = time.perf_counter() - t0
        for args in data['inputs']:
            t0 = time.perf_counter()
            fn(*decode(args))
            times.append(time.perf_counter() - t0)
        times[0] += setup
    print(json.dumps({'id': tid, 'times': times}), flush=True)
"""


def write_oracles(tasks, ids, sol: Path, perturb_last: bool) -> None:
    sol.mkdir(parents=True, exist_ok=True)
    for tid in ids:
        t = tasks[tid]
        cases = t.base + t.plus
        stem = safe(tid)
        table = {}
        for j, (inp, exp) in enumerate(cases):
            if perturb_last and j == len(cases) - 1:
                exp = perturb_case(t.kind, exp)[0]
            key = inp if t.kind == "stdio" else json.dumps(decode(inp))
            table[key] = exp
        (sol / f"{stem}.table.json").write_text(json.dumps(table))
        src = (STDIO_ORACLE.format(stem=stem) if t.kind == "stdio"
               else FUNC_ORACLE.format(stem=stem, method=t.entry_point))
        (sol / f"{stem}.py").write_text(src)


def readable(root: Path) -> None:
    os.chmod(root, 0o755)
    for p in root.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)


def report(scores, s_ids, h_ids, tasks) -> dict:
    def by(ids):
        strata = Counter(f"{tasks[t].kind}/{tasks[t].difficulty}" for t in ids)
        clean = Counter(f"{tasks[t].kind}/{tasks[t].difficulty}" for t in ids if scores[t].clean)
        return {k: f"{clean[k]}/{n}" for k, n in sorted(strata.items())}

    s, h = summarise(scores, s_ids), summarise(scores, h_ids)
    return {"S": {**s, "by_stratum_clean": by(s_ids)}, "H": {**h, "by_stratum_clean": by(h_ids)},
            "S_proxy_below_1": sorted(t for t in s_ids if scores[t].proxy < 1.0),
            "any_trips": sorted((t, scores[t].trips) for t in s_ids + h_ids if scores[t].trips),
            "notes": {t: scores[t].note for t in s_ids + h_ids if scores[t].note}}


def quantiles(xs):
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"n": len(xs), "p50": round(q(0.5), 3), "p90": round(q(0.9), 3),
            "p99": round(q(0.99), 3), "max": round(xs[-1], 3), "mean": round(statistics.mean(xs), 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "configs" / "pilot-lcb.json"))
    ap.add_argument("--out", default=str(ROOT / "runs" / "oracle-lcb"))
    ap.add_argument("--skip", default="", help="comma list of steps to skip: oracle,perturbed,timing")
    args = ap.parse_args()
    cfg = Config.load(args.config)
    skip = set(filter(None, args.skip.split(",")))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(ROOT / cfg.data_dir / "tasks.jsonl")
    splits = load_splits(ROOT / cfg.data_dir / "splits.json")
    files = build_files(tasks, splits, out / "data")
    readable(out / "data")
    s_ids = splits["screen"] + splits["full"]
    h_ids = splits["heldout"]
    ids = s_ids + h_ids
    sandbox = DockerSandbox(cfg.docker_image)
    timeout = cfg.caps.test_timeout_s
    result = {"timeout_s": timeout, "n_S": len(s_ids), "n_H": len(h_ids)}

    for step, perturbed in (("oracle", False), ("perturbed", True)):
        if step in skip:
            continue
        sol = out / step
        write_oracles(tasks, ids, sol, perturbed)
        readable(sol)
        t0 = time.time()
        scores = score_solutions(sandbox, sol, ids, files, timeout)
        result[step] = {**report(scores, s_ids, h_ids, tasks), "seconds": round(time.time() - t0)}
        print(json.dumps({step: result[step]}, indent=1), flush=True)

    if "timing" not in skip:
        sol = out / "oracle"
        if not sol.exists():
            write_oracles(tasks, ids, sol, False)
        tdir = out / "timing"
        tdir.mkdir(exist_ok=True)
        (tdir / "timer.py").write_text(TIMER)
        (tdir / "ids.json").write_text(json.dumps(ids))
        readable(sol)
        readable(tdir)
        mounts = [Mount(str(HARNESS_DIR), "/harness"), Mount(str(sol), "/solutions"),
                  Mount(str(files["inputs"].parent), "/inputs"), Mount(str(tdir), "/timing")]
        res = sandbox.run(["python", "/timing/timer.py"], mounts, 7200)
        per = [json.loads(line) for line in res.stdout.splitlines() if line.startswith("{")]
        assert len(per) == len(ids), (len(per), res.stderr[-2000:])
        case_times = {k: [] for k in ("stdio", "func")}
        task_max, task_total = {k: [] for k in case_times}, {k: [] for k in case_times}
        for r in per:
            kind = tasks[r["id"]].kind
            case_times[kind] += r["times"]
            task_max[kind].append(max(r["times"]))
            task_total[kind].append(sum(r["times"]))
        slowest = sorted(per, key=lambda r: -max(r["times"]))[:5]
        result["timing"] = {
            "per_case_s": {k: quantiles(v) for k, v in case_times.items() if v},
            "per_task_max_case_s": {k: quantiles(v) for k, v in task_max.items() if v},
            "per_task_total_s": {k: quantiles(v) for k, v in task_total.items() if v},
            "slowest_tasks": [(r["id"], round(max(r["times"]), 3)) for r in slowest],
        }
        print(json.dumps({"timing": result["timing"]}, indent=1), flush=True)

    (out / "oracle_check.json").write_text(json.dumps(result, indent=1))
    ok = True
    if "oracle" in result:
        o = result["oracle"]
        ok &= o["S"]["clean"] == 1.0 and o["H"]["clean"] == 1.0 and o["S"]["proxy"] == 1.0
        ok &= not o["any_trips"]
    if "perturbed" in result:
        p = result["perturbed"]
        ok &= p["S"]["clean"] == 0.0 and p["H"]["clean"] == 0.0
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
