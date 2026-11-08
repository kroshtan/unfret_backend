"""Find and fetch Internet Archive snapshots: the CDX API for the closest capture, then its raw ``id_`` copy."""

from evals.sources._http import PoliteClient

CDX_URL = "https://web.archive.org/cdx/search/cdx"
_MAX_HTML_BYTES = 5 * 1024 * 1024


async def closest_snapshot(client: PoliteClient, url: str, timestamp: str) -> tuple[str, str] | None:
    """
    Find the 200-status HTML capture of ``url`` closest to ``timestamp``.

    :param client: A rate-limited client.
    :param url: The page URL as recorded by the dataset.
    :param timestamp: Target time as ``YYYYMMDD`` (or any prefix of ``YYYYMMDDhhmmss``).
    :return: ``(capture timestamp, original URL)``, or ``None`` if there is no usable capture.
    """
    response = await client.get(
        CDX_URL,
        params={
            "url": url,
            "closest": timestamp,
            "sort": "closest",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "fl": "timestamp,original",
            "output": "json",
            "limit": "1",
        },
    )
    if response is None or response.status_code != 200:
        return None
    try:
        rows = response.json()
    except ValueError:
        return None
    # The first row is the header.
    if len(rows) < 2:
        return None
    return rows[1][0], rows[1][1]


async def fetch_snapshot(client: PoliteClient, timestamp: str, original: str) -> str | None:
    """
    Fetch the raw archived HTML of a capture, without the Wayback toolbar or rewritten links.

    :param client: A rate-limited client.
    :param timestamp: The capture timestamp from :func:`closest_snapshot`.
    :param original: The captured URL.
    :return: The HTML, or ``None`` on failure or when it is too large.
    """
    response = await client.get(f"https://web.archive.org/web/{timestamp}id_/{original}")
    if response is None or response.status_code != 200 or len(response.content) > _MAX_HTML_BYTES:
        return None
    return response.text
