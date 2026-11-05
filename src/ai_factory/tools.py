"""Herramientas que los agentes usan sobre el repositorio (confinadas al workspace)."""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
from pathlib import Path

from .llm import Tool

_MAX_OUTPUT = 20000

# Sin bytecode: evita .pyc obsoletos cuando un archivo se reescribe con el mismo tamaño en el mismo
# segundo, y que __pycache__ acabe en los commits de la fábrica.
SUBPROCESS_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


class Workspace:
    def __init__(self, root: Path, allowed_commands: list[str], timeout: int = 600,
                 env: dict[str, str] | None = None):
        self.root = root.resolve()
        self.allowed = set(allowed_commands)
        self.timeout = timeout
        self.env = {**SUBPROCESS_ENV, **(env or {})}

    def _path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"Ruta fuera del workspace: {rel}")
        if ".git" in p.relative_to(self.root).parts:
            raise PermissionError("No se permite acceder a .git directamente")
        return p

    # --- operaciones ---------------------------------------------------------------------
    def read_file(self, path: str) -> str:
        text = self._path(path).read_text(errors="replace")
        return text if len(text) < _MAX_OUTPUT * 5 else text[: _MAX_OUTPUT * 5] + "\n…(truncado)"

    def write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Escrito {path} ({len(content)} bytes)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self._path(path)
        text = p.read_text()
        count = text.count(old)
        if count != 1:
            raise ValueError(f"`old` aparece {count} veces en {path}; debe aparecer exactamente una")
        p.write_text(text.replace(old, new, 1))
        return f"Editado {path}"

    def list_files(self, pattern: str = "**/*") -> str:
        skip = {".git", ".factory", "node_modules", "__pycache__", ".venv", "dist", "build"}
        out = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if p.is_file() and not (set(rel.parts) & skip) and fnmatch.fnmatch(str(rel), pattern):
                out.append(str(rel))
            if len(out) >= 1000:
                out.append("…(más de 1000 archivos)")
                break
        return "\n".join(out) or "(sin coincidencias)"

    def search(self, pattern: str, glob: str = "") -> str:
        cmd = ["git", "grep", "-n", "-I", "-E", pattern]
        if glob:
            cmd += ["--", glob]
        r = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=60)
        return (r.stdout or "(sin coincidencias)")[:_MAX_OUTPUT]

    def run_command(self, command: str) -> str:
        argv = shlex.split(command)
        if not argv or argv[0] not in self.allowed:
            raise PermissionError(f"Comando no permitido: {argv[:1]}. Permitidos: {sorted(self.allowed)}")
        if argv[0] == "git" and len(argv) > 1 and argv[1] in {"push", "reset", "checkout", "rebase", "clean"}:
            raise PermissionError("La fábrica gestiona git; ese subcomando no está permitido")
        r = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=self.timeout,
                           env=self.env)
        out = f"exit_code={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        return out[-_MAX_OUTPUT:]

    # --- definiciones de herramientas -----------------------------------------------------
    def read_tools(self) -> list[Tool]:
        return [
            Tool("read_file", "Lee un archivo del repositorio.",
                 _schema(path=("string", "Ruta relativa a la raíz del repo")), self.read_file),
            Tool("list_files", "Lista archivos del repo que coinciden con un patrón glob.",
                 _schema(pattern=("string", "Patrón glob, p.ej. 'src/**/*.py'"), required=[]), self.list_files),
            Tool("search", "Busca una expresión regular en el repo (git grep).",
                 _schema(pattern=("string", "Regex extendida"), glob=("string", "Filtro de rutas opcional"),
                         required=["pattern"]), self.search),
        ]

    def write_tools(self) -> list[Tool]:
        return [
            Tool("write_file", "Crea o sobrescribe un archivo completo.",
                 _schema(path=("string", "Ruta relativa"), content=("string", "Contenido completo")),
                 self.write_file),
            Tool("edit_file", "Reemplaza un fragmento exacto (que aparece una sola vez) en un archivo.",
                 _schema(path=("string", "Ruta relativa"), old=("string", "Texto exacto a reemplazar"),
                         new=("string", "Texto nuevo")), self.edit_file),
        ]

    def exec_tools(self) -> list[Tool]:
        return [
            Tool("run_command",
                 f"Ejecuta un comando en la raíz del repo. Permitidos: {sorted(self.allowed)}.",
                 _schema(command=("string", "Comando, p.ej. 'pytest -q tests/test_x.py'")), self.run_command),
        ]


def _schema(required: list[str] | None = None, **props: tuple[str, str]) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
        "required": list(props) if required is None else required,
        "additionalProperties": False,
    }
