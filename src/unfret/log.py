"""
Structured logs, one JSON object per line, that never carry what a visitor looked at or where they came from.

Unfret stores no record of what was analysed, and its logs keep that promise: a processor drops fields that name a
page or a visitor (``url``, ``ip``, …) and masks URLs and IP addresses inside any other string, whatever the call
site passed. Wallet addresses and transaction signatures stay: they are public on chain and needed to follow money.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_DROPPED = frozenset({"url", "urls", "page_url", "ip", "client_ip", "remote_addr", "host", "referer", "user_agent"})
_URL = re.compile(r"\bhttps?://[^\s\"'<>]+", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![0-9a-z:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![0-9a-z:])", re.IGNORECASE)


def _ipv6(match: re.Match[str]) -> str:
    text = match.group(0)
    # "::" or at least three colons: an address, not a time of day like 21:06:17.
    return "<ip>" if "::" in text or text.count(":") >= 3 else text


def scrub(text: str) -> str:
    """
    Mask URLs and IP addresses in a string.

    :param text: Any log text.
    :return: The text with ``<url>`` and ``<ip>`` in their place.
    """
    return _IPV6.sub(_ipv6, _IPV4.sub("<ip>", _URL.sub("<url>", text)))


def redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """
    A structlog processor: drop page and visitor fields, and scrub every other string value.

    :param event: The event dict.
    :return: The cleaned event dict.
    """
    for key in [k for k in event if k.lower() in _DROPPED]:
        del event[key]
    for key, value in event.items():
        if isinstance(value, str):
            event[key] = scrub(value)
        elif isinstance(value, list | tuple):
            event[key] = [scrub(v) if isinstance(v, str) else v for v in value]
    return event


def configure(*, json: bool = True, level: int = logging.INFO) -> None:
    """
    Set up structlog for the service.

    :param json: One JSON object per line (production) or readable console output (development).
    :param level: The lowest level logged.
    """
    # httpx logs every request with its full URL at INFO, outside structlog's redaction: the pages the server
    # fetches for a URL-only check would land in the log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            redact,  # before the timestamp, which it would leave alone anyway
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
