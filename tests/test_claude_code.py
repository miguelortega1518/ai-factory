from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_factory.claude_code import bash_rules, subprocess_env
from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SRC = str(Path(__file__).parents[1] / "src")


def _setup(cfg, tmp_path, mode="ok"):
    log = tmp_path / "claude-calls.jsonl"
    cfg.backend = "claude-code"
    cfg.claude_bin = str(FAKE)
    cfg.allowed_commands = ["npm", "git"]
    cfg.env = {"FAKE_CLAUDE_LOG": str(log), "FAKE_SRC": SRC, "FAKE_MODE": mode,
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test", "ANTHROPIC_API_KEY": "no-debe-usarse"}
    cfg.agents["reviewer_ui"].model = "claude-haiku-5-5"
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()]


def test_full_pipeline_runs_on_claude_code(repo, cfg, tmp_path):
    log = _setup(cfg, tmp_path)
    state = Factory(cfg, client=None, log=lambda *_: None).run(task_from_text("Añade add"))

    assert state.status == RunStatus.SHIPPED, state.error
    assert (repo / "calc.py").exists()
    calls = _calls(log)
    roles = [c["role"] for c in calls]
    assert roles[0] == "spec_writer" and roles[1] == "builder" and roles[-1] == "gatekeeper"
    assert {r for r in roles if r.startswith("reviewer_")} == {
        "reviewer_security", "reviewer_ux", "reviewer_ui", "reviewer_code"}

    by_role = {c["role"]: c["args"] for c in calls}
    def opt(args, name):
        return args[args.index(name) + 1]
    # Modelo por agente
    assert opt(by_role["reviewer_ui"], "--model") == "claude-haiku-5-5"
    assert opt(by_role["spec_writer"], "--model") == "claude-opus-5-5"
    # Sólo el Builder puede escribir y ejecutar; nadie pregunta permisos
    assert opt(by_role["spec_writer"], "--tools") == "Read,Glob,Grep"
    assert opt(by_role["reviewer_security"], "--tools") == "Read,Glob,Grep"
    assert "Edit" in opt(by_role["builder"], "--tools") and "Bash" in opt(by_role["builder"], "--tools")
    assert "Bash(git push *)" in opt(by_role["builder"], "--disallowedTools")
    assert all(opt(a, "--permission-mode") == "dontAsk" for a in by_role.values())
    assert "--allowedTools" not in by_role["approver"]
    # Con token de suscripción, la API key nunca llega al CLI
    assert not any(c["env_has_api_key"] for c in calls)
    # El esquema de salida coincide con el modelo Pydantic y el Brain se indica en el prompt
    assert "acceptance_criteria" in opt(by_role["spec_writer"], "--json-schema")
    assert ".factory/brain/" in opt(by_role["spec_writer"], "--append-system-prompt")
    # Auditoría: transcripciones y coste equivalente
    run_dir = cfg.runs_dir / state.run_id
    assert list(run_dir.glob("builder-*.jsonl"))
    events = (run_dir / "events.jsonl").read_text()
    assert '"backend": "claude-code"' in events and "cost_usd_equivalent" in events


def test_claude_code_error_fails_run(repo, cfg, tmp_path):
    _setup(cfg, tmp_path, mode="max_turns")
    state = Factory(cfg, client=None, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.FAILED
    assert "error_max_turns" in state.error


def test_bash_rules_keep_git_read_only():
    rules = bash_rules(["npm", "git"])
    assert "Bash(npm *)" in rules and "Bash(git diff *)" in rules
    assert not any("git push" in r or "git commit" in r for r in rules)


def test_api_key_kept_without_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert subprocess_env({"ANTHROPIC_API_KEY": "k"})["ANTHROPIC_API_KEY"] == "k"
    assert "ANTHROPIC_API_KEY" not in subprocess_env({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"})


def test_safe_shell_helpers_allowed_for_compound_commands():
    rules = bash_rules(["python"])
    assert "Bash(echo *)" in rules and "Bash(python *)" in rules
    assert len(rules) == len(set(rules))


def test_approver_receives_factory_test_verification(repo, cfg, tmp_path):
    _setup(cfg, tmp_path)
    Factory(cfg, client=None, log=lambda *_: None).run(task_from_text("Añade add"))
    run_dir = next(cfg.runs_dir.iterdir())
    starts = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
    prompt = next(e["prompt"] for e in starts if e["agent"] == "approver" and e["kind"] == "start")
    assert "Verificación ejecutada por la fábrica" in prompt and "passed" in prompt
