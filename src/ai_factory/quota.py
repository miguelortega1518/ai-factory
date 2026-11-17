"""Límite de uso de la suscripción: detectarlo, pausar la tarea y relanzarla cuando se restablezca.

Claude Code avisa así cuando se agota la cuota:

    You've hit your org's monthly spend limit · … · your 5-hour limit resets 7:50am (UTC)
    Claude AI usage limit reached|1763366400

Un fallo por límite no es un fallo de la tarea: la fábrica la marca como «en espera», con la hora a
la que se restablece el límite, y `factory retry-paused` (un workflow programado cada hora) la
relanza a partir de esa hora con `workflow_dispatch`. Hay un máximo de reintentos seguidos para no
gastar ejecuciones si el límite es el mensual de la organización, que no se restablece en horas.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import FactoryConfig
from .llm import AgentError

Runner = Callable[..., subprocess.CompletedProcess]

# Sin hora de restablecimiento en el mensaje: se reintenta pasado este tiempo.
DEFAULT_WAIT = timedelta(hours=1)
# Margen tras la hora anunciada, para no relanzar justo en el borde.
SAFETY_MARGIN = timedelta(minutes=5)

_LIMIT_RE = re.compile(
    r"hit your (?:org's |organization's )?(?:monthly |weekly |daily |5-hour )?(?:spend |usage )?limit"
    r"|usage limit reached|5-hour limit|weekly limit|rate_limit_error",
    re.IGNORECASE,
)
_RESET_RE = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", re.IGNORECASE)
_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)


class QuotaExhausted(AgentError):
    """El agente no pudo trabajar porque se agotó la cuota de la suscripción."""

    def __init__(self, message: str, retry_at: datetime):
        super().__init__(message)
        self.retry_at = retry_at


def _tz(name: str):
    name = name.strip()
    if name.upper() in ("UTC", "GMT", "Z"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def retry_time(message: str, now: datetime | None = None) -> datetime | None:
    """Si el mensaje es de límite de uso, la hora (UTC) a partir de la que reintentar; si no, None."""
    if not message or not _LIMIT_RE.search(message):
        return None
    now = now or datetime.now(UTC)

    if m := _EPOCH_RE.search(message):
        at = datetime.fromtimestamp(int(m.group(1)), UTC)
        return max(at, now) + SAFETY_MARGIN

    if (m := _RESET_RE.search(message)) and (tz := _tz(m.group(4))):
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            local_now = now.astimezone(tz)
            at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at <= local_now:
                at += timedelta(days=1)
            return at.astimezone(UTC) + SAFETY_MARGIN

    return now + DEFAULT_WAIT


def check(agent: str, message: str) -> None:
    """Lanza QuotaExhausted si `message` es un aviso de límite de uso."""
    at = retry_time(message)
    if at is not None:
        raise QuotaExhausted(f"{agent}: límite de uso de la suscripción: {message.strip()[:600]}", at)


def fmt(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%H:%M UTC del %d/%m")


# --------------------------------------------------------------------------------------------
# Relanzar las tareas en espera (comando `factory retry-paused`).

_MARK = "<!-- ai-factory:"


def _gh(run: Runner, args: list[str], payload: dict | None = None) -> str:
    r = run(["gh", *args], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])} falló: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout


def _summaries(comments: list[dict]) -> list[dict]:
    """Resúmenes ocultos de la fábrica en los comentarios, del más antiguo al más reciente."""
    out = []
    for c in comments:
        body = c.get("body") or ""
        i = body.rfind(_MARK)
        if i < 0:
            continue
        raw = body[i + len(_MARK):].split("-->", 1)[0].strip()
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def pending_attempts(summaries: list[dict]) -> int:
    """Pausas por límite seguidas al final del historial (un resultado distinto reinicia la cuenta)."""
    n = 0
    for s in reversed(summaries):
        if not s.get("retry_at"):
            break
        n += 1
    return n


def retry_paused(cfg: FactoryConfig, *, now: datetime | None = None, dry_run: bool = False,
                 run: Runner = subprocess.run, log=print) -> list[dict]:
    """Relanza con workflow_dispatch los issues en espera cuyo límite ya se restableció."""
    if not cfg.github_repo:
        raise ValueError("Define github_repo en factory.toml")
    now = now or datetime.now(UTC)
    repo, label = cfg.github_repo, cfg.quota_label
    issues = json.loads(_gh(run, ["api", f"repos/{repo}/issues?state=open&labels={label}&per_page=50"]) or "[]")
    results = []
    for issue in issues:
        number = issue["number"]
        comments = json.loads(_gh(run, ["api", f"repos/{repo}/issues/{number}/comments?per_page=100"]) or "[]")
        summaries = _summaries(comments)
        last = summaries[-1] if summaries else {}
        retry_at = last.get("retry_at")
        attempts = pending_attempts(summaries)
        entry = {"issue": number, "retry_at": retry_at, "attempts": attempts, "action": "wait"}

        if not retry_at:
            # Ya no está en espera (otra ejecución terminó): sólo se quita la etiqueta.
            entry["action"] = "clear"
        elif "monthly" in str(last.get("error", "")).lower() or "weekly" in str(last.get("error", "")).lower():
            entry["action"] = "give_up"
        elif attempts > cfg.quota_max_retries:
            entry["action"] = "give_up"
        elif datetime.fromisoformat(retry_at) <= now:
            entry["action"] = "relaunch"
        results.append(entry)

        log(f"  #{number}: {entry['action']} (reintento {attempts}/{cfg.quota_max_retries}, "
            f"desde {retry_at or '—'})")
        if dry_run or entry["action"] == "wait":
            continue
        if entry["action"] == "relaunch":
            inputs = {"issue": str(number)}
            if last.get("checkpoint_artifact_id") and last.get("run_id"):
                inputs.update(resume_run_id=last["run_id"], checkpoint_artifact=str(last["checkpoint_artifact_id"]))
            elif last.get("status") == "paused":
                # A new-format paused task must never silently restart from scratch.
                entry["action"] = "missing_checkpoint"
                continue
            _gh(run, ["api", "-X", "POST", f"repos/{repo}/actions/workflows/{cfg.quota_workflow}/dispatches",
                      "--input", "-"], {"ref": cfg.base_branch, "inputs": inputs})
            _gh(run, ["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{label}"])
            _gh(run, ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
                {"body": f"▶ **AI Factory:** llegó la hora prevista; relanzo la tarea desde el avance disponible "
                         f"(reintento {attempts} de {cfg.quota_max_retries})."})
        elif entry["action"] == "give_up":
            # Prevent scheduled intake from restarting a permanently blocked task.
            active_labels = [x.get("name", "") if isinstance(x, dict) else x for x in issue.get("labels", [])]
            if cfg.github_label in active_labels:
                _gh(run, ["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{cfg.github_label}"])
            _gh(run, ["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{label}"])
            _gh(run, ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
                {"body": f"⛔ **AI Factory:** la tarea chocó con el límite de uso {attempts} veces seguidas y no "
                         "la relanzo más. Puede ser el límite mensual de la organización "
                         "(https://claude.ai/admin-settings/usage). Cuando haya cuota, usa Run workflow con "
                         f"issue={number}, resume_run_id={last.get('run_id', '')} y "
                         f"checkpoint_artifact={last.get('checkpoint_artifact_id', '')}; "
                         "si es una ejecución antigua sin checkpoint, inicia una tarea nueva."})
        elif entry["action"] == "clear":
            _gh(run, ["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{label}"])
    return results


__all__ = ["QuotaExhausted", "check", "fmt", "pending_attempts", "retry_paused", "retry_time"]
