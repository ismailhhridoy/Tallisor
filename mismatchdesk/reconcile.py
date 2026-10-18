"""Deterministic three-way matching: books vs payment providers vs bank.

Everything here is plain code. No language model is involved in matching,
categorising or arithmetic, so results are reproducible and auditable.

Identity the engine maintains for every run:
    expected deposits - bank received = sum(case.impact)   (residual must be 0)
If it does not hold, a 'residual' case is created so nothing is silently lost.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .models import BankTxn, BookEntry, Case, Evidence, PspTxn, Statement
from .money import fee_cents, fmt

CATEGORY_LABELS = {
    "fee_variance": "Fee higher than contracted",
    "refund_unrecorded": "Refund missing from the books",
    "chargeback": "Disputed payment (chargeback)",
    "fx_difference": "Currency conversion difference",
    "duplicate_charge": "Customer charged twice",
    "payment_failed": "Payment never collected",
    "no_payment_record": "Sale with no payment record",
    "unrecorded_sale": "Payment with no sale in the books",
    "payout_in_transit": "Payout still on its way",
    "payout_overdue": "Payout overdue",
    "unidentified_deposit": "Deposit not linked to a payout",
    "unexplained": "Difference not yet explained",
    "residual": "Unexplained remainder",
}

TONE = {  # for display: money short, extra money, timing only
    "fee_variance": "short", "refund_unrecorded": "short", "chargeback": "short",
    "fx_difference": "short", "payment_failed": "short", "no_payment_record": "short",
    "duplicate_charge": "over", "unrecorded_sale": "over", "unidentified_deposit": "over",
    "payout_in_transit": "timing", "payout_overdue": "short", "unexplained": "short", "residual": "short",
}


@dataclass
class Payout:
    provider: str
    payout_id: str
    amount: int
    arrival_date: str
    txn_count: int
    status: str = "unmatched"
    bank_txn: str = ""


@dataclass
class ReconResult:
    sales_date: str
    statement: Statement
    expected: int
    received: int
    gap: int
    cases: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    payouts: list = field(default_factory=list)
    providers: list = field(default_factory=list)
    order_count: int = 0

    @property
    def explained(self) -> int:
        return sum(c.impact for c in self.cases if c.category != "residual")

    @property
    def residual(self) -> int:
        return self.gap - self.explained

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.id == case_id or c.ref == case_id:
                return c
        raise KeyError(case_id)


def _schedule(profile: dict, provider: str, card_country: str = "") -> tuple[int, int, str]:
    sched = profile["fee_schedules"].get(provider)
    if not sched:
        return 0, 0, "no contract on file"
    home = sched.get("home_country")
    if card_country and home and card_country != home and "international" in sched:
        s = sched["international"]
        return s["bp"], s["fixed"], "international"
    s = sched["domestic"]
    return s["bp"], s["fixed"], "domestic"


def contracted_fee(profile: dict, provider: str, amount: int) -> int:
    bp, fixed, _ = _schedule(profile, provider)
    return fee_cents(amount, bp, fixed)


def _rate_text(bp: int, fixed: int) -> str:
    pct = f"{bp / 100:.2f}".rstrip("0").rstrip(".")
    return f"{pct}% + {fmt(fixed)}"


def reconcile(books: list[BookEntry], psp: list[PspTxn], bank: list[BankTxn],
              statement: Statement, profile: dict) -> ReconResult:
    by_ref: dict[str, list[PspTxn]] = defaultdict(list)
    payout_rows: list[PspTxn] = []
    for t in psp:
        (payout_rows if t.kind == "payout" else by_ref[t.ref]).append(t)

    cases: list[Case] = []
    matched = []
    expected_total = 0
    seen_refs = set()
    n = 0

    def new_id() -> str:
        nonlocal n
        n += 1
        return f"C{n:02d}"

    # ---- 1. every sale in the books -------------------------------------------------
    for b in books:
        if b.status != "paid":
            continue
        seen_refs.add(b.ref)
        exp_fee = contracted_fee(profile, b.channel, b.amount_usd)
        expected_net = b.amount_usd - exp_fee
        expected_total += expected_net
        txns = by_ref.get(b.ref, [])
        actual_net = sum(t.net for t in txns)
        variance = actual_net - expected_net
        if variance == 0 and txns:
            charge = next((t for t in txns if t.kind == "charge"), txns[0])
            matched.append({"ref": b.ref, "customer": b.customer, "provider": b.channel,
                            "gross": b.amount_usd, "fee": exp_fee, "net": expected_net,
                            "payout": charge.payout_id})
            continue
        cases.append(_order_case(new_id(), b, txns, exp_fee, expected_net, actual_net, variance, profile))

    # ---- 2. provider activity with no sale in the books ----------------------------
    for ref, txns in by_ref.items():
        if ref in seen_refs:
            continue
        net = sum(t.net for t in txns)
        if net == 0 and all(t.kind == "failed" for t in txns):
            continue
        cases.append(_unbooked_case(new_id(), ref, txns, net))

    # ---- 3. payouts against the bank -----------------------------------------------
    payouts = []
    for row in payout_rows:
        count = sum(1 for t in psp if t.payout_id == row.payout_id and t.kind != "payout")
        payouts.append(Payout(row.provider, row.payout_id, -row.net, row.arrival_date, count))
    credits = [t for t in bank if t.amount > 0]
    used = set()
    for p in payouts:
        hit = next((t for t in credits if t.txn_id not in used and
                    p.payout_id and p.payout_id.lower() in f"{t.reference} {t.remittance_info}".lower()), None)
        if hit is None:
            hit = next((t for t in credits if t.txn_id not in used and t.amount == p.amount), None)
        if hit is not None and hit.amount == p.amount:
            p.status, p.bank_txn = "received", hit.txn_id
            used.add(hit.txn_id)
        elif hit is not None:
            p.status, p.bank_txn = "amount_mismatch", hit.txn_id
            used.add(hit.txn_id)
    received = sum(t.amount for t in credits if t.txn_id in used)

    for p in payouts:
        if p.status == "received":
            continue
        if p.status == "amount_mismatch":
            bank_amt = next(t.amount for t in credits if t.txn_id == p.bank_txn)
            cases.append(_payout_case(new_id(), p, statement, short_by=p.amount - bank_amt))
            continue
        cases.append(_payout_case(new_id(), p, statement))

    for t in credits:
        if t.txn_id in used:
            continue
        cases.append(_deposit_case(new_id(), t))

    gap = expected_total - received
    result = ReconResult(
        sales_date=profile.get("sales_date", ""), statement=statement,
        expected=expected_total, received=received, gap=gap, cases=cases, matched=matched,
        payouts=payouts, providers=sorted({t.provider for t in psp}),
        order_count=len([b for b in books if b.status == "paid"]),
    )
    if result.residual != 0:
        cases.append(Case(
            id=new_id(), kind="residual", ref="(none)", party="", provider="", category="residual",
            variance=-result.residual, impact=result.residual, confidence="low",
            checks=["The causes found do not add up to the full gap. A person must investigate."],
            allowed_numbers={abs(result.residual)},
        ))
    for c in result.cases:
        c.allowed_numbers |= {abs(c.variance), abs(c.impact)}
        c.allowed_numbers |= {abs(e.amount) for e in c.evidence if e.amount is not None}
    return result


# ------------------------------------------------------------------ case builders

def _order_case(cid, b: BookEntry, txns, exp_fee, expected_net, actual_net, variance, profile) -> Case:
    charges = [t for t in txns if t.kind == "charge"]
    refunds = [t for t in txns if t.kind == "refund"]
    disputes = [t for t in txns if t.kind == "dispute"]
    dispute_fees = [t for t in txns if t.kind == "dispute_fee"]
    failed = [t for t in txns if t.kind == "failed"]

    ev = [Evidence("Books", b.ref, f"Sale recorded as paid; expected after the contracted fee of {fmt(exp_fee)}", expected_net)]
    for t in txns:
        note = {
            "charge": f"Charge succeeded{', card country ' + t.card_country if t.card_country else ''}; fee {fmt(t.fee)}",
            "refund": "Refund issued",
            "dispute": "Dispute opened by the cardholder's bank",
            "dispute_fee": "Dispute fee",
            "failed": "Payment attempt failed",
        }.get(t.kind, t.kind)
        ev.append(Evidence(t.provider, t.txn_id, note, t.net))
    ev.append(Evidence("Difference", "", "Actual minus expected", variance))

    meta = {"gross_books": b.amount_usd, "expected_fee": exp_fee, "expected_net": expected_net, "actual_net": actual_net}
    checks, secondary, conf = [], [], "high"
    allowed = {b.amount_usd, exp_fee, expected_net, actual_net}
    for t in txns:
        allowed |= {abs(t.gross), t.fee, abs(t.net)}

    if not txns:
        category = "no_payment_record"
        checks.append(f"No transaction for {b.ref} exists in any provider report for this period.")
        conf = "medium"
    elif failed and not charges:
        category = "payment_failed"
        checks.append(f"{b.channel} has {len(failed)} attempt(s) for {b.ref}, none successful.")
        checks.append("The books marked the sale paid before the payment result came back.")
        conf = "medium"
    elif len(charges) > 1:
        category = "duplicate_charge"
        extra = charges[1:]
        meta.update(dup_gross=sum(t.gross for t in extra), dup_fee=sum(t.fee for t in extra), dup_net=sum(t.net for t in extra))
        checks.append(f"{len(charges)} successful charges carry the same order reference {b.ref}.")
        checks.append(f"Charges are {fmt(charges[0].gross)} each; the books record one sale of {fmt(b.amount_usd)}.")
        allowed |= {meta["dup_gross"], meta["dup_fee"], meta["dup_net"]}
    elif disputes:
        category = "chargeback"
        d_amt = -sum(t.net for t in disputes)
        d_fee = -sum(t.net for t in dispute_fees)
        due = next((t.evidence_due for t in disputes if t.evidence_due), "")
        meta.update(dispute_amount=d_amt, dispute_fee=d_fee, evidence_due=due)
        checks.append(f"The dispute references the original charge for {b.ref}.")
        if due:
            checks.append(f"Deadline to send evidence to {b.channel}: {due}.")
        allowed |= {d_amt, d_fee, d_amt + d_fee}
    elif refunds:
        category = "refund_unrecorded"
        r_amt = -sum(t.net for t in refunds)
        meta.update(refund_amount=r_amt)
        checks.append(f"The refund is linked to the charge for {b.ref}.")
        checks.append("No refund entry exists in the books for this sale.")
        allowed |= {r_amt}
    elif b.currency != "USD" and charges and charges[0].exchange_rate and b.fx_rate and \
            Decimal(charges[0].exchange_rate) != Decimal(b.fx_rate):
        category = "fx_difference"
        ch = charges[0]
        gross_diff = b.amount_usd - ch.gross
        fee_diff = exp_fee - ch.fee
        meta.update(fx_gross_diff=gross_diff, fx_fee_diff=fee_diff)
        cur = b.currency
        checks.append(f"The order amount is {fmt(b.amount, '')} {cur} in both records.")
        checks.append(f"The books used a rate of {b.fx_rate}; {b.channel} converted at {ch.exchange_rate}.")
        checks.append(f"The fee differs by {fmt(abs(fee_diff))} because it is charged on the converted amount.")
        allowed |= {b.amount, abs(gross_diff), abs(fee_diff), ch.gross}
    elif charges and sum(t.fee for t in charges) > exp_fee:
        category = "fee_variance"
        ch = charges[0]
        bp, fixed, kind = _schedule(profile, b.channel, ch.card_country)
        sched_fee = fee_cents(ch.gross, bp, fixed)
        extra = ch.fee - exp_fee
        meta.update(fee_extra=extra, fee_kind=kind)
        dbp, dfixed, _ = _schedule(profile, b.channel)
        if ch.fee == sched_fee and kind == "international":
            checks.append(f"Card country {ch.card_country} triggers the contracted international rate of {_rate_text(bp, fixed)}.")
            checks.append(f"The books assumed the domestic rate of {_rate_text(dbp, dfixed)}.")
        else:
            checks.append(f"The fee {fmt(ch.fee)} does not match any rate in the contract. Possible overcharge.")
            conf = "medium"
        allowed |= {extra, sched_fee}
    else:
        category = "unexplained"
        conf = "low"
        checks.append("The records differ but no known cause matches. A person must investigate.")

    if refunds and category not in ("refund_unrecorded",):
        secondary.append("refund_unrecorded")

    case = Case(id=cid, kind="order", ref=b.ref, party=b.customer, provider=b.channel,
                category=category, variance=variance, impact=-variance, evidence=ev,
                checks=checks, secondary=secondary, confidence=conf, meta=meta)
    case.allowed_numbers = allowed
    _screen(case, txns)
    return case


def _unbooked_case(cid, ref, txns, net) -> Case:
    charge = next((t for t in txns if t.kind == "charge"), txns[0])
    ev = [Evidence(t.provider, t.txn_id, f"{t.kind.title()} for reference {ref or '(blank)'}", t.net) for t in txns]
    ev.append(Evidence("Books", ref, "No sale with this reference", 0))
    case = Case(
        id=cid, kind="unbooked", ref=ref or charge.txn_id, party="", provider=charge.provider,
        category="unrecorded_sale" if net > 0 else "unexplained", variance=net, impact=-net, evidence=ev,
        checks=[f"{charge.provider} received a payment for reference {ref}.",
                "No sale with this reference exists in the books, so revenue is understated."],
        confidence="medium",
        meta={"gross": sum(t.gross for t in txns), "fee": sum(t.fee for t in txns), "net": net},
    )
    case.allowed_numbers = {abs(t.gross) for t in txns} | {t.fee for t in txns} | {abs(net)}
    _screen(case, txns)
    return case


def _payout_case(cid, p: Payout, statement: Statement, short_by: int = 0) -> Case:
    if short_by:
        return Case(
            id=cid, kind="payout", ref=p.payout_id, party=f"{p.provider} to bank", provider=p.provider,
            category="unexplained", variance=-short_by, impact=short_by, confidence="low",
            evidence=[Evidence(p.provider, p.payout_id, "Payout sent", p.amount),
                      Evidence("Bank", p.bank_txn, "Deposit received", p.amount - short_by)],
            checks=["The deposit is smaller than the payout. Ask the bank and provider about deductions."],
            allowed_numbers={p.amount, short_by, p.amount - short_by},
        )
    in_transit = bool(p.arrival_date) and p.arrival_date > (statement.date or "")
    category = "payout_in_transit" if in_transit else "payout_overdue"
    checks = [f"Payout {p.payout_id} covers {p.txn_count} transactions, each matched individually."]
    if in_transit:
        checks.append(f"{p.provider} states an arrival date of {p.arrival_date}, after the statement date {statement.date}.")
    else:
        checks.append(f"The stated arrival date {p.arrival_date or '(none)'} has passed with no matching deposit.")
    return Case(
        id=cid, kind="payout", ref=p.payout_id, party=f"{p.provider} to bank", provider=p.provider,
        category=category, variance=-p.amount, impact=p.amount,
        confidence="high" if in_transit else "medium",
        evidence=[Evidence(p.provider, p.payout_id, f"Payout sent, arrival {p.arrival_date or 'not stated'}", p.amount),
                  Evidence("Bank", "", "No matching deposit on this statement", 0)],
        checks=checks, meta={"arrival_date": p.arrival_date, "amount": p.amount},
        allowed_numbers={p.amount},
    )


def _deposit_case(cid, t: BankTxn) -> Case:
    case = Case(
        id=cid, kind="deposit", ref=t.txn_id, party="Bank", provider="Bank",
        category="unidentified_deposit", variance=t.amount, impact=0, confidence="low",
        evidence=[Evidence("Bank", t.txn_id, "Credit not linked to any provider payout", t.amount)],
        checks=["This money is not part of today's sales. It is listed so it is not mistaken for a payout."],
        allowed_numbers={t.amount},
    )
    _screen(case, [], extra_text=t.remittance_info)
    return case


def _screen(case: Case, txns, extra_text: str = "") -> None:
    from .security.guard import screen_injection
    texts = [t.memo for t in txns if t.memo] + ([extra_text] if extra_text else [])
    case.meta["memos"] = texts
    hits = sorted({h for text in texts for h in screen_injection(text)})
    if hits:
        case.flags.append({"type": "injection_suspected", "matches": hits})
