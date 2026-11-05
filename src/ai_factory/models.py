"""Tipos de datos que fluyen por el pipeline."""

from __future__ import annotations

import hashlib
import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TaskSource(str, Enum):
    MANUAL = "manual"
    GITHUB = "github"
    SENTRY = "sentry"


class Task(BaseModel):
    """Unidad de trabajo unificada, venga de donde venga (paso 1: entrada unificada)."""

    source: TaskSource
    external_id: str = ""
    title: str
    body: str = ""
    url: str = ""
    labels: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @property
    def id(self) -> str:
        raw = f"{self.source.value}:{self.external_id or self.title}"
        return hashlib.sha1(raw.encode()).hexdigest()[:10]


class Spec(BaseModel):
    """Salida del Spec Writer (paso 2)."""

    summary: str
    problem: str
    acceptance_criteria: list[str]
    technical_approach: str
    files_to_touch: list[str] = Field(default_factory=list)
    test_plan: list[str]
    risks: list[str] = Field(default_factory=list)
    touches_ui: bool = False
    brain_references: list[str] = Field(
        default_factory=list, description="Notas del Brain consultadas que condicionan la spec."
    )


class BuildReport(BaseModel):
    """Salida del Builder (paso 3)."""

    summary: str
    tests_added: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    tests_passing: bool
    notes: str = ""


Severity = Literal["blocker", "major", "minor", "nit"]


class Finding(BaseModel):
    severity: Severity
    file: str = ""
    description: str
    suggestion: str = ""


class Review(BaseModel):
    """Salida de cada revisor paralelo (paso 4)."""

    reviewer: str = ""
    model: str = ""
    passed: bool
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class Verdict(BaseModel):
    """Salida del Aprobador (paso 5)."""

    approved: bool
    reasoning: str
    required_changes: list[str] = Field(default_factory=list)


class GateDecision(BaseModel):
    """Salida del gate final (paso 6)."""

    risk: Literal["low", "critical"]
    reasoning: str
    core_areas_touched: list[str] = Field(default_factory=list)
    learnings: list[str] = Field(
        default_factory=list, description="Lecciones para guardar en el Brain."
    )


class RunStatus(str, Enum):
    RUNNING = "running"
    SHIPPED = "shipped"
    AWAITING_HUMAN = "awaiting_human"
    FAILED = "failed"
    REJECTED = "rejected"


class RunState(BaseModel):
    run_id: str
    task: Task
    status: RunStatus = RunStatus.RUNNING
    branch: str = ""
    spec: Spec | None = None
    build: BuildReport | None = None
    reviews: list[Review] = Field(default_factory=list)
    verdict: Verdict | None = None
    gate: GateDecision | None = None
    iterations: int = 0
    pr_url: str = ""
    commit: str = ""  # commit de merge en la rama base, si se fusionó directamente
    error: str = ""
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
