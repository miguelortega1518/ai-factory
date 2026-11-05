"""Intake desde GitHub Issues (vía `gh`, que ya gestiona autenticación)."""

from __future__ import annotations

import json
import subprocess

from ..models import Task, TaskSource


def task_from_github_issue(issue: dict, repo: str = "") -> Task:
    labels = [l["name"] if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
    return Task(
        source=TaskSource.GITHUB,
        external_id=f"{repo}#{issue['number']}" if repo else str(issue["number"]),
        title=issue["title"],
        body=issue.get("body") or "",
        url=issue.get("url") or issue.get("html_url", ""),
        labels=labels,
        metadata={"repo": repo, "number": issue["number"]},
    )


def fetch_github_issues(repo: str, label: str, limit: int = 10) -> list[Task]:
    cmd = [
        "gh", "issue", "list", "--repo", repo, "--label", label, "--state", "open",
        "--limit", str(limit), "--json", "number,title,body,url,labels",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh issue list falló: {r.stderr.strip()}")
    return [task_from_github_issue(i, repo) for i in json.loads(r.stdout or "[]")]
