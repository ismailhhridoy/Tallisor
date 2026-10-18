"""Where a user's own data lives on their machine.

    TALLISOR_WORKSPACE (default ~/.tallisor)
        adapters/     column mappings the user created for their providers
        profile.json  the business's settings (fees, cash plan, invoices)
        runs/<id>/    each saved reconciliation: inputs, decisions, audit log
        store.sqlite  index of runs and decisions

Nothing is sent anywhere. Deleting the folder removes everything.
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path

# In hosted mode the web app sets a private folder for each visitor's session,
# so nothing one visitor uploads or saves is visible to another.
_session_root: contextvars.ContextVar = contextvars.ContextVar("tallisor_session_root", default=None)


def hosted() -> bool:
    """True when running as a shared web service (set TALLISOR_HOSTED=1)."""
    return os.getenv("TALLISOR_HOSTED", "").strip().lower() in ("1", "true", "yes")


def use_session_root(path: Path) -> None:
    _session_root.set(Path(path))


def root() -> Path:
    override = _session_root.get()
    if override is not None:
        path = override
    else:
        path = Path(os.getenv("TALLISOR_WORKSPACE", Path.home() / ".tallisor")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def adapters_dir() -> Path:
    path = root() / "adapters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runs_dir() -> Path:
    path = root() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path
