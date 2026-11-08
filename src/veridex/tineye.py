"""
TinEye reverse image search (https://api.tineye.com/rest/), paid per search.

Only the public product image URL is sent: never the caller, the page or the listing. Results are summarised to
the few facts the analysis needs and cached by perceptual image hash (:mod:`veridex.image_hash`), so the same
supplier photo reused across many listings is searched once. A monthly budget caps spending: once it's used up,
searches are refused instead of billed.
"""

from datetime import UTC, date, datetime
from typing import Any, Protocol

import httpx
import structlog

from veridex.config import settings

logger = structlog.get_logger("veridex")

# Marketplaces that supply generic goods; a product photo found there predates most resellers' listings.
SUPPLIER_DOMAINS: frozenset[str] = frozenset(
    {
        "temu.com",
        "aliexpress.com",
        "aliexpress.us",
        "aliexpress.ru",
        "alibaba.com",
        "1688.com",
        "dhgate.com",
        "shein.com",
        "wish.com",
        "banggood.com",
        "lightinthebox.com",
        "joom.com",
        "made-in-china.com",
        "miniinthebox.com",
        "cjdropshipping.com",
    }
)
_TIMEOUT_S = 30.0


class BudgetExceededError(RuntimeError):
    """The TinEye search budget for this month is used up."""


class SearchBudget(Protocol):
    """Counts searches per calendar month."""

    async def reserve(self, month: str, limit: int) -> bool:
        """
        Count one search for ``month`` if that stays within ``limit``.

        :param month: ``YYYY-MM``.
        :param limit: The monthly maximum.
        :return: ``True`` if the search may go ahead.
        """
        ...


class MemoryBudget:
    """An in-process counter (resets on restart; the app swaps in a database-backed one)."""

    def __init__(self) -> None:
        """Start with no searches counted."""
        self._counts: dict[str, int] = {}

    async def reserve(self, month: str, limit: int) -> bool:
        """
        Count one search for ``month`` if that stays within ``limit``.

        :param month: ``YYYY-MM``.
        :param limit: The monthly maximum.
        :return: ``True`` if the search may go ahead.
        """
        if self._counts.get(month, 0) >= limit:
            return False
        self._counts[month] = self._counts.get(month, 0) + 1
        return True


_budget: SearchBudget = MemoryBudget()


def use_budget(budget: SearchBudget) -> None:
    """
    Replace the process-wide budget counter (done at startup when a database is available).

    :param budget: The counter to use from now on.
    """
    global _budget  # noqa: PLW0603 - one process-wide counter, swapped once at startup
    _budget = budget


def is_enabled() -> bool:
    """Whether a TinEye key is configured."""
    return bool(settings.tineye_api_key.get_secret_value())


def _registrable(domain: str) -> str:
    parts = domain.lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce a TinEye search response to what the analysis uses.

    :param data: The parsed TinEye JSON.
    :return: ``total`` matches, ``supplier_domains`` (sorted), ``domains`` (up to 20) and ``earliest_crawl``
        (ISO date across all backlinks, or ``None``).
    """
    matches = (data.get("results") or {}).get("matches") or []
    domains = sorted({_registrable(str(m.get("domain") or "")) for m in matches if m.get("domain")})
    crawls: list[date] = []
    for match in matches:
        for link in match.get("backlinks") or []:
            try:
                crawls.append(date.fromisoformat(str(link.get("crawl_date") or "")[:10]))
            except ValueError:
                continue
    total = (data.get("stats") or {}).get("total_results") or (data.get("results") or {}).get("total_results")
    return {
        "total": int(total) if isinstance(total, int | float) else len(matches),
        "supplier_domains": [d for d in domains if d in SUPPLIER_DOMAINS],
        "domains": domains[:20],
        "earliest_crawl": min(crawls).isoformat() if crawls else None,
    }


async def search(image_url: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """
    Reverse-search one image URL, within the monthly budget. Other API errors raise :class:`httpx.HTTPError`.

    :param image_url: A public image URL (TinEye fetches it).
    :param transport: Optional httpx transport, for tests.
    :return: The summary (see :func:`summarise`).
    :raises BudgetExceededError: If this month's budget is used up (locally, or TinEye answers 402).
    """
    month = datetime.now(tz=UTC).strftime("%Y-%m")
    if not await _budget.reserve(month, settings.tineye_monthly_budget):
        raise BudgetExceededError(f"TinEye budget of {settings.tineye_monthly_budget} searches used up for {month}")
    async with httpx.AsyncClient(base_url=settings.tineye_api_url, timeout=_TIMEOUT_S, transport=transport) as client:
        r = await client.get(
            "search/",
            params={"image_url": image_url, "limit": 100, "sort": "crawl_date", "order": "asc"},
            headers={"x-api-key": settings.tineye_api_key.get_secret_value()},
        )
    if r.status_code == 402:
        raise BudgetExceededError("TinEye reports no searches left on the account")
    r.raise_for_status()
    summary = summarise(r.json())
    logger.info("tineye_search", total=summary["total"], suppliers=len(summary["supplier_domains"]))
    return summary
