<p><img src="assets/brand/tallisor-logo.png" alt="Tallisor" width="320"></p>


**See where every payment went.**

**Live demo:** [tallisor.streamlit.app](https://tallisor.streamlit.app/) (opens with a fictional sample company; no sign-up)

**A governed finance copilot for small businesses.** It checks that the money a business expected actually reached its bank, explains every difference, forecasts cash, raises alerts, and matches financing options to the business's *verified* cash flow. A person approves every change to the books, and the system cannot move money.

Entry for the 2026 Shenzhen International FinTech Competition (FinTechathon), International Track, **Topic E: SME Finance Copilot**.

| Topic E requirement | Where it is in Tallisor |
|---|---|
| Business financial analysis | Three-way reconciliation of books, payment providers and bank; every difference explained with evidence |
| Cash-flow management with alerts | Verified cash position, 13-week forecast, alerts for floor breaches, payroll, disputes, unpaid sales, refunds owed, late payouts |
| Financing-option matching | Rules-and-scoring engine over generic facility types, with sizing, a reason for each match and a reason for each exclusion |

## The problem it solves

A shop's bank balance is the wrong number for decisions. Payouts are still on their way, card fees are taken before money arrives, refunds and disputes are missing from the books, and some payments never happened. Owners find out when payroll bounces. Tallisor reconciles first, so cash planning and financing start from numbers that are proven.

## Quick start (about 2 minutes)

Requires Python 3.10 or newer.

```bash
git clone https://github.com/ismailhhridoy/tallisor.git
cd tallisor
python -m pip install -r requirements.txt
python -m pytest -q                 # 69 tests
python -m tallisor eval         # scored evaluation, writes evidence/
python -m streamlit run app.py      # web app at http://localhost:8501
```

Or with Docker (tests run during the build):

```bash
docker build -t tallisor .
docker run -p 8501:8501 tallisor
```

With `make`: `make install test eval app`.

### Command line

```bash
python -m tallisor run                          # reconcile the sample company and print results
python -m tallisor chat "Can I make payroll?"   # ask the copilot
python -m tallisor verify-log evidence/run_log.jsonl
python -m tallisor verify-log evidence/run_log_TAMPERED.jsonl   # exits 1: tampering detected
```

### Optional: connect an AI model

The product runs fully without a model, using fixed-rule explanations; that is the default, and the sidebar shows "AI writing: off". To have a model write the explanations and help classify chat requests, copy `.env.example` to `.env` in the project folder and add one key:

```bash
cp .env.example .env
# then edit .env:  ANTHROPIC_API_KEY=...   (or OPENAI_API_KEY / GEMINI_API_KEY)
```

For a free option, use a Gemini key from [Google AI Studio](https://aistudio.google.com/apikey) (default model `gemini-3.8-flash`). On Gemini's free tier Google may use prompts to improve its products and people may review them, so use it with the sample data and switch to a paid key before processing real customer data.

Restart the app and the sidebar shows "AI writing: on". The provider is picked from whichever key is present; `TALLISOR_LLM_PROVIDER` and `TALLISOR_LLM_MODEL` override it. `.env` is in `.gitignore` and is never uploaded.

Whatever the model writes, the categories, amounts and fixes still come from code, and any explanation containing an amount that is not in the evidence is rejected.

## Put it online

The app is ready for [Streamlit Community Cloud](https://share.streamlit.io) (free):

1. Sign in at share.streamlit.io with GitHub and click **Create app**.
2. Repository `ismailhhridoy/Tallisor`, branch `main`, main file `app.py`. Under advanced settings choose Python 3.12.
3. In **Secrets**, add:
   ```toml
   TALLISOR_HOSTED = "1"
   GEMINI_API_KEY = "your key"     # optional; or ANTHROPIC_API_KEY / OPENAI_API_KEY
   ```
4. Deploy. The app gets a `https://<name>.streamlit.app` address.

`TALLISOR_HOSTED=1` turns on hosted mode: each visitor gets a private temporary workspace (uploads, settings, history and mappings are never shared between visitors and disappear when the session ends), the AI key comes only from the server's secrets, and a notice asks visitors to use sample or anonymised files. This is a public demo, not a multi-customer service: there are no accounts and nothing is stored after a session.

## How it works

```
 Books CSV ─┐
 Card processor report ─┼─► Adapters ─► Matching engine ─► Cases ─► Investigator ─► Fix drafter ─► Human approval ─► Journal export
 Wallet settlement report ─┤  (JSON)      (code only)               (AI optional,    (code, checked)    (T2, people)
 Bank statement CAMT.053 ─┘                                          numbers checked)
                                           │
                                           ▼
                         Verified cash ─► 13-week forecast ─► Alerts ─► Financing matching ─► Readiness summary (draft)

 Every step: permission check (T0-T3) and a hash-chained audit log entry.
```

1. **Adapters** read each provider's own format. A card processor's balance report and a regional mobile wallet's settlement export look nothing alike (column names, day-first dates, negative commissions, positive settlement rows); each is described by a small JSON file in `tallisor/adapters/`. ISO 20022 CAMT.053 bank statements are parsed natively. Adding a provider is configuration, not code.
2. **Matching** is plain code with integer cents. It keeps one identity for every run: *expected deposits minus money received equals the sum of the explained differences*. If anything is left over, a "residual" case is raised instead of silently dropped.
3. **Investigation** categorises each difference (fee, refund, chargeback, exchange rate, duplicate charge, failed payment, unrecorded sale, payout in transit) by rule, and writes a plain explanation. A model may write the sentence; it cannot change the category or amounts.
4. **Fixes** are balanced journal entries built from the case facts. Each must balance and must change the provider clearing account by exactly the difference found, or it is refused.
5. **Approval** is for people only, and is bound to the fix's content hash so nobody approves something different from what they saw.
6. **Cash flow** starts from verified cash (bank plus payouts in transit minus refunds owed), not the bank balance.
7. **Financing** uses those verified figures to check eligibility, size each option, score fit and explain exclusions. It is informational: nothing is sent to any lender.

## Security, in one table

| Control | Covers | Tested in |
|---|---|---|
| Permission tiers T0 read, T1 propose, T2 approve (people only), T3 not available | Excessive agency, self-approval (OWASP LLM06, ASI02, ASI09) | `tests/test_security.py` |
| No money-moving code exists (capability absence) | Tool misuse (ASI02, ASI03) | same |
| Plan fixed from the owner's message before any file text is read (Plan-Then-Execute) | Goal hijack (ASI01) | `tests/test_agent.py` |
| Quarantined investigator, spotlighted file text, JSON-only output (Dual LLM) | Prompt injection (LLM01) | `tests/test_security.py` |
| Every AI-stated amount checked against evidence | Hallucinated numbers (LLM09) | same |
| Balanced, exact-size fixes only | Invalid outputs (LLM05) | same |
| Approval bound to content hash | Approval integrity (ASI09) | same |
| Hash-chained audit log with verifier | Tampering, accountability | same |
| Kill switch | Rogue behaviour (ASI10) | same |
| Size caps, safe XML parsing, DOCTYPE refused | Resource exhaustion (LLM10), XML attacks | same |

Full detail: [`docs/SECURITY_SELF_ASSESSMENT.md`](docs/SECURITY_SELF_ASSESSMENT.md). Architecture: [`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md).

## Evaluation (execution evidence)

`python -m tallisor eval` runs the agent three times on a sandbox company with a known answer key, plus 14 attacks, and writes [`evidence/eval_report.md`](evidence/eval_report.md). Current results:

- 8 of 8 seeded differences found with the exact category and amount; gap of $4,057.32 fully explained
- 7 of 7 drafted fixes balance; goal state reached in every run (pass^3)
- 14 of 14 attacks blocked (attack success rate 0%)
- Audit log verifies; the deliberately tampered copy is rejected

## Using it on your own files

1. **Upload** your sales export, one report per payment provider and your bank statement. Stripe itemised balance reports, PayPal activity downloads, CAMT.053 XML, MT940 and bank CSVs (single amount, or debit and credit columns) are read directly. Decimal commas (`1.234,56`) and day-first dates are handled. Open invoices and weekly sales history are optional; templates are in the sidebar.
2. **Unknown layout?** A file from a provider it has not seen opens a mapping screen with its best guess for each column, fee sign, date order and transaction types. Confirm once; the mapping is saved and the next file with those columns is read automatically.
3. **Settings** (owner only): business name and currency, fee contract per provider (domestic and international rates, home country), safety floor, regular payments (weekly, every two weeks, monthly, once), estimates used when there is no sales history. A provider with no contract on file has its fees accepted as charged, and a banner says so, rather than every sale being flagged.
4. **Review** the differences and approve fixes as before.
5. **History**: every run on your own files is saved with the exact files, settings, decisions and audit log. Uploading the same files again continues the saved run with your current settings; opening a run from History shows it as it was. A saved decision is re-applied only if the fix is byte-for-byte unchanged (same proposal id); otherwise it comes back for review.
6. **Excel report**: Summary, Differences, Journal, Cash forecast, Alerts, Financing and Audit log sheets, amounts as numbers with SUM cross-checks.
7. **Auto-approval rule** (off by default, owner only): approve high-confidence fixes in chosen categories (fee, FX, unrecorded refund, provider charge) at or under an amount. It acts in the owner's name, never the agent's; each approval is logged with the rule and who set it; it skips anything with a security flag and stops with the kill switch.

Your data stays on your computer, in `~/.tallisor` (set `TALLISOR_WORKSPACE` to use another folder).

## Repository layout

```
tallisor/          engine
  adapters/            provider and bank formats (JSON)
  security/            permission policy, guards, audit log
  llm/                 optional model clients (no SDK dependencies)
  ingest.py            file reading and normalisation
  reconcile.py         matching and categorising
  investigate.py       explanations (quarantined model + template)
  fixes.py             journal entries and their checks
  cashflow.py          verified cash, forecast, alerts
  financing.py         financing-option matching
  agent.py             chat copilot (intent, plan, execute)
  session.py           workflow, permissions, audit trail
  evaluate.py          scored evaluation and attack suite
  mapping.py           column mapping for file layouts it has not seen
  settings.py          business settings (fee contracts, cash plan, rules)
  store.py             saved runs, decisions and audit logs (SQLite index)
  report.py            Excel workbook of a run
app.py                 web app
data/                  illustrative financing facility types
sample_data/northwind/ sandbox company (fictional) and its answer key
sample_data/realformats/ Stripe itemised balance, PayPal activity and MT940 layouts, with answer key
scripts/make_sample.py regenerates the sandbox data
tests/                 69 tests
evidence/              evaluation report, audit logs, transcript, approved journal
docs/                  technical documentation and security self-assessment
assets/brand/          logo files and colours
```

## Limits and honest notes

- All companies in the samples are fictional. PayRail and Swiftpay are fictional providers; the Stripe, PayPal and MT940 sample files follow those formats' published layouts but contain made-up data. Real exports vary by account settings, which is what the mapping step is for.
- Financing products are generic facility types with illustrative terms, not real offers. Recommending real lenders is regulated in several countries; the product stays informational.
- The forecast is a simple direct method (8-week average sales, scheduled payments, invoices on their due date).
- There are no user accounts or logins yet. The live demo is a public, per-visitor sandbox, and installed locally it is single-user; the role selector demonstrates the permission model and is not access control.
- Only the deterministic path has been evaluated. Model-written explanations depend on the provider configured.

License: MIT.
