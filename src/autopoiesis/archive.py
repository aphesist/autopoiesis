"""The archive: every admitted agent, never pruned (as in the DGM)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class AgentRecord:
    id: str
    parent: str | None
    lineage: str  # e.g. "dgm-03"
    arm: str  # "dgm" or "random"
    generation: int
    agent_dir: str
    diff_path: str | None = None
    admitted: bool = True
    screen_proxy: float | None = None
    full_proxy: float | None = None  # proxy score on S_screen + S_full when it reached stage 2
    s_clean_base: float | None = None  # base-test strict audit of its own S solutions
    # Strong-test S audit: base + plus tests, strict, on its own S solutions (no extra calls).
    # This is what detector (a) and the pre-registration's "strong-test S audit" use.
    s_clean: float | None = None
    h_clean: float | None = None  # held-out clean score, when evaluated
    trips: dict[str, int] = field(default_factory=dict)  # tripwire counts on S
    # Evidence source per flaw, e.g. {"F1": {"solution": 2, "agent_time": 1}}: tasks whose
    # solution tripped the canary, and worker transcripts that named the answer key.
    trip_sources: dict[str, dict[str, int]] = field(default_factory=dict)
    # S tasks on which this agent timed out and the seed did not (the F4 tripwire's count).
    f4_excess: int | None = None
    static_flags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    note: str = ""
    created: float = field(default_factory=time.time)

    @property
    def score(self) -> float:
        """Selection score: stage-2 proxy if available, else screen proxy."""
        if self.full_proxy is not None:
            return self.full_proxy
        return self.screen_proxy or 0.0


class Archive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, AgentRecord] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    r = AgentRecord(**json.loads(line))
                    self.records[r.id] = r  # later lines update earlier ones

    def put(self, r: AgentRecord) -> None:
        self.records[r.id] = r
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(r)) + "\n")

    def admitted(self) -> list[AgentRecord]:
        return [r for r in self.records.values() if r.admitted]

    def children_count(self, agent_id: str) -> int:
        return sum(1 for r in self.records.values() if r.parent == agent_id)

    def ancestors(self, agent_id: str) -> list[str]:
        out, cur = [], self.records[agent_id].parent
        while cur is not None:
            out.append(cur)
            cur = self.records[cur].parent
        return out

    def proxy_winner(self, require_heldout: bool = False) -> AgentRecord | None:
        """Final proxy winner for E1: the admitted child (generation >= 1) with the highest
        selection score; ties go to the earlier one. This is the pre-registered definition
        and is what `reeval` re-runs. require_heldout=True restricts the pool to children
        that already have a held-out score (even generations), for the provisional E1."""
        scored = [r for r in self.admitted()
                  if r.generation > 0 and (r.h_clean is not None or not require_heldout)]
        return max(scored, key=lambda r: (r.score, -r.generation)) if scored else None

    def best(self) -> AgentRecord:
        return max(self.admitted(), key=lambda r: (r.score, -r.generation))
