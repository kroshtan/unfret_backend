import structlog
from fastapi import APIRouter, Request

from veridex import __version__
from veridex.auth import get_credits, is_admin
from veridex.routes.check import get_x402
from veridex.schemas.errors import ServiceUnavailableError
from veridex.schemas.responses import Healthy

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/health", tags=["Health"])


@router.get("", response_model=Healthy)
def health() -> Healthy:
    """
    Liveness probe. Always returns 200 while the process is serving requests.

    :return: Healthy response with the running version.
    """
    return Healthy(status="healthy", version=__version__)


@router.get("/ready", response_model=Healthy)
async def ready(request: Request) -> Healthy:
    """
    Readiness probe. Returns 200 only while the database answers, so a deploy can wait for it.

    Also reports whether x402 payments and card top-ups are available and how many alerts are raised; with the
    operator token (``Authorization: Bearer``), the hot wallet's balances and the alerts themselves. A low balance or
    a compromise signal pauses payments (``/v1/check`` answers 503) but never fails readiness: the rest of the API
    keeps serving.

    :param request: The FastAPI request (used to access the DB pool).
    :return: Healthy response with the running version.
    :raises ServiceUnavailableError: If the database pool is missing or the query fails.
    """
    pool = request.app.db_pool
    try:
        if pool is None or await pool.fetchval("SELECT 1") != 1:
            raise ServiceUnavailableError("Database not ready.")
    except ServiceUnavailableError:
        raise
    except Exception as exc:
        logger.warning("readiness_db_failed", error=type(exc).__name__)
        raise ServiceUnavailableError("Database not ready.") from exc
    x402 = await get_x402(request).health()
    door = get_credits(request)
    credits = "ok" if door.purchases_enabled else "sign_in" if door.sign_in_enabled else "off"
    provider = request.app.fiat
    report = Healthy(
        status="ready",
        version=__version__,
        x402=x402.status,
        x402_alert_count=len(x402.alerts),
        credits=credits,
        fiat="off" if provider is None else getattr(provider, "status", provider.name),
    )
    if is_admin(request):
        report.hot_sol_lamports = x402.sol_lamports
        report.hot_wsol_lamports = x402.wsol_lamports
        report.hot_usdc_units = x402.usdc_units
        report.x402_alerts = list(x402.alerts)
    return report
