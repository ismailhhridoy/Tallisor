"""Conversational copilot: understand the owner's intent, plan steps, run them, answer.

Plan-Then-Execute (Beurer-Kellner et al. 2025): the plan is fixed from the
owner's message alone, before any tool output or file text is read, so content
inside uploaded files cannot change which steps run. Each step is a named tool
with a permission tier; the planner can only choose from this list, and T3
requests are refused with an explanation.

Intent understanding uses deterministic rules by default. If a model is
configured it may classify the message, but only into the fixed intent list;
anything else is ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .financing import DISCLAIMER, readiness_pack
from .llm import LLM, NullLLM
from .money import fmt
from .reconcile import CATEGORY_LABELS
from .security.policy import PermissionDenied

INTENTS = ["move_money", "submit_application", "approve", "cash", "financing", "gap",
           "case", "alerts", "pack", "export", "help"]

PLANS = {
    "gap": ["read_cases"],
    "case": ["read_cases"],
    "cash": ["forecast", "alerts", "match_financing"],
    "financing": ["forecast", "match_financing"],
    "alerts": ["alerts"],
    "pack": ["forecast", "match_financing", "prepare_financing_pack"],
    "export": ["export_journal"],
    "help": [],
    "approve": ["approve_fix"],
    "move_money": ["move_money"],
    "submit_application": ["submit_financing_application"],
}

_RULES = [
    ("move_money", r"\b(transfer|wire|send (money|funds|\$)|pay (the |my )?(supplier|vendor|staff|salar)|issue (a )?refund|refund (the|this|that) customer|move (money|funds))\b"),
    ("submit_application", r"\b(apply|submit|send).{0,30}\b(loan|application|lender|financing)\b"),
    ("approve", r"\b(approve|post|book|confirm|accept)\b.{0,30}\b(fix|fixes|entr|all|journal)"),
    ("pack", r"\b(readiness|pack|summary for (a |the )?(lender|bank))\b"),
    ("export", r"\b(export|download)\b"),
    ("case", r"\b(NW-\d+|C\d{2}|payout [a-z0-9_]+)\b"),
    ("financing", r"\b(financ|loan|borrow|credit line|lend|advance)"),
    ("cash", r"\b(payroll|afford|cash|runway|forecast|run out|salar)"),
    ("alerts", r"\b(alert|warning|risk|deadline|urgent|attention)"),
    ("gap", r"\b(short|missing|gap|difference|why|where|reconcil|bank|mismatch)"),
]


@dataclass
class Reply:
    intent: str
    plan: list
    answer: str
    refused: bool = False
    steps: list = field(default_factory=list)


def classify(message: str, llm: LLM | None = None) -> str:
    text = message.lower()
    for intent, pattern in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return intent
    llm = llm or NullLLM()
    if not isinstance(llm, NullLLM):
        reply = llm.complete_json(
            "Classify the business owner's message into exactly one intent from this list: "
            + ", ".join(INTENTS) + ". Reply with JSON {\"intent\": \"...\"} only.", message)
        intent = (reply or {}).get("intent") if isinstance(reply, dict) else None
        if intent in INTENTS:
            return intent
    return "help"


class Copilot:
    def __init__(self, session, llm: LLM | None = None):
        self.s = session
        self.llm = llm or session.llm

    def ask(self, message: str) -> Reply:
        intent = classify(message, self.llm)
        plan = PLANS[intent]
        self.s.log.record("agent", "plan", "ok", None, intent=intent, steps=plan, message=message[:200])
        try:
            for step in plan:
                self.s._authorize("agent", step, via="chat")
        except PermissionDenied as exc:
            return Reply(intent, plan, self._refusal(intent, exc), refused=True)
        answer = getattr(self, f"_answer_{intent}")(message)
        if intent == "pack":
            self.s.log.record("agent", "prepare_financing_pack", "ok", 1, status="draft for the owner")
        return Reply(intent, plan, answer, steps=plan)

    # -------------------------------------------------------------- refusals
    def _refusal(self, intent: str, exc: PermissionDenied) -> str:
        if intent == "move_money":
            return ("I can't move money, pay anyone or issue refunds. Mismatch Desk has no access to do that, by design. "
                    "I can prepare the task and the amount so a person can do it in the provider or bank dashboard.")
        if intent == "submit_application":
            return ("I can't apply for financing or send your data to a lender. I can prepare a readiness summary "
                    "that you review and choose whether to share. Ask for \"a readiness pack\".")
        if intent == "approve":
            return ("I can't approve fixes, including my own. A person approves each one on the Review screen, "
                    "after seeing the exact entry.")
        return f"I can't do that: {exc.reason}."

    # -------------------------------------------------------------- answers
    def _answer_help(self, _):
        return ("Ask me things like: *Why is my bank short?*, *Can I make payroll?*, *What financing fits?*, "
                "*What needs my attention?*, *Explain NW-48242*, or *Prepare a readiness pack*.")

    def _answer_gap(self, _):
        r = self.s.recon
        lines = [self.s.gap_sentence(), ""]
        for c in sorted(r.cases, key=lambda c: -abs(c.impact)):
            if c.impact:
                lines.append(f"- **{CATEGORY_LABELS[c.category]}** ({c.ref}): {fmt(-c.impact, signed=True)}")
        lines.append("")
        lines.append(f"All {fmt(r.explained)} is accounted for." if r.residual == 0
                     else f"{fmt(r.residual)} is still unexplained and needs a person.")
        return "\n".join(lines)

    def _answer_case(self, message):
        m = re.search(r"(NW-\d+|C\d{2}|payout [a-z0-9_]+)", message, re.IGNORECASE)
        key = m.group(1) if m else ""
        key = key.split(" ", 1)[1] if key.lower().startswith("payout ") else key.upper()
        try:
            c = self.s.recon.case(key)
        except KeyError:
            return f"I couldn't find {key} among today's differences."
        lines = [f"**{CATEGORY_LABELS[c.category]}** on {c.ref} ({fmt(c.variance, signed=True)})", "", c.explanation, ""]
        lines += [f"- {x}" for x in c.checks]
        if c.proposal:
            lines += ["", f"Proposed fix: {c.proposal.summary} Status: {c.status}."]
        return "\n".join(lines)

    def _answer_cash(self, _):
        fc, s = self.s.forecast, self.s
        lines = [f"Your bank shows {fmt(fc.bank_balance)}, but your verified cash is **{fmt(fc.verified_cash)}** "
                 f"(plus {fmt(fc.in_transit)} in transit, minus {fmt(fc.refunds_owed)} you owe in refunds).", ""]
        if fc.breach_weeks:
            lines.append(f"Cash drops to **{fmt(fc.lowest.closing)}** in the week of {fc.lowest.label}, "
                         f"{fmt(fc.shortfall)} below your {fmt(fc.floor)} safety floor.")
        else:
            lines.append(f"Cash stays above your {fmt(fc.floor)} floor for the next {len(fc.weeks)} weeks.")
        crit = [a for a in s.alerts if a["severity"] == "critical"]
        for a in crit:
            lines.append(f"- {a['title']}")
        top = next((m for m in s.matches if m.eligible), None)
        if fc.shortfall and top:
            lines += ["", f"Best-fit option: **{top.product['name']}**, about {fmt(top.amount)}. Ask *what financing fits?* for details."]
        return "\n".join(lines)

    def _answer_financing(self, _):
        m, s = self.s.metrics, self.s
        lines = [f"Based on verified records: about {fmt(m.monthly_revenue)} a month in revenue, "
                 f"{fmt(m.eligible_receivables)} in open invoices, and a forecast need of {fmt(m.need)}.", ""]
        for x in s.matches:
            if x.eligible:
                lines.append(f"- **{x.product['name']}** (fit {x.score}/100): " + "; ".join(x.reasons) + ".")
        for x in s.matches:
            if not x.eligible:
                lines.append(f"- ~~{x.product['name']}~~: not a fit, " + "; ".join(x.why_not) + ".")
        lines += ["", DISCLAIMER]
        return "\n".join(lines)

    def _answer_alerts(self, _):
        icon = {"critical": "Critical", "warning": "Warning", "info": "Info"}
        return "\n".join(f"- **{icon[a['severity']]}:** {a['title']}. {a['detail']}" for a in self.s.alerts) or "Nothing needs attention."

    def _answer_pack(self, _):
        s = self.s
        top = next((m for m in s.matches if m.eligible), None)
        return readiness_pack(s.forecast, s.metrics, top, s.profile["business"]["name"])

    def _answer_export(self, _):
        approved = sum(1 for c in self.s.recon.cases if c.status == "approved")
        return f"{approved} approved fix(es) are ready. Download the journal file from the Review screen."

    def _answer_approve(self, _):  # unreachable: approval is refused for the agent
        return ""

    _answer_move_money = _answer_submit_application = _answer_approve
