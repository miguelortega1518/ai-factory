"""Registra el trabajo de la fábrica como tareas de un proyecto de Forja.

La fábrica trabaja como un miembro más del equipo: con una cuenta propia de Forja crea una tarea por
cada encargo en el proyecto configurado y la mueve según avanza:

    empieza a programar        → En progreso
    pasa a revisión            → En code review
    fusionado                  → Hecho (con enlace al PR o al commit y un comentario de resumen)
    espera aprobación humana   → En code review (con enlace al PR)
    rechazada o fallida        → Bloqueada (área «qa» u «otro», con el motivo)

Credenciales sólo por entorno: FORJA_EMAIL y FORJA_PASSWORD (la URL puede venir de FORJA_URL).
Nada de esto rompe nunca una ejecución: cualquier fallo queda en la auditoría y la tarea sigue.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .audit import AuditLog
from .config import FactoryConfig
from .models import RunState, RunStatus, Task

KIND_BY_LABEL = {"bug": "bug", "deuda": "deuda", "chore": "chore", "mejora": "mejora", "feature": "feature"}


class ForjaError(RuntimeError):
    pass


class ForjaClient:
    """Cliente mínimo de la API de Forja con sesión por cookie (como el navegador)."""

    def __init__(self, base_url: str, timeout: int = 90):
        self.base = base_url.rstrip("/")
        self.timeout = timeout  # Render (plan gratuito) puede tardar en despertar
        # La sesión se guarda a mano (no con CookieJar): la cookie de Forja lleva `Secure` y así el
        # cliente funciona igual contra producción (HTTPS) que contra una Forja local de pruebas.
        self.cookies: dict[str, str] = {}
        local = self.base.startswith(("http://127.0.0.1", "http://localhost"))
        handlers = [urllib.request.ProxyHandler({})] if local else []
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                for header in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    self.cookies[name.strip()] = rest.split(";", 1)[0]
                raw = resp.read().decode() or "null"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if "PASSWORD_CHANGE_REQUIRED" in detail:
                raise ForjaError("La cuenta de la fábrica debe cambiar su contraseña: entra una vez en Forja "
                                 "con ella, cámbiala y actualiza el secreto FORJA_PASSWORD") from e
            raise ForjaError(f"{method} {path} → HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ForjaError(f"{method} {path}: no se pudo conectar ({e})") from e

    def login(self, email: str, password: str) -> dict:
        self.request("POST", "/api/auth/login", {"email": email, "password": password})
        me = self.request("GET", "/api/auth/me")
        user = (me or {}).get("user") or {}
        if user.get("mustChangePassword"):
            raise ForjaError("La cuenta de la fábrica debe cambiar su contraseña antes de usar la API")
        return user

    def find_space(self, name: str) -> dict:
        spaces = self.request("GET", "/api/spaces") or []
        for s in spaces:
            if str(s.get("name", "")).strip().lower() == name.strip().lower():
                return s
        raise ForjaError(f"No encuentro el proyecto «{name}» en Forja (o la cuenta de la fábrica no es miembro)")

    def find_task(self, space_id: str, title: str) -> dict | None:
        from urllib.parse import quote
        rows = self.request("GET", f"/api/tasks?spaceId={space_id}&search={quote(title[:200])}") or []
        rows = rows.get("tasks", rows) if isinstance(rows, dict) else rows
        for t in rows:
            if t.get("title") == title:
                return t
        return None

    def create_task(self, **fields) -> dict:
        return self.request("POST", "/api/tasks", {k: v for k, v in fields.items() if v is not None})

    def update(self, task_id: str, **fields) -> dict:
        return self.request("PATCH", f"/api/tasks/{task_id}", fields)

    def set_status(self, task_id: str, status: str, **extra) -> dict:
        # Forja valida con zod: un campo opcional no admite null, así que se omite.
        body = {"status": status, **{k: v for k, v in extra.items() if v is not None}}
        return self.request("PATCH", f"/api/tasks/{task_id}", body)

    def comment(self, task_id: str, content: str) -> dict:
        return self.request("POST", f"/api/tasks/{task_id}/comments", {"content": content[:4900]})


def forja_title(task: Task) -> str:
    """Título estable de la tarea en Forja (con el número de issue, para encontrarla de nuevo)."""
    number = task.metadata.get("number")
    return (f"#{number} {task.title}" if number else task.title)[:500]


def kind_for(task: Task) -> str:
    for label in task.labels:
        if label.lower() in KIND_BY_LABEL:
            return KIND_BY_LABEL[label.lower()]
    title = task.title.lower()
    if any(w in title for w in ("error", "falla", "bug", "500", "corregir", "arreglar")):
        return "bug"
    return "mejora"


@dataclass
class ForjaTracker:
    """Sigue una ejecución en Forja. Si no está configurado o Forja falla, no hace nada (y lo audita)."""

    cfg: FactoryConfig
    audit: AuditLog
    client: ForjaClient | None = None
    task_id: str | None = None
    status: str | None = None
    disabled_reason: str = ""
    _env: dict = field(default_factory=lambda: dict(os.environ))

    @property
    def enabled(self) -> bool:
        return self.cfg.forja.enabled and not self.disabled_reason

    def _safe(self, what: str, fn) -> None:
        if not self.enabled:
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — Forja nunca rompe una ejecución
            self.disabled_reason = f"{what}: {e}"[:500]
            self.audit.event("forja", "error", step=what, error=self.disabled_reason)

    def start(self, state: RunState) -> None:
        def go():
            url = self.cfg.forja.url or self._env.get("FORJA_URL", "")
            email, password = self._env.get("FORJA_EMAIL"), self._env.get("FORJA_PASSWORD")
            if not (url and email and password):
                raise ForjaError("faltan FORJA_URL/forja.url, FORJA_EMAIL o FORJA_PASSWORD")
            self.client = self.client or ForjaClient(url)
            me = self.client.login(email, password)
            space = self.client.find_space(self.cfg.forja.project)
            title = forja_title(state.task)
            existing = self.client.find_task(space["id"], title)
            if existing:
                self.task_id, self.status = existing["id"], existing.get("status")
            else:
                link = f"\n\nOrigen: {state.task.url}" if state.task.url else ""
                created = self.client.create_task(
                    spaceId=space["id"], title=title, responsibleId=me["id"], kind=kind_for(state.task),
                    priority="normal", labels=["ai-factory"],
                    description=(state.task.body[:4000] + link)[:5000],
                )
                self.task_id, self.status = created["id"], created.get("status", "no_iniciada")
            self.audit.event("forja", "task", task_id=self.task_id, title=title)
        self._safe("start", go)

    def move(self, status: str, **extra) -> None:
        if self.status == status:
            return

        def go():
            if not self.task_id:
                return
            self.client.set_status(self.task_id, status, **extra)
            self.status = status
            self.audit.event("forja", "status", task_id=self.task_id, status=status)
        self._safe(f"move:{status}", go)

    def finish(self, state: RunState, summary: str) -> None:
        def go():
            if not self.task_id:
                return
            link = state.pr_url or (
                f"https://github.com/{self.cfg.github_repo}/commit/{state.commit}"
                if state.commit and self.cfg.github_repo else "")
            fields = {"branch": state.branch[:200]} if state.branch else {}
            if link:
                fields["prUrl"] = link
            if fields:
                self.client.update(self.task_id, **fields)
            self.client.comment(self.task_id, summary)
        self._safe("finish", go)
        if state.status == RunStatus.SHIPPED and not state.pr_url:
            self.move("completada")
        elif state.status in (RunStatus.SHIPPED, RunStatus.AWAITING_HUMAN):
            self.move("en_revision")  # PR con auto-merge o esperando a una persona
        elif state.status in (RunStatus.REJECTED, RunStatus.FAILED):
            area = "qa" if state.status == RunStatus.REJECTED else "otro"
            self.move("bloqueada", blockedByArea=area,
                      blockedByAreaText=(f"AI Factory: {state.error}"[:500] if area == "otro" else None))


def summary_text(state: RunState, cfg: FactoryConfig) -> str:
    """Resumen para el comentario de Forja: el mismo del issue de GitHub, sin el JSON oculto."""
    from .issue_feedback import build_comment

    built = build_comment(state, cfg)
    if built:
        return built[0].split("\n<!-- ai-factory:")[0].split("\n\n<sub>")[0]
    lines = [f"AI Factory · resultado: {state.status.value}."]
    if state.spec:
        lines.append(f"Spec: {state.spec.summary}")
    if state.error:
        lines.append(f"Motivo: {state.error[:800]}")
    return "\n\n".join(lines)


__all__ = ["ForjaClient", "ForjaError", "ForjaTracker", "forja_title", "kind_for", "summary_text"]
