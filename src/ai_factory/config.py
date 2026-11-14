"""Configuración de la fábrica (factory.toml)."""

from __future__ import annotations

import hashlib
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentConfig:
    model: str
    effort: str = "high"
    max_tokens: int = 32000


# Por defecto el vídeo usa Opus para spec/aprobador y mezcla modelos en los revisores
# para "evitar sesgos". Aquí cada agente es configurable de forma independiente.
DEFAULT_AGENTS: dict[str, AgentConfig] = {
    "spec_writer": AgentConfig("claude-opus-5-5", "high"),
    "builder": AgentConfig("claude-opus-5-5", "high", 64000),
    "reviewer_security": AgentConfig("claude-opus-5-5", "high"),
    "reviewer_ux": AgentConfig("claude-sonnet-5-5", "medium"),
    "reviewer_ui": AgentConfig("claude-sonnet-5-5", "medium"),
    "reviewer_code": AgentConfig("claude-sonnet-5-5", "high"),
    "approver": AgentConfig("claude-opus-5-5", "high"),
    "gatekeeper": AgentConfig("claude-opus-5-5", "medium"),
    # Mejora continua: retrospectiva tras cada tarea (ligera) y revisión periódica del producto (profunda).
    "improver": AgentConfig("claude-sonnet-5-5", "medium"),
    "scout": AgentConfig("claude-opus-5-5", "high"),
    # Tester funcional: escribe y ejecuta pruebas de navegador sobre la app en marcha.
    "qa": AgentConfig("claude-sonnet-5-5", "medium"),
}


@dataclass
class QAConfig:
    """Tester funcional: arranca la app y comprueba el cambio en un navegador real (Playwright)."""

    enabled: bool = False
    # Sólo se ejecuta si el diff toca alguna de estas rutas (vacío = siempre).
    paths: list[str] = field(default_factory=list)
    # Una vez por tarea, antes de la primera prueba: preparar la base de datos (migrar, sembrar…).
    setup_command: str = ""
    # Antes de cada arranque, tras la build (p. ej. copiar estáticos a .next/standalone).
    prepare_command: str = ""
    # Arranca la app en primer plano; recibe PORT y HOSTNAME en el entorno.
    start_command: str = ""
    port: int = 3100
    ready_path: str = "/"
    ready_timeout: int = 120
    # Cómo entrar en la app de prueba (usuarios del seed, nunca credenciales reales).
    credentials: str = ""
    env: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class ForjaConfig:
    """Registrar el trabajo de la fábrica como tareas de un proyecto de Forja.

    Credenciales sólo por entorno: FORJA_EMAIL y FORJA_PASSWORD (la URL también puede venir de FORJA_URL).
    """

    enabled: bool = False
    url: str = ""
    project: str = "AI Factory"


@dataclass
class FactoryConfig:
    repo_path: Path
    base_branch: str = "main"
    test_command: str = "pytest -q"
    # Se ejecuta en cada worktree recién creado, antes de que trabaje ningún agente (p. ej. "npm ci").
    setup_command: str = ""
    # Variables de entorno NO secretas para setup, tests y comandos de los agentes.
    env: dict[str, str] = field(default_factory=dict)
    allowed_commands: list[str] = field(
        default_factory=lambda: ["pytest", "python", "npm", "npx", "node", "ruff", "ls", "cat", "git"]
    )
    max_iterations: int = 3
    max_agent_turns: int = 60
    # Rutas que se consideran "núcleo": si se tocan, siempre se exige aprobación humana.
    core_paths: list[str] = field(default_factory=list)
    # Qué hacer con cambios de bajo riesgo: "merge" (fusiona en base_branch y hace push),
    # "pr" (abre PR con auto-merge) o "none" (sólo deja la rama).
    auto_ship: str = "merge"
    deploy_command: str = ""
    push: bool = True
    ui_preview_url: str = ""
    use_fallbacks: bool = True
    # Motor de los agentes: "api" (SDK con ANTHROPIC_API_KEY) o "claude-code" (CLI `claude -p`,
    # que puede usar una suscripción de Claude vía CLAUDE_CODE_OAUTH_TOKEN).
    backend: str = "api"
    claude_bin: str = "claude"
    agents: dict[str, AgentConfig] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    qa: QAConfig = field(default_factory=QAConfig)
    forja: ForjaConfig = field(default_factory=ForjaConfig)
    github_repo: str = ""  # owner/repo para intake de issues
    github_label: str = "ai-factory"
    # Comentar el resultado en el issue de GitHub de origen y cerrarlo si se fusionó.
    issue_feedback: bool = True
    # Mejora continua
    retro: bool = True  # retrospectiva tras cada tarea
    improve_max_issues: int = 3  # propuestas como máximo por revisión periódica
    proposal_label: str = "ai-factory-propuesta"  # etiqueta de las propuestas (NO lanza la fábrica)
    followup_issues: int = 2  # tareas de seguimiento por tarea, de hallazgos fuera de alcance (0 = ninguna)
    sentry_org: str = ""
    sentry_project: str = ""

    @property
    def factory_dir(self) -> Path:
        return self.repo_path / ".factory"

    @property
    def brain_dir(self) -> Path:
        return self.factory_dir / "brain"

    @property
    def worktrees_dir(self) -> Path:
        # Fuera del repo: así las herramientas del proyecto (tsc, vitest, eslint…) no ven copias duplicadas.
        tag = hashlib.sha1(str(self.repo_path).encode()).hexdigest()[:8]
        return Path(tempfile.gettempdir()) / "ai-factory-worktrees" / f"{self.repo_path.name}-{tag}"

    @property
    def runs_dir(self) -> Path:
        return self.factory_dir / "runs"

    def agent(self, name: str) -> AgentConfig:
        return self.agents.get(name) or DEFAULT_AGENTS[name]


def load_config(repo_path: str | Path = ".", config_file: str | Path | None = None) -> FactoryConfig:
    repo = Path(repo_path).resolve()
    path = Path(config_file) if config_file else repo / "factory.toml"
    data: dict = {}
    if path.exists():
        data = tomllib.loads(path.read_text())

    agents = dict(DEFAULT_AGENTS)
    for name, raw in (data.pop("agents", {}) or {}).items():
        base = agents.get(name, AgentConfig("claude-opus-5-5"))
        agents[name] = AgentConfig(
            model=raw.get("model", base.model),
            effort=raw.get("effort", base.effort),
            max_tokens=int(raw.get("max_tokens", base.max_tokens)),
        )

    qa_raw = data.pop("qa", {}) or {}
    qa_unknown = set(qa_raw) - set(QAConfig.__dataclass_fields__)
    if qa_unknown:
        raise ValueError(f"Claves desconocidas en [qa] de {path}: {sorted(qa_unknown)}")
    qa = QAConfig(**qa_raw)
    forja_raw = data.pop("forja", {}) or {}
    forja_unknown = set(forja_raw) - set(ForjaConfig.__dataclass_fields__)
    if forja_unknown:
        raise ValueError(f"Claves desconocidas en [forja] de {path}: {sorted(forja_unknown)}")
    forja = ForjaConfig(**forja_raw)

    known = set(FactoryConfig.__dataclass_fields__) - {"repo_path", "agents", "qa", "forja"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Claves desconocidas en {path}: {sorted(unknown)}")
    cfg = FactoryConfig(repo_path=repo, agents=agents, qa=qa, forja=forja, **data)
    if cfg.backend not in ("api", "claude-code"):
        raise ValueError(f"backend debe ser 'api' o 'claude-code', no {cfg.backend!r}")
    return cfg
