"""Intake desde Sentry: por API (SENTRY_AUTH_TOKEN) o desde el JSON de un webhook."""

from __future__ import annotations

import json
import os
import urllib.request

from ..models import Task, TaskSource


def task_from_sentry_event(payload: dict) -> Task:
    """Acepta un issue de la API de Sentry o el payload de un webhook (`data.issue`/`data.event`)."""
    issue = payload.get("data", {}).get("issue") or payload.get("data", {}).get("event") or payload
    title = issue.get("title") or issue.get("message") or "Error de Sentry"
    culprit = issue.get("culprit", "")
    meta = issue.get("metadata", {}) or {}
    body = "\n".join(
        x
        for x in [
            f"Error reportado por Sentry: {title}",
            f"Ubicación (culprit): {culprit}" if culprit else "",
            f"Tipo: {meta.get('type')}" if meta.get("type") else "",
            f"Valor: {meta.get('value')}" if meta.get("value") else "",
            f"Archivo: {meta.get('filename')}" if meta.get("filename") else "",
            f"Ocurrencias: {issue.get('count')}" if issue.get("count") else "",
            f"Usuarios afectados: {issue.get('userCount')}" if issue.get("userCount") else "",
        ]
        if x
    ) + "\n\nReproduce el error con un test que falle, corrígelo y verifica que el test pasa."
    return Task(
        source=TaskSource.SENTRY,
        external_id=str(issue.get("id") or issue.get("shortId") or title),
        title=f"[Sentry] {title}"[:120],
        body=body,
        url=issue.get("permalink") or issue.get("web_url", ""),
        metadata={"level": issue.get("level"), "culprit": culprit},
    )


def fetch_sentry_issues(org: str, project: str, limit: int = 10) -> list[Task]:
    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        raise RuntimeError("Falta SENTRY_AUTH_TOKEN")
    base = os.environ.get("SENTRY_URL", "https://sentry.io").rstrip("/")
    url = f"{base}/api/0/projects/{org}/{project}/issues/?query=is:unresolved&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        issues = json.loads(r.read())
    return [task_from_sentry_event(i) for i in issues]
