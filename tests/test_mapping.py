"""Column mapping: an unknown provider file becomes readable without code."""
import pytest

from mismatchdesk.ingest import UnknownFormat, load_processor
from mismatchdesk.mapping import build_adapter, kind_suggestions, save_adapter, suggest

WALLET = ("Trx ID,Date,Merchant Invoice,Trx Type,Amount,Charge,Settled,Batch,Remarks\n"
          "W001,25/10/2026 10:15,INV-77,Payment,500.00,-7.50,492.50,B-01,Wallet payment\n"
          "W002,25/10/2026 11:40,INV-78,Payment Reversal,-120.00,0.00,-120.00,B-01,Customer cancelled\n"
          "W003,26/10/2026 06:00,,Settlement,372.50,0.00,372.50,B-01,To bank\n").encode()


def test_unknown_file_raises_with_header_and_sample():
    with pytest.raises(UnknownFormat) as info:
        load_processor(("regional_wallet.csv", WALLET))
    err = info.value
    assert err.role == "processor" and "Trx Type" in err.header and len(err.sample) == 3


def test_suggestion_finds_columns_fee_sign_and_date_order():
    s = suggest(["Trx ID", "Date", "Merchant Invoice", "Trx Type", "Amount", "Charge", "Settled", "Batch", "Remarks"],
                "processor",
                [{"Charge": "-7.50", "Date": "25/10/2026 10:15"}, {"Charge": "0.00", "Date": "25/10/2026 11:40"}])
    m = s["mapping"]
    assert m["txn_id"] == "Trx ID" and m["ts"] == "Date" and m["kind"] == "Trx Type"
    assert m["ref"] == "Merchant Invoice" and m["gross"] == "Amount" and m["fee"] == "Charge"
    assert m["payout_id"] == "Batch" and m["memo"] == "Remarks"
    assert s["options"]["fee_sign"] == "negative" and s["options"]["day_first"] is True


def test_kind_suggestions():
    assert kind_suggestions(["Payment", "Payment Reversal", "Settlement", "Chargeback", "Declined"]) == {
        "Chargeback": "dispute", "Declined": "failed", "Payment": "charge",
        "Payment Reversal": "refund", "Settlement": "payout"}


def test_saved_adapter_reads_the_next_file_automatically():
    mapping = {"txn_id": "Trx ID", "ts": "Date", "ref": "Merchant Invoice", "kind": "Trx Type",
               "gross": "Amount", "fee": "Charge", "net": "Settled", "payout_id": "Batch", "memo": "Remarks"}
    cfg = build_adapter(name="Regional wallet test", role="processor", mapping=mapping, provider="RegionalWallet",
                        fee_sign="negative", day_first=True,
                        kind_map=kind_suggestions(["Payment", "Payment Reversal", "Settlement"]))
    save_adapter(cfg)
    txns, used = load_processor(("next_week.csv", WALLET))
    assert used["provider"] == "RegionalWallet"
    sale = next(t for t in txns if t.txn_id == "W001")
    assert sale.kind == "charge" and sale.fee == 750 and sale.net == 49250 and sale.ts.startswith("2026-10-25")
    assert next(t for t in txns if t.txn_id == "W002").kind == "refund"
    payout = next(t for t in txns if t.kind == "payout")
    assert payout.net == -37250 and payout.payout_id == "B-01"


def test_mapping_requires_essential_columns():
    with pytest.raises(ValueError, match="Order or invoice reference"):
        build_adapter(name="x", role="processor", mapping={"gross": "Amount"})
