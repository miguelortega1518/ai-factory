"""Download recovery only from the configured repository's trusted base-branch workflow."""
import io
import json
from pathlib import Path
import stat
import subprocess
import zipfile

from .checkpoints import inspect
from .config import FactoryConfig

MAX_ARCHIVE = 256 * 1024 * 1024


def _api(path: str, *, binary=False, runner=subprocess.run):
    r = runner(["gh", "api", path], capture_output=True, timeout=180)
    if r.returncode:
        raise RuntimeError("No se pudo descargar/verificar el artefacto de recuperación")
    return r.stdout if binary else json.loads(r.stdout)


def download(cfg: FactoryConfig, artifact_id: int, destination: Path, *, runner=subprocess.run) -> Path:
    if artifact_id <= 0 or not cfg.github_repo:
        raise ValueError("Artefacto/repositorio inválido")
    repo = cfg.github_repo
    metadata = _api(f"repos/{repo}/actions/artifacts/{artifact_id}", runner=runner)
    source = metadata.get("workflow_run", {})
    run = _api(f"repos/{repo}/actions/runs/{int(source['id'])}", runner=runner)
    if (metadata.get("expired") or metadata.get("size_in_bytes", 0) > MAX_ARCHIVE
        or run.get("status") != "completed"
        or run.get("conclusion") not in {"failure", "cancelled", "timed_out"}
        or run.get("head_branch") != cfg.base_branch
        or run.get("path") != f".github/workflows/{cfg.quota_workflow}"
        or run.get("event") not in {"issues", "workflow_dispatch", "schedule"}
        or run.get("repository", {}).get("full_name") != repo
        or run.get("head_repository", {}).get("full_name") != repo):
        raise ValueError("Origen del checkpoint no autorizado o artefacto vencido")
    raw = _api(f"repos/{repo}/actions/artifacts/{artifact_id}/zip", binary=True, runner=runner)
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("Artefacto demasiado grande")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_ARCHIVE:
            raise ValueError("Artefacto expandido demasiado grande")
        seen = set()
        for entry in z.infolist():
            p = Path(entry.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in entry.filename or entry.filename in seen:
                raise ValueError("Ruta de archivo inválida")
            seen.add(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("No se admiten enlaces en el artefacto")
            target = destination / p
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(entry))
    manifest, state = inspect(destination, cfg)
    expected = f"factory-checkpoint-{state.run_id}-{manifest['generation']}"
    if metadata.get("name") != expected or state.actions_run_id != str(source['id']):
        raise ValueError("Identidad del artefacto no coincide con su ejecución")
    return destination
