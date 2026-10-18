"""Business settings a real user owns: fee contracts, cash plan, safety floor.

Stored as profile.json in the workspace. The engine reads the same profile
structure the sample companies use, so settings and samples never diverge.
All money here is integer cents; the app converts to and from display amounts.
"""
from __future__ import annotations

import copy
import json
from datetime import date

from .workspace import root

PAYMENT_KINDS = ("weekly", "every 2 weeks", "monthly", "once")


def default_profile(name: str = "My business", currency: str = "USD") -> dict:
    return {
        "business": {"name": name, "country": "", "months_trading": 12, "reporting_currency": currency,
                     "revenue_variation_estimate": 0.3, "dispute_rate_estimate": 0.005, "card_share_estimate": 1.0},
        "sales_date": "",
        "files": {},
        "fee_schedules": {},
        "cash": {"weeks": 13, "floor": 0, "weekly_sales": 0, "payments": []},
        "auto_approve": {"enabled": False},
    }


def path():
    return root() / "profile.json"


def load() -> dict | None:
    p = path()
    if not p.exists():
        return None
    return merge_defaults(json.loads(p.read_text()))


def save(profile: dict) -> None:
    validate(profile)
    path().write_text(json.dumps(profile, indent=2))


def merge_defaults(profile: dict) -> dict:
    """Fill keys a saved or older profile may lack, without changing what is there."""
    out = default_profile()
    for key, value in profile.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def validate(profile: dict) -> None:
    problems = []
    biz = profile.get("business", {})
    if not str(biz.get("name", "")).strip():
        problems.append("Business name is empty.")
    for provider, sched in profile.get("fee_schedules", {}).items():
        for tier in ("domestic", "international"):
            s = sched.get(tier)
            if s is None:
                continue
            if not (0 <= s.get("bp", -1) <= 2000):
                problems.append(f"{provider} {tier} rate must be between 0% and 20%.")
            if not (0 <= s.get("fixed", -1) <= 10000):
                problems.append(f"{provider} {tier} fixed fee must be between 0 and 100.")
        if "domestic" not in sched:
            problems.append(f"{provider}: enter at least the domestic rate.")
    cash = profile.get("cash", {})
    if cash.get("floor", 0) < 0:
        problems.append("Safety floor cannot be negative.")
    if cash.get("weekly_sales", 0) < 0:
        problems.append("Weekly sales cannot be negative.")
    for p in cash.get("payments", []):
        if p.get("amount", 0) <= 0:
            problems.append(f"Payment '{p.get('name', '?')}' needs an amount above zero.")
        if not any(k in p for k in ("once", "every_days", "monthly_day")):
            problems.append(f"Payment '{p.get('name', '?')}' needs a schedule.")
    if problems:
        raise ValueError(" ".join(problems))


def missing_fee_contracts(profile: dict, providers: list[str]) -> list[str]:
    have = profile.get("fee_schedules", {})
    return sorted(p for p in set(providers) if p and p not in have)


def fee_schedule(domestic_pct: float, domestic_fixed: float, intl_pct: float | None = None,
                 intl_fixed: float | None = None, home_country: str = "") -> dict:
    """Build a schedule from percentages and display amounts (2.9, 0.30)."""
    out = {"domestic": {"bp": round(domestic_pct * 100), "fixed": round(domestic_fixed * 100)}}
    if intl_pct is not None:
        out["international"] = {"bp": round(intl_pct * 100), "fixed": round((intl_fixed or 0) * 100)}
        if home_country:
            out["home_country"] = home_country.upper()
    return out


def payment_rows(profile: dict) -> list[dict]:
    """Recurring payments as editable table rows in display units."""
    rows = []
    for p in profile.get("cash", {}).get("payments", []):
        if "once" in p:
            kind, start = "once", p["once"]
        elif "monthly_day" in p:
            kind, start = "monthly", f"day {p['monthly_day']}"
        elif p.get("every_days") == 14:
            kind, start = "every 2 weeks", p["first"]
        else:
            kind, start = "weekly", p.get("first", "")
        rows.append({"name": p["name"], "amount": p["amount"] / 100, "repeats": kind, "next_date": start})
    return rows


def payments_from_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        amount = round(float(r.get("amount") or 0) * 100)
        kind = r.get("repeats") or "monthly"
        when = str(r.get("next_date") or "").strip()
        item = {"name": name, "amount": amount}
        if kind == "monthly":
            day = when.lower().replace("day", "").strip()
            if day.isdigit():
                item["monthly_day"] = max(1, min(31, int(day)))
            else:
                item["monthly_day"] = date.fromisoformat(when).day if when else 1
        else:
            if not when:
                raise ValueError(f"Payment '{name}' needs a next date (YYYY-MM-DD).")
            try:
                date.fromisoformat(when)
            except ValueError as exc:
                raise ValueError(f"Payment '{name}': next date must look like 2026-11-01.") from exc
            if kind == "once":
                item["once"] = when
            else:
                item["every_days"] = 14 if kind == "every 2 weeks" else 7
                item["first"] = when
        out.append(item)
    return out


def with_files(profile: dict, sales_date: str = "") -> dict:
    """A run-ready copy: uploaded files are passed directly, so 'files' is not needed."""
    p = copy.deepcopy(profile)
    if sales_date:
        p["sales_date"] = sales_date
    return p
