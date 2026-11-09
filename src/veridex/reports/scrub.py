"""
Strip the shopper out of a reported page before it is stored (the server's double check of the extension's cleaner).

What a report keeps is the page as any visitor would see it: the product, the shop and its claims. What it must not
keep is what the page showed about the person looking at it: a "Hello, <name>" greeting, a delivery address, an email
address, a phone number, account numbers, session tokens in URLs. HTML is reduced to the facts and the visible text
(navigation, headers, forms and scripts are dropped with it), URLs lose their query string and fragment, the checkout
page (which shows the buyer's address) is dropped entirely, and the text is scrubbed with the patterns below.
"""

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from veridex.facts import PageFacts
from veridex.facts.extract import parse_page
from veridex.price_store import product_id, registrable_domain
from veridex.schemas.requests import AnalyzeRequest

MAX_TEXT_CHARS = 200_000

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Phone numbers with an international prefix, or a Dutch-style area code followed by a space or dash; plain digit runs
# (EANs, article numbers) are left alone.
_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+|00)\d{1,3}[\s.-]?\(?\d\)?(?:[\s.-]?\d){6,11}|0\d{1,3}[\s-]\d(?:[\s-]?\d){5,8})(?!\d)"
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b\d{4}(?:[ -]\d{4}){3}\b")
_GREETING = re.compile(
    r"\b(Hello|Hi|Hey|Welcome(?: back)?|Hallo|Hoi|Welkom(?: terug)?|Goedemorgen|Goedemiddag|Goedenavond)(,?\s+)"
    r"[A-ZÀ-Þ][\w'.-]*(?:[ \t]+(?:(?:van|de|der|den|von|ten|ter|le|la)[ \t]+)*[A-ZÀ-Þ][\w'.-]*){0,2}"
)
_DELIVERY = re.compile(
    r"\b(Deliver(?:ing)? to|Ship to|Bezorgen (?:aan|in|bij|naar)|Leveren aan|Verzenden naar|Bezorgadres)"
    r"(:?\s+)[^\n|]{1,80}",
    re.IGNORECASE,
)
_POSSESSIVE = re.compile(r"\b[A-ZÀ-Þ][\w-]*'s (Amazon|account|Account|lists|Lists)\b")
_SECRET_META = re.compile(r"token|csrf|session|nonce|user|customer|email", re.IGNORECASE)
_AMAZON_REF = re.compile(r"/ref=[^/]*$")


def scrub_text(text: str) -> str:
    """
    Replace personal details in page text with placeholders.

    :param text: Visible page text.
    :return: The text with emails, phone numbers, IBANs, card numbers, greetings and delivery lines replaced.
    """
    text = _EMAIL.sub("[email]", text)
    text = _IBAN.sub("[iban]", text)
    text = _CARD.sub("[card]", text)
    text = _PHONE.sub("[phone]", text)
    text = _DELIVERY.sub(r"\1\2[address]", text)
    text = _GREETING.sub(r"\1\2[name]", text)
    return _POSSESSIVE.sub(r"[name]'s \1", text)


def clean_url(url: str) -> str:
    """
    A page URL without anything that could identify the visitor: no query string, no fragment, no Amazon ``ref``.

    :param url: The URL as reported.
    :return: ``scheme://host/path`` (empty for an unusable URL).
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    path = _AMAZON_REF.sub("", parts.path).rstrip("/")
    return urlunsplit((parts.scheme, parts.hostname.lower(), path, "", ""))


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    return value


def scrub_facts(facts: PageFacts) -> PageFacts:
    """
    Scrub the facts: personal details out of every text, query strings off every URL, token-like meta tags dropped.

    :param facts: The facts as reported.
    :return: A scrubbed copy.
    """
    data = facts.model_dump(mode="json")
    data["url"] = clean_url(data["url"])
    data["meta"] = {k: v for k, v in data["meta"].items() if not _SECRET_META.search(k)}
    data["script_srcs"] = [u for u in (clean_url(s) for s in data["script_srcs"]) if u]
    data["image_urls"] = [u for u in (clean_url(s) for s in data["image_urls"]) if u]
    data["policy_links"] = {k: u for k, v in data["policy_links"].items() if (u := clean_url(v))}
    urls = {k: data.pop(k) for k in ("url", "script_srcs", "image_urls", "policy_links")}
    return PageFacts.model_validate({**_scrub_value(data), **urls})


def page_of(body: AnalyzeRequest) -> tuple[PageFacts, str, str]:
    """
    What a report stores of the analysed request: scrubbed facts, scrubbed visible text and the clean URL.

    :param body: The analysed request, as the reporter sent it back.
    :return: ``(facts, text, url)``.
    """
    content = body.page_content or ""
    if body.page_facts is not None:
        facts, text = body.page_facts, content
        if "<" in content and re.search(r"<(html|body|div|span|p)\b", content, re.IGNORECASE):
            text = parse_page(content, body.url)[1]
    else:
        facts, text = parse_page(content, body.url)
    url = clean_url(body.url or facts.url)
    return scrub_facts(facts), scrub_text(text)[:MAX_TEXT_CHARS], url


def page_identity(body: AnalyzeRequest, facts: PageFacts, url: str, text: str) -> tuple[str, str]:
    """
    Which page a report is about, so that reports of the same page meet.

    The shop's registrable domain plus the public product ID (ASIN, bol.com ID, GTIN), or else a hash of the clean
    URL (or of the text, without any URL).

    :param body: The analysed request (its raw URL carries the product ID).
    :param facts: The page facts.
    :param url: The clean URL.
    :param text: The scrubbed text.
    :return: ``(page_key, domain)``.
    """
    host = urlsplit(url).hostname or ""
    domain = registrable_domain(host) if host else "unknown"
    if pid := product_id(body.url or facts.url, facts):
        return f"{domain}|{pid}", domain
    if url:
        same = url.replace("://www.", "://", 1)  # www.shop.test and shop.test are the same page
        return f"{domain}|url:{hashlib.sha256(same.encode()).hexdigest()[:32]}", domain
    return f"{domain}|text:{hashlib.sha256(text.encode()).hexdigest()[:32]}", domain
