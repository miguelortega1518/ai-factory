from __future__ import annotations

import itertools
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_factory.agents import prompts
from ai_factory.config import FactoryConfig


def _role(system: str) -> str:
    if system.startswith(prompts.QA.split("{")[0]):
        return "qa"
    for name, text in [
        ("spec_writer", prompts.SPEC_WRITER),
        ("builder", prompts.BUILDER),
        ("approver", prompts.APPROVER),
        ("gatekeeper", prompts.GATEKEEPER),
        ("improver", prompts.IMPROVER),
        ("scout", prompts.SCOUT),
        *[(f"reviewer_{k}", v) for k, v in prompts.REVIEWERS.items()],
    ]:
        if system.startswith(text):
            return name
    raise AssertionError("rol desconocido")


class FakeClient:
    """Simula la API: cada rol responde con un guion. Sustituible por escenario."""

    _ids = itertools.count()

    def __init__(self, files: dict[str, str] | None = None, approve_on: int = 1, risk: str = "low",
                 builder_rounds: list[dict[str, str]] | None = None, proposals: list[dict] | None = None,
                 retro_fails: bool = False, findings: list[dict] | None = None,
                 followups: list[dict] | None = None, qa_reports: list[dict] | None = None,
                 qa_touches_code: bool = False):
        self.qa_reports = qa_reports or []
        self.qa_touches_code = qa_touches_code
        self.qa_seen: list[str] = []
        self.ui_prompts: list = []
        self.findings = findings or []
        self.followups = followups or []
        self.proposals = proposals if proposals is not None else []
        self.retro_fails = retro_fails
        self.retro_prompts: list[str] = []
        self.calls: list[str] = []
        self.approve_on = approve_on
        self.risk = risk
        self.approver_calls = 0
        self.builder_calls = 0
        self.builder_rounds = builder_rounds or [files or {}]

    def _msg(self, *blocks, stop="tool_use"):
        return SimpleNamespace(stop_reason=stop, content=list(blocks), usage=None)

    def _use(self, name, **inp):
        return {"type": "tool_use", "id": f"t{next(self._ids)}", "name": name, "input": inp}

    def create(self, **p):
        role = _role(p["system"])
        self.calls.append(role)
        first_turn = len(p["messages"]) == 1

        if role == "spec_writer":
            if first_turn:
                return self._msg(self._use("brain_index"))
            return self._msg(self._use("submit_result", summary="Añadir add()", problem="Falta suma",
                                       acceptance_criteria=["add(2,3)==5"], technical_approach="calc.py",
                                       test_plan=["tests/test_calc.py"], brain_references=["conventions/code-style.md"]))
        if role == "builder":
            if first_turn:
                files = self.builder_rounds[min(self.builder_calls, len(self.builder_rounds) - 1)]
                self.builder_calls += 1
                uses = [self._use("write_file", path=k, content=v) for k, v in files.items()]
                return self._msg(*uses, self._use("run_command", command="ls"))
            return self._msg(self._use("submit_result", summary="hecho", tests_passing=True,
                                       files_changed=list(self.builder_rounds[0])))
        if role == "qa":
            import re
            import urllib.request
            prompt = p["messages"][0]["content"]
            url = re.search(r"La app está en (\S+)\.", prompt).group(1)
            qa_dir = re.search(r"capturas es `([^`]+)`", prompt).group(1)
            app_dir = re.search(r"código de la app está en `([^`]+)`", prompt).group(1)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            self.qa_seen.append(opener.open(url, timeout=5).read().decode())  # la app responde de verdad
            from pathlib import Path as _P
            (_P(qa_dir) / "screenshots" / "home.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
            if self.qa_touches_code:
                (_P(app_dir) / "calc.py").write_text("roto = True\n")
            n = len(self.qa_seen) - 1
            report = self.qa_reports[min(n, len(self.qa_reports) - 1)] if self.qa_reports else {}
            return self._msg(self._use("submit_result", **{
                "applicable": True, "passed": True, "summary": "La app hace lo esperado",
                "checks": [{"criterion": "add(2,3)==5 visible", "status": "pass", "evidence": "visto"}],
                "screenshots": ["screenshots/home.png"], **report}))
        if role.startswith("reviewer_"):
            if role == "reviewer_ui":
                self.ui_prompts.append(p["messages"][0]["content"])
            return self._msg(self._use("submit_result", passed=True, summary=f"{role} ok",
                                       findings=self.findings if role == "reviewer_code" else []))
        if role == "approver":
            self.approver_calls += 1
            ok = self.approver_calls >= self.approve_on
            return self._msg(self._use("submit_result", approved=ok, reasoning="ok" if ok else "falta caso límite",
                                       required_changes=[] if ok else ["Añade test con negativos"]))
        if role == "gatekeeper":
            return self._msg(self._use("submit_result", risk=self.risk, reasoning="cambio acotado"))
        if role == "improver":
            if self.retro_fails:
                raise RuntimeError("API caída")
            self.retro_prompts.append(p["messages"][0]["content"])
            return self._msg(self._use(
                "submit_result", summary="Salió a la primera.", problems=["El revisor de código pidió negativos"],
                brain_updates=[{"category": "conventions", "title": "Utilidades matemáticas",
                                "body": "Las utilidades matemáticas viven en calc.py"}],
                factory_suggestions=["Añadir negativos al plan de tests por defecto"],
                followups=self.followups))
        if role == "scout":
            return self._msg(self._use("submit_result", proposals=self.proposals, notes="revisado"))
        raise AssertionError(role)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "factory@example.com")
    git(r, "config", "user.name", "AI Factory Test")
    (r / "README.md").write_text("demo\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    from ai_factory.brain import Brain

    Brain(r / ".factory" / "brain").init()
    (r / ".factory" / ".gitignore").write_text("runs/\nworktrees/\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def cfg(repo: Path) -> FactoryConfig:
    return FactoryConfig(repo_path=repo, test_command=f"{sys.executable} -m pytest -q -p no:cacheprovider",
                         allowed_commands=["ls", "python", "pytest"], push=False)


GOOD_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}
