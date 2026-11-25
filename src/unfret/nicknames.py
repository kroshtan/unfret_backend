"""
Leaderboard nicknames: a word filter when one is chosen, and the operator's review before ``/live`` shows it.

A contributor picks a nickname (``handle``) with ``POST /v1/contributors`` (:func:`unfret.crowd.profile`). It is
refused there when it reads as offensive (:func:`offensive`: the letters after undoing common look-alike digits and
symbols, matched against a short list of slurs, sexual words and insults in English and Dutch). One that passes is
``pending`` until the operator approves or rejects it (``python -m unfret.nicknames_cli``, the admin endpoints under
``/v1/admin/nicknames``); a changed nickname is pending again. The public ``/live`` page shows approved nicknames
only; the apps' leaderboard (``GET /v1/contributors/leaderboard``) leaves rejected ones out.
"""

import re
from typing import Any, Literal

import asyncpg

Review = Literal["pending", "approved", "rejected"]
REVIEWS: tuple[Review, ...] = ("pending", "approved", "rejected")

# Digits and symbols people use for letters ("sh1t", "@ss").
_LOOKALIKE = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a"})
# Words a nickname may not contain anywhere (after the look-alikes, without - and _, case ignored). Short on purpose:
# every nickname is reviewed by a person anyway; this keeps the worst out of the queue and off any client.
_BLOCKED = (
    "fuck",
    "fuk",
    "shit",
    "cunt",
    "bitch",
    "whore",
    "slut",
    "dick",
    "cock",
    "pussy",
    "penis",
    "vagina",
    "porn",
    "rape",
    "nigg",
    "nigga",
    "fag",
    "retard",
    "spastic",
    "tranny",
    "kike",
    "chink",
    "wank",
    "twat",
    "bastard",
    "asshole",
    "nazi",
    "hitler",
    "kanker",
    "hoer",
    "neuk",
    "flikker",
    "mongool",
    "tering",
    "tyfus",
    "suicide",
)
_SEPARATORS = re.compile(r"[-_]")


def offensive(handle: str) -> bool:
    """
    Whether a nickname contains a blocked word (look-alike digits undone, ``-`` and ``_`` dropped, case ignored).

    :param handle: The nickname.
    :return: ``True`` to refuse it.
    """
    text = _SEPARATORS.sub("", handle.lower().translate(_LOOKALIKE))
    return any(word in text for word in _BLOCKED)


async def listed(pool: asyncpg.Pool, review: Review | None = "pending") -> list[dict[str, Any]]:
    """
    Nicknames for the operator's review: the nickname, its review and the verified reports behind it.

    :param pool: The database pool.
    :param review: Only these (``None``: all).
    :return: Nickname, review, public (opted in to the leaderboard), agreed (verified reports).
    """
    rows = await pool.fetch(
        "SELECT handle, handle_review AS review, public, agreed FROM contributors WHERE handle IS NOT NULL "
        "AND ($1::text IS NULL OR handle_review = $1) ORDER BY agreed DESC, handle LIMIT 500",
        review,
    )
    return [dict(r) for r in rows]


async def review(pool: asyncpg.Pool, handle: str, decision: Review) -> bool:
    """
    Approve or reject a nickname (or put it back to pending).

    :param pool: The database pool.
    :param handle: The nickname, exactly.
    :param decision: The review.
    :return: Whether there is a contributor with that nickname.
    """
    status: str = await pool.execute("UPDATE contributors SET handle_review = $2 WHERE handle = $1", handle, decision)
    return status.endswith(" 1")
