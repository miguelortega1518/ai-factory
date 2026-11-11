"""Informa del resultado de cada ejecución en el issue de GitHub que la originó.

- shipped: comenta el resultado (commit o PR) y cierra el issue como completado. Si el cambio va por
  PR con auto-merge, sólo comenta: el PR lleva `Closes #N` y GitHub cierra el issue al fusionar.
- awaiting_human: comenta con el enlace al PR que espera aprobación (también lleva `Closes #N`).
- rejected / failed: comenta el motivo y deja el issue abierto.

Usa la API REST mediante `gh api`. Un fallo al comentar nunca rompe la ejecución: queda en la auditoría.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from .audit import AuditLog
from .config import FactoryConfig
from .models import RunState, RunStatus, TaskSource

Runner = Callable[..., subprocess.CompletedProcess]


def issue_ref(state: RunState) -> tuple[str, int] | None:
    t = state.task
    if t.source != TaskSource.GITHUB:
        return None
    repo, number = t.metadata.get("repo"), t.metadata.get("number")
    if not repo or not number:
        return None
    return str(repo), int(number)


def closes_line(state: RunState, cfg: FactoryConfig) -> str:
    """Línea `Closes #N` para el cuerpo de un PR, si el issue es del mismo repositorio."""
    ref = issue_ref(state)
    if not ref:
        return ""
    repo, number = ref
    return f"Closes #{number}" if repo == cfg.github_repo else f"Closes {repo}#{number}"


def summary(state: RunState) -> dict:
    """Resumen legible por máquinas (lo lee el monitor web de la fábrica)."""
    return {
        "v": 1,
        "run_id": state.run_id,
        "actions_run_id": state.actions_run_id,
        "status": state.status.value,
        "iterations": state.iterations,
        "commit": state.commit,
        "pr_url": state.pr_url,
        "risk": state.gate.risk if state.gate else None,
        "error": state.error[:300],
        "stages": state.stages,
        "seconds": round((state.finished_at or 0) - state.started_at, 1) if state.finished_at else None,
        "reviews": [{"reviewer": r.reviewer, "model": r.model, "passed": r.passed} for r in state.reviews],
        "retro": {
            "brain_updates": len(state.retro.brain_updates),
            "factory_suggestions": state.retro.factory_suggestions,
            "problems": state.retro.problems,
            "followup_issues": state.followup_issues,
        } if state.retro else None,
    }


def retro_section(state: RunState, applied: bool) -> str:
    r = state.retro
    if not r:
        return ""
    out = [f"\n\n### ♻ Mejora continua\n{r.summary}"]
    if r.problems:
        out.append("**Fricciones:**\n" + "\n".join(f"- {p}" for p in r.problems))
    if r.brain_updates:
        verb = "Añadido al Brain" if applied else "Propuesto para el Brain (no se aplicó: el cambio no se fusionó)"
        out.append(f"**{verb}:**\n" + "\n".join(f"- `{u.category}` · {u.title}: {u.body}" for u in r.brain_updates))
    if r.factory_suggestions:
        out.append("**Sugerencias para la fábrica:**\n" + "\n".join(f"- {x}" for x in r.factory_suggestions))
    if state.followup_issues:
        out.append("**Tareas de seguimiento propuestas** (hallazgos fuera de alcance; añade `ai-factory` para "
                   "ejecutarlas):\n" + "\n".join(f"- {u}" for u in state.followup_issues))
    return "\n\n".join(out)


def _footer(state: RunState) -> str:
    data = json.dumps(summary(state), ensure_ascii=False).replace("--", "\\u002d\\u002d")
    return f"\n\n<sub>AI Factory · ejecución `{state.run_id}`</sub>\n<!-- ai-factory:{data} -->"


def build_comment(state: RunState, cfg: FactoryConfig) -> tuple[str, bool] | None:
    """Devuelve (comentario, cerrar_issue) o None si no hay nada que decir."""
    ref = issue_ref(state)
    if not ref:
        return None
    repo = ref[0]
    reviews = "\n".join(
        f"- {'✅' if r.passed else '❌'} **{r.reviewer}** ({r.model}): {r.summary}" for r in state.reviews
    )
    spec = state.spec.summary if state.spec else ""

    if state.status == RunStatus.SHIPPED:
        if state.commit:
            where = f"Fusionado en `{cfg.base_branch}` en https://github.com/{repo}/commit/{state.commit}."
            close = True
        elif state.pr_url:
            where = f"Cambio en el PR {state.pr_url}; GitHub cierra este issue al fusionarlo (`Closes #`)."
            close = False
        else:
            where = f"El cambio está en la rama `{state.branch}`."
            close = False
        deploy = "Se ejecutó el despliegue configurado." if cfg.deploy_command else \
            "No se ha desplegado: no hay `deploy_command` configurado."
        body = (
            f"✅ **Resuelto por la AI Factory.** {where}\n\n**Spec:** {spec}\n\n"
            f"Pasó la verificación (`{cfg.test_command}`) y lo aprobó el Aprobador tras "
            f"{state.iterations} iteración(es). Gate: riesgo **{state.gate.risk if state.gate else '?'}**. {deploy}\n\n"
            f"### Revisiones\n{reviews}"
        ) + retro_section(state, applied=True)
        return body + _footer(state), close

    if state.status == RunStatus.AWAITING_HUMAN:
        where = f"PR: {state.pr_url}" if state.pr_url else f"Rama: `{state.branch}`"
        core = ", ".join(state.gate.core_areas_touched) if state.gate and state.gate.core_areas_touched else ""
        body = (
            f"✋ **Necesita aprobación humana.** El cambio está listo y aprobado técnicamente, pero el "
            f"Gatekeeper lo clasificó como **crítico**: {state.gate.reasoning if state.gate else ''}"
            + (f"\n\nToca rutas núcleo: {core}" if core else "")
            + f"\n\n{where}\n\nRevísalo y fusiónalo, o ejecuta `factory approve {state.run_id}`.\n\n"
            f"### Revisiones\n{reviews}"
            + retro_section(state, applied=True)
        )
        return body + _footer(state), False

    if state.status == RunStatus.REJECTED:
        pending = "\n".join(f"- {c}" for c in (state.verdict.required_changes if state.verdict else []))
        body = (
            f"⚠️ **La AI Factory no consiguió un cambio aprobado.** {state.error}\n\n"
            + (f"Último feedback del Aprobador:\n{pending}\n\n" if pending else "")
            + f"El trabajo quedó en la rama `{state.branch}`. El issue sigue abierto."
            + retro_section(state, applied=False)
        )
        return body + _footer(state), False

    if state.status == RunStatus.FAILED:
        body = (
            f"❌ **La ejecución de la AI Factory falló** antes de terminar:\n\n```\n{state.error[:1500]}\n```\n\n"
            "No se ha cambiado nada en la rama principal. El issue sigue abierto."
        )
        return body + _footer(state), False

    return None


def report(state: RunState, cfg: FactoryConfig, audit: AuditLog, run: Runner = subprocess.run) -> None:
    if not cfg.issue_feedback:
        return
    ref = issue_ref(state)
    built = build_comment(state, cfg)
    if not ref or not built:
        return
    repo, number = ref
    body, close = built
    try:
        _gh(run, ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"], {"body": body})
        if close:
            _gh(run, ["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "--input", "-"],
                {"state": "closed", "state_reason": "completed"})
        audit.event("factory", "issue_feedback", issue=f"{repo}#{number}", closed=close)
    except Exception as e:  # noqa: BLE001 — informar nunca debe romper la ejecución
        audit.event("factory", "issue_feedback_error", issue=f"{repo}#{number}", error=str(e)[:1000])


def _gh(run: Runner, args: list[str], payload: dict) -> None:
    r = run(["gh", *args], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} falló: {(r.stderr or r.stdout).strip()[:500]}")
