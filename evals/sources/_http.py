import asyncio
import time

import httpx
import structlog

logger = structlog.get_logger("veridex.evals")

USER_AGENT = "veridex-evals/0.1 (research corpus builder; https://git.kroshtan.com/kroshtan/veridex_backend)"


class PoliteClient:
    """
    An httpx client that keeps at least ``interval`` seconds between requests and retries with backoff.

    Retries cover connection errors, 429 and 5xx. A ``Retry-After`` header, when present, sets the wait.
    """

    def __init__(self, interval: float = 1.0, retries: int = 4, timeout: float = 30.0) -> None:
        """
        Create the client.

        :param interval: Minimum seconds between the starts of two requests.
        :param retries: Attempts after the first one.
        :param timeout: Per-request timeout in seconds.
        """
        self.interval = interval
        self.retries = retries
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _wait_turn(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    async def get(self, url: str, **kwargs: object) -> httpx.Response | None:
        """
        GET ``url``, returning the final response, or ``None`` when every attempt failed.

        Extra keyword arguments are passed through to ``httpx.AsyncClient.get``.

        :param url: The URL to fetch.
        :return: The response (any status that is not retried), or ``None``.
        """
        backoff = 2.0
        for attempt in range(self.retries + 1):
            await self._wait_turn()
            try:
                response = await self._client.get(url, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                logger.debug("http_error", url=url, attempt=attempt, error=type(exc).__name__)
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    backoff = max(backoff, float(retry_after))
                logger.debug("http_retry", url=url, attempt=attempt, status=response.status_code)
            await asyncio.sleep(backoff)
            backoff *= 2
        return None

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()
