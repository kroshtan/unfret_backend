from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


class CustomAPIError(Exception):
    """Base exception class for service API errors."""

    detail: str
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"

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
        )


class BadRequestError(CustomAPIError):
    """Client-side request error."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "BAD_REQUEST"


class ForbiddenError(CustomAPIError):
    """Authenticated but not allowed."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "FORBIDDEN"


class NotFoundError(CustomAPIError):
    """Resource not found."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


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
