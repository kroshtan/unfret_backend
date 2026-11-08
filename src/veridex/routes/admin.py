"""Operator endpoints, behind ``VERIDEX_ADMIN_TOKEN``: clearing the sticky x402 alerts after investigating them."""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from veridex.auth import require_admin
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
