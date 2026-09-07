"""Wallet ledger operations.

Three properties matter more than anything else here:

  atomic     balances move with a conditional UPDATE guarded by the balance
             itself, so two concurrent debits can never overdraw. The read that
             informed the decision is never trusted.
  idempotent every mutation takes an idempotency key. A gate controller that
             retries after a network timeout settles the bill once, not twice.
  auditable  the balance is derived state; the transaction rows are the truth,
             and `reconcile` proves the two still agree.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientFunds, NotFound
from app.core.events import Topic, bus
from app.core.logging import get_logger
from app.models.enums import TxnType
from app.models.user import Wallet, WalletTransaction

log = get_logger(__name__)


class WalletService:
    async def get_or_create(self, db: AsyncSession, user_id: int) -> Wallet:
        wallet = (
            await db.execute(select(Wallet).where(Wallet.user_id == user_id))
        ).scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=user_id, balance_minor=0)
            db.add(wallet)
            await db.flush()
        return wallet

    async def _existing(self, db: AsyncSession, key: str | None) -> WalletTransaction | None:
        if not key:
            return None
        return (
            await db.execute(
                select(WalletTransaction).where(WalletTransaction.idempotency_key == key)
            )
        ).scalar_one_or_none()

    # ── Mutations ─────────────────────────────────────────────

    async def credit(
        self,
        db: AsyncSession,
        wallet: Wallet,
        amount_minor: int,
        *,
        description: str = "Wallet top-up",
        reference: str | None = None,
        provider: str = "wallet",
        idempotency_key: str | None = None,
        session_id: int | None = None,
        meta: dict | None = None,
    ) -> WalletTransaction:
        if amount_minor <= 0:
            raise ValueError("credit amount must be positive")

        existing = await self._existing(db, idempotency_key)
        if existing is not None:
            return existing

        await db.execute(
            update(Wallet)
            .where(Wallet.id == wallet.id)
            .values(balance_minor=Wallet.balance_minor + amount_minor)
        )
        await db.refresh(wallet)

        txn = WalletTransaction(
            wallet_id=wallet.id,
            session_id=session_id,
            txn_type=TxnType.CREDIT,
            amount_minor=amount_minor,
            balance_after_minor=wallet.balance_minor,
            currency=wallet.currency,
            description=description,
            reference=reference or f"CR-{uuid.uuid4().hex[:10].upper()}",
            provider=provider,
            idempotency_key=idempotency_key,
            meta=meta or {},
        )
        db.add(txn)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent retry with the same key won the race — reuse its row.
            await db.rollback()
            replayed = await self._existing(db, idempotency_key)
            if replayed is not None:
                return replayed
            raise

        await bus.emit(
            Topic.WALLET_CREDITED,
            {
                "wallet_id": wallet.id, "amount_minor": amount_minor,
                "balance_minor": wallet.balance_minor, "description": description,
            },
            user_id=wallet.user_id,
        )
        return txn

    async def debit(
        self,
        db: AsyncSession,
        wallet: Wallet,
        amount_minor: int,
        *,
        description: str = "Parking charge",
        reference: str | None = None,
        idempotency_key: str | None = None,
        session_id: int | None = None,
        allow_auto_reload: bool = True,
        meta: dict | None = None,
    ) -> WalletTransaction:
        if amount_minor <= 0:
            raise ValueError("debit amount must be positive")

        existing = await self._existing(db, idempotency_key)
        if existing is not None:
            return existing

        await db.refresh(wallet)

        if wallet.available_minor < amount_minor and allow_auto_reload:
            await self._try_auto_reload(db, wallet, amount_minor, session_id=session_id)
            await db.refresh(wallet)

        # Conditional update: the guard is evaluated inside the database, so a
        # concurrent debit cannot slip between our check and our write.
        result = await db.execute(
            update(Wallet)
            .where(
                Wallet.id == wallet.id,
                (Wallet.balance_minor - Wallet.held_minor) >= amount_minor,
            )
            .values(balance_minor=Wallet.balance_minor - amount_minor)
        )
        if not result.rowcount:
            await bus.emit(
                Topic.PAYMENT_FAILED,
                {
                    "wallet_id": wallet.id, "amount_minor": amount_minor,
                    "available_minor": wallet.available_minor, "session_id": session_id,
                },
                user_id=wallet.user_id,
            )
            raise InsufficientFunds(
                "Wallet balance is too low to settle this parking charge.",
                details={
                    "required_minor": amount_minor,
                    "available_minor": wallet.available_minor,
                    "shortfall_minor": amount_minor - wallet.available_minor,
                    "currency": wallet.currency,
                },
            )

        await db.refresh(wallet)
        txn = WalletTransaction(
            wallet_id=wallet.id,
            session_id=session_id,
            txn_type=TxnType.DEBIT,
            amount_minor=amount_minor,
            balance_after_minor=wallet.balance_minor,
            currency=wallet.currency,
            description=description,
            reference=reference or f"DR-{uuid.uuid4().hex[:10].upper()}",
            idempotency_key=idempotency_key,
            meta=meta or {},
        )
        db.add(txn)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            replayed = await self._existing(db, idempotency_key)
            if replayed is not None:
                return replayed
            raise

        await bus.emit(
            Topic.WALLET_DEBITED,
            {
                "wallet_id": wallet.id, "amount_minor": amount_minor,
                "balance_minor": wallet.balance_minor, "description": description,
                "session_id": session_id,
            },
            user_id=wallet.user_id,
        )
        log.info(
            "wallet debited",
            extra={"wallet_id": wallet.id, "amount_minor": amount_minor, "session_id": session_id},
        )
        return txn

    async def refund(
        self, db: AsyncSession, wallet: Wallet, amount_minor: int, *,
        description: str = "Refund", session_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> WalletTransaction:
        txn = await self.credit(
            db, wallet, amount_minor, description=description,
            session_id=session_id, idempotency_key=idempotency_key,
            reference=f"RF-{uuid.uuid4().hex[:10].upper()}",
        )
        txn.txn_type = TxnType.REFUND
        await db.flush()
        return txn

    async def hold(self, db: AsyncSession, wallet: Wallet, amount_minor: int) -> bool:
        """Ring-fence funds at entry so a long stay cannot end unfunded."""
        result = await db.execute(
            update(Wallet)
            .where(
                Wallet.id == wallet.id,
                (Wallet.balance_minor - Wallet.held_minor) >= amount_minor,
            )
            .values(held_minor=Wallet.held_minor + amount_minor)
        )
        await db.refresh(wallet)
        return bool(result.rowcount)

    async def release_hold(self, db: AsyncSession, wallet: Wallet, amount_minor: int) -> None:
        await db.execute(
            update(Wallet)
            .where(Wallet.id == wallet.id)
            .values(held_minor=func.max(0, Wallet.held_minor - amount_minor))
        )
        await db.refresh(wallet)

    # ── Support ───────────────────────────────────────────────

    async def _try_auto_reload(
        self, db: AsyncSession, wallet: Wallet, needed_minor: int, *, session_id: int | None
    ) -> None:
        if not wallet.auto_reload_enabled:
            return
        if wallet.available_minor > wallet.auto_reload_threshold_minor:
            return

        from app.services.integrations.payments import payment_gateway

        # Top up by at least the shortfall, rounded up to the configured step.
        shortfall = max(0, needed_minor - wallet.available_minor)
        step = max(wallet.auto_reload_amount_minor, 1)
        amount = max(step, ((shortfall // step) + 1) * step)

        charge = await payment_gateway.charge(
            amount_minor=amount,
            currency=wallet.currency,
            customer_id=wallet.provider_customer_id or f"user-{wallet.user_id}",
            description="SmartPark wallet auto-reload",
        )
        if not charge.get("success"):
            log.warning("auto-reload declined", extra={"wallet_id": wallet.id})
            return

        await self.credit(
            db, wallet, amount,
            description="Automatic wallet top-up",
            reference=charge.get("reference"),
            provider=charge.get("provider", "gateway"),
            idempotency_key=f"autoreload:{wallet.id}:{charge.get('reference')}",
            session_id=session_id,
            meta={"auto": True, "trigger_session": session_id},
        )
        log.info("wallet auto-reloaded", extra={"wallet_id": wallet.id, "amount_minor": amount})

    async def history(
        self, db: AsyncSession, wallet_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[WalletTransaction]:
        return list(
            (
                await db.execute(
                    select(WalletTransaction)
                    .where(WalletTransaction.wallet_id == wallet_id)
                    .order_by(WalletTransaction.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()
        )

    async def reconcile(self, db: AsyncSession, wallet_id: int) -> dict:
        """Recompute the balance from the ledger and report any drift.

        Run nightly by the automation engine. A non-zero drift means a bug, and
        it should be found by this check rather than by a customer.
        """
        wallet = await db.get(Wallet, wallet_id)
        if wallet is None:
            raise NotFound(f"wallet {wallet_id} not found")

        rows = (
            await db.execute(
                select(WalletTransaction.txn_type, func.sum(WalletTransaction.amount_minor))
                .where(WalletTransaction.wallet_id == wallet_id)
                .group_by(WalletTransaction.txn_type)
            )
        ).all()

        totals = {txn_type: int(total or 0) for txn_type, total in rows}
        credits = totals.get(TxnType.CREDIT, 0) + totals.get(TxnType.REFUND, 0)
        debits = totals.get(TxnType.DEBIT, 0)
        derived = credits - debits

        return {
            "wallet_id": wallet_id,
            "stored_balance_minor": wallet.balance_minor,
            "derived_balance_minor": derived,
            "drift_minor": wallet.balance_minor - derived,
            "balanced": wallet.balance_minor == derived,
            "credits_minor": credits,
            "debits_minor": debits,
            "checked_at": datetime.now(UTC).isoformat(),
        }


wallet_service = WalletService()
