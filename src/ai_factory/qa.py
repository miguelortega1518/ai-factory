"""Tester funcional: arranca la app del worktree y deja que un agente la pruebe en un navegador real.

Flujo por iteración (sólo si el diff toca `qa.paths`):
1. Una vez por tarea, `qa.setup_command` (migrar y sembrar la base de datos de prueba).
2. `qa.prepare_command` y `qa.start_command` levantan la app en `qa.port`; se espera a `qa.ready_path`.
3. El agente `qa` escribe y ejecuta scripts de Playwright en un directorio propio, fuera del repo,
   guarda capturas y entrega un `QAReport` criterio por criterio.
4. La app se para siempre. Si el agente tocó el código del worktree, se deshace y se registra.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import git_ops
from .audit import AuditLog
from .config import FactoryConfig
from .tools import SUBPROCESS_ENV


class QAEnvironmentError(RuntimeError):
    """La infraestructura de pruebas (base de datos, preparación) falló: no es culpa del cambio."""


def applies(cfg: FactoryConfig, changed_files: list[str]) -> bool:
    if not cfg.qa.enabled or not cfg.qa.start_command:
        return False
    if not cfg.qa.paths:
        return True
    return any(fnmatch.fnmatch(f, p) for f in changed_files for p in cfg.qa.paths)


def qa_env(cfg: FactoryConfig) -> dict[str, str]:
    env = {**SUBPROCESS_ENV, **cfg.env, **cfg.qa.env,
           "PORT": str(cfg.qa.port), "HOSTNAME": "127.0.0.1", "QA_BASE_URL": cfg.qa.url}
    # La app de prueba es local: que nada intente salir por el proxy para llegar a ella.
    env["NO_PROXY"] = ",".join(x for x in [env.get("NO_PROXY", ""), "127.0.0.1", "localhost"] if x)
    env["no_proxy"] = env["NO_PROXY"]
    if "NODE_PATH" not in env and shutil.which("npm"):
        r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True)
        if r.returncode == 0:
            env["NODE_PATH"] = r.stdout.strip()  # para `require('playwright')` instalado globalmente
    return env


def _shell(cmd: str, cwd: Path, env: dict, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def setup_database(cfg: FactoryConfig, wt: Path, audit: AuditLog) -> None:
    if not cfg.qa.setup_command:
        return
    r = _shell(cfg.qa.setup_command, wt, qa_env(cfg))
    audit.event("qa", "setup", code=r.returncode, output=(r.stdout + r.stderr)[-4000:])
    if r.returncode != 0:
        raise QAEnvironmentError(f"qa.setup_command falló (exit {r.returncode}): {(r.stdout + r.stderr)[-1500:]}")


def _ready(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


@contextmanager
def running_app(cfg: FactoryConfig, wt: Path, audit: AuditLog):
    """Arranca la app; si no responde, lanza RuntimeError con el final de su log (puede ser culpa del cambio)."""
    env = qa_env(cfg)
    if cfg.qa.prepare_command:
        r = _shell(cfg.qa.prepare_command, wt, env)
        if r.returncode != 0:
            raise RuntimeError(f"qa.prepare_command falló: {(r.stdout + r.stderr)[-1500:]}")
    log_path = audit.run_dir / "qa-app.log"
    log = log_path.open("w")
    proc = subprocess.Popen(cfg.qa.start_command, shell=True, cwd=wt, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        deadline = time.monotonic() + cfg.qa.ready_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if _ready(cfg.qa.url + cfg.qa.ready_path):
                audit.event("qa", "app_ready", url=cfg.qa.url)
                yield cfg.qa.url
                return
            time.sleep(1)
        log.flush()
        tail = log_path.read_text(errors="replace")[-2000:]
        raise RuntimeError(f"La app no arrancó en {cfg.qa.url}{cfg.qa.ready_path} "
                           f"(exit={proc.poll()}). Final del log:\n{tail}")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log.close()


def guard_worktree(wt: Path, audit: AuditLog) -> bool:
    """El tester no debe modificar el código: si lo hizo, se deshace. Devuelve True si había cambios."""
    dirty = git_ops.git(wt, "status", "--porcelain")
    if not dirty:
        return False
    git_ops.git(wt, "checkout", "--", ".")
    git_ops.git(wt, "clean", "-fd")
    audit.event("qa", "touched_code_reverted", changes=dirty[:2000])
    return True


def collect_screenshots(qa_dir: Path, names: list[str], audit: AuditLog, limit: int = 8) -> list[Path]:
    """Copia las capturas a la auditoría y devuelve sus rutas (sólo las que existen, sin salir de qa_dir)."""
    out = []
    root = qa_dir.resolve()
    for name in names[:limit]:
        p = (qa_dir / name).resolve()
        if root not in p.parents or not p.is_file():
            continue
        dest = audit.run_dir / "qa" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        out.append(dest)
    return out
