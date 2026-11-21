"""
Earlier copies of a shop's home page in Common Crawl: the first archive the deep check asks.

Common Crawl (commoncrawl.org, a non-profit) crawls billions of pages about once a month and publishes each crawl as
WARC files with an index of its own (a CDX server at index.commoncrawl.org, one per crawl). For a host this module:

- lists the crawls and their dates (``collinfo.json``, kept in memory for a day: it changes once a crawl),
- picks the crawls nearest the days asked for, and asks each crawl's index for the home page (``url=<host>/``, which
  also matches its ``www.`` and ``http`` forms),
- fetches one capture's WARC record by byte range (``Range``) from data.commoncrawl.org and returns its page.

Common Crawl's rules (its terms of use, its status page, and its notes on the 2023 overload): its servers are shared
by everyone and handle a few thousand small requests a second all told, so a client sending small index or
single-page requests should stay well under ten a second; a 503 ("slow down") or a refusal by its CDN's rate limits
(403, lately 429) means slow down, never retry within a second and back off to ten seconds or more; nobody should
bulk-download through the index server; and ``collinfo.json`` should be cached. Requests must not disguise where
they come from. Unfret asks far below that, within a pace of its own (:data:`pace`, :mod:`unfret.pace`):
``UNFRET_COMMONCRAWL_PER_MINUTE`` requests a minute per process, index and data together, none at all for
``UNFRET_COMMONCRAWL_PAUSE_S`` after a 403, 429, 503, 504 or a refused connection, and never a retry within a look.

Everything goes through :func:`unfret.net.fetch_public` (public addresses only, size limits, timeouts, no cookies).
"""

import asyncio
import json
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from unfret.config import settings
from unfret.net import fetch_public
from unfret.pace import Pace

_COLLINFO_TIMEOUT_S = 10.0
_INDEX_TIMEOUT_S = 12.0
_RECORD_TIMEOUT_S = 12.0
_MAX_COLLINFO_BYTES = 512 * 1024
_MAX_INDEX_BYTES = 64 * 1024
# A home page's WARC record, compressed; bigger ones are left alone.
_MAX_RECORD_BYTES = 1024 * 1024
# The page in it, uncompressed.
_MAX_PAGE_BYTES = 3 * 1024 * 1024
_CRAWLS_TTL_S = 24 * 3600
# The first-copy probes: the crawls nearest this many years ago, all asked at once (the oldest hit counts).
FIRST_PROBE_YEARS = (10, 5, 3)
_REFUSED = frozenset({403, 429, 503, 504})


class ArchiveBusyError(RuntimeError):
    """Unfret's own pace for an archive is used up, or the archive refused us lately: not asked, not retried."""


class ArchiveUnreachableError(ConnectionError):
    """None of the archived copies could be fetched: a connection problem, so the graph tries the skill once more."""


def give_up(errors: list[BaseException]) -> None:
    """
    Raise for requests that all failed: busy when the pace stopped every one, else unreachable.

    :param errors: The failures (none: nothing is raised).
    :raises ArchiveBusyError: When every one was paced out or refused lately.
    :raises ArchiveUnreachableError: Otherwise.
    """
    if not errors:
        return
    if all(isinstance(e, ArchiveBusyError) for e in errors):
        raise ArchiveBusyError("Not asking the archive right now (Unfret's own pace).") from errors[0]
    names = ", ".join(sorted({type(e).__name__ for e in errors}))
    raise ArchiveUnreachableError(f"The archive couldn't be asked ({names}).") from errors[0]


class RecordError(ValueError):
    """A WARC record that isn't a readable page (another record type, a broken encoding, too big)."""


def new_pace() -> Pace:
    """
    A fresh pace for Common Crawl, by the settings.

    :return: The pace.
    """
    return Pace(per_minute=lambda: settings.commoncrawl_per_minute, pause_s=lambda: settings.commoncrawl_pause_s)


pace = new_pace()


@dataclass(frozen=True)
class Crawl:
    """One Common Crawl crawl: its id, its index's address, and the days it ran."""

    id: str
    api: str
    start: date
    end: date

    def distance(self, day: date) -> int:
        """
        How far a day is from the crawl.

        :param day: The day.
        :return: 0 for a day it ran, else days to its nearest end.
        """
        if self.start <= day <= self.end:
            return 0
        return min(abs((day - self.start).days), abs((day - self.end).days))


def refused(exc: BaseException) -> bool:
    """
    Whether an error means "slow down": a rate limit, an overloaded server, or a refused connection.

    :param exc: The error.
    :return: ``True`` when the archive should not be asked for a while.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _REFUSED
    return isinstance(exc, httpx.ConnectError | ConnectionRefusedError)


async def get(
    url: str, *, timeout: float, max_bytes: int, headers: dict[str, str] | None = None, within: Pace | None = None
) -> bytes:
    """
    One request to an archive, within its pace, like any outside page (:func:`unfret.net.fetch_public`).

    :param url: The address.
    :param timeout: The whole request's time limit.
    :param max_bytes: The largest answer accepted.
    :param headers: More request headers (a ``Range``).
    :param within: The archive's pace (Common Crawl's by default).
    :return: The answer's body.
    :raises ArchiveBusyError: When the pace is used up or the archive refused us lately.
    :raises Exception: What :func:`unfret.net.fetch_public` raises (a refusal pauses the pace first).
    """
    which = within or pace
    if not which.take():
        raise ArchiveBusyError("Not asking the archive right now (Unfret's own pace).")
    try:
        return await fetch_public(url, timeout=timeout, total_timeout=timeout, max_bytes=max_bytes, headers=headers)
    except Exception as exc:
        if refused(exc):
            which.pause()
        raise


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_crawls(data: object) -> list[Crawl]:
    """
    The crawls in a ``collinfo.json`` answer, newest first.

    :param data: The parsed JSON.
    :return: The crawls with an index and both dates.
    """
    found = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        start, end, api = _day(item.get("from")), _day(item.get("to")), str(item.get("cdx-api", ""))
        if start and end and api.startswith(settings.commoncrawl_index_url + "/"):
            found.append(Crawl(str(item.get("id", "")), api, start, end))
    return sorted(found, key=lambda c: c.end, reverse=True)


_crawls: tuple[float, list[Crawl]] | None = None


async def crawls() -> list[Crawl]:
    """
    Common Crawl's crawls, from ``collinfo.json``, kept in memory for a day.

    :return: The crawls, newest first. A list that can't be had raises (:func:`get`), and nothing is kept: the next
        look asks again.
    """
    global _crawls  # noqa: PLW0603 - a per-process copy of a list that changes once a month
    if _crawls is not None and _crawls[0] > time.monotonic():
        return _crawls[1]
    body = await get(
        f"{settings.commoncrawl_index_url}/collinfo.json", timeout=_COLLINFO_TIMEOUT_S, max_bytes=_MAX_COLLINFO_BYTES
    )
    found = parse_crawls(json.loads(body or b"[]"))
    if found:
        _crawls = (time.monotonic() + _CRAWLS_TTL_S, found)
    return found


def nearest(
    crawls: list[Crawl], day: date, tolerance: int | None = None, skip: set[str] | None = None
) -> Crawl | None:
    """
    The crawl nearest a day.

    :param crawls: The crawls.
    :param day: The day.
    :param tolerance: The most days off it may be (``None``: any).
    :param skip: Crawl ids already chosen.
    :return: The crawl, or ``None``.
    """
    near = [(c.distance(day), c) for c in crawls if c.id not in (skip or set())]
    best = min(near, key=lambda t: t[0], default=None)
    return best[1] if best is not None and (tolerance is None or best[0] <= tolerance) else None


def pick(crawls: list[Crawl], today: date, targets: tuple[tuple[int, int], ...]) -> list[Crawl]:
    """
    The crawls nearest each target day, each within its tolerance, each crawl once.

    :param crawls: The crawls.
    :param today: Today.
    :param targets: ``(days ago, tolerance in days)`` pairs.
    :return: Up to one crawl per target, in the targets' order.
    """
    chosen: list[Crawl] = []
    for days, tolerance in targets:
        crawl = nearest(crawls, today - timedelta(days=days), tolerance, {c.id for c in chosen})
        if crawl is not None:
            chosen.append(crawl)
    return chosen


def _lines(body: bytes) -> list[dict[str, Any]]:
    rows = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


async def captures(crawl: Crawl, host: str, *, status: str = "200", limit: int = 5) -> list[dict[str, Any]]:
    """
    A crawl's captures of the host's home page (its index, ``output=json``).

    :param crawl: The crawl.
    :param host: The shop's host.
    :param status: A regular expression the capture's HTTP status must match.
    :param limit: How many at most.
    :return: The index's rows (``timestamp``, ``url``, ``mime``, ``status``, ``filename``, ``offset``, ``length``, …);
        empty when the crawl has none (the index answers 404).
    :raises httpx.HTTPStatusError: When the index refused or failed otherwise (an index that can't be reached raises
        too, from :func:`get`; neither is cached).
    """
    url = f"{crawl.api}?url={quote(host)}/&output=json&filter={quote('status:' + status)}&limit={limit}"
    try:
        body = await get(url, timeout=_INDEX_TIMEOUT_S, max_bytes=_MAX_INDEX_BYTES)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:  # "No Captures found"
            return []
        raise
    return [r | {"crawl": crawl.id} for r in _lines(body) if str(r.get("timestamp", "")).isdigit()]


def html_capture(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    The first capture that is an HTML page Unfret can fetch: a WARC file, a sane offset, a record within the limit.

    :param rows: :func:`captures`.
    :return: The row, or ``None``.
    """
    for row in rows:
        mime = f"{row.get('mime', '')} {row.get('mime-detected', '')}".lower()
        length, offset = str(row.get("length", "")), str(row.get("offset", ""))
        filename = str(row.get("filename", ""))
        if (
            "html" in mime
            and length.isdigit()
            and offset.isdigit()
            and 0 < int(length) <= _MAX_RECORD_BYTES
            and filename.startswith("crawl-data/")
            and filename.endswith(".warc.gz")
            and ".." not in filename
        ):
            return row
    return None


def _bounded(data: bytes, wbits: int) -> bytes:
    decoder = zlib.decompressobj(wbits)
    out = decoder.decompress(data, _MAX_PAGE_BYTES + 1)
    if len(out) > _MAX_PAGE_BYTES:
        raise RecordError("Record too big.")
    return out


def _dechunk(body: bytes) -> bytes:
    out, rest = bytearray(), body
    while rest:
        size_line, _, rest = rest.partition(b"\r\n")
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out += rest[:size]
        rest = rest[size + 2 :]
    return bytes(out)


def _headers(block: bytes) -> dict[bytes, bytes]:
    headers = {}
    for line in block.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _decoded(body: bytes, headers: dict[bytes, bytes]) -> bytes:
    try:
        if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
            body = _dechunk(body)
        encoding = headers.get(b"content-encoding", b"").lower()
        if encoding in {b"gzip", b"x-gzip"}:
            return _bounded(body, 16 + zlib.MAX_WBITS)
        if encoding == b"deflate":
            return _bounded(body, zlib.MAX_WBITS)
    except (ValueError, zlib.error) as exc:
        raise RecordError("Broken body.") from exc
    if encoding not in {b"", b"identity"}:
        raise RecordError("Unsupported content encoding.")
    return body


def page_of(record: bytes) -> bytes:
    """
    The page in one gzipped WARC ``response`` record: its HTTP body, decoded.

    Common Crawl stores bodies decoded and renames the original ``Content-Encoding`` and ``Transfer-Encoding``
    headers (``X-Crawler-…``); a record that still has either is decoded here, within the size limit.

    :param record: The record's bytes, as fetched by range (one gzip member).
    :return: The page.
    :raises RecordError: For another record type, a broken record or one too big.
    """
    try:
        warc = _bounded(record, 16 + zlib.MAX_WBITS)
    except zlib.error as exc:
        raise RecordError("Not a gzip member.") from exc
    head, _, http = warc.partition(b"\r\n\r\n")
    if not head.startswith(b"WARC/") or b"WARC-Type: response" not in head:
        raise RecordError("Not a WARC response record.")
    length = _headers(head).get(b"content-length", b"")
    if length.isdigit():
        http = http[: int(length)]  # the record's block, without the two line ends after it
    status_and_headers, _, body = http.partition(b"\r\n\r\n")
    return _decoded(body, _headers(status_and_headers))


async def fetch_page(capture: dict[str, Any]) -> bytes:
    """
    Fetch a capture's page: its WARC record by byte range from data.commoncrawl.org.

    :param capture: A row of :func:`captures` (:func:`html_capture`).
    :return: The page's HTML. A record that can't be fetched or read raises (:func:`get`, :func:`page_of`).
    """
    offset, length = int(capture["offset"]), int(capture["length"])
    record = await get(
        f"{settings.commoncrawl_data_url}/{capture['filename']}",
        timeout=_RECORD_TIMEOUT_S,
        max_bytes=length,
        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
    )
    return page_of(record)


def copy_url(crawl: str, timestamp: str, original: str) -> str:
    """
    Where a person sees a copy's record: the crawl's own index entry for it (Common Crawl has no page viewer).

    :param crawl: The crawl's id.
    :param timestamp: The copy's 14-digit timestamp.
    :param original: The address it copied.
    :return: The index.commoncrawl.org link.
    """
    return f"{settings.commoncrawl_index_url}/{crawl}-index?url={quote(original, safe=':/')}&from={timestamp}&limit=1"


async def first_capture(host: str, today: date | None = None) -> str | None:
    """
    The oldest copy Common Crawl has of the home page among a few probes: the address is at least that old.

    The crawls nearest :data:`FIRST_PROBE_YEARS` years ago are asked at once (one index request each, a page or a
    redirect counting), and the oldest capture found counts. A minimum: Common Crawl doesn't crawl every address
    every time, and the address may be older still.

    :param host: The shop's host.
    :param today: Today (tests pass their own).
    :return: ISO date of the oldest capture found, or ``None`` when no probe found one. When a probe failed and none
        found a copy it raises (:func:`give_up`), so the answer isn't cached as "none".
    """
    today = today or datetime.now(tz=UTC).date()
    found = await crawls()
    probes: list[Crawl] = []
    for years in FIRST_PROBE_YEARS:
        crawl = nearest(found, today - timedelta(days=365 * years), 365, {c.id for c in probes})
        if crawl is not None:
            probes.append(crawl)
    answers = await asyncio.gather(
        *(captures(c, host, status="[23]..", limit=1) for c in probes), return_exceptions=True
    )
    errors = [a for a in answers if isinstance(a, BaseException)]
    days = [d for a in answers if isinstance(a, list) for r in a if (d := _stamp_day(str(r["timestamp"])))]
    if not days:
        give_up(errors)
    return min(days).isoformat() if days else None


def _stamp_day(timestamp: str) -> date | None:
    try:
        return datetime.strptime(timestamp[:8], "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None
