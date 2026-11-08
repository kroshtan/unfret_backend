import structlog
from fastapi import APIRouter, Request

from veridex import __version__
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
    return Healthy(status="ready", version=__version__)
