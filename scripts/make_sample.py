"""Generate the sandbox company "Northwind Market" with seeded, known mismatches.

Writes sample_data/northwind/ plus gold.json, the answer key the evaluation
scores against. Deterministic: the same seed always produces the same files.
All companies, providers, people and banks are fictional.

    python scripts/make_sample.py
"""
from __future__ import annotations

import csv
import json
import random
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sample_data" / "northwind"

SALES_DATE = "2026-10-09"
STATEMENT_DATE = "2026-10-10"

CUSTOMERS = ["Harbor Print Co", "Linden Cycles", "Okafor Studio", "Mesa Coffee Roasters", "Brightwell Dental",
             "Tanaka Ceramics", "Castillo Bikes", "Fenwick Books", "Morrow Bakery", "Sato Audio",
             "Ellery Stationers", "Marlow Garden Supply", "Ibarra Textiles", "Rahman Electronics",
             "Chowdhury Crafts", "Nguyen Tailoring", "Adeyemi Foods", "Haddad Spices"]


def fee(cents: int, bp: int, fixed: int) -> int:
    return (cents * bp + 5000) // 10000 + fixed


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def main(seed: int = 20261009) -> None:
    rnd = random.Random(seed)
    OUT.mkdir(parents=True, exist_ok=True)
    books, payrail, swift, gold = [], [], [], []
    n = 0

    def ref() -> str:
        nonlocal n
        n += 1
        return f"NW-{48200 + n}"

    def ts(hour: int, minute: int) -> str:
        return f"{SALES_DATE} {hour:02d}:{minute:02d}:{rnd.randint(0, 59):02d}"

    # -------- normal card orders: 14 morning (payout A), 10 afternoon (payout B)
    for i in range(24):
        r, amt = ref(), rnd.randint(1800, 62000)
        cust = rnd.choice(CUSTOMERS)
        books.append([r, SALES_DATE, cust, "PayRail", "USD", money(amt), money(amt), "", "paid"])
        f = fee(amt, 290, 30)
        batch = "po_A_1009" if i < 14 else "po_B_1009"
        hour = 8 + i // 3 if i < 14 else 14 + (i - 14) // 3
        payrail.append(dict(id=f"ch_{rnd.randrange(16**6):06x}", created=ts(hour, rnd.randint(0, 59)),
                            reporting_category="charge", order_id=r, gross=money(amt), fee=money(f), net=money(amt - f),
                            currency="usd", card_country="US", payout_id=batch))

    # -------- normal wallet orders (Swiftpay, regional format)
    for i in range(8):
        r, amt = ref(), rnd.randint(1500, 30000)
        cust = rnd.choice(CUSTOMERS)
        books.append([r, SALES_DATE, cust, "Swiftpay", "USD", money(amt), money(amt), "", "paid"])
        f = fee(amt, 150, 0)
        swift.append(dict(TrxID=f"SWP{rnd.randrange(10**9):09d}", TxnDateTime=f"09/10/2026 {9 + i:02d}:{rnd.randint(0, 59):02d}",
                          MerchantRef=r, TxnType="SALE", GrossAmt=money(amt), Commission=money(-f), NetAmt=money(amt - f),
                          Ccy="USD", SettlementBatch="SWB-20261009", ValueDate="", Narration="Wallet payment"))

    n = 39  # seeded cases start at NW-48240

    # 1. international card: higher fee
    r, amt = ref(), 124000
    books.append([r, SALES_DATE, "Aalto Lighting", "PayRail", "USD", money(amt), money(amt), "", "paid"])
    f = fee(amt, 440, 30)
    payrail.append(dict(id="ch_7Kd2a1", created=ts(10, 12), reporting_category="charge", order_id=r, gross=money(amt),
                        fee=money(f), net=money(amt - f), currency="usd", card_country="DE", payout_id="po_A_1009"))
    gold.append({"ref": r, "category": "fee_variance", "variance": -(f - fee(amt, 290, 30))})

    # 2. partial refund not in the books
    r, amt, refund = ref(), 34000, 8500
    books.append([r, SALES_DATE, "Juniper Florist", "PayRail", "USD", money(amt), money(amt), "", "paid"])
    f = fee(amt, 290, 30)
    payrail.append(dict(id="ch_3Vp9b2", created=ts(9, 5), reporting_category="charge", order_id=r, gross=money(amt),
                        fee=money(f), net=money(amt - f), currency="usd", card_country="US", payout_id="po_A_1009"))
    payrail.append(dict(id="re_3Vp9", created=ts(14, 12), reporting_category="refund", order_id=r, gross=money(-refund),
                        fee="0.00", net=money(-refund), currency="usd", payout_id="po_A_1009",
                        description="Refund by support: damaged item"))
    gold.append({"ref": r, "category": "refund_unrecorded", "variance": -refund})

    # 3. chargeback with dispute fee
    r, amt, dfee = ref(), 61250, 1500
    books.append([r, SALES_DATE, "Greyfield Tools", "PayRail", "USD", money(amt), money(amt), "", "paid"])
    f = fee(amt, 290, 30)
    payrail.append(dict(id="ch_1Qx4c3", created=ts(8, 40), reporting_category="charge", order_id=r, gross=money(amt),
                        fee=money(f), net=money(amt - f), currency="usd", card_country="US", payout_id="po_A_1009"))
    payrail.append(dict(id="dp_1Qx4", created=ts(16, 2), reporting_category="dispute", order_id=r, gross=money(-amt),
                        fee="0.00", net=money(-amt), currency="usd", payout_id="po_A_1009",
                        description="Dispute: product not received", evidence_due_by="2026-10-23"))
    payrail.append(dict(id="fee_dp_1Qx4", created=ts(16, 2), reporting_category="dispute_fee", order_id=r, gross="0.00",
                        fee=money(dfee), net=money(-dfee), currency="usd", payout_id="po_A_1009", description="Dispute fee"))
    gold.append({"ref": r, "category": "chargeback", "variance": -(amt + dfee)})

    # 4. euro order converted at a different rate
    r = ref()
    eur = 200000
    book_usd = int((Decimal(eur) * Decimal("1.0900")).to_integral_value())
    psp_usd = int((Decimal(eur) * Decimal("1.0815")).to_integral_value())
    books.append([r, SALES_DATE, "Novak Optics", "PayRail", "EUR", money(eur), money(book_usd), "1.0900", "paid"])
    f = fee(psp_usd, 290, 30)
    payrail.append(dict(id="ch_9Lm6d4", created=ts(11, 30), reporting_category="charge", order_id=r, gross=money(psp_usd),
                        fee=money(f), net=money(psp_usd - f), currency="usd", exchange_rate="1.0815", card_country="US",
                        payout_id="po_A_1009", customer_currency="eur", customer_amount=money(eur)))
    gold.append({"ref": r, "category": "fx_difference",
                 "variance": (psp_usd - f) - (book_usd - fee(book_usd, 290, 30))})

    # 5. customer charged twice
    r, amt = ref(), 12900
    books.append([r, SALES_DATE, "Pinecrest Pets", "PayRail", "USD", money(amt), money(amt), "", "paid"])
    f = fee(amt, 290, 30)
    for cid, t in (("ch_2Bn8e5", ts(11, 3)), ("ch_2Bn9e6", ts(11, 3))):
        payrail.append(dict(id=cid, created=t, reporting_category="charge", order_id=r, gross=money(amt), fee=money(f),
                            net=money(amt - f), currency="usd", card_country="US", payout_id="po_A_1009",
                            description="Checkout retry after timeout"))
    gold.append({"ref": r, "category": "duplicate_charge", "variance": amt - f})

    # 6. payment failed but the books say paid
    r, amt = ref(), 47500
    books.append([r, SALES_DATE, "Ridgeway Yoga", "PayRail", "USD", money(amt), money(amt), "", "paid"])
    payrail.append(dict(id="pi_5Tz1f7", created=ts(15, 20), reporting_category="failed_charge", order_id=r,
                        gross=money(amt), fee="0.00", net="0.00", currency="usd", card_country="US",
                        description="Card declined"))
    gold.append({"ref": r, "category": "payment_failed", "variance": -(amt - fee(amt, 290, 30))})

    # 7. wallet payment with no sale in the books
    r, amt = ref(), 9600
    f = fee(amt, 150, 0)
    swift.append(dict(TrxID="SWP774100231", TxnDateTime="09/10/2026 17:41", MerchantRef=r, TxnType="SALE",
                      GrossAmt=money(amt), Commission=money(-f), NetAmt=money(amt - f), Ccy="USD",
                      SettlementBatch="SWB-20261009", ValueDate="", Narration="Wallet payment"))
    gold.append({"ref": r, "category": "unrecorded_sale", "variance": amt - f})

    # -------- payouts
    def to_cents(s: str) -> int:
        return int(Decimal(s) * 100)

    def batch_net(rows, key, batch, net_key):
        return sum(to_cents(x[net_key]) for x in rows if x.get(key) == batch)

    po_a = batch_net(payrail, "payout_id", "po_A_1009", "net")
    po_b = batch_net(payrail, "payout_id", "po_B_1009", "net")
    swb = batch_net(swift, "SettlementBatch", "SWB-20261009", "NetAmt")
    payrail.append(dict(id="po_A_1009", created=f"{SALES_DATE} 18:00:00", reporting_category="payout", gross=money(-po_a),
                        fee="0.00", net=money(-po_a), currency="usd", payout_id="po_A_1009", arrival_date=STATEMENT_DATE,
                        description="PAYOUT A"))
    payrail.append(dict(id="po_B_1009", created=f"{SALES_DATE} 23:30:00", reporting_category="payout", gross=money(-po_b),
                        fee="0.00", net=money(-po_b), currency="usd", payout_id="po_B_1009", arrival_date="2026-10-13",
                        description="PAYOUT B"))
    swift.append(dict(TrxID="SWS000918", TxnDateTime="10/10/2026 06:00", MerchantRef="", TxnType="SETTLEMENT",
                      GrossAmt=money(swb), Commission="0.00", NetAmt=money(swb), Ccy="USD",
                      SettlementBatch="SWB-20261009", ValueDate="2026-10-10", Narration="Daily settlement to bank"))
    gold.append({"ref": "po_B_1009", "category": "payout_in_transit", "variance": -po_b})

    # -------- write files
    with open(OUT / "books.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["invoice_no", "date", "customer", "paid_via", "currency", "amount", "amount_usd", "fx_rate", "status"])
        w.writerows(books)
    pr_cols = ["id", "created", "reporting_category", "order_id", "gross", "fee", "net", "currency", "exchange_rate",
               "customer_currency", "customer_amount", "card_country", "payout_id", "arrival_date", "description",
               "evidence_due_by"]
    with open(OUT / "payrail_balance_2026-10-09.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=pr_cols)
        w.writeheader()
        for row in payrail:
            w.writerow({k: row.get(k, "") for k in pr_cols})
    sw_cols = ["TrxID", "TxnDateTime", "MerchantRef", "TxnType", "GrossAmt", "Commission", "NetAmt", "Ccy",
               "SettlementBatch", "ValueDate", "Narration"]
    with open(OUT / "swiftpay_settlement_09102026.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=sw_cols)
        w.writeheader()
        for row in swift:
            w.writerow({k: row.get(k, "") for k in sw_cols})

    opening = 1840000
    supplier = 320000
    closing = opening + po_a + swb - supplier
    (OUT / "bank_statement_camt053.xml").write_text(camt053(opening, closing, po_a, swb, supplier))

    with open(OUT / "history.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["week_start", "card_net", "wallet_net", "charges", "disputes"])
        weeks = ["2026-07-20", "2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24",
                 "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05"]
        for i, wk in enumerate(weeks):
            card = 3900000 + rnd.randint(-250000, 250000) + i * 12000
            wallet = 780000 + rnd.randint(-60000, 60000)
            charges = 690 + rnd.randint(-40, 40)
            w.writerow([wk, money(card), money(wallet), charges, rnd.randint(2, 5)])

    with open(OUT / "receivables.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["invoice", "customer", "amount", "issued", "due", "status"])
        w.writerows([
            ["INV-2207", "Harbor Hotels Group", "18000.00", "2026-09-10", "2026-11-10", "open"],
            ["INV-2211", "Linden Office Supplies", "14000.00", "2026-09-20", "2026-11-20", "open"],
            ["INV-2216", "Mesa Events Co", "9500.00", "2026-10-05", "2026-12-05", "open"],
            ["INV-2198", "Fenwick Books Ltd", "2400.00", "2026-08-30", "2026-09-30", "open"],
            ["INV-2190", "Okafor Studio", "3100.00", "2026-08-15", "2026-09-15", "paid"],
        ])

    profile = {
        "business": {"name": "Northwind Market", "country": "Fictional", "months_trading": 18,
                     "reporting_currency": "USD", "sector": "Online retail and wholesale"},
        "sales_date": SALES_DATE,
        "files": {"books": "books.csv",
                  "processors": ["payrail_balance_2026-10-09.csv", "swiftpay_settlement_09102026.csv"],
                  "bank": "bank_statement_camt053.xml", "history": "history.csv", "receivables": "receivables.csv"},
        "fee_schedules": {
            "PayRail": {"home_country": "US", "domestic": {"bp": 290, "fixed": 30}, "international": {"bp": 440, "fixed": 30}},
            "Swiftpay": {"domestic": {"bp": 150, "fixed": 0}},
        },
        "cash": {
            "forecast_start": "2026-10-12", "weeks": 13, "floor": 1500000,
            "payments": [
                {"name": "Payroll", "amount": 3800000, "every_days": 14, "first": "2026-10-23"},
                {"name": "Supplier payments", "amount": 2000000, "every_days": 7, "first": "2026-10-14"},
                {"name": "Operating costs", "amount": 400000, "every_days": 7, "first": "2026-10-15"},
                {"name": "Rent", "amount": 950000, "monthly_day": 1},
                {"name": "Holiday stock order", "amount": 5200000, "once": "2026-10-27"},
            ],
        },
    }
    (OUT / "profile.json").write_text(json.dumps(profile, indent=2))
    (OUT / "gold.json").write_text(json.dumps({"gap": None, "cases": gold}, indent=2))
    print(f"wrote {OUT}: {len(books)} book rows, {len(payrail)} PayRail rows, {len(swift)} Swiftpay rows, "
          f"{len(gold)} seeded cases")


def camt053(opening: int, closing: int, po_a: int, swb: int, supplier: int) -> str:
    def amt(c):
        return money(c)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>FMB-STMT-20261010</MsgId><CreDtTm>{STATEMENT_DATE}T07:00:00</CreDtTm></GrpHdr>
    <Stmt>
      <Id>STMT-0412</Id>
      <CreDtTm>{STATEMENT_DATE}T07:00:00</CreDtTm>
      <Acct><Id><Othr><Id>FMB-0412-778899</Id></Othr></Id><Ccy>USD</Ccy><Svcr><FinInstnId><Nm>First Meridian Bank</Nm></FinInstnId></Svcr></Acct>
      <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="USD">{amt(opening)}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>{STATEMENT_DATE}</Dt></Dt></Bal>
      <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="USD">{amt(closing)}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>{STATEMENT_DATE}</Dt></Dt></Bal>
      <Ntry>
        <NtryRef>FMB0412-01</NtryRef><Amt Ccy="USD">{amt(po_a)}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>{STATEMENT_DATE}</Dt></BookgDt><ValDt><Dt>{STATEMENT_DATE}</Dt></ValDt><AcctSvcrRef>FMB0412-01</AcctSvcrRef>
        <NtryDtls><TxDtls><Refs><EndToEndId>po_A_1009</EndToEndId></Refs><RmtInf><Ustrd>PAYRAIL PAYOUT po_A_1009</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <NtryRef>FMB0412-02</NtryRef><Amt Ccy="USD">{amt(swb)}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>{STATEMENT_DATE}</Dt></BookgDt><ValDt><Dt>{STATEMENT_DATE}</Dt></ValDt><AcctSvcrRef>FMB0412-02</AcctSvcrRef>
        <NtryDtls><TxDtls><Refs><EndToEndId>SWB-20261009</EndToEndId></Refs><RmtInf><Ustrd>SWIFTPAY SETTLEMENT SWB-20261009</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <NtryRef>FMB0412-03</NtryRef><Amt Ccy="USD">{amt(supplier)}</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>{STATEMENT_DATE}</Dt></BookgDt><ValDt><Dt>{STATEMENT_DATE}</Dt></ValDt><AcctSvcrRef>FMB0412-03</AcctSvcrRef>
        <NtryDtls><TxDtls><RmtInf><Ustrd>Harbor Print supplies invoice 5521</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


if __name__ == "__main__":
    main()
