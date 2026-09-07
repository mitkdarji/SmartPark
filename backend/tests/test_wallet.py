"""Wallet ledger: atomicity, idempotency, and reconciliation."""

from __future__ import annotations

import pytest

from app.core.errors import InsufficientFunds
from app.core.security import hash_password
from app.models.enums import TxnType
from app.models.user import User, Wallet
from app.services.billing.wallet import wallet_service


async def make_user(db, email="w@test.dev", balance=100_00) -> tuple[User, Wallet]:
    user = User(email=email, password_hash=hash_password("x" * 12), full_name="W")
    db.add(user)
    await db.flush()
    wallet = Wallet(user_id=user.id, balance_minor=balance)
    db.add(wallet)
    await db.flush()
    return user, wallet


async def test_credit_increases_the_balance_and_records_the_ledger(db):
    _, wallet = await make_user(db, balance=0)
    txn = await wallet_service.credit(db, wallet, 50_00, description="top-up")
    assert wallet.balance_minor == 50_00
    assert txn.txn_type == TxnType.CREDIT
    assert txn.balance_after_minor == 50_00


async def test_debit_reduces_the_balance(db):
    _, wallet = await make_user(db, balance=100_00)
    await wallet_service.debit(db, wallet, 30_00, description="parking")
    assert wallet.balance_minor == 70_00


async def test_debit_beyond_the_balance_is_refused(db):
    _, wallet = await make_user(db, balance=10_00)
    with pytest.raises(InsufficientFunds) as exc:
        await wallet_service.debit(db, wallet, 50_00, allow_auto_reload=False)
    assert exc.value.details["shortfall_minor"] == 40_00
    await db.refresh(wallet)
    assert wallet.balance_minor == 10_00      # unchanged


async def test_idempotency_key_prevents_a_double_charge(db):
    """A gate controller retrying after a timeout must not bill twice."""
    _, wallet = await make_user(db, balance=100_00)
    key = "session-exit:42"

    first = await wallet_service.debit(db, wallet, 25_00, idempotency_key=key)
    second = await wallet_service.debit(db, wallet, 25_00, idempotency_key=key)

    assert first.id == second.id
    await db.refresh(wallet)
    assert wallet.balance_minor == 75_00       # charged once, not twice


async def test_held_funds_are_not_spendable(db):
    _, wallet = await make_user(db, balance=100_00)
    assert await wallet_service.hold(db, wallet, 80_00)
    assert wallet.available_minor == 20_00

    with pytest.raises(InsufficientFunds):
        await wallet_service.debit(db, wallet, 50_00, allow_auto_reload=False)

    await wallet_service.release_hold(db, wallet, 80_00)
    await wallet_service.debit(db, wallet, 50_00, allow_auto_reload=False)
    assert wallet.balance_minor == 50_00


async def test_hold_beyond_the_balance_is_refused(db):
    _, wallet = await make_user(db, balance=10_00)
    assert not await wallet_service.hold(db, wallet, 90_00)


async def test_reconciliation_agrees_with_the_ledger(db):
    _, wallet = await make_user(db, balance=0)
    await wallet_service.credit(db, wallet, 200_00)
    await wallet_service.debit(db, wallet, 45_00)
    await wallet_service.credit(db, wallet, 30_00)

    report = await wallet_service.reconcile(db, wallet.id)
    assert report["balanced"]
    assert report["drift_minor"] == 0
    assert report["derived_balance_minor"] == 185_00


async def test_reconciliation_detects_injected_drift(db):
    """The check must actually catch a corrupted balance, not just always pass."""
    _, wallet = await make_user(db, balance=0)
    await wallet_service.credit(db, wallet, 100_00)

    wallet.balance_minor += 5_00       # simulate a bug writing the balance directly
    await db.flush()

    report = await wallet_service.reconcile(db, wallet.id)
    assert not report["balanced"]
    assert report["drift_minor"] == 5_00


async def test_a_stale_read_cannot_overdraw_the_wallet(tmp_path):
    """The lost-update scenario the conditional UPDATE exists to prevent.

    Two requests both read a balance of 100. One spends 60 and commits. The
    other still holds its stale 100 in memory and tries to spend 60 too. If the
    guard were a Python `if wallet.balance >= amount` it would pass and the
    wallet would go to -20. Because the guard is a predicate inside the UPDATE,
    the database re-evaluates it against the committed 40 and refuses.

    Run on a real file-backed database so the two sessions genuinely hold
    separate connections and transactions.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.base import Base

    url = f"sqlite+aiosqlite:///{tmp_path / 'concurrency.db'}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with factory() as setup:
        _, wallet = await make_user(setup, email="race@test.dev", balance=100_00)
        await setup.commit()
        wallet_id = wallet.id

    async with factory() as session_a, factory() as session_b:
        stale = await session_a.get(Wallet, wallet_id)   # A reads 100.00
        fresh = await session_b.get(Wallet, wallet_id)

        await wallet_service.debit(session_b, fresh, 60_00, allow_auto_reload=False)
        await session_b.commit()                         # B commits; balance is now 40.00

        assert stale.balance_minor == 100_00             # A's copy is stale
        with pytest.raises(InsufficientFunds):
            await wallet_service.debit(session_a, stale, 60_00, allow_auto_reload=False)
        await session_a.rollback()

    async with factory() as check:
        final = await check.get(Wallet, wallet_id)
        assert final.balance_minor == 40_00
        assert final.balance_minor >= 0

    await engine.dispose()


async def test_zero_or_negative_amounts_are_rejected(db):
    _, wallet = await make_user(db)
    with pytest.raises(ValueError):
        await wallet_service.credit(db, wallet, 0)
    with pytest.raises(ValueError):
        await wallet_service.debit(db, wallet, -100)
