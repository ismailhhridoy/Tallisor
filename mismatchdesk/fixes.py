"""Draft correcting journal entries for each case.

Drafts are built by code from case facts, then checked by two invariants before
anyone sees them:
  1. debits equal credits
  2. the net change on the provider clearing account equals the case variance,
     so the fix corrects exactly the difference found, no more, no less.
A draft failing either check is refused (never shown as approvable).
"""
from __future__ import annotations

from .models import Case, JournalLine, Proposal
from .money import fmt


class InvalidProposal(ValueError):
    pass


def clearing_account(provider: str) -> str:
    return f"{provider} clearing" if provider else "Payment clearing"


def build_proposal(case: Case) -> Proposal | None:
    m, clr = case.meta, clearing_account(case.provider)
    L = JournalLine
    lines, tasks, summary = [], [], ""
    v = case.variance

    if case.category == "fee_variance":
        x = m["fee_extra"]
        lines = [L("Payment processing fees", debit=x), L(clr, credit=x)]
        summary = f"Record the extra {fmt(x)} fee as a processing cost."
        if case.confidence != "high":
            tasks = ["Ask the provider to explain or refund the fee that does not match the contract."]
    elif case.category == "refund_unrecorded":
        r = m["refund_amount"]
        lines = [L("Sales refunds", debit=r), L(clr, credit=r)]
        summary = f"Record the {fmt(r)} refund against {case.ref}."
    elif case.category == "chargeback":
        d, f = m["dispute_amount"], m["dispute_fee"]
        lines = [L("Disputed charges receivable", debit=d), L("Dispute fees", debit=f), L(clr, credit=d + f)]
        summary = "Move the sale to disputed charges and record the dispute fee."
        due = m.get("evidence_due") or "the provider's deadline"
        tasks = [f"Send proof of delivery for {case.ref} to {case.provider} before {due}."]
    elif case.category == "fx_difference":
        g, f = m["fx_gross_diff"], m["fx_fee_diff"]
        if g >= 0:
            lines.append(L("Foreign exchange loss", debit=g))
        else:
            lines.append(L("Foreign exchange gain", credit=-g))
        if f >= 0:
            lines.append(L("Payment processing fees", credit=f))
        else:
            lines.append(L("Payment processing fees", debit=-f))
        lines.append(L(clr, credit=-v) if v < 0 else L(clr, debit=v))
        summary = "Record the exchange-rate difference and adjust the fee estimate."
    elif case.category == "duplicate_charge":
        g, f, n = m["dup_gross"], m["dup_fee"], m["dup_net"]
        lines = [L(clr, debit=n), L("Payment processing fees", debit=f), L("Refunds owed to customers", credit=g)]
        summary = f"Record the second charge of {fmt(g)} as money owed back to the customer."
        tasks = [f"Refund the duplicate {fmt(g)} charge to {case.party or 'the customer'} (done by a person in the provider dashboard)."]
    elif case.category == "payment_failed":
        g, f, e = m["gross_books"], m["expected_fee"], m["expected_net"]
        lines = [L("Accounts receivable", debit=g), L("Payment processing fees", credit=f), L(clr, credit=e)]
        summary = "Reopen the invoice: the money was never collected."
        tasks = [f"Ask {case.party or 'the customer'} to pay again and hold the shipment if it has not left."]
    elif case.category == "no_payment_record":
        g, f, e = m["gross_books"], m["expected_fee"], m["expected_net"]
        lines = [L("Accounts receivable", debit=g), L("Payment processing fees", credit=f), L(clr, credit=e)]
        summary = "Treat the sale as unpaid until a payment is found."
        tasks = [f"Confirm with the team how {case.ref} was paid."]
    elif case.category == "unrecorded_sale":
        g, f, n = m["gross"], m["fee"], m["net"]
        lines = [L(clr, debit=n), L("Payment processing fees", debit=f), L("Sales revenue", credit=g)]
        summary = f"Record the missing sale of {fmt(g)}."
        tasks = [f"Create the missing invoice for reference {case.ref}."]
    elif case.category == "provider_fee":
        x = m["fee_total"]
        lines = [L("Payment processing fees", debit=x), L(clr, credit=x)]
        summary = f"Record the {fmt(x)} provider charge as a cost."
    elif case.category == "payout_in_transit":
        summary = f"Nothing to record. Check the bank again on {case.meta.get('arrival_date')}."
        tasks = [f"Confirm the {fmt(case.meta['amount'])} deposit arrives on {case.meta.get('arrival_date')}."]
        return Proposal(case.id, [], tasks, summary, clr)
    elif case.category == "payout_overdue":
        summary = "Nothing to record yet. The payout is late."
        tasks = [f"Contact {case.provider} about payout {case.ref} ({fmt(case.meta['amount'])})."]
        return Proposal(case.id, [], tasks, summary, clr)
    else:
        return None

    proposal = Proposal(case.id, lines, tasks, summary, clr)
    validate(proposal, case)
    return proposal


def validate(p: Proposal, case: Case) -> None:
    if not p.lines:
        return
    for line in p.lines:
        if line.debit < 0 or line.credit < 0 or (line.debit and line.credit):
            raise InvalidProposal(f"{case.id}: malformed line on {line.account}")
    if not p.balanced:
        raise InvalidProposal(f"{case.id}: debits {p.total_debit} != credits {p.total_credit}")
    clearing_net = sum(l.debit - l.credit for l in p.lines if l.account == p.clearing_account)
    if clearing_net != case.variance:
        raise InvalidProposal(
            f"{case.id}: fix changes clearing by {clearing_net}, but the difference found is {case.variance}")
