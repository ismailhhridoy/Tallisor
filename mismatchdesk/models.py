"""Normalised records. Every source format is converted into these shapes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class BookEntry:
    """A sale as the business recorded it in its own books."""
    ref: str
    date: str
    customer: str
    channel: str            # payment provider the customer paid through
    currency: str
    amount: int             # in order currency, minor units
    amount_usd: int         # in reporting currency, minor units
    fx_rate: Optional[str]  # rate the books used, if foreign currency
    status: str             # paid / unpaid


@dataclass
class PspTxn:
    """One line from a payment provider's report."""
    provider: str
    txn_id: str
    ts: str
    kind: str               # charge | refund | dispute | dispute_fee | failed | payout | adjustment
    ref: str                # merchant order reference
    gross: int
    fee: int                # always positive = cost to the merchant
    net: int
    currency: str = "USD"
    exchange_rate: Optional[str] = None
    card_country: str = ""
    payout_id: str = ""
    arrival_date: str = ""
    memo: str = ""          # free text from the provider: UNTRUSTED
    evidence_due: str = ""
    original_currency: str = ""
    original_amount: int = 0


@dataclass
class BankTxn:
    txn_id: str
    date: str
    amount: int             # positive = money in
    reference: str
    remittance_info: str    # free text from the bank: UNTRUSTED


@dataclass
class Statement:
    account: str
    date: str
    opening_balance: int
    closing_balance: int
    currency: str = "USD"


@dataclass
class Evidence:
    source: str
    ref: str
    note: str
    amount: Optional[int] = None


@dataclass
class JournalLine:
    account: str
    debit: int = 0
    credit: int = 0


@dataclass
class Proposal:
    case_id: str
    lines: list
    tasks: list
    summary: str
    clearing_account: str = ""

    @property
    def total_debit(self) -> int:
        return sum(line.debit for line in self.lines)

    @property
    def total_credit(self) -> int:
        return sum(line.credit for line in self.lines)

    @property
    def balanced(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def id(self) -> str:
        """Content hash. The same source rows and lines always give the same id,
        so retries are idempotent and approvals are bound to exact content."""
        payload = json.dumps(
            {"case": self.case_id, "lines": [asdict(l) for l in self.lines], "tasks": self.tasks},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Case:
    id: str
    kind: str                     # order | payout | unbooked | deposit | residual
    ref: str
    party: str
    provider: str
    category: str
    variance: int                 # actual minus expected, as seen in the provider data
    impact: int                   # contribution to (expected deposits - bank received)
    evidence: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    secondary: list = field(default_factory=list)
    confidence: str = "high"
    explanation: str = ""
    explanation_source: str = "template"
    proposal: Optional[Proposal] = None
    status: str = "open"          # open | approved | escalated | rejected
    flags: list = field(default_factory=list)
    allowed_numbers: set = field(default_factory=set)
    meta: dict = field(default_factory=dict)
