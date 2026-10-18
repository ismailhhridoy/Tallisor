"""Verified cash position, a 13-week forecast and cash-flow alerts.

The forecast starts from the *verified* position (bank balance plus payouts
confirmed in transit, minus refunds the business already owes), not the raw bank
balance. That is the point of reconciling first.

Method: direct cash forecast. Weekly sales receipts = average of the last 8
weeks of settled sales. Receivables are collected on their due date. Recurring
and one-off payments come from the business profile. Deliberately simple and
explainable; every number traces to an input file.
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .money import fmt, to_cents
from .reconcile import ReconResult


def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


@dataclass
class Week:
    start: date
    opening: int
    sales_in: int
    receivables_in: int
    other_in: int
    outflows: int
    closing: int
    items: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.start.strftime("%d %b")


@dataclass
class Forecast:
    bank_balance: int
    in_transit: int
    refunds_owed: int
    verified_cash: int
    weekly_sales: int
    floor: int
    weeks: list
    history: list
    receivables: list

    @property
    def lowest(self) -> Week:
        return min(self.weeks, key=lambda w: w.closing)

    @property
    def shortfall(self) -> int:
        return max(0, self.floor - self.lowest.closing)

    @property
    def breach_weeks(self) -> list:
        return [w for w in self.weeks if w.closing < self.floor]


def load_history(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append({"week_start": r["week_start"], "card_net": to_cents(r["card_net"]),
                         "wallet_net": to_cents(r["wallet_net"]), "charges": int(r["charges"]),
                         "disputes": int(r["disputes"])})
    return rows


def load_receivables(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append({"invoice": r["invoice"], "customer": r["customer"], "amount": to_cents(r["amount"]),
                         "issued": r["issued"], "due": r["due"], "status": r["status"]})
    return rows


def _events(profile: dict, start: date, end: date) -> list[tuple[date, str, int]]:
    out = []
    for item in profile["cash"]["payments"]:
        amt = item["amount"]
        if "once" in item:
            d = _d(item["once"])
            if start <= d < end:
                out.append((d, item["name"], amt))
        elif "every_days" in item:
            d = _d(item["first"])
            while d < end:
                if d >= start:
                    out.append((d, item["name"], amt))
                d += timedelta(days=item["every_days"])
        elif "monthly_day" in item:
            y, m = start.year, start.month
            while True:
                d = date(y, m, item["monthly_day"])
                if d >= end:
                    break
                if d >= start:
                    out.append((d, item["name"], amt))
                m = 1 if m == 12 else m + 1
                y = y + 1 if m == 1 else y
    return sorted(out)


def build_forecast(recon: ReconResult, profile: dict, history: list[dict], receivables: list[dict]) -> Forecast:
    cash = profile["cash"]
    start = _d(cash["forecast_start"])
    weeks_n = cash.get("weeks", 13)
    end = start + timedelta(days=7 * weeks_n)

    in_transit = sum(c.meta["amount"] for c in recon.cases if c.category == "payout_in_transit")
    refunds_owed = sum(c.meta.get("dup_gross", 0) for c in recon.cases if c.category == "duplicate_charge")
    bank = recon.statement.closing_balance
    verified = bank + in_transit - refunds_owed

    recent = history[-8:]
    weekly_sales = int(statistics.mean(h["card_net"] + h["wallet_net"] for h in recent)) if recent else 0

    events = _events(profile, start, end)
    weeks, balance = [], verified
    for i in range(weeks_n):
        ws, we = start + timedelta(days=7 * i), start + timedelta(days=7 * (i + 1))
        # Open invoices are collected on their due date. Invoices already overdue
        # are NOT counted (conservative); they are raised as alerts instead.
        rec_in = sum(r["amount"] for r in receivables if r["status"] == "open" and ws <= _d(r["due"]) < we)
        out_items = [(d, n, a) for d, n, a in events if ws <= d < we]
        outflows = sum(a for _, _, a in out_items)
        opening = balance
        balance = opening + weekly_sales + rec_in - outflows
        weeks.append(Week(ws, opening, weekly_sales, rec_in, 0, outflows, balance,
                          items=[{"date": d.isoformat(), "name": n, "amount": a} for d, n, a in out_items]))
    return Forecast(bank, in_transit, refunds_owed, verified, weekly_sales, cash["floor"], weeks, history, receivables)


def alerts(recon: ReconResult, fc: Forecast, profile: dict) -> list[dict]:
    out = []
    as_of = _d(recon.statement.date) if recon.statement.date else _d(profile["cash"]["forecast_start"])

    breaches = fc.breach_weeks
    if breaches:
        first = breaches[0]
        out.append({"severity": "critical", "type": "cash_floor",
                    "title": f"Cash falls below your {fmt(fc.floor)} safety floor in the week of {first.label}",
                    "detail": f"Lowest point {fmt(fc.lowest.closing)} in the week of {fc.lowest.label}, "
                              f"{fmt(fc.shortfall)} below the floor. "
                              f"{len(breaches)} week(s) are below the floor."})
    for w in fc.weeks:
        for item in w.items:
            if item["name"].lower().startswith("payroll") and w.closing < 0:
                out.append({"severity": "critical", "type": "payroll",
                            "title": f"Payroll of {fmt(item['amount'])} on {item['date']} is not covered",
                            "detail": f"Projected balance after that week: {fmt(w.closing)}."})
    for c in recon.cases:
        if c.category == "payout_in_transit":
            out.append({"severity": "info", "type": "payout",
                        "title": f"{fmt(c.meta['amount'])} payout from {c.provider} due {c.meta['arrival_date']}",
                        "detail": "Counted in your verified cash. You will be alerted if it is late."})
        if c.category == "payout_overdue":
            out.append({"severity": "warning", "type": "payout",
                        "title": f"Payout {c.ref} from {c.provider} is overdue",
                        "detail": f"{fmt(c.meta['amount'])} expected by {c.meta.get('arrival_date') or 'an unstated date'}."})
        if c.category == "chargeback" and c.meta.get("evidence_due"):
            days = (_d(c.meta["evidence_due"]) - as_of).days
            out.append({"severity": "warning" if days > 5 else "critical", "type": "dispute",
                        "title": f"Dispute on {c.ref}: send evidence within {days} days",
                        "detail": f"{fmt(c.meta['dispute_amount'])} is at stake. Deadline {c.meta['evidence_due']}."})
        if c.category in ("payment_failed", "no_payment_record"):
            out.append({"severity": "warning", "type": "unpaid",
                        "title": f"{c.ref} from {c.party} was never paid",
                        "detail": f"{fmt(c.meta['gross_books'])} to collect. Hold the goods if they have not shipped."})
        if c.category == "duplicate_charge":
            out.append({"severity": "warning", "type": "refund_owed",
                        "title": f"Refund {fmt(c.meta['dup_gross'])} owed to {c.party}",
                        "detail": "A double charge left you holding the customer's money."})
        if c.category == "fee_variance" and c.confidence != "high":
            out.append({"severity": "warning", "type": "fee",
                        "title": f"Possible overcharge by {c.provider} on {c.ref}",
                        "detail": f"{fmt(c.meta['fee_extra'])} above the contract."})
    for r in fc.receivables:
        if r["status"] == "open" and _d(r["due"]) < as_of:
            out.append({"severity": "warning", "type": "receivable",
                        "title": f"Invoice {r['invoice']} from {r['customer']} is overdue",
                        "detail": f"{fmt(r['amount'])} was due {r['due']}."})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["severity"]])
