"""Real-world layouts: Stripe-style itemized balance report, PayPal-style activity
download, SWIFT MT940 and a debit/credit bank CSV."""
import json

from tallisor.ingest import load_bank, load_processor
from tallisor.session import Session

from conftest import ROOT

REAL = ROOT / "sample_data" / "realformats"


def test_stripe_report_builds_payouts_from_groups():
    txns, cfg = load_processor(REAL / "stripe_itemized_balance.csv")
    assert cfg["provider"] == "Stripe"
    payouts = [t for t in txns if t.kind == "payout"]
    assert len(payouts) == 1 and payouts[0].payout_id == "po_1QW"
    assert -payouts[0].net == sum(t.net for t in txns if t.kind != "payout")
    dispute = next(t for t in txns if t.kind == "dispute")
    assert dispute.fee == 1500 and dispute.gross == -8000      # fee on the dispute row itself
    assert next(t for t in txns if t.ref == "ORD-1001").card_country == "US"


def test_paypal_negative_fees_denied_status_and_withdrawal():
    txns, cfg = load_processor(REAL / "paypal_activity.csv")
    sale = next(t for t in txns if t.ref == "ORD-1004")
    assert sale.kind == "charge" and sale.fee == 258 and sale.net == 5742
    denied = next(t for t in txns if t.ref == "ORD-1005")
    assert denied.kind == "failed" and denied.net == 0
    payout = next(t for t in txns if t.kind == "payout")
    assert payout.net == -5742 and payout.arrival_date == "2026-10-11"   # month-first date + 2 days


def test_mt940_statement():
    txns, stmt = load_bank(REAL / "bank_statement.mt940")
    assert stmt.opening_balance == 100000 and stmt.closing_balance == 117705
    assert stmt.date == "2026-10-12"
    assert [t.amount for t in txns] == [26963, 5742, -15000]
    assert txns[0].remittance_info == "STRIPE PAYMENTS PAYOUT"
    assert stmt.closing_balance == stmt.opening_balance + sum(t.amount for t in txns)


def test_debit_credit_bank_csv():
    csv_text = ("Date,Description,Reference,Debit,Credit,Balance\n"
                "09/10/2026,STRIPE PAYOUT,PO1,,269.63,1269.63\n"
                "10/10/2026,Office rent,R1,150.00,,1119.63\n").encode()
    txns, stmt = load_bank(("statement.csv", csv_text))
    assert [t.amount for t in txns] == [26963, -15000]
    assert txns[0].date == "2026-10-09"                  # day-first
    assert stmt.closing_balance == 111963                # taken from the running balance


def test_semicolon_and_windows_encoding():
    data = "Date;Description;Reference;Debit;Credit;Balance\n09/10/2026;Caf\xe9 supplies;X;10,00;;\n"
    txns, _ = load_bank(("stmt.csv", data.encode("cp1252")))
    assert txns[0].remittance_info == "Café supplies" and txns[0].amount == -1000


def test_full_run_on_real_formats_matches_answer_key():
    gold = json.loads((REAL / "gold.json").read_text())
    s = Session.from_dir(REAL).run()
    r = s.recon
    assert (r.expected, r.received, r.gap, r.residual) == (gold["expected"], gold["received"], gold["gap"], 0)
    found = {(c.ref, c.category, c.variance) for c in r.cases}
    assert found == {(g["ref"], g["category"], g["variance"]) for g in gold["cases"]}
    for c in r.cases:
        assert c.proposal is None or not c.proposal.lines or c.proposal.balanced


def test_forecast_without_history_uses_owner_estimate():
    s = Session.from_dir(REAL).run()
    assert "estimate" in s.forecast.sales_basis
    assert s.forecast.weekly_sales == 210000
    assert s.metrics.estimated                      # financing says its inputs are estimates
