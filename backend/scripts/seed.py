"""Seed a realistic demo environment.

Creates two facilities that exercise different halves of the platform: a public
shopping mall (dynamic pricing, guest vehicles, hybrid allocation) and a private
corporate campus (authorised-vehicle allow-list, reserved bays, waived charges).

Then it back-fills three weeks of traffic. That matters more than it sounds: the
demand forecaster needs 120+ observations before it will train at all, and the
anomaly detector needs 60+ completed sessions. Without history the platform
honestly reports that it is running on baselines — which is correct, but makes
for a poor demonstration of the parts that do learn.

    python -m scripts.seed --reset
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select

from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import SessionLocal, engine, init_db
from app.models.automation import (
    AgentConversation,
    AgentMessage,
    AutomationRule,
    AutomationRun,
)
from app.models.enums import (
    AccessMode,
    AllocationStrategy,
    AnomalyKind,
    GateKind,
    PaymentStatus,
    SessionStatus,
    Severity,
    SlotStatus,
    UserRole,
    VehicleType,
)
from app.models.facility import (
    AuthorizedVehicle,
    Facility,
    Gate,
    Level,
    OccupancySnapshot,
    PriceSnapshot,
    Slot,
)
from app.models.parking import Anomaly, ParkingSession, RecognitionEvent
from app.models.user import Notification, User, Vehicle, Wallet, WalletTransaction
from app.services.allocation.simulator import DEMAND_CURVE
from app.services.anpr.plate_utils import normalize_plate, pretty_plate
from app.services.anpr.synthetic import random_plate
from app.services.billing.pricing import pricing_engine
from app.services.layout import layout_service

configure_logging()
log = get_logger("seed")

DEMO_PASSWORD = "SmartPark2026!"

# Accounts this script creates. Anything outside this set was made by hand, so
# wiping it would destroy real work rather than reset a demo.
SEEDED_EMAILS = {
    "admin@smartpark.dev",
    "owner@smartpark.dev",
    *(email for email, *_ in [
        ("mit.darji@smartpark.dev",), ("abhishek.patel@smartpark.dev",),
        ("priya.shah@smartpark.dev",), ("rahul.mehta@smartpark.dev",),
        ("neha.iyer@smartpark.dev",), ("arjun.rao@smartpark.dev",),
    ]),
}

DRIVERS = [
    ("mit.darji@smartpark.dev", "Mit Darji", "GJ01AB1234", VehicleType.SEDAN, False),
    ("abhishek.patel@smartpark.dev", "Abhishek Patel", "GJ18CD5678", VehicleType.SUV, False),
    ("priya.shah@smartpark.dev", "Priya Shah", "GJ05EV9012", VehicleType.EV, True),
    ("rahul.mehta@smartpark.dev", "Rahul Mehta", "MH12XY4567", VehicleType.HATCHBACK, False),
    ("neha.iyer@smartpark.dev", "Neha Iyer", "KA03MN7788", VehicleType.HATCHBACK, False),
    ("arjun.rao@smartpark.dev", "Arjun Rao", "TN09PQ3321", VehicleType.TWO_WHEELER, False),
]

AUTHORISED_CAMPUS = [
    ("GJ01AB1234", "Mit Darji — student"),
    ("GJ18CD5678", "Abhishek Patel — student"),
    ("GJ27FA0001", "Dr. Gaurav Bhargava — faculty"),
    ("GJ27FA0002", "Faculty pool car"),
    ("GJ27SE0100", "Campus security"),
]


async def guard_existing_data(force: bool) -> None:
    """Refuse to wipe hand-made accounts unless the caller insists.

    Both seed paths are destructive — `--reset` drops every table and the
    default wipes every row. That is correct for a demo reset and catastrophic
    for someone who has spent an afternoon drawing a facility. So the script
    looks first, names exactly what it would destroy, and stops.
    """
    try:
        async with SessionLocal() as db:
            users = (await db.execute(select(User))).scalars().all()
            facilities = (await db.execute(select(Facility))).scalars().all()
    except Exception:
        return  # no database yet — nothing to protect

    seeded_ids = {u.id for u in users if u.email in SEEDED_EMAILS}
    handmade_users = [u for u in users if u.email not in SEEDED_EMAILS]
    handmade_facilities = [f for f in facilities if f.owner_id not in seeded_ids]

    if not handmade_users and not handmade_facilities:
        return

    print("\n" + "!" * 68)
    print("  This database contains data the seed script did not create.")
    print("!" * 68)
    if handmade_users:
        print(f"\n  {len(handmade_users)} account(s):")
        for user in handmade_users[:10]:
            print(f"    - {user.email}  ({user.role})")
    if handmade_facilities:
        print(f"\n  {len(handmade_facilities)} facility/facilities:")
        for facility in handmade_facilities[:10]:
            print(f"    - {facility.name}")

    if force:
        print("\n  --force given: proceeding, and all of the above will be destroyed.\n")
        return

    print(
        "\n  Seeding would destroy all of it.\n"
        "\n  Back it up first:"
        "\n    cp backend/data/smartpark.db backend/data/smartpark.backup.db"
        "\n"
        "\n  Then re-run with --force if you really want a clean slate:"
        "\n    python -m scripts.seed --reset --force\n"
    )
    raise SystemExit(1)


async def reset_database() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    log.info("database reset")


async def wipe_rows(db) -> None:
    """Clear seeded rows without dropping the schema."""
    for model in (
        AgentMessage, AgentConversation, AutomationRun, AutomationRule, Anomaly,
        RecognitionEvent, WalletTransaction, ParkingSession, Notification,
        PriceSnapshot, OccupancySnapshot, AuthorizedVehicle, Slot, Gate, Level,
        Facility, Vehicle, Wallet, User,
    ):
        await db.execute(delete(model))
    await db.commit()
    log.info("existing rows cleared")


async def create_users(db) -> dict:
    admin = User(
        email="admin@smartpark.dev", password_hash=hash_password(DEMO_PASSWORD),
        full_name="Platform Administrator", role=UserRole.ADMIN, phone="+919000000000",
    )
    owner = User(
        email="owner@smartpark.dev", password_hash=hash_password(DEMO_PASSWORD),
        full_name="Gandhinagar Facilities Pvt Ltd", role=UserRole.OWNER,
        phone="+919111111111",
    )
    db.add_all([admin, owner])
    await db.flush()
    db.add_all([
        Wallet(user_id=admin.id, balance_minor=0),
        Wallet(user_id=owner.id, balance_minor=0),
    ])

    drivers = []
    for email, name, plate, vtype, is_ev in DRIVERS:
        user = User(
            email=email, password_hash=hash_password(DEMO_PASSWORD), full_name=name,
            role=UserRole.DRIVER, phone=f"+9198{random.randint(10000000, 99999999)}",
            voice_pin=f"{random.randint(1000, 9999)}",
        )
        db.add(user)
        await db.flush()
        db.add(
            Wallet(
                user_id=user.id,
                balance_minor=random.randint(150, 900) * 100,
                auto_reload_enabled=random.random() < 0.5,
            )
        )
        normalized = normalize_plate(plate)
        db.add(
            Vehicle(
                owner_id=user.id, plate=pretty_plate(normalized),
                plate_normalized=normalized, vehicle_type=vtype, is_ev=is_ev,
                make=random.choice(["Maruti", "Hyundai", "Tata", "Honda", "Kia"]),
                model=random.choice(["Nexon", "i20", "Swift", "City", "Seltos"]),
                color=random.choice(["White", "Silver", "Blue", "Red", "Grey"]),
            )
        )
        drivers.append(user)

    await db.flush()
    log.info("users created", extra={"drivers": len(drivers)})
    return {"admin": admin, "owner": owner, "drivers": drivers}


async def create_mall(db, owner: User) -> Facility:
    facility = Facility(
        owner_id=owner.id,
        name="Infocity Central Mall",
        slug="infocity-central-mall",
        description=(
            "Three-level retail car park on Infocity Road. Public access, "
            "occupancy-based pricing, EV charging on the ground deck."
        ),
        address="Infocity Road, Gandhinagar",
        city="Gandhinagar",
        latitude=23.1897, longitude=72.6350,
        access_mode=AccessMode.PUBLIC,
        allocation_strategy=AllocationStrategy.HYBRID,
        base_rate_minor_per_hour=4000,      # ₹40/hour
        free_minutes=15,
        billing_increment_minutes=15,
        daily_cap_minor=45_000,
        tax_percent=18.0,
        dynamic_pricing_enabled=True,
        overstay_after_hours=12,
        contact_phone="+917926000000",
        amenities=["ev_charging", "cctv", "covered", "lift_access", "valet"],
    )
    db.add(facility)
    await db.flush()

    for index, name in enumerate(["Ground", "Level 1", "Level 2"]):
        db.add(Level(facility_id=facility.id, name=name, order_index=index))
    await db.flush()

    levels = (
        await db.execute(
            select(Level).where(Level.facility_id == facility.id).order_by(Level.order_index)
        )
    ).scalars().all()

    db.add_all([
        Gate(
            facility_id=facility.id, level_id=levels[0].id, name="North Entry",
            kind=GateKind.ENTRY, x=1.0, y=1.0, camera_id="CAM-N-01", is_primary=True,
        ),
        Gate(
            facility_id=facility.id, level_id=levels[0].id, name="North Exit",
            kind=GateKind.EXIT, x=1.0, y=8.0, camera_id="CAM-N-02",
        ),
        Gate(
            facility_id=facility.id, level_id=levels[1].id, name="South Entry",
            kind=GateKind.BIDIRECTIONAL, x=36.0, y=1.0, camera_id="CAM-S-01",
        ),
    ])
    await db.flush()

    await layout_service.generate_grid(
        db, facility, level_id=levels[0].id, rows=4, columns=12,
        zone_prefix="G", ev_every=8,
    )
    await layout_service.generate_grid(
        db, facility, level_id=levels[1].id, rows=4, columns=12, zone_prefix="L",
    )
    await layout_service.generate_grid(
        db, facility, level_id=levels[2].id, rows=2, columns=12, zone_prefix="M",
    )
    await db.flush()
    log.info("mall created", extra={"facility_id": facility.id})
    return facility


async def create_campus(db, owner: User) -> Facility:
    facility = Facility(
        owner_id=owner.id,
        name="PDEU Campus Parking",
        slug="pdeu-campus-parking",
        description=(
            "Restricted campus parking. Only vehicles on the authorised list may "
            "enter; faculty bays are reserved and charges are waived."
        ),
        address="Pandit Deendayal Energy University, Raysan",
        city="Gandhinagar",
        latitude=23.1533, longitude=72.6640,
        access_mode=AccessMode.PRIVATE,
        allocation_strategy=AllocationStrategy.RECENCY,
        base_rate_minor_per_hour=2000,
        free_minutes=30,
        daily_cap_minor=15_000,
        tax_percent=18.0,
        dynamic_pricing_enabled=False,
        overstay_after_hours=14,
        amenities=["cctv", "two_wheeler_bays", "shaded"],
    )
    db.add(facility)
    await db.flush()

    level = Level(facility_id=facility.id, name="Main Yard", order_index=0)
    db.add(level)
    await db.flush()
    db.add(
        Gate(
            facility_id=facility.id, level_id=level.id, name="Campus Gate",
            kind=GateKind.BIDIRECTIONAL, x=0.0, y=0.0, camera_id="CAM-PDEU-01",
            is_primary=True,
        )
    )
    await db.flush()

    await layout_service.generate_grid(
        db, facility, level_id=level.id, rows=4, columns=10, zone_prefix="C",
    )
    await db.flush()

    reserved = (
        await db.execute(
            select(Slot).where(Slot.facility_id == facility.id).order_by(Slot.code).limit(3)
        )
    ).scalars().all()

    for index, (plate, label) in enumerate(AUTHORISED_CAMPUS):
        normalized = normalize_plate(plate)
        is_faculty = "faculty" in label.lower() or "security" in label.lower()
        db.add(
            AuthorizedVehicle(
                facility_id=facility.id,
                plate_normalized=normalized,
                label=label,
                owner_name=label.split("—")[0].strip(),
                is_active=True,
                waive_charges=is_faculty,
                reserved_slot_id=(
                    reserved[index - 2].id if is_faculty and index >= 2 and index - 2 < len(reserved)
                    else None
                ),
            )
        )
    await db.flush()
    log.info("campus created", extra={"facility_id": facility.id})
    return facility


async def backfill_history(db, facility: Facility, drivers: list[User], *, days: int) -> int:
    """Generate hourly occupancy snapshots and completed sessions.

    The traffic follows the same demand curve the simulator uses, so the history
    is internally consistent with the rest of the system rather than white noise.
    """
    rng = random.Random(2026)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    slots = (
        await db.execute(
            select(Slot).where(Slot.facility_id == facility.id).order_by(Slot.distance_from_entry)
        )
    ).scalars().all()
    capacity = len(slots)
    if not capacity:
        return 0

    driver_vehicles = {}
    for driver in drivers:
        vehicle = (
            await db.execute(select(Vehicle).where(Vehicle.owner_id == driver.id))
        ).scalar_one_or_none()
        if vehicle:
            driver_vehicles[driver.id] = vehicle

    guest_plates = [random_plate(rng) for _ in range(60)]
    strategies = [
        AllocationStrategy.HYBRID, AllocationStrategy.RECENCY,
        AllocationStrategy.NEAREST, AllocationStrategy.BALANCED,
    ]

    sessions_created = 0
    # Track each bay's most recent departure so the recency allocator has real
    # history to sort on after seeding — otherwise every slot looks brand new
    # and the policy degrades to its cold-start branch during the demo.
    slot_activity: dict[int, tuple[datetime, int, int]] = {}

    for hours_ago in range(days * 24, 0, -1):
        ts = now - timedelta(hours=hours_ago)
        demand = DEMAND_CURVE[ts.hour]
        weekend_boost = 1.25 if ts.weekday() >= 5 else 1.0

        occupancy = min(0.96, max(0.02, demand * weekend_boost * rng.uniform(0.75, 1.05)))
        occupied = int(round(occupancy * capacity))
        arrivals = max(0, int(round(capacity * demand * 0.22 * weekend_boost * rng.uniform(0.7, 1.3))))

        rate, multiplier, components = pricing_engine.effective_rate(
            facility, occupancy=occupancy, moment=ts
        )

        db.add(
            OccupancySnapshot(
                facility_id=facility.id, ts=ts, capacity=capacity, occupied=occupied,
                occupancy_pct=round(occupancy, 4), arrivals=arrivals,
                departures=max(0, arrivals + rng.randint(-3, 3)),
                avg_dwell_minutes=round(rng.lognormvariate(math.log(95), 0.5), 1),
                revenue_minor=int(arrivals * rate * 1.4),
                effective_rate_minor=rate,
            )
        )
        if hours_ago % 6 == 0:
            db.add(
                PriceSnapshot(
                    facility_id=facility.id, created_at=ts,
                    base_rate_minor=facility.base_rate_minor_per_hour,
                    effective_rate_minor=rate, occupancy_pct=round(occupancy, 4),
                    multiplier=round(multiplier, 4), components=components,
                    reason="back-filled history",
                )
            )

        # Materialise a fraction of the arrivals as full session records.
        for _ in range(min(arrivals, 4)):
            entry_at = ts + timedelta(minutes=rng.randint(0, 59))
            dwell = min(rng.lognormvariate(math.log(95), 0.7), 11 * 60)
            exit_at = entry_at + timedelta(minutes=dwell)
            if exit_at >= now:
                continue

            slot = rng.choice(slots)
            use_registered = rng.random() < 0.45 and driver_vehicles
            if use_registered:
                driver = rng.choice([d for d in drivers if d.id in driver_vehicles])
                vehicle = driver_vehicles[driver.id]
                plate_norm = vehicle.plate_normalized
                user_id, vehicle_id = driver.id, vehicle.id
            else:
                plate_norm = rng.choice(guest_plates)
                user_id = vehicle_id = None

            quote = pricing_engine.quote(
                facility, raw_minutes=dwell, occupancy=occupancy,
                entry_at=entry_at, exit_at=exit_at, slot_type=slot.slot_type,
            )
            strategy = rng.choice(strategies)
            entry_conf = round(rng.uniform(0.72, 0.99), 3)
            exit_conf = round(rng.uniform(0.68, 0.99), 3)

            session = ParkingSession(
                facility_id=facility.id, vehicle_id=vehicle_id, user_id=user_id,
                slot_id=slot.id, plate=pretty_plate(plate_norm), plate_normalized=plate_norm,
                entry_at=entry_at, exit_at=exit_at, status=SessionStatus.COMPLETED,
                allocation_strategy=strategy,
                allocation_reason=f"seeded history ({strategy})",
                allocation_latency_ms=round(rng.uniform(0.4, 4.5), 2),
                walk_distance_m=round(
                    slot.distance_from_entry * rng.uniform(0.95, 1.15), 1
                ),
                candidates_considered=rng.randint(8, capacity),
                entry_confidence=entry_conf, exit_confidence=exit_conf,
                billable_minutes=quote.billable_minutes,
                subtotal_minor=quote.subtotal_minor, discount_minor=quote.discount_minor,
                tax_minor=quote.tax_minor, total_minor=quote.total_minor,
                currency=facility.currency,
                payment_status=(
                    PaymentStatus.PAID if (user_id and rng.random() > 0.03)
                    else (PaymentStatus.PENDING if not user_id else PaymentStatus.FAILED)
                ),
                rate_breakdown=quote.as_dict(),
            )
            db.add(session)
            await db.flush()
            session.invoice_no = f"SP-{facility.id:03d}-{session.id:06d}"
            sessions_created += 1

            last_exit, uses, minutes = slot_activity.get(slot.id, (exit_at, 0, 0))
            slot_activity[slot.id] = (
                max(last_exit, exit_at), uses + 1, minutes + int(dwell)
            )

            db.add(
                RecognitionEvent(
                    facility_id=facility.id, session_id=session.id, direction="entry",
                    plate_raw=plate_norm, plate_normalized=plate_norm,
                    confidence=entry_conf, backend="segmentation",
                    processing_ms=round(rng.uniform(18, 65), 2),
                    decision="entry_allowed", needs_review=entry_conf < 0.75,
                    created_at=entry_at,
                )
            )

            # A realistic trickle of anomalies keeps the triage view meaningful.
            if entry_conf < 0.75 and rng.random() < 0.4:
                db.add(
                    Anomaly(
                        facility_id=facility.id, session_id=session.id,
                        kind=AnomalyKind.LOW_CONFIDENCE_READ, severity=Severity.MEDIUM,
                        score=round(1 - entry_conf, 3),
                        summary=(
                            f"Plate {pretty_plate(plate_norm)} read at only "
                            f"{entry_conf:.0%} confidence."
                        ),
                        detail={"confidence": entry_conf},
                        created_at=entry_at,
                    )
                )

        if hours_ago % 200 == 0:
            await db.flush()

    for slot in slots:
        activity = slot_activity.get(slot.id)
        if activity is None:
            continue
        last_exit, uses, minutes = activity
        slot.last_vacated_at = last_exit
        slot.total_uses = uses
        slot.total_occupied_minutes = minutes

    await db.flush()
    log.info(
        "history back-filled",
        extra={"facility_id": facility.id, "days": days, "sessions": sessions_created},
    )
    return sessions_created


async def create_live_sessions(
    db, facility: Facility, drivers: list[User], count: int,
    already_parked: set[str] | None = None,
) -> int:
    """Park a few vehicles right now, so the dashboard is not empty on first load.

    `already_parked` carries plates that are live elsewhere. Without it the same
    vehicle ends up parked in two facilities simultaneously — which the platform
    correctly treats as a cloned-plate anomaly, so the seed must not create it.
    """
    already_parked = already_parked if already_parked is not None else set()
    rng = random.Random(7)
    now = datetime.now(UTC)

    free = (
        await db.execute(
            select(Slot)
            .where(Slot.facility_id == facility.id, Slot.status == SlotStatus.EMPTY)
            .order_by(Slot.distance_from_entry)
            .limit(count * 3)
        )
    ).scalars().all()
    if not free:
        return 0

    created = 0
    for index in range(min(count, len(free))):
        slot = free[index]
        if index < len(drivers):
            driver = drivers[index]
            vehicle = (
                await db.execute(select(Vehicle).where(Vehicle.owner_id == driver.id))
            ).scalar_one_or_none()
            if vehicle is None or vehicle.plate_normalized in already_parked:
                continue
            plate_norm, user_id, vehicle_id = vehicle.plate_normalized, driver.id, vehicle.id
        else:
            plate_norm, user_id, vehicle_id = random_plate(rng), None, None
            while plate_norm in already_parked:
                plate_norm = random_plate(rng)

        entry_at = now - timedelta(minutes=rng.randint(8, 220))
        session = ParkingSession(
            facility_id=facility.id, vehicle_id=vehicle_id, user_id=user_id,
            slot_id=slot.id, plate=pretty_plate(plate_norm), plate_normalized=plate_norm,
            entry_at=entry_at, status=SessionStatus.ACTIVE,
            allocation_strategy=facility.allocation_strategy,
            allocation_reason="seeded live session",
            allocation_latency_ms=round(rng.uniform(0.5, 3.0), 2),
            walk_distance_m=slot.distance_from_entry,
            candidates_considered=rng.randint(10, 60),
            entry_confidence=round(rng.uniform(0.82, 0.99), 3),
            currency=facility.currency,
            navigation_path={
                "waypoints": [[0, 0], [slot.x, 0], [slot.x, slot.y]],
                "distance_m": slot.distance_from_entry,
                "instructions": [
                    "Enter the facility and follow the marked aisle.",
                    f"Drive {slot.distance_from_entry:.0f} metres — slot {slot.code} "
                    f"is in zone {slot.zone}, on your right.",
                ],
            },
        )
        db.add(session)
        slot.status = SlotStatus.OCCUPIED
        slot.last_occupied_at = entry_at
        slot.total_uses += 1
        already_parked.add(plate_norm)
        created += 1

    await db.flush()
    return created


async def create_automation_rules(db, mall: Facility, campus: Facility) -> int:
    rules = [
        AutomationRule(
            facility_id=mall.id,
            name="Flag overstaying vehicles",
            description="Raise an anomaly and notify the driver past the overstay threshold.",
            trigger="schedule:tick",
            conditions={},
            actions=[{"action": "flag_overstays", "params": {}}],
            cooldown_seconds=600, priority=10,
        ),
        AutomationRule(
            facility_id=mall.id,
            name="Reprice on high occupancy",
            description="Recompute the dynamic rate once the deck passes 70% full.",
            trigger="facility.occupancy_changed",
            conditions={"occupancy_pct_gte": 70},
            actions=[{"action": "recompute_pricing", "params": {}}],
            cooldown_seconds=300, priority=20,
        ),
        AutomationRule(
            facility_id=mall.id,
            name="Alert the operator on a failed payment",
            description="A charge that could not be collected needs a human.",
            trigger="wallet.payment_failed",
            conditions={},
            actions=[
                {
                    "action": "alert_operator",
                    "params": {"message": "A parking charge could not be collected."},
                }
            ],
            priority=15,
        ),
        AutomationRule(
            facility_id=None,
            name="Release expired reservations",
            description="Free bays held for vehicles that never arrived.",
            trigger="schedule:tick",
            conditions={},
            actions=[{"action": "release_stale_reservations", "params": {}}],
            cooldown_seconds=120, priority=30,
        ),
        AutomationRule(
            facility_id=None,
            name="Nightly wallet reconciliation",
            description="Recompute every wallet from its ledger and report drift.",
            trigger="schedule:tick",
            conditions={"hour": 2},
            actions=[{"action": "reconcile_wallets", "params": {}}],
            cooldown_seconds=3600, priority=90,
        ),
        AutomationRule(
            facility_id=campus.id,
            name="Escalate unauthorised entry attempts",
            description="Notify campus security when a plate is refused at the gate.",
            trigger="vehicle.entry_denied",
            conditions={},
            actions=[
                {
                    "action": "alert_operator",
                    "params": {"message": "An unauthorised vehicle was refused at the campus gate."},
                }
            ],
            priority=5,
        ),
        AutomationRule(
            facility_id=None,
            name="Close abandoned sessions",
            description="Close sessions with no exit scan after 48 hours and free the bay.",
            trigger="schedule:tick",
            conditions={"hour": 4},
            actions=[{"action": "close_abandoned_sessions", "params": {"after_hours": 48}}],
            cooldown_seconds=3600, priority=95,
        ),
    ]
    db.add_all(rules)
    await db.flush()
    return len(rules)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the SmartPark demo environment.")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate every table.")
    parser.add_argument("--days", type=int, default=21, help="Days of history to back-fill.")
    parser.add_argument("--train", action="store_true", help="Train the ML models afterwards.")
    parser.add_argument(
        "--force", action="store_true",
        help="Wipe even if the database holds accounts this script did not create.",
    )
    args = parser.parse_args()

    await guard_existing_data(args.force)

    if args.reset:
        await reset_database()
    else:
        await init_db()

    async with SessionLocal() as db:
        if not args.reset:
            await wipe_rows(db)

        people = await create_users(db)
        mall = await create_mall(db, people["owner"])
        campus = await create_campus(db, people["owner"])

        mall_sessions = await backfill_history(db, mall, people["drivers"], days=args.days)
        campus_sessions = await backfill_history(db, campus, people["drivers"], days=args.days)

        parked_now: set[str] = set()
        live_mall = await create_live_sessions(
            db, mall, people["drivers"][:4], count=9, already_parked=parked_now
        )
        # Different drivers, so no plate is live in two facilities at once.
        live_campus = await create_live_sessions(
            db, campus, people["drivers"][4:], count=3, already_parked=parked_now
        )

        rules = await create_automation_rules(db, mall, campus)
        await db.commit()

        if args.train:
            from app.services.analytics import analytics_service

            for facility in (mall, campus):
                result = await analytics_service.train_models(db, facility)
                log.info("models trained", extra={"facility": facility.name, "result": result})
            await db.commit()

    print("\n" + "=" * 68)
    print("  SmartPark demo environment ready")
    print("=" * 68)
    print(f"  Facilities        : {mall.name} (public), {campus.name} (private)")
    print(f"  History           : {args.days} days")
    print(f"  Sessions seeded   : {mall_sessions + campus_sessions} completed, "
          f"{live_mall + live_campus} live")
    print(f"  Automation rules  : {rules}")
    print("\n  Sign in with:")
    print(f"    owner   owner@smartpark.dev        / {DEMO_PASSWORD}")
    print(f"    driver  mit.darji@smartpark.dev    / {DEMO_PASSWORD}")
    print(f"    admin   admin@smartpark.dev        / {DEMO_PASSWORD}")
    print("\n  Next:  uvicorn app.main:app --reload    →  http://localhost:8000/docs")
    print("=" * 68 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
