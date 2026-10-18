"""Command line.

    python -m mismatchdesk run      [DATA_DIR]       reconcile and print the summary
    python -m mismatchdesk chat     "question"       ask the copilot
    python -m mismatchdesk eval     [--k 3]          scored evaluation, writes evidence/
    python -m mismatchdesk verify-log FILE           check an audit log's hash chain
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import Copilot
from .llm import from_env
from .money import fmt
from .reconcile import CATEGORY_LABELS
from .security.audit import verify_file
from .session import Session

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "sample_data" / "northwind"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mismatchdesk")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("data", nargs="?", default=str(DEFAULT))
    r.add_argument("--log", default=None)
    c = sub.add_parser("chat")
    c.add_argument("question")
    c.add_argument("--data", default=str(DEFAULT))
    e = sub.add_parser("eval")
    e.add_argument("--k", type=int, default=3)
    v = sub.add_parser("verify-log")
    v.add_argument("file")
    a = p.parse_args(argv)

    if a.cmd == "run":
        s = Session.from_dir(a.data, llm=from_env(), log_path=a.log).run()
        print(s.gap_sentence())
        for case in s.recon.cases:
            print(f"  {case.id} {case.ref:<12} {CATEGORY_LABELS[case.category]:<36} {fmt(case.variance, signed=True):>12}  {case.explanation_source}")
        fc = s.forecast
        print(f"Verified cash {fmt(fc.verified_cash)} (bank {fmt(fc.bank_balance)}). Lowest {fmt(fc.lowest.closing)} week of {fc.lowest.label}.")
        for alert in s.alerts:
            print(f"  [{alert['severity']}] {alert['title']}")
        top = next((m for m in s.matches if m.eligible), None)
        if top:
            print(f"Best-fit financing: {top.product['name']} about {fmt(top.amount)}")
        print(f"Audit head {s.log.head[:16]} ({len(s.log.events)} events)")
        return 0
    if a.cmd == "chat":
        s = Session.from_dir(a.data, llm=from_env()).run()
        reply = Copilot(s).ask(a.question)
        print(f"[plan: {', '.join(reply.plan) or 'none'}{' - refused' if reply.refused else ''}]")
        print(reply.answer)
        return 0
    if a.cmd == "eval":
        from .evaluate import run
        rep = run(k=a.k)
        print(f"goal state pass^{rep['k']}: {rep['pass_k']}")
        print(f"attacks blocked: {sum(x['blocked'] for x in rep['attacks'])}/{len(rep['attacks'])}")
        print(f"audit log verified: {rep['audit_log']['verified']}; tampered copy rejected: {rep['tampered_log']['rejected']}")
        print("report: evidence/eval_report.md")
        return 0 if rep["pass_k"] and rep["attack_success_rate"] == 0 else 1
    if a.cmd == "verify-log":
        ok, line, why = verify_file(Path(a.file))
        print(("OK: " if ok else f"TAMPERED at line {line}: ") + why)
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
