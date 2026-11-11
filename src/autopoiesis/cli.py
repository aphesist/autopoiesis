"""Command line: autopoiesis {budget,controls,run,reeval,detect,analyze}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config

REPO = Path(__file__).resolve().parents[2]


def _cfg(path: str | None) -> Config:
    return Config.load(path) if path else Config()


def cmd_budget(args) -> None:
    """Planned spend from the config and assumed per-call costs (an ESTIMATE, not a measurement)."""
    c = _cfg(args.config)
    e, s = c.experiment, c.splits
    lineages = len(e.arms) * e.seeds_per_arm
    children = lineages * e.generations * e.children_per_generation
    full_frac = args.full_fraction
    h_evals = children * (e.generations // e.heldout_every + (e.generations % e.heldout_every != 0)) / e.generations
    tasks = children * (s.screen + full_frac * s.full) + h_evals * args.admit_fraction * s.heldout
    mut = children * args.usd_per_mutation * (0.5 if c.use_batch_for_mutations else 1.0)
    work = tasks * args.usd_per_task
    rows = {
        "lineages": lineages, "children": children, "worker_tasks": round(tasks),
        "mutations_usd": round(mut, 2), "worker_usd": round(work, 2),
        "subtotal_usd": round(mut + work, 2), "budget_usd": c.budget_usd,
        "assumptions": {"usd_per_mutation_sync": args.usd_per_mutation,
                        "usd_per_task": args.usd_per_task, "full_fraction": full_frac,
                        "admit_fraction": args.admit_fraction},
    }
    print(json.dumps(rows, indent=2))


def cmd_controls(args) -> None:
    """Run the tripwire positive/negative controls through the configured sandbox."""
    import pytest

    sys.exit(pytest.main(["-q", str(REPO / "tests" / "test_evaluators.py")]))


def cmd_run(args) -> None:
    from .llm import AnthropicLLM, Ledger
    from .loop import Run
    from .sandbox import make_sandbox
    from .tasks import load_splits, load_tasks

    c = _cfg(args.config)
    run_dir = Path(c.runs_dir) / args.name
    ledger = Ledger(run_dir / "ledger.jsonl", c.budget_usd)
    run = Run(c, run_dir, AnthropicLLM(ledger, thinking={"worker": c.models.worker_thinking}), make_sandbox(c.sandbox, c.docker_image),
              load_tasks(Path(c.data_dir) / "tasks.jsonl"), load_splits(Path(c.data_dir) / "splits.json"),
              REPO / "agent_seed", run_seed=args.seed)
    run.run(args.generations)
    if run.stopped:
        print(f"stopped early (budget): {run.stopped}")
    print(f"done; spent ${ledger.spent:.2f}")


def _run_llm(run_dir: Path):
    """The run's own config, and a ledger-backed client that continues the run's ledger."""
    from .llm import AnthropicLLM, Ledger

    c = Config.load(run_dir / "config.json")
    ledger = Ledger(run_dir / "ledger.jsonl", c.budget_usd)
    return c, ledger, AnthropicLLM(ledger, thinking={"worker": c.models.worker_thinking})


def cmd_reeval(args) -> None:
    """Re-run each lineage's proxy winner K times and the seed seed_baseline_k times (E1)."""
    from .confirmatory import reeval
    from .sandbox import make_sandbox

    run_dir = Path(args.run_dir)
    c, ledger, llm = _run_llm(run_dir)
    res = reeval(run_dir, llm, make_sandbox(c.sandbox, c.docker_image), k=args.k, ledger=ledger)
    done = {aid: f"{a['reps_done']}/{a['reps_wanted']}" for aid, a in res["agents"].items()}
    print(json.dumps({"status": res["status"], "reps": done}, indent=2))
    print(f"spent ${ledger.spent:.2f} of ${ledger.budget_usd:.2f}")


def cmd_detect(args) -> None:
    """Run the four E5 detectors on every admitted child's diff."""
    from .confirmatory import detect

    run_dir = Path(args.run_dir)
    _, ledger, llm = _run_llm(run_dir)
    print(json.dumps(detect(run_dir, llm, ledger=ledger), indent=2))
    print(f"spent ${ledger.spent:.2f} of ${ledger.budget_usd:.2f}")


def cmd_analyze(args) -> None:
    from .analysis import analyse

    print(json.dumps(analyse(args.run_dir), indent=2, default=str))


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=VALUE lines from a local .env without overriding the environment."""
    import os

    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="autopoiesis")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("budget")
    b.add_argument("--usd-per-mutation", type=float, default=0.13)
    b.add_argument("--usd-per-task", type=float, default=0.0015)
    b.add_argument("--full-fraction", type=float, default=0.6)
    b.add_argument("--admit-fraction", type=float, default=0.8)
    b.set_defaults(fn=cmd_budget)
    sub.add_parser("controls").set_defaults(fn=cmd_controls)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--generations", type=int)
    r.add_argument("--seed", type=int, default=0)
    r.set_defaults(fn=cmd_run)
    rv = sub.add_parser("reeval")
    rv.add_argument("run_dir")
    rv.add_argument("--k", type=int, help="re-runs per lineage winner (default: experiment.rerun_k)")
    rv.set_defaults(fn=cmd_reeval)
    d = sub.add_parser("detect")
    d.add_argument("run_dir")
    d.set_defaults(fn=cmd_detect)
    a = sub.add_parser("analyze")
    a.add_argument("run_dir")
    a.set_defaults(fn=cmd_analyze)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
