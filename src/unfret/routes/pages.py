"""
The site's pages: home, sign-in, checkout, the link checker, accuracy, and the pages about Unfret.

``/`` introduces Unfret (``/nl`` is its one Dutch counterpart), ``/welcome`` greets a new extension install,
``/connect`` signs in and hands the session to the extension, then ``/checkout`` (which hands a new API key to the
extension), ``/affiliates`` (the program) and ``/affiliates/stats`` (an affiliate's own numbers). ``/about``,
``/why``, ``/money``, ``/press``, ``/terms``, ``/privacy`` and ``/objection`` (for shop owners) are in the footer.
``/check`` checks a link from any browser, phones first; with its web app manifest and service worker it can be
installed and shows up in Android's share sheet.

Static HTML and vanilla JS from ``unfret/static``, no framework and no third-party requests; a strict Content Security
Policy lets them load only their own scripts and talk only to this API.
"""

import html
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from unfret import __version__, accuracy, clients_info
from unfret.config import settings
from unfret.credits import invites, packs
from unfret.credits.keys import MAX_EXTRA_BITS
from unfret.prices import format_usd
from unfret.routes.check import get_x402_base
from unfret.x402.verdict import POLICY_THRESHOLDS, tier_description

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
    # A site that opens one of these pages gets no handle on it (it can't navigate it on, say from /check#pass=… to
    # /checkout?send=pass); popups the page opens itself (a wallet's) keep theirs.
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
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


def _data_block(value: Any) -> str:
    # JSON inside <script type="application/json">: nothing in it can close the element or start markup.
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def fill_page(name: str, fields: dict[str, Any], data: Any, markup: dict[str, str] | None = None) -> str:
    """
    A result page from its template, in one pass (so text from a page can't bring in a placeholder of its own).

    :param name: The template's file name in the static directory.
    :param fields: Text for ``{{name}}`` placeholders, HTML-escaped.
    :param data: The result, for the ``{{data}}`` block (JSON, made safe inside a script element).
    :param markup: Trusted HTML for other placeholders.
    :return: The page.
    """
    values = {key: html.escape(str(value)) for key, value in fields.items()} | (markup or {})
    values["data"] = _data_block(data)
    page = versioned(template(name))
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), page)


router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    """
    The home page: what Unfret is, for shoppers and for agents (also the homepage of the store listing).

    The agents' section quotes the x402 prices from the same settings as ``/developers``, and the page names ``/nl``
    as its Dutch counterpart.

    :return: The page.
    """
    page = with_store_links(template("index.html", "/")).replace("{{hreflang}}", hreflang_links())
    fields = developer_fields()
    for name in ("base_url", "quick_price", "deep_price"):
        page = page.replace("{{" + name + "}}", html.escape(fields[name]))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


def hreflang_links() -> str:
    """
    The home page and the Dutch landing page name each other as the same page in another language.

    :return: ``<link rel="alternate">`` elements for English (also the default) and Dutch, with absolute addresses.
    """
    base = html.escape(settings.public_base_url.rstrip("/"))
    return "\n  ".join(
        f'<link rel="alternate" hreflang="{lang}" href="{base}{path}">'
        for lang, path in (("en", "/"), ("nl", "/nl"), ("x-default", "/"))
    )


@router.get("/nl", response_class=HTMLResponse)
def dutch() -> HTMLResponse:
    """
    The one Dutch page: check the seller before you pay, with Marktplaats as the example, and iDEAL | Wero.

    Everything else is on the English pages it links to; the shared header and footer stay English, marked as such.

    :return: The page.
    """
    page = template("nl.html").replace("{{hreflang}}", hreflang_links())
    page = page.replace('<header class="site-header">', '<header class="site-header" lang="en">', 1)
    page = page.replace('<footer class="site-footer">', '<footer class="site-footer" lang="en">', 1)
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


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


def developer_fields() -> dict[str, str]:
    """
    What the pages for developers and agents say about x402: the address, the network and each tier's price.

    :return: Text for the ``{{base_url}}``, ``{{network_name}}``, ``{{quick_price}}``, ``{{deep_price}}``,
        ``{{quick_includes}}`` and ``{{deep_includes}}`` placeholders (not yet HTML-escaped).
    """
    network = "Solana" if settings.solana_network == "solana-mainnet" else f"Solana {settings.solana_network[7:]}"
    return {
        "base_url": settings.public_base_url.rstrip("/"),
        "network_name": network,
        "quick_price": format_usd(settings.x402_price_quick_usd_micros),
        "deep_price": format_usd(settings.x402_price_deep_usd_micros),
        "quick_includes": tier_description("quick"),
        "deep_includes": tier_description("deep"),
    }


_BASE_NAMES = {"base-mainnet": "Base", "base-sepolia": "Base Sepolia"}
_COMING_BASE = re.compile(r"\s*<!-- base-off -->.*?<!-- /base-off -->", re.DOTALL)
# The sample response and the median latencies on /developers: measured on a running server (dev), never made up.
# Re-measure with scripts/measure_developers.py, which rewrites this file.
DEVELOPERS_MEASURED = STATIC_DIR / "developers-measured.json"


def _seconds(value: float) -> str:
    return f"{value:.1f} s"


def _limits_and_measurements() -> dict[str, str]:
    """
    The pass's prices and limits (from the settings) and what was measured on a running server.

    :return: Text for the ``/developers`` placeholders about packs, limits, the sample response and the times.
    """
    measured = json.loads(DEVELOPERS_MEASURED.read_text(encoding="utf-8"))
    sample = measured["sample"]
    deep, quick = measured["deep"], measured["quick"]
    return {
        "developer_checks": str(settings.developer_free_checks),
        "packs_line": ", ".join(f"{p.checks} for {format_usd(p.card_usd_micros)}" for p in packs.packs(settings)),
        "check_per_minute": str(settings.x402_check_per_minute),
        "quick_per_minute": str(settings.quick_checks_per_minute),
        "fetch_per_minute": str(settings.analyze_fetch_per_minute),
        "free_outcomes_per_hour": str(settings.analyze_free_outcomes_per_hour),
        "max_page_mb": f"{settings.max_page_bytes / 1024 / 1024:g}",
        "analysis_timeout": f"{settings.analysis_timeout_s:g}",
        "x402_timeout": f"{min(settings.analysis_timeout_s, settings.x402_analysis_timeout_s):g}",
        "sample_server": measured["server"],
        "measured_on": measured["measured_on"],
        "sample_json": json.dumps(sample, indent=2, ensure_ascii=False),
        "sample_action": str(sample.get("action")),
        "sample_band": str(sample.get("band")),
        "sample_risk": str(sample.get("risk_score")),
        "latency_server": measured["server"],
        "deep_median": _seconds(deep["median_s"]),
        "quick_median": _seconds(quick["median_s"]),
        "latency_note": (
            f"the median of {deep['runs']} fresh deep and {quick['runs']} quick checks of the test shop on "
            f"{measured['server']} (the test server, on {measured['measured_on']}), from request to answer, with "
            "Unfret fetching the page; deep ranged "
            f"{_seconds(deep['min_s'])}–{_seconds(deep['max_s'])}. Production runs on other hardware and other pages "
            "take more or less time; these are dev's figures, not a promise."
        ),
    }


def _payment_options(request: Request) -> tuple[str, bool]:
    """
    What ``/v1/check`` takes over x402 right now, in words, and whether Base is among it.

    :param request: The incoming request (for the Base payment service).
    :return: ``("USDC and wSOL on Solana devnet[, and USDC on Base Sepolia]", base_on)``.
    """
    network = developer_fields()["network_name"]
    base_on = get_x402_base(request).configured
    options = f"USDC and wSOL on {network}"
    if base_on:
        options += f", and USDC on {_BASE_NAMES[settings.base_network]}"
    return options, base_on


def python_install_html() -> str:
    """
    How to install the Python client, as HTML for ``/developers`` (see :mod:`unfret.clients_info`).

    :return: Trusted markup: ``pip install unfret-client``, and before its first release on PyPI the source install,
        each as a code block with a Copy button.
    """
    block = (
        '<div class="code-block">\n          <pre class="terminal" id="{id}">{command}</pre>\n'
        '          <button type="button" class="copy" data-copy="{id}">Copy</button>\n        </div>'
    )
    pypi = block.format(id="client-python-pip", command="pip install unfret-client")
    if clients_info.PYTHON_CLIENT_ON_PYPI:
        return pypi
    source = html.escape(f'pip install "{clients_info.PYTHON_CLIENT_SOURCE}"')
    return (
        '<p class="small">From PyPI once its first release is out (it comes with Unfret\'s next release):</p>\n'
        f"        {pypi}\n"
        '        <p class="small">Until then, from the source:</p>\n'
        f"        {block.format(id='client-python-source', command=source)}"
    )


_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _pow_example(bits: int) -> str:
    """
    What ``bits`` leading zero bits mean for the digest's bytes, for ``/developers/http``.

    :param bits: The difficulty.
    :return: E.g. "the first 2 bytes are 0 and the next one is below 64" for 18.
    """
    whole, rest = divmod(bits, 8)
    zero = "the first byte is 0" if whole == 1 else f"the first {whole} bytes are 0"
    if not rest:
        return zero
    below = f"{'the first byte' if not whole else 'the next one'} is below {2 ** (8 - rest)}"
    return below if not whole else f"{zero} and {below}"


@router.get("/developers", response_class=HTMLResponse)
def developers(request: Request) -> HTMLResponse:
    """
    For developers and AI agents: three-line calls, a real response, prices, speed and limits, the MCP server.

    The tiers' prices, the pass's limits and what they include come from the settings (the same as
    ``/.well-known/x402``), so the page can't drift from what a check costs; the networks are those ``/v1/check``
    takes (Base only while it is set up); the sample response and the median times come from
    ``developers-measured.json``, measured on a running server and stated as that server's figures.

    :param request: The incoming request (for the Base payment service).
    :return: The page.
    """
    options, base_on = _payment_options(request)
    fields = {**developer_fields(), **_limits_and_measurements(), "payment_options": options}
    page = template("developers.html", "/developers").replace("{{python_install}}", python_install_html())
    if base_on:  # no longer "coming next"
        page = _COMING_BASE.sub("", page)
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", html.escape(value))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/developers/http", response_class=HTMLResponse)
def developers_http(request: Request) -> HTMLResponse:
    """
    Use Unfret from any language: the two ways to pay, a developer pass in code, the request, the response, errors.

    Every number comes from the settings (prices, limits, the proof of work's difficulty and lifetime) or from
    ``developers-measured.json`` (the times), like ``/developers``; the examples are run in the tests
    (``tests/unit/test_developers_http.py``) against the server's own proof-of-work check and models.

    :param request: The incoming request (for the Base payment service).
    :return: The page.
    """
    options, base_on = _payment_options(request)
    warn, block = POLICY_THRESHOLDS["balanced"]
    bits, passes = settings.key_pow_bits, settings.developer_passes_per_ip_per_day
    fields = {
        **developer_fields(),
        **_limits_and_measurements(),
        "payment_options": options,
        "pow_bits": str(bits),
        "pow_example": _pow_example(bits),
        "pow_hashes": f"{2**bits:,}",
        "pow_extra_max": str(MAX_EXTRA_BITS),
        "pow_ttl_min": f"{settings.key_pow_ttl_s / 60:g}",
        "developer_passes_per_day": _count(passes) + (" developer pass" if passes == 1 else " developer passes"),
        "key_create_per_day": str(settings.key_create_per_ip_per_day),
        "balanced_warn": f"{warn:g}",
        "balanced_block": f"{block:g}",
    }
    page = template("developers-http.html")
    if base_on:  # Base is on: nothing to announce
        page = _COMING_BASE.sub("", page)
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", html.escape(value))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/accuracy", response_class=HTMLResponse)
def accuracy_page() -> HTMLResponse:
    """
    How often the verdicts are right: the method, the numbers per verdict, the limits and the scorer's history.

    The numbers come from ``static/accuracy.json`` (the open baseline) and ``static/accuracy-history.json`` (every
    published scorer version, the retrained one unfret.net runs included), both from ``make accuracy`` (aggregates of
    a corpus replay, no corpus records), filled into the page here so it needs no script (:mod:`unfret.accuracy`).

    :return: The page.
    """
    page = accuracy.render(template("accuracy.html", "/accuracy"))
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


@router.get("/affiliates", response_class=HTMLResponse)
def affiliates_page() -> HTMLResponse:
    """
    The affiliate program: how it works, the numbers, and the disclosure rules affiliates follow.

    :return: The page.
    """
    page = with_affiliate_numbers(with_store_links(template("affiliates.html", "/affiliates")))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


def with_affiliate_numbers(page: str) -> str:
    """
    Fill in the affiliate program's numbers from the settings, so the program page and the terms say the same.

    :param page: A page's HTML with ``{{bonus_pct}}``, ``{{commission_pct}}``, ``{{months}}``, ``{{hold_days}}`` and
        ``{{min_payout}}``.
    :return: The HTML with them filled in.
    """
    minimum = settings.affiliate_min_payout_cents // 100
    for name, value in {
        "bonus_pct": settings.affiliate_bonus_pct,
        "commission_pct": settings.affiliate_commission_pct,
        "months": settings.affiliate_months,
        "hold_days": settings.affiliate_hold_days,
        "min_payout": f"${minimum} or €{minimum}",
    }.items():
        page = page.replace("{{" + name + "}}", str(value))
    return page


@router.get("/affiliates/stats", response_class=HTMLResponse)
def affiliate_stats_page() -> HTMLResponse:
    """
    An affiliate's own numbers, reached through their private link (the token is in the link's fragment).

    :return: The page.
    """
    return _page("affiliate-stats.html")


@router.get("/about", response_class=HTMLResponse)
def about() -> HTMLResponse:
    """
    About Unfret: what it does, who makes it, open source, and how it makes money in short.

    :return: The page.
    """
    return _page("about.html", "/about")


@router.get("/why", response_class=HTMLResponse)
def why() -> HTMLResponse:
    """
    Why Unfret: questions to ask any checker, and Unfret next to free scam checkers and AI assistants.

    Compares with kinds of tools, never a named one (Fakespot, closed in 2025, only as the closed service it is). The
    free tier's numbers come from the settings, like the home page's.

    :return: The page.
    """
    page = with_store_links(template("why.html", "/why"))
    for name, value in {
        "free_checks": settings.free_checks_per_pass,
        "quick_per_minute": settings.quick_checks_per_minute,
    }.items():
        page = page.replace("{{" + name + "}}", str(value))
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/money", response_class=HTMLResponse)
def money() -> HTMLResponse:
    """
    How Unfret makes money: paid checks, and nothing else.

    :return: The page.
    """
    return _page("money.html", "/money")


@router.get("/press", response_class=HTMLResponse)
def press() -> HTMLResponse:
    """
    For the press: a fact sheet, the logo files and screenshots of the test shop.

    :return: The page.
    """
    return _page("press.html", "/press")


# Shown at the top of /terms while UNFRET_TERMS_DRAFT is on: for a new version of the terms that isn't approved yet.
TERMS_DRAFT_NOTE = (
    '<aside class="panel notice draft-note" role="note" aria-label="Draft">\n'
    "      <p><strong>Draft, not in force.</strong> These terms are a draft and don't apply until they are\n"
    "        approved.</p>\n"
    "    </aside>"
)


@router.get("/terms", response_class=HTMLResponse)
def terms() -> HTMLResponse:
    """
    The terms of use for the site, the extension, the app and the API; marked as a draft while ``terms_draft`` is on.

    The affiliate section's numbers come from the same settings as ``/affiliates``.

    :return: The page.
    """
    page = template("terms.html", "/terms").replace("{{draft_note}}", TERMS_DRAFT_NOTE if settings.terms_draft else "")
    page = with_affiliate_numbers(page)
    return HTMLResponse(versioned(page), headers=PAGE_HEADERS)


@router.get("/objection", response_class=HTMLResponse)
def objection() -> HTMLResponse:
    """
    For shop owners and sellers: what Unfret keeps about shops, and a form to object, correct or remove.

    The form posts to ``/v1/objections`` (:mod:`unfret.routes.objections`) from its script; the page itself is static.

    :return: The page.
    """
    return _page("objection.html", "/objection")


@router.get("/checkout", response_class=HTMLResponse)
def checkout() -> HTMLResponse:
    """
    Get checks: by card, with an intro code, as a top-up of a key, or with SOL or USDC.

    A new key shown there is handed to the Unfret extension in this browser, if it answers (``handoff.js``).

    :return: The page.
    """
    return _page("checkout.html", "/checkout")


# What the welcome page says above its three ways in when the extension's new pass came without free checks.
WELCOME_NOTES = {
    "had-free": "This browser has had its free checks already, so your new Unfret pass starts without them. Quick "
    "checks stay free; for deep checks, buy a pack or use a gift code from a friend.",
    "none-today": "There were no free checks to give just now, so your new Unfret pass starts without them. Quick "
    "checks stay free; for deep checks, buy a pack or use a gift code from a friend.",
}
# Above it all when the extension's old pass was retired (``?renewed=1``, the 0.13.0 fresh start): calm, one line.
RENEWED_NOTE = '<p class="note" role="status">Unfret has a new free start: your old pass has ended.</p>'


@router.get("/welcome", response_class=HTMLResponse)
def welcome(free: int = 0, setup: int = 0, note: str = "", renewed: int = 0) -> HTMLResponse:
    """
    The first page after installing the extension (it opens it once).

    With ``?setup=1`` (extension 0.12.0, on install or from "Get your free checks"): "Getting your free checks
    ready", while ``welcome.js`` tells the extension whether this browser already had its free checks (the link
    checker's note, a yes or no) and the extension makes its pass; the page then moves on to one of the others. With
    ``?free=<n>`` (the pass came with that many free checks): "You have n free checks", and how to check a page.
    Without: a code, buying checks, or a pass you have; with ``?note=had-free`` or ``?note=none-today`` saying why
    the extension's new pass has no free checks. ``?renewed=1`` (with any of them) adds one line: the extension's old
    pass ended (it was retired), and this is the new free start.

    :param free: The free checks the extension's new pass came with.
    :param setup: 1 while the extension makes its pass.
    :param note: Why the extension's new pass came without free checks.
    :param renewed: 1 when the extension's old pass ended and it made a new one.
    :return: The page.
    """
    ended = RENEWED_NOTE if renewed == 1 else ""
    if setup == 1:
        page = with_store_links(template("welcome-setup.html", cta=("/check", "Check a link")))
        return HTMLResponse(versioned(page.replace("{{renewed_note}}", ended)), headers=PAGE_HEADERS)
    if 0 < free <= 100:
        page = with_store_links(template("welcome-free.html", cta=("/check", "Check a link")))
        page = page.replace("{{free_checks}}", str(free)).replace("{{renewed_note}}", ended)
        return HTMLResponse(versioned(page), headers=PAGE_HEADERS)
    text = WELCOME_NOTES.get(note)
    shown = ended + (f'<p class="note" role="status">{html.escape(text)}</p>' if text else "")
    page = with_store_links(template("welcome.html", cta=("/checkout", "Buy checks")))
    return HTMLResponse(versioned(page.replace("{{free_note}}", shown)), headers=PAGE_HEADERS)


# Big shops that turn the server's fetch away (a refusal, a bot check, or a page that only its scripts fill in), so a
# link from them can't be checked on /check: the extension and the Android app read them in the shopper's own browser.
# Each was tried with a product page and its home page from unfret.net's server (2026-11-17). Technical notes, never a
# judgement of the shop. ``hosts`` let /check's script recognise a link before it asks (check.js).
BROWSER_ONLY_SHOPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AliExpress", ("aliexpress.com", "aliexpress.us")),
    ("Allbirds", ("allbirds.com", "allbirds.eu", "allbirds.co.uk", "allbirds.ca", "allbirds.com.au")),
    ("Best Buy", ("bestbuy.com", "bestbuy.ca")),
    ("bol.com", ("bol.com",)),
    ("Coolblue", ("coolblue.nl", "coolblue.be", "coolblue.de")),
    ("Costco", ("costco.com", "costco.ca", "costco.co.uk")),
    ("De Bijenkorf", ("debijenkorf.nl", "debijenkorf.be")),
    (
        "Decathlon",
        ("decathlon.com", "decathlon.nl", "decathlon.be", "decathlon.fr", "decathlon.de", "decathlon.es"),
    ),
    (
        "eBay",
        ("ebay.com", "ebay.nl", "ebay.de", "ebay.co.uk", "ebay.fr", "ebay.it", "ebay.es", "ebay.be", "ebay.at"),
    ),
    ("Etsy", ("etsy.com",)),
    ("Fnac", ("fnac.com", "fnac.be", "fnac.es", "fnac.pt")),
    ("Gamma", ("gamma.nl", "gamma.be")),
    ("H&M", ("hm.com",)),
    ("Home Depot", ("homedepot.com", "homedepot.ca")),
    ("MediaMarkt", ("mediamarkt.nl", "mediamarkt.de", "mediamarkt.be", "mediamarkt.es", "mediamarkt.at")),
    ("Otto", ("otto.de",)),
    ("Temu", ("temu.com",)),
    ("Wehkamp", ("wehkamp.nl",)),
)


def browser_only_list() -> str:
    """
    The shops of :data:`BROWSER_ONLY_SHOPS` as list items, each with its hosts for the page's script.

    :return: ``<li>`` elements.
    """
    return "\n          ".join(
        f'<li data-hosts="{html.escape(" ".join(hosts))}">{html.escape(name)}</li>'
        for name, hosts in BROWSER_ONLY_SHOPS
    )


@router.get("/check", response_class=HTMLResponse)
def check_page() -> HTMLResponse:
    """
    The link checker: paste or share a product link and check it, signed in with a wallet or an API key.

    The page is static: a shared link (``?url=``, ``?text=``, ``?title=``, or the same after ``#``) is read by its
    script in the browser, never by the server, and the script removes it from the address bar.

    :return: The page.
    """
    examples = "\n        ".join(
        f'<li><button type="button" class="link" data-example="{html.escape(link)}">{html.escape(label)}</button></li>'
        for label, link in settings.check_examples
    )
    page = (
        template("check.html", "/check")
        .replace("{{examples}}", examples)
        .replace("{{browser_only_shops}}", browser_only_list())
    )
    return HTMLResponse(versioned(page), headers=CHECK_PAGE_HEADERS)


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
