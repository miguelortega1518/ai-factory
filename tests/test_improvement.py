from __future__ import annotations

import json
import re

from ai_factory.improve import issue_body, run_improvement
from ai_factory.intake import task_from_github_issue, task_from_text
from ai_factory.models import Proposal, RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient
from .test_issue_feedback import ISSUE, GhRecorder

PROPOSAL = {
    "title": "Añadir tests a rate-limit-policy", "category": "tests", "priority": "media", "effort": "S",
    "problem": "La política de rate limit no tiene tests.", "evidence": ["src/lib/rate-limit-policy.ts:1"],
    "proposal": "Cubrir los límites por ruta.", "acceptance_criteria": ["Tests de cada límite"],
}


# --- Retrospectiva tras cada tarea -------------------------------------------------------

def test_retro_updates_brain_and_sees_history(repo, cfg):
    client = FakeClient(files=GOOD_FILES, approve_on=2)
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and state.retro is not None
    # La nota de la retrospectiva se fusionó con el cambio
    notes = list((repo / ".factory" / "brain" / "conventions").glob("*utilidades*"))
    assert notes and "calc.py" in notes[0].read_text() and state.run_id in notes[0].read_text()
    # Vio las dos iteraciones, el rechazo del Aprobador y los tiempos
    prompt = client.retro_prompts[0]
    assert "Iteración 1" in prompt and "Iteración 2" in prompt and "Añade test con negativos" in prompt
    assert len(state.history) == 2 and state.history[0].approved is False
    assert "retro" in state.stages


def test_retro_in_issue_comment(repo, cfg):
    gh = GhRecorder()
    cfg.github_repo = "o/r"
    Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None, runner=gh).run(task_from_github_issue(ISSUE, "o/r"))
    body = gh.comments()[0]
    assert "Mejora continua" in body and "Añadido al Brain" in body and "Sugerencias para la fábrica" in body
    data = json.loads(re.search(r"<!-- ai-factory:(\{.*\}) -->", body).group(1))
    assert data["retro"]["brain_updates"] == 1


def test_rejected_retro_is_reported_but_not_applied(repo, cfg):
    gh = GhRecorder()
    cfg.github_repo, cfg.max_iterations = "o/r", 1
    state = Factory(cfg, FakeClient(files=GOOD_FILES, approve_on=99), log=lambda *_: None, runner=gh).run(
        task_from_github_issue(ISSUE, "o/r"))
    assert state.status == RunStatus.REJECTED and state.retro is not None
    assert "no se aplicó" in gh.comments()[0]
    assert not list((repo / ".factory" / "brain" / "conventions").glob("*utilidades*"))


def test_retro_failure_never_breaks_the_run(repo, cfg):
    state = Factory(cfg, FakeClient(files=GOOD_FILES, retro_fails=True), log=lambda *_: None).run(
        task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and state.retro is None
    assert "retro_error" in (cfg.runs_dir / state.run_id / "events.jsonl").read_text()


def test_retro_can_be_disabled(repo, cfg):
    cfg.retro = False
    client = FakeClient(files=GOOD_FILES)
    Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert "improver" not in client.calls


# --- Revisión periódica del producto ------------------------------------------------------

class IssuesGh(GhRecorder):
    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def __call__(self, args, input=None, **kw):
        if "--jq" in args and args[args.index("--jq") + 1] == "[.[].title]":
            import subprocess
            self.calls.append((args, {}))
            return subprocess.CompletedProcess(args, 0, json.dumps(self.existing), "")
        r = super().__call__(args, input=input, **kw)
        if "issues" in args[4] if len(args) > 4 else False:
            r.stdout = "https://github.com/o/r/issues/99"
        return r


def test_improve_creates_proposal_issues_without_factory_label(repo, cfg):
    cfg.github_repo = "o/r"
    gh = IssuesGh(existing=["Arreglar fechas UTC"])
    dup = {**PROPOSAL, "title": "Arreglar fechas  UTC"}
    second = {**PROPOSAL, "title": "Validar el tamaño de la evidencia"}
    third = {**PROPOSAL, "title": "Otra mejora más"}
    client = FakeClient(proposals=[dup, PROPOSAL, second, third])
    out = run_improvement(cfg, client, max_items=2, run=gh, log=lambda *_: None)
    assert [o["title"] for o in out] == [PROPOSAL["title"], second["title"]]  # sin duplicado, máximo 2
    created = [p for a, p in gh.calls if a[-4:-2] == ["--input", "-"] and "title" in p]
    assert len(created) == 2
    for p in created:
        assert p["labels"] == ["ai-factory-propuesta"] and "ai-factory" not in p["labels"]
        assert "añade la etiqueta `ai-factory`" in p["body"]


def test_improve_dry_run_creates_nothing(repo, cfg):
    cfg.github_repo = "o/r"
    gh = IssuesGh(existing=[])
    out = run_improvement(cfg, FakeClient(proposals=[PROPOSAL]), dry_run=True, run=gh, log=lambda *_: None)
    assert len(out) == 1 and out[0]["url"] == ""
    assert not any("POST" in a for a, _ in gh.calls)


def test_issue_body_flags_core_changes(cfg):
    body = issue_body(Proposal(**{**PROPOSAL, "touches_core": True}), cfg)
    assert "rutas núcleo" in body and "`src/lib/rate-limit-policy.ts:1`" in body
    assert "ai-factory-proposal:" in body
