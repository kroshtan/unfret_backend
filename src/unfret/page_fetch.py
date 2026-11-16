"""
Fetch a shop's page on the caller's behalf, and tell a page we can read from one we can't.

Used when a caller sends only a URL (the web link checker, agents over x402). A page is only worth a check when the
server got the real thing: not an error, not a bot-check interstitial and not an empty shell that a script fills in
later. Each of those is a :class:`PageUnreadableError`, raised before any money moves, so it never costs a check.

Nothing here logs or keeps the URL: the error's ``detail`` is a fixed sentence.
"""

from typing import Literal

import httpx

from unfret.config import settings
from unfret.facts.extract import parse_page
from unfret.facts.service_platforms import SERVICE_PLATFORMS
from unfret.graph.platforms import platform_rule
from unfret.net import UnresolvableHostError, UnsafeURLError, fetch_public

Reason = Literal["PAGE_UNAVAILABLE", "PAGE_BLOCKED", "PAGE_TOO_THIN", "INVALID_URL"]

# Interstitials served instead of the page (Cloudflare, DataDome, PerimeterX, Amazon, Fastly's client challenge);
# markers, not generic captchas, so a shop with a reCAPTCHA newsletter form still counts as a page.
CHALLENGE_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "<title>just a moment...</title>",
    "<title>attention required! | cloudflare</title>",
    "captcha-delivery.com",
    "px-captcha",
    "/errors/validatecaptcha",
    "<title>robot check</title>",
    "<title>client challenge</title>",
    "/_fs-ch-",
)
# Answers that mean "not for robots" rather than "no such page": refused, rate limited, or a bot wall's status
# (Cloudflare and DataDome answer 403 or 503, LinkedIn-style walls 999).
_BLOCKING_STATUSES = frozenset({401, 403, 405, 406, 429, 503, 999})
# Below this much visible text, without a price or product data either, the page is a shell its scripts fill in.
MIN_TEXT_CHARS = 400

_DETAILS: dict[Reason, str] = {
    "PAGE_UNAVAILABLE": "The page couldn't be fetched.",
    "PAGE_BLOCKED": "The shop blocks automatic checks of this page.",
    "PAGE_TOO_THIN": "The page shows too little without a browser to be checked.",
    "INVALID_URL": "That isn't a public web address.",
}
# A platform that answers a server with its page but leaves the profile out of it (Houzz's /professionals/ pages).
_PROFILE_WITHHELD = (
    "The platform doesn't show this profile to automatic checks: check it in your browser with the Unfret extension."
)
_UNREACHABLE = "The site couldn't be reached: its address doesn't lead anywhere (any more)."


class PageUnreadableError(Exception):
    """The page can't be checked from a server-side fetch; ``reason`` says why."""

    def __init__(self, reason: Reason, detail: str = "") -> None:
        """
        Describe why the page can't be checked.

        :param reason: One of :data:`Reason`.
        :param detail: A more precise message than the reason's own.
        """
        self.reason: Reason = reason
        self.detail = detail or _DETAILS[reason]
        super().__init__(self.detail)


def is_challenge_page(html: str) -> bool:
    """
    Whether this is a bot-check interstitial rather than the page.

    :param html: The page.
    :return: ``True`` for Cloudflare, DataDome, PerimeterX and Amazon challenge pages.
    """
    head = html[:200_000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def is_too_thin(html: str, url: str) -> bool:
    """
    Whether the page has too little in it to judge: hardly any visible text, no price and no product data.

    Single-page shops send an empty shell and build the page in the browser; checking the shell would judge a
    page nobody sees.

    :param html: The page.
    :param url: Its URL (for the facts).
    :return: ``True`` when there is nothing to go on.
    """
    facts, text = parse_page(html, url)
    return len(text.strip()) < MIN_TEXT_CHARS and facts.price is None and not facts.json_ld


def is_withheld_profile(html: str, url: str) -> bool:
    """
    Whether this is a service platform's profile page that came without the profile.

    A platform (Houzz, behind Fastly) may answer a server with its page frame but none of the provider's rating,
    reviews or membership: a check of that would say "a few things to check" about nothing at all. Only while the
    page classes beyond products are on (with them off these pages aren't checked as profiles anyway).

    :param html: The page.
    :param url: Its URL.
    :return: ``True`` for a known service platform's profile URL whose page shows no profile.
    """
    if not settings.expansion_classes:
        return False
    facts, text = parse_page(html, url)
    if facts.platform not in SERVICE_PLATFORMS:
        return False
    rule = platform_rule(facts, url, text)
    return rule is not None and rule[0] == "service_listing" and facts.seller_profile is None and not facts.reviews


def _reason_for(exc: Exception) -> Reason:
    if isinstance(exc, UnresolvableHostError):
        return "PAGE_UNAVAILABLE"  # a dead domain is unreachable, not a private address
    if isinstance(exc, UnsafeURLError):
        return "INVALID_URL"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _BLOCKING_STATUSES:
        return "PAGE_BLOCKED"
    return "PAGE_UNAVAILABLE"


async def fetch_page(url: str) -> str:
    """
    Fetch a page for analysis, refusing anything that isn't the real, readable page.

    :param url: The page's absolute http(s) URL.
    :return: Its HTML.
    :raises PageUnreadableError: If it can't be fetched, the shop refuses us, it is a bot check or it is too thin.
    """
    try:
        raw = await fetch_public(url, max_bytes=settings.max_page_bytes)
    except Exception as exc:
        detail = _UNREACHABLE if isinstance(exc, UnresolvableHostError) else ""
        raise PageUnreadableError(_reason_for(exc), detail) from exc
    html = raw.decode("utf-8", errors="replace")
    if is_challenge_page(html):
        raise PageUnreadableError("PAGE_BLOCKED")
    if is_too_thin(html, url):
        raise PageUnreadableError("PAGE_TOO_THIN")
    if is_withheld_profile(html, url):
        raise PageUnreadableError("PAGE_BLOCKED", _PROFILE_WITHHELD)
    return html
