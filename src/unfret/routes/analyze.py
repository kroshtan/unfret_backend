import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, NoReturn

import anyio
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import UsageMetadataCallbackHandler

from unfret import card, crowd, observations, page_fetch, reports, result_cache, shares, spend
from unfret.auth import get_credits, optional_account
from unfret.config import settings
from unfret.credits import free, keys, ledger
from unfret.deep_pow import DeepChallenges
from unfret.facts import PageFacts
from unfret.graph.state import SERVICE_TYPES, Finding
from unfret.schemas.errors import (
    BadRequestError,
    GatewayTimeoutError,
    InsufficientCreditsError,
    InternalServerError,
    ProofOfWorkRequiredError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnreadablePageError,
)
from unfret.schemas.requests import AnalyzeRequest
from unfret.schemas.responses import AnalyzeResponse, ChallengeResponse, FreePause
from unfret.usage import note as note_usage
from unfret.visitors import RateLimit

logger = structlog.get_logger("unfret")

router = APIRouter(prefix="/v1/analyze", tags=["Analyze"])

_SKILL_TAG_RE = re.compile(r"\[([^\]]+)\]")
_RESULT_KEYS = ("score", "explanation", "page_type", "assessment", "verdict")
# Kept with a result (in memory, as the cache entry) for its share token: the product's title, from the page.
_TITLE = "title"
# Kept with a result: it is about a service provider's business, so it gets no share token (results stay private).
_PRIVATE = "private"
# Pages fetched for URL-only requests, per account: a page we can't read is free, so this bounds the free fetches.
_fetch_rate = RateLimit(settings.analyze_fetch_per_minute)
# Quick checks (free for everyone), per account or, without one, per visitor network.
_quick_rate = RateLimit(settings.quick_checks_per_minute)
# Free deep checks per visitor network an hour (behind Cloudflare's own edge limits; in memory, salted hashes).
_free_deep_rate = RateLimit(settings.free_deep_checks_per_ip_per_hour, window_s=3600)
# Analyses per account that ran but were refunded (hung up, or not assessable): they cost LLM calls all the same.
_free_outcomes = RateLimit(settings.analyze_free_outcomes_per_hour, window_s=3600)


def _check_free_outcomes(address: str) -> None:
    """
    Refuse a new analysis while the account has had too many uncharged ones this hour.

    :param address: The account.
    :raises TooManyRequestsError: Over :attr:`~unfret.config.Settings.analyze_free_outcomes_per_hour`.
    """
    if (wait := _free_outcomes.waiting(address)) is not None:
        raise TooManyRequestsError(
            "Too many checks this hour ended without a result; wait a while before the next one.",
            metadata={"retry_after_s": max(1, int(wait) + 1)},
        )


def _count_free_outcome(charge: Charge) -> None:
    # A deep check that ran and was given back (the client hung up, or the page couldn't be assessed). Quick checks
    # cost no LLM call and have their own per-minute limit.
    if charge.charged and charge.account is not None:
        _free_outcomes.retry_after(charge.account)  # counts it (the result, a wait, is for the next request)


def _graph_input(body: AnalyzeRequest, request: Request) -> tuple[Any, dict[str, object]]:
    """
    Check the graph and build its input state.

    :param body: The request body.
    :param request: The FastAPI request.
    :return: The compiled graph and its input state.
    :raises InternalServerError: If the graph is not initialised.
    """
    graph = request.app.quick_graph if body.tier == "quick" else request.app.graph
    if graph is None:
        raise InternalServerError("Analysis graph is not initialised.")
    state: dict[str, object] = {
        "page_content": body.page_content or "",
        "url": body.url,
        "flags": body.flags,
    }
    if body.page_facts is not None:
        state["page_facts"] = body.page_facts
    if body.checkout_content:
        state["checkout_content"] = body.checkout_content
    return graph, state


_NOT_ENOUGH = "Not enough credits for an analysis."
# A developer pass with only free checks once they are paused (unfret.credits.free): a 402, as for an empty pass, with
# this detail (the day's budget) or DEVELOPER_PAUSED_MONTH (the month's LLM spend ceiling).
DEVELOPER_PAUSED = "Today's free developer checks are used up; they're back at 00:00 UTC. Bought checks still work."
DEVELOPER_PAUSED_MONTH = "Free developer checks are paused until next month. Bought checks still work."
# A shopper's free deep check at the month's LLM spend ceiling (unfret.spend): 429 FREE_CHECKS_PAUSED.
FREE_PAUSED = (
    "Free deep checks are paused until the 1st of next month: they run on donations, and this month's are used up. "
    "Quick checks still work."
)
# The stable key clients show their own words for, with the donate link.
FREE_PAUSED_MESSAGE_KEY = "free_checks_run_on_donations"


def donate_link() -> str:
    """
    Where clients send someone who wants to support Unfret: this site's ``/donate``.

    It redirects to the payment link configured now (``donate_url``), so the link can change without a client release.

    :return: The address.
    """
    return f"{settings.public_base_url.rstrip('/')}/donate"


def _shortfall(balance: int) -> dict[str, int]:
    price = settings.analysis_price_usd_micros
    return {
        "balance_usd_micros": balance,
        "analysis_usd_micros": price,
        # Deprecated aliases (same values), kept for extension versions up to 0.5.0.
        "balance_lamports": balance,
        "analysis_lamports": price,
    }


async def _developer_paused(request: Request, balance: int) -> NoReturn:
    """
    Refuse a check only a developer pass's paused free checks could pay for: a 402, as for an empty pass.

    :param request: The FastAPI request.
    :param balance: The pass's balance.
    :raises InsufficientCreditsError: Always, with the shortfall and when the free checks are back.
    """
    pool = request.app.db_pool
    month = (await spend.ceiling(pool, settings)).reached
    raise InsufficientCreditsError(
        DEVELOPER_PAUSED_MONTH if month else DEVELOPER_PAUSED,
        metadata={**_shortfall(balance), "retry_after_s": await free.back_in_s(pool, settings)},
    )


def paused_metadata() -> dict[str, Any]:
    """
    What a 429 ``FREE_CHECKS_PAUSED`` carries besides its detail (and ``/v1/pricing``'s ``free`` while paused).

    :return: ``reason`` (``spend_ceiling``), ``retry_after_s`` (to the 1st of next month, UTC), ``quick_checks``
        (``true``: they still work), ``message_key`` and ``donate_url``.
    """
    return {
        "reason": "spend_ceiling",
        "retry_after_s": spend.seconds_to_next_month(),
        "quick_checks": True,
        "message_key": FREE_PAUSED_MESSAGE_KEY,
        "donate_url": donate_link(),
    }


def _ip(request: Request) -> str:
    client = getattr(request, "client", None)  # a request built by hand (in-process callers) may have none
    return client.host if client else "unknown"


def provenance(request: Request, body: AnalyzeRequest, account: str | None) -> observations.Provenance:
    """
    Where the analysed page came from, for what the analysis records (:mod:`unfret.observations`).

    :param request: The FastAPI request (its client's network, hashed with today's salt, never stored).
    :param body: The request body: a URL-only request is a page Unfret fetched itself; anything else was supplied.
    :param account: The account, if any.
    :return: The provenance.
    """
    return observations.for_request(_ip(request), account, fetched=body.url_only, tier=body.tier)


def _key_id(account: str | None) -> str | None:
    return account.removeprefix(keys.ACCOUNT_PREFIX) if account and keys.is_key_account(account) else None


@dataclass
class Charge:
    """How one analysis is paid: a check from the balance, a developer pass's free check, or nothing (free)."""

    account: str | None
    paid: bool = False
    spent: free.Spent | None = None

    @property
    def charged(self) -> bool:
        """Whether this analysis used a check (bought, or a developer pass's free one)."""
        return self.paid or self.spent is not None


async def _can_pay(request: Request, account: str, *, api: bool = False) -> None:
    """
    Make sure a deep check can be paid for before a page is fetched for it (nothing is taken here).

    :param request: The FastAPI request.
    :param account: The account.
    :param api: For ``/v1/check``, where a developer pass's free checks count (:mod:`unfret.credits.free`).
    :raises InsufficientCreditsError: Without checks, or with only a developer pass's free checks while they are
        paused.
    """
    pool = request.app.db_pool
    key_id = _key_id(account)
    has_free = key_id is not None and await free.left(pool, key_id, api=api) > 0
    if has_free and not await free.paused(pool, settings):
        return
    balance = await _balance(request, account)
    if balance < settings.analysis_price_usd_micros:
        if has_free:
            await _developer_paused(request, balance)
        raise InsufficientCreditsError(_NOT_ENOUGH, metadata=_shortfall(balance))


async def _debit(request: Request, account: str, *, api: bool = False) -> Charge:
    """
    Pay for a deep check: a developer pass's free check first (on ``/v1/check`` only), then the balance.

    :param request: The FastAPI request.
    :param account: The account (a signed-in wallet or an API key).
    :param api: For ``/v1/check``.
    :return: What was taken, to give back on a refund.
    :raises InsufficientCreditsError: If nothing covers the analysis (with :data:`DEVELOPER_PAUSED` when only a
        developer pass's free checks would, and they are paused).
    """
    pool = request.app.db_pool
    outcome: free.Taken = "none"
    if (key_id := _key_id(account)) is not None:
        outcome, spent = await free.take_for_key(pool, settings, key_id, api=api)
        if spent is not None:
            logger.info("free_check_used")
            return Charge(account, spent=spent)
    if await ledger.debit(pool, account, settings.analysis_price_usd_micros) is None:
        balance = await ledger.balance(pool, account) or 0
        if outcome == "developer_paused":
            await _developer_paused(request, balance)
        raise InsufficientCreditsError(_NOT_ENOUGH, metadata=_shortfall(balance))
    return Charge(account, paid=True)


async def _fetch_if_url_only(
    request: Request, body: AnalyzeRequest, account: str | None, state: dict[str, object]
) -> AnalyzeRequest:
    """
    For a request with only a URL, fetch the page into the graph input, before anything is charged.

    For a deep check the caller's checks are looked at first (an empty account gets its 402 without a fetch), and a
    page that can't be read (unreachable, refused, a bot check, an empty shell) is answered with a free 422. Nothing
    about the URL is logged. A link that redirected (a short link) is checked under the page's own address: the graph,
    the result cache, the share token and what is paid for all see that address.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account, or ``None`` (a quick check without a pass).
    :param state: The graph input; its ``page_content`` is set to the fetched HTML and its ``url`` to its address.
    :return: The request as analysed: ``body``, with the page's own address when the link redirected.
    :raises TooManyRequestsError: If the account (or network) fetched too many pages in the last minute.
    :raises UnreadablePageError: If the page can't be checked from a server-side fetch (``error_code`` says why).
    """
    if not body.url_only:
        return body
    if account is not None and body.tier == "deep":
        await _can_pay(request, account)
    if (wait := _fetch_rate.retry_after(account or _ip(request))) is not None:
        raise TooManyRequestsError(
            "Too many links checked in a minute; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )
    try:
        page = await page_fetch.fetch_page(body.url.strip())
    except page_fetch.PageUnreadableError as exc:
        logger.info("analysis_page_unreadable", reason=exc.reason)
        raise UnreadablePageError(f"{exc.detail} No check was used.", error_code=exc.reason) from exc
    state["page_content"] = page.html
    if page.url == body.url.strip():
        return body
    logger.info("analysis_link_redirected")  # that it did, never where from or to
    state["url"] = page.url
    return body.model_copy(update={"url": page.url})


async def _caller(request: Request, body: AnalyzeRequest, account: str | None) -> str | None:
    """
    Who pays: a pass (or wallet) whose bought checks cover a deep check; nobody for a free one.

    A deep check that no bought check pays for is a shopper's free check: anonymous (the pass, if one was sent, is
    not used, so the check is tied to nothing), behind the month's spend ceiling, a proof of work and a per-network
    limit (:func:`_free_gate`). A quick check keeps the account it was sent with (for its report token), if any.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account from ``Authorization``, or ``None`` without the header.
    :return: The account that pays, or ``None`` (a free check).
    """
    if body.tier == "quick" or account is None:
        return account
    if result_cache.paid_for(account, body.url):
        return account  # its own repeat within the hour: served again, free (_cached)
    if await _balance(request, account) >= settings.analysis_price_usd_micros:
        return account
    return None


def _deep_challenges(request: Request) -> DeepChallenges:
    challenges: DeepChallenges | None = getattr(request.app.state, "deep_challenges", None)
    secret = get_credits(request).secret
    if challenges is None or challenges.secret != secret or challenges.bits != settings.deep_pow_bits:
        challenges = DeepChallenges(secret=secret, bits=settings.deep_pow_bits, ttl_s=settings.deep_pow_ttl_s)
        request.app.state.deep_challenges = challenges
    return challenges


async def _free_gate(request: Request, body: AnalyzeRequest) -> None:
    """
    Let a free deep check through: under the month's spend ceiling, with a solved challenge, within the hour's limit.

    :param request: The FastAPI request.
    :param body: The request body (its ``proof_of_work``).
    :raises ServiceUnavailableError: While free deep checks can't be offered (no session secret or database).
    :raises TooManyRequestsError: ``FREE_CHECKS_PAUSED`` at the spend ceiling; ``TOO_MANY_REQUESTS`` over the
        network's hourly limit.
    :raises ProofOfWorkRequiredError: Without a solved challenge.
    :raises BadRequestError: ``BAD_CHALLENGE`` for a challenge that is forged, expired, used or unsolved.
    """
    if not get_credits(request).secret or request.app.db_pool is None:
        raise ServiceUnavailableError("Free deep checks are not available right now. Quick checks still work.")
    if (await spend.ceiling(request.app.db_pool, settings)).reached:
        raise TooManyRequestsError(FREE_PAUSED, error_code="FREE_CHECKS_PAUSED", metadata=paused_metadata())
    pow_ = body.proof_of_work
    if pow_ is None:
        raise ProofOfWorkRequiredError(
            "A free deep check needs a solved challenge: GET /v1/analyze/challenge, then send it as proof_of_work.",
            metadata={"challenge_url": "/v1/analyze/challenge"},
        )
    if not _deep_challenges(request).redeem(pow_.challenge, pow_.solution):
        raise BadRequestError(
            "Challenge invalid, expired, used or unsolved; get a new one.", error_code="BAD_CHALLENGE"
        )
    if (wait := _free_deep_rate.retry_after(_ip(request))) is not None:
        raise TooManyRequestsError(
            "Too many deep checks from here this hour; wait a while. Quick checks still work.",
            metadata={"retry_after_s": max(1, int(wait) + 1)},
        )


def _quick_limit(request: Request, body: AnalyzeRequest, account: str | None) -> None:
    """
    Count a quick check against its per-minute limit.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account, or ``None``.
    :raises TooManyRequestsError: If the account (or network) ran too many quick checks in the last minute.
    """
    if body.tier == "quick" and (wait := _quick_rate.retry_after(account or _ip(request))) is not None:
        raise TooManyRequestsError(
            "Too many quick checks in a minute; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )


async def _charge(request: Request, body: AnalyzeRequest, account: str | None) -> Charge:
    """
    Pay for the analysis: a deep check a pass pays for uses a check; a quick check or a free deep check nothing.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account, or ``None``.
    :return: What was taken.
    """
    if body.tier == "quick" or account is None:  # a quick check, or a shopper's free deep check (_free_gate)
        charge = Charge(account)
    else:
        charge = await _debit(request, account)
    return charge


async def _refund(request: Request, charge: Charge) -> int | None:
    """
    Give back what an analysis took: it couldn't be done, failed, or the client hung up.

    Shielded: this often runs while a client disconnect is cancelling the request, and a cancelled refund would keep
    the money.

    :param request: The FastAPI request.
    :param charge: What was taken.
    :return: The account's balance afterwards (``None`` without an account).
    """
    balance: int | None = None
    with anyio.CancelScope(shield=True):
        if charge.paid and charge.account is not None:
            charge.paid = False
            balance = await ledger.credit(request.app.db_pool, charge.account, settings.analysis_price_usd_micros)
        elif charge.spent is not None:
            await free.give_back(request.app.db_pool, charge.spent)
            charge.spent = None
        if balance is None and charge.account is not None:
            balance = await _balance(request, charge.account)
    return balance


async def _balance(request: Request, address: str) -> int:
    return await ledger.balance(request.app.db_pool, address) or 0


def _cache_key(body: AnalyzeRequest) -> str:
    # Keyed on exactly what was sent (see unfret.result_cache): a result is only reused for identical input, so
    # one caller's crafted facts are never served to another. Quick and deep checks are different results.
    return result_cache.key_for(
        url=body.url,
        page_content=body.page_content or "",
        page_facts_json=body.page_facts.model_dump_json() if body.page_facts else "",
        checkout_content=body.checkout_content or "",
        tier="full" if body.tier == "deep" else "quick",
        flags=body.flags,
    )


def _log_usage(usage: UsageMetadataCallbackHandler) -> None:
    # Token counts per model, for pricing. Deliberately nothing that identifies the page or the caller.
    for model, tokens in usage.usage_metadata.items():
        logger.info(
            "analysis_llm_usage",
            model=model,
            input_tokens=tokens.get("input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
        )


def _report_token(body: AnalyzeRequest, account: str | None, result: dict[str, Any]) -> str | None:
    # Lets this account report the result later (POST /v1/reports) without anything being stored now, for a quick
    # check as for a deep one (the token binds the tier). Not without an account: reports are per account (the
    # monthly limit, the reward), so an anonymous quick check gets none.
    if account is None:
        return None
    return reports.issue_token(
        settings.session_secret.get_secret_value(),
        account,
        body,
        score=int(result["score"]),
        explanation=str(result["explanation"]),
        ttl_s=settings.report_window_s,
    )


def _kept(result: dict[str, Any]) -> dict[str, Any]:
    # What the result cache keeps of a finished analysis: the answer, and the title for its share token.
    kept = {k: result[k] for k in _RESULT_KEYS if k in result}
    facts = result.get("facts")
    kept[_TITLE] = facts.title if isinstance(facts, PageFacts) else str(result.get(_TITLE) or "")
    kept[_PRIVATE] = bool(result.get(_PRIVATE)) or result.get("page_type") in SERVICE_TYPES
    return kept


def _response(
    result: dict[str, Any],
    balance: int | None,
    *,
    charged: bool,
    tier: str = "deep",
    free_check: bool = False,
    report_token: str | None = None,
    shared_from: AnalyzeRequest | None = None,
) -> AnalyzeResponse:
    note_usage(payment="pass" if charged and not free_check else "free")
    response = AnalyzeResponse(
        score=result["score"],
        explanation=result["explanation"],
        page_type=result.get("page_type"),
        assessment=result.get("assessment"),
        verdict=result.get("verdict"),
        balance_usd_micros=balance,
        balance_lamports=balance,
        charged=charged,
        tier=tier,
        free_check=free_check or None,
        report_token=report_token,
    )
    secret = settings.session_secret.get_secret_value()
    verdict = result.get("verdict") or {}
    risk = verdict.get("risk") or {}
    if shared_from is not None and settings.crowd_labels and 0 <= int(result["score"]) <= 100:
        # Lets whoever sees this result report it (POST /v1/labels); nothing is stored now, and it names no one.
        response.label_token = crowd.issue_token(
            secret,
            reports.input_hash(shared_from),
            tier=tier,
            score=int(result["score"]),
            level=int(risk["level"]) if "level" in risk else None,
            types=[b["type"] for b in risk.get("because", [])] + [r["type"] for r in verdict.get("reasons", [])],
            ttl_s=settings.crowd_label_token_ttl_s,
        )
    if shared_from is not None and shared_from.tier == "deep" and secret and not result.get(_PRIVATE):
        # Lets whoever sees this result make it public later (POST /v1/shares); it names no account.
        response.share_token = shares.issue_token(
            secret,
            url=shared_from.url,
            title=str(result.get(_TITLE) or ""),
            response=response.model_dump(mode="json", exclude_none=True),
            checkout=bool(shared_from.checkout_content),
            ttl_s=settings.share_token_ttl_s,
        )
    return response


async def deep_cost(
    request: Request, account: str | None, balance: int | None, *, api: bool = False
) -> tuple[str, card.DeepAction]:
    """
    What a deep check would cost this caller, for a quick result's suggestion (:func:`unfret.card.deep_check_cost`).

    On ``/v1/analyze`` a deep check is free unless the caller's bought checks pay for it (then it uses one); on
    ``/v1/check`` a developer pass's free checks count first.

    :param request: The FastAPI request.
    :param account: The account, or ``None``.
    :param balance: The account's balance (micro-dollars), or ``None``.
    :param api: For ``/v1/check``.
    :return: The words and the action.
    """
    pool = request.app.db_pool
    bought = max(balance or 0, 0) // settings.analysis_price_usd_micros
    if api and account is not None:
        key_id = _key_id(account)
        free_left = await free.left(pool, key_id, api=True) if key_id is not None else 0
        paused = bool(free_left) and await free.paused(pool, settings)
        return card.deep_check_cost(free_left=free_left, paused=paused, bought=bought)
    if bought > 0:
        return card.deep_check_cost(bought=bought)
    return card.shopper_deep_check_cost(paused=pool is None or (await spend.ceiling(pool, settings)).reached)


async def _charge_note(request: Request, account: str | None, response: AnalyzeResponse) -> str:
    """
    What the check cost this caller, in one sentence (:func:`unfret.card.charge_note`), with the checks left.

    :param request: The FastAPI request.
    :param account: The account, or ``None``.
    :param response: The response (tier, score, charged, free_check, balance).
    :return: The sentence.
    """
    free_check = bool(response.free_check)
    free_left: int | None = None
    bought_left: int | None = None
    if response.charged and free_check and (key_id := _key_id(account)) is not None:
        free_left = await free.left(request.app.db_pool, key_id, api=True)
    elif response.charged and not free_check and response.balance_usd_micros is not None:
        bought_left = max(response.balance_usd_micros, 0) // settings.analysis_price_usd_micros
    return card.charge_note(
        tier=response.tier or "deep",
        score=response.score,
        charged=response.charged,
        free_check=free_check,
        free_left=free_left,
        bought_left=bought_left,
        shopper=account is None,
    )


async def _offered(request: Request, account: str | None, response: AnalyzeResponse) -> AnalyzeResponse:
    """
    The per-caller parts of a response, which the result cache keeps without.

    What the check cost this caller, and a quick result's suggestion of a deep check, with what one would cost.

    :param request: The FastAPI request.
    :param account: The account, or ``None``.
    :param response: The response.
    :return: The response, with its charge note and its suggestion priced for the caller.
    """
    response.charge_note = await _charge_note(request, account, response)
    offer = response.verdict.deep_check if response.verdict is not None else None
    if response.tier != "quick" or offer is None or not offer.suggested or response.verdict is None:
        return response
    cost = await deep_cost(request, account, response.balance_usd_micros)
    response.verdict.deep_check = card.with_cost(offer, cost)
    return response


async def _cached(
    request: Request, body: AnalyzeRequest, account: str | None, key: str, *, sent: AnalyzeRequest | None = None
) -> AnalyzeResponse | None:
    """
    The same input analysed recently: its result again.

    For a paying account a deep result costs a check, as if it were analysed now, so an account can't tell whether
    anyone checked the page lately; except its own repeat of a listing it paid for within the hour, and a cached page
    that couldn't be assessed (score -1: never charged). A quick check and a shopper's free deep check (which passed
    the same gate as a fresh one: :func:`_free_gate`) cost nothing either way.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account, or ``None`` (a quick check or a free deep check).
    :param key: The result cache key.
    :param sent: The request as the caller sent it, when ``body`` is at the page's own address (a short link): the
        report token is for what the caller will send back.
    :return: The response, or ``None`` on a miss.
    """
    cached = result_cache.get(key)
    if cached is None:
        return None
    if body.tier == "deep" and account is not None and not result_cache.paid_for(account, body.url):
        if cached["score"] == -1:
            await _can_pay(request, account)
        else:
            charge = await _charge(request, body, account)
            logger.info("analysis_cache_hit", charged=charge.charged)
            return await _finished(request, body, charge, cached, sent=sent)
    logger.info("analysis_cache_hit", charged=False)
    balance = None if account is None else await _balance(request, account)
    token = _report_token(sent or body, account, cached)
    shopper = account is None and body.tier == "deep" and cached["score"] != -1
    response = _response(
        cached, balance, charged=False, tier=body.tier, free_check=shopper, report_token=token, shared_from=body
    )
    return await _offered(request, account, response)


async def _finished(
    request: Request,
    body: AnalyzeRequest,
    charge: Charge,
    result: dict[str, Any],
    *,
    sent: AnalyzeRequest | None = None,
) -> AnalyzeResponse:
    """
    The response to a finished analysis: a page that couldn't be analysed (score -1) is given back.

    :param request: The FastAPI request.
    :param body: The request as analysed (at the page's own address).
    :param charge: What the analysis took.
    :param result: The result (as cached).
    :param sent: The request as sent, when that was a link to elsewhere (for the report token).
    :return: The response.
    """
    if result["score"] == -1:
        note_usage(unreadable=True)
        _count_free_outcome(charge)
        response = _response(result, await _refund(request, charge), charged=False, tier=body.tier)
        return await _offered(request, charge.account, response)
    if charge.charged and charge.account is not None:
        result_cache.mark_paid(charge.account, body.url)  # its repeat within the hour is free (reopened, refreshed)
    balance = None if charge.account is None else await _balance(request, charge.account)
    response = _response(
        result,
        balance,
        charged=charge.charged,
        tier=body.tier,
        free_check=charge.spent is not None or (charge.account is None and body.tier == "deep"),
        report_token=_report_token(sent or body, charge.account, result),
        shared_from=body,
    )
    return await _offered(request, charge.account, response)


async def _admit(request: Request, body: AnalyzeRequest, account: str | None) -> str | None:
    """
    Who pays, and the limits before anything runs.

    A quick check's per-minute limit, a free deep check's gate, a paying account's hourly limit on checks that ended
    without a result.

    :param request: The FastAPI request.
    :param body: The request body.
    :param account: The account from ``Authorization``, or ``None``.
    :return: The account that pays (or keeps a quick check's report token), or ``None``.
    """
    note_usage(tier=body.tier)
    account = await _caller(request, body, account)
    note_usage(account=account)
    _quick_limit(request, body, account)
    if body.tier == "deep" and account is None:
        await _free_gate(request, body)
    elif body.tier == "deep" and account is not None:
        _check_free_outcomes(account)
    return account


@router.get("/challenge", response_model=ChallengeResponse, response_model_exclude_none=True)
async def challenge(request: Request) -> ChallengeResponse:
    """
    A proof-of-work challenge for a free deep check: solve it, then send it with the check as ``proof_of_work``.

    Find a ``solution`` (at most 32 characters, e.g. a decimal counter) with ``SHA-256(challenge + ":" + solution)``
    starting with ``bits`` zero bits. Each challenge works once, until ``expires_at``. It names nothing: no client,
    network or page. While the month's free deep checks are paused (``paused``: the LLM spend ceiling), don't solve
    it: the check would be refused with 429 ``FREE_CHECKS_PAUSED``; ``paused_until`` and the rest say why and what to
    show (``message_key``, ``donate_url``). Quick checks need no challenge.

    :param request: The incoming request.
    :return: The challenge, its difficulty and expiry, and whether free deep checks are paused.
    :raises ServiceUnavailableError: While free deep checks can't be offered.
    """
    if not get_credits(request).secret or request.app.db_pool is None:
        raise ServiceUnavailableError("Free deep checks are not available right now. Quick checks still work.")
    text, bits, expires = _deep_challenges(request).issue()
    paused = (await spend.ceiling(request.app.db_pool, settings)).reached
    return ChallengeResponse(
        challenge=text,
        bits=bits,
        expires_at=expires,
        paused=paused,
        pause=FreePause(**paused_metadata()) if paused else None,
    )


@router.post("", response_model=AnalyzeResponse, response_model_exclude_none=True)
async def analyze_page(
    body: AnalyzeRequest,
    request: Request,
    account: str | None = Depends(optional_account),
) -> AnalyzeResponse:
    """
    Analyse a product web page: free for shoppers, or paid from a pass's bought checks.

    Passes the page (client-supplied facts and/or HTML or cleaned text) through the graph, which extracts the
    facts, runs the cheap skills in parallel, lets the triage decide on expensive ones, then synthesises a
    reliability score, an explanation and the shopper verdict (in English).

    **Free (shoppers: the extension, the app, the link checker).** Send no ``Authorization``. ``"tier": "quick"``
    (rules only) needs nothing else, a few a minute. A deep check (the default tier) needs a solved challenge from
    ``GET /v1/analyze/challenge`` as ``proof_of_work`` (428 ``PROOF_OF_WORK_REQUIRED`` without one, 400
    ``BAD_CHALLENGE`` for a bad one), at most ``free_deep_checks_per_ip_per_hour`` per network (429
    ``TOO_MANY_REQUESTS``). Once this month's LLM spend reaches its ceiling, free deep checks are refused with 429
    ``FREE_CHECKS_PAUSED`` (``metadata``: ``reason`` ``spend_ceiling``, ``retry_after_s``, ``quick_checks``,
    ``message_key`` ``free_checks_run_on_donations``, ``donate_url``) until the 1st of next month. Nothing ties a free
    check to anyone: it carries no ``report_token``.

    **Paid (a pass or wallet with bought checks).** ``Authorization: Bearer ufk_…`` (or a wallet's session): a deep
    check uses one bought check, taken up front and given back when the page can't be analysed (score -1) or the
    analysis fails. A pass without bought checks is not used: its deep check is a free one (needs ``proof_of_work``).
    The same input analysed again within the hour costs a paying account a check like a fresh analysis, except for
    the account that already paid for that page within the hour.

    Nothing about the page or the result is stored; the ``report_token`` (a paying account's or a quick check's with
    an account) lets the caller report a wrong result later (``POST /v1/reports``); the ``share_token`` lets anyone
    who has the result make it public as a link (``POST /v1/shares``, without any account).

    A request with only a ``url`` has the server fetch the page first (the web link checker). A page it can't check
    that way is answered with a free 422: ``PAGE_UNAVAILABLE`` (couldn't be fetched), ``PAGE_BLOCKED`` (the shop
    refuses automatic visitors, or answered with a bot check), ``PAGE_TOO_THIN`` (an empty shell that needs a
    browser) or ``INVALID_URL``.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param account: The account (injected by optional_account), or ``None`` without ``Authorization`` (only a quick
        check may do without one).
    :return: Reliability score (0–100), explanation, verdict, the scorer's assessment, the balance left, a report
        token and a share token.
    :raises InternalServerError: If the graph fails.
    :raises GatewayTimeoutError: If the analysis takes longer than ``analysis_timeout_s``.
    :raises asyncio.CancelledError: If the client hung up (after the refund).
    """
    account = await _admit(request, body, account)
    graph, state = _graph_input(body, request)
    key = _cache_key(body)
    if (hit := await _cached(request, body, account, key)) is not None:
        return hit

    sent, body = body, await _fetch_if_url_only(request, body, account, state)
    if body is not sent:  # a short link: the page may have been checked under its own address
        key = _cache_key(body)
        if (hit := await _cached(request, body, account, key, sent=sent)) is not None:
            return hit
    charge = await _charge(request, body, account)
    usage = UsageMetadataCallbackHandler()
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            with observations.observed_as(provenance(request, body, account)):
                result = await graph.ainvoke(state, config={"callbacks": [usage]})
    except TimeoutError as exc:
        logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
        await _refund(request, charge)
        raise GatewayTimeoutError("Analysis took too long.") from exc
    except asyncio.CancelledError:
        _count_free_outcome(charge)
        await _refund(request, charge)  # the client went away before the result
        raise
    except Exception as exc:
        logger.exception("graph_execution_failed")
        await _refund(request, charge)
        raise InternalServerError("Analysis failed.") from exc
    finally:
        _log_usage(usage)
    kept = _kept(result)
    result_cache.put(key, kept)
    skill_counts = Counter(
        m.group(1) for entry in result.get("skill_results", []) if (m := _SKILL_TAG_RE.match(entry))
    )
    logger.info("skills_used", **skill_counts)
    return await _finished(request, body, charge, result if result["score"] == -1 else kept, sent=sent)


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _update_events(update: dict[str, Any], final: dict[str, Any]) -> list[str]:
    # One LangGraph "updates" chunk ({node: state update}) → one `step` event per finished node (contract v1:
    # {"skill", "findings", "check"}, plus "page_type" from the classifier); the score is collected in `final` for
    # `result`. "check" is true for a step the result's checklist counts as a check (unfret.card.is_check): reading
    # the page, classifying it and triage are steps, not checks, so a client counting progress counts these.
    # Steps carry progress only: which skill finished and the types of what it found, never the evidence. The
    # evidence comes with the (charged) result, so reading the steps and hanging up early gets nothing for free.
    events: list[str] = []
    for node, values in update.items():
        if not isinstance(values, dict):
            continue
        final |= {k: values[k] for k in _RESULT_KEYS if k in values}
        if isinstance(values.get("facts"), PageFacts):
            final[_TITLE] = values["facts"].title
        if "score" in values:
            continue  # the judge (or the early exit): reported as the `result` event
        found = [f.model_dump() if isinstance(f, Finding) else dict(f) for f in values.get("findings", []) or []]
        features = values.get("features")
        step: dict[str, Any] = {
            "skill": node,
            "findings": [{"skill": f.get("skill", node), "type": f.get("type", "")} for f in found],
            # One of the checks the result's checklist counts (its checks_run), so progress can count the same unit.
            "check": isinstance(features, dict)
            and card.is_check(node, features, str(final.get("page_type") or "storefront")),
        }
        if "page_type" in values:
            step["page_type"] = values["page_type"]
            final[_PRIVATE] = values["page_type"] in SERVICE_TYPES
        events.append(_event("step", step))
    return events


@router.post("/stream", response_class=StreamingResponse)
async def analyze_page_stream(
    body: AnalyzeRequest,
    request: Request,
    account: str | None = Depends(optional_account),
) -> StreamingResponse:
    """
    Analyse a page like ``POST /v1/analyze``, streaming progress as server-sent events while skills finish.

    Events: one ``step`` per finished node (``{"skill", "findings", "check"}`` with the finding types and whether the
    step is one of the checks the result's ``checks_run`` counts, plus ``page_type`` once classified), then a final
    ``result`` (the ``POST /v1/analyze`` body, with all the evidence) or ``error``.
    Charged like ``POST /v1/analyze`` (a cache hit's result comes at once, charged as there), refunded on ``error``,
    a score of -1, or when the client hangs up before the analysis finished. A finished result is cached first, so a
    client that lost the connection gets it again for free by repeating the request with the same account. A
    URL-only request fetches the page before the stream starts; a page that can't be checked is a free 422 (no
    stream), as for ``POST /v1/analyze``.
    Free checks (``proof_of_work``, the spend ceiling) and ``"tier": "quick"`` work as for ``POST /v1/analyze``.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param account: The account (injected by optional_account), or ``None`` without ``Authorization``.
    :return: A ``text/event-stream`` response.
    """
    account = await _admit(request, body, account)
    graph, state = _graph_input(body, request)
    key = _cache_key(body)
    if (hit := await _cached(request, body, account, key)) is not None:
        return _stream(_replay(hit))

    sent, body = body, await _fetch_if_url_only(request, body, account, state)
    if body is not sent:  # a short link: the page may have been checked under its own address
        key = _cache_key(body)
        if (hit := await _cached(request, body, account, key, sent=sent)) is not None:
            return _stream(_replay(hit))
    charge = await _charge(request, body, account)
    return _stream(_live_events(request, body, charge, graph, state, key=key, sent=sent))


def _stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def _replay(hit: AnalyzeResponse) -> AsyncIterator[str]:
    yield _event("result", hit.model_dump(mode="json", exclude_none=True))


async def _live_events(
    request: Request,
    body: AnalyzeRequest,
    charge: Charge,
    graph: Any,
    state: dict[str, object],
    *,
    key: str,
    sent: AnalyzeRequest | None = None,
) -> AsyncGenerator[str]:
    """
    Run the graph and stream its steps, then the result; refund unless the analysis finished.

    :param request: The FastAPI request.
    :param body: The request as analysed (for the share token).
    :param charge: What the analysis took (already taken).
    :param graph: The compiled graph.
    :param state: The graph input.
    :param key: The result cache key.
    :param sent: The request as sent, when that was a link to elsewhere (for the report token).
    :yield: Server-sent events.
    """
    usage = UsageMetadataCallbackHandler()
    final: dict[str, Any] = {}
    refund = True  # until the analysis has finished (its result is then cached and handed over)
    failed = False  # our side failed (timeout, error), rather than the client hanging up
    try:
        try:
            async with asyncio.timeout(settings.analysis_timeout_s):
                # subgraphs=True: each page class runs in its own subgraph, whose skill steps stream as they finish.
                stream = observations.stream_as(
                    provenance(request, body, charge.account),
                    graph.astream(state, config={"callbacks": [usage]}, stream_mode="updates", subgraphs=True),
                )
                async for _namespace, update in stream:
                    for event in _update_events(update, final):
                        yield event
        except TimeoutError:
            failed = True
            logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
            yield _event("error", {"detail": "Analysis took too long.", "error_code": "GATEWAY_TIMEOUT"})
            return
        except Exception:
            failed = True
            logger.exception("graph_execution_failed")
            yield _event("error", {"detail": "Analysis failed.", "error_code": "INTERNAL_ERROR"})
            return
        finally:
            _log_usage(usage)
        if "score" not in final:
            failed = True
            yield _event("error", {"detail": "Analysis failed.", "error_code": "INTERNAL_ERROR"})
            return
        result_cache.put(key, dict(final))
        refund = False
        result = await _finished(request, body, charge, final, sent=sent)
        yield _event("result", result.model_dump(mode="json", exclude_none=True))
    finally:
        if refund:
            if not failed:
                _count_free_outcome(charge)  # the client hung up before the result
            await _refund(request, charge)
