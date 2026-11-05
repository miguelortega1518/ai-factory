from __future__ import annotations

import hashlib

from ..models import Task, TaskSource


def task_from_text(text: str, title: str = "") -> Task:
    text = text.strip()
    if not text:
        raise ValueError("La tarea está vacía")
    first = text.splitlines()[0]
    return Task(
        source=TaskSource.MANUAL,
        external_id=hashlib.sha1(text.encode()).hexdigest()[:8],
        title=title or (first[:80] + ("…" if len(first) > 80 else "")),
        body=text,
    )
