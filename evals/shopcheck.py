"""A cheap, deterministic guess at whether a page is an online shop, used to filter dataset pages."""

import re

from unfret.html_cleaner import clean_html

_PRICE_RE = re.compile(r"(?:[$€£¥]\s?\d{1,5}(?:[.,]\d{2})?)|(?:\d{1,5}(?:[.,]\d{2})\s?(?:€|EUR|USD|GBP|kr|zł))")
_CART_RE = re.compile(
    r"add[\s_-]?to[\s_-]?(?:cart|bag|basket)|shopping[\s_-]?cart|checkout|in den warenkorb|in winkelwagen"
    r"|ajouter au panier|añadir al carrito|aggiungi al carrello|buy now|shop now",
    re.IGNORECASE,
)
_PLATFORM_RE = re.compile(
    r"cdn\.shopify\.com|woocommerce|wp-content/plugins/woocommerce|magento|prestashop|bigcommerce|shopware",
    re.IGNORECASE,
)
_MIN_TEXT_CHARS = 200


def shop_signals(html: str) -> dict[str, int]:
    """
    Count the shop indicators in a page.

    :param html: Raw page HTML.
    :return: Counts of ``prices``, ``cart`` phrases and ``platform`` markers, and ``text_chars`` of visible text.
    """
    text = clean_html(html)
    return {
        "prices": len(_PRICE_RE.findall(text)),
        "cart": len(_CART_RE.findall(html)),
        "platform": len(_PLATFORM_RE.findall(html)),
        "text_chars": len(text),
    }


def looks_like_shop(html: str) -> bool:
    """
    Guess whether ``html`` is a shop or product page.

    It needs visible text, at least two prices, and either a cart phrase or a known shop platform. Fake charities,
    crypto sites and parked domains in the scam datasets fail this check.

    :param html: Raw page HTML.
    :return: ``True`` if the page looks like a shop.
    """
    signals = shop_signals(html)
    return (
        signals["text_chars"] >= _MIN_TEXT_CHARS
        and signals["prices"] >= 2
        and (signals["cart"] > 0 or signals["platform"] > 0)
    )
