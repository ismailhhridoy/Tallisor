"""Read books, payment-provider reports and bank statements into normalised records.

Provider formats are described by small JSON adapters in ./adapters, so adding a
new provider is a configuration change, not new code. Adapters are picked by
matching the file's header row against each adapter's signature.

All uploaded content is treated as untrusted:
  * size and row caps (protects against resource exhaustion, OWASP LLM10)
  * XML is parsed with defusedxml and DOCTYPE/ENTITY declarations are refused
  * free-text fields are kept as data and never executed or obeyed
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Union

from defusedxml import ElementTree as SafeET

from .models import BankTxn, BookEntry, PspTxn, Statement
from .money import MoneyParseError, to_cents, to_cents_or_zero

ADAPTER_DIR = Path(__file__).parent / "adapters"
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 100_000

Source = Union[str, Path, tuple]   # a path, or (filename, bytes) from an upload


class InputRejected(ValueError):
    """Raised when an input file is refused for safety or format reasons."""


def _read(source: Source) -> tuple[str, str]:
    if isinstance(source, tuple):
        name, data = source
    else:
        path = Path(source)
        if path.stat().st_size > MAX_BYTES:
            raise InputRejected(f"{path.name}: file larger than {MAX_BYTES // 1024 // 1024} MB")
        name, data = path.name, path.read_bytes()
    if len(data) > MAX_BYTES:
        raise InputRejected(f"{name}: file larger than {MAX_BYTES // 1024 // 1024} MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputRejected(f"{name}: not UTF-8 text") from exc
    return name, text


def load_adapters(role: str | None = None) -> list[dict]:
    adapters = []
    for path in sorted(ADAPTER_DIR.glob("*.json")):
        cfg = json.loads(path.read_text())
        cfg["_id"] = path.stem
        if role is None or cfg.get("role") == role:
            adapters.append(cfg)
    return adapters


def _rows(name: str, text: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            raise InputRejected(f"{name}: more than {MAX_ROWS} rows")
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    if not header:
        raise InputRejected(f"{name}: no header row")
    return header, rows


def detect_adapter(header: Iterable[str], role: str) -> dict:
    header_set = set(header)
    for cfg in load_adapters(role):
        if set(cfg["signature"]).issubset(header_set):
            return cfg
    raise InputRejected(f"no adapter recognises these columns: {', '.join(header)}")


def _iso_date(raw: str, fmt: str | None) -> str:
    if not raw:
        return ""
    if fmt:
        try:
            return datetime.strptime(raw, fmt).isoformat(sep=" ")
        except ValueError:
            pass
    for guess in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, guess).isoformat(sep=" ")
        except ValueError:
            continue
    return raw


# ---------------------------------------------------------------- books

def load_books(source: Source) -> list[BookEntry]:
    name, text = _read(source)
    header, rows = _rows(name, text)
    cfg = detect_adapter(header, "books")
    col = cfg["columns"]
    entries = []
    for line_no, row in enumerate(rows, start=2):
        try:
            amount = to_cents(row[col["amount"]])
            amount_usd = to_cents(row.get(col["amount_usd"]) or row[col["amount"]])
        except (KeyError, MoneyParseError) as exc:
            raise InputRejected(f"{name} line {line_no}: {exc}") from exc
        entries.append(BookEntry(
            ref=row[col["ref"]],
            date=row.get(col["date"], ""),
            customer=row.get(col["customer"], ""),
            channel=row.get(col["channel"], ""),
            currency=(row.get(col["currency"]) or "USD").upper(),
            amount=amount,
            amount_usd=amount_usd,
            fx_rate=row.get(col["fx_rate"]) or None,
            status=(row.get(col["status"]) or "paid").lower(),
        ))
    return entries


# ---------------------------------------------------------------- processors

def load_processor(source: Source) -> tuple[list[PspTxn], dict]:
    name, text = _read(source)
    header, rows = _rows(name, text)
    cfg = detect_adapter(header, "processor")
    col, kinds = cfg["columns"], cfg["kind_map"]
    txns = []
    for line_no, row in enumerate(rows, start=2):
        get = lambda key: row.get(col.get(key, ""), "")  # noqa: E731
        raw_kind = get("kind")
        kind = kinds.get(raw_kind, kinds.get(raw_kind.upper(), "adjustment"))
        try:
            gross = to_cents_or_zero(get("gross"))
            fee = abs(to_cents_or_zero(get("fee")))
            net = to_cents(get("net"))
        except MoneyParseError as exc:
            raise InputRejected(f"{name} line {line_no}: {exc}") from exc
        if kind == "payout" and cfg.get("payout_sign") == "positive":
            net, gross = -abs(net), -abs(gross or net)
        txns.append(PspTxn(
            provider=cfg["provider"],
            txn_id=get("txn_id"),
            ts=_iso_date(get("ts"), cfg.get("date_format")),
            kind=kind,
            ref=get("ref"),
            gross=gross,
            fee=fee,
            net=net,
            currency=(get("currency") or "USD").upper(),
            exchange_rate=get("exchange_rate") or None,
            card_country=get("card_country"),
            payout_id=get("payout_id"),
            arrival_date=_iso_date(get("arrival_date"), None)[:10],
            memo=get("memo"),
            evidence_due=get("evidence_due"),
            original_currency=(get("original_currency") or "").upper(),
            original_amount=to_cents_or_zero(get("original_amount")),
        ))
    return txns, cfg


# ---------------------------------------------------------------- bank

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node, *path):
    """Namespace-agnostic child lookup by local tag names."""
    current = [node]
    for name in path:
        nxt = []
        for n in current:
            nxt.extend(c for c in list(n) if _local(c.tag) == name)
        current = nxt
    return current


def _text(node, *path) -> str:
    found = _find(node, *path)
    return (found[0].text or "").strip() if found else ""


def _load_camt053(name: str, text: str) -> tuple[list[BankTxn], Statement]:
    head = text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise InputRejected(f"{name}: XML with DOCTYPE or ENTITY declarations is refused")
    try:
        root = SafeET.fromstring(text)
    except Exception as exc:  # defusedxml raises several types
        raise InputRejected(f"{name}: unreadable XML ({type(exc).__name__})") from exc
    stmts = [n for n in root.iter() if _local(n.tag) == "Stmt"]
    if not stmts:
        raise InputRejected(f"{name}: not a CAMT.053 statement")
    stmt = stmts[0]
    opening = closing = 0
    for bal in _find(stmt, "Bal"):
        code = _text(bal, "Tp", "CdOrPrtry", "Cd")
        amount = to_cents(_text(bal, "Amt"))
        if _text(bal, "CdtDbtInd") == "DBIT":
            amount = -amount
        if code == "OPBD":
            opening = amount
        elif code == "CLBD":
            closing = amount
    account = _text(stmt, "Acct", "Id", "IBAN") or _text(stmt, "Acct", "Id", "Othr", "Id")
    date = (_text(stmt, "CreDtTm") or _text(root, "GrpHdr", "CreDtTm"))[:10]
    txns = []
    for i, ntry in enumerate(_find(stmt, "Ntry"), start=1):
        if len(txns) >= MAX_ROWS:
            raise InputRejected(f"{name}: too many entries")
        amount = to_cents(_text(ntry, "Amt"))
        if _text(ntry, "CdtDbtInd") == "DBIT":
            amount = -amount
        remit = " ".join(filter(None, [
            _text(ntry, "NtryDtls", "TxDtls", "RmtInf", "Ustrd"),
            _text(ntry, "AddtlNtryInf"),
        ]))
        txns.append(BankTxn(
            txn_id=_text(ntry, "AcctSvcrRef") or _text(ntry, "NtryRef") or f"E{i}",
            date=_text(ntry, "BookgDt", "Dt") or _text(ntry, "ValDt", "Dt"),
            amount=amount,
            reference=_text(ntry, "NtryDtls", "TxDtls", "Refs", "EndToEndId") or _text(ntry, "NtryRef"),
            remittance_info=remit,
        ))
    return txns, Statement(account=account, date=date, opening_balance=opening, closing_balance=closing)


def load_bank(source: Source) -> tuple[list[BankTxn], Statement]:
    name, text = _read(source)
    if text.lstrip().startswith("<"):
        return _load_camt053(name, text)
    header, rows = _rows(name, text)
    cfg = detect_adapter(header, "bank")
    col = cfg["columns"]
    txns = []
    for i, row in enumerate(rows, start=1):
        txns.append(BankTxn(
            txn_id=row.get(col["txn_id"]) or f"L{i}",
            date=row.get(col["date"], ""),
            amount=to_cents(row[col["amount"]]),
            reference=row.get(col["reference"], ""),
            remittance_info=row.get(col["remittance_info"], ""),
        ))
    dates = sorted(t.date for t in txns if t.date)
    return txns, Statement(account="", date=dates[-1] if dates else "",
                           opening_balance=0, closing_balance=sum(t.amount for t in txns))
