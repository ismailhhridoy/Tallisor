"""Financing-option matching from verified cash flow.

Informational only. Products are generic facility types with illustrative
terms, not offers from real lenders. The engine:
  1. computes the business's metrics from verified data (no self-reported figures),
  2. checks each facility's eligibility rules in code,
  3. sizes what each facility could provide against the forecast need,
  4. scores fit (covers the need, matches how long the gap lasts, cost, speed),
  5. explains every recommendation and every exclusion ("why not").
No application is submitted and no data is shared with any lender (tier T3).
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .cashflow import Forecast
from .money import fmt

DISCLAIMER = ("Illustrative facility types for planning, not offers or advice. Mismatch Desk does not "
              "submit applications or share your data with lenders.")


@dataclass
class Metrics:
    months_trading: int
    monthly_revenue: int
    card_monthly: int
    revenue_volatility: float
    chargeback_rate: float
    eligible_receivables: int
    need: int
    need_weeks: int
    weeks_until_need: int
    receivables_after_gap: int = 0


@dataclass
class Match:
    product: dict
    eligible: bool
    amount: int
    score: int
    reasons: list = field(default_factory=list)
    why_not: list = field(default_factory=list)


def load_catalogue(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text())["products"]


def metrics(fc: Forecast, profile: dict) -> Metrics:
    weeks = fc.history[-12:]
    weekly = [h["card_net"] + h["wallet_net"] for h in weeks] or [0]
    mean = statistics.mean(weekly)
    cv = statistics.pstdev(weekly) / mean if mean else 1.0
    charges = sum(h["charges"] for h in weeks) or 1
    disputes = sum(h["disputes"] for h in weeks)
    start = fc.weeks[0].start.isoformat() if fc.weeks else ""
    eligible_rec = sum(r["amount"] for r in fc.receivables if r["status"] == "open" and r["due"] >= start)
    breach = fc.breach_weeks
    first_idx = fc.weeks.index(breach[0]) if breach else len(fc.weeks)
    after_gap = 0
    if breach:
        last = breach[-1].start.isoformat()
        horizon = (breach[-1].start + timedelta(days=42)).isoformat()
        after_gap = sum(r["amount"] for r in fc.receivables
                        if r["status"] == "open" and last <= r["due"] <= horizon)
    return Metrics(
        months_trading=profile["business"]["months_trading"],
        monthly_revenue=int(mean * 52 / 12),
        card_monthly=int(statistics.mean(h["card_net"] for h in weeks) * 52 / 12) if weeks else 0,
        revenue_volatility=round(cv, 3),
        chargeback_rate=round(disputes / charges, 4),
        eligible_receivables=eligible_rec,
        need=fc.shortfall,
        need_weeks=len(breach),
        weeks_until_need=first_idx,
        receivables_after_gap=after_gap,
    )


def match(fc: Forecast, profile: dict, catalogue: list[dict]) -> tuple[Metrics, list[Match]]:
    m = metrics(fc, profile)
    results = []
    for p in catalogue:
        r = p["rules"]
        why_not, reasons = [], []
        if m.months_trading < r.get("min_months", 0):
            why_not.append(f"needs {r['min_months']} months of trading; you have {m.months_trading}")
        if m.monthly_revenue < r.get("min_monthly_revenue", 0):
            why_not.append(f"needs {fmt(r['min_monthly_revenue'])} monthly revenue; yours is {fmt(m.monthly_revenue)}")
        if "max_volatility" in r and m.revenue_volatility > r["max_volatility"]:
            why_not.append(f"needs steadier revenue (variation {m.revenue_volatility:.0%}, limit {r['max_volatility']:.0%})")
        if "max_chargeback_rate" in r and m.chargeback_rate > r["max_chargeback_rate"]:
            why_not.append(f"needs a dispute rate under {r['max_chargeback_rate']:.1%}; yours is {m.chargeback_rate:.1%}")
        if m.eligible_receivables < r.get("min_receivables", 0):
            why_not.append(f"needs at least {fmt(r['min_receivables'])} of unpaid customer invoices")
        if m.card_monthly < r.get("min_card_monthly", 0):
            why_not.append(f"needs {fmt(r['min_card_monthly'])} of monthly card sales")

        sizing = p["sizing"]
        if sizing["basis"] == "receivables":
            amount = int(m.eligible_receivables * sizing["rate"])
            basis = f"{sizing['rate']:.0%} of {fmt(m.eligible_receivables)} in open invoices"
        elif sizing["basis"] == "card_monthly":
            amount = int(m.card_monthly * sizing["rate"])
            basis = f"{sizing['rate']:.0%} of monthly card sales"
        else:
            amount = int(m.monthly_revenue * sizing["rate"])
            basis = f"{sizing['rate']:.0%} of monthly revenue"

        eligible = not why_not
        score = 0
        if eligible:
            covers = m.need == 0 or amount >= m.need
            score += 40 if covers else int(40 * amount / max(m.need, 1))
            reasons.append(f"could provide about {fmt(amount)} ({basis})"
                           + (", enough to cover the gap" if covers and m.need else ""))
            short_gap = m.need_weeks <= p["fit"]["max_gap_weeks"]
            if short_gap:
                score += 25
                reasons.append(f"suits a gap of {m.need_weeks} week(s)")
            else:
                why_not.append(f"built for gaps up to {p['fit']['max_gap_weeks']} weeks; yours lasts {m.need_weeks}")
            score += {1: 20, 2: 14, 3: 8, 4: 2}.get(p["fit"]["cost_rank"], 0)
            days = p["fit"]["days_to_fund"]
            days_left = 7 * m.weeks_until_need
            if days + 3 <= days_left:  # needs a few days of margin before the gap
                score += 15
                reasons.append(f"can fund in about {days} days, before the gap starts in {days_left} days")
            else:
                why_not.append(f"takes about {days} days to fund, too close to the gap starting in {days_left} days")
            if sizing["basis"] == "receivables" and m.need and m.receivables_after_gap >= m.need:
                score += 10
                reasons.append("repaid by customer invoices due right after the gap, so it matches its cause")
            if p.get("caution"):
                score -= 15
                reasons.append(p["caution"])
        results.append(Match(p, eligible, amount if eligible else 0, min(max(score, 0), 100), reasons, why_not))
    results.sort(key=lambda x: (not x.eligible, -x.score))
    return m, results


def readiness_pack(fc: Forecast, m: Metrics, top: Match | None, business: str) -> str:
    """A draft summary the owner can choose to share with a lender. Draft only (T1)."""
    lines = [
        f"# Financing readiness summary: {business}",
        "",
        "Prepared from reconciled payment, bank and invoice records. Draft for the owner's review.",
        "",
        f"- Verified cash today: {fmt(fc.verified_cash)} (bank {fmt(fc.bank_balance)}, payouts in transit {fmt(fc.in_transit)}, refunds owed {fmt(fc.refunds_owed)})",
        f"- Average monthly revenue (last 12 weeks): {fmt(m.monthly_revenue)}",
        f"- Revenue variation week to week: {m.revenue_volatility:.0%}",
        f"- Dispute rate: {m.chargeback_rate:.2%}",
        f"- Open customer invoices: {fmt(m.eligible_receivables)}",
        f"- Forecast need: {fmt(m.need)} below the safety floor, lasting {m.need_weeks} week(s), starting in {m.weeks_until_need} week(s)",
    ]
    if top:
        lines += ["", f"Best-fit facility type: **{top.product['name']}**, about {fmt(top.amount)}."]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
