from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException


class CustomAPIError(Exception):
    """Base exception class for service API errors."""

    detail: str
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"
    headers: dict[str, str] | None = None

    def __init__(
        self,
        detail: str,
        metadata: Any | None = None,
        error_code: str | None = None,
    ) -> None:
        """Initialize the CustomAPIError."""
        self.detail = detail
        self.metadata = metadata
        if error_code:
            self.error_code = error_code
        super().__init__(detail)

    @staticmethod
    def handle(_: Request, exc: Exception) -> Response:
        """
        Render a :class:`CustomAPIError` as a JSON error response.

        Registered as the FastAPI exception handler for this class and its subclasses.

        :param exc: The raised exception (always a ``CustomAPIError``).
        :return: JSON response with ``detail``, ``error_code`` and optional ``metadata``.
        """
        assert isinstance(exc, CustomAPIError)  # noqa: S101
        content: dict[str, Any] = {
            "detail": exc.detail,
            "error_code": exc.error_code,
        }
        if exc.metadata is not None:
            content["metadata"] = exc.metadata

        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(content),
            headers=exc.headers,
        )


class BadRequestError(CustomAPIError):
    """Client-side request error."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "BAD_REQUEST"


class UnauthorizedError(CustomAPIError):
    """Missing, invalid or expired session."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "UNAUTHORIZED"
    headers = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012 - class-level constant, never mutated


class InsufficientCreditsError(CustomAPIError):
    """The wallet's balance doesn't cover the analysis."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    error_code = "INSUFFICIENT_CREDITS"


class ForbiddenError(CustomAPIError):
    """Authenticated but not allowed."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "FORBIDDEN"


class NotFoundError(CustomAPIError):
    """Resource not found."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


class ConflictError(CustomAPIError):
    """The resource is in a state that doesn't allow this (e.g. a pairing that's already signed in)."""

    status_code = status.HTTP_409_CONFLICT
    error_code = "CONFLICT"


class GoneError(CustomAPIError):
    """The resource existed but has expired."""

    status_code = status.HTTP_410_GONE
    error_code = "GONE"


class TooManyRequestsError(CustomAPIError):
    """Usage quota exhausted."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "TOO_MANY_REQUESTS"


class InternalServerError(CustomAPIError):
    """Internal server error."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "INTERNAL_ERROR"


class GatewayTimeoutError(CustomAPIError):
    """An upstream dependency (the analysis pipeline) took too long."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "GATEWAY_TIMEOUT"


class ServiceUnavailableError(CustomAPIError):
    """A dependency (such as the database) is not available yet."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "SERVICE_UNAVAILABLE"


_HTTP_CODES = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def handle_validation_error(_: Request, exc: Exception) -> Response:
    """
    Render a request validation failure with an ``error_code``, like every other error.

    The offending input is left out: it can be a whole page of HTML.

    :param exc: The raised ``RequestValidationError``.
    :return: 422 with ``detail``, ``error_code: VALIDATION_ERROR`` and ``errors`` (``loc``, ``msg``, ``type``).
    """
    raw = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in raw
    ]
    first = errors[0] if errors else {"loc": [], "msg": "invalid request"}
    where = ".".join(first["loc"][1:]) or "body"
    return JSONResponse(
        status_code=422,
        content={
            "detail": f"Invalid request: {where}: {first['msg']}",
            "error_code": "VALIDATION_ERROR",
            "errors": errors,
        },
    )


def handle_http_error(_: Request, exc: Exception) -> Response:
    """
    Render a framework HTTP error (unknown route, wrong method, …) with an ``error_code``.

    :param exc: The raised ``HTTPException``.
    :return: The same status with ``detail`` and ``error_code``.
    """
    code = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else "Internal error."
    headers = exc.headers if isinstance(exc, StarletteHTTPException) else None
    return JSONResponse(
        status_code=code,
        content={"detail": str(detail), "error_code": _HTTP_CODES.get(code, "HTTP_ERROR")},
        headers=headers,
    )
