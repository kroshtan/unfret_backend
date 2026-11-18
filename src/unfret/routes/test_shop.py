"""
The Unfret test shop (:mod:`unfret.test_shop`): a made-up store at ``/test-shop`` with every trick Unfret catches.

Three static pages from ``static/test-shop``: the product page, its checkout and the sister shop's product page (the
same made-up photo under another made-up brand, for the photo match: :mod:`unfret.test_shop_photos`). Served under
the site's Content Security Policy (no inline scripts, nothing loaded from elsewhere) and kept out of search engines.
The reviews are dated relative to the day the page is served, as review widgets show them, so the review burst stays a
burst.
"""

import re
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from unfret.routes.pages import PAGE_HEADERS, STATIC_DIR, versioned
from unfret.test_shop import PATH
from unfret.test_shop_photos import SISTER_PATH

SHOP_DIR = STATIC_DIR / "test-shop"
HEADERS = {**PAGE_HEADERS, "X-Robots-Tag": "noindex, nofollow"}
# ``{{iso:3}}`` and ``{{date:3}}``: the day three days before today, as 2026-11-14 and as Nov 14, 2026.
_DAYS_AGO_RE = re.compile(r"\{\{(iso|date):(\d{1,3})\}\}")

router = APIRouter(include_in_schema=False)


def render(name: str, today: date | None = None) -> str:
    """
    A test shop page's HTML, its review dates filled in and its ``/static/`` addresses versioned.

    :param name: The page's file in ``static/test-shop``.
    :param today: The day to date the reviews from (default: today, UTC).
    :return: The HTML.
    """
    day = today or datetime.now(UTC).date()

    def fill(m: re.Match[str]) -> str:
        then = day - timedelta(days=int(m.group(2)))
        return then.isoformat() if m.group(1) == "iso" else f"{then:%b} {then.day}, {then.year}"

    return versioned(_DAYS_AGO_RE.sub(fill, (SHOP_DIR / name).read_text(encoding="utf-8")))


@router.get(PATH, response_class=HTMLResponse)
def product() -> HTMLResponse:
    """
    The test shop's product page: an invented star projector with every trick on it.

    :return: The page.
    """
    return HTMLResponse(render("product.html"), headers=HEADERS)


@router.get(f"{PATH}/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """
    The test shop's checkout: the add-on still ticked, and fees that appear only here.

    :return: The page.
    """
    return HTMLResponse(render("checkout.html"), headers=HEADERS)


@router.get(SISTER_PATH, response_class=HTMLResponse)
def sister_shop() -> HTMLResponse:
    """
    The sister shop's product page: the same made-up projector and photo, cheaper, under another made-up brand.

    :return: The page.
    """
    return HTMLResponse(render("sister.html"), headers=HEADERS)
