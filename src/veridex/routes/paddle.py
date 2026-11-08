import hashlib
import hmac
import json
import time

import structlog
from fastapi import APIRouter, Header, Request

from veridex.config import settings
from veridex.schemas.errors import BadRequestError, InternalServerError
from veridex.schemas.responses import OkResponse

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/paddle", tags=["Paddle"])

# Reject signatures older than this to limit replay of captured webhooks.
_SIGNATURE_TOLERANCE_S = 5 * 60


def verify_signature(raw_body: bytes, signature_header: str, secret: str, now: float | None = None) -> bool:
    """
    Verify a Paddle Billing webhook signature.

    Paddle signs ``{ts}:{raw_body}`` using HMAC-SHA256 with the webhook secret. The
    ``Paddle-Signature`` header has the form ``ts=<unix timestamp>;h1=<hex digest>``.

    :param raw_body: The raw request body bytes.
    :param signature_header: The value of the ``Paddle-Signature`` header.
    :param secret: The webhook signing secret.
    :param now: Current unix time (injectable for tests).
    :return: ``True`` if the signature is valid and recent.
    """
    try:
        parts = dict(part.split("=", 1) for part in signature_header.split(";"))
        ts, h1 = parts["ts"], parts["h1"]
        age = (time.time() if now is None else now) - int(ts)
    except ValueError, KeyError:
        return False
    if abs(age) > _SIGNATURE_TOLERANCE_S:
        return False

    expected = hmac.new(secret.encode(), f"{ts}:".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


@router.post("/webhook", include_in_schema=False, response_model=OkResponse)
async def paddle_webhook(
    request: Request,
    paddle_signature: str = Header(..., alias="Paddle-Signature"),
) -> OkResponse:
    """
    Receive and process Paddle Billing webhook events.

    Handled events:

    * ``subscription.activated`` – upgrades a free/premium account to ``premium``.
    * ``subscription.canceled``  – reverts the account to ``free`` if the canceled
      subscription is the one currently linked (fires at the end of the billing period).

    Admin and blocked accounts are never changed by webhooks. The Paddle checkout must
    pass ``customData: { username: "<username>" }`` so we know which account to update.

    :param request: The raw FastAPI request (body read for signature verification).
    :param paddle_signature: Value of the ``Paddle-Signature`` header.
    :return: ``{"ok": true}`` on success.
    :raises InternalServerError: If no webhook secret is configured.
    :raises BadRequestError: If the signature is invalid.
    """
    if not settings.paddle_webhook_secret:
        logger.error("paddle_webhook_secret_not_configured")
        raise InternalServerError("Webhook verification is not configured.")

    raw_body = await request.body()
    if not verify_signature(raw_body, paddle_signature, settings.paddle_webhook_secret):
        raise BadRequestError("Invalid signature.")

    event = json.loads(raw_body)
    event_type: str = event.get("event_type", "")
    data: dict = event.get("data", {})
    custom_data: dict = data.get("custom_data") or {}
    username = str(custom_data.get("username") or "").lower()
    subscription_id = data.get("id")

    if not username:
        logger.warning("paddle_webhook_missing_username", event_type=event_type)
        return OkResponse()

    pool = request.app.db_pool

    if event_type == "subscription.activated":
        await pool.execute(
            "UPDATE accounts SET subscription_status = 'premium', paddle_subscription_id = $2 "
            "WHERE username = $1 AND subscription_status IN ('free', 'premium')",
            username,
            subscription_id,
        )
        logger.info("subscription_upgraded", username=username)

    elif event_type == "subscription.canceled":
        await pool.execute(
            "UPDATE accounts SET subscription_status = 'free', paddle_subscription_id = NULL "
            "WHERE username = $1 AND subscription_status = 'premium' AND paddle_subscription_id = $2",
            username,
            subscription_id,
        )
        logger.info("subscription_downgraded", username=username)

    return OkResponse()
