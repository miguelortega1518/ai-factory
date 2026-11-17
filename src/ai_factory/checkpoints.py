"""Atomic, portable recovery packages. No branch is pushed to save incomplete work."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
try:
    import fcntl
except ImportError:  # Windows local runners
    fcntl = None
    import msvcrt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from . import git_ops
from .config import FactoryConfig
from .models import RunState

SCHEMA = 1
STAGES = {"spec", "build", "tests", "qa", "review", "approve", "gate", "retro", "publish", "done"}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def context_hash(cfg: FactoryConfig, task) -> str:
    settings = asdict(cfg)
    settings.pop("repo_path")
    # Hash values; do not persist secrets or environment contents in the manifest.
    code = hashlib.sha256()
    for p in sorted(Path(__file__).parent.rglob("*.py")):
        code.update(p.relative_to(Path(__file__).parent).as_posix().encode())
        code.update(p.read_bytes())
    return hashlib.sha256(json.dumps({"settings": settings, "task": {"title": task.title, "body": task.body},
                                     "code": code.hexdigest()}, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def task_lock(cfg: FactoryConfig, task_id: str):
    lock_dir = cfg.worktrees_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{task_id}.lock").open("w") as lock:
        try:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                lock.write("0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Ya hay una ejecución activa para esta tarea") from None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _git_env(wt: Path, env: dict, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=wt, env=env, capture_output=True, text=True)
    if r.returncode:
        raise git_ops.GitError(r.stderr)
    return r.stdout.strip()


def save(cfg: FactoryConfig, state: RunState, wt: Path, qa_dir: Path) -> Path:
    root = cfg.runs_dir / state.run_id / "checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    target = root / generation
    temp = Path(tempfile.mkdtemp(dir=root, prefix=".pending-"))
    ref = f"refs/factory-checkpoints/{generation}"
    try:
        env = {**os.environ, "GIT_INDEX_FILE": str(temp / "index")}
        _git_env(wt, env, "read-tree", "HEAD")
        # Track deletions and additions in a separate index; don't disturb the Builder's index.
        _git_env(wt, env, "add", "-A", "--", ".", ":(exclude).factory/runs", ":(exclude).factory/locks")
        # Untracked credential-like files must not enter a portable artifact.
        original = set(git_ops.git(wt, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
        files = _git_env(wt, env, "ls-files").splitlines()
        for name in files:
            p = Path(name)
            if name not in original and (p.name.startswith(".env") and p.name != ".env.example"
                                        or p.suffix in {".pem", ".key", ".p12"}):
                raise ValueError(f"Archivo sensible no versionado: {name}; no se publica el checkpoint")
        tree = _git_env(wt, env, "write-tree")
        sha = _git_env(wt, env, "commit-tree", tree, "-p", "HEAD", "-m", "factory: recovery snapshot (unverified)")
        git_ops.git(wt, "update-ref", ref, sha)
        git_ops.git(wt, "bundle", "create", str(temp / "code.bundle"), ref)
        (temp / "index").unlink(missing_ok=True)
        state.checkpoint = generation
        (temp / "state.json").write_text(state.model_dump_json(indent=2))
        # Captures only, never the QA scripts or credentials.
        if (qa_dir / "screenshots").exists():
            for p in (qa_dir / "screenshots").glob("*.png"):
                if p.is_symlink() or not p.is_file():
                    continue
                (temp / "screenshots").mkdir(exist_ok=True)
                shutil.copyfile(p, temp / "screenshots" / p.name)
        payload = {p.relative_to(temp).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in temp.rglob("*") if p.is_file()}
        (temp / "manifest.json").write_text(json.dumps({"schema": SCHEMA, "repo": cfg.github_repo,
            "run_id": state.run_id, "generation": generation, "sha": sha, "ref": ref, "files": payload}, indent=2))
        os.replace(temp, target)
        atomic_write(root / "latest.json", json.dumps({"generation": generation}))
        return target
    finally:
        git_ops.git(wt, "update-ref", "-d", ref, check=False)
        if temp.exists():
            shutil.rmtree(temp)


def inspect(package: Path, cfg: FactoryConfig) -> tuple[dict, RunState]:
    package = package.resolve()
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA or manifest.get("repo") != cfg.github_repo:
        raise ValueError("Checkpoint incompatible o de otro repositorio")
    if not re.fullmatch(r"[a-f0-9]{32}", manifest.get("generation", "")):
        raise ValueError("Generación inválida")
    if not re.fullmatch(r"[a-f0-9]{40,64}", manifest.get("sha", "")):
        raise ValueError("SHA inválido")
    expected_ref = f"refs/factory-checkpoints/{manifest['generation']}"
    if manifest.get("ref") != expected_ref:
        raise ValueError("Referencia inválida")
    files = manifest.get("files", {})
    if not {"state.json", "code.bundle"}.issubset(files):
        raise ValueError("Checkpoint incompleto")
    for name, digest in files.items():
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts or not (name in {"state.json", "code.bundle"}
                                                        or re.fullmatch(r"screenshots/[\w.-]+\.png", name)):
            raise ValueError("Ruta inválida en el checkpoint")
        p = package / rel
        if p.is_symlink() or package not in p.resolve().parents or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Checkpoint corrupto: {name}")
    state = RunState.model_validate_json((package / "state.json").read_text())
    if (state.run_id != manifest['run_id'] or state.schema_version != SCHEMA
            or state.next_stage not in STAGES or not re.fullmatch(r"[\w-]+", state.run_id)
            or not state.branch.startswith("factory/") or state.branch.startswith("-")):
        raise ValueError("Estado de recuperación inválido")
    return manifest, state


def restore(package: Path, cfg: FactoryConfig, state: RunState) -> Path:
    manifest, saved = inspect(package, cfg)
    if saved.run_id != state.run_id:
        raise ValueError("La ejecución no coincide")
    repo = cfg.repo_path
    git_ops.git(repo, "bundle", "verify", str(package / "code.bundle"))
    heads = git_ops.git(repo, "bundle", "list-heads", str(package / "code.bundle"))
    if heads != f"{manifest['sha']} {manifest['ref']}":
        raise ValueError("El bundle no coincide con el manifiesto")
    git_ops.git(repo, "fetch", "--no-tags", str(package / "code.bundle"), manifest['ref'])
    # A fresh local branch avoids overwriting a branch changed by a person or another attempt.
    state.branch = f"factory/{state.run_id}-attempt-{state.attempt}-{uuid.uuid4().hex[:6]}"
    wt = git_ops.create_worktree(repo, cfg.worktrees_dir, state.branch, manifest['sha'])
    shots = cfg.worktrees_dir / f"qa-{state.run_id}" / "screenshots"
    for name in manifest['files']:
        if name.startswith("screenshots/"):
            shots.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package / name, shots / Path(name).name)
    return wt
