"""
The two pages the credits door needs: ``/connect`` (sign in, hand the session to the extension) and ``/checkout``.

Static HTML and vanilla JS from ``veridex/static``, no framework and no third-party requests; a strict Content Security
Policy lets them load only their own scripts and talk only to this API.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

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

router = APIRouter(include_in_schema=False)


def _page(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / name).read_text(encoding="utf-8"), headers=PAGE_HEADERS)


@router.get("/connect", response_class=HTMLResponse)
def connect() -> HTMLResponse:
    """
    Sign in with a wallet; the page hands the session to the browser extension via ``window.postMessage``.

    :return: The page.
    """
    return _page("connect.html")


@router.get("/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """
    Buy credits with SOL from a browser wallet or, by Solana Pay QR code, from a phone.

    :return: The page.
    """
    return _page("checkout.html")
