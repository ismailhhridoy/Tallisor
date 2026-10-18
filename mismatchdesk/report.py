"""Excel workbook of a run, for the accountant, the lender or the files.

Sheets: Summary, Differences, Journal, Cash forecast, Alerts, Financing, Audit log.
Amounts are written as numbers (not text) with a currency format, so the
accountant can sum and filter them. Every number comes from the session; the
workbook adds no calculation of its own except the Excel SUM totals, which are
cross-checks the reader can see.
"""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .financing import DISCLAIMER
from .reconcile import CATEGORY_LABELS

MONEY = '#,##0.00;[Red]-#,##0.00'
HEAD = PatternFill("solid", fgColor="0A5C4F")


def _sheet(wb, title, header, rows, money_cols=(), widths=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    for idx in money_cols:
        letter = get_column_letter(idx)
        for cell in ws[letter][1:]:
            if isinstance(cell.value, (int, float)):
                cell.number_format = MONEY
    for i, h in enumerate(header, start=1):
        width = (widths or {}).get(h) or min(60, max(12, len(str(h)) + 2,
                                                     *(len(str(r[i - 1])) + 2 for r in rows[:200] if i - 1 < len(r))))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    return ws


def _m(cents: int) -> float:
    return round(cents / 100, 2)


def build_workbook(session) -> bytes:
    s, r, fc = session.summary(), session.recon, session.forecast
    ccy = session.profile["business"].get("reporting_currency", "USD")
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    rows = [
        ("Business", s["business"]), ("Currency", ccy), ("Sales date", s["sales_date"]),
        ("Bank statement date", s["statement_date"]), ("Providers", ", ".join(s["providers"])),
        ("Orders checked", s["orders"]), ("Matched automatically", s["matched"]), ("Differences found", s["cases"]),
        ("Expected deposits", _m(s["expected"])), ("Received in bank", _m(s["received"])),
        ("Gap", _m(s["gap"])), ("Explained", _m(s["explained"])), ("Unexplained", _m(s["residual"])),
        ("Bank balance", _m(s["bank_balance"])), ("Payouts in transit", _m(s["in_transit"])),
        ("Refunds owed", _m(s["refunds_owed"])), ("Verified cash", _m(s["verified_cash"])),
        ("Lowest forecast cash", _m(s["lowest_cash"])), ("Lowest week", s["lowest_week"]),
        ("Shortfall below safety floor", _m(s["shortfall"])),
        ("Approved fixes", sum(c.status == "approved" for c in r.cases)),
        ("Open fixes", sum(c.status == "open" for c in r.cases)),
        ("Audit log head hash", s["audit_head"]),
    ]
    ws.append(["Mismatch Desk report", ""])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value in rows:
        ws.append([label, value])
        if isinstance(value, float):
            ws.cell(ws.max_row, 2).number_format = MONEY
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 70

    _sheet(wb, "Differences",
           ["Case", "Reference", "Party", "Provider", "Category", "Impact on gap", "Variance", "Confidence",
            "Status", "Explanation", "Explained by"],
           [[c.id, c.ref, c.party, c.provider, CATEGORY_LABELS.get(c.category, c.category), _m(c.impact),
             _m(c.variance), c.confidence, c.status, c.explanation, c.explanation_source] for c in r.cases],
           money_cols=(6, 7), widths={"Explanation": 80})
    ws = wb["Differences"]
    if r.cases:
        ws.append(["Total", "", "", "", "", f"=SUM(F2:F{len(r.cases) + 1})"])
        ws.cell(ws.max_row, 6).number_format = MONEY
        ws.cell(ws.max_row, 1).font = Font(bold=True)

    journal = []
    for c in r.cases:
        if c.proposal:
            for line in c.proposal.lines:
                journal.append([r.statement.date, c.id, c.ref, c.status, line.account,
                                _m(line.debit) if line.debit else None, _m(line.credit) if line.credit else None,
                                c.proposal.summary, c.proposal.id])
    _sheet(wb, "Journal", ["Date", "Case", "Reference", "Status", "Account", "Debit", "Credit", "Memo",
                           "Proposal id"], journal, money_cols=(6, 7), widths={"Memo": 60})
    ws = wb["Journal"]
    if journal:
        n = len(journal) + 1
        ws.append(["Total", "", "", "", "", f"=SUM(F2:F{n})", f"=SUM(G2:G{n})"])
        for col in (6, 7):
            ws.cell(ws.max_row, col).number_format = MONEY
        ws.cell(ws.max_row, 1).font = Font(bold=True)

    _sheet(wb, "Cash forecast", ["Week of", "Opening", "Sales in", "Invoices in", "Other in", "Payments out",
                                 "Closing", "Below floor"],
           [[w.start.isoformat(), _m(w.opening), _m(w.sales_in), _m(w.receivables_in), _m(w.other_in),
             _m(w.outflows), _m(w.closing), "yes" if w.closing < fc.floor else ""] for w in fc.weeks],
           money_cols=(2, 3, 4, 5, 6, 7))

    _sheet(wb, "Alerts", ["Severity", "Alert", "Detail"],
           [[a["severity"], a["title"], a["detail"]] for a in session.alerts], widths={"Detail": 90})

    fin_rows = [[m.product["name"], "yes" if m.eligible else "no", _m(m.amount) if m.eligible else None,
                 m.score if m.eligible else None, "; ".join(m.reasons), "; ".join(m.why_not)]
                for m in session.matches]
    _sheet(wb, "Financing", ["Option", "Eligible", "Suggested amount", "Fit score", "Why", "Why not"], fin_rows,
           money_cols=(3,), widths={"Why": 60, "Why not": 60})
    ws = wb["Financing"]
    ws.append([])
    ws.append([DISCLAIMER])

    _sheet(wb, "Audit log", ["Seq", "Time", "Actor", "Action", "Tier", "Outcome", "Details", "Hash"],
           [[e["seq"], e["ts"], e["actor"], e["action"], e["tier"], e["outcome"],
             str(e["details"])[:500], e["hash"][:16]] for e in session.log.events], widths={"Details": 80})

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
