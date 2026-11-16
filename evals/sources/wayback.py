"""Find and fetch Internet Archive snapshots: the CDX API for the closest capture, then its raw ``id_`` copy."""

import re

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


async def first_capture(client: PoliteClient, domain: str) -> str | None:
    """
    The timestamp of the Internet Archive's earliest 200-status capture of a domain's pages.

    :param client: A rate-limited client.
    :param domain: The domain.
    :return: ``YYYYMMDDhhmmss``, or ``None`` when it was never archived.
    """
    response = await client.get(
        CDX_URL,
        params={"url": domain, "filter": "statuscode:200", "fl": "timestamp", "output": "json", "limit": "1"},
    )
    if response is None or response.status_code != 200:
        return None
    try:
        rows = response.json()
    except ValueError:
        return None
    return str(rows[1][0]) if len(rows) > 1 else None


class WaybackUnavailableError(Exception):
    """The archive didn't answer (every attempt failed): try the page again later rather than counting it a miss."""


_CAPTURE_RE = re.compile(r"web\.archive\.org/web/(\d{14})(?:id_)?/(.+)$")


def parse_capture_url(url: str) -> tuple[str, str] | None:
    """
    The capture timestamp and original URL of a Wayback address (``…/web/<timestamp>id_/<original>``).

    :param url: A ``web.archive.org/web/…`` URL.
    :return: ``(timestamp, original)``, or ``None`` for any other URL.
    """
    m = _CAPTURE_RE.search(url)
    return (m.group(1), m.group(2)) if m else None


async def earliest_capture(client: PoliteClient, domain: str) -> str | None:
    """
    The timestamp of the Internet Archive's earliest capture of a domain's home page, of any status.

    Asks for the capture closest to 1996 and reads where the archive redirects, so no index query (CDX can take
    tens of seconds per query) and no page download is needed.

    :param client: A rate-limited client.
    :param domain: The domain.
    :return: ``YYYYMMDDhhmmss``, or ``None`` when it was never archived.
    :raises WaybackUnavailableError: When the archive didn't answer.
    """
    response = await client.get(f"https://web.archive.org/web/1996id_/http://{domain}/", follow_redirects=False)
    if response is None:
        raise WaybackUnavailableError(domain)
    target = response.headers.get("location", "") if response.is_redirect else str(response.url)
    found = parse_capture_url(target) if response.is_redirect or response.status_code == 200 else None
    return found[0] if found else None


async def fetch_closest(client: PoliteClient, timestamp: str, url: str) -> tuple[str, str, str] | None:
    """
    Fetch the raw capture of ``url`` closest to ``timestamp``, following the archive's redirects to it.

    :param client: A rate-limited client.
    :param timestamp: Target time, ``YYYYMMDD`` (or any prefix of ``YYYYMMDDhhmmss``).
    :param url: The page URL.
    :return: ``(capture timestamp, captured URL, HTML)``, or ``None`` when there is no usable HTML capture.
    :raises WaybackUnavailableError: When the archive didn't answer.
    """
    response = await client.get(f"https://web.archive.org/web/{timestamp}id_/{url}")
    if response is None:
        raise WaybackUnavailableError(url)
    if response.status_code != 200 or len(response.content) > _MAX_HTML_BYTES:
        return None
    if "html" not in response.headers.get("content-type", "html"):
        return None
    found = parse_capture_url(str(response.url))
    return (found[0], found[1], response.text) if found else None
