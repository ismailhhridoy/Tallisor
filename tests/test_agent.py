from mismatchdesk.agent import Copilot, classify


def test_intent_understanding():
    assert classify("Why is my bank short today?") == "gap"
    assert classify("Can I make payroll next month?") == "cash"
    assert classify("What financing fits my business?") == "financing"
    assert classify("What needs my attention?") == "alerts"
    assert classify("Explain NW-48242") == "case"
    assert classify("Prepare a readiness pack for a lender") == "pack"
    assert classify("approve all fixes") == "approve"
    assert classify("transfer money to my supplier") == "move_money"
    assert classify("hello") == "help"


def test_plan_logged_before_execution(session):
    Copilot(session).ask("Can I make payroll?")
    plans = [e for e in session.log.events if e["action"] == "plan"]
    assert plans and plans[-1]["details"]["steps"] == ["forecast", "alerts", "match_financing"]


def test_answers_contain_verified_numbers(session):
    bot = Copilot(session)
    gap = bot.ask("Why is my bank short?")
    assert "All $" in gap.answer and "accounted for" in gap.answer
    cash = bot.ask("Can I make payroll?")
    assert "verified cash" in cash.answer and "Receivables financing" in cash.answer
    fin = bot.ask("What financing fits?")
    assert "Working-capital term loan" in fin.answer and "not a fit" in fin.answer
    case = bot.ask("Explain NW-48242")
    assert "chargeback" in case.answer.lower()
    pack = bot.ask("Prepare a readiness pack")
    assert "Draft" in pack.answer
