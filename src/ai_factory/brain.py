"""El Brain: memoria persistente del proyecto.

Son notas Markdown versionadas en `.factory/brain/` (arquitectura, decisiones, convenciones,
lecciones). Viven en el repo, así que cada cambio de memoria es auditable vía git.
El Spec Writer DEBE consultarlo antes de escribir la spec.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .llm import Tool
from .tools import _schema

CATEGORIES = ("architecture", "decisions", "conventions", "learnings")

_SEED = {
    "architecture/overview.md": "# Visión general de la arquitectura\n\n"
    "_Describe aquí los módulos principales, cómo se comunican y qué partes son núcleo._\n",
    "conventions/code-style.md": "# Convenciones de código\n\n"
    "- Todo cambio de comportamiento lleva tests (TDD).\n"
    "- Mantén los cambios pequeños y enfocados en la tarea.\n",
    "decisions/0001-ai-factory.md": "# ADR 0001: Adoptar la AI Factory\n\n"
    "Los cambios entran por el pipeline intake → spec → build → review → approve → gate.\n",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "nota"


class Brain:
    def __init__(self, root: Path):
        self.root = root

    def init(self) -> None:
        for rel, content in _SEED.items():
            p = self.root / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
        for c in CATEGORIES:
            (self.root / c).mkdir(parents=True, exist_ok=True)

    def notes(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("*.md") if p.is_file())

    def index(self) -> str:
        """Lista de notas con su título, para dar al agente una vista general barata."""
        lines = []
        for p in self.notes():
            first = next((l for l in p.read_text().splitlines() if l.strip()), "")
            lines.append(f"- {p.relative_to(self.root)} — {first.lstrip('# ').strip()}")
        return "\n".join(lines) or "(el Brain está vacío)"

    def read(self, path: str) -> str:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents:
            raise PermissionError("Ruta fuera del Brain")
        return p.read_text()

    def search(self, query: str) -> str:
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        scored = []
        for p in self.notes():
            text = p.read_text()
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                scored.append((score, p, text))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, p, text in scored[:5]:
            out.append(f"## {p.relative_to(self.root)} (score {score})\n{text[:3000]}")
        return "\n\n".join(out) or "(sin resultados)"

    def add(self, category: str, title: str, body: str) -> Path:
        if category not in CATEGORIES:
            raise ValueError(f"Categoría inválida: {category}. Usa una de {CATEGORIES}")
        p = self.root / category / f"{time.strftime('%Y%m%d')}-{_slug(title)}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.write_text(p.read_text().rstrip() + f"\n\n{body.strip()}\n")
        else:
            p.write_text(f"# {title}\n\n{body.strip()}\n")
        return p

    def tools(self) -> list[Tool]:
        return [
            Tool("brain_index", "Lista todas las notas del Brain (memoria persistente del proyecto).",
                 _schema(required=[]), lambda: self.index()),
            Tool("brain_read", "Lee una nota del Brain.",
                 _schema(path=("string", "Ruta relativa dentro del Brain")), self.read),
            Tool("brain_search", "Busca en el Brain notas relevantes para un tema.",
                 _schema(query=("string", "Términos de búsqueda")), self.search),
        ]
