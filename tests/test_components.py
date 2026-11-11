from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_factory.audit import AuditLog
from ai_factory.brain import Brain
from ai_factory.config import AgentConfig, load_config
from ai_factory.intake import task_from_github_issue, task_from_sentry_event, task_from_text
from ai_factory.llm import Agent, AgentError
from ai_factory.models import TaskSource, Verdict
from ai_factory.tools import Workspace


def test_intake_sources_produce_unified_task():
    gh = task_from_github_issue({"number": 7, "title": "Bug", "body": "x", "url": "u", "labels": [{"name": "bug"}]}, "o/r")
    se = task_from_sentry_event({"data": {"issue": {"id": "42", "title": "TypeError: x", "culprit": "app.py",
                                                     "metadata": {"type": "TypeError"}}}})
    mt = task_from_text("Añade dark mode\nmás detalles")
    assert (gh.source, gh.external_id, gh.labels) == (TaskSource.GITHUB, "o/r#7", ["bug"])
    assert se.source == TaskSource.SENTRY and "app.py" in se.body and se.title.startswith("[Sentry]")
    assert mt.title == "Añade dark mode"
    assert len({gh.id, se.id, mt.id}) == 3


def test_workspace_is_sandboxed(tmp_path):
    ws = Workspace(tmp_path, ["ls"])
    ws.write_file("a/b.txt", "hola")
    assert ws.read_file("a/b.txt") == "hola"
    ws.edit_file("a/b.txt", "hola", "adiós")
    assert ws.read_file("a/b.txt") == "adiós"
    with pytest.raises(PermissionError):
        ws.read_file("../fuera.txt")
    with pytest.raises(PermissionError):
        ws.run_command("rm -rf /")
    with pytest.raises(PermissionError):
        ws.write_file(".git/config", "x")


def test_brain_search_and_add(tmp_path):
    b = Brain(tmp_path / "brain")
    b.init()
    b.add("decisions", "Usar PostgreSQL", "Elegimos PostgreSQL por JSONB.")
    assert "PostgreSQL" in b.search("postgresql jsonb")
    assert "decisions/" in b.index()
    with pytest.raises(ValueError):
        b.add("otra", "x", "y")


def test_config_loads_agents_override(tmp_path):
    (tmp_path / "factory.toml").write_text('max_iterations = 5\n[agents.reviewer_ui]\nmodel = "claude-haiku-5-5"\n')
    cfg = load_config(tmp_path)
    assert cfg.max_iterations == 5
    assert cfg.agent("reviewer_ui").model == "claude-haiku-5-5"
    assert cfg.agent("approver").model == "claude-opus-5-5"
    (tmp_path / "factory.toml").write_text("typo_key = 1\n")
    with pytest.raises(ValueError):
        load_config(tmp_path)


class _Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)

    def create(self, **_):
        return self.responses.pop(0)


def _agent(tmp_path, client):
    return Agent("t", "sys", AgentConfig("m"), client, AuditLog(tmp_path / "run"))


def test_agent_retries_invalid_result_then_accepts(tmp_path):
    bad = {"type": "tool_use", "id": "1", "name": "submit_result", "input": {"approved": "quizá"}}
    good = {"type": "tool_use", "id": "2", "name": "submit_result", "input": {"approved": True, "reasoning": "ok"}}
    client = _Scripted(SimpleNamespace(stop_reason="tool_use", content=[bad], usage=None),
                       SimpleNamespace(stop_reason="tool_use", content=[good], usage=None))
    assert _agent(tmp_path, client).run("x", Verdict).approved is True


def test_agent_raises_on_refusal(tmp_path):
    client = _Scripted(SimpleNamespace(stop_reason="refusal", content=[], usage=None, stop_details=None))
    with pytest.raises(AgentError):
        _agent(tmp_path, client).run("x", Verdict)


def test_builder_can_delete_files_inside_workspace(tmp_path):
    ws = Workspace(tmp_path, [])
    ws.write_file("tests/extra.py", "x")
    assert ws.delete_file("tests/extra.py") == "Borrado tests/extra.py"
    assert not (tmp_path / "tests" / "extra.py").exists()
    with pytest.raises(PermissionError):
        ws.delete_file("../fuera.txt")
    with pytest.raises(FileNotFoundError):
        ws.delete_file("no-existe.py")
    assert "delete_file" in [t.name for t in ws.write_tools()]
