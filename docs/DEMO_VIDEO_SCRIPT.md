# Demo video script (under 5 minutes)

Record the screen with the app running (`python -m streamlit run app.py`) and a terminal beside it. Speak slowly; the judges review in English. Target length 4:30.

Before recording: run `python -m tallisor eval` once so `evidence/` is fresh, open the app, set "Your role" to owner, and zoom the browser to 110%.

| Time | Screen | Say |
|---|---|---|
| 0:00 to 0:20 | Overview tab | "This is Tallisor, a finance copilot for small-business owners. Our sandbox company, Northwind Market, has $25,841 in the bank. The agent says cash will fall to minus $15,175 in early November, and payroll is not covered. Let's see how it knows." |
| 0:20 to 1:10 | Overview: scroll to the bar chart | "Task one, business financial analysis. The books expected $14,699 from yesterday's sales; the bank received $10,641. The agent read four files: the books, a card processor report, a regional wallet report in a completely different layout, and an ISO 20022 bank statement. It matched 32 of 38 orders automatically and explained the remaining $4,057 to the cent: a payout still in transit, a chargeback, a failed payment, an unrecorded refund, an international card fee, an exchange-rate loss, a double charge and a wallet sale missing from the books." |
| 1:10 to 1:50 | Review differences: open "Disputed payment" | "Each difference shows its evidence, what was checked, and a balanced journal entry. The explanation is generated, but every amount in it is verified by code against the source rows. I tick that I've checked it and approve. The agent cannot approve its own work; only a person can, and the approval is tied to the exact entry I saw." Click the tick box, then Approve fix. |
| 1:50 to 2:40 | Cash flow tab | "Task two, cash-flow management. The forecast starts from verified cash, not the bank balance: the bank plus the payout in transit, minus the refund owed for the double charge. Thirteen weeks ahead, cash falls below the $15,000 floor from 26 October and below zero on 2 November, when payroll is due. The alerts below are what the owner sees first." |
| 2:40 to 3:20 | Financing tab | "Task three, financing-option matching, using those verified figures. Receivables financing fits best: about $33,200 against open invoices, funded in days, and repaid by the invoices that end the gap. A credit line is slower, a cash advance is expensive, and a term loan is excluded because the business is only 18 months old. Every option says why. The agent drafts a readiness summary, but it never applies or shares data." |
| 3:20 to 4:00 | Ask the copilot | Click "Can I make payroll?", then "Approve all fixes", then "Transfer $5,000 to my supplier". "The owner can just ask. The agent shows its plan, runs only permitted steps and answers from verified records. When I ask it to approve everything or transfer money, it refuses: moving money is not a rule it might break, the capability does not exist." |
| 4:00 to 4:30 | Audit and security tab, then terminal | "Every step, including those refusals, is in a hash-chained audit log, and the chain verifies." In the terminal run `python -m tallisor verify-log evidence/run_log_TAMPERED.jsonl`. "If anyone edits a line, the verifier catches it. Our evaluation blocks 14 of 14 attacks, and one command reproduces everything. Thank you." |

## Execution evidence checklist

The video and these files together are the "simulated-account/sandbox operation logs and demo video":

- `evidence/eval_report.md` and `evidence/eval_report.json`: scored results and attack suite
- `evidence/run_log.jsonl`: hash-chained log of one complete sandbox run
- `evidence/run_log_TAMPERED.jsonl`: edited copy the verifier rejects
- `evidence/chat_transcript.md`: the copilot answering eight owner questions, including two refused requests
- `evidence/approved_journal.csv`: the journal entries a reviewer approved in that run
