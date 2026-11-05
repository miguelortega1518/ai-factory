"""Orquestador: intake → spec → build (TDD) → revisión paralela → aprobador → gate → ship."""

from __future__ import annotations

import fnmatch
import json
import shlex
import subprocess
import time
from pathlib import Path

from . import git_ops
from .agents import Agents
from .audit import AuditLog
from .brain import Brain
from .config import FactoryConfig
from .llm import AgentError, LLMClient
from .models import RunState, RunStatus, Task, Verdict
from .tools import SUBPROCESS_ENV, Workspace

HUMAN_LABEL = "needs-human-approval"


class Factory:
    def __init__(self, cfg: FactoryConfig, client: LLMClient, log=print):
        self.cfg, self.client, self.log = cfg, client, log

    # ---------------------------------------------------------------------------------
    def init(self) -> None:
        """Prepara el repo: Brain inicial, .gitignore de la fábrica y factory.toml de ejemplo."""
        Brain(self.cfg.brain_dir).init()
        gi = self.cfg.factory_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("runs/\nworktrees/\n")
        toml = self.cfg.repo_path / "factory.toml"
        if not toml.exists():
            example = Path(__file__).with_name("factory.example.toml")
            toml.write_text(example.read_text())

    # ---------------------------------------------------------------------------------
    def run(self, task: Task) -> RunState:
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{task.id}"
        state = RunState(run_id=run_id, task=task, branch=f"factory/{run_id}")
        audit = AuditLog(self.cfg.runs_dir / run_id)
        audit.event("factory", "task", task=task.model_dump(mode="json"))
        self._save(state)
        self.log(f"▶ [{run_id}] {task.title}")

        wt: Path | None = None
        try:
            wt = git_ops.create_worktree(
                self.cfg.repo_path, self.cfg.factory_dir / "worktrees", state.branch, self.cfg.base_branch
            )
            ws = Workspace(wt, self.cfg.allowed_commands)
            brain = Brain(wt / ".factory" / "brain")
            agents = Agents(self.cfg, self.client, audit)

            # 2. Spec ---------------------------------------------------------------------
            self.log("  ✎ Spec Writer: consultando el Brain y redactando la especificación…")
            state.spec = agents.write_spec(task, ws, brain)
            audit.artifact("spec.json", state.spec.model_dump_json(indent=2))
            self._save(state)

            # 3–5. Build ↔ Review ↔ Approve ---------------------------------------------
            feedback: list[str] | None = None
            for i in range(1, self.cfg.max_iterations + 1):
                state.iterations = i
                self.log(f"  ⚒ Builder (iteración {i}): TDD en {state.branch}…")
                state.build = agents.build(state.spec, ws, brain, feedback)
                git_ops.commit_all(wt, f"factory: {task.title} (iteración {i})")

                ok, test_out = self._run_tests(wt)
                audit.event("factory", "tests", passed=ok, output=test_out[-4000:])
                if not ok:
                    self.log("  ✗ La suite de tests falla; vuelve al Builder sin pasar por revisión.")
                    feedback = [f"La suite de tests (`{self.cfg.test_command}`) falla:\n{test_out[-3000:]}"]
                    state.verdict = Verdict(approved=False, reasoning="Tests en rojo", required_changes=feedback)
                    self._save(state)
                    continue

                diff = git_ops.diff_against(wt, self.cfg.base_branch)
                if not diff.strip():
                    raise AgentError("El Builder no produjo ningún cambio")
                audit.artifact(f"diff-{i}.patch", diff)

                self.log("  ⚖ Revisión paralela: seguridad · UX · UI · código…")
                state.reviews = agents.review_all(state.spec, diff, ws)
                for r in state.reviews:
                    mark = "✓" if r.passed else "✗"
                    self.log(f"    {mark} {r.reviewer} ({r.model}): {r.summary[:100]}")

                self.log("  ⚑ Aprobador: analizando el feedback…")
                state.verdict = agents.approve(state.spec, state.build, diff, state.reviews, ws)
                self._save(state)
                if state.verdict.approved:
                    self.log("  ✓ Aprobado.")
                    break
                feedback = state.verdict.required_changes or [state.verdict.reasoning]
                self.log(f"  ↺ Cambios requeridos: {len(feedback)}")
            else:
                state.status = RunStatus.REJECTED
                state.error = f"No aprobado tras {self.cfg.max_iterations} iteraciones"
                self.log(f"  ✗ {state.error}. La rama {state.branch} queda para revisión manual.")
                return self._finish(state, audit)

            # 6. Gate final ---------------------------------------------------------------
            files = git_ops.changed_files(wt, self.cfg.base_branch)
            core_hits = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.core_paths)]
            state.gate = agents.gate(task, state.spec, diff, files, core_hits)
            if core_hits and state.gate.risk != "critical":
                state.gate = state.gate.model_copy(
                    update={"risk": "critical", "core_areas_touched": core_hits,
                            "reasoning": state.gate.reasoning + " [Forzado a critical: toca core_paths]"}
                )
            self._record_learnings(brain, task, state)
            git_ops.commit_all(wt, f"factory: memoria del Brain para {task.title}")
            self._save(state)

            if state.gate.risk == "low":
                self.log("  🚀 Riesgo bajo: desplegando automáticamente.")
                self._ship(state, wt, audit)
            else:
                self.log("  ✋ Riesgo crítico: requiere aprobación humana.")
                self._request_human(state, wt, audit)
            return self._finish(state, audit)

        except Exception as e:  # noqa: BLE001 — cualquier fallo queda registrado y auditable
            state.status = RunStatus.FAILED
            state.error = f"{type(e).__name__}: {e}"
            audit.event("factory", "error", error=state.error)
            self.log(f"  ✗ Error: {state.error}")
            return self._finish(state, audit)
        finally:
            if wt is not None:
                git_ops.remove_worktree(self.cfg.repo_path, wt)

    # ---------------------------------------------------------------------------------
    def approve(self, run_id: str) -> RunState:
        """Aprobación humana de un cambio crítico: lo fusiona y despliega."""
        state = self.load(run_id)
        if state.status != RunStatus.AWAITING_HUMAN:
            raise ValueError(f"La ejecución {run_id} está en estado {state.status.value}, no awaiting_human")
        audit = AuditLog(self.cfg.runs_dir / run_id)
        audit.event("human", "approve")
        if state.pr_url:
            r = subprocess.run(["gh", "pr", "merge", state.pr_url, "--squash"], cwd=self.cfg.repo_path,
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise git_ops.GitError(f"gh pr merge falló: {r.stderr.strip()}")
            state.status = RunStatus.SHIPPED
            audit.event("factory", "ship", mode="pr-merge", pr=state.pr_url)
        else:
            self._ship(state, None, audit)
        return self._finish(state, audit)

    def reject(self, run_id: str, reason: str = "") -> RunState:
        state = self.load(run_id)
        audit = AuditLog(self.cfg.runs_dir / run_id)
        audit.event("human", "reject", reason=reason)
        state.status, state.error = RunStatus.REJECTED, reason or "Rechazado por un humano"
        return self._finish(state, audit)

    def list_runs(self) -> list[RunState]:
        if not self.cfg.runs_dir.exists():
            return []
        out = []
        for d in sorted(self.cfg.runs_dir.iterdir()):
            if (d / "state.json").exists():
                out.append(RunState.model_validate_json((d / "state.json").read_text()))
        return out

    def load(self, run_id: str) -> RunState:
        path = self.cfg.runs_dir / run_id / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"No existe la ejecución {run_id}")
        return RunState.model_validate_json(path.read_text())

    # --- internos ---------------------------------------------------------------------
    def _run_tests(self, wt: Path) -> tuple[bool, str]:
        """La fábrica verifica los tests por sí misma: no se fía del informe del Builder."""
        r = subprocess.run(shlex.split(self.cfg.test_command), cwd=wt, capture_output=True, text=True,
                           timeout=1800, env=SUBPROCESS_ENV)
        return r.returncode == 0, r.stdout + r.stderr

    def _record_learnings(self, brain: Brain, task: Task, state: RunState) -> None:
        for lesson in (state.gate.learnings if state.gate else [])[:3]:
            brain.add("learnings", lesson[:60], f"{lesson}\n\n_Origen: {task.title} ({state.run_id})_")

    def _ship(self, state: RunState, wt: Path | None, audit: AuditLog) -> None:
        repo, base, branch = self.cfg.repo_path, self.cfg.base_branch, state.branch
        remote = git_ops.has_remote(repo) and self.cfg.push
        mode = self.cfg.auto_ship
        if mode == "none":
            state.status = RunStatus.SHIPPED
            audit.event("factory", "ship", mode="none", branch=branch)
            return
        if mode == "pr" and remote:
            git_ops.push(repo, branch)
            state.pr_url = state.pr_url or git_ops.open_pr(
                repo, branch, base, state.task.title, self._pr_body(state), ["ai-factory"]
            )
            subprocess.run(["gh", "pr", "merge", state.pr_url, "--auto", "--squash"], cwd=repo,
                           capture_output=True, text=True)
        else:
            git_ops.merge_into_base(repo, branch, base, f"factory: {state.task.title} ({state.run_id})")
            if remote:
                git_ops.push(repo, base)
        if self.cfg.deploy_command:
            r = subprocess.run(self.cfg.deploy_command, shell=True, cwd=repo, capture_output=True, text=True)
            audit.event("factory", "deploy", code=r.returncode, output=(r.stdout + r.stderr)[-4000:])
            if r.returncode != 0:
                raise RuntimeError(f"El despliegue falló (exit {r.returncode})")
        state.status = RunStatus.SHIPPED
        audit.event("factory", "ship", mode=mode, branch=branch, pr=state.pr_url)

    def _request_human(self, state: RunState, wt: Path, audit: AuditLog) -> None:
        state.status = RunStatus.AWAITING_HUMAN
        if git_ops.has_remote(self.cfg.repo_path) and self.cfg.push:
            git_ops.push(wt, state.branch)
            state.pr_url = git_ops.open_pr(
                wt, state.branch, self.cfg.base_branch, f"[Aprobación humana] {state.task.title}",
                self._pr_body(state), [HUMAN_LABEL, "ai-factory"],
            )
        audit.event("factory", "awaiting_human", pr=state.pr_url)
        self.log(f"    Revisa y ejecuta: factory approve {state.run_id}" + (f"  ({state.pr_url})" if state.pr_url else ""))

    def _pr_body(self, s: RunState) -> str:
        reviews = "\n".join(f"- {'✅' if r.passed else '❌'} **{r.reviewer}** ({r.model}): {r.summary}" for r in s.reviews)
        criteria = "\n".join(f"- {c}" for c in (s.spec.acceptance_criteria if s.spec else []))
        return (
            f"Generado por la AI Factory (ejecución `{s.run_id}`).\n\n"
            f"**Origen:** {s.task.source.value} {s.task.url}\n\n"
            f"## Spec\n{s.spec.summary if s.spec else ''}\n\n### Criterios de aceptación\n{criteria}\n\n"
            f"## Revisiones\n{reviews}\n\n"
            f"## Aprobador\n{s.verdict.reasoning if s.verdict else ''}\n\n"
            f"## Gate\nRiesgo: **{s.gate.risk if s.gate else '?'}** — {s.gate.reasoning if s.gate else ''}\n"
        )

    def _finish(self, state: RunState, audit: AuditLog) -> RunState:
        state.finished_at = time.time()
        self._save(state)
        audit.event("factory", "finish", status=state.status.value, error=state.error)
        self.log(f"■ [{state.run_id}] {state.status.value}")
        return state

    def _save(self, state: RunState) -> None:
        d = self.cfg.runs_dir / state.run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(state.model_dump_json(indent=2))


def summarize(state: RunState) -> dict:
    return json.loads(state.model_dump_json(include={"run_id", "status", "branch", "iterations", "pr_url", "error"}))
