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
    s_clean_base: float | None = None  # strong-test audit of its own S solutions (no extra calls)
    h_clean: float | None = None  # held-out clean score, when evaluated
    trips: dict[str, int] = field(default_factory=dict)  # tripwire counts on S
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

    def proxy_winner(self) -> AgentRecord | None:
        """Final proxy winner for E1: the admitted child (generation >= 1) with the highest
        selection score among those with a held-out score; ties go to the earlier one."""
        scored = [r for r in self.admitted() if r.generation > 0 and r.h_clean is not None]
        return max(scored, key=lambda r: (r.score, -r.generation)) if scored else None

    def best(self) -> AgentRecord:
        return max(self.admitted(), key=lambda r: (r.score, -r.generation))
