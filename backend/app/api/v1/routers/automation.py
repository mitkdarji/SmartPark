"""Automation rule management."""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OwnerUser
from app.core.errors import Conflict, NotFound, PermissionDenied
from app.models.automation import AutomationRule, AutomationRun
from app.models.enums import UserRole
from app.models.facility import Facility
from app.schemas.ai import AutomationRuleIn, AutomationRuleOut, AutomationRunOut
from app.services.automation.engine import ACTION_REGISTRY, TRIGGERS, automation_engine

router = APIRouter(prefix="/automation", tags=["automation"])


async def _assert_owns(db, user, facility_id: int | None) -> None:
    if facility_id is None:
        if user.role != UserRole.ADMIN:
            raise PermissionDenied("Only administrators can create global rules.")
        return
    facility = await db.get(Facility, facility_id)
    if facility is None:
        raise NotFound(f"Facility {facility_id} was not found.")
    if facility.owner_id != user.id and user.role != UserRole.ADMIN:
        raise PermissionDenied("You do not operate this facility.")


def _validate(payload: AutomationRuleIn) -> None:
    if payload.trigger not in TRIGGERS:
        raise Conflict(
            f"Unknown trigger '{payload.trigger}'. Valid triggers: {', '.join(TRIGGERS)}"
        )
    unknown = [
        a.get("action") for a in payload.actions if a.get("action") not in ACTION_REGISTRY
    ]
    if unknown:
        raise Conflict(
            f"Unknown actions: {', '.join(str(u) for u in unknown)}. "
            f"Valid actions: {', '.join(sorted(ACTION_REGISTRY))}"
        )
    if not payload.actions:
        raise Conflict("A rule must have at least one action.")


@router.get("/catalog", response_model=dict)
async def catalog() -> dict:
    """Triggers and actions a rule may reference — also the AI copilot's vocabulary."""
    return {
        "triggers": TRIGGERS,
        "actions": sorted(ACTION_REGISTRY),
        "condition_operators": [
            "<field>", "<field>_eq", "<field>_gte", "<field>_lte", "hour", "weekday",
        ],
        "note": (
            "Conditions are matched against the triggering event's payload. "
            "`hour` and `weekday` are evaluated against the clock instead."
        ),
    }


@router.post("/rules", response_model=AutomationRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: AutomationRuleIn, user: OwnerUser, db: DbSession,
    created_by_ai: bool = Query(default=False),
) -> AutomationRuleOut:
    await _assert_owns(db, user, payload.facility_id)
    _validate(payload)

    rule = AutomationRule(created_by_ai=created_by_ai, **payload.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return AutomationRuleOut.model_validate(rule)


@router.get("/rules", response_model=list[AutomationRuleOut])
async def list_rules(
    user: CurrentUser, db: DbSession, facility_id: int | None = Query(default=None)
) -> list[AutomationRuleOut]:
    query = select(AutomationRule)
    if facility_id is not None:
        await _assert_owns(db, user, facility_id)
        query = query.where(AutomationRule.facility_id == facility_id)
    elif user.role != UserRole.ADMIN:
        owned = (
            await db.execute(select(Facility.id).where(Facility.owner_id == user.id))
        ).scalars().all()
        query = query.where(AutomationRule.facility_id.in_(owned))

    rows = (
        await db.execute(query.order_by(AutomationRule.priority, AutomationRule.id))
    ).scalars().all()
    return [AutomationRuleOut.model_validate(row) for row in rows]


@router.patch("/rules/{rule_id}", response_model=AutomationRuleOut)
async def update_rule(
    rule_id: int, payload: AutomationRuleIn, user: OwnerUser, db: DbSession
) -> AutomationRuleOut:
    rule = await db.get(AutomationRule, rule_id)
    if rule is None:
        raise NotFound(f"Rule {rule_id} was not found.")
    await _assert_owns(db, user, rule.facility_id)
    _validate(payload)

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
    await db.commit()
    await db.refresh(rule)
    return AutomationRuleOut.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_rule(rule_id: int, user: OwnerUser, db: DbSession) -> None:
    rule = await db.get(AutomationRule, rule_id)
    if rule is None:
        raise NotFound(f"Rule {rule_id} was not found.")
    await _assert_owns(db, user, rule.facility_id)
    await db.delete(rule)
    await db.commit()


@router.get("/runs", response_model=list[AutomationRunOut])
async def list_runs(
    user: CurrentUser,
    db: DbSession,
    facility_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AutomationRunOut]:
    query = select(AutomationRun)
    if facility_id is not None:
        await _assert_owns(db, user, facility_id)
        query = query.where(AutomationRun.facility_id == facility_id)
    rows = (
        await db.execute(query.order_by(AutomationRun.id.desc()).limit(limit))
    ).scalars().all()
    return [AutomationRunOut.model_validate(row) for row in rows]


@router.post("/tick", response_model=dict)
async def trigger_tick(user: OwnerUser) -> dict:
    """Run the scheduled pass now — used for demos and for testing a new rule."""
    return await automation_engine.tick()
