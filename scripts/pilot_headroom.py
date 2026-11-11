"""Pilot step 1: headroom and cost per task (PREREGISTRATION section 1, headroom rule).

Runs the seed agent on 50 problems drawn from S and H, and reports the clean pass rate,
the selection pass rate and the measured cost per task from the ledger.

    python scripts/pilot_headroom.py runs/pilot/headroom --config configs/pilot.json \
        --ledger runs/pilot/ledger.jsonl
    autopoiesis --config configs/pilot.json run pilot   # same ledger, same $15 cap
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autopoiesis.config import Config  # noqa: E402
from autopoiesis.evaluators.data import build_files  # noqa: E402
from autopoiesis.llm import AnthropicLLM, Ledger, WorkerProxy  # noqa: E402
from autopoiesis.sandbox import make_sandbox  # noqa: E402
from autopoiesis.stats import wilson  # noqa: E402
from autopoiesis.tasks import load_splits, load_tasks  # noqa: E402
from autopoiesis.worker import copy_agent, evaluate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--config")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--ledger", help="share a ledger with the pilot run so one cap covers both")
    args = ap.parse_args()
    from autopoiesis.cli import load_dotenv

    load_dotenv()
    cfg = Config.load(args.config) if args.config else Config()
    run = Path(args.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(Path(cfg.data_dir) / "tasks.jsonl")
    splits = load_splits(Path(cfg.data_dir) / "splits.json")
    files = build_files(tasks, splits, run / "data")
    pool = splits["screen"] + splits["full"] + splits["heldout"]
    ids = sorted(random.Random(7).sample(pool, args.n))
    ledger_path = Path(args.ledger) if args.ledger else run / "ledger.jsonl"
    ledger = Ledger(ledger_path, cfg.budget_usd)
    before = ledger.spent
    proxy = WorkerProxy(AnthropicLLM(ledger), cfg.models.worker, cfg.models.worker_effort, cfg.caps,
                        run / "worker.sock")
    if not (run / "agent").exists():
        copy_agent(ROOT / "agent_seed", run / "agent")
    proxy.start()
    try:
        res = evaluate(make_sandbox(cfg.sandbox, cfg.docker_image), run / "agent", "headroom", ids,
                       files, proxy, run / "headroom", cfg.caps)
    finally:
        proxy.stop()
    k = sum(res.scores[t].clean for t in ids)
    calls = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
    worker = [c for c in calls if c["role"] == "worker"]
    out = {
        "n": len(ids),
        "clean_pass": k / len(ids), "clean_wilson95": wilson(k, len(ids)),
        "selection_pass": res.summary["proxy"],
        "in_headroom_band": 0.30 <= k / len(ids) <= 0.70,
        "usd_this_step": round(ledger.spent - before, 4),
        "usd_per_task": round((ledger.spent - before) / len(ids), 5),
        "worker_calls": len(worker),
        "mean_input_tokens": sum(c["usage"].get("input_tokens", 0) for c in worker) / max(1, len(worker)),
        "mean_output_tokens": sum(c["usage"].get("output_tokens", 0) for c in worker) / max(1, len(worker)),
        "trips": res.summary["trips"],
    }
    (run / "headroom.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
