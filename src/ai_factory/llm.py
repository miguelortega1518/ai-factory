"""Bucle de agente genérico sobre la API de Claude.

Cada agente de la fábrica es: (system prompt, herramientas, modelo) y termina llamando a la
herramienta `submit_result` con un objeto validado por Pydantic. Esto da salidas estructuradas
y auditables sin depender de `tool_choice` forzado (no soportado en los modelos actuales).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .audit import AuditLog
from .config import AgentConfig

T = TypeVar("T", bound=BaseModel)

# Modelos que aceptan el parámetro server-side `fallbacks: "default"` en la API de Claude.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(RuntimeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., str | list]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class LLMClient(Protocol):
    def create(self, **params: Any) -> Any: ...


class AnthropicClient:
    """Adaptador fino sobre el SDK oficial. Usa streaming para no chocar con timeouts."""

    def __init__(self, use_fallbacks: bool = True):
        import anthropic

        self._client = anthropic.Anthropic()
        self.use_fallbacks = use_fallbacks

    def create(self, **params: Any) -> Any:
        if self.use_fallbacks and params.get("model") in _FALLBACK_MODELS:
            params = {**params, "betas": [_FALLBACK_BETA], "fallbacks": "default"}
        with self._client.beta.messages.stream(**params) as stream:
            return stream.get_final_message()


def _block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(vars(block))


@dataclass
class Agent:
    name: str
    system: str
    config: AgentConfig
    client: LLMClient
    audit: AuditLog
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 60

    def run(self, prompt: str | list, output: type[T]) -> T:
        submit_schema = output.model_json_schema()
        tools = [t for t in self.tools]
        tool_map = {t.name: t for t in tools}
        api_tools = [t.spec() for t in tools] + [
            {
                "name": "submit_result",
                "description": (
                    "Entrega el resultado final de tu trabajo. Llama a esta herramienta exactamente "
                    "una vez, cuando hayas terminado. El input debe cumplir el esquema."
                ),
                "input_schema": submit_schema,
            }
        ]
        system = (
            self.system
            + "\n\nCuando termines, llama a la herramienta `submit_result` con tu resultado final. "
            "No respondas sólo con texto."
        )
        messages: list[dict] = [{"role": "user", "content": prompt}]
        self.audit.event(self.name, "start", model=self.config.model, prompt=prompt)

        for turn in range(self.max_turns):
            response = self.client.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=system,
                tools=api_tools,
                messages=messages,
                output_config={"effort": self.config.effort},
                cache_control={"type": "ephemeral"},
            )
            usage = getattr(response, "usage", None)
            self.audit.event(
                self.name,
                "response",
                turn=turn,
                stop_reason=response.stop_reason,
                usage=_block_to_dict(usage) if usage else None,
            )

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise AgentError(f"{self.name}: el modelo rechazó la petición ({details})")
            if response.stop_reason == "max_tokens":
                raise AgentError(f"{self.name}: respuesta truncada por max_tokens; súbelo en factory.toml")

            content = [_block_to_dict(b) for b in response.content]
            messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                messages.append(
                    {"role": "user", "content": "Debes llamar a `submit_result` para entregar el resultado."}
                )
                continue

            results = []
            final: T | None = None
            for use in tool_uses:
                name, args = use["name"], use.get("input") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                if name == "submit_result":
                    try:
                        final = output.model_validate(args)
                        results.append(_result(use["id"], "Resultado recibido."))
                    except ValidationError as e:
                        results.append(_result(use["id"], f"Resultado inválido: {e}", error=True))
                    continue
                tool = tool_map.get(name)
                if tool is None:
                    results.append(_result(use["id"], f"Herramienta desconocida: {name}", error=True))
                    continue
                try:
                    out = tool.fn(**args)
                    is_err = False
                except Exception as e:  # el error vuelve al modelo para que se corrija
                    out, is_err = f"{type(e).__name__}: {e}", True
                logged = out[:2000] if isinstance(out, str) else "(contenido multimedia)"
                self.audit.event(self.name, "tool", tool=name, args=args, error=is_err, output=logged)
                results.append(_result(use["id"], out, error=is_err))

            if final is not None:
                self.audit.event(self.name, "result", result=final.model_dump())
                return final
            messages.append({"role": "user", "content": results})

        raise AgentError(f"{self.name}: no entregó resultado tras {self.max_turns} turnos")


def _result(tool_use_id: str, content: str | list, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content or "(vacío)"}
    if error:
        block["is_error"] = True
    return block
