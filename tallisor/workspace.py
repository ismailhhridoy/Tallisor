"""Where a user's own data lives on their machine.

    TALLISOR_WORKSPACE (default ~/.tallisor)
        adapters/     column mappings the user created for their providers
        profile.json  the business's settings (fees, cash plan, invoices)
        runs/<id>/    each saved reconciliation: inputs, decisions, audit log
        store.sqlite  index of runs and decisions

Nothing is sent anywhere. Deleting the folder removes everything.
"""
from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
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
