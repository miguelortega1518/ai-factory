"""Orquestador: intake → spec → build (TDD) → revisión paralela → aprobador → gate → ship."""

from __future__ import annotations

import fnmatch
import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import git_ops, issue_feedback
from .agents import Agents
from .audit import AuditLog
from .brain import Brain
from .config import FactoryConfig
from .llm import AgentError, LLMClient
from .models import IterationRecord, RunState, RunStatus, Task, Verdict
from .tools import SUBPROCESS_ENV, Workspace

HUMAN_LABEL = "needs-human-approval"


def _log(msg: str) -> None:
    # Con hora y sin búfer: en CI cada línea sale al instante, así el log mide cuánto tarda cada etapa.
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Factory:
    def __init__(self, cfg: FactoryConfig, client: LLMClient, log=_log, runner=subprocess.run):
        self.cfg, self.client, self.log = cfg, client, log
        self.runner = runner  # ejecuta `gh` para informar en los issues (sustituible en tests)

    # ---------------------------------------------------------------------------------
    def init(self) -> None:
        """Prepara el repo: Brain inicial, .gitignore de la fábrica y factory.toml de ejemplo."""
        Brain(self.cfg.brain_dir).init()
        gi = self.cfg.factory_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("runs/\nprocessed.json\n")
        toml = self.cfg.repo_path / "factory.toml"
        if not toml.exists():
            example = Path(__file__).with_name("factory.example.toml")
            toml.write_text(example.read_text())

    @contextmanager
    def _stage(self, state: RunState, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            state.stages[name] = round(state.stages.get(name, 0.0) + time.monotonic() - t0, 1)

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
            if self.cfg.push:
                git_ops.sync_base(self.cfg.repo_path, self.cfg.base_branch)
            wt = git_ops.create_worktree(
                self.cfg.repo_path, self.cfg.worktrees_dir, state.branch, self.cfg.base_branch
            )
            with self._stage(state, "setup"):
                self._setup(wt, audit)
            ws = Workspace(wt, self.cfg.allowed_commands, env=self.cfg.env)
            brain = Brain(wt / ".factory" / "brain")
            agents = Agents(self.cfg, self.client, audit)

            # 2. Spec ---------------------------------------------------------------------
            self.log("  ✎ Spec Writer: consultando el Brain y redactando la especificación…")
            with self._stage(state, "spec"):
                state.spec = agents.write_spec(task, ws, brain)
            audit.artifact("spec.json", state.spec.model_dump_json(indent=2))
            self._save(state)

            # 3–5. Build ↔ Review ↔ Approve ---------------------------------------------
            feedback: list[str] | None = None
            for i in range(1, self.cfg.max_iterations + 1):
                state.iterations = i
                self.log(f"  ⚒ Builder (iteración {i}): TDD en {state.branch}…")
                with self._stage(state, "build"):
                    state.build = agents.build(state.spec, ws, brain, feedback)
                git_ops.commit_all(wt, f"factory: {task.title} (iteración {i})")

                with self._stage(state, "tests"):
                    ok, test_out = self._run_tests(wt)
                audit.event("factory", "tests", passed=ok, output=test_out[-4000:])
                if not ok:
                    self.log("  ✗ La suite de tests falla; vuelve al Builder sin pasar por revisión.")
                    feedback = [f"La suite de tests (`{self.cfg.test_command}`) falla:\n{test_out[-3000:]}"]
                    state.verdict = Verdict(approved=False, reasoning="Tests en rojo", required_changes=feedback)
                    state.history.append(IterationRecord(iteration=i, tests_passed=False, required_changes=feedback))
                    self._save(state)
                    continue

                diff = git_ops.diff_against(wt, self.cfg.base_branch)
                if not diff.strip():
                    raise AgentError("El Builder no produjo ningún cambio")
                audit.artifact(f"diff-{i}.patch", diff)

                self.log("  ⚖ Revisión paralela: seguridad · UX · UI · código…")
                with self._stage(state, "review"):
                    state.reviews = agents.review_all(state.spec, diff, ws, test_out)
                for r in state.reviews:
                    mark = "✓" if r.passed else "✗"
                    self.log(f"    {mark} {r.reviewer} ({r.model}): {r.summary[:100]}")

                self.log("  ⚑ Aprobador: analizando el feedback…")
                with self._stage(state, "approve"):
                    state.verdict = agents.approve(state.spec, state.build, diff, state.reviews, ws, test_out)
                state.history.append(IterationRecord(
                    iteration=i, tests_passed=True, reviews=state.reviews, approved=state.verdict.approved,
                    required_changes=state.verdict.required_changes,
                ))
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
                self._retro(agents, state, ws, brain, audit, apply=False)
                return self._finish(state, audit)

            # 6. Gate final ---------------------------------------------------------------
            files = git_ops.changed_files(wt, self.cfg.base_branch)
            core_hits = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.core_paths)]
            with self._stage(state, "gate"):
                state.gate = agents.gate(task, state.spec, diff, files, core_hits, ws)
            if core_hits and state.gate.risk != "critical":
                state.gate = state.gate.model_copy(
                    update={"risk": "critical", "core_areas_touched": core_hits,
                            "reasoning": state.gate.reasoning + " [Forzado a critical: toca core_paths]"}
                )
            # El resultado (fusión o aprobación humana) ya está decidido: la retrospectiva lo sabe.
            state.status = RunStatus.SHIPPED if state.gate.risk == "low" else RunStatus.AWAITING_HUMAN
            self._retro(agents, state, ws, brain, audit, apply=True)
            git_ops.commit_all(wt, f"factory: memoria del Brain para {task.title}")
            self._save(state)

            if state.gate.risk == "low":
                self.log("  🚀 Riesgo bajo: desplegando automáticamente.")
                with self._stage(state, "ship"):
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
    def _shell(self, command: str, cwd: Path, timeout: int = 1800) -> subprocess.CompletedProcess:
        # Los comandos de setup/test vienen de factory.toml (no de los agentes): se permite `&&`.
        return subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env={**SUBPROCESS_ENV, **self.cfg.env})

    def _setup(self, wt: Path, audit: AuditLog) -> None:
        if not self.cfg.setup_command:
            return
        self.log(f"  ⚙ Preparando el worktree: {self.cfg.setup_command}")
        r = self._shell(self.cfg.setup_command, wt)
        audit.event("factory", "setup", code=r.returncode, output=(r.stdout + r.stderr)[-4000:])
        if r.returncode != 0:
            raise RuntimeError(f"setup_command falló (exit {r.returncode}): {(r.stdout + r.stderr)[-1500:]}")

    def _run_tests(self, wt: Path) -> tuple[bool, str]:
        """La fábrica verifica los tests por sí misma: no se fía del informe del Builder."""
        r = self._shell(self.cfg.test_command, wt)
        return r.returncode == 0, r.stdout + r.stderr

    def _retro(self, agents: Agents, state: RunState, ws: Workspace, brain: Brain, audit: AuditLog,
               apply: bool) -> None:
        """Sector de mejora continua: retrospectiva de la tarea. Nunca rompe la ejecución."""
        if not self.cfg.retro:
            return
        self.log("  ♻ Mejora continua: retrospectiva de la tarea…")
        try:
            with self._stage(state, "retro"):
                state.retro = agents.retro(state, ws, brain)
        except Exception as e:  # noqa: BLE001
            audit.event("factory", "retro_error", error=f"{type(e).__name__}: {e}"[:1000])
            self.log(f"    (retrospectiva no disponible: {type(e).__name__})")
            return
        audit.artifact("retro.json", state.retro.model_dump_json(indent=2))
        if apply:
            origin = f"_Origen: retrospectiva de «{state.task.title}» ({state.run_id})_"
            for u in state.retro.brain_updates[:3]:
                brain.add(u.category, u.title[:60], f"{u.body}\n\n{origin}")
        self.log(f"    {len(state.retro.brain_updates)} nota(s) para el Brain, "
                 f"{len(state.retro.factory_suggestions)} sugerencia(s) para la fábrica")
        self._followups(state, audit)

    def _followups(self, state: RunState, audit: AuditLog) -> None:
        """Publica como propuestas las tareas de seguimiento de la retrospectiva. Nunca rompe la ejecución."""
        items = state.retro.followups if state.retro else []
        if not items or not self.cfg.github_repo or self.cfg.followup_issues <= 0:
            return
        from .improve import publish_proposals

        try:
            created = publish_proposals(self.cfg, items, max_items=self.cfg.followup_issues,
                                        run=self.runner, log=self.log, audit=audit,
                                        origin=state.task.url or state.task.title)
            state.followup_issues = [c["url"] for c in created if c["url"]]
        except Exception as e:  # noqa: BLE001
            audit.event("factory", "followups_error", error=f"{type(e).__name__}: {e}"[:1000])

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
            # La base pudo avanzar mientras trabajábamos (otra tarea fusionada antes, o en paralelo):
            # se incorpora, se vuelve a verificar si entró algo nuevo, y si el push pierde la carrera
            # se deshace la fusión local y se repite.
            for attempt in range(1, 4):
                git_ops.sync_base(repo, base)
                if wt is not None and git_ops.merge_base_into_branch(wt, base):
                    self.log(f"  ↻ {base} avanzó mientras se trabajaba: se incorporó y se vuelve a verificar…")
                    with self._stage(state, "tests"):
                        ok, out = self._run_tests(wt)
                    audit.event("factory", "tests_after_rebase", passed=ok, output=out[-4000:])
                    if not ok:
                        raise RuntimeError(f"Tras incorporar los últimos cambios de {base}, la verificación falla: "
                                           f"{out[-1500:]}")
                before = git_ops.git(repo, "rev-parse", base)
                git_ops.merge_into_base(repo, branch, base, f"factory: {state.task.title} ({state.run_id})")
                state.commit = git_ops.git(repo, "rev-parse", "HEAD")
                if not remote:
                    break
                try:
                    git_ops.push(repo, base)
                    break
                except git_ops.GitError as e:
                    if attempt == 3 or not git_ops.is_push_rejected(e):
                        raise
                    git_ops.git(repo, "reset", "--hard", before)  # deshace la fusión local que no se subió
                    state.commit = ""
                    audit.event("factory", "push_retry", attempt=attempt)
                    self.log(f"  ↻ Otra tarea fusionó en {base} justo antes; reintento {attempt + 1}/3…")
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
        closes = issue_feedback.closes_line(s, self.cfg)
        return (
            f"Generado por la AI Factory (ejecución `{s.run_id}`).\n\n"
            + (f"{closes}\n\n" if closes else "") +
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
        issue_feedback.report(state, self.cfg, audit, run=self.runner)
        self.log(f"■ [{state.run_id}] {state.status.value}")
        return state

    def _save(self, state: RunState) -> None:
        d = self.cfg.runs_dir / state.run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(state.model_dump_json(indent=2))


def summarize(state: RunState) -> dict:
    return json.loads(state.model_dump_json(include={"run_id", "status", "branch", "iterations", "pr_url", "error"}))
