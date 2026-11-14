"""Registro del trabajo de la fábrica en Forja (con un cliente de Forja simulado)."""

from __future__ import annotations

from ai_factory.config import ForjaConfig
from ai_factory.forja import ForjaError, ForjaTracker, forja_title, kind_for
from ai_factory.intake import task_from_github_issue, task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient
from .test_issue_feedback import ISSUE, GhRecorder


class FakeForja:
    def __init__(self, existing=None, fail_on=None):
        self.calls, self.tasks, self.fail_on = [], dict(existing or {}), fail_on

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise ForjaError(f"{what} falló")

    def login(self, email, password):
        self.calls.append(("login", email)); self._maybe_fail("login")
        return {"id": "bot-1", "name": "AI Factory"}

    def find_space(self, name):
        self.calls.append(("space", name)); return {"id": "sp-1", "name": name}

    def find_task(self, space_id, title):
        return self.tasks.get(title)

    def create_task(self, **f):
        self.calls.append(("create", f)); t = {"id": "t-1", "status": "no_iniciada", **f}
        self.tasks[f["title"]] = t; return t

    def set_status(self, task_id, status, **extra):
        self.calls.append(("status", status, {k: v for k, v in extra.items() if v is not None}))
        self._maybe_fail(f"status:{status}")

    def update(self, task_id, **f):
        self.calls.append(("update", f))

    def comment(self, task_id, content):
        self.calls.append(("comment", content))


def _factory(cfg, fake, monkeypatch, **client_kw):
    cfg.forja = ForjaConfig(enabled=True, url="https://forja.example", project="AI Factory")
    monkeypatch.setenv("FORJA_EMAIL", "bot@x"); monkeypatch.setenv("FORJA_PASSWORD", "secreta")
    monkeypatch.setattr("ai_factory.forja.ForjaClient", lambda url: fake)
    return Factory(cfg, FakeClient(files=GOOD_FILES, **client_kw), log=lambda *_: None, runner=GhRecorder())


def statuses(fake):
    return [c[1] for c in fake.calls if c[0] == "status"]


def test_shipped_task_moves_to_done_with_link_and_summary(repo, cfg, monkeypatch):
    fake = FakeForja()
    cfg.github_repo = "o/r"
    state = _factory(cfg, fake, monkeypatch).run(task_from_github_issue(ISSUE, "o/r"))
    assert state.status == RunStatus.SHIPPED
    create = next(c[1] for c in fake.calls if c[0] == "create")
    assert create["title"] == "#42 Añade add" and create["responsibleId"] == "bot-1" and create["labels"] == ["ai-factory"]
    assert statuses(fake) == ["en_proceso", "en_revision", "completada"]
    update = next(c[1] for c in fake.calls if c[0] == "update")
    assert update["prUrl"] == f"https://github.com/o/r/commit/{state.commit}" and update["branch"] == state.branch
    assert "Resuelto por la AI Factory" in next(c[1] for c in fake.calls if c[0] == "comment")


def test_rejected_task_is_blocked_in_qa(repo, cfg, monkeypatch):
    fake = FakeForja()
    cfg.max_iterations = 1
    _factory(cfg, fake, monkeypatch, approve_on=99).run(task_from_text("Añade add"))
    assert statuses(fake)[-1] == "bloqueada"
    assert ("status", "bloqueada", {"blockedByArea": "qa"}) in fake.calls


def test_existing_task_is_reused(repo, cfg, monkeypatch):
    fake = FakeForja(existing={"#42 Añade add": {"id": "t-9", "status": "bloqueada"}})
    _factory(cfg, fake, monkeypatch).run(task_from_github_issue(ISSUE, "o/r"))
    assert not [c for c in fake.calls if c[0] == "create"]
    assert statuses(fake)[0] == "en_proceso"


def test_forja_failure_never_breaks_the_run(repo, cfg, monkeypatch):
    fake = FakeForja(fail_on="status:en_revision")
    state = _factory(cfg, fake, monkeypatch).run(task_from_text("Añade add"))
    assert state.status == RunStatus.SHIPPED and (repo / "calc.py").exists()
    assert statuses(fake) == ["en_proceso", "en_revision"]  # tras el fallo deja de intentarlo
    events = (cfg.runs_dir / state.run_id / "events.jsonl").read_text()
    assert '"agent": "forja", "kind": "error"' in events and "en_revision falló" in events


def test_missing_credentials_disable_tracking(repo, cfg, monkeypatch):
    fake = FakeForja()
    factory = _factory(cfg, fake, monkeypatch)
    logs = []
    factory.log = logs.append
    monkeypatch.delenv("FORJA_PASSWORD")
    state = factory.run(task_from_text("Añade add"))
    assert any("⚠ Forja" in line and "FORJA_PASSWORD" in line for line in logs)  # visible en el log
    assert state.status == RunStatus.SHIPPED and fake.calls == []
    assert "FORJA_PASSWORD" in (cfg.runs_dir / state.run_id / "events.jsonl").read_text()


def test_disabled_by_default_makes_no_calls(repo, cfg, monkeypatch):
    fake = FakeForja()
    monkeypatch.setattr("ai_factory.forja.ForjaClient", lambda url: fake)
    Factory(cfg, FakeClient(files=GOOD_FILES), log=lambda *_: None).run(task_from_text("Añade add"))
    assert fake.calls == []


def test_title_and_kind():
    t = task_from_github_issue({**ISSUE, "labels": [{"name": "bug"}]}, "o/r")
    assert forja_title(t) == "#42 Añade add" and kind_for(t) == "bug"
    assert kind_for(task_from_text("Corregir el error 500 del login")) == "bug"
    assert kind_for(task_from_text("Añadir filtro")) == "mejora"


def test_long_summary_is_truncated_and_status_moves_before_comment(repo, cfg, monkeypatch):
    from ai_factory.forja import ForjaClient as RealClient

    class Recording(RealClient):
        def __init__(self):
            super().__init__("https://forja.example"); self.sent = []

        def request(self, method, path, body=None):
            self.sent.append((method, path, body))
            if path.endswith("/comments"):
                assert len(body["content"]) <= self.COMMENT_MAX
            return {"id": "t-1", "status": "no_iniciada", "user": {"id": "bot-1"}} if method != "GET" else []

    client = Recording()
    client.comment("t-1", "x" * 5000)
    assert len(client.sent[-1][2]["content"]) == RealClient.COMMENT_MAX and client.sent[-1][2]["content"].endswith("…")

    fake = FakeForja()
    cfg.max_iterations = 1
    _factory(cfg, fake, monkeypatch, approve_on=99).run(task_from_text("Añade add"))
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("comment") > max(i for i, k in enumerate(kinds) if k == "status")  # estado antes que comentario
