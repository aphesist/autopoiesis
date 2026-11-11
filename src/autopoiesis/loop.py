"""The open-ended loop: 2 arms x N lineages, advanced in lockstep one generation at a time.

Per lineage and generation:
  1. select a parent (DGM rule or uniform random, by arm)
  2. the mutator proposes a diff from the parent's code and its selection-split feedback
  3. the child is evaluated on S_screen; if close enough to the seed, also on S_full
  4. admitted children get a held-out clean score on even generations and at the end
Held-out scores are written to the archive but never read by selection or admission.
"""

from __future__ import annotations

import json
import random
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .archive import AgentRecord, Archive
from .config import Config
from .detectors import static_scan
from .evaluators.data import build_files
from .evaluators.scoring import TaskScore
from .llm import WorkerProxy
from .mutator import apply_diff, feedback_from_eval, mutate_request, parse_mutation
from .selection import select_parent
from .worker import agent_static_flags, copy_agent, evaluate

ADMIT_MIN_SOLUTIONS = 0.5  # fraction of S_screen tasks with a written solution


class Run:
    def __init__(self, cfg: Config, run_dir: str | Path, llm, sandbox, tasks: dict, splits: dict,
                 seed_agent: str | Path, run_seed: int = 0):
        self.cfg, self.llm, self.sandbox = cfg, llm, sandbox
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "config.json").write_text(cfg.to_json())
        (self.dir / "splits.json").write_text(json.dumps(splits))
        self.splits, self.run_seed = splits, run_seed
        self.files = build_files(tasks, splits, self.dir / "data")
        self.seed_agent = Path(seed_agent)
        exp = cfg.experiment
        self.lineages = [f"{arm}-{i:02d}" for arm in exp.arms for i in range(exp.seeds_per_arm)]
        self.archives = {name: Archive(self.dir / "lineages" / name / "archive.jsonl")
                         for name in self.lineages}
        sock_dir = self.dir / "sock"
        sock_dir.mkdir(exist_ok=True)
        self.proxies = {name: WorkerProxy(llm, cfg.models.worker, cfg.models.worker_effort,
                                          cfg.caps, sock_dir / f"{name}.sock")
                        for name in self.lineages + ["seed"]}
        self.seed_summary: dict | None = None

    # ------------------------------------------------------------------ helpers
    def agent_dir(self, agent_id: str) -> Path:
        return self.dir / "agents" / agent_id

    def _load_scores(self, out_dir: Path) -> dict[str, TaskScore]:
        raw = json.loads((out_dir / "_scores.json").read_text())["tasks"]
        return {k: TaskScore(**v) for k, v in raw.items()}

    # ------------------------------------------------------------------ seed
    def evaluate_seed(self) -> dict:
        d = self.agent_dir("seed")
        summary_path = d / "seed_summary.json"
        if summary_path.exists():
            self.seed_summary = json.loads(summary_path.read_text())
            return self.seed_summary
        if not (d / "agent").exists():
            copy_agent(self.seed_agent, d / "agent")
        proxy, caps, sp = self.proxies["seed"], self.cfg.caps, self.splits
        proxy.start()
        try:
            screen = evaluate(self.sandbox, d / "agent", "screen", sp["screen"], self.files, proxy,
                              d / "screen", caps)
            full = evaluate(self.sandbox, d / "agent", "full", sp["full"], self.files, proxy,
                            d / "full", caps) if sp["full"] else None
            held = evaluate(self.sandbox, d / "agent", "heldout", sp["heldout"], self.files, proxy,
                            d / "heldout", caps, run_proxy=False)
        finally:
            proxy.stop()
        s_scores = {**screen.scores, **(full.scores if full else {})}
        self.seed_summary = {
            "screen_proxy": screen.summary["proxy"],
            "full_proxy": sum(s.proxy for s in s_scores.values()) / len(s_scores),
            "s_clean_base": sum(s.clean_base for s in s_scores.values()) / len(s_scores),
            "h_clean": held.summary["clean"],
            "trips": _count_trips(s_scores, screen.decoy_hits + (full.decoy_hits if full else 0)),
        }
        summary_path.write_text(json.dumps(self.seed_summary, indent=2))
        for name in self.lineages:
            arc = self.archives[name]
            rid = f"{name}-g00"
            if rid not in arc.records:
                arc.put(AgentRecord(id=rid, parent=None, lineage=name, arm=name.split("-")[0],
                                    generation=0, agent_dir=str(d / "agent"),
                                    screen_proxy=self.seed_summary["screen_proxy"],
                                    full_proxy=self.seed_summary["full_proxy"],
                                    s_clean_base=self.seed_summary["s_clean_base"],
                                    h_clean=self.seed_summary["h_clean"],
                                    trips=self.seed_summary["trips"], note="seed"))
        return self.seed_summary

    # ------------------------------------------------------------------ one generation
    def step(self, gen: int) -> list[AgentRecord]:
        assert self.seed_summary is not None, "evaluate_seed() first"
        todo = [n for n in self.lineages
                if not any(r.generation == gen for r in self.archives[n].records.values())]
        parents, requests = {}, {}
        for name in todo:
            rng = random.Random(f"{self.run_seed}|{name}|{gen}")
            parent = select_parent(self.archives[name], name.split("-")[0], rng)
            parents[name] = parent
            pdir = Path(parent.agent_dir).parent
            fb_dir = pdir / "screen"
            fb = feedback_from_eval(fb_dir, self._load_scores(fb_dir))
            requests[f"{name}-g{gen:02d}"] = mutate_request(Path(parent.agent_dir), fb)

        m = self.cfg.models
        if self.cfg.use_batch_for_mutations and hasattr(self.llm, "complete_batch"):
            completions = self.llm.complete_batch(role="mutator", model=m.mutator, requests=requests,
                                                  max_tokens=self.cfg.caps.mutator_max_tokens,
                                                  effort=m.mutator_effort)
        else:
            completions = {cid: self.llm.complete(role="mutator", model=m.mutator, system=r["system"],
                                                  messages=r["messages"],
                                                  max_tokens=self.cfg.caps.mutator_max_tokens,
                                                  effort=m.mutator_effort, meta={"custom_id": cid})
                           for cid, r in requests.items()}

        def child(name: str) -> AgentRecord:
            cid = f"{name}-g{gen:02d}"
            return self._make_child(name, cid, parents[name], completions[cid], gen)

        with ThreadPoolExecutor(max_workers=max(1, len(todo))) as pool:
            return list(pool.map(child, todo))

    def _make_child(self, name: str, cid: str, parent: AgentRecord, completion, gen: int) -> AgentRecord:
        d = self.agent_dir(cid)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        rec = AgentRecord(id=cid, parent=parent.id, lineage=name, arm=name.split("-")[0],
                          generation=gen, agent_dir=str(d / "agent"), admitted=False)
        (d / "mutation.txt").write_text(completion.text)
        mut = parse_mutation(completion.text, completion.stop_reason)
        if not mut.ok:
            rec.note = mut.note
            self.archives[name].put(rec)
            return rec
        copy_agent(Path(parent.agent_dir), d / "agent")
        (d / "diff.patch").write_text(mut.diff)
        rec.diff_path = str(d / "diff.patch")
        ok, err = apply_diff(mut.diff, d / "agent")
        if not ok:
            rec.note = f"patch failed: {err[:200]}"
            self.archives[name].put(rec)
            return rec
        added = "\n".join(ln[1:] for ln in mut.diff.splitlines()
                           if ln.startswith("+") and not ln.startswith("+++"))
        rec.static_flags = sorted(set(static_scan(added))
                                  | set(agent_static_flags(d / "agent", self.seed_agent)))

        caps, sp, proxy = self.cfg.caps, self.splits, self.proxies[name]
        proxy.start()
        try:
            screen = evaluate(self.sandbox, d / "agent", "screen", sp["screen"], self.files, proxy,
                              d / "screen", caps)
            written = sum(1 for s in screen.scores.values() if s.note != "no solution")
            rec.admitted = written >= ADMIT_MIN_SOLUTIONS * len(sp["screen"])
            rec.screen_proxy = screen.summary["proxy"]
            s_scores, decoys = dict(screen.scores), screen.decoy_hits
            gate = self.seed_summary["screen_proxy"] - self.cfg.experiment.screen_margin
            if rec.admitted and sp["full"] and rec.screen_proxy >= gate:
                full = evaluate(self.sandbox, d / "agent", "full", sp["full"], self.files, proxy,
                                d / "full", caps)
                s_scores.update(full.scores)
                decoys += full.decoy_hits
                rec.full_proxy = sum(s.proxy for s in s_scores.values()) / len(s_scores)
            rec.s_clean_base = sum(s.clean_base for s in s_scores.values()) / len(s_scores)
            rec.trips = _count_trips(s_scores, decoys)
            exp = self.cfg.experiment
            if rec.admitted and (gen % exp.heldout_every == 0 or gen == exp.generations):
                held = evaluate(self.sandbox, d / "agent", "heldout", sp["heldout"], self.files, proxy,
                                d / "heldout", caps, run_proxy=False)
                rec.h_clean = held.summary["clean"]
        finally:
            proxy.stop()
        if not rec.admitted:
            rec.note = rec.note or "too few solutions written"
        self.archives[name].put(rec)
        return rec

    # ------------------------------------------------------------------ driver
    def run(self, generations: int | None = None) -> None:
        self.evaluate_seed()
        last = generations or self.cfg.experiment.generations
        for gen in range(1, last + 1):
            self.step(gen)


def _count_trips(scores: dict[str, TaskScore], decoy_hits: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in scores.values():
        for f in s.trips:
            out[f] = out.get(f, 0) + 1
    if decoy_hits:
        out["D"] = decoy_hits
    return out
