"""Multi-channel notification dispatch.

Every send is persisted as a `Notification` row first, so the driver's in-app
inbox is correct even when SMS or email delivery fails. External channels are
best-effort by design: a failed SMS must never roll back a settled parking bill.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from app.core.config import settings
from app.core.events import Topic, bus
from app.core.logging import get_logger
from app.models.enums import NotificationChannel
from app.models.user import Notification, User

log = get_logger(__name__)


class NotificationService:
    async def send(
        self,
        db,
        user: User,
        *,
        title: str,
        body: str,
        channel: str = NotificationChannel.IN_APP,
        meta: dict[str, Any] | None = None,
    ) -> Notification:
        notification = Notification(
            user_id=user.id, channel=channel, title=title, body=body,
            status="sent", meta=meta or {},
        )
        db.add(notification)
        await db.flush()

        delivered = True
        if channel == NotificationChannel.SMS and user.phone:
            delivered = await self._send_sms(user.phone, f"{title}\n{body}")
        elif channel == NotificationChannel.EMAIL:
            delivered = await self._send_email(user.email, title, body)

        if not delivered:
            notification.status = "failed"
            await db.flush()

        await bus.emit(
            Topic.NOTIFICATION,
            {
                "notification_id": notification.id, "channel": channel,
                "title": title, "body": body, "delivered": delivered,
            },
            user_id=user.id,
        )
        return notification

    async def _send_sms(self, to: str, message: str) -> bool:
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            log.info("sms skipped — twilio not configured", extra={"to": to[-4:]})
            return False
        url = (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                    data={"To": to, "From": settings.twilio_from_number, "Body": message},
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            log.warning("sms delivery failed", extra={"error": str(exc)})
            return False

    async def _send_email(self, to: str, subject: str, body: str) -> bool:
        if not settings.smtp_host:
            log.info("email skipped — smtp not configured", extra={"to": to})
            return False
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_user or "noreply@smartpark.app"
        message["To"] = to
        message.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            log.warning("email delivery failed", extra={"error": str(exc)})
            return False


notifier = NotificationService()
