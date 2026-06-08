"""Regression tests for PaperWallet.credit() idempotency.

The bug: if the same exit order fires a fill callback twice (race condition or
retry), wallet.credit() was called twice → balance over-credited by the same
amount. The fix adds a paper_wallet_credits collection with a unique index on
order_id, mirroring the processed_fill_ids guard in portfolio_ledger.py.
"""
import os
import sys
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo.errors import DuplicateKeyError
from core.paper_broker import PaperWallet, PAPER_INITIAL_BALANCE


# ── Mock helpers ───────────────────────────────────────────────────────────────

def _make_db(initial_balance: float = PAPER_INITIAL_BALANCE):
    mock_db = MagicMock()
    mock_db.paper_wallets.find_one = AsyncMock(return_value={
        "user_id": "user-1",
        "balance": initial_balance,
        "initial_balance": PAPER_INITIAL_BALANCE,
        "total_debited": 0.0,
        "total_credited": 0.0,
    })
    mock_db.paper_wallets.update_one = AsyncMock()
    mock_db.paper_wallet_credits.insert_one = AsyncMock()
    return mock_db


# ── Idempotency tests ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_duplicate_credit_same_order_id_credited_once():
    """Calling credit() twice with the same order_id must credit the wallet only once.

    The second call must detect the DuplicateKeyError from paper_wallet_credits
    and return without touching paper_wallets.
    """
    db = _make_db()
    # First insert succeeds; second raises DuplicateKeyError (unique index hit)
    db.paper_wallet_credits.insert_one = AsyncMock(
        side_effect=[None, DuplicateKeyError("duplicate order_id")]
    )

    wallet = PaperWallet(db)
    await wallet.credit("user-1", 5000.0, "order-exit-001")
    await wallet.credit("user-1", 5000.0, "order-exit-001")  # duplicate

    assert db.paper_wallets.update_one.call_count == 1, (
        "Wallet balance must only be updated once even if credit() is called twice "
        "with the same order_id"
    )


@pytest.mark.anyio
async def test_distinct_order_ids_both_credited():
    """Two different exit orders must each credit the wallet independently."""
    db = _make_db()
    db.paper_wallet_credits.insert_one = AsyncMock(return_value=None)

    wallet = PaperWallet(db)
    await wallet.credit("user-1", 5000.0, "order-exit-001")
    await wallet.credit("user-1", 3000.0, "order-exit-002")

    assert db.paper_wallets.update_one.call_count == 2


@pytest.mark.anyio
async def test_duplicate_credit_logs_warning(caplog):
    """The duplicate skip must log a warning so operators can spot it."""
    import logging
    db = _make_db()
    db.paper_wallet_credits.insert_one = AsyncMock(
        side_effect=[None, DuplicateKeyError("duplicate order_id")]
    )

    wallet = PaperWallet(db)
    with caplog.at_level(logging.WARNING, logger="quantg.paper_broker"):
        await wallet.credit("user-1", 5000.0, "order-dup-log")
        await wallet.credit("user-1", 5000.0, "order-dup-log")

    assert any("duplicate" in r.message.lower() or "already applied" in r.message.lower()
               for r in caplog.records), "Expected a warning log for the skipped duplicate credit"


@pytest.mark.anyio
async def test_credit_idempotency_record_stored_with_correct_fields():
    """The paper_wallet_credits record must include order_id, user_id, and amount."""
    db = _make_db()
    db.paper_wallet_credits.insert_one = AsyncMock(return_value=None)

    wallet = PaperWallet(db)
    await wallet.credit("user-1", 7500.0, "order-abc")

    db.paper_wallet_credits.insert_one.assert_called_once()
    record = db.paper_wallet_credits.insert_one.call_args.args[0]
    assert record["order_id"] == "order-abc"
    assert record["user_id"] == "user-1"
    assert record["amount"] == 7500.0
    assert "credited_at" in record


@pytest.mark.anyio
async def test_zero_amount_credit_skips_entirely():
    """A zero-amount credit must be skipped without touching the DB at all."""
    db = _make_db()

    wallet = PaperWallet(db)
    await wallet.credit("user-1", 0.0, "order-zero")

    db.paper_wallet_credits.insert_one.assert_not_called()
    db.paper_wallets.update_one.assert_not_called()


@pytest.mark.anyio
async def test_credit_without_order_id_still_applies():
    """A credit with no order_id (edge case) must still update the wallet balance
    since there is no idempotency key to deduplicate on."""
    db = _make_db()

    wallet = PaperWallet(db)
    await wallet.credit("user-1", 2000.0, "")  # empty string → falsy

    db.paper_wallet_credits.insert_one.assert_not_called()
    db.paper_wallets.update_one.assert_called_once()


# ── Debit is unchanged — quick sanity check ────────────────────────────────────

@pytest.mark.anyio
async def test_debit_returns_true_when_sufficient_balance():
    db = _make_db(initial_balance=500_000.0)
    wallet = PaperWallet(db)
    result = await wallet.debit("user-1", 10_000.0, "order-buy-001")
    assert result is True
    db.paper_wallets.update_one.assert_called_once()


@pytest.mark.anyio
async def test_debit_returns_false_when_insufficient_balance():
    db = _make_db(initial_balance=5_000.0)
    wallet = PaperWallet(db)
    result = await wallet.debit("user-1", 10_000.0, "order-buy-002")
    assert result is False
    db.paper_wallets.update_one.assert_not_called()
