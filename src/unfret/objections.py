"""
Requests from shop owners and sellers: an objection to a result, a correction, or removal of their data.

Sent from the ``/objection`` page (``POST /v1/objections``, no sign-in) into the operator's queue
(``/v1/admin/objections``), who answers by e-mail and acts with the existing tools: ``DELETE /v1/admin/shops/{domain}``
and ``POST /v1/admin/contacts/forget`` for removal, the report queue and the corpus for a wrong result.

**What is stored** (``objections``): what the sender wrote and nothing else: the kind of request, the site (its
host and path; anything after ``?`` or ``#`` is dropped), the message, the reply address if they gave one, and the
day it arrived. No IP address, time of day, sign-in or browser details. The operator deletes a request once it is
handled; the hourly housekeeping deletes any still open after ``objection_days`` (90). The route limits requests per
visitor network in memory only (:mod:`unfret.visitors`).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import asyncpg
import structlog

logger = structlog.get_logger("unfret")

Kind = Literal["correction", "removal", "other"]

MAX_SITE_CHARS = 500
MAX_MESSAGE_CHARS = 2000
MAX_REPLY_TO_CHARS = 254
# Open requests at most: a full queue answers 503 rather than growing without bound.
MAX_OPEN = 1000

_HOST_RE = re.compile(
    r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ObjectionError(Exception):
    """A request that can't be taken; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        """
        Name the problem.

        :param code: ``BAD_SITE``, ``BAD_REPLY_TO`` or ``QUEUE_FULL``.
        :param detail: What to tell the sender.
        """
        super().__init__(detail)
        self.code = code
        self.detail = detail


def site_of(text: str) -> str:
    """
    The site a request is about, as host and path: a link or a bare domain, without scheme, query or fragment.

    :param text: What the sender typed (``https://shop.example/p/1?ref=x``, ``shop.example``).
    :return: ``shop.example/p/1`` (``shop.example`` for the home page).
    :raises ObjectionError: ``BAD_SITE`` when it names no web address.
    """
    value = text.strip()
    parts = urlsplit(value if "://" in value else f"//{value}")
    try:
        host = (parts.hostname or "").rstrip(".").removeprefix("www.")
    except ValueError:
        host = ""
    if parts.scheme not in {"", "http", "https"} or not _HOST_RE.match(host):
        raise ObjectionError("BAD_SITE", "Give the address of your shop or page, such as shop.example.")
    path = parts.path.rstrip("/")
    return f"{host}{path}"[:MAX_SITE_CHARS]


def reply_address(text: str) -> str | None:
    """
    The address to answer at, if the sender gave one.

    :param text: What the sender typed (may be empty).
    :return: The address, or ``None`` for none.
    :raises ObjectionError: ``BAD_REPLY_TO`` when it isn't an e-mail address.
    """
    value = text.strip()
    if not value:
        return None
    if len(value) > MAX_REPLY_TO_CHARS or not _EMAIL_RE.match(value):
        raise ObjectionError("BAD_REPLY_TO", "That isn't an e-mail address we can answer at.")
    return value


def _message(text: str) -> str:
    return _CONTROL_RE.sub("", text).strip()[:MAX_MESSAGE_CHARS]


@dataclass(frozen=True)
class Received:
    """A request in the queue."""

    objection_id: int
    received_on: date


async def submit(pool: asyncpg.Pool, *, kind: Kind, site: str, message: str, reply_to: str) -> Received:
    """
    Put a request in the operator's queue, as the sender wrote it.

    :param pool: The database pool.
    :param kind: What the sender asks for.
    :param site: The site or page it is about (see :func:`site_of`).
    :param message: What they wrote.
    :param reply_to: Where to answer (may be empty).
    :return: Its id and day.
    :raises ObjectionError: For a site or reply address that isn't one, or a full queue.
    """
    where, answer_at, text = site_of(site), reply_address(reply_to), _message(message)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE objections IN SHARE ROW EXCLUSIVE MODE")
        if await conn.fetchval("SELECT COUNT(*) FROM objections") >= MAX_OPEN:
            raise ObjectionError("QUEUE_FULL", "Our queue is full right now; please e-mail hello@unfret.net instead.")
        row = await conn.fetchrow(
            "INSERT INTO objections (kind, site, message, reply_to) VALUES ($1, $2, $3, $4) RETURNING id, received_on",
            kind,
            where,
            text,
            answer_at,
        )
    logger.info("objection_received", kind=kind)
    return Received(objection_id=row["id"], received_on=row["received_on"])


async def queue(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    """
    Open requests for the operator, oldest first.

    :param pool: The database pool.
    :param limit: How many.
    :return: The requests, each with its ``received_on`` as ``YYYY-MM-DD``.
    """
    rows = await pool.fetch(
        "SELECT id, kind, site, message, reply_to, received_on FROM objections ORDER BY id LIMIT $1", limit
    )
    return [{**dict(r), "received_on": r["received_on"].isoformat()} for r in rows]


async def remove(pool: asyncpg.Pool, objection_id: int) -> bool:
    """
    Delete a request once it is handled.

    :param pool: The database pool.
    :param objection_id: The request.
    :return: Whether there was one.
    """
    status = await pool.execute("DELETE FROM objections WHERE id = $1", objection_id)
    removed = str(status).endswith(" 1")
    if removed:
        logger.info("objection_handled")
    return removed


async def purge(pool: asyncpg.Pool, days: int, today: date | None = None) -> int:
    """
    Delete requests still open after ``days`` (housekeeping).

    :param pool: The database pool.
    :param days: How long an open request is kept.
    :param today: The current day (injectable for tests).
    :return: How many were deleted.
    """
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=days)
    status = await pool.execute("DELETE FROM objections WHERE received_on < $1", cutoff)
    return int(str(status).rsplit(" ", 1)[-1] or 0)
