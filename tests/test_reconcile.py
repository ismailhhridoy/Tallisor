from mismatchdesk.ingest import load_bank, load_processor
from mismatchdesk.money import fee_cents, fmt, to_cents


def test_money_parsing_is_exact():
    assert to_cents("1,234.56") == 123456
    assert to_cents("(45.00)") == -4500
    assert to_cents("−5.10") == -510
    assert to_cents("0.005") == 1          # half up
    assert to_cents("10,00") == 1000       # decimal comma
    assert to_cents("1.234,56") == 123456
    assert to_cents("-0,5") == -50
    assert to_cents("1,000") == 100000     # thousands comma stays thousands
    assert to_cents("1,234,567.89") == 123456789
    assert fee_cents(124000, 440, 30) == 5486
    assert fmt(-62750) == "−$627.50"


def test_regional_wallet_format_is_normalised(sample_data):
    txns, cfg = load_processor(sample_data / "swiftpay_settlement_09102026.csv")
    assert cfg["provider"] == "Swiftpay"
    sale = next(t for t in txns if t.kind == "charge")
    assert sale.fee > 0 and sale.net == sale.gross - sale.fee      # negative commission column normalised
    payout = next(t for t in txns if t.kind == "payout")
    assert payout.net < 0                                          # positive settlement row normalised to outflow
    assert sale.ts.startswith("2026-10-09")                        # day-first date parsed


def test_camt053_bank_statement(sample_data):
    txns, stmt = load_bank(sample_data / "bank_statement_camt053.xml")
    assert stmt.date == "2026-10-10"
    assert stmt.closing_balance == stmt.opening_balance + sum(t.amount for t in txns)
    assert any(t.amount < 0 for t in txns)


def test_gap_is_fully_explained(session):
    r = session.recon
    assert r.gap == r.expected - r.received
    assert r.residual == 0
    assert sum(c.impact for c in r.cases) == r.gap


def test_every_seeded_case_found_with_right_category_and_amount(session, gold):
    found = {c.ref: c for c in session.recon.cases}
    for g in gold:
        assert g["ref"] in found, g
        assert found[g["ref"]].category == g["category"], (g, found[g["ref"]].category)
        assert found[g["ref"]].variance == g["variance"], g
    assert len(found) == len(gold)              # no false positives


def test_clean_orders_match_automatically(session):
    r = session.recon
    assert len(r.matched) == r.order_count - sum(1 for c in r.cases if c.kind == "order")


def test_every_fix_balances_and_corrects_exactly_the_difference(session):
    for c in session.recon.cases:
        p = c.proposal
        if p and p.lines:
            assert p.balanced, c.id
            clearing = sum(l.debit - l.credit for l in p.lines if l.account == p.clearing_account)
            assert clearing == c.variance, c.id


def test_proposal_ids_are_deterministic(session, sample_data):
    from mismatchdesk.session import Session
    again = Session.from_dir(sample_data).run()
    a = {c.ref: c.proposal.id for c in session.recon.cases if c.proposal}
    b = {c.ref: c.proposal.id for c in again.recon.cases if c.proposal}
    assert a == b


def test_template_explanations_only_use_evidence_numbers(session):
    from mismatchdesk.security.guard import verify_numbers
    for c in session.recon.cases:
        ok, bad, _ = verify_numbers(c.explanation, c.allowed_numbers)
        assert ok, (c.id, bad, c.explanation)
