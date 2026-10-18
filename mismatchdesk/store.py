"""Saved runs: inputs, settings, decisions and the audit log, kept per run.

Layout under the workspace:
    runs/<run_id>/inputs/<role>__<n>__<file name>   the exact uploaded bytes
    runs/<run_id>/profile.json                      settings used for the run
    runs/<run_id>/decisions.json                    human decisions (replayable)
    runs/<run_id>/audit.jsonl                       hash-chained audit log
    store.sqlite                                    index for the history screen

Reopening a run re-reads the stored bytes, reruns the deterministic engine and
replays decisions only where the fix is unchanged (same proposal id).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .workspace import root, runs_dir

ROLES = ("books", "processors", "bank", "history", "receivables")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(root() / "store.sqlite")
    con.execute("""CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, created TEXT, updated TEXT, business TEXT, sales_date TEXT,
        statement_date TEXT, providers TEXT, gap INTEGER, cases INTEGER, open INTEGER,
        approved INTEGER, shortfall INTEGER, audit_head TEXT)""")
    return con


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:120] or "file"


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _bytes(src) -> tuple[str, bytes]:
    if isinstance(src, tuple):
        return src[0], src[1]
    p = Path(src)
    return p.name, p.read_bytes()


def save_run(session, run_id: str | None = None) -> str:
    """Create or update the stored copy of a session. Returns the run id."""
    sources = {"books": session.sources["books"], "processors": session.sources["processors"],
               "bank": session.sources["bank"], "history": session.history_src,
               "receivables": session.receivables_src}
    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"{stamp}-{inputs_digest(sources)[:8]}"
    folder = runs_dir() / run_id
    inputs = folder / "inputs"
    if not inputs.exists():
        inputs.mkdir(parents=True)
        for role in ROLES:
            for n, src in enumerate(_as_list(sources[role])):
                name, data = _bytes(src)
                (inputs / f"{role}__{n}__{_safe(name)}").write_bytes(data)
    (folder / "profile.json").write_text(json.dumps(session.profile, indent=2))
    (folder / "decisions.json").write_text(json.dumps(session.decisions, indent=2))
    (folder / "audit.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in session.log.events))
    s = session.summary()
    statuses = [c.status for c in session.recon.cases]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _db() as con:
        con.execute("""INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, open=excluded.open,
            approved=excluded.approved, audit_head=excluded.audit_head""",
                    (run_id, now, now, s["business"], s["sales_date"], s["statement_date"],
                     ", ".join(s["providers"]), s["gap"], s["cases"], statuses.count("open"),
                     statuses.count("approved"), s["shortfall"], s["audit_head"]))
    return run_id


def inputs_digest(files: dict) -> str:
    """Fingerprint of the input files (content and role, not file names)."""
    digest = hashlib.sha256()
    for role in ROLES:
        for src in _as_list(files.get(role)):
            digest.update(role.encode() + b"\0" + hashlib.sha256(_bytes(src)[1]).digest())
    return digest.hexdigest()


def find_by_inputs(files: dict) -> str | None:
    """The most recent saved run made from exactly these files, if any."""
    tag = inputs_digest(files)[:8]
    for run in list_runs(500):
        if run["id"].endswith("-" + tag) and (runs_dir() / run["id"] / "inputs").is_dir():
            return run["id"]
    return None


def list_runs(limit: int = 50) -> list[dict]:
    with _db() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stored_inputs(run_id: str) -> dict:
    folder = runs_dir() / run_id / "inputs"
    if not folder.is_dir():
        raise KeyError(run_id)
    found: dict = {role: [] for role in ROLES}
    for f in sorted(folder.iterdir(), key=lambda p: (p.name.split("__")[0], int(p.name.split("__")[1]))):
        role, _, name = f.name.split("__", 2)
        found[role].append((name, f.read_bytes()))
    single = lambda role: found[role][0] if found[role] else None  # noqa: E731
    return {"books": single("books"), "processors": found["processors"], "bank": single("bank"),
            "history": single("history"), "receivables": single("receivables")}


def open_run(run_id: str, profile: dict | None = None, **session_kw):
    """Rebuild a saved run and replay its decisions. Returns (session, replay report).

    By default the run's own saved settings are used (a faithful historical view).
    Pass `profile` to recalculate the same files with current settings; decisions
    whose fix changed as a result are skipped and reported for review.
    """
    from .session import Session
    folder = runs_dir() / run_id
    inputs = stored_inputs(run_id)
    profile = profile or json.loads((folder / "profile.json").read_text())
    decisions = json.loads((folder / "decisions.json").read_text())
    s = Session(books=inputs["books"], processors=inputs["processors"], bank=inputs["bank"], profile=profile,
                history=inputs["history"], receivables=inputs["receivables"], **session_kw).run()
    report = s.replay(decisions)
    return s, report


def delete_run(run_id: str) -> None:
    """Remove a run from the history list. Files are kept on disk for the owner to clear."""
    with _db() as con:
        con.execute("DELETE FROM runs WHERE id = ?", (run_id,))
