import hashlib
import itertools
import json

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from veridex.config import settings
from veridex.credits import keys
from veridex.main import app

PRICE = settings.analysis_price_usd_micros
PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater"}


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)  # a handful of hashes, not a second's worth


def solve(challenge: str, bits: int) -> str:
    for n in itertools.count():
        if keys.pow_ok(challenge, str(n), bits):
            return str(n)
    raise AssertionError  # pragma: no cover


async def new_key(client: httpx.AsyncClient, ip_headers: dict[str, str] | None = None) -> httpx.Response:
    challenge = (await client.get("/v1/keys/challenge")).json()
    body = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}
    return await client.post("/v1/keys", json=body, headers=ip_headers or {})


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def balance(pool: asyncpg.Pool, account: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", account)
    return None if value is None else int(value)


async def test_a_new_key_is_empty_and_works_like_a_session(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    response = await new_key(client)

    assert response.status_code == 201
    issued = response.json()
    assert issued["key"].startswith(f"ufk_{issued['key_id']}_")
    assert issued["recovery_code"].startswith(f"ufr_{issued['key_id']}_")
    assert issued["account"] == f"key:{issued['key_id']}"
    assert issued["balance_usd_micros"] == 0  # keys start empty
    credits = await client.get("/v1/credits", headers=bearer(issued["key"]))
    assert credits.json()["address"] == issued["account"]
    assert credits.json()["balance_usd_micros"] == 0
    assert (await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))).status_code == 402

    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued["account"], PRICE)
    paid = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))
    assert paid.status_code == 200
    assert paid.json()["charged"] is True
    assert await balance(pool, issued["account"]) == 0


async def test_only_hashes_of_the_key_and_code_are_stored(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = (await new_key(client)).json()

    row = await pool.fetchrow("SELECT * FROM api_keys WHERE id = $1", issued["key_id"])
    assert bytes(row["key_hash"]) == hashlib.sha256(issued["key"].encode()).digest()
    assert bytes(row["recovery_hash"]) == hashlib.sha256(issued["recovery_code"].encode()).digest()
    assert set(row.keys()) == {"id", "key_hash", "recovery_hash", "created_on", "rotated_on"}
    dump = json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM api_keys")], default=str)
    assert issued["key"].split("_")[2] not in dump
    assert issued["recovery_code"].split("_")[2] not in dump


async def test_recovery_rotates_the_key_and_keeps_the_balance(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = (await new_key(client)).json()
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, 450000)", issued["account"])

    response = await client.post("/v1/keys/recover", json={"recovery_code": issued["recovery_code"]})

    assert response.status_code == 200
    fresh = response.json()
    assert fresh["key_id"] == issued["key_id"]
    assert fresh["balance_usd_micros"] == 450_000
    assert fresh["key"] != issued["key"]
    assert fresh["recovery_code"] != issued["recovery_code"]
    old = await client.get("/v1/credits", headers=bearer(issued["key"]))
    assert old.status_code == 401
    assert old.json()["error_code"] == "INVALID_API_KEY"
    assert (await client.get("/v1/credits", headers=bearer(fresh["key"]))).json()["balance_usd_micros"] == 450_000
    again = await client.post("/v1/keys/recover", json={"recovery_code": issued["recovery_code"]})
    assert again.status_code == 401  # a recovery code works once


@pytest.mark.parametrize(
    "key", ["ufk_nonsense", "ufk_aaaaaaaaaa_" + "a" * 52, "ufr_aaaaaaaaaa_" + "a" * 52, "ufk_" + "a" * 10]
)
async def test_bad_keys_are_refused(client: httpx.AsyncClient, key: str) -> None:
    response = await client.get("/v1/credits", headers=bearer(key))
    assert response.status_code == 401


async def test_a_challenge_is_used_once_and_must_be_solved(client: httpx.AsyncClient) -> None:
    challenge = (await client.get("/v1/keys/challenge")).json()
    solution = solve(challenge["challenge"], challenge["bits"])
    unsolved = next(str(n) for n in itertools.count() if not keys.pow_ok(challenge["challenge"], str(n), 4))

    assert (
        await client.post("/v1/keys", json={"challenge": challenge["challenge"], "solution": unsolved})
    ).status_code == 400
    assert (
        await client.post("/v1/keys", json={"challenge": challenge["challenge"], "solution": solution})
    ).status_code == 201
    replay = await client.post("/v1/keys", json={"challenge": challenge["challenge"], "solution": solution})
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "BAD_CHALLENGE"
    forged = challenge["challenge"].rsplit(".", 1)[0] + "." + "0" * 32
    assert (await client.post("/v1/keys", json={"challenge": forged, "solution": solution})).status_code == 400


async def test_key_creation_is_limited_per_network(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_create_per_ip_per_day", 2)
    app.credits = None  # rebuilt with the new limit

    statuses = [(await new_key(client)).status_code for _ in range(3)]

    assert statuses == [201, 201, 429]
