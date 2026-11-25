"""
"Wrong result?" reports through ``POST /v1/labels``: the page kept, never who sent it; contributor keys, settling,
the leaderboard; and the migrations that brought labels and older reports into one store.
"""

import base64
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import DROP_ALL, FakeGraph
from unfret import crowd
from unfret.config import settings
from unfret.migrations import MIGRATIONS, migrate

URL = "https://www.shop.example/products/lamp?utm_source=mail&color=red#reviews"
RISK = {
    "level": 3,
    "key": "doesnt_work",
    "label": "May not work as described",
    "because": [{"type": "claim_contradicted", "level": 3, "title": "A claim that doesn't add up"}],
    "combined": None,
    "modifier": None,
    "explanation": "Level 3 because: A claim that doesn't add up.",
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
ADMIN = {"Authorization": "Bearer operator-token"}
Send = Callable[..., Awaitable[httpx.Response]]


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))


@pytest.fixture
def checked(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Callable[..., Any]
) -> Callable[..., Awaitable[tuple[dict[str, Any], str]]]:
    graph.result = {"score": 30, "explanation": "A claim that doesn't add up.", "verdict": VERDICT}

    async def make(url: str = URL) -> tuple[dict[str, Any], str]:
        body = {"url": url, "page_content": "Hello, Floris van Beers. Lamp, 10,000 lumens. Call +31 6 1234 5678."}
        _, headers = await wallet(10**9)  # who checked is never sent with the report
        sent = await client.post("/v1/analyze", json=body, headers=headers)
        assert sent.status_code == 200, sent.text
        return body, str(sent.json()["label_token"])

    return make


@pytest.fixture
def send(client: httpx.AsyncClient, checked: Callable[..., Awaitable[tuple[dict[str, Any], str]]]) -> Send:
    async def report(url: str = URL, **label: Any) -> httpx.Response:
        body, token = await checked(url)
        return await client.post("/v1/labels", json={"label_token": token, "request": body, **label})

    return report


async def test_a_report_keeps_the_page_scrubbed_and_never_who_sent_it(
    client: httpx.AsyncClient, pool: asyncpg.Pool, send: Send
) -> None:
    sent = await send(
        level=4,
        wrong=["countdown"],
        missing=["new_domain"],
        comment="I paid by bank transfer and nothing came. Mail me at jan.jansen@example.nl or +31 6 1234 5678.",
    )
    assert sent.status_code == 201, sent.text
    assert sent.json() == {"status": "queued", "updated": False, "anonymous": True}
    item = dict(await pool.fetchrow("SELECT * FROM reports"))
    assert (item["level"], item["shown_level"], item["score"], item["reason"]) == (4, 3, 30, "scam")
    payload = json.loads(item["payload"])
    assert payload["url"] == "https://www.shop.example/products/lamp"
    assert "Floris" not in item["payload"] and "1234 5678" not in item["payload"]
    assert (payload["wrong"], payload["missing"]) == (["countdown"], ["new_domain"])
    label = dict(await pool.fetchrow("SELECT * FROM report_labels"))
    assert (label["level"], label["wrong"], label["missing"], label["anonymous"]) == (
        4,
        ["countdown"],
        ["new_domain"],
        True,
    )
    assert "jan.jansen" not in json.dumps(label, default=str)
    queue = (await client.get("/v1/admin/reports", headers=ADMIN)).json()["items"][0]
    assert queue["reports"] == {
        "count": 1,
        "anonymous": 1,
        "levels": {"4": 1},
        "wrong": {"countdown": 1},
        "missing": {"new_domain": 1},
        "comments": ["I paid by bank transfer and nothing came. Mail me at [email] or [phone]."],
    }


async def test_bad_reports_are_refused(
    client: httpx.AsyncClient, checked: Callable[..., Awaitable[tuple[dict[str, Any], str]]]
) -> None:
    body, token = await checked()

    async def post(**extra: Any) -> httpx.Response:
        return await client.post("/v1/labels", json={"label_token": token, "request": body, **extra})

    forged = await post(label_token=token[:-2] + "00", level=1)
    assert forged.status_code == 400 and forged.json()["error_code"] == "BAD_LABEL_TOKEN"
    other_page = await post(level=1, request={**body, "page_content": "Another page."})
    assert other_page.status_code == 400 and other_page.json()["error_code"] == "BAD_LABEL_TOKEN"
    unshown = await post(wrong=["new_domain"])
    assert unshown.status_code == 400 and unshown.json()["error_code"] == "BAD_LABEL"
    shown_not_missing = await post(missing=["countdown"])
    assert shown_not_missing.status_code == 400
    assert (await post()).status_code == 400  # says nothing
    bad_key = await post(level=0, contributor_key="x")
    assert bad_key.status_code == 400 and bad_key.json()["error_code"] == "BAD_CONTRIBUTOR_KEY"
    assert (await post(disagree=["countdown"])).status_code == 201  # the older name of "wrong"
    again = await post(level=1)
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_LABELLED"  # one per result


async def test_a_contributor_reports_once_per_page_within_a_daily_limit(
    pool: asyncpg.Pool, send: Send, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = new_key()
    first = await send(level=2, contributor_key=key)
    assert first.json() == {"status": "queued", "updated": False, "anonymous": False}
    second = await send(level=3, contributor_key=key)
    assert second.json() == {"status": "already_reported", "updated": True, "anonymous": False}
    assert await pool.fetchval("SELECT COUNT(*) FROM report_labels") == 1
    assert await pool.fetchval("SELECT level FROM report_labels") == 3
    assert await pool.fetchval("SELECT corroborations FROM reports") == 0  # a change of mind confirms nothing
    assert await pool.fetchval("SELECT weight FROM report_labels") == pytest.approx(settings.crowd_new_key_weight)
    monkeypatch.setattr(settings, "crowd_key_daily_labels", 2)
    over = await send(level=3, contributor_key=key)
    assert over.status_code == 429 and over.json()["error_code"] == "LABEL_LIMIT"


async def _trusted(pool: asyncpg.Pool, key: str) -> None:
    digest = crowd.key_hash(settings.session_secret.get_secret_value(), key)
    await pool.execute(
        "INSERT INTO contributors (key_hash, settled, agreed) VALUES ($1, $2, $2)",
        digest,
        settings.crowd_probation_settled,
    )


async def test_agreeing_reports_settle_the_page_and_move_reputations(pool: asyncpg.Pool, send: Send) -> None:
    keys = [new_key() for _ in range(2)]
    for key in keys:
        await _trusted(pool, key)
    for key, level in zip(keys, (4, 2), strict=True):
        await send(level=level, wrong=["countdown"], contributor_key=key)
    assert await crowd.settle(pool, settings) == 0  # one against one
    await send(level=4)  # anonymous: 0.2
    assert await crowd.settle(pool, settings) == 0  # 1.2 of 2.2
    await _trusted(pool, k := new_key())
    await send(level=4, wrong=["countdown"], contributor_key=k)
    assert await crowd.settle(pool, settings) == 1  # 2.2 of 3.2
    answer = dict(await pool.fetchrow("SELECT * FROM report_answers"))
    assert (answer["level"], answer["shown_level"], answer["held"], answer["by_person"]) == (4, 3, False, False)
    assert answer["wrong"] == ["countdown"]
    reputations = [r["reputation"] for r in await pool.fetch("SELECT reputation FROM contributors ORDER BY id")]
    assert reputations[0] > 1 and reputations[1] < 1
    assert await pool.fetchval("SELECT COUNT(*) FROM report_labels WHERE contributor_id IS NOT NULL") == 0


async def test_a_reviewers_level_settles_the_reports_and_gives_points(
    client: httpx.AsyncClient, pool: asyncpg.Pool, send: Send
) -> None:
    right, wrong = new_key(), new_key()
    await send(level=3, contributor_key=right)
    await send(level=0, contributor_key=wrong)
    item = await pool.fetchval("SELECT id FROM reports")

    done = await client.post(f"/v1/admin/reports/{item}/label", json={"level": 3}, headers=ADMIN)

    assert done.status_code == 200 and done.json()["level"] == 3 and done.json()["label"] == "junk"
    assert await pool.fetchval("SELECT level FROM labelled_pages") == 3
    assert await pool.fetchval("SELECT by_person FROM report_answers") is True
    mine = await client.post("/v1/contributors", json={"contributor_key": right})
    assert (mine.json()["points"], mine.json()["settled"]) == (1, 1)
    theirs = await client.post("/v1/contributors", json={"contributor_key": wrong})
    assert (theirs.json()["points"], theirs.json()["settled"]) == (0, 1)


async def test_anonymous_reports_alone_never_settle_a_page(
    pool: asyncpg.Pool, send: Send, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crowd_anonymous_weight", 1.0)
    for _ in range(5):
        await send(level=0)
    assert await crowd.settle(pool, settings) == 0


async def test_a_burst_that_lowers_the_level_is_held(
    pool: asyncpg.Pool, send: Send, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crowd_burst_labels", 2)
    for _ in range(3):
        await _trusted(pool, k := new_key())
        await send(level=0, contributor_key=k)
    assert await crowd.settle(pool, settings) == 1
    assert await pool.fetchval("SELECT held FROM report_answers") is True
    assert await pool.fetchval("SELECT MAX(settled) FROM contributors") == settings.crowd_probation_settled


async def test_profiles_and_the_leaderboard(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    key = new_key()
    await _trusted(pool, key)
    private = await client.post("/v1/contributors", json={"contributor_key": key})
    assert private.json()["public"] is False and private.json()["rank"] is None
    no_handle = await client.post("/v1/contributors", json={"contributor_key": key, "public": True})
    assert no_handle.status_code == 400 and no_handle.json()["error_code"] == "BAD_HANDLE"
    assert (await client.post("/v1/contributors", json={"contributor_key": key, "handle": "scamhunter"})).is_error
    shown = await client.post("/v1/contributors", json={"contributor_key": key, "handle": "lamp_fan", "public": True})
    assert shown.json()["rank"] == 1 and shown.json()["trusted"] is True and shown.json()["points"] == 5
    taken = await client.post("/v1/contributors", json={"contributor_key": new_key(), "handle": "lamp_fan"})
    assert taken.status_code == 409
    board = await client.get("/v1/contributors/leaderboard")
    assert board.json() == {"entries": [{"rank": 1, "handle": "lamp_fan", "agreed": 5, "settled": 5}]}
    assert (await client.post("/v1/contributors/forget", json={"contributor_key": key})).json() == {"deleted": True}
    assert (await client.get("/v1/contributors/leaderboard")).json() == {"entries": []}


async def _fresh_until(pool: asyncpg.Pool, version: int) -> None:
    await pool.execute(DROP_ALL)
    await pool.execute("DROP SCHEMA IF EXISTS stats CASCADE")
    await migrate(pool, [m for m in MIGRATIONS if m[0] <= version])


async def test_labels_stored_before_the_renumbering_flip(pool: asyncpg.Pool) -> None:
    await _fresh_until(pool, 40)
    await pool.execute(
        "INSERT INTO crowd_labels (page_key, anonymous, tier, shown_level, level, weight, created_day) VALUES "
        "('p', TRUE, 'deep', 1, 4, 0.2, CURRENT_DATE), ('q', TRUE, 'deep', 0, NULL, 0.2, CURRENT_DATE)"
    )
    await pool.execute(
        "INSERT INTO crowd_answers (page_key, level, shown_level, tier, labels, weight, settled_on) "
        "VALUES ('p', 2, 3, 'deep', 2, 2.0, CURRENT_DATE)"
    )
    await migrate(pool)
    rows = [dict(r) for r in await pool.fetch("SELECT shown_level, level FROM report_labels ORDER BY page_key")]
    assert rows == [{"shown_level": 4, "level": 1}, {"shown_level": 0, "level": None}]
    assert dict(await pool.fetchrow("SELECT level, shown_level FROM report_answers")) == {"level": 3, "shown_level": 2}


async def test_open_reports_move_into_the_one_store_without_their_accounts(pool: asyncpg.Pool) -> None:
    await _fresh_until(pool, 41)
    item = await pool.fetchval(
        "INSERT INTO reports (page_key, domain, account, reason, score, payload, created_at) VALUES "
        "('shop.example|url:1', 'shop.example', 'key:abcdefghij', 'scam', 80, '{\"tier\": \"quick\"}', "
        "'2026-11-20 14:33:00+00') RETURNING id"
    )
    for tag in (b"a" * 16, b"b" * 16):
        await pool.execute("INSERT INTO report_votes (report_id, account_tag) VALUES ($1, $2)", item, tag)
    await pool.execute(
        "INSERT INTO labelled_pages (page_key, domain, label, score, reason, payload) "
        "VALUES ('x', 'shop.example', 'legit', 20, 'real_brand', '{}')"
    )
    await migrate(pool)
    row = dict(await pool.fetchrow("SELECT * FROM reports"))
    assert "account" not in row and row["level"] == 4
    assert row["created_at"].isoformat().startswith("2026-11-20T00:00:00")
    labels = [dict(r) for r in await pool.fetch("SELECT page_key, level, tier, anonymous FROM report_labels")]
    assert labels == [{"page_key": "shop.example|url:1", "level": 4, "tier": "quick", "anonymous": True}] * 2
    assert await pool.fetchval("SELECT level FROM labelled_pages") == 0
    gone = await pool.fetch("SELECT tablename FROM pg_tables WHERE tablename IN ('report_votes', 'report_quota')")
    assert gone == []
