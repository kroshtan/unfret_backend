"""
Operator endpoints, behind ``VERIDEX_ADMIN_TOKEN``.

Clearing the sticky x402 alerts after investigating them, removing shops or contact details from the
fingerprint store on request, the review queue of reported results (``make reports``), a small grant of credit to
an API key (support, and test credits for a store reviewer), and the quarterly export of crypto sales records for
the bookkeeping.
"""

import json
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from veridex import reports, shop_store
from veridex.auth import require_admin
from veridex.config import settings
from veridex.credits import keys, ledger, vat
from veridex.fingerprint import contact_hash, normalise_email, normalise_phone
from veridex.price_store import registrable_domain
from veridex.routes.check import get_x402

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/admin", tags=["Admin"], dependencies=[Depends(require_admin)], include_in_schema=False)


@router.post("/x402/clear-alerts")
async def clear_alerts(request: Request) -> dict[str, Any]:
    """
    Clear a suspected compromise and post-settlement mismatches, so payments resume.

    Only once the operator has checked the flagged transactions (``x402_outflow_to_non_cold`` and
    ``x402_post_settlement_mismatch`` in the logs) and, for a real compromise, rotated the hot wallet first.

    :param request: The incoming request.
    :return: What was cleared, and the x402 status now.
    """
    x402 = get_x402(request)
    cleared = x402.clear_alerts()
    return {"cleared": cleared, "x402": (await x402.health()).status}


class ForgetContact(BaseModel):
    """A merchant's contact detail to remove from the fingerprint store (sent in plaintext, never stored)."""

    kind: Literal["email", "phone"]
    value: str = Field(min_length=3, max_length=254)


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


@router.post("/contacts/forget")
async def forget_contact(body: ForgetContact) -> dict[str, Any]:
    """
    Remove a contact detail wherever it is linked (a merchant's objection or erasure request).

    :param body: The contact, normalised and hashed here exactly as when it was stored.
    :return: How many shop links were removed.
    :raises HTTPException: 422 if it isn't a valid address or number, 503 without a contact key.
    """
    value = normalise_email(body.value) if body.kind == "email" else normalise_phone(body.value)
    if value is None:
        raise HTTPException(422, "Not a valid contact.")
    key = shop_store.contact_key()
    if key is None:
        raise HTTPException(503, "No contact hash key configured, so no contacts are stored.")
    removed = await shop_store.store().forget_contact(body.kind, contact_hash(body.kind, value, key).hex())
    logger.info("contact_forgotten", removed=removed)
    return {"removed": removed}


@router.get("/reports")
async def report_queue(request: Request, limit: int = 50) -> dict[str, Any]:
    """
    The review queue: open reports, most corroborated and least certain first, without the reporters' accounts.

    :param request: The incoming request.
    :param limit: How many items (at most 200).
    :return: The items and how many are open.
    """
    pool = request.app.db_pool
    await reports.expire(pool, settings)
    items = await reports.queue(pool, min(max(limit, 1), 200))
    for item in items:
        item["payload"] = json.loads(item["payload"])
        item["created_at"] = item["created_at"].isoformat()
    return {"open": await pool.fetchval("SELECT COUNT(*) FROM reports"), "items": items}


class LabelRequest(BaseModel):
    """The human decision on a reported page."""

    label: Literal["legit", "junk", "scam", "reject"]


@router.post("/reports/{report_id}/label")
async def label_report(report_id: int, body: LabelRequest, request: Request) -> dict[str, Any]:
    """
    Label a reported page: pay the reporter if the score was far off, keep the page as training data, unlink it.

    :param report_id: The item.
    :param body: The label, or ``reject`` (not a product page, unusable, can't tell): no sample, no reward.
    :param request: The incoming request.
    :return: The gap, the reward and whether the page counts as training data.
    :raises HTTPException: 404 if the item is gone (handled or expired).
    """
    label = None if body.label == "reject" else body.label
    done = await reports.handle(request.app.db_pool, settings, report_id, label)
    if done is None:
        raise HTTPException(404, "No such open report.")
    return {
        "label": done.label,
        "gap": done.gap,
        "reward_usd_micros": done.reward_usd_micros,
        "training": done.training,
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
