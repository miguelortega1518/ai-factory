"""Sector de mejora continua: revisión periódica del producto.

Recorre el repositorio con el agente "scout", y cada mejora concreta que encuentra la publica como
issue de GitHub con la etiqueta de propuesta (por defecto `ai-factory-propuesta`). Esa etiqueta NO
lanza la fábrica: una persona decide qué propuestas ejecutar añadiéndoles la etiqueta `ai-factory`.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Callable

from .agents import Agents
from .audit import AuditLog
from .brain import Brain
from .config import FactoryConfig
from .llm import LLMClient
from .models import Proposal, ScoutReport
from .tools import Workspace

Runner = Callable[..., subprocess.CompletedProcess]


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñü]+", " ", title.lower()).strip()


def _gh(run: Runner, args: list[str], payload: dict | None = None) -> str:
    r = run(["gh", *args], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])} falló: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout


def existing_titles(cfg: FactoryConfig, run: Runner) -> list[str]:
    out = _gh(run, ["api", f"repos/{cfg.github_repo}/issues?state=all&per_page=100", "--jq", "[.[].title]"])
    return json.loads(out or "[]")


def issue_body(p: Proposal, cfg: FactoryConfig) -> str:
    evidence = "\n".join(f"- `{e}`" for e in p.evidence) or "-"
    criteria = "\n".join(f"- {c}" for c in p.acceptance_criteria)
    core = ("\n> ⚠️ Toca rutas núcleo: aunque la fábrica lo implemente, el cambio esperará tu aprobación en un PR.\n"
            if p.touches_core else "")
    meta = json.dumps(p.model_dump(include={"category", "priority", "effort", "touches_core"}), ensure_ascii=False)
    return (
        f"## Problema\n{p.problem}\n\n## Evidencia\n{evidence}\n\n## Propuesta\n{p.proposal}\n\n"
        f"## Criterios de aceptación\n{criteria}\n- `{cfg.test_command}` pasa.\n\n"
        f"---\n**Categoría:** {p.category} · **Prioridad:** {p.priority} · **Esfuerzo:** {p.effort}\n{core}\n"
        f"> Propuesta del sector de **mejora continua** de la AI Factory. Si quieres que la fábrica la "
        f"implemente, añade la etiqueta `{cfg.github_label}`. Si no, ciérrala.\n"
        f"<!-- ai-factory-proposal:{meta} -->"
    )


def ensure_label(cfg: FactoryConfig, run: Runner) -> None:
    try:
        _gh(run, ["api", "-X", "POST", f"repos/{cfg.github_repo}/labels", "--input", "-"],
            {"name": cfg.proposal_label, "color": "C5DEF5",
             "description": "Mejora propuesta por la AI Factory. Añade ai-factory para ejecutarla."})
    except RuntimeError:
        pass  # ya existe


def run_improvement(cfg: FactoryConfig, client: LLMClient | None, *, max_items: int | None = None,
                    dry_run: bool = False, run: Runner = subprocess.run, log=print) -> list[dict]:
    """Ejecuta una revisión periódica. Devuelve las propuestas con la URL del issue creado (si lo hay)."""
    max_items = max_items or cfg.improve_max_issues
    audit = AuditLog(cfg.runs_dir / f"improve-{time.strftime('%Y%m%d-%H%M%S')}")
    ws = Workspace(cfg.repo_path, cfg.allowed_commands, env=cfg.env)
    brain = Brain(cfg.brain_dir)

    existing: list[str] = []
    if cfg.github_repo:
        existing = existing_titles(cfg, run)
    log(f"♻ Mejora continua: revisando {cfg.repo_path.name} ({len(existing)} issues existentes)…")

    report: ScoutReport = Agents(cfg, client, audit).scout(ws, brain, existing, max_items)
    audit.artifact("scout.json", report.model_dump_json(indent=2))

    seen = {_norm(t) for t in existing}
    results: list[dict] = []
    for p in report.proposals:
        if len(results) >= max_items:
            break
        if _norm(p.title) in seen:
            audit.event("scout", "duplicate", title=p.title)
            continue
        seen.add(_norm(p.title))
        item = {"title": p.title, "priority": p.priority, "category": p.category, "url": ""}
        if cfg.github_repo and not dry_run:
            if not results:
                ensure_label(cfg, run)
            out = _gh(run, ["api", "-X", "POST", f"repos/{cfg.github_repo}/issues", "--input", "-", "--jq", ".html_url"],
                      {"title": p.title, "body": issue_body(p, cfg), "labels": [cfg.proposal_label]})
            item["url"] = out.strip()
        audit.event("scout", "proposal", **item)
        log(f"  • [{p.priority}] {p.title} {item['url']}")
        results.append(item)

    if not results:
        log(f"  Sin propuestas nuevas. {report.notes}")
    return results
