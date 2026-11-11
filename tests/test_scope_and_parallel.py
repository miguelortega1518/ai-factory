"""Revisores con la verificación, hallazgos fuera de alcance → seguimiento, y fusión en paralelo."""

from __future__ import annotations

import json
import subprocess

from ai_factory import git_ops
from ai_factory.intake import task_from_github_issue, task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient, git
from .test_concurrent_base import _with_remote
from .test_improvement import PROPOSAL, IssuesGh
from .test_issue_feedback import ISSUE

OUT_OF_SCOPE = {"severity": "major", "file": "legacy.py:3", "description": "Falta validar la entrada",
                "out_of_scope": True}


def _events(cfg, state):
    return [json.loads(line) for line in (cfg.runs_dir / state.run_id / "events.jsonl").read_text().splitlines()]


def test_reviewers_receive_factory_verification(repo, cfg):
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
    starts = {e["agent"]: e["prompt"] for e in _events(cfg, state) if e["kind"] == "start"}
    for r in ("reviewer_security", "reviewer_ux", "reviewer_ui", "reviewer_code"):
        assert "Verificación ejecutada por la fábrica sobre este diff: PASÓ" in starts[r]
        assert "passed" in starts[r]  # salida real de pytest


def test_out_of_scope_findings_become_followup_issues(repo, cfg):
    cfg.github_repo = "o/r"
    gh = IssuesGh(existing=[])
    followup = {**PROPOSAL, "title": "Validar la entrada en legacy.py", "evidence": ["legacy.py:3"]}
    client = FakeClient(files=GOOD_FILES, findings=[OUT_OF_SCOPE], followups=[followup])
    state = Factory(cfg, client, log=lambda *_: None, runner=gh).run(task_from_github_issue(ISSUE, "o/r"))

    assert state.status == RunStatus.SHIPPED  # un hallazgo fuera de alcance no bloquea
    assert "[FUERA DE ALCANCE]" in client.retro_prompts[0]
    created = [p for a, p in gh.calls if "title" in p and "repos/o/r/issues" in a]
    assert [p["title"] for p in created] == ["Validar la entrada en legacy.py"]
    assert created[0]["labels"] == ["ai-factory-propuesta"]
    assert "hallazgo fuera de alcance" in created[0]["body"] and ISSUE["url"] in created[0]["body"]
    assert state.followup_issues == ["https://github.com/o/r/issues/99"]
    comment = gh.comments()[-1]
    assert "Tareas de seguimiento propuestas" in comment and "issues/99" in comment


def test_followups_respect_limit_and_duplicates(repo, cfg):
    cfg.github_repo, cfg.followup_issues = "o/r", 1
    gh = IssuesGh(existing=["Validar la entrada en legacy.py"])
    a = {**PROPOSAL, "title": "Validar la entrada en legacy.py"}
    b = {**PROPOSAL, "title": "Otra cosa"}
    c = {**PROPOSAL, "title": "Una tercera"}
    state = Factory(cfg, FakeClient(files=GOOD_FILES, followups=[a, b, c]), log=lambda *_: None, runner=gh).run(
        task_from_github_issue(ISSUE, "o/r"))
    created = [p["title"] for x, p in gh.calls if "title" in p and "repos/o/r/issues" in x]
    assert created == ["Otra cosa"]  # se salta el duplicado y se respeta el límite de 1
    cfg.followup_issues = 0
    gh2 = IssuesGh(existing=[])
    Factory(cfg, FakeClient(files=GOOD_FILES, followups=[b]), log=lambda *_: None, runner=gh2).run(
        task_from_github_issue(ISSUE, "o/r"))
    assert not [p for x, p in gh2.calls if "title" in p and "repos/o/r/issues" in x]


def test_push_race_is_retried(repo, cfg, tmp_path, monkeypatch):
    """Dos ejecuciones en paralelo: la otra sube justo entre nuestra fusión y nuestro push."""
    remote, other = _with_remote(repo, tmp_path)
    cfg.push = True
    real_push = git_ops.push
    raced = {"done": False}

    def racing_push(cwd, branch):
        if not raced["done"]:
            raced["done"] = True
            (other / "paralela.py").write_text("X = 1\n")
            git(other, "add", "-A")
            git(other, "commit", "-q", "-m", "tarea en paralelo")
            git(other, "push", "-q", "origin", "main")
        return real_push(cwd, branch)

    monkeypatch.setattr(git_ops, "push", racing_push)
    logs = []
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=logs.append).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED, state.error
    assert any("reintento 2/3" in line for line in logs)
    files = git(repo, "ls-tree", "-r", "--name-only", "origin/main").split()
    assert "paralela.py" in files and "calc.py" in files
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "origin/main") == state.commit
