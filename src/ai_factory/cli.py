"""CLI: `factory init | run | intake | status | approve | reject | brain`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .brain import Brain
from .config import load_config
from .intake import (
    fetch_github_issues,
    fetch_sentry_issues,
    task_from_github_issue,
    task_from_sentry_event,
    task_from_text,
)
from .models import RunStatus
from .pipeline import Factory, summarize


def _factory(args) -> Factory:
    cfg = load_config(args.repo, args.config)
    from .llm import AnthropicClient

    return Factory(cfg, AnthropicClient(use_fallbacks=cfg.use_fallbacks))


def _processed(cfg_dir: Path) -> set[str]:
    p = cfg_dir / "processed.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


def _mark_processed(cfg_dir: Path, ids: set[str]) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "processed.json").write_text(json.dumps(sorted(ids)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description="AI Factory: fábrica de software con agentes")
    ap.add_argument("--repo", default=".", help="Ruta al repositorio objetivo")
    ap.add_argument("--config", default=None, help="Ruta a factory.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Prepara el repo (Brain, factory.toml)")

    r = sub.add_parser("run", help="Ejecuta el pipeline para una tarea")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--task", help="Descripción de la tarea en texto")
    src.add_argument("--github-issue", type=int, help="Número de issue (usa github_repo de la config)")
    src.add_argument("--sentry-json", help="Archivo JSON con un issue/webhook de Sentry")

    i = sub.add_parser("intake", help="Procesa tareas pendientes de GitHub/Sentry")
    i.add_argument("--source", choices=["github", "sentry", "all"], default="all")
    i.add_argument("--limit", type=int, default=5)
    i.add_argument("--watch", type=int, default=0, help="Repite cada N segundos (0 = una vez)")

    sub.add_parser("status", help="Lista las ejecuciones")
    a = sub.add_parser("approve", help="Aprobación humana de un cambio crítico")
    a.add_argument("run_id")
    rj = sub.add_parser("reject", help="Rechaza un cambio pendiente")
    rj.add_argument("run_id")
    rj.add_argument("--reason", default="")

    b = sub.add_parser("brain", help="Gestiona la memoria persistente")
    bsub = b.add_subparsers(dest="bcmd", required=True)
    bsub.add_parser("list")
    bs = bsub.add_parser("search")
    bs.add_argument("query")
    ba = bsub.add_parser("add")
    ba.add_argument("category", choices=["architecture", "decisions", "conventions", "learnings"])
    ba.add_argument("title")
    ba.add_argument("body")

    args = ap.parse_args(argv)
    cfg = load_config(args.repo, args.config)

    if args.cmd == "init":
        Factory(cfg, client=None).init()  # type: ignore[arg-type]
        print(f"Listo. Brain en {cfg.brain_dir}. Edita factory.toml y haz commit de .factory/brain.")
        return 0

    if args.cmd == "brain":
        brain = Brain(cfg.brain_dir)
        if args.bcmd == "list":
            print(brain.index())
        elif args.bcmd == "search":
            print(brain.search(args.query))
        else:
            print(brain.add(args.category, args.title, args.body))
        return 0

    if args.cmd == "status":
        for s in Factory(cfg, client=None).list_runs():  # type: ignore[arg-type]
            print(json.dumps(summarize(s), ensure_ascii=False))
        return 0

    factory = _factory(args)

    if args.cmd == "run":
        if args.task:
            task = task_from_text(args.task)
        elif args.github_issue:
            if not cfg.github_repo:
                ap.error("Define github_repo en factory.toml")
            import subprocess

            out = subprocess.run(
                ["gh", "issue", "view", str(args.github_issue), "--repo", cfg.github_repo,
                 "--json", "number,title,body,url,labels"], capture_output=True, text=True, check=True,
            ).stdout
            task = task_from_github_issue(json.loads(out), cfg.github_repo)
        else:
            task = task_from_sentry_event(json.loads(Path(args.sentry_json).read_text()))
        state = factory.run(task)
        print(json.dumps(summarize(state), ensure_ascii=False, indent=2))
        return 0 if state.status in (RunStatus.SHIPPED, RunStatus.AWAITING_HUMAN) else 1

    if args.cmd == "intake":
        while True:
            tasks = []
            if args.source in ("github", "all") and cfg.github_repo:
                tasks += fetch_github_issues(cfg.github_repo, cfg.github_label, args.limit)
            if args.source in ("sentry", "all") and cfg.sentry_org and cfg.sentry_project:
                tasks += fetch_sentry_issues(cfg.sentry_org, cfg.sentry_project, args.limit)
            done = _processed(cfg.factory_dir)
            pending = [t for t in tasks if t.id not in done]
            print(f"{len(pending)} tareas nuevas")
            for t in pending:
                factory.run(t)
                done.add(t.id)
                _mark_processed(cfg.factory_dir, done)
            if not args.watch:
                return 0
            time.sleep(args.watch)

    if args.cmd == "approve":
        print(json.dumps(summarize(factory.approve(args.run_id)), ensure_ascii=False))
        return 0
    if args.cmd == "reject":
        print(json.dumps(summarize(factory.reject(args.run_id, args.reason)), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
