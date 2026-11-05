"""Entrada unificada (paso 1): todas las fuentes producen el mismo `Task`."""

from .github import fetch_github_issues, task_from_github_issue
from .manual import task_from_text
from .sentry import fetch_sentry_issues, task_from_sentry_event

__all__ = [
    "fetch_github_issues",
    "fetch_sentry_issues",
    "task_from_github_issue",
    "task_from_sentry_event",
    "task_from_text",
]
