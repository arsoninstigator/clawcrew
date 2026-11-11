"""exception ladder. every error carries a stable machine code the api maps to
an http status, so clients never have to string-match messages."""

from __future__ import annotations


class ClawCrewError(Exception):
    code = "clawcrew_error"
    status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFound(ClawCrewError):
    code = "not_found"
    status = 404


class ValidationError(ClawCrewError):
    code = "validation_error"
    status = 422


class Conflict(ClawCrewError):
    code = "conflict"
    status = 409


class NotAllowed(ClawCrewError):
    code = "not_allowed"
    status = 403


class ProviderNotConfigured(ClawCrewError):
    """raised when a sandbox/scheduler backend has no credentials."""

    code = "provider_not_configured"
    status = 503


class ProviderFailure(ClawCrewError):
    code = "provider_failure"
    status = 502
