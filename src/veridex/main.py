import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from veridex import (
    __version__,
    fact_cache,
    image_store,
    log,
    price_store,
    reports,
    scoring,
    shop_store,
    verdict_store,
)
from veridex.api import VeridexAPI
from veridex.config import settings
from veridex.credits import CreditsDoor, fiat, vat
from veridex.db import PostgresFactStore, create_pool, init_db
from veridex.graph import SKILLS, build_graph
from veridex.graph.skills import reverse_image
from veridex.middleware import RequestGuardMiddleware
from veridex.routes import router
from veridex.routes.pages import STATIC_DIR
from veridex.schemas.errors import CustomAPIError, handle_http_error, handle_validation_error
from veridex.x402.service import X402
from veridex.x402.verdict import skills_for

log.configure(json=settings.log_json)
logger = structlog.get_logger("veridex")


async def housekeeping(pool: asyncpg.Pool, interval_s: float = 3600.0) -> None:
    """
    Every hour until cancelled: expire old reports, fiat order rows, VAT quotes and sales records.

    Unhandled reports go with their account links; sales records only after their ten-year retention.

    :param pool: The database pool.
    :param interval_s: Seconds between rounds.
    """
    while True:
        for name, job in (
            ("reports", reports.expire(pool, settings)),
            ("fiat", fiat.purge(pool, settings.fiat_retention_days)),
            ("vat", vat.purge(pool, settings)),
        ):
            try:
                await job
            except Exception as exc:  # noqa: BLE001 - retried next round
                logger.warning("housekeeping_failed", job=name, error=type(exc).__name__)
        await asyncio.sleep(interval_s)


@asynccontextmanager
async def lifespan(api: VeridexAPI) -> AsyncGenerator[None]:
    """Build the analysis graph and open the DB pool for the duration of the app's life."""
    logger.info("Unfret backend service starting", version=__version__, config=settings.summary())
    rates_age = (datetime.now(UTC).date() - vat.rates_checked_on()).days
    if rates_age > 45:  # `make vat-rates-check` compares the vendored table with the EU's TEDB
        logger.warning("vat_rates_stale", checked_days_ago=rates_age)
    api.graph = build_graph(SKILLS)
    api.quick_graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False)
    api.db_pool = await create_pool()
    await init_db(api.db_pool)
    # Store-level facts persist in Postgres, shared across restarts and workers.
    fact_cache.use_store(PostgresFactStore(api.db_pool))
    price_store.use_store(price_store.PostgresPriceStore(api.db_pool))
    shop_store.use_store(shop_store.PostgresShopStore(api.db_pool))
    reverse_image.use_store(image_store.PostgresImageStore(api.db_pool))
    verdict_store.use_store(verdict_store.PostgresVerdictStore(api.db_pool))
    api.x402 = X402(settings)
    # Credits are bought by paying the same hot wallet; the scanner turns finalized transfers into balance.
    api.credits = CreditsDoor(settings, api.x402.rpc, api.x402.hot.pubkey if api.x402.hot else None, api.x402.prices)
    api.credits.attach(api.db_pool)
    # Single instance: the sweep to cold is not coordinated across processes (the scanner is safe either way).
    tasks = [asyncio.create_task(api.x402.run_forever())] if api.x402.configured else []
    if api.credits.scanner is not None:
        tasks.append(asyncio.create_task(api.credits.run_forever()))
    tasks.append(asyncio.create_task(housekeeping(api.db_pool)))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(TimeoutError):  # fingerprint writes still in flight
            async with asyncio.timeout(5):
                await shop_store.drain()
                await reverse_image.drain()
        await api.db_pool.close()
        await api.x402.rpc.aclose()
    logger.info("Unfret backend service shutting down")


app = VeridexAPI(
    title="Unfret API",
    version=__version__,
    lifespan=lifespan,
)

# Body size cap and x-process-time header for all requests (added first, so CORS wraps it
# and even a 413 carries CORS headers)
app.add_middleware(RequestGuardMiddleware, max_body_bytes=settings.max_page_bytes)

# The API is called from the browser extension (chrome-extension:// and moz-extension:// origins) and from
# agents, and uses bearer tokens rather than cookies, so a wildcard origin without credentials is safe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "PAYMENT-SIGNATURE"],  # Authorization: sessions and API keys
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
)


# Include API routes, and the scripts and styles of the /connect and /checkout pages
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Add custom exception handlers
app.add_exception_handler(CustomAPIError, CustomAPIError.handle)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.add_exception_handler(StarletteHTTPException, handle_http_error)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "veridex.main:app",
        host="0.0.0.0",  # noqa: S104 - in a container, behind the proxy
        port=int(os.getenv("PORT", "8080")),
        reload=bool(os.getenv("RELOAD")),
    )
