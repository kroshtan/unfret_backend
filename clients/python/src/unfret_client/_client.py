"""The client: one call per check, answered with allow, warn or block."""

import hashlib
import itertools
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Action = Literal["allow", "warn", "block"]
Band = Literal["reliable", "concerns", "careful", "junk_or_scam"]
Tier = Literal["quick", "deep"]

DEFAULT_URL = "https://unfret.net"
# The four verdicts a shopper sees, and what an agent does about each by default.
DEFAULT_POLICY: Mapping[str, Action] = {
    "reliable": "allow",  # Looks fine
    "concerns": "warn",  # Worth a look
    "careful": "block",  # Think twice
    "junk_or_scam": "block",  # Serious concerns
}
BAND_NAMES: Mapping[str, str] = {
    "reliable": "Looks fine",
    "concerns": "Worth a look",
    "careful": "Think twice",
    "junk_or_scam": "Serious concerns",
}
_TIMEOUT = httpx.Timeout(120, connect=10)  # a deep check takes up to about a minute
_USER_AGENT = "unfret-client-python"


class UnfretError(Exception):
    """Unfret refused or couldn't answer a call. Nothing was charged for any of these."""

    def __init__(self, status: int, code: str, detail: str, retry_after: int | None = None) -> None:
        """
        Record the answer.

        :param status: The HTTP status.
        :param code: Unfret's ``error_code``.
        :param detail: Unfret's ``detail``.
        :param retry_after: Seconds to wait before retrying, when Unfret said.
        """
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status, self.code, self.detail, self.retry_after = status, code, detail, retry_after


class InvalidPassError(UnfretError):
    """The pass isn't valid (mistyped, or replaced with its backup code)."""


class NoChecksLeftError(UnfretError):
    """The pass has no deep checks left: buy more at unfret.net/checkout, or use the free quick tier."""


class RateLimitedError(UnfretError):
    """Too many calls (``retry_after`` says how long to wait), or free deep checks are paused until 00:00 UTC."""


class PageUnreadableError(UnfretError):
    """Unfret couldn't fetch the page (or got a bot check): send its HTML as ``page_content``."""


@dataclass(frozen=True)
class Decision:
    """What to do about a page, and why."""

    action: Action
    """``allow``: go ahead. ``warn``: ask your human first. ``block``: don't buy."""
    band: str | None
    """The verdict a shopper would see: ``reliable``, ``concerns``, ``careful`` or ``junk_or_scam`` (``None`` when
    the page couldn't be assessed)."""
    verdict: dict[str, Any] = field(repr=False)
    """Unfret's whole answer: ``risk_score``, ``confidence``, ``flags`` (the evidence), ``action`` (Unfret's own,
    from the request's ``policy``), ``charged``, ``free_checks_left``, …"""

    @property
    def label(self) -> str | None:
        """The verdict in words: "Looks fine", "Worth a look", "Think twice" or "Serious concerns"."""
        return BAND_NAMES.get(self.band or "")

    @property
    def reasons(self) -> list[str]:
        """The evidence, one line per flag."""
        return [str(f.get("label") or f.get("type")) + ": " + str(f.get("evidence", "")) for f in self.flags]

    @property
    def flags(self) -> list[dict[str, Any]]:
        """The flags: ``type``, ``evidence``, ``weight``, ``label``, ``severity``."""
        return list(self.verdict.get("flags") or [])

    @property
    def assessed(self) -> bool:
        """Whether Unfret could assess the page (``False``: not a store or product page)."""
        return bool(self.verdict.get("assessed", True))


def decide(
    verdict: Mapping[str, Any], policy: Mapping[str, Action] | None = None, unassessed: Action = "warn"
) -> Action:
    """
    Map Unfret's verdict to an action by its band (the four verdicts), with a policy.

    :param verdict: Unfret's answer.
    :param policy: Band → action; missing bands fall back to :data:`DEFAULT_POLICY`.
    :param unassessed: The action for a page Unfret couldn't assess.
    :return: ``allow``, ``warn`` or ``block``.
    """
    if not verdict.get("assessed", True):
        return unassessed
    band = verdict.get("band")
    rules = {**DEFAULT_POLICY, **(policy or {})}
    if band in rules:
        return rules[band]
    action = verdict.get("action")  # an answer without a band: Unfret's own action for the request's policy
    return action if action in ("allow", "warn", "block") else unassessed


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = str(body.get("error_code") or response.status_code)
    detail = str(body.get("detail") or "")
    wait = (body.get("metadata") or {}).get("retry_after_s") or response.headers.get("retry-after")
    retry_after = int(wait) if wait is not None and str(wait).isdigit() else None
    kind: type[UnfretError] = UnfretError
    if response.status_code == 401:
        kind = InvalidPassError
    elif code == "INSUFFICIENT_CREDITS" or response.status_code == 402:
        kind = NoChecksLeftError
    elif response.status_code == 429:
        kind = RateLimitedError
    elif code.startswith("PAGE_"):
        kind = PageUnreadableError
    raise kind(response.status_code, code, detail, retry_after)


def _body(url: str, tier: Tier, page_content: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"url": url, "tier": tier}
    if page_content:
        body["page_content"] = page_content
    return body


def _leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + 8 - byte.bit_length()
    return bits


def solve(challenge: str, bits: int) -> str:
    """
    Find the proof of work ``POST /v1/keys`` asks for: SHA-256(challenge + ":" + solution) with ``bits`` zero bits.

    :param challenge: The challenge from ``GET /v1/keys/challenge``.
    :param bits: Its difficulty.
    :return: The solution (a decimal counter).
    """
    for n in itertools.count():
        if _leading_zero_bits(hashlib.sha256(f"{challenge}:{n}".encode()).digest()) >= bits:
            return str(n)
    raise AssertionError  # pragma: no cover - itertools.count never ends


class Unfret:
    """
    Unfret's check, with an Unfret pass.

    ``Unfret(pass_="ufk_…").check(url)`` → a :class:`Decision`. The pass comes from ``pass_`` or ``UNFRET_PASS``,
    the server from ``base_url`` or ``UNFRET_API_URL`` (``https://unfret.net``).
    """

    def __init__(
        self,
        pass_: str | None = None,
        *,
        base_url: str | None = None,
        policy: Mapping[str, Action] | None = None,
        unassessed: Action = "warn",
        http: httpx.Client | None = None,
    ) -> None:
        """
        Set the client up.

        :param pass_: The Unfret pass (``ufk_…``); default ``UNFRET_PASS``.
        :param base_url: The server; default ``UNFRET_API_URL`` or ``https://unfret.net``.
        :param policy: Band → action, over :data:`DEFAULT_POLICY` (e.g. ``{"concerns": "allow"}``).
        :param unassessed: The action for a page Unfret couldn't assess (default ``warn``).
        :param http: An ``httpx.Client`` to use (tests, proxies).
        """
        self.unfret_pass = pass_ or os.environ.get("UNFRET_PASS") or None
        self.base_url = (base_url or os.environ.get("UNFRET_API_URL") or DEFAULT_URL).rstrip("/")
        self.policy, self.unassessed = dict(policy or {}), unassessed
        self._http = http or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})

    def __enter__(self) -> "Unfret":
        """
        Use the client in a ``with`` block.

        :return: The client.
        """
        return self

    def __exit__(self, *_: object) -> None:
        """Close the client."""
        self.close()

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()

    def _headers(self) -> dict[str, str]:
        if not self.unfret_pass:
            raise InvalidPassError(401, "NO_PASS", "Pass an Unfret pass, or set UNFRET_PASS.")
        return {"Authorization": f"Bearer {self.unfret_pass}"}

    def check(self, url: str, *, tier: Tier = "deep", page_content: str | None = None) -> Decision:
        """
        Check a store or product page.

        :param url: The page's address.
        :param tier: ``deep`` (everything; one check from the pass) or ``quick`` (rules on the page; free).
        :param page_content: The page's HTML, if you have it (needed for marketplaces that block fetches).
        :return: The decision.
        :raises UnfretError: The call was refused (nothing charged): :class:`InvalidPassError`,
            :class:`NoChecksLeftError`, :class:`RateLimitedError`, :class:`PageUnreadableError`.
        """
        response = self._http.post(
            f"{self.base_url}/v1/check", json=_body(url, tier, page_content), headers=self._headers()
        )
        _raise_for(response)
        verdict: dict[str, Any] = response.json()
        return Decision(decide(verdict, self.policy, self.unassessed), verdict.get("band"), verdict)

    def checks_left(self) -> dict[str, Any]:
        """
        What the pass has left.

        :return: ``GET /v1/credits``: ``checks_remaining``, ``free_checks_remaining``, ``balance_usd_micros``, …
        :raises UnfretError: The call was refused.
        """
        response = self._http.get(f"{self.base_url}/v1/credits", headers=self._headers())
        _raise_for(response)
        credits: dict[str, Any] = response.json()
        return credits

    def new_developer_pass(self) -> dict[str, Any]:
        """
        Make a developer pass (20 free deep checks; one a day per network) and use it from now on.

        Solves the proof of work ``POST /v1/keys`` asks for (a second or so). Keep ``key`` and ``recovery_code``:
        they are shown only this once.

        :return: ``key_id``, ``key``, ``recovery_code``, ``free_checks`` (0 when this network already had its
            developer pass today), …
        :raises UnfretError: The call was refused.
        """
        challenge = self._http.get(f"{self.base_url}/v1/keys/challenge")
        _raise_for(challenge)
        work = challenge.json()
        body = {"challenge": work["challenge"], "solution": solve(work["challenge"], work["bits"]), "developer": True}
        response = self._http.post(f"{self.base_url}/v1/keys", json=body)
        _raise_for(response)
        issued: dict[str, Any] = response.json()
        self.unfret_pass = issued["key"]
        return issued


class AsyncUnfret:
    """:class:`Unfret` for asyncio: ``await AsyncUnfret(pass_="ufk_…").check(url)``."""

    def __init__(
        self,
        pass_: str | None = None,
        *,
        base_url: str | None = None,
        policy: Mapping[str, Action] | None = None,
        unassessed: Action = "warn",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Set the client up (the arguments of :class:`Unfret`).

        :param pass_: The Unfret pass; default ``UNFRET_PASS``.
        :param base_url: The server; default ``UNFRET_API_URL`` or ``https://unfret.net``.
        :param policy: Band → action, over :data:`DEFAULT_POLICY`.
        :param unassessed: The action for a page Unfret couldn't assess.
        :param http: An ``httpx.AsyncClient`` to use.
        """
        self.unfret_pass = pass_ or os.environ.get("UNFRET_PASS") or None
        self.base_url = (base_url or os.environ.get("UNFRET_API_URL") or DEFAULT_URL).rstrip("/")
        self.policy, self.unassessed = dict(policy or {}), unassessed
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})

    async def __aenter__(self) -> "AsyncUnfret":
        """
        Use the client in an ``async with`` block.

        :return: The client.
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.unfret_pass:
            raise InvalidPassError(401, "NO_PASS", "Pass an Unfret pass, or set UNFRET_PASS.")
        return {"Authorization": f"Bearer {self.unfret_pass}"}

    async def check(self, url: str, *, tier: Tier = "deep", page_content: str | None = None) -> Decision:
        """
        Check a store or product page (see :meth:`Unfret.check`).

        :param url: The page's address.
        :param tier: ``deep`` or ``quick``.
        :param page_content: The page's HTML, if you have it.
        :return: The decision.
        :raises UnfretError: The call was refused (nothing charged).
        """
        response = await self._http.post(
            f"{self.base_url}/v1/check", json=_body(url, tier, page_content), headers=self._headers()
        )
        _raise_for(response)
        verdict: dict[str, Any] = response.json()
        return Decision(decide(verdict, self.policy, self.unassessed), verdict.get("band"), verdict)

    async def checks_left(self) -> dict[str, Any]:
        """
        What the pass has left (see :meth:`Unfret.checks_left`).

        :return: ``GET /v1/credits``.
        :raises UnfretError: The call was refused.
        """
        response = await self._http.get(f"{self.base_url}/v1/credits", headers=self._headers())
        _raise_for(response)
        credits: dict[str, Any] = response.json()
        return credits

    async def new_developer_pass(self) -> dict[str, Any]:
        """
        Make a developer pass and use it from now on (see :meth:`Unfret.new_developer_pass`).

        :return: The new pass.
        :raises UnfretError: The call was refused.
        """
        challenge = await self._http.get(f"{self.base_url}/v1/keys/challenge")
        _raise_for(challenge)
        work = challenge.json()
        body = {"challenge": work["challenge"], "solution": solve(work["challenge"], work["bits"]), "developer": True}
        response = await self._http.post(f"{self.base_url}/v1/keys", json=body)
        _raise_for(response)
        issued: dict[str, Any] = response.json()
        self.unfret_pass = issued["key"]
        return issued
