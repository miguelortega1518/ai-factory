"""Construcción de los agentes de la fábrica."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..audit import AuditLog
from ..brain import Brain
from ..config import FactoryConfig
from ..llm import Agent, LLMClient, Tool
from ..models import BuildReport, GateDecision, Review, Spec, Task, Verdict
from ..tools import Workspace, _schema
from . import prompts

REVIEWER_NAMES = ("security", "ux", "ui", "code")


class Agents:
    def __init__(self, cfg: FactoryConfig, client: LLMClient, audit: AuditLog):
        self.cfg, self.client, self.audit = cfg, client, audit

    def _agent(self, name: str, system: str, tools: list[Tool]) -> Agent:
        return Agent(
            name=name,
            system=system,
            config=self.cfg.agent(name),
            client=self.client,
            audit=self.audit,
            tools=tools,
            max_turns=self.cfg.max_agent_turns,
        )

    # 2. Spec Writer --------------------------------------------------------------------
    def write_spec(self, task: Task, ws: Workspace, brain: Brain) -> Spec:
        agent = self._agent("spec_writer", prompts.SPEC_WRITER, brain.tools() + ws.read_tools())
        prompt = (
            f"# Tarea ({task.source.value})\n**{task.title}**\n\n{task.body}\n\n"
            f"URL: {task.url or '-'}\nEtiquetas: {', '.join(task.labels) or '-'}\n\n"
            f"# Índice del Brain\n{brain.index()}\n\n"
            "Consulta las notas relevantes del Brain y el código antes de escribir la spec."
        )
        return agent.run(prompt, Spec)

    # 3. Builder ------------------------------------------------------------------------
    def build(self, spec: Spec, ws: Workspace, brain: Brain, feedback: list[str] | None = None) -> BuildReport:
        tools = ws.read_tools() + ws.write_tools() + ws.exec_tools() + brain.tools()
        agent = self._agent("builder", prompts.BUILDER, tools)
        prompt = (
            f"# Especificación\n```json\n{spec.model_dump_json(indent=2)}\n```\n\n"
            f"Comando de test del proyecto: `{self.cfg.test_command}`\n"
        )
        if feedback:
            prompt += (
                "\n# Cambios requeridos por el Aprobador (iteración anterior)\n"
                + "\n".join(f"- {f}" for f in feedback)
                + "\n\nEl código de la iteración anterior ya está en el workspace. Corrige estos puntos."
            )
        return agent.run(prompt, BuildReport)

    # 4. Revisión paralela --------------------------------------------------------------
    def review_all(self, spec: Spec, diff: str, ws: Workspace) -> list[Review]:
        context = (
            f"# Especificación\n```json\n{spec.model_dump_json(indent=2)}\n```\n\n"
            f"# Diff contra {self.cfg.base_branch}\n```diff\n{diff[:150000]}\n```"
        )

        def one(name: str) -> Review:
            tools = ws.read_tools()
            if name == "ui" and self.cfg.ui_preview_url:
                tools.append(_screenshot_tool(self.cfg.ui_preview_url))
            agent_name = f"reviewer_{name}"
            review = self._agent(agent_name, prompts.REVIEWERS[name], tools).run(context, Review)
            return review.model_copy(update={"reviewer": name, "model": self.cfg.agent(agent_name).model})

        with ThreadPoolExecutor(max_workers=len(REVIEWER_NAMES)) as pool:
            return list(pool.map(one, REVIEWER_NAMES))

    # 5. Aprobador ----------------------------------------------------------------------
    def approve(self, spec: Spec, build: BuildReport, diff: str, reviews: list[Review], ws: Workspace) -> Verdict:
        agent = self._agent("approver", prompts.APPROVER, ws.read_tools())
        prompt = (
            f"# Especificación\n```json\n{spec.model_dump_json(indent=2)}\n```\n\n"
            f"# Informe del Builder\n```json\n{build.model_dump_json(indent=2)}\n```\n\n"
            f"# Revisiones\n```json\n[{','.join(r.model_dump_json() for r in reviews)}]\n```\n\n"
            f"# Diff\n```diff\n{diff[:150000]}\n```"
        )
        return agent.run(prompt, Verdict)

    # 6. Gate final ---------------------------------------------------------------------
    def gate(self, task: Task, spec: Spec, diff: str, files: list[str], core_hits: list[str]) -> GateDecision:
        agent = self._agent("gatekeeper", prompts.GATEKEEPER, [])
        prompt = (
            f"# Tarea\n{task.title}\n\n# Spec\n{spec.summary}\n\n"
            f"# Archivos cambiados\n" + "\n".join(f"- {f}" for f in files) + "\n\n"
            + (f"# Rutas núcleo tocadas (según configuración)\n{core_hits}\n\n" if core_hits else "")
            + f"# Diff\n```diff\n{diff[:100000]}\n```"
        )
        return agent.run(prompt, GateDecision)


def _screenshot_tool(base_url: str) -> Tool:
    """Captura la app en ejecución para el revisor de UI (computer-use ligero con Playwright)."""

    def screenshot(path: str = "/", width: int = 1280) -> list:
        from playwright.sync_api import sync_playwright

        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": int(width), "height": 900})
            page.goto(url, wait_until="networkidle", timeout=30000)
            png = page.screenshot(full_page=True)
            browser.close()
        return [
            {"type": "text", "text": f"Captura de {url} a {width}px"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": base64.standard_b64encode(png).decode()}},
        ]

    return Tool(
        "screenshot",
        f"Hace una captura de pantalla de la app en ejecución ({base_url}). Prueba también ancho móvil (390).",
        _schema(path=("string", "Ruta, p.ej. '/'"), width=("integer", "Ancho del viewport"), required=["path"]),
        screenshot,
    )
