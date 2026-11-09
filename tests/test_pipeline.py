from __future__ import annotations

from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient, git


def test_low_risk_change_ships_to_main(repo, cfg):
    client = FakeClient(files=GOOD_FILES)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade una función add"))

    assert state.status == RunStatus.SHIPPED, state.error
    assert (repo / "calc.py").read_text().startswith("def add")
    assert state.spec.brain_references == ["conventions/code-style.md"]
    # Orden del pipeline: spec → builder → 4 revisores → aprobador → gate → mejora continua.
    assert client.calls[0] == "spec_writer"
    assert {c for c in client.calls if c.startswith("reviewer_")} == {
        "reviewer_security", "reviewer_ux", "reviewer_ui", "reviewer_code"}
    assert client.calls[-2:] == ["gatekeeper", "improver"]
    # Las notas de la retrospectiva se guardan en el Brain y viajan con el cambio.
    notes = list((repo / ".factory" / "brain" / "conventions").glob("*utilidades*"))
    assert notes and "calc.py" in notes[0].read_text()
    # Auditoría completa
    run_dir = cfg.runs_dir / state.run_id
    assert (run_dir / "spec.json").exists() and (run_dir / "diff-1.patch").exists()
    assert "factory" in (run_dir / "events.jsonl").read_text()


def test_approver_rejection_loops_back_to_builder(repo, cfg):
    client = FakeClient(files=GOOD_FILES, approve_on=2)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED
    assert state.iterations == 2
    assert client.calls.count("builder") >= 4  # 2 turnos por iteración


def test_failing_tests_skip_review_and_retry(repo, cfg):
    broken = {**GOOD_FILES, "calc.py": "def add(a, b):\n    return a - b\n"}
    client = FakeClient(builder_rounds=[broken, GOOD_FILES])
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED
    assert state.iterations == 2
    # En la primera iteración no se llamó a ningún revisor (tests en rojo).
    first_builder_end = client.calls.index("reviewer_security") if "reviewer_security" in client.calls else -1
    assert client.calls[:first_builder_end].count("builder") == 4


def test_gives_up_after_max_iterations(repo, cfg):
    cfg.max_iterations = 2
    client = FakeClient(files=GOOD_FILES, approve_on=99)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.REJECTED
    assert not (repo / "calc.py").exists()
    assert state.branch in git(repo, "branch")


def test_core_paths_force_human_approval(repo, cfg):
    cfg.core_paths = ["calc.py"]
    client = FakeClient(files=GOOD_FILES, risk="low")
    factory = Factory(cfg, client, log=lambda *_: None)
    state = factory.run(task_from_text("Añade add"))

    assert state.status == RunStatus.AWAITING_HUMAN
    assert state.gate.risk == "critical"
    assert not (repo / "calc.py").exists()  # nada llega a main sin humano

    approved = factory.approve(state.run_id)
    assert approved.status == RunStatus.SHIPPED
    assert (repo / "calc.py").exists()


def test_critical_risk_from_gatekeeper(repo, cfg):
    client = FakeClient(files=GOOD_FILES, risk="critical")
    factory = Factory(cfg, client, log=lambda *_: None)
    state = factory.run(task_from_text("Cambia la autenticación"))
    assert state.status == RunStatus.AWAITING_HUMAN
    rejected = factory.reject(state.run_id, "no")
    assert rejected.status == RunStatus.REJECTED
    assert [s.run_id for s in factory.list_runs()] == [state.run_id]


def test_setup_command_and_env_reach_worktree(repo, cfg):
    cfg.setup_command = 'echo "$FACTORY_FLAG" > setup-marker.txt'
    cfg.env = {"FACTORY_FLAG": "preparado"}
    cfg.test_command = cfg.test_command + ' && test "$FACTORY_FLAG" = preparado'
    files = {**GOOD_FILES, ".gitignore": "setup-marker.txt\n"}
    state = Factory(cfg, FakeClient(files=files), log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED, state.error
    assert cfg.worktrees_dir.parent.name == "ai-factory-worktrees"
    assert repo not in cfg.worktrees_dir.parents  # los worktrees viven fuera del repo


def test_failing_setup_marks_run_failed(repo, cfg):
    cfg.setup_command = "exit 3"
    state = Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.FAILED
    assert "setup_command" in state.error
