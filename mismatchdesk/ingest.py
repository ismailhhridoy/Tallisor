"""Read books, payment-provider reports and bank statements into normalised records.

Provider formats are described by small JSON adapters, so adding a provider is
a configuration change, not new code. Built-in adapters live in ./adapters;
adapters a user creates with the column-mapping step are saved in the
workspace (see workspace.py) and loaded alongside them. An adapter is picked
by matching the file's header row against each adapter's signature.

Adapter options (all optional except columns/signature):
  columns          normalised field -> source column, or a list of fallbacks
  kind_map         exact source type -> normalised kind
  kind_contains    [[substring, kind], ...] checked in order, case-insensitive
  status_column / failed_statuses   rows with these statuses become 'failed'
  fee_sign         positive (fee shown as a cost) | negative (fee shown as -x)
  payout_sign      negative (payout reduces the balance) | positive
  date_formats     list of strptime formats, tried in order
  payouts_from_group {"id": col, "date": col}: build payout rows by summing
                   transactions that share a payout id (reports without payout rows)
  payout_arrival_days  arrival = transaction date + N days when not stated
  derive_rate      compute the exchange rate from gross / customer amount

All uploaded content is treated as untrusted:
  * size and row caps (protects against resource exhaustion, OWASP LLM10)
  * XML is parsed with defusedxml and DOCTYPE/ENTITY declarations are refused
  * free-text fields are kept as data and never executed or obeyed
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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


class UnknownFormat(InputRejected):
    """No adapter recognises the file. Carries what the mapping step needs."""

    def __init__(self, name: str, role: str, header: list[str], sample: list[dict]):
        super().__init__(f"{name}: no adapter recognises these columns: {', '.join(header)}")
        self.name, self.role, self.header, self.sample = name, role, header, sample


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
    for encoding in ("utf-8-sig", "cp1252"):   # many bank exports are Windows-1252
        try:
            return name, data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InputRejected(f"{name}: not a text file")


def _adapter_dirs() -> list[Path]:
    from .workspace import adapters_dir
    return [ADAPTER_DIR, adapters_dir()]


def load_adapters(role: str | None = None) -> list[dict]:
    adapters, seen = [], set()
    for folder in _adapter_dirs():
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                cfg = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            cfg["_id"] = path.stem
            cfg["_custom"] = folder != ADAPTER_DIR
            if path.stem in seen:
                continue
            seen.add(path.stem)
            if role is None or cfg.get("role") == role:
                adapters.append(cfg)
    return adapters


def _rows(name: str, text: str) -> tuple[list[str], list[dict]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            raise InputRejected(f"{name}: more than {MAX_ROWS} rows")
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None})
    if not header:
        raise InputRejected(f"{name}: no header row")
    return header, rows


def detect_adapter(header: Iterable[str], role: str, name: str = "file", rows=None) -> dict:
    header_set = set(header)
    # custom adapters first: a user's own mapping wins over a built-in guess
    for cfg in sorted(load_adapters(role), key=lambda c: not c.get("_custom")):
        if set(cfg["signature"]).issubset(header_set):
            return cfg
    raise UnknownFormat(name, role, list(header), (rows or [])[:200])


def _iso_date(raw: str, formats=None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    candidates = [raw, raw[:19], raw[:16], raw[:10]]   # tolerate trailing seconds, zones, "Z"
    for fmt in list(formats or []) + ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                                       "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d.%m.%Y"]:
        for text in candidates:
            try:
                return datetime.strptime(text, fmt).isoformat(sep=" ")
            except ValueError:
                continue
    return raw


def _col(row: dict, spec) -> str:
    """Value of the first listed column that is present and non-empty."""
    if not spec:
        return ""
    for name in ([spec] if isinstance(spec, str) else spec):
        value = row.get(name, "")
        if value != "":
            return value
    return ""


# ---------------------------------------------------------------- books

PAID_WORDS = {"paid", "completed", "complete", "settled", "succeeded", "success", "successful", "captured",
              "closed", "fulfilled"}


def _book_status(raw: str, cfg: dict) -> str:
    """Sales exports say 'Paid', 'Completed', 'Settled'... The engine needs 'paid'."""
    low = (raw or "paid").strip().lower()
    if low in {w.lower() for w in cfg.get("paid_statuses", [])} or low in PAID_WORDS:
        return "paid"
    return low


def load_books(source: Source, adapter: dict | None = None) -> list[BookEntry]:
    name, text = _read(source)
    header, rows = _rows(name, text)
    cfg = adapter or detect_adapter(header, "books", name, rows)
    col = cfg["columns"]
    entries = []
    for line_no, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        try:
            amount = to_cents(_col(row, col["amount"]))
            amount_usd = to_cents(_col(row, col.get("amount_usd")) or _col(row, col["amount"]))
        except (KeyError, MoneyParseError) as exc:
            raise InputRejected(f"{name} line {line_no}: {exc}") from exc
        entries.append(BookEntry(
            ref=_col(row, col["ref"]),
            date=_iso_date(_col(row, col.get("date")), cfg.get("date_formats"))[:10],
            customer=_col(row, col.get("customer")),
            channel=_col(row, col.get("channel")) or cfg.get("default_channel", ""),
            currency=(_col(row, col.get("currency")) or "USD").upper(),
            amount=amount,
            amount_usd=amount_usd,
            fx_rate=_col(row, col.get("fx_rate")) or None,
            status=_book_status(_col(row, col.get("status")), cfg),
        ))
    return entries


# ---------------------------------------------------------------- processors

def _kind(cfg: dict, row: dict) -> str:
    col = cfg["columns"]
    status_col = cfg.get("status_column")
    if status_col and row.get(status_col, "").strip().lower() in {s.lower() for s in cfg.get("failed_statuses", [])}:
        return "failed"
    raw = _col(row, col.get("kind"))
    if not raw and cfg.get("default_kind"):
        # No type column: positive amounts are sales, negative ones refunds.
        try:
            return cfg["default_kind"] if to_cents(_col(row, col.get("gross")) or "0") >= 0 else "refund"
        except MoneyParseError:
            return cfg["default_kind"]
    kinds = cfg.get("kind_map", {})
    if raw in kinds:
        return kinds[raw]
    if raw.upper() in kinds:
        return kinds[raw.upper()]
    low = raw.lower()
    for needle, kind in cfg.get("kind_contains", []):
        if needle.lower() in low:
            return kind
    return "adjustment"


def load_processor(source: Source, adapter: dict | None = None) -> tuple[list[PspTxn], dict]:
    name, text = _read(source)
    header, rows = _rows(name, text)
    cfg = adapter or detect_adapter(header, "processor", name, rows)
    col = cfg["columns"]
    formats = cfg.get("date_formats") or ([cfg["date_format"]] if cfg.get("date_format") else None)
    group = cfg.get("payouts_from_group")
    txns = []
    for line_no, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        get = lambda key: _col(row, col.get(key))  # noqa: E731
        kind = _kind(cfg, row)
        try:
            gross = to_cents_or_zero(get("gross"))
            fee_raw = to_cents_or_zero(get("fee"))
            fee = -fee_raw if cfg.get("fee_sign") == "negative" else fee_raw
            net_text = get("net")
            net = to_cents(net_text) if net_text else gross - fee
        except MoneyParseError as exc:
            raise InputRejected(f"{name} line {line_no}: {exc}") from exc
        if kind == "failed":
            net = 0
        if kind == "payout" and cfg.get("payout_sign") in ("positive", "auto"):
            net, gross = -abs(net), -abs(gross or net)
        ts = _iso_date(get("ts"), formats)
        arrival = _iso_date(get("arrival_date"), formats)[:10]
        if kind == "payout" and not arrival and cfg.get("payout_arrival_days") is not None and ts:
            arrival = (datetime.fromisoformat(ts[:10]) + timedelta(days=cfg["payout_arrival_days"])).date().isoformat()
        payout_id = get("payout_id") or (row.get(group["id"], "") if group else "")
        if kind == "payout" and not payout_id:
            payout_id = get("txn_id")
        rate = get("exchange_rate") or None
        orig_cur, orig_amt = (get("original_currency") or "").upper(), to_cents_or_zero(get("original_amount"))
        if not rate and cfg.get("derive_rate") and orig_amt and orig_cur and orig_cur != (get("currency") or "").upper():
            try:
                rate = str((Decimal(gross) / Decimal(orig_amt)).quantize(Decimal("0.0001")))
            except (InvalidOperation, ZeroDivisionError):
                rate = None
        txns.append(PspTxn(
            provider=cfg.get("provider") or cfg.get("name", "Provider"),
            txn_id=get("txn_id"), ts=ts, kind=kind, ref=get("ref"),
            gross=gross, fee=fee, net=net,
            currency=(get("currency") or "USD").upper(), exchange_rate=rate,
            card_country=get("card_country").upper(), payout_id=payout_id, arrival_date=arrival,
            memo=get("memo"), evidence_due=_iso_date(get("evidence_due"), formats)[:10],
            original_currency=orig_cur, original_amount=orig_amt,
        ))
    if group:
        txns.extend(_payouts_from_groups(txns, rows, group, cfg, formats))
    return txns, cfg


def _payouts_from_groups(txns, rows, group, cfg, formats) -> list[PspTxn]:
    existing = {t.payout_id for t in txns if t.kind == "payout"}
    dates = {}
    for row in rows:
        pid = row.get(group["id"], "")
        if pid and group.get("date"):
            dates[pid] = _iso_date(row.get(group["date"], ""), formats)[:10]
    totals: dict[str, int] = {}
    for t in txns:
        if t.payout_id and t.kind != "payout":
            totals[t.payout_id] = totals.get(t.payout_id, 0) + t.net
    out = []
    for pid, total in totals.items():
        if pid in existing:
            continue
        out.append(PspTxn(provider=cfg.get("provider", "Provider"), txn_id=pid, ts=dates.get(pid, ""),
                          kind="payout", ref="", gross=-total, fee=0, net=-total, payout_id=pid,
                          arrival_date=dates.get(pid, "")))
    return out


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


_MT940_61 = re.compile(r"^(\d{6})(\d{4})?(R?[CD])([A-Z])?([\d,]+)N?(\w{3})?(.*)$")


def _mt940_amount(text: str) -> int:
    return to_cents(text.replace(",", "."))


def _mt940_balance(value: str) -> tuple[int, str]:
    # e.g. C261010USD18400,00
    m = re.match(r"^([CD])(\d{6})([A-Z]{3})([\d,]+)$", value.strip())
    if not m:
        return 0, ""
    sign = 1 if m.group(1) == "C" else -1
    d = datetime.strptime(m.group(2), "%y%m%d").date().isoformat()
    return sign * _mt940_amount(m.group(4)), d


def _load_mt940(name: str, text: str) -> tuple[list[BankTxn], Statement]:
    """SWIFT MT940 customer statement: :60F: opening, :61: entries, :86: details, :62F: closing."""
    fields, current = [], None
    for line in text.splitlines():
        m = re.match(r"^:(\d{2}[A-Z]?):(.*)$", line)
        if m:
            current = [m.group(1), m.group(2)]
            fields.append(current)
        elif current is not None and not line.startswith("-}"):
            current[1] += "\n" + line
    txns, opening, closing, date, account = [], 0, 0, "", ""
    for i, (tag, value) in enumerate(fields):
        if tag == "25":
            account = value.strip()
        elif tag in ("60F", "60M"):
            opening, _ = _mt940_balance(value)
        elif tag in ("62F", "62M"):
            closing, date = _mt940_balance(value)
        elif tag == "61":
            first = value.split("\n")[0]
            m = _MT940_61.match(first)
            if not m:
                raise InputRejected(f"{name}: unreadable :61: line")
            vdate = datetime.strptime(m.group(1), "%y%m%d").date().isoformat()
            amount = _mt940_amount(m.group(5))
            if m.group(3) in ("D", "RC"):
                amount = -amount
            rest = m.group(7) or ""          # customer reference, then //bank reference
            ref = rest.split("//")[0]
            details = fields[i + 1][1].replace("\n", " ").strip() if i + 1 < len(fields) and fields[i + 1][0] == "86" else ""
            txns.append(BankTxn(txn_id=f"M{len(txns) + 1}", date=vdate, amount=amount,
                                reference=ref.strip(), remittance_info=details))
            if len(txns) > MAX_ROWS:
                raise InputRejected(f"{name}: too many entries")
    if not txns and not closing:
        raise InputRejected(f"{name}: not an MT940 statement")
    return txns, Statement(account=account, date=date, opening_balance=opening, closing_balance=closing)


def load_bank(source: Source, adapter: dict | None = None) -> tuple[list[BankTxn], Statement]:
    name, text = _read(source)
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return _load_camt053(name, text)
    if ":20:" in text[:2000] and ":61:" in text:
        return _load_mt940(name, text)
    header, rows = _rows(name, text)
    cfg = adapter or detect_adapter(header, "bank", name, rows)
    col = cfg["columns"]
    formats = cfg.get("date_formats")
    txns = []
    for i, row in enumerate(rows, start=1):
        if not any(row.values()):
            continue
        try:
            if col.get("amount"):
                amount = to_cents(_col(row, col["amount"]))
            else:
                amount = to_cents_or_zero(_col(row, col.get("credit"))) - abs(to_cents_or_zero(_col(row, col.get("debit"))))
        except MoneyParseError as exc:
            raise InputRejected(f"{name} line {i + 1}: {exc}") from exc
        txns.append(BankTxn(
            txn_id=_col(row, col.get("txn_id")) or f"L{i}",
            date=_iso_date(_col(row, col.get("date")), formats)[:10],
            amount=amount,
            reference=_col(row, col.get("reference")),
            remittance_info=_col(row, col.get("remittance_info")),
        ))
    dates = sorted(t.date for t in txns if t.date)
    closing = None
    if col.get("balance"):
        last = [r for r in rows if _col(r, col["balance"])]
        if last:
            closing = to_cents_or_zero(_col(last[-1], col["balance"]))
    return txns, Statement(account="", date=dates[-1] if dates else "",
                           opening_balance=0, closing_balance=closing if closing is not None else sum(t.amount for t in txns))
