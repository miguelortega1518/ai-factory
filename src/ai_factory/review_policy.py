"""Conservative, deterministic reviewer selection. Unknown changes get all reviewers."""
from pathlib import PurePosixPath
from .models import Spec

REVIEWERS = ("security", "ux", "ui", "code")
POLICY_VERSION = 1


def select_reviewers(files: list[str], spec: Spec, diff: str = "") -> dict[str, str]:
    reasons = {"security": "Siempre: control de seguridad", "code": "Siempre: revisión técnica"}
    # Only a narrow allowlist is eligible for skipping. Public docs, APIs, email,
    # config and dependencies can alter user experience indirectly: keep all.
    internal_docs = bool(files) and all(
        f.startswith((".factory/brain/learnings/", ".factory/brain/conventions/"))
        and PurePosixPath(f).suffix == ".md" for f in files
    )
    if internal_docs and not spec.touches_ui:
        # Flag executable/visual references even inside the allowlisted notes.
        suspicious = any(s in diff.lower() for s in ("<script", "<iframe", "<style", "<img", ".tsx", ".jsx"))
        if not suspicious:
            return {**reasons, "ux": "omit: Sólo notas internas del Brain", "ui": "omit: Sólo notas internas del Brain"}
    return {**reasons, "ux": "Impacto de usuario posible o incierto", "ui": "Impacto visual posible o incierto"}


def active_reviewers(selection: dict[str, str], mode: str, extra: list[str] = ()) -> list[str]:
    return [r for r in REVIEWERS if mode != "selective" or not selection[r].startswith("omit:") or r in extra]
