"""Wallet: balance, top-ups, ledger, auto-reload, reconciliation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.schemas.auth import WalletOut
from app.schemas.parking import AutoReloadUpdate, TopUpRequest, WalletTransactionOut
from app.services.billing.wallet import wallet_service
from app.services.integrations.payments import payment_gateway

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=WalletOut)
async def get_wallet(user: CurrentUser, db: DbSession) -> WalletOut:
    wallet = await wallet_service.get_or_create(db, user.id)
    await db.commit()
    return WalletOut.model_validate(wallet)


@router.post("/topup", response_model=WalletOut)
async def top_up(payload: TopUpRequest, user: CurrentUser, db: DbSession) -> WalletOut:
    """Add funds. The gateway charge happens first; the credit only follows success."""
    wallet = await wallet_service.get_or_create(db, user.id)
    reference = payload.reference or uuid.uuid4().hex[:16]

    charge = await payment_gateway.charge(
        amount_minor=payload.amount_minor,
        currency=wallet.currency,
        customer_id=wallet.provider_customer_id or f"user-{user.id}",
        description="SmartPark wallet top-up",
    )
    if not charge.get("success"):
        from app.core.errors import SmartParkError

        raise SmartParkError(
            f"The payment could not be completed ({charge.get('error', 'unknown error')}).",
            details=charge,
        )

    await wallet_service.credit(
        db, wallet, payload.amount_minor,
        description="Wallet top-up",
        reference=charge.get("reference"),
        provider=charge.get("provider", "gateway"),
        idempotency_key=f"topup:{user.id}:{reference}",
    )
    await db.commit()
    await db.refresh(wallet)
    return WalletOut.model_validate(wallet)


@router.get("/transactions", response_model=list[WalletTransactionOut])
async def transactions(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[WalletTransactionOut]:
    wallet = await wallet_service.get_or_create(db, user.id)
    await db.commit()
    rows = await wallet_service.history(db, wallet.id, limit=limit, offset=offset)
    return [WalletTransactionOut.model_validate(row) for row in rows]


@router.put("/auto-reload", response_model=WalletOut)
async def set_auto_reload(
    payload: AutoReloadUpdate, user: CurrentUser, db: DbSession
) -> WalletOut:
    wallet = await wallet_service.get_or_create(db, user.id)
    wallet.auto_reload_enabled = payload.enabled
    wallet.auto_reload_threshold_minor = payload.threshold_minor
    wallet.auto_reload_amount_minor = payload.amount_minor
    await db.commit()
    await db.refresh(wallet)
    return WalletOut.model_validate(wallet)


@router.get("/reconcile", response_model=dict)
async def reconcile(user: CurrentUser, db: DbSession) -> dict:
    """Recompute the balance from the ledger and report any drift."""
    wallet = await wallet_service.get_or_create(db, user.id)
    await db.commit()
    return await wallet_service.reconcile(db, wallet.id)


@router.get("/reconcile/all", response_model=dict)
async def reconcile_all(admin: AdminUser, db: DbSession) -> dict:
    from sqlalchemy import select

    from app.models.user import Wallet

    wallet_ids = (await db.execute(select(Wallet.id))).scalars().all()
    reports = [await wallet_service.reconcile(db, wid) for wid in wallet_ids]
    drifted = [r for r in reports if not r["balanced"]]
    return {
        "wallets_checked": len(reports),
        "drifted": len(drifted),
        "details": drifted,
        "healthy": not drifted,
    }
