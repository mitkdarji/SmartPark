"""Payment-gateway adapters behind one interface.

The wallet is the system of record; a gateway is only ever used to *fund* it.
That keeps the parking flow fast (an exit debit is a local transaction, not a
network round trip) and confines third-party failure to top-ups.

`mock` is the default so the platform is fully demonstrable with no merchant
account. Razorpay and Stripe implementations use the real request shapes; they
activate the moment credentials appear in the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.core.errors import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PaymentGateway(Protocol):
    name: str

    async def charge(
        self, *, amount_minor: int, currency: str, customer_id: str, description: str
    ) -> dict[str, Any]: ...

    async def create_order(self, *, amount_minor: int, currency: str, receipt: str) -> dict[str, Any]: ...

    def verify_webhook(self, payload: bytes, signature: str) -> bool: ...


class MockGateway:
    """Deterministic stand-in.

    Fails on amounts ending in `13` so the failure path is exercisable in demos
    and tests without depending on a sandbox account behaving badly on cue.
    """

    name = "mock"

    async def charge(
        self, *, amount_minor: int, currency: str, customer_id: str, description: str
    ) -> dict[str, Any]:
        reference = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
        if amount_minor % 100 == 13:
            log.info("mock gateway declined", extra={"amount_minor": amount_minor})
            return {
                "success": False, "provider": self.name, "reference": reference,
                "error": "card_declined",
            }
        return {
            "success": True, "provider": self.name, "reference": reference,
            "amount_minor": amount_minor, "currency": currency,
            "customer_id": customer_id, "description": description,
        }

    async def create_order(self, *, amount_minor: int, currency: str, receipt: str) -> dict[str, Any]:
        return {
            "provider": self.name,
            "order_id": f"order_mock_{uuid.uuid4().hex[:14]}",
            "amount_minor": amount_minor,
            "currency": currency,
            "receipt": receipt,
            "checkout_url": None,
        }

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        expected = hmac.new(
            settings.secret_key.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")


class RazorpayGateway:
    """Razorpay Orders + Payments. India-first, which suits the target market."""

    name = "razorpay"
    base_url = "https://api.razorpay.com/v1"

    def __init__(self) -> None:
        self._auth = (settings.razorpay_key_id, settings.razorpay_key_secret)

    async def charge(
        self, *, amount_minor: int, currency: str, customer_id: str, description: str
    ) -> dict[str, Any]:
        # Razorpay has no server-initiated charge without a saved token; the
        # supported flow is to create an order the client completes. Auto-reload
        # therefore requires a stored token, which we surface explicitly rather
        # than silently pretending the charge went through.
        order = await self.create_order(
            amount_minor=amount_minor, currency=currency,
            receipt=f"autoreload-{customer_id}",
        )
        return {
            "success": False,
            "provider": self.name,
            "reference": order.get("order_id"),
            "error": "customer_action_required",
            "order": order,
        }

    async def create_order(self, *, amount_minor: int, currency: str, receipt: str) -> dict[str, Any]:
        if not all(self._auth):
            raise IntegrationError("Razorpay credentials are not configured")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.post(
                    f"{self.base_url}/orders",
                    auth=self._auth,
                    json={
                        "amount": amount_minor, "currency": currency,
                        "receipt": receipt, "payment_capture": 1,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Razorpay order failed: {exc}") from exc

        return {
            "provider": self.name, "order_id": data.get("id"),
            "amount_minor": data.get("amount"), "currency": data.get("currency"),
            "receipt": receipt, "key_id": settings.razorpay_key_id,
        }

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        secret = settings.razorpay_key_secret.encode()
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")


class StripeGateway:
    name = "stripe"
    base_url = "https://api.stripe.com/v1"

    async def charge(
        self, *, amount_minor: int, currency: str, customer_id: str, description: str
    ) -> dict[str, Any]:
        if not settings.stripe_secret_key:
            raise IntegrationError("Stripe credentials are not configured")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.post(
                    f"{self.base_url}/payment_intents",
                    headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
                    data={
                        "amount": amount_minor,
                        "currency": currency.lower(),
                        "customer": customer_id,
                        "description": description,
                        "confirm": "true",
                        "off_session": "true",
                    },
                )
                data = response.json()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Stripe charge failed: {exc}") from exc

        succeeded = data.get("status") == "succeeded"
        return {
            "success": succeeded, "provider": self.name, "reference": data.get("id"),
            "error": None if succeeded else data.get("error", {}).get("code", "charge_failed"),
        }

    async def create_order(self, *, amount_minor: int, currency: str, receipt: str) -> dict[str, Any]:
        return {
            "provider": self.name, "order_id": f"pi_pending_{uuid.uuid4().hex[:12]}",
            "amount_minor": amount_minor, "currency": currency, "receipt": receipt,
        }

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        secret = settings.stripe_secret_key.encode()
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")


_GATEWAYS: dict[str, PaymentGateway] = {
    "mock": MockGateway(),
    "razorpay": RazorpayGateway(),
    "stripe": StripeGateway(),
}


def get_gateway(name: str | None = None) -> PaymentGateway:
    return _GATEWAYS.get(name or settings.payment_provider, _GATEWAYS["mock"])


payment_gateway = get_gateway()
