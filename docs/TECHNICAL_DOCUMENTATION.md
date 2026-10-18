# Mismatch Desk: technical documentation

FinTechathon 2026, International Track, Topic E (SME Finance Copilot). Version 0.1.0, 19 October 2026.

## 1. What the agent does

Mismatch Desk is an AI agent that acts as a **digital employee for a small-business owner**. Given the business's own records, it completes a chain of real finance tasks:

1. **Business financial analysis.** Reconciles the books against every payment provider's report and the bank statement, and explains each difference with evidence.
2. **Cash-flow management with alerts.** Computes the *verified* cash position, forecasts 13 weeks, and raises alerts (cash floor breach, uncovered payroll, dispute deadlines, unpaid sales, refunds owed, late payouts, overdue invoices).
3. **Financing-option matching.** Matches financing facility types to the forecast need using verified figures, with a reason for every match and every exclusion, and drafts a readiness summary the owner may choose to share.

It does this inside a strict permission model: the agent reads and proposes; people approve; nobody can move money through the system, because that capability does not exist.

**Why reconcile first.** The bank balance is the wrong input for cash planning or credit. Payouts are in transit, fees are netted out, refunds and disputes are missing from the books, and some recorded sales were never paid. Advice built on that number is wrong in a predictable direction. Reconciliation turns the business's records into a verified position that the rest of the agent can trust.

## 2. Architecture

```
                 ┌──────────────── untrusted inputs ────────────────┐
 Books CSV       │ Card processor CSV   Wallet settlement CSV   Bank CAMT.053 XML │
                 └───────────────┬──────────────────────────────────┘
                                 ▼
  ingest.py      Adapters (JSON per provider) · size and row caps · defusedxml · DOCTYPE refused
                 → normalised BookEntry / PspTxn / BankTxn / Statement (integer cents)
                                 ▼
  reconcile.py   Deterministic matching engine (no model)
                 per-order expected vs actual · payouts vs bank · unbooked activity · residual check
                 → Cases with evidence, code-verified checks, allowed amounts
                                 ▼
  investigate.py Quarantined investigator: model optional, no tools, JSON only,
                 spotlighted file text, numeric guard; deterministic template fallback
                                 ▼
  fixes.py       Fix drafter: balanced journal entries; exact-difference invariant
                                 ▼
  session.py     Human approval (T2, bound to content hash) → journal export
                                 ▼
  cashflow.py    Verified cash → 13-week direct forecast → alerts
  financing.py   Metrics from verified data → eligibility → sizing → fit score → reasons
                                 ▼
  agent.py       Chat copilot: intent → fixed plan → permitted tools → answer

  security/      policy.py (tiers T0–T3) · guard.py (spotlighting, screening, numeric guard)
                 audit.py (SHA-256 hash chain, verifier) — every step above is authorised and logged
```

| Module | Responsibility | Lines of logic that matter |
|---|---|---|
| `ingest.py` | Read and normalise files; refuse unsafe ones | adapter detection by header signature; sign and date conventions |
| `reconcile.py` | Matching, categorising, case evidence | the gap identity; category precedence |
| `investigate.py` | Plain-language explanation | model call, guard, fallback |
| `fixes.py` | Journal entries | two invariants |
| `cashflow.py` | Verified cash, forecast, alerts | conservative receivables rule |
| `financing.py` | Facility matching | rules, sizing, scoring, why-not |
| `agent.py` | Conversation | Plan-Then-Execute |
| `session.py` | Workflow, permissions, decisions, audit | authorise-then-log on every step |
| `evaluate.py` | Scored evaluation and attack suite | goal state, pass^k, attack success rate |
| `app.py` | Web interface | review and approve, chat, audit view |

The engine is pure Python with one third-party runtime dependency for safety (`defusedxml`); the web app adds Streamlit, pandas and Altair. Model clients use only the standard library, so no vendor SDK is required.

## 3. Data model

All money is held as integer minor units (cents). Parsing goes through `Decimal` with round-half-up, and no floating-point arithmetic touches an amount. This is what makes the gap identity hold to the cent.

| Record | Key fields |
|---|---|
| `BookEntry` | ref, customer, channel (provider), currency, amount, amount_usd, fx_rate, status |
| `PspTxn` | provider, txn_id, kind (charge, refund, dispute, dispute_fee, failed, payout), ref, gross, fee (always positive), net, exchange_rate, card_country, payout_id, arrival_date, memo (untrusted), evidence_due |
| `BankTxn` | txn_id, date, amount (positive = in), reference, remittance_info (untrusted) |
| `Case` | category, variance (actual − expected), impact (contribution to the gap), evidence, checks, confidence, explanation, proposal, status, flags, allowed amounts |
| `Proposal` | journal lines, tasks for people, summary; `id` = SHA-256 of its content |

### Provider adapters

Each provider format is a JSON file mapping its columns and conventions to the normalised shape. The sample includes two deliberately different layouts:

| | PayRail (card processor) | Swiftpay (regional mobile wallet) |
|---|---|---|
| Dates | `2026-10-09 10:12:41` | `09/10/2026 10:12` (day first) |
| Type column | `reporting_category`: charge, refund, dispute… | `TxnType`: SALE, REVERSAL, SETTLEMENT |
| Fee sign | positive | negative (`Commission = -4.50`) |
| Payout row | negative amount | positive amount |

Adding a provider means adding one JSON file; the adapter is selected automatically when a file's header contains its signature columns. ISO 20022 CAMT.053 bank statements are parsed natively (balances `OPBD`/`CLBD`, entries with credit/debit indicator, end-to-end reference and remittance text), namespace-agnostic so different CAMT versions work.

## 4. Core algorithms

### 4.1 Matching and the gap identity

For each paid sale in the books:

```
expected_net = amount_usd − contracted_fee(provider, amount_usd)
actual_net   = Σ net of every provider transaction carrying the same order reference (excluding payouts)
variance     = actual_net − expected_net          → matched if 0, otherwise a Case
```

Provider transactions with no sale in the books become `unrecorded_sale` cases. Each payout is matched to a bank credit first by its payout ID appearing in the bank reference or remittance text, then by exact amount. Unmatched payouts become `payout_in_transit` (stated arrival after the statement date) or `payout_overdue`. Bank credits matching no payout are listed separately so they are never mistaken for sales.

The engine maintains, and tests assert, one identity:

```
gap = expected deposits − money received = Σ case.impact        (residual = 0)
```

If causes found do not add up to the gap, a `residual` case is created for a person. The system never reports "reconciled" while money is unaccounted for.

### 4.2 Categorising a difference

Categories are decided by code, in a fixed precedence that resolves overlaps (a disputed order may also have a refund):

| Order | Condition | Category |
|---|---|---|
| 1 | no provider record at all | `no_payment_record` |
| 2 | only failed attempts | `payment_failed` |
| 3 | more than one successful charge | `duplicate_charge` |
| 4 | a dispute row | `chargeback` |
| 5 | a refund row | `refund_unrecorded` |
| 6 | foreign currency and the provider's rate differs from the books' rate | `fx_difference` |
| 7 | fee above contract; checked against the contract's international rate | `fee_variance` (high confidence if it equals a contracted rate, medium "possible overcharge" otherwise) |
| 8 | anything else | `unexplained` (low confidence, sent to a person) |

Each case carries code-generated checks (for example "Card country DE triggers the contracted international rate of 4.4% + $0.30") and the set of amounts that legitimately appear in its evidence.

### 4.3 Drafting a fix and its invariants

Each category maps to a journal template. For example, a chargeback becomes:

| Account | Debit | Credit |
|---|---|---|
| Disputed charges receivable | 612.50 | |
| Dispute fees | 15.00 | |
| PayRail clearing | | 627.50 |

Before a draft is shown it must pass two checks, or it is refused and the case goes to a person:

1. **Balance:** total debits equal total credits.
2. **Exact size:** the net change on the provider clearing account equals the case's variance, so a fix corrects exactly the difference found, no more and no less.

Actions that are not bookkeeping (refunding a customer, sending dispute evidence, reopening an invoice) are produced as **tasks for a person**, never executed.

### 4.4 Verified cash and the forecast

```
verified cash = bank closing balance + payouts confirmed in transit − refunds owed to customers
```

The 13-week direct forecast adds the average of the last 8 weeks of settled sales, adds open customer invoices on their due date, and subtracts scheduled payments (payroll, suppliers, rent, one-off orders) from the business profile. Overdue invoices are **not** counted as cash; they are raised as alerts. The method is intentionally simple: every number traces to an input file and an owner can check it.

### 4.5 Financing-option matching

Metrics come from verified data only: months trading, average monthly revenue, week-to-week revenue variation, dispute rate, open (not overdue) invoices, and the forecast need (amount below the safety floor, how many weeks, how soon).

For each facility type in `data/financing_products.json`:

1. **Eligibility rules** (minimum months, revenue, receivables or card sales; maximum variation or dispute rate). Any failure is a "why not".
2. **Sizing** from the facility's basis (80% of open invoices, 25% of monthly revenue, 60% of monthly card sales…).
3. **Fit score out of 100:** covers the need (40), suits the gap's duration (25), cost rank (up to 20), can fund with margin before the gap starts (15), +10 for receivables financing when invoices due right after the gap match its cause, −15 for high-cost products.
4. **Explanation:** every recommendation lists its reasons; every exclusion lists its why-not.

In the sample, receivables financing scores 100 (about $33,200 against a $30,175 need, repaid by invoices due right after the gap), a credit line scores lower because it funds too close to the gap, a merchant cash advance is flagged as the most expensive, and a term loan is excluded (needs 24 months of trading, the business has 18).

Products are generic types with illustrative terms, not real offers. The agent never submits an application or shares data (tier T3). This keeps the feature informational, which matters because recommending specific lenders is a regulated activity in several jurisdictions.

### 4.6 The copilot: intent, plan, execute

The owner asks in plain language. The agent:

1. **Understands intent** by deterministic rules into a fixed list (gap, case, cash, financing, alerts, pack, export, approve, move money, submit application, help). A configured model may classify only into this list.
2. **Plans** a fixed sequence of named tools for that intent, from the message alone, before reading any tool output or file text (Plan-Then-Execute), and logs the plan.
3. **Authorises** each step against the permission policy. Requests to approve, move money or apply for financing are refused with an explanation of what the agent can do instead.
4. **Answers** from tool outputs: verified numbers, with references.

## 5. Where the AI is, and where it is not

| Step | Who decides | Why |
|---|---|---|
| Matching, categories, amounts, fixes, forecast, financing scores | Code | Must be exact, reproducible and auditable |
| Plain-language explanation of each case | Model (optional) or template | Language is where a model adds value |
| Classifying an ambiguous chat message | Rules, then model into a fixed list | Flexibility without new capabilities |
| Approving a fix | A person | Accountability |
| Moving money, contacting customers, applying for credit | Nobody (not implemented) | Irreversible actions stay outside the system |

The investigator follows the **Dual-LLM** pattern: it receives code-computed facts, has no tools, may return only `{"explanation": "..."}`, and sees file text only inside spotlighting fences with datamarking. Any money amount in its reply must equal an amount in the case's evidence, or the reply is discarded and the template is used. Every model-written explanation is labelled "AI-generated" in the interface.

The product works with no model at all. That is the default, so tests and the evaluation are reproducible and nobody needs an API key to run the submission. Supported providers when configured: Anthropic, OpenAI, Google Gemini.

## 6. Security design (summary)

Full detail, including the known-risk list, is in `SECURITY_SELF_ASSESSMENT.md`.

| Tier | Meaning | Roles |
|---|---|---|
| T0 Read | read inputs, compute, explain | agent, people |
| T1 Propose | investigate, draft fixes and readiness summaries | agent, people |
| T2 Approve | approve or reject a fix, bound to its exact content | people only |
| T3 Not available | move money, refund, contact customers, apply for financing, change bank details | nobody; not implemented |

Other controls: default-deny for unknown actions; approval bound to the proposal's content hash; kill switch; hash-chained audit log of every step including refusals; input size caps; safe XML parsing; injection screening as a tripwire (decisions never depend on free text).

## 7. Evaluation

`python -m mismatchdesk eval` runs the agent on the sandbox company Northwind Market (38 sales across a card processor and a regional wallet, a CAMT.053 bank statement, 8 seeded differences of 8 different kinds) and an attack suite. Metrics follow agent-benchmark practice: task success against a goal state and pass^k consistency (τ-bench), and utility under attack with attack success rate (AgentDojo).

| Measure | Result |
|---|---|
| Seeded differences found with exact category and amount | 8 / 8 (precision and recall 100% in every category) |
| Gap explained | $4,057.32 of $4,057.32, remainder $0.00 |
| Drafted fixes that balance and match their difference | 7 / 7 |
| Goal state in every run | pass^3 = yes |
| Attacks blocked | 14 / 14 (attack success rate 0%) |
| Audit log | verifies; tampered copy rejected at the edited line |
| Run time | under 0.01 s per run (deterministic mode) |

The evaluation is not a claim of real-world accuracy: the sandbox is synthetic and the seeded cases are the kinds the engine was built for. Its value is that it is reproducible, covers every category and every security control, and would catch a regression. It did catch one during development: the numeric guard missed an amount followed by a full stop, which was fixed and is now a regression test.

## 8. Deployment

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m mismatchdesk eval
python -m streamlit run app.py
```

Docker: `docker build -t mismatch-desk . && docker run -p 8501:8501 mismatch-desk` (tests run during the build; the container runs as a non-root user). Streamlit usage statistics are disabled in `.streamlit/config.toml` so no data leaves the machine except calls to a model provider, if one is configured.

## 9. Limitations

- The sample company, providers and bank are fictional; real formats need adapters, and real data will contain cases the rules do not cover (those become `unexplained` and go to a person).
- One sales day and one statement per run; multi-day matching windows and partial captures are not yet handled.
- The forecast does not model seasonality or uncertainty ranges.
- The fee contract, cash plan and invoice list come from a profile file; in the web demo, uploads reuse the sample's profile.
- Model-written explanations have not been evaluated against human judgement.

## 10. Roadmap

1. Adapter library for regional processors and wallets, built one market at a time with real exports.
2. Multi-day reconciliation with matching windows and learned rules from approved decisions.
3. Forecast ranges (best and worst case) and scenario questions in chat ("what if the stock order moves a week?").
4. Consent-based sharing of a verified readiness pack with a licensed lender partner, initiated by the owner.
5. Anchoring the audit log's head hash on a public ledger so third parties can verify it independently.
