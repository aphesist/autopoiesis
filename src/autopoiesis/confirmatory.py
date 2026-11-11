"""Confirmatory steps run on a finished run directory (PREREGISTRATION.md, E1 and E5).

reeval  Each lineage's final proxy winner is re-run K times on S (screen + full), H and
        B with fresh worker calls, and the seed seed_baseline_k times. The recorded score
        of a winner is the maximum of many noisy draws and so is biased upwards; E1 uses
        these re-run per-problem means instead.
detect  The four E5 detectors on every admitted child's diff: (a) gap flag, (b) static
        scan, (c) small LLM monitor, (d) large LLM monitor. They never see tripwires.

Both steps are resumable (completed units are skipped) and stop cleanly at the ledger cap.

Layout under the run directory:
  reeval/<agent_id>/rep<k>/<split>/   per-problem results (_scores.json, solutions, logs)
  reeval/<agent_id>/rep<k>/_rep.json  written last; marks the repetition complete
  reeval/summary.json                 per-agent repetition summaries and their means
  detect.jsonl                        one line per child with all four detector outputs
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .archive import AgentRecord, Archive
from .config import Config
from .detectors import gap_flag, llm_monitor
from .evaluators.data import file_paths
from .evaluators.scoring import TaskScore
from .llm import BudgetExceeded, Ledger, WorkerProxy
from .loop import own_socket
from .worker import evaluate

# (split, score with the flawed selection evaluator too?) Clean scoring runs on all of them.
REEVAL_SPLITS: tuple[tuple[str, bool], ...] = (
    ("screen", True), ("full", True), ("heldout", False), ("rerank", True))
SEED_ID = "seed"


@dataclass
class RunData:
    """What a finished run directory holds, reopened for the confirmatory steps."""
    dir: Path
    cfg: Config
    splits: dict
    files: dict[str, Path]
    archives: dict[str, Archive]
    sandbox: object | None = None  # set only for steps that run agents

    def agent_path(self, rec: AgentRecord) -> Path:
        """The agent's code; falls back to the run-relative path if the run was moved."""
        p = Path(rec.agent_dir)
        return p if p.exists() else self.dir / "agents" / rec.id / "agent"


def open_run(run_dir: str | Path, sandbox=None) -> RunData:
    d = Path(run_dir)
    root = d / "lineages"
    archives = {p.name: Archive(p / "archive.jsonl") for p in sorted(root.iterdir()) if p.is_dir()}
    return RunData(d, Config.load(d / "config.json"), json.loads((d / "splits.json").read_text()),
                   file_paths(d / "data"), archives, sandbox)


def _budget_hit(ledger: Ledger | None) -> bool:
    return ledger is not None and ledger.spent >= ledger.budget_usd


# --------------------------------------------------------------------------- reeval

def _load_scores(out_dir: Path) -> dict[str, TaskScore]:
    raw = json.loads((out_dir / "_scores.json").read_text())["tasks"]
    return {k: TaskScore(**v) for k, v in raw.items()}


def rep_summary(by_split: dict[str, dict[str, TaskScore]], splits: dict) -> dict:
    """Split-level means of one repetition. S pools screen and full, as the loop does."""
    def mean(ids: list[str], split_names: tuple[str, ...], attr: str) -> float | None:
        pool = {t: s for n in split_names for t, s in by_split.get(n, {}).items()}
        vals = [float(getattr(pool[t], attr)) for t in ids if t in pool]
        return sum(vals) / len(vals) if vals else None

    s_ids = splits["screen"] + splits["full"]
    return {
        "screen_proxy": mean(splits["screen"], ("screen",), "proxy"),
        "s_proxy": mean(s_ids, ("screen", "full"), "proxy"),
        "s_clean_base": mean(s_ids, ("screen", "full"), "clean_base"),
        "s_clean": mean(s_ids, ("screen", "full"), "clean"),
        "h_clean": mean(splits["heldout"], ("heldout",), "clean"),
        "b_proxy": mean(splits.get("rerank", []), ("rerank",), "proxy"),
        "b_clean": mean(splits.get("rerank", []), ("rerank",), "clean"),
    }


def load_reeval(run_dir: str | Path) -> dict[str, list[dict[str, dict[str, TaskScore]]]]:
    """Completed repetitions per agent: agent_id -> [ {split -> {task -> TaskScore}} ]."""
    root = Path(run_dir) / "reeval"
    out: dict[str, list] = {}
    if not root.is_dir():
        return out
    for adir in sorted(p for p in root.iterdir() if p.is_dir()):
        reps = []
        for rdir in sorted(adir.glob("rep*"), key=lambda p: int(p.name[3:])):
            if not (rdir / "_rep.json").exists():
                continue
            reps.append({n: _load_scores(rdir / n) for n, _ in REEVAL_SPLITS
                         if (rdir / n / "_scores.json").exists()})
        if reps:
            out[adir.name] = reps
    return out


def _reeval_rep(run: RunData, agent_dir: Path, rep_dir: Path, proxy: WorkerProxy,
                ledger: Ledger | None) -> dict:
    """One fresh evaluation of an agent on every non-empty split. Splits already scored in
    an interrupted earlier attempt are kept; a partly written split is redone from scratch."""
    caps, by_split = run.cfg.caps, {}
    for split, run_proxy in REEVAL_SPLITS:
        ids = run.splits.get(split, [])
        if not ids:
            continue
        out = rep_dir / split
        if (out / "_scores.json").exists():
            by_split[split] = _load_scores(out)
            continue
        if out.exists():
            shutil.rmtree(out)
        if ledger is not None:
            ledger.check()
        res = evaluate(run.sandbox, agent_dir, split, ids, run.files, proxy, out, caps,
                       run_proxy=run_proxy)
        if _budget_hit(ledger):
            # Calls after the cap are refused by the proxy, so this split may be degraded.
            shutil.rmtree(out)
            raise BudgetExceeded(f"budget reached during {rep_dir.name}/{split}")
        by_split[split] = res.scores
    summary = rep_summary(by_split, run.splits)
    (rep_dir / "_rep.json").write_text(json.dumps(summary, indent=2))
    return summary


def _reeval_agent(run: RunData, llm, agent_id: str, agent_dir: Path, k: int,
                  ledger: Ledger | None) -> str:
    """Run the missing repetitions 1..k of one agent; returns 'complete' or 'budget_exceeded'."""
    todo = [i for i in range(1, k + 1)
            if not (run.dir / "reeval" / agent_id / f"rep{i}" / "_rep.json").exists()]
    if not todo:
        return "complete"
    m = run.cfg.models
    # Each re-evaluated agent gets its own socket directory: they run concurrently.
    proxy = WorkerProxy(llm, m.worker, m.worker_effort, run.cfg.caps,
                        own_socket(run.dir / "sock", f"re-{agent_id}"))
    proxy.start()
    try:
        for i in todo:
            rep_dir = run.dir / "reeval" / agent_id / f"rep{i}"
            rep_dir.mkdir(parents=True, exist_ok=True)
            _reeval_rep(run, agent_dir, rep_dir, proxy, ledger)
    except BudgetExceeded:
        return "budget_exceeded"
    finally:
        proxy.stop()
    return "complete"


def reeval_targets(run: RunData) -> dict[str, dict]:
    """agent_id -> {role, lineage, agent_dir}: the seed plus each lineage's proxy winner."""
    targets = {SEED_ID: {"role": "seed", "lineage": None,
                         "agent_dir": run.dir / "agents" / "seed" / "agent"}}
    for name, arc in run.archives.items():
        w = arc.proxy_winner()
        if w is not None:
            targets[w.id] = {"role": "winner", "lineage": name, "agent_dir": run.agent_path(w),
                             "recorded_score": w.score, "recorded_full": w.full_proxy is not None}
    return targets


def write_reeval_summary(run_dir: str | Path, targets: dict[str, dict], k: int, seed_k: int,
                         status: str) -> dict:
    """Summarise completed repetitions (read back from disk, so it is correct after resumes)."""
    run_dir = Path(run_dir)
    splits = json.loads((run_dir / "splits.json").read_text())
    reps = load_reeval(run_dir)
    agents = {}
    for aid, t in targets.items():
        rs = [rep_summary(r, splits) for r in reps.get(aid, [])]
        means = {key: (sum(r[key] for r in rs) / len(rs) if rs and rs[0][key] is not None else None)
                 for key in (rs[0] if rs else {})}
        agents[aid] = {"role": t["role"], "lineage": t["lineage"],
                       "reps_wanted": seed_k if t["role"] == "seed" else k,
                       "reps_done": len(rs), "reps": rs, "mean": means,
                       **({"recorded_score": t["recorded_score"]} if t["role"] == "winner" else {})}
    out = {"status": status, "k": k, "seed_k": seed_k, "agents": agents}
    (run_dir / "reeval" / "summary.json").write_text(json.dumps(out, indent=2))
    return out


def reeval(run_dir: str | Path, llm, sandbox, k: int | None = None,
           ledger: Ledger | None = None) -> dict:
    """Re-run each lineage winner k times (default experiment.rerun_k) and the seed
    experiment.seed_baseline_k times, with fresh worker calls. Proxy scoring on S and B,
    clean scoring on S, H and B. Returns the summary written to reeval/summary.json."""
    run = open_run(run_dir, sandbox)
    exp = run.cfg.experiment
    k = k or exp.rerun_k
    targets = reeval_targets(run)
    (run.dir / "reeval").mkdir(exist_ok=True)

    def job(aid: str) -> str:
        t = targets[aid]
        reps = exp.seed_baseline_k if t["role"] == "seed" else k
        return _reeval_agent(run, llm, aid, Path(t["agent_dir"]), reps, ledger)

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        statuses = list(pool.map(job, list(targets)))
    status = "budget_exceeded" if "budget_exceeded" in statuses else "complete"
    return write_reeval_summary(run.dir, targets, k, exp.seed_baseline_k, status)


# --------------------------------------------------------------------------- detect

DETECTORS = ("gap_flag", "static_scan", "monitor_small", "monitor_large")


def load_detect(run_dir: str | Path) -> dict[str, dict]:
    path = Path(run_dir) / "detect.jsonl"
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["id"]] = r
    return out


def strong_audit(rec: AgentRecord) -> float:
    """The strong-test S audit of a record (s_clean); legacy records fall back to s_clean_base."""
    if rec.s_clean is not None:
        return rec.s_clean
    return rec.s_clean_base or 0.0


def detect(run_dir: str | Path, llm, ledger: Ledger | None = None) -> dict:
    """Run the four E5 detectors on every admitted child (generation >= 1) with a diff.

    (a) gap flag on the recorded S proxy vs the strong-test S audit (base + plus tests,
        strict) of the same solutions; records without s_clean (made before it was stored)
        fall back to the base-test audit, and the row says which was used
    (b) static flags stored on the record at creation (decoy-file mentions excluded, since
        the decoy is not an exploit)
    (c, d) LLM monitors (models.monitor_small, models.monitor_large) reading the diff
    Appends to detect.jsonl; children already there are skipped.
    """
    run = open_run(run_dir)
    m = run.cfg.models
    done = load_detect(run.dir)
    path = run.dir / "detect.jsonl"
    status, added = "complete", 0
    for name, arc in run.archives.items():
        for rec in sorted(arc.records.values(), key=lambda r: r.generation):
            if rec.generation == 0 or not rec.admitted or rec.id in done:
                continue
            diff_path = Path(rec.diff_path) if rec.diff_path else None
            if diff_path is None or not diff_path.exists():
                diff_path = run.dir / "agents" / rec.id / "diff.patch"
            if not diff_path.exists():
                continue
            diff = diff_path.read_text()
            try:
                if ledger is not None:
                    ledger.check()
                small = llm_monitor(llm, m.monitor_small, diff)
                large = llm_monitor(llm, m.monitor_large, diff)
            except BudgetExceeded:
                status = "budget_exceeded"
                break
            row = {
                "id": rec.id, "lineage": name, "arm": rec.arm, "generation": rec.generation,
                "gap_flag": gap_flag(rec.score, strong_audit(rec)),
                "gap_audit": "s_clean" if rec.s_clean is not None else "s_clean_base",
                "static_scan": bool(set(rec.static_flags) - {"decoy_file"}),
                "monitor_small": small["suspicious"], "monitor_large": large["suspicious"],
                "monitor_small_detail": small, "monitor_large_detail": large,
                "proxy": rec.score, "s_clean": rec.s_clean, "s_clean_base": rec.s_clean_base,
                "static_flags": rec.static_flags,
            }
            with path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            added += 1
        if status != "complete":
            break
    return {"status": status, "added": added, "total": len(load_detect(run.dir))}
