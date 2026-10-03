"""Typed connector errors.

Every failure the agent can see is an AppError subclass with a stable code, a
human message, and an optional hint telling the agent what to do next. The MCP
layer turns these into tool error results; nothing else crosses that boundary.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False

    def __init__(self, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_payload(self, correlation_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        if correlation_id:
            payload["correlation_id"] = correlation_id
        return payload

    def to_json(self, correlation_id: str | None = None) -> str:
        return json.dumps(self.to_payload(correlation_id))


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND

    def __init__(self, resource: str, identifier: str, *, hint: str | None = None) -> None:
        super().__init__(
            f"{resource} '{identifier}' not found",
            hint=hint,
            details={"resource": resource, "identifier": identifier},
        )


class AuthRequiredError(AppError):
    code = ErrorCode.AUTH_REQUIRED

    def __init__(self, message: str = "Zoho authorization missing or expired") -> None:
        super().__init__(
            message,
            hint="A human must run `zoho-connector auth login` on the connector host. Do not retry.",
        )


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED
    retryable = True

    def __init__(self, message: str = "Zoho rate limit hit", *, retry_after: float | None = None) -> None:
        super().__init__(
            message,
            hint="Do not retry in a loop. Wait retry_after_seconds (if given) before calling Zoho tools again, and tell the user if it is long.",
            details={"retry_after_seconds": retry_after} if retry_after is not None else None,
        )
        self.retry_after = retry_after


class DailyBudgetExhaustedError(AppError):
    code = ErrorCode.DAILY_BUDGET_EXHAUSTED

    def __init__(self, limit: int, resets_in_seconds: int) -> None:
        super().__init__(
            f"Daily Zoho API budget of {limit} requests used up",
            hint="Stop calling Zoho tools; tell the user the connector is out of quota until the budget resets.",
            details={"limit": limit, "resets_in_seconds": resets_in_seconds},
        )


class UpstreamError(AppError):
    code = ErrorCode.UPSTREAM_ERROR


class ServiceUnavailableError(AppError):
    code = ErrorCode.SERVICE_UNAVAILABLE
    retryable = True
