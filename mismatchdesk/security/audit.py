"""Tamper-evident audit log.

Each event is one JSON line carrying the SHA-256 of the previous line, so any
edit, deletion or reordering breaks the chain from that point on
(hash chaining as in Crosby & Wallach, USENIX Security 2009). The head hash can
be published elsewhere (for example a public blockchain) to make the whole log
verifiable by a third party.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GENESIS = "0" * 64


def _digest(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Optional[Path] = None, clock=None):
        self.events: list[dict] = []
        self.path = Path(path) if path else None
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")

    @property
    def head(self) -> str:
        return self.events[-1]["hash"] if self.events else GENESIS

    def record(self, actor: str, action: str, outcome: str, tier: Optional[int] = None, **details) -> dict:
        event = {
            "seq": len(self.events) + 1,
            "ts": self._clock(),
            "actor": actor,
            "action": action,
            "tier": f"T{tier}" if tier is not None else None,
            "outcome": outcome,
            "details": details,
            "prev_hash": self.head,
        }
        event["hash"] = _digest(event)
        self.events.append(event)
        if self.path:
            with self.path.open("a") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        return event


def verify_events(events: list[dict]) -> tuple[bool, Optional[int], str]:
    prev = GENESIS
    for i, event in enumerate(events, start=1):
        if event.get("seq") != i:
            return False, i, "sequence number out of order (line missing or reordered)"
        if event.get("prev_hash") != prev:
            return False, i, "previous-hash link broken"
        if _digest(event) != event.get("hash"):
            return False, i, "content does not match its hash (line edited)"
        prev = event["hash"]
    return True, None, f"{len(events)} events verified, head {prev[:16]}"


def verify_file(path: Path) -> tuple[bool, Optional[int], str]:
    events = []
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            return False, n, "line is not valid JSON"
    return verify_events(events)
