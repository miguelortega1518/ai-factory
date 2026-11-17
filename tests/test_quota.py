from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from ai_factory.intake import task_from_github_issue
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory
from ai_factory.quota import (
    QuotaExhausted,
    check,
    pending_attempts,
    retry_paused,
    retry_time,
)

from .test_claude_code import _setup
from .test_issue_feedback import ISSUE, GhRecorder

REAL = ("You've hit your org's monthly spend limit · ask your admin to raise it at "
        "https://claude.ai/admin-settings/usage · your 5-hour limit resets {when} (UTC)")


def at(h, m=0, day=17):
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


@pytest.mark.parametrize("when,now,expected", [
    ("7:50am", at(4, 9), at(7, 55)),           # más tarde el mismo día
    ("10pm", at(18, 24), at(22, 5)),           # pm
    ("6am", at(7, 0), at(6, 5, day=18)),       # ya pasó hoy: mañana
    ("12am", at(23, 0), at(0, 5, day=18)),     # medianoche
    ("12:30pm", at(9, 0), at(12, 35)),         # mediodía
])
def test_retry_time_parses_the_reset_hour(when, now, expected):
    assert retry_time(REAL.format(when=when), now) == expected


def test_retry_time_other_formats():
    now = at(10)
    assert retry_time("todo bien", now) is None
    assert retry_time("error_max_turns: sin turnos", now) is None
    # Límite sin hora: se espera una hora.
    assert retry_time("Claude AI usage limit reached", now) == now + timedelta(hours=1)
    # Formato con epoch.
    epoch = int(at(12).timestamp())
    assert retry_time(f"Claude AI usage limit reached|{epoch}", now) == at(12, 5)
    # Zona horaria con nombre IANA (7am en Santo Domingo = 11:00 UTC).
    msg = "You've hit your usage limit · resets 7am (America/Santo_Domingo)"
    assert retry_time(msg, now) == at(11, 5)


def test_check_raises_only_for_limits():
    check("builder", "fallo cualquiera")
    with pytest.raises(QuotaExhausted) as e:
        check("builder", REAL.format(when="7:50am"))
    assert e.value.retry_at.tzinfo is not None and "builder" in str(e.value)


def test_quota_pauses_the_run_and_labels_the_issue(repo, cfg, tmp_path):
    _setup(cfg, tmp_path, mode="quota")
    cfg.github_repo = "o/r"
    gh = GhRecorder()
    state = Factory(cfg, client=None, log=lambda *_: None, runner=gh).run(task_from_github_issue(ISSUE, "o/r"))
    assert state.status == RunStatus.FAILED
    assert state.retry_at and datetime.fromisoformat(state.retry_at) > datetime.now(UTC)
    [comment] = gh.comments()
    assert "En espera por el límite de uso" in comment and "relanzará sola" in comment
    assert f'"retry_at": "{state.retry_at}"' in comment
    labels = [p for a, p in gh.calls if a[-3].endswith("/labels")]
    assert labels == [{"labels": ["ai-factory-en-espera"]}]
    assert not gh.closed()


def _comment(retry_at=None, status="failed"):
    data = json.dumps({"status": status, "retry_at": retry_at})
    return {"body": f"texto\n<!-- ai-factory:{data} -->"}


class FakeGh:
    """Responde a `gh api` con issues y comentarios de prueba y registra las escrituras."""

    def __init__(self, issues: dict[int, list[dict]]):
        self.issues = issues
        self.writes: list[tuple[list[str], dict]] = []

    def __call__(self, args, input=None, **_):
        path = next(a for a in args if a.startswith("repos/"))
        if "-X" in args:
            self.writes.append((args, json.loads(input) if input else {}))
            out = ""
        elif path.endswith("/comments?per_page=100"):
            out = json.dumps(self.issues[int(path.split("/")[4])])
        else:
            out = json.dumps([{"number": n} for n in self.issues])
        return subprocess.CompletedProcess(args, 0, out, "")


def test_pending_attempts_counts_trailing_pauses():
    s = [{"retry_at": "x"}, {"status": "rejected"}, {"retry_at": "y"}, {"retry_at": "z"}]
    assert pending_attempts(s) == 2
    assert pending_attempts([]) == 0


def test_retry_paused_relaunches_waits_gives_up_and_clears(cfg):
    cfg.github_repo, cfg.base_branch, cfg.quota_max_retries = "o/r", "master", 2
    now = at(10)
    gh = FakeGh({
        1: [_comment(at(9).isoformat())],                                     # ya toca: relanzar
        2: [_comment(at(11).isoformat())],                                    # aún no
        3: [_comment(at(8).isoformat())] * 3,                                 # 3 pausas seguidas: se rinde
        4: [_comment(at(8).isoformat()), _comment(status="shipped")],         # ya no está en espera
    })
    results = retry_paused(cfg, now=now, run=gh, log=lambda *_: None)
    assert {r["issue"]: r["action"] for r in results} == {1: "relaunch", 2: "wait", 3: "give_up", 4: "clear"}

    dispatch = [(a, p) for a, p in gh.writes if a[-3].endswith("/dispatches")]
    assert dispatch == [(dispatch[0][0], {"ref": "master", "inputs": {"issue": "1"}})]
    assert "repos/o/r/actions/workflows/ai-factory.yml/dispatches" in dispatch[0][0]
    removed = sorted(int(a[-1].split("/")[4]) for a, _ in gh.writes if "DELETE" in a)
    assert removed == [1, 3, 4]
    bodies = {int(a[-3].split("/")[4]): p["body"] for a, p in gh.writes if a[-3].endswith("/comments")}
    assert "relanzo la tarea" in bodies[1] and "no la relanzo más" in bodies[3] and 2 not in bodies


def test_retry_paused_dry_run_writes_nothing(cfg):
    cfg.github_repo = "o/r"
    gh = FakeGh({1: [_comment(at(9).isoformat())]})
    [r] = retry_paused(cfg, now=at(10), dry_run=True, run=gh, log=lambda *_: None)
    assert r["action"] == "relaunch" and gh.writes == []
