"""Occupancy-sensitive dynamic pricing.

The rate a driver pays is the base rate multiplied by a product of bounded
factors. Every factor is small, explainable, and individually capped — the aim
is demand response, not surge-pricing outrage:

    occupancy   the headline factor. A piecewise-linear curve, flat until the
                lot is 60% full, rising to a hard ceiling at capacity.
    time        evening and weekend peaks at a mall; a night discount.
    duration    a declining block tariff — later hours cost less, which is how
                real car parks encourage long stays to use the far zones.
    slot        EV bays and premium zones carry their own multiplier.
    demand      optional nudge from the ML forecaster when a surge is predicted
                within the hour, so price moves *before* the lot fills.

Every quote returns the full component breakdown. The driver app shows it, the
audit table stores it, and a billing dispute can be answered exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.enums import SlotType
from app.models.facility import Facility

# Global guard rails. No configuration may price outside this band.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 2.0

DEFAULT_PRICING_CONFIG: dict = {
    "occupancy_free_threshold": 0.60,   # no surge below this occupancy
    "occupancy_max_multiplier": 1.6,    # multiplier at 100% occupancy
    "night_discount": 0.8,              # 22:00-06:00
    "peak_multiplier": 1.15,            # 18:00-21:00
    "weekend_multiplier": 1.10,
    "long_stay_discount_after_hours": 4,
    "long_stay_discount": 0.75,         # applied to hours beyond the threshold
    "ev_multiplier": 1.25,
    "forecast_sensitivity": 0.15,       # weight on the predicted-surge nudge
}

SLOT_TYPE_MULTIPLIER = {
    SlotType.EV: 1.25,
    SlotType.LARGE: 1.15,
    SlotType.ACCESSIBLE: 1.0,
    SlotType.COMPACT: 0.9,
    SlotType.TWO_WHEELER: 0.5,
    SlotType.STANDARD: 1.0,
}


@dataclass(slots=True)
class PriceQuote:
    base_rate_minor: int
    effective_rate_minor: int
    multiplier: float
    billable_minutes: int
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    components: dict = field(default_factory=dict)
    explanation: str = ""

    def as_dict(self) -> dict:
        return {
            "base_rate_minor": self.base_rate_minor,
            "effective_rate_minor": self.effective_rate_minor,
            "multiplier": round(self.multiplier, 4),
            "billable_minutes": self.billable_minutes,
            "subtotal_minor": self.subtotal_minor,
            "discount_minor": self.discount_minor,
            "tax_minor": self.tax_minor,
            "total_minor": self.total_minor,
            "currency": self.currency,
            "components": self.components,
            "explanation": self.explanation,
        }


class PricingEngine:
    def config(self, facility: Facility) -> dict:
        return {**DEFAULT_PRICING_CONFIG, **(facility.pricing_config or {})}

    # ── Rate ──────────────────────────────────────────────────

    def occupancy_multiplier(self, occupancy: float, config: dict) -> float:
        """Flat until the free threshold, then linear to the configured ceiling."""
        threshold = float(config["occupancy_free_threshold"])
        ceiling = float(config["occupancy_max_multiplier"])
        occupancy = max(0.0, min(1.0, occupancy))
        if occupancy <= threshold:
            return 1.0
        progress = (occupancy - threshold) / max(1e-6, 1.0 - threshold)
        return 1.0 + (ceiling - 1.0) * progress

    def time_multiplier(self, moment: datetime, config: dict) -> tuple[float, str]:
        hour = moment.hour
        if hour >= 22 or hour < 6:
            return float(config["night_discount"]), "night discount"
        if 18 <= hour < 21:
            return float(config["peak_multiplier"]), "evening peak"
        if moment.weekday() >= 5:
            return float(config["weekend_multiplier"]), "weekend"
        return 1.0, "standard hours"

    def effective_rate(
        self,
        facility: Facility,
        *,
        occupancy: float,
        moment: datetime | None = None,
        slot_type: str = SlotType.STANDARD,
        forecast_occupancy: float | None = None,
    ) -> tuple[int, float, dict]:
        """Return (rate in minor units per hour, multiplier, component breakdown)."""
        config = self.config(facility)
        moment = moment or datetime.now(UTC)

        components: dict = {"occupancy_pct": round(occupancy * 100, 1)}

        if not facility.dynamic_pricing_enabled:
            components["dynamic_pricing"] = "disabled — flat rate"
            return facility.base_rate_minor_per_hour, 1.0, components

        occ_mult = self.occupancy_multiplier(occupancy, config)
        components["occupancy"] = round(occ_mult, 3)

        time_mult, time_label = self.time_multiplier(moment, config)
        components["time"] = round(time_mult, 3)
        components["time_label"] = time_label

        slot_mult = SLOT_TYPE_MULTIPLIER.get(slot_type, 1.0)
        components["slot_type"] = round(slot_mult, 3)

        forecast_mult = 1.0
        if forecast_occupancy is not None:
            # Nudge the price ahead of a predicted surge, but only upward and
            # only gently — a wrong forecast must not produce a wrong bill.
            delta = max(0.0, forecast_occupancy - occupancy)
            forecast_mult = 1.0 + float(config["forecast_sensitivity"]) * delta
            components["forecast"] = round(forecast_mult, 3)
            components["forecast_occupancy_pct"] = round(forecast_occupancy * 100, 1)

        multiplier = occ_mult * time_mult * slot_mult * forecast_mult
        multiplier = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier))
        components["multiplier_final"] = round(multiplier, 3)

        rate = int(round(facility.base_rate_minor_per_hour * multiplier))
        return rate, multiplier, components

    # ── Bill ──────────────────────────────────────────────────

    def billable_minutes(self, facility: Facility, raw_minutes: float) -> int:
        """Free grace period, then round up to the billing increment."""
        chargeable = raw_minutes - facility.free_minutes
        if chargeable <= 0:
            return 0
        increment = max(1, facility.billing_increment_minutes)
        return int(math.ceil(chargeable / increment) * increment)

    def quote(
        self,
        facility: Facility,
        *,
        raw_minutes: float,
        occupancy: float,
        entry_at: datetime | None = None,
        exit_at: datetime | None = None,
        slot_type: str = SlotType.STANDARD,
        forecast_occupancy: float | None = None,
        waive: bool = False,
    ) -> PriceQuote:
        config = self.config(facility)
        exit_at = exit_at or datetime.now(UTC)
        currency = facility.currency

        minutes = self.billable_minutes(facility, raw_minutes)
        rate, multiplier, components = self.effective_rate(
            facility, occupancy=occupancy, moment=exit_at,
            slot_type=slot_type, forecast_occupancy=forecast_occupancy,
        )

        if waive or minutes == 0:
            reason = "authorised vehicle — charges waived" if waive else (
                f"within the {facility.free_minutes}-minute free period"
            )
            return PriceQuote(
                base_rate_minor=facility.base_rate_minor_per_hour,
                effective_rate_minor=rate, multiplier=multiplier,
                billable_minutes=minutes, subtotal_minor=0, discount_minor=0,
                tax_minor=0, total_minor=0, currency=currency,
                components={**components, "waived": bool(waive)},
                explanation=reason,
            )

        # Declining block tariff: hours past the threshold bill at a discount.
        threshold_min = int(config["long_stay_discount_after_hours"]) * 60
        standard_minutes = min(minutes, threshold_min)
        extended_minutes = max(0, minutes - threshold_min)
        long_stay_rate = float(config["long_stay_discount"])

        subtotal = rate * standard_minutes / 60.0
        extended_full = rate * extended_minutes / 60.0
        extended_charged = extended_full * long_stay_rate
        discount = extended_full - extended_charged
        subtotal_total = subtotal + extended_full

        if extended_minutes:
            components["long_stay_hours"] = round(extended_minutes / 60, 2)
            components["long_stay_rate"] = long_stay_rate

        subtotal_minor = int(round(subtotal_total))
        discount_minor = int(round(discount))
        net = subtotal_minor - discount_minor

        # Daily cap applies to the pre-tax net.
        if facility.daily_cap_minor and net > facility.daily_cap_minor:
            discount_minor += net - facility.daily_cap_minor
            net = facility.daily_cap_minor
            components["daily_cap_applied"] = True

        tax_minor = int(round(net * facility.tax_percent / 100.0))
        total_minor = net + tax_minor

        # Name every factor that actually moved the price. A bill that says only
        # "1.44x base" invites a dispute; one that says which three things pushed
        # it there usually answers the question before it is asked.
        drivers: list[str] = []
        if components.get("occupancy", 1.0) > 1.001:
            drivers.append(f"lot {occupancy:.0%} full")
        if components.get("time", 1.0) != 1.0:
            drivers.append(str(components.get("time_label", "time of day")))
        if components.get("slot_type", 1.0) != 1.0:
            drivers.append(f"{slot_type.replace('_', ' ')} bay")
        if components.get("forecast", 1.0) > 1.001:
            drivers.append("predicted surge")

        hours = minutes / 60.0
        explanation = (
            f"{hours:.2f} h billable at {rate / 100:.2f} {currency}/h "
            f"({multiplier:.2f}x base"
        )
        explanation += f" — {', '.join(drivers)})" if drivers else ")"
        if discount_minor:
            explanation += f", less {discount_minor / 100:.2f} {currency} discount"
        explanation += f", plus {facility.tax_percent:.0f}% tax."

        return PriceQuote(
            base_rate_minor=facility.base_rate_minor_per_hour,
            effective_rate_minor=rate,
            multiplier=multiplier,
            billable_minutes=minutes,
            subtotal_minor=subtotal_minor,
            discount_minor=discount_minor,
            tax_minor=tax_minor,
            total_minor=total_minor,
            currency=currency,
            components=components,
            explanation=explanation,
        )

    def estimate(
        self, facility: Facility, *, hours: float, occupancy: float,
        slot_type: str = SlotType.STANDARD,
    ) -> PriceQuote:
        """Forward-looking quote shown before the driver commits to parking."""
        return self.quote(
            facility,
            raw_minutes=hours * 60 + facility.free_minutes,
            occupancy=occupancy,
            slot_type=slot_type,
        )


pricing_engine = PricingEngine()
