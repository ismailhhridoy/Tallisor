from tallisor.money import fmt


def test_verified_cash_differs_from_bank_balance(session):
    fc = session.forecast
    assert fc.verified_cash == fc.bank_balance + fc.in_transit - fc.refunds_owed
    assert fc.in_transit > 0 and fc.refunds_owed > 0


def test_forecast_rolls_forward_exactly(session):
    fc = session.forecast
    balance = fc.verified_cash
    for w in fc.weeks:
        assert w.opening == balance
        balance = w.opening + w.sales_in + w.receivables_in + w.other_in - w.outflows
        assert w.closing == balance


def test_overdue_invoices_are_not_counted_as_cash(session):
    fc = session.forecast
    overdue = [r for r in fc.receivables if r["status"] == "open" and r["due"] < fc.weeks[0].start.isoformat()]
    assert overdue
    counted = sum(w.receivables_in for w in fc.weeks)
    assert counted == sum(r["amount"] for r in fc.receivables if r["status"] == "open") - sum(r["amount"] for r in overdue)


def test_alerts_raised(session):
    types = {a["type"] for a in session.alerts}
    assert {"cash_floor", "payroll", "dispute", "refund_owed", "unpaid", "receivable", "payout"} <= types
    assert session.alerts[0]["severity"] == "critical"


def test_financing_recommendation_is_explained(session):
    m, matches = session.metrics, session.matches
    assert m.need == session.forecast.shortfall > 0
    top = matches[0]
    assert top.eligible and top.product["id"] == "receivables"
    assert top.amount >= m.need
    assert top.reasons
    ineligible = [x for x in matches if not x.eligible]
    assert ineligible and all(x.why_not for x in ineligible)     # every exclusion has a reason
    assert all(0 <= x.score <= 100 for x in matches)


def test_financing_uses_verified_not_self_reported_figures(session):
    # The need comes from the forecast, which starts from verified cash.
    fc = session.forecast
    assert session.metrics.need == max(0, fc.floor - min(w.closing for w in fc.weeks))
    assert fmt(session.metrics.need).startswith("$")
