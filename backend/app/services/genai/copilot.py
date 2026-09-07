"""Generative reporting and explanation.

Everything here follows the same pattern: gather hard facts from the database
first, hand the model *only* those facts, and ask it to write prose. The model
never queries anything itself and is told explicitly not to introduce numbers.
That keeps hallucination structurally unlikely rather than merely discouraged.

Each function has a deterministic template fallback that produces a genuinely
useful — if less fluent — result with no API key. The report always says which
path produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.facility import Facility
from app.models.parking import Anomaly, ParkingSession
from app.services.analytics import analytics_service
from app.services.genai.client import claude

log = get_logger(__name__)

GROUNDING_RULE = (
    "Use ONLY the figures in the DATA block. Never invent, extrapolate or round "
    "a number that is not there. If the data does not answer something, say it "
    "is not available. Write plain prose — no markdown headings, no bullet "
    "characters, no bold."
)


@dataclass(slots=True)
class Briefing:
    title: str
    body: str
    highlights: list[str]
    generated_by: str
    facts: dict

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "highlights": self.highlights,
            "generated_by": self.generated_by,
            "facts": self.facts,
            "generated_at": datetime.now(UTC).isoformat(),
        }


def _money(minor: int, currency: str = "INR") -> str:
    symbol = {"INR": "₹", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
    return f"{symbol}{minor / 100:,.2f}"


class Copilot:
    # ── Daily operations briefing ─────────────────────────────

    async def daily_briefing(
        self, db: AsyncSession, facility: Facility, *, days: int = 7
    ) -> Briefing:
        kpis = await analytics_service.kpis(db, facility, days=days)
        forecast = await analytics_service.forecast(db, facility, horizon_hours=6)
        total, occupied, ratio = await _occupancy(db, facility.id)
        anomalies = (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.facility_id == facility.id, Anomaly.resolved.is_(False))
                .order_by(Anomaly.created_at.desc())
                .limit(5)
            )
        ).scalars().all()
        allocation = await analytics_service.allocation_performance(db, facility.id, days=days)

        peak = max(forecast.points, key=lambda p: p["occupancy_pct"], default=None)

        facts = {
            "facility": facility.name,
            "now": {
                "capacity": total, "occupied": occupied,
                "free": total - occupied, "occupancy_pct": round(ratio * 100, 1),
            },
            "last_period": {
                "days": days,
                "sessions": kpis.sessions,
                "revenue": _money(kpis.revenue_minor, kpis.currency),
                "avg_stay_minutes": round(kpis.avg_stay_minutes, 1),
                "avg_ticket": _money(kpis.avg_ticket_minor, kpis.currency),
                "avg_walk_distance_m": round(kpis.avg_walk_distance_m, 1),
                "utilisation_pct": round(kpis.utilisation_pct, 1),
                "turnover_per_slot": round(kpis.turnover_per_slot, 2),
                "payment_success_rate_pct": round(kpis.payment_success_rate * 100, 1),
                "anpr_mean_confidence_pct": round(kpis.anpr_accuracy_pct, 1),
                "anpr_manual_review_rate_pct": round(kpis.anpr_review_rate * 100, 1),
            },
            "forecast": {
                "model": forecast.model,
                "note": forecast.note,
                "peak_hour": peak["hour"] if peak else None,
                "peak_occupancy_pct": round(peak["occupancy_pct"] * 100, 1) if peak else None,
                "peak_confidence": peak["confidence"] if peak else None,
            },
            "open_anomalies": [
                {"kind": a.kind, "severity": a.severity, "summary": a.summary}
                for a in anomalies
            ],
            "allocation_strategies": allocation.get("strategies", []),
        }

        if not claude.enabled:
            return self._template_briefing(facility, facts)

        prompt = (
            f"Write the daily operations briefing for the manager of {facility.name}, "
            f"a car park.\n\n"
            f"DATA:\n{json.dumps(facts, indent=2, default=str)}\n\n"
            "Structure: one short paragraph on how the site is running right now, one "
            "on the last period's performance, one on what to expect in the next six "
            "hours and what to do about it. Then a line 'Highlights:' followed by two "
            "to four single-sentence items, one per line, each starting with '- '.\n"
            "Be direct and specific. If the forecast came from the seasonal baseline "
            "rather than a trained model, say so — it is materially less reliable. "
            "Mention anomalies only if there are any."
        )
        response = await claude.complete(
            system=(
                "You are a parking operations analyst writing for a busy facility "
                "manager. You are precise, brief, and never flatter the numbers. "
                + GROUNDING_RULE
            ),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            thinking=False,
        )
        if response.degraded or not response.text:
            return self._template_briefing(facility, facts)

        body, highlights = _split_highlights(response.text)
        return Briefing(
            title=f"{facility.name} — operations briefing",
            body=body,
            highlights=highlights,
            generated_by=response.model,
            facts=facts,
        )

    def _template_briefing(self, facility: Facility, facts: dict) -> Briefing:
        now = facts["now"]
        last = facts["last_period"]
        forecast = facts["forecast"]

        body = (
            f"{facility.name} is {now['occupancy_pct']}% full, with {now['free']} of "
            f"{now['capacity']} bays free.\n\n"
            f"Over the last {last['days']} days the site handled {last['sessions']} "
            f"sessions for {last['revenue']}, averaging {last['avg_ticket']} per visit "
            f"and a {last['avg_stay_minutes']:.0f}-minute stay. Mean utilisation was "
            f"{last['utilisation_pct']}% and each bay turned over "
            f"{last['turnover_per_slot']} times. Plate recognition ran at "
            f"{last['anpr_mean_confidence_pct']}% mean confidence with "
            f"{last['anpr_manual_review_rate_pct']}% of reads flagged for review.\n\n"
        )
        if forecast["peak_hour"] is not None:
            body += (
                f"Occupancy is projected to peak around {forecast['peak_hour']:02d}:00 at "
                f"{forecast['peak_occupancy_pct']}%"
            )
            if forecast["model"] == "seasonal_naive":
                body += (
                    " — from the seasonal baseline, not a trained model, so treat it "
                    "as indicative only."
                )
            else:
                body += "."

        highlights = [
            f"{now['free']} bays free right now ({now['occupancy_pct']}% occupied).",
            f"{last['sessions']} sessions and {last['revenue']} over {last['days']} days.",
        ]
        if last["payment_success_rate_pct"] < 98:
            highlights.append(
                f"Payment success is {last['payment_success_rate_pct']}% — worth investigating."
            )
        if facts["open_anomalies"]:
            highlights.append(
                f"{len(facts['open_anomalies'])} unresolved anomalies need review."
            )

        return Briefing(
            title=f"{facility.name} — operations briefing",
            body=body.strip(),
            highlights=highlights,
            generated_by="deterministic_template (no ANTHROPIC_API_KEY configured)",
            facts=facts,
        )

    # ── Bill explanation ──────────────────────────────────────

    async def explain_bill(self, db: AsyncSession, session: ParkingSession) -> dict:
        facility = await db.get(Facility, session.facility_id)
        breakdown = session.rate_breakdown or {}
        facts = {
            "plate": session.plate,
            "facility": facility.name if facility else "",
            "entry": session.entry_at.strftime("%d %b %Y, %H:%M"),
            "exit": session.exit_at.strftime("%d %b %Y, %H:%M") if session.exit_at else None,
            "total_minutes": round(session.elapsed_minutes(session.exit_at), 1),
            "free_minutes": facility.free_minutes if facility else 0,
            "billable_minutes": session.billable_minutes,
            "base_rate": _money(breakdown.get("base_rate_minor", 0), session.currency),
            "effective_rate": _money(breakdown.get("effective_rate_minor", 0), session.currency),
            "multiplier": breakdown.get("multiplier"),
            "subtotal": _money(session.subtotal_minor, session.currency),
            "discount": _money(session.discount_minor, session.currency),
            "tax": _money(session.tax_minor, session.currency),
            "total": _money(session.total_minor, session.currency),
            "components": breakdown.get("components", {}),
            "payment_status": session.payment_status,
        }

        if not claude.enabled:
            text = (
                f"You parked at {facts['facility']} from {facts['entry']} to "
                f"{facts['exit']}, a total of {facts['total_minutes']:.0f} minutes. "
                f"The first {facts['free_minutes']} minutes were free, leaving "
                f"{facts['billable_minutes']} chargeable minutes at "
                f"{facts['effective_rate']} per hour. That came to {facts['subtotal']}"
            )
            if session.discount_minor:
                text += f", less {facts['discount']} in discounts"
            text += f", plus {facts['tax']} tax — a total of {facts['total']}."
            return {"explanation": text, "generated_by": "deterministic_template", "facts": facts}

        response = await claude.complete(
            system=(
                "You explain parking bills to drivers. Warm, plain, three or four "
                "sentences. If a surge multiplier above 1.0 was applied, say why in "
                "one clause. " + GROUNDING_RULE
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Explain this parking bill to the driver who received it.\n\n"
                        f"DATA:\n{json.dumps(facts, indent=2, default=str)}"
                    ),
                }
            ],
            max_tokens=500,
            fast=True,
        )
        return {
            "explanation": response.text or "Explanation unavailable.",
            "generated_by": response.model,
            "facts": facts,
        }

    # ── Automation-rule drafting ──────────────────────────────

    async def suggest_automations(self, db: AsyncSession, facility: Facility) -> dict:
        from app.services.automation.engine import ACTION_REGISTRY, TRIGGERS

        kpis = await analytics_service.kpis(db, facility, days=14)
        facts = {
            "facility": facility.name,
            "access_mode": facility.access_mode,
            "dynamic_pricing_enabled": facility.dynamic_pricing_enabled,
            "overstay_after_hours": facility.overstay_after_hours,
            "kpis": kpis.as_dict(),
            "available_triggers": TRIGGERS,
            "available_actions": sorted(ACTION_REGISTRY),
        }

        if not claude.enabled:
            return {
                "suggestions": _default_rule_suggestions(facility),
                "generated_by": "deterministic_template",
                "facts": facts,
            }

        response = await claude.complete(
            system=(
                "You design automation rules for a parking platform. Return ONLY a "
                "JSON array. Each element must be an object with keys: name, "
                "description, trigger, conditions, actions. `trigger` must be one of "
                "available_triggers and every action name must be one of "
                "available_actions. `conditions` is an object of field/value checks; "
                "`actions` is an array of objects with `action` and `params`. "
                "Propose two to four rules that this specific facility would "
                "genuinely benefit from, based on its KPIs. No prose, no code fences."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"DATA:\n{json.dumps(facts, indent=2, default=str)}",
                }
            ],
            max_tokens=1500,
        )
        try:
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].removeprefix("json").strip()
            suggestions = json.loads(text)
            if not isinstance(suggestions, list):
                raise ValueError("expected a JSON array")
        except Exception as exc:
            log.warning("could not parse rule suggestions", extra={"error": str(exc)})
            return {
                "suggestions": _default_rule_suggestions(facility),
                "generated_by": "deterministic_template (model output was unparseable)",
                "facts": facts,
            }

        # Never trust generated action names — validate before offering them.
        valid = [
            s for s in suggestions
            if s.get("trigger") in TRIGGERS
            and all(a.get("action") in ACTION_REGISTRY for a in s.get("actions", []))
        ]
        return {
            "suggestions": valid,
            "rejected": len(suggestions) - len(valid),
            "generated_by": response.model,
            "facts": facts,
        }

    # ── Anomaly triage ────────────────────────────────────────

    async def triage_anomalies(self, db: AsyncSession, facility: Facility) -> dict:
        rows = (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.facility_id == facility.id, Anomaly.resolved.is_(False))
                .order_by(Anomaly.created_at.desc())
                .limit(25)
            )
        ).scalars().all()
        if not rows:
            return {
                "summary": "No unresolved anomalies at this facility.",
                "groups": [], "generated_by": "no_data",
            }

        by_kind: dict[str, list[Anomaly]] = {}
        for anomaly in rows:
            by_kind.setdefault(anomaly.kind, []).append(anomaly)
        groups = [
            {
                "kind": kind,
                "count": len(items),
                "worst_severity": max(i.severity for i in items),
                "examples": [i.summary for i in items[:3]],
            }
            for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
        ]

        if not claude.enabled:
            return {
                "summary": (
                    f"{len(rows)} unresolved anomalies across {len(groups)} categories. "
                    f"Largest group: {groups[0]['kind']} ({groups[0]['count']})."
                ),
                "groups": groups,
                "generated_by": "deterministic_template",
            }

        response = await claude.complete(
            system=(
                "You triage security and billing alerts for a parking operator. "
                "Say what is happening, which group to deal with first, and what the "
                "operator should actually do. Four sentences maximum. " + GROUNDING_RULE
            ),
            messages=[
                {"role": "user", "content": f"DATA:\n{json.dumps(groups, indent=2, default=str)}"}
            ],
            max_tokens=400,
            fast=True,
        )
        return {
            "summary": response.text or "Triage unavailable.",
            "groups": groups,
            "generated_by": response.model,
        }


async def _occupancy(db: AsyncSession, facility_id: int) -> tuple[int, int, float]:
    from app.services.parking import parking_service

    return await parking_service.occupancy(db, facility_id)


def _split_highlights(text: str) -> tuple[str, list[str]]:
    marker = "highlights:"
    lowered = text.lower()
    if marker not in lowered:
        return text.strip(), []
    index = lowered.rindex(marker)
    body = text[:index].strip()
    highlights = [
        line.strip().lstrip("-•* ").strip()
        for line in text[index + len(marker):].splitlines()
        if line.strip()
    ]
    return body, [h for h in highlights if h]


def _default_rule_suggestions(facility: Facility) -> list[dict]:
    return [
        {
            "name": "Alert on overstay",
            "description": (
                f"Notify the driver when a vehicle passes "
                f"{facility.overstay_after_hours} hours on site."
            ),
            "trigger": "schedule:tick",
            "conditions": {"overstay_hours": facility.overstay_after_hours},
            "actions": [{"action": "flag_overstays", "params": {}}],
        },
        {
            "name": "Recompute price on occupancy change",
            "description": "Refresh the dynamic rate whenever occupancy shifts materially.",
            "trigger": "facility.occupancy_changed",
            "conditions": {"occupancy_pct_gte": 70},
            "actions": [{"action": "recompute_pricing", "params": {}}],
        },
        {
            "name": "Release stale reservations",
            "description": "Free bays held for vehicles that never arrived.",
            "trigger": "schedule:tick",
            "conditions": {},
            "actions": [{"action": "release_stale_reservations", "params": {}}],
        },
        {
            "name": "Nightly wallet reconciliation",
            "description": "Recompute every wallet balance from its ledger and report drift.",
            "trigger": "schedule:tick",
            "conditions": {"hour": 2},
            "actions": [{"action": "reconcile_wallets", "params": {}}],
        },
    ]


copilot = Copilot()
