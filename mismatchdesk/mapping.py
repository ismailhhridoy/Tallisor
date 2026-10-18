"""Teach Mismatch Desk a new file layout without writing code.

When no adapter recognises a file, suggest() proposes which column holds each
field (from column names and sample values), the user confirms or corrects it,
and save_adapter() stores it in the workspace. The same provider's next file is
then read automatically.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from .money import MoneyParseError, to_cents
from .workspace import adapters_dir

FIELDS = {
    "processor": {
        "required": ["ref", "gross"],
        "optional": ["txn_id", "ts", "kind", "fee", "net", "currency", "payout_id", "arrival_date",
                     "card_country", "memo"],
    },
    "books": {"required": ["ref", "amount"], "optional": ["date", "customer", "channel", "currency", "status"]},
    "bank": {"required": ["date"], "optional": ["amount", "debit", "credit", "reference", "remittance_info",
                                                 "txn_id", "balance"]},
}

LABELS = {
    "ref": "Order or invoice reference", "gross": "Amount charged", "amount": "Sale amount",
    "txn_id": "Transaction ID", "ts": "Date and time", "kind": "Transaction type", "fee": "Fee",
    "net": "Net amount", "currency": "Currency", "payout_id": "Payout or settlement batch",
    "arrival_date": "Payout arrival date", "card_country": "Card country", "memo": "Description",
    "date": "Date", "customer": "Customer", "channel": "Paid via (provider)", "status": "Status",
    "debit": "Money out (debit)", "credit": "Money in (credit)", "reference": "Reference",
    "remittance_info": "Description / narration", "balance": "Running balance",
}

SYNONYMS = {
    "ref": ["order id", "order", "orderref", "merchant ref", "merchantref", "reference", "invoice", "invoice no",
            "invoice number", "order number", "bill no", "ref", "merchant order id", "customer ref"],
    "gross": ["gross", "amount", "gross amount", "transaction amount", "txn amount", "sale amount", "total"],
    "amount": ["amount", "total", "invoice total", "sale amount", "gross", "value"],
    "txn_id": ["transaction id", "txn id", "trx id", "trxid", "id", "transaction ref", "payment id"],
    "ts": ["date", "created", "time", "date time", "transaction date", "txn date", "timestamp", "created at"],
    "kind": ["type", "transaction type", "txn type", "category", "reporting category", "event"],
    "fee": ["fee", "fees", "commission", "charge", "mdr", "processing fee", "service charge"],
    "net": ["net", "net amount", "settled amount", "payable"],
    "currency": ["currency", "ccy", "cur"],
    "payout_id": ["payout id", "payout", "settlement id", "settlement batch", "batch", "batch id", "transfer id"],
    "arrival_date": ["arrival date", "settlement date", "value date", "payout date"],
    "card_country": ["card country", "issuer country", "country"],
    "memo": ["description", "narration", "note", "details", "remarks", "memo"],
    "date": ["date", "transaction date", "value date", "posting date", "booking date", "invoice date"],
    "customer": ["customer", "customer name", "name", "client", "buyer"],
    "channel": ["paid via", "payment method", "gateway", "channel", "provider"],
    "status": ["status", "state", "payment status"],
    "debit": ["debit", "withdrawal", "money out", "dr", "paid out"],
    "credit": ["credit", "deposit", "money in", "cr", "paid in"],
    "reference": ["reference", "ref", "ref no", "cheque no", "transaction reference"],
    "remittance_info": ["description", "details", "narration", "particulars", "remarks", "transaction details"],
    "balance": ["balance", "running balance", "closing balance"],
}

KIND_WORDS = [
    ("failed", ["fail", "declin", "denied", "void", "reject"]),
    ("refund", ["refund", "reversal", "return"]),
    ("dispute", ["dispute", "chargeback"]),
    ("payout", ["payout", "settlement", "withdraw", "transfer to bank", "disburse"]),
    ("fee", ["fee", "commission", "charge fee"]),
    ("charge", ["sale", "payment", "charge", "purchase", "capture", "credit"]),
]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def suggest(header: list[str], role: str, sample: list[dict]) -> dict:
    """Best-guess mapping field -> column, plus detected options."""
    fields = FIELDS[role]["required"] + FIELDS[role]["optional"]
    taken, mapping = set(), {}
    for field in fields:
        best, best_score = None, 0
        for col in header:
            if col in taken:
                continue
            n = _norm(col)
            for syn in SYNONYMS.get(field, []):
                score = 3 if n == syn else 2 if n.replace(" ", "") == syn.replace(" ", "") else 1 if syn in n.split() or n.startswith(syn) else 0
                if score > best_score:
                    best, best_score = col, score
        if best and best_score >= 1:
            mapping[field] = best
            taken.add(best)
    options = {"fee_sign": "positive", "day_first": True}
    fee_col = mapping.get("fee")
    if fee_col:
        values = [_try(r.get(fee_col, "")) for r in sample]
        values = [v for v in values if v]
        if values and sum(v < 0 for v in values) > len(values) / 2:
            options["fee_sign"] = "negative"
    date_col = mapping.get("ts") or mapping.get("date")
    if date_col:
        options["day_first"] = _day_first([r.get(date_col, "") for r in sample])
    return {"mapping": mapping, "options": options}


def _try(text: str):
    try:
        return to_cents(text)
    except MoneyParseError:
        return None


def _day_first(values: list[str]) -> bool:
    for v in values:
        m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", v.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return True
            if b > 12:
                return False
    return True


def kind_suggestions(values: list[str]) -> dict:
    out = {}
    for v in sorted(set(x for x in values if x)):
        low = v.lower()
        out[v] = next((kind for kind, words in KIND_WORDS if any(w in low for w in words)), "charge")
    return out


def date_formats(day_first: bool) -> list[str]:
    if day_first:
        return ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
    return ["%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]


def build_adapter(*, name: str, role: str, mapping: dict, provider: str = "", fee_sign: str = "positive",
                  day_first: bool = True, kind_map: dict | None = None, payout_arrival_days: int | None = None) -> dict:
    missing = [f for f in FIELDS[role]["required"] if not mapping.get(f)]
    if role == "bank" and not (mapping.get("amount") or (mapping.get("debit") or mapping.get("credit"))):
        missing.append("amount, or debit and credit")
    if missing:
        raise ValueError("Choose a column for: " + ", ".join(LABELS.get(m, m) for m in missing))
    columns = {k: v for k, v in mapping.items() if v}
    cfg = {"name": name.strip() or f"Custom {role} format", "role": role,
           "description": "Created with the column-mapping step.",
           "signature": sorted(set(columns.values())), "columns": columns,
           "date_formats": date_formats(day_first)}
    if role == "processor":
        cfg.update(provider=provider.strip() or name.strip(), fee_sign=fee_sign,
                   payout_sign="auto", kind_map=kind_map or {}, default_kind="charge")
        if payout_arrival_days is not None:
            cfg["payout_arrival_days"] = payout_arrival_days
    if role == "books" and provider:
        cfg["default_channel"] = provider.strip()
    return cfg


def save_adapter(cfg: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", cfg["name"].lower()).strip("_")[:60] or "custom"
    path = adapters_dir() / f"custom_{slug}.json"
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    path.write_text(json.dumps(clean, indent=2))
    return path.stem


def distinct(sample_rows: list[dict], column: str) -> list[str]:
    return [v for v, _ in Counter(r.get(column, "") for r in sample_rows).most_common(50) if v]
