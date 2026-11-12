"""Tipos de datos que fluyen por el pipeline."""

from __future__ import annotations

import hashlib
import os
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
    out_of_scope: bool = Field(
        default=False,
        description="True si el problema ya existía antes de este cambio y queda fuera de la tarea: "
        "no bloquea, se propone como tarea de seguimiento.",
    )


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
    # Obsoleto: las lecciones las produce ahora la retrospectiva (Retro.brain_updates).
    learnings: list[str] = Field(default_factory=list)


class QACheck(BaseModel):
    criterion: str = Field(description="El criterio de aceptación o comportamiento comprobado.")
    status: Literal["pass", "fail", "not_applicable"]
    evidence: str = Field(description="Qué se hizo y qué se observó (pasos, textos vistos, códigos HTTP).")


class QAReport(BaseModel):
    """Salida del tester funcional."""

    applicable: bool = Field(description="False si el cambio no tiene nada observable en la app en marcha.")
    passed: bool = Field(description="True si todos los checks aplicables pasan.")
    summary: str
    checks: list[QACheck] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list, description="Rutas de las capturas, relativas a tu directorio.")


class IterationRecord(BaseModel):
    """Qué pasó en cada vuelta Builder → tests → revisores → Aprobador (lo usa la retrospectiva)."""

    iteration: int
    tests_passed: bool
    qa: QAReport | None = None
    reviews: list[Review] = Field(default_factory=list)
    approved: bool | None = None
    required_changes: list[str] = Field(default_factory=list)


class BrainUpdate(BaseModel):
    category: Literal["conventions", "learnings"]
    title: str = Field(description="Título corto de la nota, 3–8 palabras.")
    body: str = Field(description="La regla o lección, accionable, en 1–4 frases.")


class Retro(BaseModel):
    """Salida del sector de mejora continua tras cada tarea."""

    summary: str = Field(description="Una o dos frases: cómo fue la tarea y por qué.")
    went_well: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list, description="Fricciones reales: rechazos, tests en rojo, hallazgos repetidos.")
    brain_updates: list[BrainUpdate] = Field(
        default_factory=list, description="0–3 notas nuevas para el Brain. Ninguna si no hay nada duradero que aprender."
    )
    factory_suggestions: list[str] = Field(
        default_factory=list, description="0–3 cambios a la fábrica (prompts, configuración, rutas núcleo, modelos)."
    )
    followups: list["Proposal"] = Field(
        default_factory=list,
        description="0–2 tareas de seguimiento a partir de hallazgos fuera de alcance que merezcan la pena.",
    )


class Proposal(BaseModel):
    """Una mejora concreta para el producto, encontrada por la revisión periódica."""

    title: str = Field(description="Título del issue, imperativo y concreto.")
    category: Literal["bug", "tests", "deuda", "rendimiento", "ux", "seguridad"]
    priority: Literal["alta", "media", "baja"]
    effort: Literal["S", "M", "L"]
    problem: str
    evidence: list[str] = Field(description="Referencias `ruta:línea` que demuestran el problema.")
    proposal: str
    acceptance_criteria: list[str]
    touches_core: bool = Field(default=False, description="True si toca rutas núcleo (requerirá aprobación humana).")


class ScoutReport(BaseModel):
    proposals: list[Proposal] = Field(default_factory=list)
    notes: str = ""


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
    # Segundos acumulados por etapa (setup, spec, build, tests, review, approve, gate, ship).
    stages: dict[str, float] = Field(default_factory=dict)
    history: list[IterationRecord] = Field(default_factory=list)
    followup_issues: list[str] = Field(default_factory=list)  # URLs de las tareas de seguimiento creadas
    retro: Retro | None = None
    qa: QAReport | None = None
    # Ejecución de GitHub Actions que lanzó esta tarea, si la hay.
    actions_run_id: str = Field(default_factory=lambda: os.environ.get("GITHUB_RUN_ID", ""))
    error: str = ""
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None


Retro.model_rebuild()
