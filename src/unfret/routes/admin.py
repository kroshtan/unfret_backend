"""
Operator endpoints, behind ``UNFRET_ADMIN_TOKEN``.

Clearing the sticky x402 alerts after investigating them, removing shops or contact details from the
fingerprint store on request, the review queue of reported results (``make reports``), a small grant of credit to
an API key (support, and test credits for a store reviewer), the quarterly export of crypto sales records for
the bookkeeping, and the "Spotted recently" showcase (``python -m unfret.showcase``): operator-run checks, drafts
until published.
"""

import json
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from unfret import page_fetch, reports, shop_store, showcase
from unfret.auth import require_admin
from unfret.config import settings
from unfret.credits import keys, ledger, vat
from unfret.fingerprint import contact_hash, normalise_email, normalise_phone
from unfret.price_store import registrable_domain
from unfret.routes.check import get_x402

logger = structlog.get_logger("unfret")

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


# ── Showcase ("Spotted recently") ─────────────────────────────────────────────────────────────────────────────────


class ShowcaseAdd(BaseModel):
    """A public product page for the operator's own check."""

    url: str = Field(min_length=8, max_length=showcase.MAX_URL_CHARS)
    origin: showcase.Origin = "operator"
    title: str | None = Field(default=None, max_length=showcase.MAX_TITLE_CHARS)


class ShowcaseRemove(BaseModel):
    """Why a case comes off the site."""

    reason: str = Field(min_length=1, max_length=500)


class ShowcaseTitle(BaseModel):
    """The title to show with a case."""

    title: str = Field(min_length=1, max_length=showcase.MAX_TITLE_CHARS)


def _pool(request: Request) -> Any:
    if request.app.db_pool is None:
        raise HTTPException(503, "Database not ready.")
    return request.app.db_pool


def _operator_view(row: dict[str, Any]) -> dict[str, Any]:
    """
    A case for the operator: everything stored (but the screenshot's bytes) and what the public would see.

    :param row: The case.
    :return: The case, its public view and what that view leaves out.
    """
    left_out: list[str] = []
    public = showcase.public_view(row, left_out)
    return {
        "id": row["id"],
        "status": row["status"],
        "origin": row["origin"],
        "title": row["title"],
        "host": row["host"],
        "product_url": row["product_url"],
        "checked_at": row["checked_at"].isoformat(),
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        "removed_at": row["removed_at"].isoformat() if row["removed_at"] else None,
        "removed_reason": row["removed_reason"],
        "screenshot": {"type": row["screenshot_type"], "bytes": row["screenshot_bytes"]}
        if row["screenshot_type"]
        else None,
        "response": row["response"],
        "public": {k: v for k, v in public.items() if k not in {"checked_at", "published_at"}},
        "left_out": left_out,
    }


async def _case_or_404(pool: Any, case_id: int) -> dict[str, Any]:
    row = await showcase.get(pool, case_id)
    if row is None:
        raise HTTPException(404, "No such case.")
    return row


@router.post("/showcase", status_code=201)
async def showcase_add(body: ShowcaseAdd, request: Request) -> dict[str, Any]:
    """
    Check a public product page as the operator and keep it as a draft case (nothing is public until published).

    The server fetches the page and runs the full analysis, like a URL-only check, without any account or charge.

    :param body: The page's address, where it came from and optionally the title to show.
    :param request: The incoming request.
    :return: The draft.
    :raises HTTPException: 422 when the page can't be read or wasn't assessed, 504 when the analysis took too
        long, 503 without the graph or database.
    """
    pool = _pool(request)
    if request.app.graph is None:
        raise HTTPException(503, "Analysis graph is not initialised.")
    try:
        analysed = await showcase.analyse(
            request.app.graph, body.url, timeout_s=settings.analysis_timeout_s, title=body.title
        )
    except page_fetch.PageUnreadableError as exc:
        raise HTTPException(422, f"{exc.reason}: {exc.detail}") from exc
    except showcase.ShowcaseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "The analysis took too long.") from exc
    case_id = await showcase.add(pool, analysed, body.origin)
    return _operator_view(await _case_or_404(pool, case_id))


@router.get("/showcase")
async def showcase_list(request: Request, status: showcase.Status | None = None, limit: int = 100) -> dict[str, Any]:
    """
    The cases, newest first.

    :param request: The incoming request.
    :param status: Only drafts, published or removed cases.
    :param limit: How many (at most 500).
    :return: The cases.
    """
    rows = await showcase.cases(_pool(request), status, min(max(limit, 1), 500))
    return {"items": [_operator_view(row) for row in rows]}


@router.get("/showcase/{case_id}")
async def showcase_show(case_id: int, request: Request) -> dict[str, Any]:
    """
    One case, with what the public would see.

    :param case_id: The case.
    :param request: The incoming request.
    :return: The case.
    """
    return _operator_view(await _case_or_404(_pool(request), case_id))


@router.post("/showcase/{case_id}/publish")
async def showcase_publish(case_id: int, request: Request) -> dict[str, Any]:
    """
    Publish a draft on the site.

    :param case_id: The case.
    :param request: The incoming request.
    :return: The published case.
    :raises HTTPException: 409 if it isn't a draft or can't be shown.
    """
    pool = _pool(request)
    await _case_or_404(pool, case_id)
    try:
        row = await showcase.publish(pool, case_id)
    except showcase.ShowcaseError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _operator_view(row)


@router.post("/showcase/{case_id}/remove")
async def showcase_remove(case_id: int, body: ShowcaseRemove, request: Request) -> dict[str, Any]:
    """
    Take a case off the site for good (its screenshot is dropped; the reason is kept).

    :param case_id: The case.
    :param body: Why.
    :param request: The incoming request.
    :return: The removed case.
    :raises HTTPException: 409 if it was removed already.
    """
    pool = _pool(request)
    await _case_or_404(pool, case_id)
    if not await showcase.remove(pool, case_id, body.reason):
        raise HTTPException(409, "The case was removed already.")
    return _operator_view(await _case_or_404(pool, case_id))


@router.post("/showcase/{case_id}/title")
async def showcase_title(case_id: int, body: ShowcaseTitle, request: Request) -> dict[str, Any]:
    """
    Change the title shown with a case.

    :param case_id: The case.
    :param body: The title.
    :param request: The incoming request.
    :return: The case.
    :raises HTTPException: 409 if it was removed.
    """
    pool = _pool(request)
    await _case_or_404(pool, case_id)
    try:
        changed = await showcase.set_title(pool, case_id, body.title)
    except showcase.ShowcaseError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(409, "The case was removed.")
    return _operator_view(await _case_or_404(pool, case_id))


@router.put("/showcase/{case_id}/screenshot")
async def showcase_screenshot(case_id: int, request: Request) -> dict[str, Any]:
    """
    Attach a screenshot (the request body: a PNG, JPEG or WebP image of at most 800 kB).

    :param case_id: The case.
    :param request: The incoming request.
    :return: The case.
    :raises HTTPException: 422 for a file that isn't such an image.
    """
    pool = _pool(request)
    await _case_or_404(pool, case_id)
    try:
        await showcase.set_screenshot(pool, case_id, await request.body())
    except showcase.ShowcaseError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _operator_view(await _case_or_404(pool, case_id))
