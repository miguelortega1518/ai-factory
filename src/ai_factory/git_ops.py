"""Operaciones git: cada ejecución trabaja en su propio worktree y rama."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} falló: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def create_worktree(repo: Path, worktrees_dir: Path, branch: str, base: str) -> Path:
    path = worktrees_dir / branch.replace("/", "-")
    if path.exists():
        return path
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)


def has_changes(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain"))


def commit_all(wt: Path, message: str) -> str:
    git(wt, "add", "-A")
    if not git(wt, "diff", "--cached", "--name-only"):
        return ""
    git(wt, "commit", "-m", message)
    return git(wt, "rev-parse", "HEAD")


def diff_against(wt: Path, base: str) -> str:
    return git(wt, "diff", f"{base}...HEAD")


def changed_files(wt: Path, base: str) -> list[str]:
    out = git(wt, "diff", "--name-only", f"{base}...HEAD")
    return [l for l in out.splitlines() if l]


def has_remote(repo: Path) -> bool:
    return bool(git(repo, "remote", check=False))


def push(cwd: Path, branch: str) -> None:
    git(cwd, "push", "-u", "origin", branch)


def is_push_rejected(err: Exception) -> bool:
    """El remoto rechazó el push porque la rama avanzó (otra ejecución fusionó antes)."""
    msg = str(err).lower()
    return any(k in msg for k in ("rejected", "fetch first", "non-fast-forward", "failed to push some refs"))


def sync_base(repo: Path, base: str) -> bool:
    """Trae la última versión de `base` del remoto y avanza la local (sólo fast-forward).

    Devuelve True si la base local avanzó. Necesario porque, en CI, la copia de trabajo puede ser
    la de cuando se lanzó el evento, y otra ejecución puede haber fusionado algo después.
    """
    if not has_remote(repo):
        return False
    git(repo, "fetch", "origin", base)
    before = git(repo, "rev-parse", base)
    remote_sha = git(repo, "rev-parse", f"origin/{base}")
    if before == remote_sha:
        return False
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current == base:
        git(repo, "merge", "--ff-only", f"origin/{base}")
    else:
        git(repo, "branch", "-f", base, f"origin/{base}")
    return True


def merge_base_into_branch(wt: Path, base: str) -> bool:
    """Incorpora `base` a la rama del worktree. True si entró algo nuevo; GitError si hay conflictos."""
    before = git(wt, "rev-parse", "HEAD")
    r = subprocess.run(["git", "merge", "--no-edit", base], cwd=wt, capture_output=True, text=True)
    if r.returncode != 0:
        git(wt, "merge", "--abort", check=False)
        raise GitError(f"La rama entra en conflicto con {base}; hace falta resolverlo a mano: "
                       f"{(r.stdout + r.stderr).strip()[:500]}")
    return git(wt, "rev-parse", "HEAD") != before


def merge_into_base(repo: Path, branch: str, base: str, message: str) -> None:
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base:
        raise GitError(f"El repo principal debe estar en '{base}' para fusionar (está en '{current}')")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise GitError("El repo principal tiene cambios sin commitear; no se fusiona")
    git(repo, "merge", "--no-ff", branch, "-m", message)


def open_pr(cwd: Path, branch: str, base: str, title: str, body: str, labels: list[str]) -> str:
    args = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 and labels:
        # La etiqueta puede no existir en el repo: reintenta sin etiquetas.
        r = subprocess.run(args[: args.index("--label")], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"gh pr create falló: {r.stderr.strip()}")
    return r.stdout.strip().splitlines()[-1]
