"""
Labels from the product (``POST /v1/labels``), contributor keys, settling and the leaderboard.
"""

import base64
import os
from collections.abc import Callable
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from unfret import crowd
from unfret.config import settings

URL = "https://www.shop.example/products/lamp?utm_source=mail&color=red#reviews"
RISK = {
    "level": 2,
    "key": "doesnt_work",
    "label": "May not work as described",
    "because": [{"type": "claim_contradicted", "level": 2, "title": "A claim that doesn't add up"}],
    "combined": None,
    "modifier": None,
    "explanation": "Level 2 because: A claim that doesn't add up.",
    "source": "band",
    "confidence": None,
}
VERDICT = {
    "band": "careful",
    "label": "Think twice",
    "headline": "A claim that doesn't add up.",
    "reasons": [
        {"type": "claim_contradicted", "title": "A claim that doesn't add up", "detail": "x", "severity": "medium"},
        {"type": "countdown", "title": "Countdown timer", "detail": "y", "severity": "low"},
    ],
    "risk": RISK,
}


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture
def token(client: httpx.AsyncClient, graph: FakeGraph, wallet: Callable[..., Any]) -> Callable[..., Any]:
    graph.result = {"score": 30, "explanation": "A claim that doesn't add up.", "verdict": VERDICT}

    async def make(url: str = URL) -> str:
        _, headers = await wallet(10**9)
        body = {"url": url, "page_content": "<html>lamp</html>"}
        sent = await client.post("/v1/analyze", json=body, headers=headers)
        assert sent.status_code == 200, sent.text
        return str(sent.json()["label_token"])

    return make


async def test_a_result_can_be_labelled_and_nothing_names_the_page(
    client: httpx.AsyncClient, pool: asyncpg.Pool, token: Callable[..., Any]
) -> None:
    label_token = await token()
    sent = await client.post("/v1/labels", json={"label_token": label_token, "level": 1, "disagree": ["countdown"]})
    assert sent.status_code == 201, sent.text
    assert sent.json() == {"status": "recorded", "anonymous": True}
    row = dict(await pool.fetchrow("SELECT * FROM crowd_labels"))
    assert row["shown_level"] == 2 and row["level"] == 1 and row["disagree"] == ["countdown"]
    assert row["contributor_id"] is None and row["weight"] == pytest.approx(settings.crowd_anonymous_weight)
    stored = " ".join(str(v) for v in row.values())
    assert "shop.example" not in stored and "lamp" not in stored  # a keyed hash, not the address
    # The same page from another link (tracking, www, fragment) meets it.
    assert row["page_key"] == crowd.page_key(
        settings.session_secret.get_secret_value(), "https://shop.example/products/lamp?color=red"
    )
    again = await client.post("/v1/labels", json={"label_token": label_token, "level": 1})
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_LABELLED"


async def test_bad_labels_are_refused(client: httpx.AsyncClient, token: Callable[..., Any]) -> None:
    label_token = await token()
    forged = await client.post("/v1/labels", json={"label_token": label_token[:-2] + "00", "level": 1})
    assert forged.status_code == 400 and forged.json()["error_code"] == "BAD_LABEL_TOKEN"
    unshown = await client.post("/v1/labels", json={"label_token": label_token, "disagree": ["new_domain"]})
    assert unshown.status_code == 400 and unshown.json()["error_code"] == "BAD_LABEL"
    empty = await client.post("/v1/labels", json={"label_token": label_token})
    assert empty.status_code == 400
    bad_key = await client.post("/v1/labels", json={"label_token": label_token, "level": 0, "contributor_key": "x"})
    assert bad_key.status_code == 400 and bad_key.json()["error_code"] == "BAD_CONTRIBUTOR_KEY"


async def test_a_contributor_labels_once_per_page_and_within_a_daily_limit(
    client: httpx.AsyncClient, pool: asyncpg.Pool, token: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    key = new_key()
    first = await client.post("/v1/labels", json={"label_token": await token(), "level": 2, "contributor_key": key})
    assert first.json() == {"status": "recorded", "anonymous": False}
    second = await client.post("/v1/labels", json={"label_token": await token(), "level": 3, "contributor_key": key})
    assert second.json()["status"] == "updated"
    assert await pool.fetchval("SELECT COUNT(*) FROM crowd_labels") == 1
    assert await pool.fetchval("SELECT level FROM crowd_labels") == 3
    # A new key is on probation.
    assert await pool.fetchval("SELECT weight FROM crowd_labels") == pytest.approx(settings.crowd_new_key_weight)
    assert key.encode() not in b"".join(r["key_hash"] for r in await pool.fetch("SELECT key_hash FROM contributors"))
    monkeypatch.setattr(settings, "crowd_key_daily_labels", 2)
    over = await client.post("/v1/labels", json={"label_token": await token(), "level": 3, "contributor_key": key})
    assert over.status_code == 429 and over.json()["error_code"] == "LABEL_LIMIT"


async def _trusted(pool: asyncpg.Pool, key: str, reputation: float = 1.0) -> None:
    digest = crowd.key_hash(settings.session_secret.get_secret_value(), key)
    await pool.execute(
        "INSERT INTO contributors (key_hash, settled, agreed, reputation) VALUES ($1, $2, $2, $3)",
        digest,
        settings.crowd_probation_settled,
        reputation,
    )


async def test_agreeing_labels_settle_the_page_and_move_reputations(
    client: httpx.AsyncClient, pool: asyncpg.Pool, token: Callable[..., Any]
) -> None:
    keys = [new_key() for _ in range(2)]
    for key in keys:
        await _trusted(pool, key)
    for key, level in zip(keys, (1, 3), strict=True):
        sent = await client.post(
            "/v1/labels",
            json={"label_token": await token(), "level": level, "disagree": ["countdown"], "contributor_key": key},
        )
        assert sent.status_code == 201
    assert await crowd.settle(pool, settings) == 0  # one against one
    await client.post("/v1/labels", json={"label_token": await token(), "level": 1})  # anonymous, alone: 0.2
    assert await crowd.settle(pool, settings) == 0  # 1.2 of 2.2: under the weight and two thirds
    await _trusted(pool, k := new_key())
    await client.post(
        "/v1/labels",
        json={"label_token": await token(), "level": 1, "disagree": ["countdown"], "contributor_key": k},
    )
    assert await crowd.settle(pool, settings) == 1  # 2.2 of 3.2
    answer = dict(await pool.fetchrow("SELECT * FROM crowd_answers"))
    assert (answer["level"], answer["shown_level"], answer["held"], answer["disagree"]) == (1, 2, False, ["countdown"])
    reputations = [r["reputation"] for r in await pool.fetch("SELECT reputation FROM contributors ORDER BY id")]
    assert reputations[0] > 1 and reputations[1] < 1
    # Settled: no label links to a contributor any more.
    assert await pool.fetchval("SELECT COUNT(*) FROM crowd_labels WHERE contributor_id IS NOT NULL") == 0


async def test_anonymous_labels_alone_never_settle_a_page(
    client: httpx.AsyncClient, pool: asyncpg.Pool, token: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crowd_anonymous_weight", 1.0)
    for _ in range(5):
        await client.post("/v1/labels", json={"label_token": await token(), "level": 0})
    assert await crowd.settle(pool, settings) == 0


async def test_a_burst_that_lowers_the_level_is_held(
    client: httpx.AsyncClient, pool: asyncpg.Pool, token: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crowd_burst_labels", 2)
    for _ in range(3):
        await _trusted(pool, k := new_key())
        await client.post("/v1/labels", json={"label_token": await token(), "level": 0, "contributor_key": k})
    assert await crowd.settle(pool, settings) == 1
    assert await pool.fetchval("SELECT held FROM crowd_answers") is True
    assert await pool.fetchval("SELECT MAX(settled) FROM contributors") == settings.crowd_probation_settled


async def test_profiles_and_the_leaderboard(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    key = new_key()
    await _trusted(pool, key)
    private = await client.post("/v1/contributors", json={"contributor_key": key})
    assert private.json()["public"] is False and private.json()["rank"] is None
    no_handle = await client.post("/v1/contributors", json={"contributor_key": key, "public": True})
    assert no_handle.status_code == 400 and no_handle.json()["error_code"] == "BAD_HANDLE"
    bad = await client.post("/v1/contributors", json={"contributor_key": key, "handle": "scamhunter"})
    assert bad.status_code == 400
    shown = await client.post("/v1/contributors", json={"contributor_key": key, "handle": "lamp_fan", "public": True})
    assert shown.json()["rank"] == 1 and shown.json()["trusted"] is True
    taken = await client.post("/v1/contributors", json={"contributor_key": new_key(), "handle": "lamp_fan"})
    assert taken.status_code == 409
    board = await client.get("/v1/contributors/leaderboard")
    assert board.json() == {"entries": [{"rank": 1, "handle": "lamp_fan", "agreed": 5, "settled": 5}]}
    gone = await client.post("/v1/contributors/forget", json={"contributor_key": key})
    assert gone.json() == {"deleted": True}
    assert (await client.get("/v1/contributors/leaderboard")).json() == {"entries": []}
