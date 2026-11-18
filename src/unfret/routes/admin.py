"""
Operator endpoints, behind ``UNFRET_ADMIN_TOKEN``.

Listing the transactions behind the sticky x402 alerts and clearing them after investigating them, removing shops or
contact details from the fingerprint store on request, the queue of shop owners' requests (``/objection``), the
review queue of reported results (``make reports``), a small grant of credit to an API key (support, and test credits
for a store reviewer) and taking it back, the quarterly export of crypto sales records for the bookkeeping, the count
of invites outstanding (a gift, not revenue), intro codes (``python -m unfret.credits.codes``: make, list, switch
off, more uses), the review of shared results a shop owner objected to, how the photo, price and review stores
are fed (counts only), and retiring every pass made before a moment (``python -m unfret.admin_cli retire-passes``).
"""

import json
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from unfret import ad_store, objections, observations, recording, reports, shares, shop_store
from unfret.auth import require_admin
from unfret.config import settings
from unfret.credits import invites, keys, ledger, retire, vat
from unfret.fingerprint import contact_hash, normalise_address, normalise_email, normalise_phone
from unfret.price_store import registrable_domain
from unfret.routes.check import get_x402, get_x402_base

logger = structlog.get_logger("unfret")

router = APIRouter(prefix="/v1/admin", tags=["Admin"], dependencies=[Depends(require_admin)], include_in_schema=False)


@router.post("/x402/clear-alerts")
async def clear_alerts(request: Request, network: Literal["all", "solana", "base"] = "all") -> dict[str, Any]:
    """
    Clear a suspected compromise and post-settlement mismatches, so payments resume.

    Only once the operator has checked the flagged transactions (``GET /v1/admin/x402/alerts``, or
    ``x402_outflow_to_non_cold`` and ``x402_post_settlement_mismatch`` in the logs) and, for a real compromise,
    rotated the hot wallet (Solana, or Base: a new ``UNFRET_X402_BASE_HOT_KEY``) first. Cleared in the database
    too: they don't come back on a restart.

    :param request: The incoming request.
    :param network: Which network's alerts to clear (``?network=solana`` or ``base``; both by default).
    :return: What was cleared, and each network's status now.
    """
    x402, base = get_x402(request), get_x402_base(request)
    cleared = await x402.clear_alerts() if network in {"all", "solana"} else []
    base_cleared = await base.clear_alerts() if network in {"all", "base"} else []
    return {
        "cleared": cleared,
        "x402": (await x402.health()).status,
        "base_cleared": base_cleared,
        "x402_base": (await base.health()).status,
    }


@router.get("/x402/alerts")
async def x402_alerts(request: Request) -> dict[str, Any]:
    """
    The x402 status, its alerts, and each transaction behind a sticky alert with the reason, to check before clearing.

    ``flagged`` lists every unexpected outflow (and audit gap) and post-settlement mismatch not cleared yet:
    ``signature`` (look it up on an explorer), ``reason`` (what moved where) and ``found_at``. Public readiness only
    counts the alerts. ``base`` has the same for x402 on Base.

    :param request: The incoming request.
    :return: ``x402`` (status), ``alerts`` (names), ``flagged`` and ``base``.
    """
    x402, base = get_x402(request), get_x402_base(request)
    health, base_health = await x402.health(), await base.health()
    return {
        "x402": health.status,
        "alerts": list(health.alerts),
        "flagged": [
            {"signature": flag.signature, "reason": flag.reason, "found_at": flag.found_at.isoformat()}
            for flag in x402.flagged()
        ],
        # Base: the hot wallet's outflows (``signature`` is the transaction hash, or the nonces or blocks a flag
        # covers) and post-settlement mismatches.
        "base": {
            "status": base_health.status,
            "network": base.network,
            "pay_to": base.pay_to or None,
            "cold": base.cold or None,
            "usdc_units": base_health.usdc_units,
            "eth_wei": base_health.eth_wei,
            "alerts": list(base_health.alerts),
            "flagged": [
                {"signature": flag.signature, "reason": flag.reason, "found_at": flag.found_at.isoformat()}
                for flag in base.flagged()
            ],
        },
    }


class ForgetContact(BaseModel):
    """
    A merchant's contact detail to remove from the fingerprint store (sent in plaintext, never stored).

    A phone number with its country code (``+31 30 234 5678``), as service sites' numbers are stored that way.
    """

    kind: Literal["email", "phone", "address", "email_domain"]
    value: str = Field(min_length=3, max_length=300)


@router.delete("/shops/{domain}")
async def forget_shop(domain: str) -> dict[str, Any]:
    """
    Remove a shop and its identifiers from the fingerprint store (an objection or removal request).

    :param domain: The shop's domain; its registrable domain is removed.
    :return: Whether it was stored.
    """
    target = registrable_domain(domain)
    removed = await shop_store.store().forget_shop(target)
    logger.info("shop_forgotten", removed=removed)
    return {"domain": target, "removed": removed}


def _email_domain(value: str) -> str | None:
    domain = value.strip().lstrip("@").lower()
    return domain if "." in domain and "@" not in domain else None


@router.post("/contacts/forget")
async def forget_contact(body: ForgetContact) -> dict[str, Any]:
    """
    Remove a contact detail wherever it is linked (a merchant's objection or erasure request).

    :param body: The contact, normalised and hashed here exactly as when it was stored.
    :return: How many shop links and ad contacts were removed.
    :raises HTTPException: 422 if it isn't a valid address or number, 503 without a contact key.
    """
    normalise = {
        "email": normalise_email,
        "phone": normalise_phone,
        "address": normalise_address,
        "email_domain": _email_domain,
    }[body.kind]
    value = normalise(body.value)
    if value is None:
        raise HTTPException(422, "Not a valid contact.")
    key = shop_store.contact_key()
    if key is None:
        raise HTTPException(503, "No contact hash key configured, so no contacts are stored.")
    removed = await shop_store.store().forget_contact(body.kind, contact_hash(body.kind, value, key).hex())
    # Ads store phone numbers in their comparable form (country code added), so remove that form too.
    ad_values = {value, ad_store.comparable_phone(value) or value} if body.kind == "phone" else {value}
    for ad_value in ad_values:
        removed += await ad_store.store().forget_contact(contact_hash(body.kind, ad_value, key))
    logger.info("contact_forgotten", removed=removed)
    return {"removed": removed}


@router.get("/objections")
async def objection_queue(request: Request, limit: int = 100) -> dict[str, Any]:
    """
    Shop owners' and sellers' requests from ``/objection``, oldest first, as they sent them.

    Answer at ``reply_to`` (if given), act (``DELETE /v1/admin/shops/{domain}``, ``POST /v1/admin/contacts/forget``,
    or a look at the result), then delete the request (``DELETE /v1/admin/objections/{id}``).

    :param request: The incoming request.
    :param limit: How many (at most 500).
    :return: How many are open, and the requests.
    """
    pool = _pool(request)
    items = await objections.queue(pool, min(max(limit, 1), 500))
    return {"open": await pool.fetchval("SELECT COUNT(*) FROM objections"), "items": items}


@router.delete("/objections/{objection_id}")
async def objection_handled(objection_id: int, request: Request) -> dict[str, Any]:
    """
    Delete a shop owner's request once it is handled.

    :param objection_id: The request.
    :param request: The incoming request.
    :return: ``{"deleted": true}``.
    :raises HTTPException: 404 if there is none.
    """
    if not await objections.remove(_pool(request), objection_id):
        raise HTTPException(404, "No such request.")
    return {"deleted": True}


# Per store: rows, rows that count in verdicts, and rows seen in quick checks / client content / pages Unfret fetched.
_OBSERVED = """
SELECT {table!r} AS store, COUNT(*) AS rows,
       COUNT(*) FILTER (WHERE provenance & $1 <> 0 OR sources >= $2) AS counting,
       COUNT(*) FILTER (WHERE provenance & $3 <> 0) AS quick,
       COUNT(*) FILTER (WHERE provenance & $4 <> 0) AS client,
       COUNT(*) FILTER (WHERE provenance & $5 <> 0) AS fetched
FROM {table}
"""
_OBSERVED_SQL = " UNION ALL ".join(
    _OBSERVED.format(table=t)
    for t in ("image_prints", "price_observations", "market_prices", "review_fingerprints", "shop_links")
)


@router.get("/observations")
async def observed(request: Request) -> dict[str, Any]:
    """
    How free checks feed the stores: counts only, nothing about a page, shop or caller.

    ``stores`` has, per table, its rows, those that count in verdicts (seen on a page Unfret fetched, kept from before,
    or corroborated by ``min_sources`` sources), and those seen in quick checks, in client-supplied content and on
    pages Unfret fetched. ``quick_recording`` counts what this process recorded after quick checks since it started
    (pages, photos, prices, market prices, reviews) and what it skipped (queue full, over a network's daily limit,
    timed out, failed).

    :param request: The incoming request.
    :return: ``min_sources``, ``stores`` and ``quick_recording``.
    """
    rows = await _pool(request).fetch(
        _OBSERVED_SQL,
        *observations.trusted_params(),
        observations.QUICK,
        observations.CLIENT,
        observations.FETCHED,
    )
    return {
        "min_sources": settings.observation_min_sources,
        "stores": {r["store"]: {k: r[k] for k in ("rows", "counting", "quick", "client", "fetched")} for r in rows},
        "quick_recording": dict(recording.stats),
    }


@router.get("/reports")
async def report_queue(
    request: Request,
    limit: int = 50,
    order: reports.Order = "value",
    agreement: reports.Agreement | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """
    The review queue: open reports with their second opinions, without the reporters' accounts.

    :param request: The incoming request.
    :param limit: How many items (at most 1000).
    :param order: ``value`` (worth most first) or ``confidence`` (the most confident proposals first).
    :param agreement: Only items whose second opinion sides with the ``reporter``, the ``score``, ``neither``, is
        ``unsure``, or has ``none`` yet.
    :param min_confidence: Only items with a proposed label at least this confident (the preview of a bulk confirm).
    :return: The items, how many are open, and the second opinions' states.
    """
    pool = _pool(request)
    await reports.expire(pool, settings)
    items = await reports.queue(
        pool,
        settings,
        min(max(limit, 1), 1000),
        order=order,
        agreement_is=agreement,
        min_confidence=min_confidence,
    )
    for item in items:
        item["payload"] = json.loads(item["payload"])
        item["created_at"] = item["created_at"].isoformat()
    states = await pool.fetch("SELECT opinion_state, COUNT(*) AS n FROM reports GROUP BY opinion_state")
    return {
        "open": await pool.fetchval("SELECT COUNT(*) FROM reports"),
        "cap": settings.report_queue_cap,
        "second_opinions": {row["opinion_state"]: row["n"] for row in states},
        "items": items,
    }


class LabelRequest(BaseModel):
    """The human decision on a reported page."""

    label: Literal["legit", "junk", "scam", "reject"]


def _handled(done: reports.Handled) -> dict[str, Any]:
    return {
        "label": done.label,
        "gap": done.gap,
        "reward_checks": done.reward_checks,
        "reward_usd_micros": done.reward_usd_micros,
        "training": done.training,
    }


@router.post("/reports/{report_id}/label")
async def label_report(report_id: int, body: LabelRequest, request: Request) -> dict[str, Any]:
    """
    Label a reported page: pay the first reporter if the score was off, keep the page as training data, unlink it.

    :param report_id: The item.
    :param body: The label, or ``reject`` (not a product page, unusable, can't tell): no sample, no reward.
    :param request: The incoming request.
    :return: The gap, the reward and whether the page counts as training data.
    :raises HTTPException: 404 if the item is gone (handled or expired).
    """
    label = None if body.label == "reject" else body.label
    done = await reports.handle(_pool(request), settings, report_id, label)
    if done is None:
        raise HTTPException(404, "No such open report.")
    return _handled(done)


class ConfirmedProposal(BaseModel):
    """One previewed second opinion the operator confirms as their label."""

    id: int
    label: reports.Label


class ConfirmRequest(BaseModel):
    """Second opinions confirmed in bulk, after a preview (``GET /v1/admin/reports?min_confidence=…``)."""

    min_confidence: float = Field(ge=0.5, le=1, description="The confidence the preview was filtered by.")
    items: list[ConfirmedProposal] = Field(min_length=1, max_length=500)


@router.post("/reports/confirm")
async def confirm_reports(body: ConfirmRequest, request: Request) -> dict[str, Any]:
    """
    Confirm previewed second opinions as the operator's labels, in one go.

    Only items that still carry exactly the proposal shown (that label, at least ``min_confidence``) are labelled;
    the rest stay in the queue for one-by-one review. Each label then does what a typed one does.

    :param body: The confirmed ``(id, label)`` pairs and the preview's confidence.
    :param request: The incoming request.
    :return: Per item what happened, and the totals.
    """
    results = await reports.confirm_proposals(
        _pool(request), settings, [(item.id, item.label) for item in body.items], body.min_confidence
    )
    out = [{"id": rid, "outcome": outcome, **(_handled(done) if done else {})} for rid, outcome, done in results]
    labelled = [r for r in out if r["outcome"] == "labelled"]
    logger.info("reports_confirmed", labelled=len(labelled), left=len(out) - len(labelled))
    return {
        "labelled": len(labelled),
        "left": len(out) - len(labelled),
        "reward_checks": sum(r["reward_checks"] for r in labelled),
        "items": out,
    }


# A grant is for support and store reviews, not a way to mint money: one grant is at most this much.
MAX_GRANT_USD_MICROS = 10_000_000


class GrantRequest(BaseModel):
    """Credit to add to an API key, in micro-dollars."""

    usd_micros: int = Field(gt=0, le=MAX_GRANT_USD_MICROS)


@router.post("/keys/{key_id}/grant")
async def grant_key(key_id: str, body: GrantRequest, request: Request) -> dict[str, Any]:
    """
    Add a small amount of credit to an existing API key (support, or test checks for a store reviewer).

    :param key_id: The key's public id (the part after ``ufk_``).
    :param body: How much, at most $10 per grant.
    :param request: The incoming request.
    :return: The key's new balance.
    :raises HTTPException: 404 for an id that isn't an existing key.
    """
    pool = request.app.db_pool
    if not keys.is_key_id(key_id) or not await keys.exists(pool, key_id):
        raise HTTPException(404, "No such API key.")
    balance = await ledger.credit(pool, keys.account_of(key_id), body.usd_micros)
    logger.info("key_granted", usd_micros=body.usd_micros)
    return {"key_id": key_id, "balance_usd_micros": balance}


@router.post("/keys/{key_id}/debit")
async def debit_key(key_id: str, body: GrantRequest, request: Request) -> dict[str, Any]:
    """
    Take a small amount of credit back from an API key (undo a grant; the e2e sweep nets its test key to zero).

    Never below zero: a debit larger than the balance is refused and changes nothing.

    :param key_id: The key's public id (the part after ``ufk_``).
    :param body: How much, at most $10 per call.
    :param request: The incoming request.
    :return: The key's new balance.
    :raises HTTPException: 404 for an id that isn't an existing key, 409 if the balance is smaller.
    """
    pool = request.app.db_pool
    if not keys.is_key_id(key_id) or not await keys.exists(pool, key_id):
        raise HTTPException(404, "No such API key.")
    balance = await ledger.debit(pool, keys.account_of(key_id), body.usd_micros)
    if balance is None:
        raise HTTPException(409, "The key's balance is smaller than that.")
    logger.info("key_debited", usd_micros=body.usd_micros)
    return {"key_id": key_id, "balance_usd_micros": balance}


@router.get("/sales/{quarter}", response_class=PlainTextResponse)
async def sales_export(quarter: str, request: Request) -> PlainTextResponse:
    """
    A quarter's crypto credit sales as CSV, for the bookkeeping.

    Date, country, net, VAT rate, VAT, asset, amount and
    transaction signature per sale (card sales are the payment provider's).

    :param quarter: ``YYYY-Qn``, e.g. ``2026-Q4``.
    :param request: The incoming request.
    :return: The CSV.
    :raises HTTPException: For a malformed quarter (400) or without a database (503).
    """
    try:
        vat.quarter_bounds(quarter)
    except ValueError as exc:
        raise HTTPException(400, "Give the quarter as YYYY-Qn.") from exc
    if request.app.db_pool is None:
        raise HTTPException(503, "Database not ready.")
    name = f"unfret-crypto-sales-{quarter.upper()}.csv"
    return PlainTextResponse(
        await vat.sales_csv(request.app.db_pool, quarter),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/invites")
async def invites_summary(request: Request) -> dict[str, int]:
    """
    Invites issued, redeemed and outstanding, and what the outstanding ones would credit (gifts: not in the sales).

    :param request: The incoming request.
    :return: The counts.
    :raises HTTPException: Without a database (503).
    """
    if request.app.db_pool is None:
        raise HTTPException(503, "Database not ready.")
    return await invites.summary(request.app.db_pool, settings)


class RetireRequest(BaseModel):
    """Retire every pass made before a moment, and every wallet account (dry run unless ``confirm``)."""

    before: datetime = Field(description="ISO date or time (UTC unless it says otherwise); not in the future.")
    keep: list[str] = Field(default_factory=list, max_length=100, description="Key ids of passes to keep.")
    confirm: bool = False
    include_wallet_balances: bool = False
    include_purchases: bool = False


@router.post("/passes/retire")
async def retire_passes(body: RetireRequest, request: Request) -> dict[str, Any]:
    """
    Count, and with ``confirm`` retire, the passes made before ``before`` (:mod:`unfret.credits.retire`).

    :param body: The moment, the passes to keep and the switches.
    :param request: The incoming request.
    :return: Counts only (never a key, key id or wallet); ``retired`` once done, ``refused`` with the reasons.
    :raises HTTPException: 422 for a moment in the future or a malformed key id.
    """
    try:
        report = await retire.retire_passes(
            _pool(request),
            before=body.before,
            keep=[k.strip() for k in body.keep],
            confirm=body.confirm,
            settings=settings,
            include_wallet_balances=body.include_wallet_balances,
            include_purchases=body.include_purchases,
        )
    except retire.RetireError as exc:
        raise HTTPException(422, str(exc)) from exc
    return report.as_dict()


# ── Intro codes ────────────────────────────────────────────────────────────────────────────────────────────────


class IntroCreate(BaseModel):
    """A new intro code."""

    code: str = Field(min_length=3, max_length=24, description="3 to 24 letters or digits (stored upper case).")
    checks: int = Field(default=5, gt=0, le=20)
    max_uses: int = Field(gt=0, le=10_000)


class IntroUpdate(BaseModel):
    """Switch a code on or off, or change its number of uses."""

    active: bool | None = None
    add_uses: int = Field(default=0, ge=-10_000, le=10_000)


@router.get("/codes")
async def codes_list(request: Request) -> dict[str, Any]:
    """
    Every intro code with its uses, today's checks given against the cap, and buyers by how they came.

    :param request: The incoming request.
    :return: The codes and counts.
    """
    return await invites.list_intros(_pool(request), settings)


@router.post("/codes", status_code=201)
async def codes_create(body: IntroCreate, request: Request) -> dict[str, Any]:
    """
    Make an intro code.

    :param body: Its name, checks per use and number of uses.
    :param request: The incoming request.
    :return: The code.
    :raises HTTPException: 400 for a malformed name, 409 if it exists.
    """
    try:
        made = await invites.create_intro(_pool(request), body.code, body.checks, body.max_uses)
    except invites.InviteError as exc:
        raise HTTPException(400 if exc.kind == "invalid" else 409, str(exc)) from exc
    logger.info("intro_code_created", checks=made.checks, max_uses=made.max_uses)
    return made.__dict__


@router.post("/codes/{code}")
async def codes_update(code: str, body: IntroUpdate, request: Request) -> dict[str, Any]:
    """
    Switch an intro code on or off, or change its number of uses.

    :param code: The name.
    :param body: What to change.
    :param request: The incoming request.
    :return: The code.
    :raises HTTPException: 404 for an unknown code.
    """
    changed = await invites.update_intro(_pool(request), code, active=body.active, add_uses=body.add_uses)
    if changed is None:
        raise HTTPException(404, "No such code.")
    return changed.__dict__


def _pool(request: Request) -> Any:
    if request.app.db_pool is None:
        raise HTTPException(503, "Database not ready.")
    return request.app.db_pool


# ── Shared results ───────────────────────────────────────────────────────────────────────────────────────────────


def _share_for_operator(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **shares.public_view(row),
        "checked_on": row["checked_on"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "hidden_reason": row["hidden_reason"],
    }


@router.get("/shares")
async def share_list(request: Request, hidden: bool = True, limit: int = 100) -> dict[str, Any]:
    """
    Shared results for review: by default the ones hidden after an objection.

    :param request: The incoming request.
    :param hidden: Only hidden ones (``false``: all, hidden first).
    :param limit: How many (at most 500).
    :return: The shares, as the public would see them, with why they are hidden.
    """
    rows = await shares.listing(_pool(request), hidden_only=hidden, limit=min(max(limit, 1), 500))
    return {"items": [_share_for_operator(row) for row in rows]}


@router.post("/shares/{share_id}/unhide")
async def share_unhide(share_id: str, request: Request) -> dict[str, Any]:
    """
    Put a hidden shared result back after review (the objection didn't hold).

    :param share_id: The share.
    :param request: The incoming request.
    :return: ``{"unhidden": true}``.
    :raises HTTPException: 404 if no hidden share has this id.
    """
    if not await shares.unhide(_pool(request), share_id):
        raise HTTPException(404, "No hidden share with this id.")
    return {"unhidden": True}


@router.delete("/shares/{share_id}")
async def share_delete(share_id: str, request: Request) -> dict[str, Any]:
    """
    Delete a shared result for good (after review of an objection).

    :param share_id: The share.
    :param request: The incoming request.
    :return: ``{"deleted": true}``.
    :raises HTTPException: 404 if there is none.
    """
    if not await shares.remove(_pool(request), share_id):
        raise HTTPException(404, "No such share.")
    return {"deleted": True}
