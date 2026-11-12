"""Motor alternativo: cada agente se ejecuta con Claude Code en modo headless (`claude -p`).

Permite usar una suscripción de Claude (Pro/Max/Team/Enterprise) mediante un token generado con
`claude setup-token` y guardado en `CLAUDE_CODE_OAUTH_TOKEN`, en vez de una API key de pago por uso.

Cada agente recibe sólo las herramientas de su rol:
- "read":  Read, Glob, Grep (Spec Writer, revisores, Aprobador, Gatekeeper)
- "build": además Edit, Write y Bash limitado a `allowed_commands` (Builder)
y corre en `--permission-mode dontAsk`: todo lo que no esté permitido se deniega sin preguntar.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .audit import AuditLog
from .config import AgentConfig
from .llm import AgentError
from .tools import SUBPROCESS_ENV

T = TypeVar("T", bound=BaseModel)

READ_TOOLS = "Read,Glob,Grep"
BUILD_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
# Utilidades de sólo lectura que los modelos encadenan con frecuencia (p. ej. `pytest; echo "exit=$?"`).
# Si falta una, Claude Code deniega el comando compuesto entero.
SAFE_SHELL = ("cd", "echo", "pwd", "ls", "cat", "head", "tail", "wc", "grep", "sort", "true", "which")
# Subcomandos de git que el Builder puede usar (lectura, y `rm` para borrar archivos del repo).
_GIT_READONLY = ("status", "diff", "log", "show", "rm")  # rm: borrar archivos del repo (la fábrica commitea)
# La fábrica gestiona ramas, commits y push: los agentes no.
_DENY = [
    "Bash(git push *)", "Bash(git commit *)", "Bash(git reset *)", "Bash(git checkout *)",
    "Bash(git rebase *)", "Bash(git clean *)", "Bash(git branch *)", "Bash(git worktree *)",
    "Bash(npx *)", "Bash(curl *)", "Bash(wget *)", "Bash(rm -rf *)",
]

BRAIN_HINT = (
    "\n\nEl Brain (memoria persistente del proyecto) está en `.factory/brain/` dentro del repositorio: "
    "notas Markdown en architecture/, conventions/, decisions/ y learnings/. Léelo con Read, Glob y Grep."
)

_counter = itertools.count(1)


def bash_rules(allowed_commands: list[str]) -> list[str]:
    rules: list[str] = []
    for cmd in dict.fromkeys([*allowed_commands, *SAFE_SHELL]):
        if cmd == "git":
            for sub in _GIT_READONLY:
                rules += [f"Bash(git {sub})", f"Bash(git {sub} *)"]
        else:
            rules += [f"Bash({cmd})", f"Bash({cmd} *)"]
    return rules


def subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**SUBPROCESS_ENV, **(extra or {})}
    # Si hay token de suscripción, que no se use una API key por accidente (tiene prioridad y se factura).
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


@dataclass
class ClaudeCodeAgent:
    name: str
    system: str
    config: AgentConfig
    audit: AuditLog
    cwd: Path
    profile: str = "read"  # "read" | "build"
    allowed_commands: list[str] = field(default_factory=list)
    max_turns: int = 60
    claude_bin: str = "claude"
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 3600
    add_dirs: list[Path] = field(default_factory=list)  # directorios extra legibles (--add-dir)

    def command(self, schema: dict) -> list[str]:
        args = [
            self.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema),
            "--model", self.config.model,
            "--effort", self.config.effort,
            "--max-turns", str(self.max_turns),
            "--permission-mode", "dontAsk",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--append-system-prompt", self.system + BRAIN_HINT,
        ]
        for d in self.add_dirs:
            args += ["--add-dir", str(d)]
        if self.profile == "qa":
            # Escribe y ejecuta sus scripts de Playwright con node; nada de git, npx ni red externa.
            args += ["--tools", BUILD_TOOLS,
                     "--allowedTools", ",".join(["Edit", "Write", *bash_rules(["node", "mkdir"])]),
                     "--disallowedTools", ",".join(_DENY)]
        elif self.profile == "build":
            args += ["--tools", BUILD_TOOLS,
                     "--allowedTools", ",".join(["Edit", "Write", *bash_rules(self.allowed_commands)]),
                     "--disallowedTools", ",".join(_DENY)]
        else:
            args += ["--tools", READ_TOOLS]
        return args

    def run(self, prompt: str, output: type[T]) -> T:
        schema = output.model_json_schema()
        args = self.command(schema)
        n = next(_counter)
        self.audit.event(self.name, "start", backend="claude-code", model=self.config.model,
                         profile=self.profile, prompt=prompt)
        try:
            proc = subprocess.run(args, input=prompt, cwd=self.cwd, capture_output=True, text=True,
                                  timeout=self.timeout, env=subprocess_env(self.env))
        except FileNotFoundError as e:
            raise AgentError(f"No se encontró `{self.claude_bin}`. Instala Claude Code: curl -fsSL https://claude.ai/install.sh | bash") from e
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"{self.name}: Claude Code superó {self.timeout}s") from e

        transcript = self.audit.artifact(f"{self.name}-{n}.jsonl", proc.stdout)
        result = _result_event(proc.stdout)
        if result is None:
            raise AgentError(f"{self.name}: Claude Code terminó sin resultado (exit {proc.returncode}): "
                             f"{(proc.stderr or proc.stdout)[-1500:]}")
        self.audit.event(
            self.name, "response", backend="claude-code", subtype=result.get("subtype"),
            is_error=result.get("is_error"), num_turns=result.get("num_turns"),
            cost_usd_equivalent=result.get("total_cost_usd"), usage=result.get("usage"),
            permission_denials=result.get("permission_denials"), transcript=transcript.name,
        )
        structured = result.get("structured_output")
        if result.get("is_error") or structured is None:
            raise AgentError(f"{self.name}: Claude Code no entregó un resultado válido "
                             f"({result.get('subtype')}): {str(result.get('result'))[:1500]}")
        try:
            final = output.model_validate(structured)
        except ValidationError as e:
            raise AgentError(f"{self.name}: resultado fuera de esquema: {e}") from e
        self.audit.event(self.name, "result", result=final.model_dump())
        return final


def _result_event(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            return event
    return None


def check_auth(claude_bin: str = "claude", env: dict[str, str] | None = None) -> dict:
    """Devuelve `claude auth status` como dict (lanza AgentError si no hay sesión)."""
    try:
        proc = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=60,
                              env=subprocess_env(env))
    except FileNotFoundError as e:
        raise AgentError(f"No se encontró `{claude_bin}`") from e
    try:
        status = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        status = {}
    if proc.returncode != 0 or not status.get("loggedIn"):
        raise AgentError("Claude Code no está autenticado. Define CLAUDE_CODE_OAUTH_TOKEN (claude setup-token).")
    return status


__all__ = ["ClaudeCodeAgent", "bash_rules", "check_auth", "subprocess_env"]
