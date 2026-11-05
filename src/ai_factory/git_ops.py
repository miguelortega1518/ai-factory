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
