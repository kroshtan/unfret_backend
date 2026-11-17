"""
A minimal client for Unfret: check a store or product page before an agent buys, and get allow, warn or block.

    from unfret_client import Unfret

    decision = Unfret(pass_="ufk_…").check("https://shop.example/p/42")
    if decision.action == "block":
        ...

The action comes from the verdict a shopper would see (:data:`DEFAULT_POLICY`): "Looks fine" → allow, "Worth a
look" → warn (ask your human), "Think twice" and "Serious concerns" → block; pass ``policy`` to change that.
"""

from unfret_client._client import (
    BAND_NAMES,
    DEFAULT_POLICY,
    Action,
    AsyncUnfret,
    Band,
    Decision,
    InvalidPassError,
    NoChecksLeftError,
    PageUnreadableError,
    RateLimitedError,
    Tier,
    Unfret,
    UnfretError,
    decide,
    solve,
)

__all__ = [
    "BAND_NAMES",
    "DEFAULT_POLICY",
    "Action",
    "AsyncUnfret",
    "Band",
    "Decision",
    "InvalidPassError",
    "NoChecksLeftError",
    "PageUnreadableError",
    "RateLimitedError",
    "Tier",
    "Unfret",
    "UnfretError",
    "decide",
    "solve",
]
