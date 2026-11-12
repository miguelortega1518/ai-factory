"""Tester funcional: arranca la app de verdad y la prueba (con un agente simulado)."""

from __future__ import annotations

import json
import sys

from ai_factory.config import QAConfig
from ai_factory.intake import task_from_github_issue, task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient, git
from .test_issue_feedback import ISSUE, GhRecorder

FAIL = {"passed": False, "summary": "El total no aparece",
        "checks": [{"criterion": "add(2,3)==5 visible", "status": "fail", "evidence": "la página muestra 4"}]}


def _qa(cfg, tmp_path, **kw):
    cfg.qa = QAConfig(enabled=True, start_command=f'{sys.executable} -m http.server "$PORT" --bind 127.0.0.1',
                      port=kw.pop("port", 38123), ready_path="/", ready_timeout=20,
                      setup_command=f'echo listo > "{tmp_path}/db-setup.txt"', **kw)


def test_qa_runs_against_the_live_app_and_feeds_reviewers(repo, cfg, tmp_path):
    _qa(cfg, tmp_path)
    client = FakeClient(files=GOOD_FILES)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED, state.error
    assert client.calls.index("qa") < client.calls.index("reviewer_ui")  # antes de los revisores
    assert "calc.py" in client.qa_seen[0]  # la app servía el worktree con el cambio
    assert (tmp_path / "db-setup.txt").exists()
    assert (cfg.runs_dir / state.run_id / "qa" / "home.png").exists()
    ui = client.ui_prompts[0]  # motor api: la captura llega como imagen al revisor de UI
    assert isinstance(ui, list) and any(b.get("type") == "image" for b in ui)
    assert "Tester funcional" in ui[0]["text"]
    assert "qa" in state.stages and state.qa.passed


def test_qa_failure_sends_task_back_to_builder(repo, cfg, tmp_path):
    _qa(cfg, tmp_path, port=38124)
    client = FakeClient(files=GOOD_FILES, qa_reports=[FAIL, {}])
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and state.iterations == 2
    first = state.history[0]
    assert first.qa and not first.qa.passed and first.reviews == []  # no pasó a revisión
    assert any("la página muestra 4" in c for c in first.required_changes)
    assert client.calls.count("qa") == 2


def test_qa_skipped_when_change_does_not_touch_qa_paths(repo, cfg, tmp_path):
    _qa(cfg, tmp_path, paths=["src/app/**"])
    client = FakeClient(files=GOOD_FILES)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and "qa" not in client.calls and state.qa is None


def test_app_that_does_not_start_counts_as_a_failure_of_the_change(repo, cfg, tmp_path):
    _qa(cfg, tmp_path)
    cfg.qa.start_command, cfg.qa.ready_timeout, cfg.max_iterations = "echo arrancando; exit 3", 5, 1
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.REJECTED
    assert "no arranca" in state.history[0].required_changes[0] and "arrancando" in state.history[0].required_changes[0]


def test_broken_qa_infrastructure_stops_the_task(repo, cfg, tmp_path):
    _qa(cfg, tmp_path)
    cfg.qa.setup_command = "exit 9"
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.FAILED and "qa.setup_command" in state.error
    assert not (repo / "calc.py").exists()


def test_tester_cannot_change_app_code(repo, cfg, tmp_path):
    _qa(cfg, tmp_path)
    state = Factory(cfg, FakeClient(files=GOOD_FILES, qa_touches_code=True), log=lambda *_: None).run(
        task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED
    assert "return a + b" in (repo / "calc.py").read_text()  # lo del tester se deshizo
    assert "touched_code_reverted" in (cfg.runs_dir / state.run_id / "events.jsonl").read_text()


def test_qa_section_in_issue_comment(repo, cfg, tmp_path):
    _qa(cfg, tmp_path, port=38125)
    cfg.github_repo = "o/r"
    gh = GhRecorder()
    Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None, runner=gh).run(task_from_github_issue(ISSUE, "o/r"))
    body = gh.comments()[-1]
    assert "🧪 Tester funcional (pasó)" in body and "1 captura(s)" in body
    import re
    assert json.loads(re.search(r"<!-- ai-factory:(\{.*\}) -->", body).group(1))["qa"] == {
        "passed": True, "checks": 1, "blocked": False}


def test_blocked_tester_does_not_bounce_to_builder(repo, cfg, tmp_path):
    _qa(cfg, tmp_path, port=38126)
    blocked = {"passed": False, "blocked": True, "summary": "Me denegaron ejecutar node", "checks": []}
    client = FakeClient(files=GOOD_FILES, qa_reports=[blocked])
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and state.iterations == 1  # siguió a revisión
    assert "reviewer_ui" in client.calls
    assert "NO PUDO PROBAR" in client.ui_prompts[0][0]["text"]
    assert '"blocked"' in (cfg.runs_dir / state.run_id / "events.jsonl").read_text()


def test_occupied_port_stops_the_task(repo, cfg, tmp_path):
    import subprocess
    other = subprocess.Popen([sys.executable, "-m", "http.server", "38127", "--bind", "127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time, urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                opener.open("http://127.0.0.1:38127/", timeout=1); break
            except OSError:
                time.sleep(0.1)
        _qa(cfg, tmp_path, port=38127)
        state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
        assert state.status == RunStatus.FAILED and "ya está ocupado" in state.error
    finally:
        other.terminate()


def test_qa_section_also_when_awaiting_human(repo, cfg, tmp_path):
    _qa(cfg, tmp_path, port=38128)
    cfg.github_repo, cfg.core_paths = "o/r", ["calc.py"]
    gh = GhRecorder()
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None, runner=gh).run(
        task_from_github_issue(ISSUE, "o/r"))
    assert state.status == RunStatus.AWAITING_HUMAN
    assert "🧪 Tester funcional (pasó)" in gh.comments()[-1]
