"""Orquestador: intake → spec → build (TDD) → revisión paralela → aprobador → gate → ship."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import uuid
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import shutil

from . import checkpoints, git_ops, issue_feedback, qa, quota
from .review_policy import select_reviewers, active_reviewers
from .forja import ForjaTracker, summary_text
from .agents import Agents
from .audit import AuditLog
from .brain import Brain
from .config import FactoryConfig
from .llm import AgentError, LLMClient
from .quota import QuotaExhausted
from .models import IterationRecord, QAReport, RunState, RunStatus, Task, Verdict
from .tools import SUBPROCESS_ENV, Workspace

HUMAN_LABEL = "needs-human-approval"


class RevalidationRequired(RuntimeError):
    """A definitely rejected publication can be retried after new reviews."""



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
            gi.write_text("runs/\nlocks/\nprocessed.json\n")
        toml = self.cfg.repo_path / "factory.toml"
        if not toml.exists():
            example = Path(__file__).with_name("factory.example.toml")
            toml.write_text(example.read_text())

    def _qa_dir(self, state: RunState) -> Path:
        return self.cfg.worktrees_dir / f"qa-{state.run_id}"

    def _run_qa(self, agents: Agents, state: RunState, wt: Path, ws: Workspace,
                audit: AuditLog) -> tuple[QAReport, list[Path]]:
        qa_dir = self._qa_dir(state)
        first = not (qa_dir / "database-ready").exists()
        (qa_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        if first:
            qa.setup_database(self.cfg, wt, audit)  # QAEnvironmentError: infraestructura, detiene la tarea
            (qa_dir / "database-ready").touch()
        env = qa.qa_env(self.cfg)
        qa_ws = Workspace(qa_dir, ["node", "ls", "cat"], env=env)
        try:
            with qa.running_app(self.cfg, wt, audit) as url:
                report = agents.qa(state.spec, ws, qa_ws, url, env)
        except (qa.QAEnvironmentError, AgentError):
            raise
        except RuntimeError as e:  # la app no arrancó con el cambio: es un fallo del cambio
            report = QAReport(applicable=True, passed=False,
                              summary=f"La app no arranca con este cambio: {str(e)[:1500]}")
        finally:
            if qa.guard_worktree(wt, audit):
                self.log("    (el tester modificó el código de la app; se deshizo)")
        shots = qa.collect_screenshots(qa_dir, report.screenshots, audit)
        audit.artifact("qa.json", report.model_dump_json(indent=2))
        return report, shots

    def _guard_protected(self, wt: Path, audit: AuditLog) -> list[str]:
        """Deshace los cambios del Builder en `protected_paths` (p. ej. .github/workflows)."""
        touched = [f for f in git_ops.changed_files(wt, self.cfg.base_branch)
                   if any(fnmatch.fnmatch(f, p) for p in self.cfg.protected_paths)]
        if touched:
            git_ops.revert_paths(wt, self.cfg.base_branch, touched, "factory: deshacer cambios en rutas protegidas")
            audit.event("factory", "protected_paths_reverted", files=touched)
            self.log(f"  ⚠ Rutas protegidas: se deshicieron los cambios en {', '.join(touched)} "
                     "(los añade una persona).")
        return touched

    @contextmanager
    def _stage(self, state: RunState, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            state.stages[name] = round(state.stages.get(name, 0.0) + time.monotonic() - t0, 1)

    # ---------------------------------------------------------------------------------
    def run(self, task: Task) -> RunState:
        if self.cfg.quota_label in task.labels:
            raise ValueError("Tarea en espera: usa resume con su checkpoint; no se reinicia desde cero")
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{task.id}-{uuid.uuid4().hex[:6]}"
        state = RunState(run_id=run_id, task=task, branch=f"factory/{run_id}")
        with checkpoints.task_lock(self.cfg, task.id):
            return self._execute(state)

    def resume(self, run_id: str, *, package: Path | None = None, task: Task | None = None) -> RunState:
        if not re.fullmatch(r"[\w-]+", run_id):
            raise ValueError("Identificador inválido")
        local_state = self.cfg.runs_dir / run_id / "state.json"
        if local_state.exists():
            previous = self.load(run_id)
            if previous.status in (RunStatus.SHIPPED, RunStatus.AWAITING_HUMAN, RunStatus.REJECTED):
                raise ValueError("La ejecución ya terminó; no se reproducen checkpoints anteriores")
        if package is None:
            old = self.load(run_id)
            root = self.cfg.runs_dir / old.run_id / "checkpoints"
            generation = json.loads((root / "latest.json").read_text())["generation"]
            if not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Generación inválida")
            package = root / generation
        _, state = checkpoints.inspect(package, self.cfg)
        if state.run_id != run_id:
            raise ValueError("El checkpoint pertenece a otra ejecución")
        if state.next_stage == "done" or state.status in (RunStatus.SHIPPED, RunStatus.AWAITING_HUMAN, RunStatus.REJECTED):
            raise ValueError("La ejecución terminó; no se reanuda")
        # Publication/deployment may have succeeded just before a crash. Never blindly repeat it.
        if state.publication_started:
            raise ValueError("Publicación iniciada: concilia commit/PR/despliegue antes de reanudar; no se repetirá automáticamente")
        if task is not None and task.id != state.task.id:
            raise ValueError("El issue no coincide con la tarea guardada")
        with checkpoints.task_lock(self.cfg, state.task.id):
            state.task = task or state.task
            state.source_actions_run_id = state.actions_run_id
            state.actions_run_id = os.environ.get("GITHUB_RUN_ID", "")
            state.attempt += 1
            state.status = RunStatus.RUNNING
            state.error = state.retry_at = ""
            state.finished_at = None
            return self._execute(state, package=package)

    def _checkpoint(self, state: RunState, wt: Path, audit: AuditLog) -> None:
        responses = [e for e in audit.events() if e.get("kind") == "response"]
        for event in responses[getattr(self, "_usage_seen", 0):]:
            cost = event.get("cost_usd_equivalent")
            if cost is not None:
                role = event["agent"]
                state.usage_by_agent[role] = state.usage_by_agent.get(role, 0.0) + float(cost)
                state.usage_calls += 1
        self._usage_seen = len(responses)
        self._save(state)
        if not self.cfg.recovery_enabled:
            return
        package = checkpoints.save(self.cfg, state, wt, self._qa_dir(state))
        self._save(state)
        audit.event("factory", "checkpoint", generation=state.checkpoint, next_stage=state.next_stage,
                    attempt=state.attempt)
        # Trusted operator hook, not configurable by generated project files.
        # GitHub's wrapper uploads this immutable package and returns its artifact ID.
        hook = os.environ.get("FACTORY_CHECKPOINT_UPLOADER")
        if hook:
            import shlex
            r = subprocess.run([*shlex.split(hook), str(package)], capture_output=True, text=True, timeout=180)
            if r.returncode:
                raise RuntimeError(f"No se pudo persistir el checkpoint remoto: {r.stderr[-500:]}")
            state.checkpoint_artifact_id = int(r.stdout.strip().splitlines()[-1])
            self._save(state)

    def _invalidate(self, state: RunState, *, specification: bool = False) -> None:
        state.reviews = []
        state.verdict = state.gate = state.qa = state.retro = None
        state.requested_reviewers = []
        state.review_selection = {}
        if specification:
            state.spec = state.build = None
            state.next_stage = "spec"
        else:
            state.next_stage = "tests" if state.build else "build"

    def _execute(self, state: RunState, package: Path | None = None) -> RunState:
        self._preserve_worktree = False
        task = state.task
        audit = AuditLog(self.cfg.runs_dir / state.run_id)
        self._usage_seen = len([e for e in audit.events() if e.get("kind") == "response"])
        audit.event("factory", "resume" if package else "task", task=task.model_dump(mode="json"), attempt=state.attempt)
        self._save(state)
        self._tracker = ForjaTracker(self.cfg, audit)
        self._tracker.start(state)
        if self.cfg.forja.enabled and not self._tracker.task_id:
            self.log(f"  ⚠ Forja: {self._tracker.disabled_reason}")
        wt = None
        try:
            if self.cfg.push:
                git_ops.sync_base(self.cfg.repo_path, self.cfg.base_branch)
            base_sha = git_ops.git(self.cfg.repo_path, "rev-parse", self.cfg.base_branch)
            context = checkpoints.context_hash(self.cfg, task)
            if package:
                wt = checkpoints.restore(package, self.cfg, state)
                old_stage = state.next_stage
                if context != state.context_hash:
                    self._invalidate(state, specification=True)
                    audit.event("factory", "invalidated", reason="requirements/config/prompts changed")
                if base_sha != state.base_sha:
                    git_ops.merge_base_into_branch(wt, self.cfg.base_branch)
                    self._invalidate(state, specification=True)
                    audit.event("factory", "invalidated", reason="base changed")
                if state.next_stage == old_stage:
                    state.reused_stages.append(f"attempt-{state.attempt}: continue at {old_stage}")
                    audit.event("factory", "reused", next_stage=old_stage,
                                reviewers=[r.reviewer for r in state.reviews])
            else:
                wt = git_ops.create_worktree(self.cfg.repo_path, self.cfg.worktrees_dir, state.branch, self.cfg.base_branch)
            state.base_sha, state.context_hash = base_sha, context
            git_ops.commit_all(wt, "factory: recover partial changes")
            if self._guard_protected(wt, audit):
                self._invalidate(state)
            with self._stage(state, "setup"):
                self._setup(wt, audit)
            ws = Workspace(wt, self.cfg.allowed_commands, env=self.cfg.env)
            brain = Brain(wt / ".factory" / "brain")
            agents = Agents(self.cfg, self.client, audit)
            # A new runner must verify again even when model reviews can be reused.
            if package and state.next_stage in {"qa", "review", "approve", "gate", "retro", "publish"}:
                ok, out = self._run_tests(wt)
                audit.event("factory", "resume_tests", passed=ok, output=out[-4000:])
                state.test_output = out[-4000:]
                if not ok:
                    self._invalidate(state)
                    state.next_stage = "tests"
            self._checkpoint(state, wt, audit)
            while state.next_stage != "done":
                stage = state.next_stage
                self.log(f"  [{state.run_id}, intento {state.attempt}] {stage}")
                if stage == "spec":
                    with self._stage(state, "spec"):
                        state.spec = agents.write_spec(task, ws, brain)
                    audit.artifact("spec.json", state.spec.model_dump_json(indent=2))
                    state.next_stage = "build"
                elif stage == "build":
                    if state.iterations >= self.cfg.max_iterations and not state.feedback[:1] == ["Continuar trabajo parcial"]:
                        state.verdict = Verdict(approved=False, reasoning="Iteraciones agotadas", required_changes=state.feedback)
                        state.status = RunStatus.REJECTED
                        state.error = f"No aprobado tras {self.cfg.max_iterations} iteraciones"
                        state.next_stage = "done"
                        self._retro(agents, state, ws, brain, audit, apply=False)
                        break
                    continuing = state.feedback[:1] == ["Continuar trabajo parcial"]
                    if not continuing:
                        state.iterations += 1
                    feedback = state.feedback or None
                    state.feedback = ["Continuar trabajo parcial", *(state.feedback[1:] if continuing else state.feedback)]
                    # Persist iteration reservation BEFORE invoking the Builder.
                    self._checkpoint(state, wt, audit)
                    self._tracker.move("en_proceso")
                    with self._stage(state, "build"):
                        state.build = agents.build(state.spec, ws, brain, feedback)
                    state.feedback = []
                    git_ops.commit_all(wt, f"factory: {task.title} (iteración {state.iterations})")
                    self._guard_protected(wt, audit)
                    self._invalidate(state)
                    state.next_stage = "tests"
                elif stage == "tests":
                    with self._stage(state, "tests"):
                        ok, out = self._run_tests(wt)
                    state.test_output = out[-4000:]
                    audit.event("factory", "tests", passed=ok, output=state.test_output)
                    if not ok:
                        self._retry_build(state, f"La suite falla: {out[-3000:]}", tests_passed=False)
                    else:
                        if not git_ops.diff_against(wt, self.cfg.base_branch).strip():
                            raise AgentError("El Builder no produjo ningún cambio")
                        state.next_stage = "qa"
                elif stage == "qa":
                    state.qa = None
                    if qa.applies(self.cfg, git_ops.changed_files(wt, self.cfg.base_branch)):
                        # Recreate QA infrastructure on a new runner even if screenshots were restored.
                        with self._stage(state, "qa"):
                            state.qa, _ = self._run_qa(agents, state, wt, ws, audit)
                        if state.qa.applicable and not state.qa.passed and not state.qa.blocked:
                            self._retry_build(state, "QA en rojo: " + state.qa.summary + "\n" + "\n".join(c.evidence for c in state.qa.checks if c.status == "fail"), tests_passed=True)
                        else:
                            state.next_stage = "review"
                    else:
                        state.next_stage = "review"
                elif stage == "review":
                    diff = git_ops.diff_against(wt, self.cfg.base_branch)
                    files = git_ops.changed_files(wt, self.cfg.base_branch)
                    state.review_selection = select_reviewers(files, state.spec, diff)
                    names = active_reviewers(state.review_selection, self.cfg.review_mode, state.requested_reviewers)
                    audit.event("factory", "review_selection", mode=self.cfg.review_mode,
                                decisions=state.review_selection, active=names)
                    audit.artifact(f"diff-{state.iterations}.patch", diff)
                    self._tracker.move("en_revision")
                    def completed(review):
                        state.reviews = [r for r in state.reviews if r.reviewer != review.reviewer] + [review]
                        self._checkpoint(state, wt, audit)
                    shots = list((self._qa_dir(state) / "screenshots").glob("*.png"))
                    with self._stage(state, "review"):
                        state.reviews = agents.review_all(state.spec, diff, ws, state.test_output, state.qa, shots,
                                                         names=names, cached=state.reviews, on_complete=completed)
                    state.next_stage = "approve"
                elif stage == "approve":
                    with self._stage(state, "approve"):
                        state.verdict = agents.approve(state.spec, state.build, git_ops.diff_against(wt, self.cfg.base_branch),
                            state.reviews, ws, state.test_output, state.qa, selection=state.review_selection)
                    missing = set(state.verdict.additional_reviewers) - {r.reviewer for r in state.reviews}
                    if missing:
                        state.requested_reviewers = sorted(set(state.requested_reviewers) | missing)
                        state.next_stage = "review"
                    else:
                        state.history.append(IterationRecord(iteration=state.iterations, tests_passed=True,
                            qa=state.qa, reviews=state.reviews, approved=state.verdict.approved,
                            required_changes=state.verdict.required_changes))
                        if state.verdict.approved:
                            state.next_stage = "gate"
                        else:
                            state.feedback = state.verdict.required_changes or [state.verdict.reasoning]
                            self._invalidate(state)
                            state.next_stage = "build"
                elif stage == "gate":
                    files = git_ops.changed_files(wt, self.cfg.base_branch)
                    core = [f for f in files if any(fnmatch.fnmatch(f, p) for p in self.cfg.core_paths)]
                    with self._stage(state, "gate"):
                        state.gate = agents.gate(task, state.spec, git_ops.diff_against(wt, self.cfg.base_branch), files, core, ws)
                    if core:
                        state.gate = state.gate.model_copy(update={"risk": "critical", "core_areas_touched": core})
                    state.next_stage = "retro"
                elif stage == "retro":
                    # Retrospective expects the planned outcome, but it is not the persisted terminal status.
                    state.status = RunStatus.SHIPPED if state.gate.risk == "low" else RunStatus.AWAITING_HUMAN
                    self._retro(agents, state, ws, brain, audit, apply=True)
                    state.status = RunStatus.RUNNING
                    git_ops.commit_all(wt, f"factory: memoria del Brain para {task.title}")
                    state.next_stage = "publish"
                elif stage == "publish":
                    if self.cfg.push:
                        git_ops.sync_base(self.cfg.repo_path, self.cfg.base_branch)
                    current_base = git_ops.git(self.cfg.repo_path, "rev-parse", self.cfg.base_branch)
                    if current_base != state.base_sha:
                        self.log("La base avanzó mientras se trabajaba: revalidando y repitiendo revisiones")
                        git_ops.merge_base_into_branch(wt, self.cfg.base_branch)
                        state.base_sha = current_base
                        self._invalidate(state)
                        ok, out = self._run_tests(wt)
                        if not ok:
                            raise RuntimeError(f"Tras incorporar la base, la verificación falla: {out[-1500:]}")
                        self._checkpoint(state, wt, audit)
                        continue
                    self._guard_protected(wt, audit)
                    state.publication_started = True
                    self._checkpoint(state, wt, audit)
                    try:
                        if state.gate.risk == "low":
                            with self._stage(state, "ship"):
                                self._ship(state, wt, audit)
                        else:
                            self._request_human(state, wt, audit)
                    except RevalidationRequired:
                        state.publication_revalidations += 1
                        if state.publication_revalidations > 3:
                            raise RuntimeError("La base sigue cambiando; checkpoint conservado para reanudación manual")
                        git_ops.sync_base(self.cfg.repo_path, self.cfg.base_branch)
                        git_ops.merge_base_into_branch(wt, self.cfg.base_branch)
                        state.base_sha = git_ops.git(self.cfg.repo_path, "rev-parse", self.cfg.base_branch)
                        self._invalidate(state)
                        self.log("Reintento de publicación: repetir pruebas y revisiones sobre la nueva base")
                        audit.event("factory", "publication_revalidation", attempt=state.publication_revalidations)
                        self._checkpoint(state, wt, audit)
                        continue
                    state.next_stage = "done"
                self._checkpoint(state, wt, audit)
            return self._finish(state, audit)
        except QuotaExhausted as e:
            state.status = RunStatus.PAUSED if self.cfg.recovery_enabled else RunStatus.FAILED
            state.error = f"{type(e).__name__}: {e}"
            state.retry_at = e.retry_at.isoformat()
            audit.event("factory", "quota", error=state.error, retry_at=state.retry_at)
            self._preserve_failure(state, wt, audit)
            return self._finish(state, audit)
        except Exception as e:
            state.status = RunStatus.RECOVERABLE if wt is not None and self.cfg.recovery_enabled else RunStatus.FAILED
            state.error = f"{type(e).__name__}: {e}"
            audit.event("factory", "error", error=state.error)
            self._preserve_failure(state, wt, audit)
            return self._finish(state, audit)
        finally:
            # If persistence failed, retain the worktree for local/manual recovery.
            if wt is not None and not getattr(self, "_preserve_worktree", False):
                git_ops.remove_worktree(self.cfg.repo_path, wt)
            shutil.rmtree(self._qa_dir(state), ignore_errors=True)

    def _retry_build(self, state: RunState, reason: str, *, tests_passed: bool) -> None:
        state.history.append(IterationRecord(iteration=state.iterations, tests_passed=tests_passed,
                                            qa=state.qa, required_changes=[reason]))
        self._invalidate(state)
        state.feedback = [reason]
        state.next_stage = "build"

    def _preserve_failure(self, state: RunState, wt: Path | None, audit: AuditLog) -> None:
        if wt is None:
            return
        try:
            self._checkpoint(state, wt, audit)
        except Exception as e:
            self._preserve_worktree = True
            audit.event("factory", "checkpoint_failed", error=str(e), worktree=str(wt))
            state.error += f"; checkpoint no persistido: {e}; worktree conservado en {wt}"

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
        if not re.fullmatch(r"[\w-]+", run_id):
            raise ValueError("Identificador de ejecución inválido")
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
                    if self.cfg.recovery_enabled:
                        state.base_sha = git_ops.git(repo, "rev-parse", base)
                        self._invalidate(state)
                        state.publication_started = False
                        raise RevalidationRequired("La base cambió durante publicación; revalidar")
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
                    if self.cfg.recovery_enabled:
                        state.publication_started = False
                        self._invalidate(state)
                        raise RevalidationRequired("Push rechazado por avance de base; revalidar") from e
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
        tracker = getattr(self, "_tracker", None)
        if tracker is None or tracker.audit is not audit:
            tracker = ForjaTracker(self.cfg, audit)  # aprobación o rechazo humano: se busca la tarea existente
            tracker.start(state)
        had_task = bool(tracker.task_id)
        tracker.finish(state, summary_text(state, self.cfg))
        if self.cfg.forja.enabled and had_task and tracker.disabled_reason:
            self.log(f"  ⚠ Forja: la tarea no se actualizó del todo. {tracker.disabled_reason}")
        self._tracker = None
        self.log(f"■ [{state.run_id}] {state.status.value}")
        return state

    def _save(self, state: RunState) -> None:
        d = self.cfg.runs_dir / state.run_id
        d.mkdir(parents=True, exist_ok=True)
        checkpoints.atomic_write(d / "state.json", state.model_dump_json(indent=2))


def summarize(state: RunState) -> dict:
    return json.loads(state.model_dump_json(include={"run_id", "status", "branch", "iterations", "pr_url", "error", "attempt", "next_stage", "checkpoint_artifact_id", "usage_by_agent", "usage_calls", "reused_stages"}))
