"""
The site's pages: home, sign-in, checkout, privacy and the link checker.

``/`` introduces Unfret, ``/welcome`` greets a new extension install, ``/connect`` signs in and hands the session to
the extension, then ``/checkout`` (which hands a new API key to the extension) and ``/privacy``. ``/check`` checks a
link from any browser, phones first; with its web app manifest and service worker it can be installed and shows up in
Android's share sheet.

Static HTML and vanilla JS from ``unfret/static``, no framework and no third-party requests; a strict Content Security
Policy lets them load only their own scripts and talk only to this API.
"""

import html
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from unfret import __version__
from unfret.config import settings
from unfret.credits import invites
from unfret.prices import format_usd
from unfret.x402.verdict import tier_description

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Pages may load only their own scripts, styles and font, images from here (the QR code) or inline data (wallet
# icons, the icons in the stylesheet), and call only this API. No framing (clickjacking a sign-in), no forms posting
# elsewhere.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}

# The link checker is also an installable web app: it may load its manifest and register its service worker.
CHECK_PAGE_HEADERS = {
    **PAGE_HEADERS,
    "Content-Security-Policy": PAGE_HEADERS["Content-Security-Policy"] + "; manifest-src 'self'; worker-src 'self'",
}

# Every page names its scripts, styles and images under a folder for the running build (/static/v-<commit>/…), which
# the static server ignores when it finds the file. A deploy changes every address, so no cache (Cloudflare's included)
# can hand a new page an old stylesheet, and the scripts' relative imports stay within the same build.
_ASSET_TAG_RE = re.compile(r"[^0-9A-Za-z.]")


def asset_tag() -> str:
    """
    The folder name for this build's static files.

    :return: ``v-`` and the first 12 characters of the commit (the version outside a built image).
    """
    return "v-" + _ASSET_TAG_RE.sub("", settings.git_sha.strip() or __version__)[:12]


def versioned(page: str) -> str:
    """
    Point a page's ``/static/`` addresses at this build's folder.

    :param page: The page's HTML.
    :return: The HTML with ``"/static/`` turned into ``"/static/v-<build>/``.
    """
    return page.replace('"/static/', f'"/static/{asset_tag()}/')


PARTIALS_DIR = STATIC_DIR / "partials"
_NAV_LINK_RE = r'(<a\b[^>]*\bhref="{}")'


def _indented(block: str, indent: str) -> str:
    return block.rstrip("\n").replace("\n", "\n" + indent)


_CTA_RE = re.compile(r'<a class="cta" href="[^"]*">[^<]*</a>')


def template(name: str, current: str | None = None, cta: tuple[str, str] | None = None) -> str:
    """
    A page's HTML with the shared header and footer in place of ``{{header}}`` and ``{{footer}}``.

    The header and footer come from ``static/partials``, with the page's own menu link marked as the current page.
    Every page has the same header and footer, so a change to the menu is made once. The result still carries the
    page's other placeholders and its ``/static/`` addresses unversioned (see :func:`versioned`).

    :param name: The page's file in the static directory.
    :param current: The page's own path (``/checkout``), marked ``aria-current="page"`` in the menu.
    :param cta: The menu's button as ``(href, text)``, for a page where "Get Unfret" makes no sense (``/welcome``).
    :return: The HTML.
    """
    page = (STATIC_DIR / name).read_text(encoding="utf-8")
    header = (PARTIALS_DIR / "header.html").read_text(encoding="utf-8")
    if current:
        logo, nav = header.split("<nav", 1)
        nav = re.sub(_NAV_LINK_RE.format(re.escape(current)), r'\1 aria-current="page"', nav, count=1)
        header = f"{logo}<nav{nav}"
    if cta:
        href, text = cta
        header = _CTA_RE.sub(f'<a class="cta" href="{html.escape(href)}">{html.escape(text)}</a>', header, count=1)
    footer = (PARTIALS_DIR / "footer.html").read_text(encoding="utf-8")
    return page.replace("{{header}}", _indented(header, "  ")).replace("{{footer}}", _indented(footer, "  "))


router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    """
    The home page: what Unfret is, for shoppers and for agents (also the homepage of the store listing).

    :return: The page.
    """
    return _page("index.html", "/")


def with_store_links(template: str) -> str:
    """
    Fill in the extension's store listings (Chrome Web Store, Firefox Add-ons).

    :param template: A page's HTML with ``{{store_url}}`` and ``{{firefox_url}}``.
    :return: The HTML with both filled in.
    """
    return template.replace("{{store_url}}", html.escape(settings.chrome_store_url)).replace(
        "{{firefox_url}}", html.escape(settings.firefox_store_url)
    )


def _page(name: str, current: str | None = None, cta: tuple[str, str] | None = None) -> HTMLResponse:
    page = with_store_links(template(name, current, cta))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/key", include_in_schema=False)
def key_link(code: str = "") -> RedirectResponse:
    """
    The link an intro code is shared as (``/key?code=FRIENDS``): checkout for an API key, the code filled in.

    :param code: The intro code; anything else is dropped.
    :return: A redirect to ``/checkout?for=key`` (with ``&code=`` for a valid code).
    """
    name = invites.normalise_intro(code)
    return RedirectResponse(f"/checkout?for=key&code={name}" if name else "/checkout?for=key", status_code=302)


@router.get("/connect", response_class=HTMLResponse)
def connect() -> HTMLResponse:
    """
    Sign in with a wallet; the page hands the session to the browser extension via ``window.postMessage``.

    :return: The page.
    """
    return _page("connect.html", "/connect")


@router.get("/developers", response_class=HTMLResponse)
def developers() -> HTMLResponse:
    """
    For developers and AI agents: the MCP server, x402 on ``/v1/check`` and a prepaid pass as an API key.

    The tiers' prices and what they include come from the same settings as ``/.well-known/x402``, so the page can't
    drift from what a check costs.

    :return: The page.
    """
    network = "Solana" if settings.solana_network == "solana-mainnet" else f"Solana {settings.solana_network[7:]}"
    fields = {
        "base_url": settings.public_base_url.rstrip("/"),
        "network_name": network,
        "quick_price": format_usd(settings.x402_price_quick_usd_micros),
        "deep_price": format_usd(settings.x402_price_deep_usd_micros),
        "quick_includes": tier_description("quick"),
        "deep_includes": tier_description("deep"),
    }
    page = template("developers.html", "/developers")
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", html.escape(value))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/my-checks", response_class=HTMLResponse)
def my_checks() -> HTMLResponse:
    """
    My checks: the shopper's Unfret pass (an API key) or wallet on this device, and what to do with it.

    Adding checks, putting the pass on a phone, and a new pass from the backup code (the recovery code). Static like
    every page: it reads the link checker's sign-in from the browser's storage, never from the server.

    :return: The page.
    """
    return _page("my-checks.html", "/my-checks")


@router.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    """
    The privacy statement the extension's consent screen links to.

    :return: The page.
    """
    return _page("privacy.html", "/privacy")


@router.get("/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """
    Get checks: by card, with an intro code, as a top-up of a key, or with SOL or USDC.

    A new key shown there is handed to the Unfret extension in this browser, if it answers (``handoff.js``).

    :return: The page.
    """
    return _page("checkout.html", "/checkout")


@router.get("/welcome", response_class=HTMLResponse)
def welcome() -> HTMLResponse:
    """
    The first page after installing the extension (it opens it once): a code, buying checks, or a key you have.

    :return: The page.
    """
    return _page("welcome.html", cta=("/checkout?mode=code", "I have a code"))


@router.get("/check", response_class=HTMLResponse)
def check_page() -> HTMLResponse:
    """
    The link checker: paste or share a product link and check it, signed in with a wallet or an API key.

    The page is static: a shared link (``?url=``, ``?text=``, ``?title=``, or the same after ``#``) is read by its
    script in the browser, never by the server, and the script removes it from the address bar.

    :return: The page.
    """
    return HTMLResponse(versioned(template("check.html", "/check")), headers=CHECK_PAGE_HEADERS)


@router.get("/manifest.webmanifest")
def manifest() -> Response:
    """
    The web app manifest: "Unfret", opening at ``/check``, and a share target so shared links land there.

    :return: The manifest.
    """
    return Response(
        (STATIC_DIR / "manifest.webmanifest").read_bytes(),
        media_type="application/manifest+json",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )


@router.get("/check-sw.js")
def service_worker() -> Response:
    """
    The link checker's service worker: makes the app installable and moves a shared link out of the request.

    It caches nothing. A share opens ``/check?url=…``; the worker answers that navigation itself with a redirect to
    ``/check#url=…``, so the shared link never reaches the network or any server log.

    :return: The script.
    """
    return Response(
        (STATIC_DIR / "check-sw.js").read_bytes(),
        media_type="text/javascript",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )
