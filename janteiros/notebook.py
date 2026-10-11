"""O Caderno: every fact the specialists saw, with its origin. Raw facts only, never conclusions.

The engine reads only facts that passed the code audit (status "audited" or "confirmed").
Nothing is deleted: a rejected fact stays with its reason so the specialist can record a better one.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

OK_STATES = ("audited", "confirmed")


@dataclass
class Fact:
    id: int
    kind: str
    data: dict[str, Any]
    origin: dict[str, Any]
    agent: str
    step: int
    at: float
    status: str = "registered"
    reason: str = ""
    need: str = ""

    def brief(self) -> str:
        keep = {k: v for k, v in self.data.items() if k not in ("evidence",) and v not in (None, "", [])}
        return f"#{self.id} {self.kind} {json.dumps(keep, ensure_ascii=False)[:300]}"


@dataclass
class Notebook:
    path: Path | None = None
    facts: list[Fact] = field(default_factory=list)
    listeners: list[Callable[[Fact], None]] = field(default_factory=list)
    t0: float = field(default_factory=time.monotonic)

    def add(self, kind: str, data: dict, *, agent: str, step: int, origin: dict, status: str = "audited", reason: str = "", need: str = "") -> Fact:
        f = Fact(id=len(self.facts) + 1, kind=kind, data=data, origin=origin, agent=agent, step=step,
                 at=round(time.monotonic() - self.t0, 2), status=status, reason=reason, need=need)
        self.facts.append(f)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(f), ensure_ascii=False, default=str) + "\n")
        for cb in list(self.listeners):
            try:
                cb(f)
            except Exception:  # noqa: BLE001 - a listener must never break recording
                pass
        return f

    def ok(self, kind: str | None = None) -> list[Fact]:
        return [f for f in self.facts if f.status in OK_STATES and (kind is None or f.kind == kind)]

    def by_agent(self, agent: str) -> list[Fact]:
        return [f for f in self.facts if f.agent == agent]

    def count(self, kind: str) -> int:
        return len(self.ok(kind))
