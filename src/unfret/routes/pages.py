"""
The site's pages: home, sign-in, checkout, privacy and the link checker.

``/`` introduces Unfret, ``/connect`` signs in and hands the session to the extension, then ``/checkout`` and
``/privacy``. ``/check`` checks a link from any browser, phones first; with its web app manifest and service worker
it can be installed and shows up in Android's share sheet.

Static HTML and vanilla JS from ``unfret/static``, no framework and no third-party requests; a strict Content Security
Policy lets them load only their own scripts and talk only to this API.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from unfret.credits import invites

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Pages may load only their own scripts and styles, images from here (the QR code) or inline data (wallet icons),
# and call only this API. No framing (clickjacking a sign-in), no forms posting elsewhere.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
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

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    """
    The home page: what Unfret is, for shoppers and for agents (also the homepage of the store listing).

    :return: The page.
    """
    return _page("index.html")


def _page(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / name).read_text(encoding="utf-8"), headers=PAGE_HEADERS)


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
    return _page("connect.html")


@router.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    """
    The privacy statement the extension's consent screen links to.

    :return: The page.
    """
    return _page("privacy.html")


@router.get("/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """
    Buy credits with SOL from a browser wallet or, by Solana Pay QR code, from a phone.

    :return: The page.
    """
    return _page("checkout.html")


@router.get("/check", response_class=HTMLResponse)
def check_page() -> HTMLResponse:
    """
    The link checker: paste or share a product link and check it, signed in with a wallet or an API key.

    The page is static: a shared link (``?url=``, ``?text=``, ``?title=``, or the same after ``#``) is read by its
    script in the browser, never by the server, and the script removes it from the address bar.

    :return: The page.
    """
    return HTMLResponse((STATIC_DIR / "check.html").read_text(encoding="utf-8"), headers=CHECK_PAGE_HEADERS)


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
