"""Domain exceptions mapped to HTTP responses by a single handler in main.py."""

from __future__ import annotations


class SmartParkError(Exception):
    status_code = 400
    code = "smartpark_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(SmartParkError):
    status_code = 404
    code = "not_found"


class PermissionDenied(SmartParkError):
    status_code = 403
    code = "permission_denied"


class AuthenticationFailed(SmartParkError):
    status_code = 401
    code = "authentication_failed"


class Conflict(SmartParkError):
    status_code = 409
    code = "conflict"


class InsufficientFunds(SmartParkError):
    status_code = 402
    code = "insufficient_funds"


class NoSlotAvailable(SmartParkError):
    status_code = 409
    code = "no_slot_available"


class AccessDenied(SmartParkError):
    """Vehicle is not on the authorised list of a private facility."""

    status_code = 403
    code = "vehicle_not_authorised"


class RecognitionFailed(SmartParkError):
    status_code = 422
    code = "plate_recognition_failed"


class IntegrationError(SmartParkError):
    status_code = 502
    code = "integration_error"
