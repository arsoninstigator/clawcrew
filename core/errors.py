"""exception ladder. every error carries a stable machine code the api maps to
an http status, so clients never have to string-match messages."""

from __future__ import annotations


class CritterTownError(Exception):
    code = "crittertown_error"
    status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFound(CritterTownError):
    code = "not_found"
    status = 404


class ValidationError(CritterTownError):
    code = "validation_error"
    status = 422


class Conflict(CritterTownError):
    code = "conflict"
    status = 409


class NotAllowed(CritterTownError):
    code = "not_allowed"
    status = 403


class ProviderNotConfigured(CritterTownError):
    """raised when a sandbox/scheduler backend has no credentials."""

    code = "provider_not_configured"
    status = 503


class ProviderFailure(CritterTownError):
    code = "provider_failure"
    status = 502
