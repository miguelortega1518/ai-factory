"""Otra tarea fusiona en la rama base mientras la fábrica trabaja (lo que le pasó al issue #10)."""

from __future__ import annotations

import subprocess

from ai_factory.intake import task_from_text
from ai_factory.models import RunStatus
from ai_factory.pipeline import Factory

from .conftest import GOOD_FILES, FakeClient, git


def _with_remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "o@e.com")
    git(other, "config", "user.name", "Otra tarea")
    return remote, other


class RaceClient(FakeClient):
    """Cuando el Gatekeeper decide, otra tarea ya ha subido un commit a origin/main."""

    def __init__(self, other, content: str, **kw):
        super().__init__(**kw)
        self.other, self.content = other, content

    def create(self, **p):
        if p["system"].startswith(__import__("ai_factory.agents.prompts", fromlist=["x"]).GATEKEEPER):
            (self.other / "otra.py").write_text(self.content)
            git(self.other, "add", "-A")
            git(self.other, "commit", "-q", "-m", "otra tarea")
            git(self.other, "push", "-q", "origin", "main")
        return super().create(**p)


def test_base_moved_meanwhile_is_merged_reverified_and_pushed(repo, cfg, tmp_path):
    remote, other = _with_remote(repo, tmp_path)
    cfg.push = True
    logs = []
    client = RaceClient(other, "VALOR = 1\n", files=GOOD_FILES)
    state = Factory(cfg, client, log=logs.append).run(task_from_text("Añade add"))

    assert state.status == RunStatus.SHIPPED, state.error
    assert any("avanzó mientras se trabajaba" in line for line in logs)
    # En el remoto están las dos cosas: el commit de la otra tarea y el cambio de la fábrica
    files = git(repo, "ls-tree", "-r", "--name-only", "origin/main").split()
    assert "otra.py" in files and "calc.py" in files
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "origin/main")


def test_base_change_that_breaks_tests_is_not_pushed(repo, cfg, tmp_path):
    remote, other = _with_remote(repo, tmp_path)
    cfg.push = True
    # La otra tarea sube un test que falla: la combinación no se puede fusionar.
    client = RaceClient(other, "", files=GOOD_FILES)
    (other / "tests").mkdir(exist_ok=True)
    client.content = ""
    orig = client.create

    def create(**p):
        (other / "tests" / "test_roto.py").write_text("def test_roto():\n    assert False\n")
        return orig(**p)

    client.create = create
    state = Factory(cfg, client, log=lambda *_: None).run(task_from_text("Añade add"))
    assert state.status == RunStatus.FAILED
    assert "verificación falla" in state.error
    files = git(repo, "ls-tree", "-r", "--name-only", "origin/main").split()
    assert "calc.py" not in files  # nada sin verificar llegó a la base
