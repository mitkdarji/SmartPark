"""One-off repair for sessions orphaned before owner-claiming existed.

A session captures its owner once, at entry. Any vehicle registered *after* it
drove in stayed a guest for the rest of its stay — invisible in the driver app
and uncollectable at the exit gate.

The claim now runs at registration, but sessions created before that shipped are
still orphaned. This applies the identical rule retroactively, once.

    python -m scripts.backfill_session_owners --dry-run
    python -m scripts.backfill_session_owners
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import SessionLocal
from app.models.enums import SessionStatus
from app.models.parking import ParkingSession
from app.models.user import User, Vehicle

configure_logging()
log = get_logger("backfill")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    args = parser.parse_args()

    async with SessionLocal() as db:
        # Active guest sessions whose plate is now registered to somebody.
        rows = (
            await db.execute(
                select(ParkingSession, Vehicle, User)
                .join(Vehicle, Vehicle.plate_normalized == ParkingSession.plate_normalized)
                .join(User, User.id == Vehicle.owner_id)
                .where(
                    ParkingSession.status == SessionStatus.ACTIVE,
                    ParkingSession.user_id.is_(None),
                )
            )
        ).all()

        if not rows:
            print("No orphaned sessions found — nothing to repair.")
            return

        print(f"\n{len(rows)} active session(s) belong to a now-registered vehicle:\n")
        for session, vehicle, user in rows:
            print(
                f"  session {session.id:<6} {vehicle.plate:<16} "
                f"-> {user.email} (entered {session.entry_at:%d %b %H:%M})"
            )
            if not args.dry_run:
                session.user_id = vehicle.owner_id
                session.vehicle_id = vehicle.id

        if args.dry_run:
            print("\nDry run — nothing written. Re-run without --dry-run to apply.")
            return

        await db.commit()
        print(f"\nRepaired {len(rows)} session(s).")
        log.info("backfilled session owners", extra={"sessions": len(rows)})


if __name__ == "__main__":
    asyncio.run(main())
