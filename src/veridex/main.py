import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from veridex import __version__, fact_cache, scoring, tineye
from veridex.api import VeridexAPI
from veridex.config import settings
from veridex.credits import CreditsDoor
from veridex.db import PostgresBudget, PostgresFactStore, create_pool, init_db
from veridex.graph import SKILLS, build_graph
from veridex.middleware import RequestGuardMiddleware
from veridex.routes import router
from veridex.schemas.errors import CustomAPIError
from veridex.x402.service import X402
from veridex.x402.verdict import skills_for

logger = structlog.get_logger("veridex")


@asynccontextmanager
async def lifespan(api: VeridexAPI) -> AsyncGenerator[None]:
    """Build the analysis graph and open the DB pool for the duration of the app's life."""
    logger.info("Veridex backend service starting")
    api.graph = build_graph(SKILLS)
    api.quick_graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH)
    api.db_pool = await create_pool()
    await init_db(api.db_pool)
    # Store-level facts and the TinEye spending cap persist in Postgres, shared across restarts and workers.
    fact_cache.use_store(PostgresFactStore(api.db_pool))
    tineye.use_budget(PostgresBudget(api.db_pool, "tineye"))
    api.x402 = X402(settings)
    # Credits are bought by paying the same hot wallet; the scanner turns finalized transfers into balance.
    api.credits = CreditsDoor(settings, api.x402.rpc, api.x402.hot.pubkey if api.x402.hot else None)
    api.credits.attach(api.db_pool)
    # Single instance: the sweep to cold is not coordinated across processes (the scanner is safe either way).
    tasks = [asyncio.create_task(api.x402.run_forever())] if api.x402.configured else []
    if api.credits.scanner is not None:
        tasks.append(asyncio.create_task(api.credits.run_forever()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await api.db_pool.close()
    logger.info("Veridex backend service shutting down")


app = VeridexAPI(
    title="Veridex Backend Service",
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
    allow_headers=["Content-Type", "Authorization", "PAYMENT-SIGNATURE"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
)


# Redirect root to always see the API docs
@app.get("/", include_in_schema=False)
async def docs_redirect() -> RedirectResponse:
    """Redirect to the API documentation."""
    return RedirectResponse(url="/docs")


# Include API routes
app.include_router(router)

# Add custom exception handlers
app.add_exception_handler(CustomAPIError, CustomAPIError.handle)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "veridex.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=bool(os.getenv("RELOAD"))
    )
