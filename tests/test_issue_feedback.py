from __future__ import annotations

import json
import subprocess

from ai_factory.intake import task_from_github_issue
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient

ISSUE = {"number": 42, "title": "Añade add", "body": "Necesito add()", "url": "https://github.com/o/r/issues/42"}


class GhRecorder:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail = fail

    def __call__(self, args, input=None, **_):
        self.calls.append((args, json.loads(input or "{}")))
        return subprocess.CompletedProcess(args, 1 if self.fail else 0, "", "403 Forbidden" if self.fail else "")

    def comments(self):
        return [p["body"] for a, p in self.calls if any(x.endswith("/comments") for x in a)]

    def closed(self):
        return any("PATCH" in a and p.get("state") == "closed" for a, p in self.calls)


def _run(cfg, gh, **client_kw):
    cfg.github_repo = "o/r"
    factory = Factory(cfg, FakeClient(files=GOOD_FILES, **client_kw), log=lambda *_: None, runner=gh)
    return factory, factory.run(task_from_github_issue(ISSUE, "o/r"))


def test_shipped_comments_with_commit_and_closes(repo, cfg):
    gh = GhRecorder()
    _, state = _run(cfg, gh)
    assert state.status == RunStatus.SHIPPED and len(state.commit) == 40
    [comment] = gh.comments()
    assert "Resuelto por la AI Factory" in comment and state.commit in comment
    assert "security" in comment and state.run_id in comment
    assert gh.closed()
    assert all(a[:2] == ["gh", "api"] and any(x.startswith("repos/o/r/issues/42") for x in a)
               for a, _ in gh.calls)


def test_awaiting_human_comments_without_closing(repo, cfg):
    gh = GhRecorder()
    cfg.core_paths = ["calc.py"]
    factory, state = _run(cfg, gh)
    assert state.status == RunStatus.AWAITING_HUMAN
    [comment] = gh.comments()
    assert "Necesita aprobación humana" in comment and "calc.py" in comment
    assert f"factory approve {state.run_id}" in comment
    assert not gh.closed()
    # Al aprobarlo (sin remoto: fusión local) se informa y se cierra.
    factory.approve(state.run_id)
    assert "Resuelto por la AI Factory" in gh.comments()[-1] and gh.closed()


def test_rejected_and_failed_leave_issue_open(repo, cfg):
    gh = GhRecorder()
    cfg.max_iterations = 1
    _, state = _run(cfg, gh, approve_on=99)
    assert state.status == RunStatus.REJECTED
    assert "no consiguió un cambio aprobado" in gh.comments()[-1]
    assert "Añade test con negativos" in gh.comments()[-1]
    cfg.setup_command = "exit 7"
    _, state = _run(cfg, gh)
    assert state.status == RunStatus.FAILED and "falló" in gh.comments()[-1]
    assert not gh.closed()


def test_feedback_errors_do_not_break_the_run(repo, cfg):
    gh = GhRecorder(fail=True)
    _, state = _run(cfg, gh)
    assert state.status == RunStatus.SHIPPED
    events = (cfg.runs_dir / state.run_id / "events.jsonl").read_text()
    assert "issue_feedback_error" in events and "403" in events


def test_manual_tasks_and_disabled_feedback_make_no_calls(repo, cfg):
    gh = GhRecorder()
    cfg.issue_feedback = False
    _run(cfg, gh)
    assert gh.calls == []


def test_pr_body_closes_issue(repo, cfg):
    from ai_factory import issue_feedback
    from ai_factory.models import RunState

    cfg.github_repo = "o/r"
    st = RunState(run_id="x", task=task_from_github_issue(ISSUE, "o/r"))
    assert issue_feedback.closes_line(st, cfg) == "Closes #42"
    cfg.github_repo = "otro/repo"
    assert issue_feedback.closes_line(st, cfg) == "Closes o/r#42"
    assert "Closes o/r#42" in Factory(cfg, None, log=lambda *_: None)._pr_body(st)
