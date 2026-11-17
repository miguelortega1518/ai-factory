from __future__ import annotations

from ai_factory.claude_code import _DENY
from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient, git


def test_builder_changes_to_workflows_are_reverted(repo, cfg):
    files = {**GOOD_FILES, ".github/workflows/nuevo.yml": "on: push\n"}
    logs: list[str] = []
    state = Factory(cfg, FakeClient(files=files), log=logs.append).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED
    shipped = git(repo, "diff", "--name-only", f"{state.commit}^1", state.commit)
    assert "calc.py" in git(repo, "ls-files")
    assert ".github/workflows/nuevo.yml" not in git(repo, "ls-files")
    assert ".github/workflows/nuevo.yml" not in shipped
    assert any("Rutas protegidas" in line and "nuevo.yml" in line for line in logs)
    events = (cfg.runs_dir / state.run_id / "events.jsonl").read_text()
    assert "protected_paths_reverted" in events


def test_existing_workflow_is_restored(repo, cfg):
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ci")
    files = {**GOOD_FILES, ".github/workflows/ci.yml": "on: pull_request\n"}
    state = Factory(cfg, FakeClient(files=files), log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED
    assert (repo / ".github" / "workflows" / "ci.yml").read_text() == "on: push\n"


def test_claude_code_denies_writing_workflows():
    assert "Edit(.github/workflows/**)" in _DENY and "Write(.github/workflows/**)" in _DENY
