"""Registro auditable: cada evento de cada agente queda en .factory/runs/<run_id>/events.jsonl."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class AuditLog:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._path = run_dir / "events.jsonl"
        self._lock = threading.Lock()

    def event(self, agent: str, kind: str, **data) -> None:
        record = {"ts": time.time(), "agent": agent, "kind": kind, **data}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self._path.open("a") as f:
            f.write(line + "\n")

    def artifact(self, name: str, content: str) -> Path:
        path = self.run_dir / name
        path.write_text(content)
        return path

    def events(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(l) for l in self._path.read_text().splitlines() if l.strip()]
