import asyncio
import contextlib
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from os import PathLike

import asyncpg
import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Scope

from unfret import (
    __version__,
    ad_store,
    fact_cache,
    image_store,
    log,
    market_prices,
    mcp_remote,
    oauth,
    objections,
    price_store,
    recording,
    reports,
    review_store,
    scoring,
    shares,
    shop_store,
    verdict_store,
)
from unfret.api import UnfretAPI
from unfret.config import settings
from unfret.credits import CreditsDoor, affiliates, fiat, polar, vat
from unfret.db import PostgresFactStore, create_pool, init_db
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import reverse_image
from unfret.middleware import RequestGuardMiddleware
from unfret.reports import second_opinion
from unfret.routes import router
from unfret.routes.pages import STATIC_DIR
from unfret.schemas.errors import CustomAPIError, handle_http_error, handle_validation_error
from unfret.x402.base import BaseX402
from unfret.x402.service import X402
from unfret.x402.verdict import skills_for

log.configure(json=settings.log_json)
logger = structlog.get_logger("unfret")


async def housekeeping(pool: asyncpg.Pool, interval_s: float = 3600.0) -> None:
    """
    Every hour until cancelled: expire old reports, fiat orders, VAT quotes, sales records, shares, requests, OAuth.

    Unhandled reports go with their account links; sales records only after their ten-year retention.

    :param pool: The database pool.
    :param interval_s: Seconds between rounds.
    """
    while True:
        for name, job in (
            ("reports", reports.expire(pool, settings)),
            ("second_opinions", second_opinion.catch_up(pool, settings)),
            ("fiat", fiat.purge(pool, settings.fiat_retention_days)),
            ("vat", vat.purge(pool, settings)),
            ("affiliates", affiliates.purge(pool, settings)),
            ("shares", shares.purge(pool)),
            ("objections", objections.purge(pool, settings.objection_days)),
            ("oauth", oauth.purge(pool)),
        ):
            try:
                await job
            except Exception as exc:  # noqa: BLE001 - retried next round
                logger.warning("housekeeping_failed", job=name, error=type(exc).__name__)
        await asyncio.sleep(interval_s)


def use_postgres_stores(pool: asyncpg.Pool) -> None:
    """
    Keep store-level facts, prices, shop and review fingerprints, image prints and kept verdicts in Postgres.

    They are then shared across restarts and workers (each defaults to an in-memory store).

    :param pool: The database pool.
    """
    fact_cache.use_store(PostgresFactStore(pool))
    price_store.use_store(price_store.PostgresPriceStore(pool))
    market_prices.use_store(market_prices.PostgresMarketStore(pool))
    review_store.use_store(review_store.PostgresReviewStore(pool))
    ad_store.use_store(ad_store.PostgresAdStore(pool))
    shop_store.use_store(shop_store.PostgresShopStore(pool))
    reverse_image.use_store(image_store.PostgresImageStore(pool))
    verdict_store.use_store(verdict_store.PostgresVerdictStore(pool))


@asynccontextmanager
async def lifespan(api: UnfretAPI) -> AsyncGenerator[None]:
    """Build the analysis graph and open the DB pool for the duration of the app's life."""
    logger.info("Unfret backend service starting", version=__version__, config=settings.summary())
    rates_age = (datetime.now(UTC).date() - vat.rates_checked_on()).days
    if rates_age > 45:  # `make vat-rates-check` compares the vendored table with the EU's TEDB
        logger.warning("vat_rates_stale", checked_days_ago=rates_age)
    # A configured scorer (UNFRET_MODEL_PATH, UNFRET_MODEL_QUICK_PATH) that can't be loaded stops the start here.
    scorers = scoring.check_models({f"{s.name}.{f}" for s in SKILLS for f in s.features})
    logger.info(
        "scorer_loaded",
        **scorers,
        source="configured" if settings.model_path or settings.model_quick_path else "baseline",
    )
    api.graph = build_graph(SKILLS)
    api.quick_graph = build_graph(
        skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False, record_page=True
    )
    api.db_pool = await create_pool()
    await init_db(api.db_pool)
    use_postgres_stores(api.db_pool)
    api.x402 = X402(settings)
    await api.x402.attach(api.db_pool)  # the outflow audit's cursor and uncleared alerts survive restarts
    api.x402_base = BaseX402(settings)  # off unless UNFRET_X402_BASE_ENABLED, its hot key and cold address are set
    await api.x402_base.attach(api.db_pool)
    # Credits are bought by paying the same hot wallet; the scanner turns finalized transfers into balance.
    api.credits = CreditsDoor(settings, api.x402.rpc, api.x402.hot.pubkey if api.x402.hot else None, api.x402.prices)
    api.credits.attach(api.db_pool)
    # Card top-ups for API keys: on only when Polar is fully configured (token, webhook secret, every pack's product).
    api.fiat = polar.from_settings(settings)
    # Single instance: the sweep to cold is not coordinated across processes (the scanner is safe either way).
    tasks = [asyncio.create_task(api.x402.run_forever())] if api.x402.configured else []
    if api.x402_base.configured:
        tasks.append(asyncio.create_task(api.x402_base.run_forever()))
    if api.credits.scanner is not None:
        tasks.append(asyncio.create_task(api.credits.run_forever()))
    tasks.append(asyncio.create_task(housekeeping(api.db_pool)))
    try:
        async with mcp_remote.running(api):  # the remote MCP server at /mcp
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
                await recording.drain()
        await second_opinion.stop()  # an unfinished one leaves its item to a person
        await api.db_pool.close()
        await api.x402.rpc.aclose()
        await api.x402_base.aclose()
        if api.fiat is not None:
            await api.fiat.aclose()
    logger.info("Unfret backend service shutting down")


app = UnfretAPI(
    title="Unfret API",
    version=__version__,
    description=(
        "Scam, dropshipping and dark-pattern signals for a web shop, a marketplace listing, a second-hand ad or a "
        "service provider's site. Start with the [developer guide](/developers): the MCP server, pay-per-check over "
        "x402 on `POST /v1/check` (no account; `/llms.txt`, `/.well-known/x402`), or a prepaid pass, sent as "
        "`Authorization: Bearer ufk_…`, on `POST /v1/analyze`."
    ),
    contact={"email": "hello@unfret.net"},
    # x402scan (the agentcash discovery spec) points agents at the guide for language models.
    openapi_extensions={"x-agentcash-guidance": {"llmsTxtUrl": f"{settings.public_base_url}/llms.txt"}},
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
    allow_methods=["GET", "POST", "DELETE"],
    # Authorization: sessions and API keys; X-Delete-Code: deleting a shared result; X-Unfret-Claim: collecting a
    # purchase's invites (unfret.credits.invites); Mcp-*: the remote MCP server (/mcp) for browser-based MCP clients
    allow_headers=[
        "Content-Type",
        "Authorization",
        "PAYMENT-SIGNATURE",
        "X-Delete-Code",
        "X-Unfret-Claim",
        "Mcp-Protocol-Version",
        "Mcp-Session-Id",
        "Mcp-Method",
        "Mcp-Name",
        "Last-Event-ID",
    ],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "Mcp-Session-Id", "WWW-Authenticate"],
)


_BUILD_FOLDER_RE = re.compile(r"^v-[0-9A-Za-z.]+/")


class RevalidatedStaticFiles(StaticFiles):
    """
    The pages' scripts, styles and images, which caches must check with us before reusing (``no-cache``).

    Without it Cloudflare keeps a file for hours after a deploy: a new page then gets an old stylesheet, or a mix of
    old and new scripts. The ETag still lets a cache reuse an unchanged file after a quick check (304).
    """

    def file_response(
        self, full_path: str | PathLike[str], stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        """
        Serve a file as Starlette does, marked to be revalidated.

        :param full_path: The file.
        :param stat_result: Its stat.
        :param scope: The request scope.
        :param status_code: The status.
        :return: The response, with ``Cache-Control: no-cache``.
        """
        response = super().file_response(full_path, stat_result, scope, status_code)
        # A file asked for under a build folder never changes at that address; any other is checked first.
        built = "/v-" in scope["path"] and _BUILD_FOLDER_RE.match(self.get_raw_path(scope)) is not None
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if built else "no-cache"
        return response

    def get_raw_path(self, scope: Scope) -> str:
        """
        The path a request names within the static directory, build folder included.

        :param scope: The request scope.
        :return: For instance ``v-1a2b3c/site.css``.
        """
        return super().get_path(scope)

    def get_path(self, scope: Scope) -> str:
        """
        The file a request names, without the build folder pages put in front (``v-<commit>/site.css``).

        :param scope: The request scope.
        :return: The path within the static directory.
        """
        return _BUILD_FOLDER_RE.sub("", super().get_path(scope), count=1)


# Include API routes, and the scripts and styles of the /connect and /checkout pages
app.include_router(router)
# The remote MCP server (streamable HTTP; Unfret passes, or OAuth tokens for one): unfret.mcp_remote
app.router.routes.append(Route(mcp_remote.PATH, mcp_remote.endpoint, methods=["GET", "POST", "DELETE"]))
app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

# Add custom exception handlers
app.add_exception_handler(CustomAPIError, CustomAPIError.handle)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.add_exception_handler(StarletteHTTPException, handle_http_error)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "unfret.main:app",
        host="0.0.0.0",  # noqa: S104 - in a container, behind the proxy
        port=int(os.getenv("PORT", "8080")),
        reload=bool(os.getenv("RELOAD")),
    )
