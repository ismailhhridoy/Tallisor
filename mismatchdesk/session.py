"""One reconciliation session: the agent's workflow, permission checks and audit trail.

Every step goes through Policy.authorize() and is written to the hash-chained
audit log, including refusals. The agent role runs the pipeline (tiers T0-T1);
only a human role can approve a fix (T2); T3 actions do not exist.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import cashflow, financing
from .fixes import InvalidProposal, build_proposal
from .ingest import InputRejected, load_bank, load_books, load_processor
from .investigate import explain
from .llm import LLM, NullLLM
from .money import fmt
from .reconcile import CATEGORY_LABELS, reconcile
from .security.audit import AuditLog
from .security.guard import screen_injection
from .security.policy import HUMAN_ROLES, PermissionDenied, Policy

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "data" / "financing_products.json"


class Session:
    def __init__(self, *, books, processors, bank, profile: dict, history, receivables,
                 llm: Optional[LLM] = None, log_path: Optional[Path] = None, clock=None):
        self.sources = {"books": books, "processors": processors, "bank": bank}
        self.profile = profile
        self.history_src, self.receivables_src = history, receivables
        self.llm = llm or NullLLM()
        self.policy = Policy()
        self.log = AuditLog(log_path, clock=clock)
        self.recon = None
        self.forecast = None
        self.alerts: list = []
        self.metrics = None
        self.matches: list = []
        self.adapters: list = []
        self.stats = {"numbers_checked": 0, "numbers_rejected": 0, "injection_flags": 0,
                      "proposals_refused": 0, "llm_explanations": 0}

    # ------------------------------------------------------------- construction
    @classmethod
    def from_dir(cls, path, **kw) -> "Session":
        path = Path(path)
        profile = json.loads((path / "profile.json").read_text())
        f = profile["files"]
        return cls(books=path / f["books"], processors=[path / p for p in f["processors"]],
                   bank=path / f["bank"], profile=profile, history=path / f["history"],
                   receivables=path / f["receivables"], **kw)

    # ------------------------------------------------------------- plumbing
    def _authorize(self, role: str, action: str, **details):
        try:
            tier = self.policy.authorize(role, action)
        except PermissionDenied as exc:
            self.log.record(role, action, "denied", int(self.policy.tier_of(action)), reason=exc.reason, **details)
            raise
        return tier

    def request(self, role: str, action: str, **details) -> None:
        """Ask to perform an action by name. Used for T3 attempts from chat or files."""
        self._authorize(role, action, **details)

    # ------------------------------------------------------------- the agent's run
    def run(self) -> "Session":
        role = "agent"
        t = self._authorize(role, "ingest")
        books = load_books(self.sources["books"])
        psp = []
        for src in self.sources["processors"]:
            txns, cfg = load_processor(src)
            psp.extend(txns)
            self.adapters.append(cfg["name"])
        bank, statement = load_bank(self.sources["bank"])
        self.log.record(role, "ingest", "ok", int(t), books=len(books), provider_rows=len(psp),
                        bank_entries=len(bank), adapters=self.adapters)
        for b in bank:
            hits = screen_injection(b.remittance_info)
            if hits:
                self.stats["injection_flags"] += 1
                self.log.record(role, "screen_input", "flagged", 0, source="bank", entry=b.txn_id, matches=hits,
                                handling="treated as data; no decision uses this text")

        t = self._authorize(role, "reconcile")
        self.recon = reconcile(books, psp, bank, statement, self.profile)
        r = self.recon
        self.log.record(role, "reconcile", "ok", int(t), orders=r.order_count, matched=len(r.matched),
                        cases=len(r.cases), expected=r.expected, received=r.received, gap=r.gap, residual=r.residual)

        for c in r.cases:
            for flag in c.flags:
                if flag["type"] == "injection_suspected":
                    self.stats["injection_flags"] += 1
                    self.log.record(role, "screen_input", "flagged", 0, case=c.id, matches=flag["matches"],
                                    handling="treated as data; no decision uses this text")
            t = self._authorize(role, "investigate", case=c.id)
            outcome = explain(c, self.llm)
            self.stats["numbers_checked"] += outcome.get("numbers_checked", 0)
            self.stats["numbers_rejected"] += outcome.get("numbers_rejected", 0)
            if outcome["source"].startswith("ai:"):
                self.stats["llm_explanations"] += 1
            self.log.record(role, "investigate", "ok", int(t), case=c.id, ref=c.ref, category=c.category,
                            variance=c.variance, **outcome)
            t = self._authorize(role, "draft_fix", case=c.id)
            try:
                c.proposal = build_proposal(c)
            except InvalidProposal as exc:
                self.stats["proposals_refused"] += 1
                c.proposal, c.status = None, "escalated"
                self.log.record(role, "draft_fix", "refused", int(t), case=c.id, reason=str(exc))
                continue
            if c.proposal is None:
                c.status = "escalated"
                self.log.record(role, "draft_fix", "none", int(t), case=c.id, reason="no safe automatic fix; sent to a person")
            else:
                self.log.record(role, "draft_fix", "ok", int(t), case=c.id, proposal=c.proposal.id,
                                balanced=c.proposal.balanced, lines=len(c.proposal.lines))

        t = self._authorize(role, "forecast")
        history = cashflow.load_history(self.history_src)
        receivables = cashflow.load_receivables(self.receivables_src)
        self.forecast = cashflow.build_forecast(r, self.profile, history, receivables)
        fc = self.forecast
        self.log.record(role, "forecast", "ok", int(t), verified_cash=fc.verified_cash, bank=fc.bank_balance,
                        lowest=fc.lowest.closing, shortfall=fc.shortfall)

        t = self._authorize(role, "alerts")
        self.alerts = cashflow.alerts(r, fc, self.profile)
        self.log.record(role, "alerts", "ok", int(t), count=len(self.alerts),
                        critical=sum(a["severity"] == "critical" for a in self.alerts))

        t = self._authorize(role, "match_financing")
        self.metrics, self.matches = financing.match(fc, self.profile, financing.load_catalogue(CATALOGUE))
        self.log.record(role, "match_financing", "ok", int(t), need=self.metrics.need,
                        eligible=[m.product["id"] for m in self.matches if m.eligible])
        return self

    # ------------------------------------------------------------- human decisions
    def _decide(self, role: str, action: str, case_id: str, proposal_id: str, person: str, status: str):
        c = self.recon.case(case_id)
        self._authorize(role, action, case=case_id)
        if role not in HUMAN_ROLES:  # defence in depth: policy already blocks this
            raise PermissionDenied(role, action, "only a person can decide")
        if c.proposal is None or c.proposal.id != proposal_id:
            self.log.record(role, action, "refused", 2, case=case_id, reason="the fix changed since it was shown")
            raise PermissionDenied(role, action, "the fix changed since it was shown; review it again")
        if c.status != "open":
            raise PermissionDenied(role, action, f"case is already {c.status}")
        c.status = status
        self.log.record(role, action, "ok", 2, case=case_id, proposal=proposal_id, person=person)
        return c

    def approve(self, case_id: str, *, role: str, proposal_id: str, person: str):
        return self._decide(role, "approve_fix", case_id, proposal_id, person, "approved")

    def reject(self, case_id: str, *, role: str, proposal_id: str, person: str):
        return self._decide(role, "reject_fix", case_id, proposal_id, person, "rejected")

    def escalate(self, case_id: str, *, role: str, person: str, note: str = ""):
        c = self.recon.case(case_id)
        self._authorize(role, "escalate_fix", case=case_id)
        c.status = "escalated"
        self.log.record(role, "escalate_fix", "ok", 1, case=case_id, person=person, note=note)
        return c

    def undo(self, case_id: str, *, role: str, person: str):
        c = self.recon.case(case_id)
        self._authorize(role, "approve_fix", case=case_id)
        c.status = "open" if c.proposal else "escalated"
        self.log.record(role, "undo_decision", "ok", 2, case=case_id, person=person)
        return c

    def halt(self, role: str, person: str = ""):
        self._authorize(role, "halt")
        self.policy.halted = True
        self.log.record(role, "halt", "ok", 0, person=person, effect="agent actions blocked for this session")

    # ------------------------------------------------------------- outputs
    def export_journal(self, role: str = "reviewer") -> str:
        self._authorize(role, "export_journal")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "case", "reference", "account", "debit", "credit", "memo"])
        n = 0
        for c in self.recon.cases:
            if c.status != "approved" or not c.proposal:
                continue
            for line in c.proposal.lines:
                w.writerow([self.recon.statement.date, c.id, c.ref, line.account,
                            f"{line.debit / 100:.2f}" if line.debit else "",
                            f"{line.credit / 100:.2f}" if line.credit else "", c.proposal.summary])
                n += 1
        self.log.record(role, "export_journal", "ok", 0, lines=n)
        return buf.getvalue()

    def summary(self) -> dict:
        r, fc = self.recon, self.forecast
        return {
            "business": self.profile["business"]["name"],
            "sales_date": r.sales_date, "statement_date": r.statement.date,
            "providers": r.providers, "adapters": self.adapters,
            "orders": r.order_count, "matched": len(r.matched), "cases": len(r.cases),
            "expected": r.expected, "received": r.received, "gap": r.gap,
            "explained": r.explained, "residual": r.residual,
            "bank_balance": fc.bank_balance, "verified_cash": fc.verified_cash,
            "in_transit": fc.in_transit, "refunds_owed": fc.refunds_owed,
            "lowest_cash": fc.lowest.closing, "lowest_week": fc.lowest.label, "shortfall": fc.shortfall,
            "breakdown": [{"case": c.id, "category": c.category, "label": CATEGORY_LABELS[c.category],
                           "impact": c.impact} for c in r.cases],
            "stats": self.stats, "audit_head": self.log.head,
        }

    def case_dict(self, c) -> dict:
        d = {k: v for k, v in asdict(c).items() if k not in ("allowed_numbers",)}
        d["label"] = CATEGORY_LABELS.get(c.category, c.category)
        d["proposal_id"] = c.proposal.id if c.proposal else None
        return d

    def gap_sentence(self) -> str:
        r = self.recon
        return (f"Your books expected {fmt(r.expected)}. The bank received {fmt(r.received)}. "
                f"That leaves {fmt(r.gap)} to explain.")
