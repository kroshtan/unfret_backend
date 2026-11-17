"""
The Unfret test shop: a made-up web shop, served at ``/test-shop``, with every trick Unfret catches.

It is a safe page to demo, screenshot and test against: a US-style store selling an invented star projector, built
from the tricks seen on real listings with every name removed (``static/test-shop``, ``routes/test_shop.py``). Its
tricks are deliberate, so nothing Unfret learns or keeps about shops may take it in: the shop fingerprint store,
the price history, the market prices, the review fingerprints, the photo index, the verdict store and the review
queue for reported results all leave it out (:func:`is_test_shop`).
"""

from urllib.parse import urlparse

from unfret.config import settings
from unfret.net import hostname

PATH = "/test-shop"
_OWN_DOMAIN = "unfret.net"


def is_test_shop(url: str) -> bool:
    """
    Whether a URL is one of the test shop's pages, on this service or any Unfret host.

    By address only, never by the page's content, so a real shop can't opt out of the stores by copying its markup.

    :param url: The checked URL.
    :return: ``True`` for ``/test-shop`` and the pages under it on ``unfret.net``, its subdomains or this service.
    """
    host = hostname(url)
    own = hostname(settings.public_base_url)
    if not host or not (host in {own, _OWN_DOMAIN} or host.endswith(f".{_OWN_DOMAIN}")):
        return False
    path = urlparse(url if "://" in url else f"https://{url}").path.rstrip("/")
    return path == PATH or path.startswith(f"{PATH}/")
