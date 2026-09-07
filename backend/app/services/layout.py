"""Layout construction: grid generation, distance computation, plan detection."""

from __future__ import annotations

import statistics

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.logging import get_logger
from app.models.enums import SlotType
from app.models.facility import Facility, Gate, Level, Slot
from app.services.allocation.navigation import route_to_slot

log = get_logger(__name__)

# Layout coordinates are METRES throughout — slot geometry, gate positions,
# aisle polylines, canvas extents. The frontend scales metres to pixels for
# drawing; the backend never deals in pixels. Getting this wrong once already
# produced "the nearest bay is 250 m away", so the unit is stated here and
# nowhere else re-interpreted.
SLOT_WIDTH = 2.7          # standard Indian bay: 2.5 m plus a 0.2 m stripe
SLOT_HEIGHT = 5.0
AISLE_WIDTH = 6.0         # two-way drive aisle
MARGIN = 3.0
LEVEL_RAMP_PENALTY_M = 45.0   # driving up one deck


class LayoutService:
    async def generate_grid(
        self,
        db: AsyncSession,
        facility: Facility,
        *,
        level_id: int,
        rows: int,
        columns: int,
        zone_prefix: str = "A",
        ev_every: int = 0,
    ) -> list[Slot]:
        """Lay out `rows` x `columns` bays in back-to-back rows with aisles between.

        Rows are paired: two rows of bays face each other across a shared aisle,
        which is how real decks are built and what makes the generated aisle
        polylines match the geometry the wayfinder routes along.
        """
        level = await db.get(Level, level_id)
        if level is None or level.facility_id != facility.id:
            raise NotFound(f"Level {level_id} was not found at this facility.")

        existing_codes = set(
            (
                await db.execute(select(Slot.code).where(Slot.facility_id == facility.id))
            ).scalars().all()
        )

        created: list[Slot] = []
        aisles: list[list[list[float]]] = []
        row_pitch = SLOT_HEIGHT * 2 + AISLE_WIDTH

        for row in range(rows):
            pair_index, side = divmod(row, 2)
            band_top = MARGIN + pair_index * row_pitch
            y = band_top if side == 0 else band_top + SLOT_HEIGHT + AISLE_WIDTH
            aisle_y = band_top + SLOT_HEIGHT + AISLE_WIDTH / 2

            if side == 0:
                aisles.append(
                    [
                        [MARGIN, aisle_y],
                        [MARGIN + columns * SLOT_WIDTH, aisle_y],
                    ]
                )

            zone = f"{zone_prefix}{'' if rows <= 2 else chr(ord('A') + pair_index)}"
            for column in range(columns):
                index = row * columns + column + 1
                code = f"{zone}{index:03d}"
                if code in existing_codes:
                    continue
                existing_codes.add(code)

                slot_type = SlotType.STANDARD
                charger = False
                if ev_every and index % ev_every == 0:
                    slot_type, charger = SlotType.EV, True
                elif index % 17 == 0:
                    slot_type = SlotType.ACCESSIBLE
                elif index % 7 == 0:
                    slot_type = SlotType.COMPACT

                slot = Slot(
                    facility_id=facility.id,
                    level_id=level.id,
                    code=code,
                    zone=zone,
                    x=MARGIN + column * SLOT_WIDTH,
                    y=y,
                    width=SLOT_WIDTH - 0.2,
                    height=SLOT_HEIGHT - 0.3,
                    slot_type=slot_type,
                    has_ev_charger=charger,
                )
                db.add(slot)
                created.append(slot)

        # A spine down the left edge connects every cross aisle to the gate.
        if aisles:
            spine_x = MARGIN - 1.5
            aisles.append([[spine_x, aisles[0][0][1]], [spine_x, aisles[-1][0][1]]])
            for aisle in aisles[:-1]:
                aisle[0][0] = spine_x

        level.canvas_width = max(level.canvas_width, MARGIN * 2 + columns * SLOT_WIDTH)
        level.canvas_height = max(
            level.canvas_height, MARGIN * 2 + ((rows + 1) // 2) * row_pitch
        )
        level.aisles = aisles

        await db.flush()
        await self.recompute_distances(db, facility.id)
        log.info(
            "grid generated",
            extra={"facility_id": facility.id, "slots": len(created), "rows": rows},
        )
        return created

    async def recompute_distances(self, db: AsyncSession, facility_id: int) -> int:
        """Recompute every slot's routed distance from the primary entry gate.

        Straight-line distance would misrank bays that are close as the crow
        flies but reached by driving around a whole block, so the stored value is
        the routed distance along the aisle graph. The allocator reads this
        column, which keeps allocation a cheap sort instead of N pathfindings.
        """
        gate = (
            await db.execute(
                select(Gate)
                .where(Gate.facility_id == facility_id, Gate.is_active.is_(True))
                .order_by(Gate.is_primary.desc(), Gate.id)
            )
        ).scalars().first()
        gate_point = gate.position if gate else (0.0, 0.0)

        levels = {
            level.id: level
            for level in (
                await db.execute(select(Level).where(Level.facility_id == facility_id))
            ).scalars().all()
        }
        slots = (
            await db.execute(select(Slot).where(Slot.facility_id == facility_id))
        ).scalars().all()

        for slot in slots:
            level = levels.get(slot.level_id)
            route = route_to_slot(
                gate_point, slot.center,
                aisles=level.aisles if level else [],
                slot_code=slot.code, zone=slot.zone,
            )
            # Bays on an upper deck also cost the ramp climb.
            level_penalty = (level.order_index * LEVEL_RAMP_PENALTY_M) if level else 0.0
            slot.distance_from_entry = round(route.distance_m + level_penalty, 2)

        await db.flush()
        return len(slots)

    # ── Floor-plan slot detection (Phase-3 stretch goal) ──────

    def detect_slots_from_image(self, payload: bytes) -> dict:
        """Propose bay rectangles from a clean, structured floor-plan image.

        Deliberately scoped, and honest about it: this finds repeated rectangles
        of consistent size in a line-drawing. It is not a general parser for
        arbitrary architectural drawings, and it will return nothing rather than
        guess at a photograph or a heavily annotated CAD export.

        The output is always *proposals* for the canvas editor to review.
        """
        buffer = np.frombuffer(payload, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            return {"detected_slots": [], "note": "the image could not be decoded"}

        height, width = image.shape[:2]
        scale = 1.0
        if width > 1600:
            scale = 1600 / width
            image = cv2.resize(image, (1600, int(height * scale)), interpolation=cv2.INTER_AREA)
            height, width = image.shape[:2]

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 8
        )
        binary = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        )

        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[tuple[int, int, int, int, float]] = []
        frame_area = float(width * height)
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 0.0002 * frame_area or area > 0.02 * frame_area:
                continue
            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.04 * peri, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            x, y, w, h = cv2.boundingRect(approx)
            aspect = max(w, h) / max(1, min(w, h))
            if not 1.3 <= aspect <= 3.2:      # a parking bay is a longish rectangle
                continue
            if area / float(w * h) < 0.75:    # must actually fill its bounding box
                continue
            boxes.append((x, y, w, h, area))

        if len(boxes) < 4:
            return {
                "detected_slots": [],
                "note": (
                    "No consistent bay rectangles were found. This detector targets "
                    "clean line-drawing plans; draw the layout manually instead."
                ),
            }

        # Real bays are near-identical in size. Keep the dominant size cluster and
        # discard everything else — that filter removes most false positives.
        areas = [b[4] for b in boxes]
        median_area = statistics.median(areas)
        kept = [b for b in boxes if 0.6 * median_area <= b[4] <= 1.6 * median_area]
        if len(kept) < 4:
            return {
                "detected_slots": [],
                "note": "Rectangles were found but their sizes were too inconsistent to be bays.",
            }

        kept.sort(key=lambda b: (round(b[1] / 40), b[0]))
        proposals = []
        for index, (x, y, w, h, _) in enumerate(kept[:400], start=1):
            row_band = int(y / (statistics.median([b[3] for b in kept]) * 2 + 1))
            proposals.append(
                {
                    "code": f"P{index:03d}",
                    "zone": chr(ord("A") + min(row_band, 25)),
                    "x": round(x / scale, 1),
                    "y": round(y / scale, 1),
                    "width": round(w / scale, 1),
                    "height": round(h / scale, 1),
                    "rotation": 0.0,
                    "slot_type": SlotType.STANDARD.value,
                    "confidence": 0.6,
                }
            )

        return {
            "detected_slots": proposals,
            "count": len(proposals),
            "note": (
                f"{len(proposals)} candidate bays detected from the plan. These are "
                f"proposals — review, adjust and rename them in the layout editor "
                f"before saving."
            ),
        }


layout_service = LayoutService()
