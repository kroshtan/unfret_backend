"""
The end-to-end sweep: every path a customer or an agent takes, against dev (devnet, real infrastructure).

Run before a release goes to production (the deploy-production workflow's gate) or by hand with ``make e2e``. It
refuses to run unless the target says it is on Solana devnet. Money moves only on devnet, from the e2e payer; the
e2e API key and the payer's wallet account end where they started. Fixture pages are served from the forge at the
commit dev runs, each URL carrying this run's marker, which must not show up in dev's logs or database afterwards.

Each test stands on its own (the shared state is the e2e key's balance, which each test measures around itself), so a
failure reads as the path that broke. Settings: ``UNFRET_E2E_*`` (README, "End-to-end sweep").
"""

import asyncio
import base64
import secrets
import subprocess
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from unfret_mcp.payer import Payer, SpendGuard, build_payment, choose_requirement  # on sys.path: tests/e2e/__init__
from unfret_mcp.solana import CAIP2, USDC_MINTS, WSOL_MINT, Rpc, associated_token_address

from tests.e2e.conftest import Account, Target, bearer, key_balance
from tests.e2e.helpers import (
    DEV_APP_FILTER,
    DEV_DB_CONTAINER,
    DEVNET_CAIP2,
    E2EConfig,
    b64json,
    fixture_url,
    nonce_hits,
    parse_solana_pay,
    parse_sse,
    polar_order,
    reference_only_transfer,
    sign_purchase,
    signed_webhook,
    ssh_command,
    x402_header,
)
from tests.integration.test_credits import sign_in
from tests.integration.test_keys import solve

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]

PRODUCT_PAGE = "shopify-productgroup-us.html"  # a product page with a price: assessed by both tiers
BOT_CHECK_PAGE = "bot-check-interstitial.html"
THIN_PAGE = "script-shell.html"
MISSING_PAGE = "no-such-page.html"  # not in the repository: the forge answers 404
DEVNET_USDC = USDC_MINTS["solana-devnet"]
PACK = 25
QUOTE_PAID_WITHIN_S = 90
SOL_CREDIT_TOLERANCE = 0.03  # SOL is valued when credited, so a SOL purchase may differ a little from its quote


def page(target: Target, name: str, label: str) -> str:
    return fixture_url(target.fixture_ref, name, target.nonce, label)


async def eventually[T](read: Callable[[], Awaitable[T]], done: Callable[[T], bool], timeout_s: float = 30) -> T:
    """Read until ``done`` (an RPC node may lag the one that confirmed), then return the last value."""
    deadline = time.monotonic() + timeout_s
    while True:
        value = await read()
        if done(value) or time.monotonic() > deadline:
            return value
        await asyncio.sleep(2)


async def usdc_of(rpc: Rpc, owner: Pubkey) -> int:
    return int(await rpc.token_balance(associated_token_address(owner, DEVNET_USDC)))


async def wsol_of(rpc: Rpc, owner: Pubkey) -> int:
    return int(await rpc.token_balance(associated_token_address(owner, WSOL_MINT)))


async def confirmed_on_chain(rpc: Rpc, signature: str) -> bool:
    async def status() -> dict[str, Any] | None:
        found: dict[str, Any] | None = (
            await rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        )["value"][0]
        return found

    found = await eventually(status, lambda s: s is not None and s.get("confirmationStatus") is not None)
    return found is not None and found.get("err") is None and found["confirmationStatus"] in {"confirmed", "finalized"}


@pytest.fixture(scope="session")
def rpc(config: E2EConfig) -> Rpc:
    return Rpc(config.rpc_url)


async def pricing(api: httpx.AsyncClient) -> dict[str, Any]:
    response = await api.get("/v1/pricing")
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def the_pack(document: dict[str, Any]) -> dict[str, Any]:
    chosen: dict[str, Any] = next(p for p in document["packs"] if p["checks"] == PACK)
    return chosen


async def offered(api: httpx.AsyncClient, url: str, tier: str) -> list[dict[str, Any]]:
    response = await api.post("/v1/check", json={"url": url, "tier": tier, "policy": "balanced"})
    assert response.status_code == 402, f"unpaid /v1/check: {response.status_code} {response.text[:200]}"
    assert response.json()["error_code"] == "PAYMENT_REQUIRED"
    accepts: list[dict[str, Any]] = b64json(response.headers["PAYMENT-REQUIRED"])["accepts"]
    return accepts


async def paid_check(
    api: httpx.AsyncClient, url: str, tier: str, requirement: dict[str, Any], payer: Keypair, rpc: Rpc, **change: Any
) -> tuple[httpx.Response, str]:
    """Pay ``requirement`` (built with ``change`` applied, e.g. a different amount) and send the check."""
    tx = build_payment({**requirement, **change}, payer, await rpc.latest_blockhash())
    header = x402_header(requirement, tx)
    response = await api.post(
        "/v1/check", json={"url": url, "tier": tier, "policy": "balanced"}, headers={"PAYMENT-SIGNATURE": header}
    )
    return response, header


async def quote_paid(api: httpx.AsyncClient, quote_id: str) -> dict[str, Any]:
    async def status() -> dict[str, Any]:
        response = await api.get(f"/v1/credits/quotes/{quote_id}")
        assert response.status_code == 200, f"quote {quote_id}: {response.status_code}"
        body: dict[str, Any] = response.json()
        return body

    return await eventually(status, lambda s: bool(s["paid"]), timeout_s=QUOTE_PAID_WITHIN_S)


# ── The deployment ─────────────────────────────────────────────────────────────────────────────────────────────


async def test_health_and_readiness_report_the_release(api: httpx.AsyncClient, config: E2EConfig) -> None:
    live = await api.get("/v1/health")
    assert live.status_code == 200
    assert live.json()["status"] == "healthy"
    assert live.json()["commit"], "the image doesn't report its commit (GIT_SHA build argument)"
    if config.expected_commit:
        assert live.json()["commit"] == config.expected_commit

    ready = await api.get("/v1/health/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["commit"] == live.json()["commit"]
    assert body["x402"] == "ok", f"x402 is {body['x402']} (alerts: {body['x402_alert_count']})"
    assert body["credits"] == "ok"
    assert body["fiat"] == config.expect_fiat
    assert body["hot_sol_lamports"] is None, "the hot wallet's balances are shown without the operator token"


async def test_discovery_says_devnet_and_the_tier_prices(api: httpx.AsyncClient, config: E2EConfig) -> None:
    discovery = (await api.get("/.well-known/x402")).json()
    prices = (await pricing(api))["x402"]

    assert discovery["network"] == DEVNET_CAIP2 == CAIP2["solana-devnet"]
    assert discovery["network_name"] == "solana-devnet"
    assert discovery["endpoint"] == f"{config.base_url}/v1/check"
    assert discovery["assets"]["USDC"]["mint"] == str(DEVNET_USDC)
    assert discovery["tiers"]["quick"]["price_usd_micros"] == prices["quick_usd_micros"]
    assert discovery["tiers"]["deep"]["price_usd_micros"] == prices["deep_usd_micros"]


async def test_llms_txt_describes_the_paid_endpoint(api: httpx.AsyncClient, config: E2EConfig) -> None:
    response = await api.get("/llms.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("# Unfret")
    assert f"{config.base_url}/v1/check" in response.text
    assert DEVNET_CAIP2 in response.text


# ── API keys ───────────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_new_key_starts_empty_takes_a_grant_and_survives_recovery(
    api: httpx.AsyncClient, config: E2EConfig, target: Target
) -> None:
    admin = bearer(config.need("admin_token"))
    challenge = (await api.get("/v1/keys/challenge")).json()
    created = await api.post(
        "/v1/keys",
        json={"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])},
    )
    assert created.status_code != 429, "this address created too many keys today (UNFRET_KEY_CREATE_PER_IP_PER_DAY)"
    assert created.status_code == 201, created.text
    issued = created.json()
    assert issued["balance_usd_micros"] == 0
    assert issued["account"] == f"key:{issued['key_id']}"
    current = issued["key"]
    try:
        empty = await api.post(
            "/v1/analyze", json={"url": page(target, PRODUCT_PAGE, "empty-key")}, headers=bearer(issued["key"])
        )
        assert empty.status_code == 402
        assert empty.json()["error_code"] == "INSUFFICIENT_CREDITS"

        granted = await api.post(
            f"/v1/admin/keys/{issued['key_id']}/grant", json={"usd_micros": 400_000}, headers=admin
        )
        assert granted.status_code == 200
        assert granted.json()["balance_usd_micros"] == 400_000
        assert await key_balance(api, issued["key"]) == 400_000
        nobody = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(10))
        unknown = await api.post(f"/v1/admin/keys/{nobody}/grant", json={"usd_micros": 1}, headers=admin)
        assert unknown.status_code == 404

        recovered = await api.post("/v1/keys/recover", json={"recovery_code": issued["recovery_code"]})
        assert recovered.status_code == 200, recovered.text
        new = recovered.json()
        current = new["key"]
        assert new["key_id"] == issued["key_id"]
        assert new["key"] != issued["key"]
        assert new["balance_usd_micros"] == 400_000
        old = await api.get("/v1/credits", headers=bearer(issued["key"]))
        assert old.status_code == 401
        assert old.json()["error_code"] == "INVALID_API_KEY"
        assert await key_balance(api, new["key"]) == 400_000
        reused = await api.post("/v1/keys/recover", json={"recovery_code": issued["recovery_code"]})
        assert reused.status_code == 401
    finally:  # leave the throwaway key empty
        left = await key_balance(api, current)
        if left > 0:
            emptied = await api.post(
                f"/v1/admin/keys/{issued['key_id']}/debit", json={"usd_micros": left}, headers=admin
            )
            assert emptied.json()["balance_usd_micros"] == 0, emptied.text


async def test_analyze_needs_an_account(api: httpx.AsyncClient, target: Target) -> None:
    body = {"url": page(target, PRODUCT_PAGE, "anonymous")}

    anonymous = await api.post("/v1/analyze", json=body)
    forged = await api.post("/v1/analyze", json=body, headers=bearer("ufk_" + "a" * 10 + "_" + "a" * 52))

    assert anonymous.status_code == 401
    assert anonymous.json()["error_code"] == "UNAUTHORIZED"
    assert forged.status_code == 401
    assert forged.json()["error_code"] == "INVALID_API_KEY"


# ── Analyses paid from credits ─────────────────────────────────────────────────────────────────────────────────


async def test_an_analysis_is_charged_once_and_then_served_free(
    api: httpx.AsyncClient, account: Account, target: Target
) -> None:
    price = (await pricing(api))["analysis_usd_micros"]
    body = {"url": page(target, PRODUCT_PAGE, "analyze")}
    before = await key_balance(api, account.key)

    first = await api.post("/v1/analyze", json=body, headers=bearer(account.key))
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["charged"] is True
    assert 0 <= result["score"] <= 100
    assert result["verdict"]["band"]
    assert result["report_token"]
    assert result["balance_usd_micros"] == before - price == await key_balance(api, account.key)

    again = await api.post("/v1/analyze", json=body, headers=bearer(account.key))
    assert again.status_code == 200
    assert again.json()["charged"] is False
    assert again.json()["score"] == result["score"]
    assert await key_balance(api, account.key) == before - price


async def test_a_streamed_analysis_reports_steps_then_the_charged_result(
    api: httpx.AsyncClient, account: Account, target: Target
) -> None:
    price = (await pricing(api))["analysis_usd_micros"]
    before = await key_balance(api, account.key)

    response = await api.post(
        "/v1/analyze/stream", json={"url": page(target, PRODUCT_PAGE, "stream")}, headers=bearer(account.key)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names.count("result") == 1 and names[-1] == "result", names
    assert "step" in names
    assert all(set(data) >= {"skill", "findings"} for name, data in events if name == "step")
    result = events[-1][1]
    assert result["charged"] is True
    assert result["report_token"]
    assert await key_balance(api, account.key) == before - price


@pytest.mark.parametrize(
    ("fixture", "url", "code"),
    [
        (BOT_CHECK_PAGE, None, "PAGE_BLOCKED"),
        (MISSING_PAGE, None, "PAGE_UNAVAILABLE"),
        (None, "http://127.0.0.1/products/e2e", "INVALID_URL"),
        (THIN_PAGE, None, "PAGE_TOO_THIN"),
    ],
)
async def test_a_page_that_cant_be_read_is_free(
    api: httpx.AsyncClient, account: Account, target: Target, fixture: str | None, url: str | None, code: str
) -> None:
    before = await key_balance(api, account.key)

    response = await api.post(
        "/v1/analyze", json={"url": url or page(target, str(fixture), code.lower())}, headers=bearer(account.key)
    )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == code
    assert await key_balance(api, account.key) == before


async def test_an_invalid_request_is_a_free_validation_error(api: httpx.AsyncClient, account: Account) -> None:
    before = await key_balance(api, account.key)

    response = await api.post("/v1/analyze", json={"flags": "not-a-list"}, headers=bearer(account.key))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert await key_balance(api, account.key) == before


# ── A wallet's session (Sign-In With Solana) ───────────────────────────────────────────────────────────────────


async def test_the_payer_signs_in_with_its_wallet(api: httpx.AsyncClient, payer: Keypair, target: Target) -> None:
    signed = await sign_in(api, payer)
    assert signed.status_code == 200, signed.text
    session = signed.json()
    assert session["address"] == str(payer.pubkey())

    credits = await api.get("/v1/credits", headers=bearer(session["token"]))
    assert credits.status_code == 200
    assert credits.json()["address"] == str(payer.pubkey())
    balance = credits.json()["balance_usd_micros"]
    if balance < (await pricing(api))["analysis_usd_micros"]:  # the payer's wallet account normally holds nothing
        short = await api.post(
            "/v1/analyze", json={"url": page(target, PRODUCT_PAGE, "wallet")}, headers=bearer(session["token"])
        )
        assert short.status_code == 402
        assert short.json()["error_code"] == "INSUFFICIENT_CREDITS"
        assert short.json()["metadata"]["balance_usd_micros"] == balance
    forged = await api.get("/v1/credits", headers=bearer(session["token"][:-4] + "AAAA"))
    assert forged.status_code == 401
    assert (await api.get("/v1/credits", headers=bearer(session["token"]))).json()["balance_usd_micros"] == balance


# ── x402 pay-per-check ─────────────────────────────────────────────────────────────────────────────────────────


async def test_an_unpaid_check_asks_for_the_tier_price_in_usdc_and_wsol(
    api: httpx.AsyncClient, target: Target
) -> None:
    prices = (await pricing(api))["x402"]
    for tier in ("quick", "deep"):
        accepts = await offered(api, page(target, PRODUCT_PAGE, f"unpaid-{tier}"), tier)
        assets = {option["asset"]: option for option in accepts}
        assert set(assets) == {str(DEVNET_USDC), str(WSOL_MINT)}, "both USDC and wSOL should be offered"
        assert int(assets[str(DEVNET_USDC)]["amount"]) == prices[f"{tier}_usd_micros"]
        assert all(option["network"] == DEVNET_CAIP2 and option["extra"]["tier"] == tier for option in accepts)


async def test_a_quick_check_paid_in_usdc_by_the_mcp_client(
    api: httpx.AsyncClient, config: E2EConfig, payer: Keypair, rpc: Rpc, target: Target
) -> None:
    price = (await pricing(api))["x402"]["quick_usd_micros"]
    client = Payer(
        api_url=config.base_url,
        agent=payer,
        rpc=rpc,
        network="solana-devnet",
        guard=SpendGuard(max_per_call=price, session_cap=price),
        prefer="USDC",
    )
    before = await usdc_of(rpc, payer.pubkey())

    outcome = await client.check({"url": page(target, PRODUCT_PAGE, "quick-usdc"), "tier": "quick"})

    assert outcome["status"] == "ok" and outcome["ok"], outcome
    assert outcome["paid"] == {"currency": "USDC", "amount": price, "usd_micros": price}
    assert outcome["verdict"]["tier"] == "quick"
    assert outcome["verdict"]["action"] in {"allow", "warn", "block"}
    assert await confirmed_on_chain(rpc, outcome["transaction"])
    after = await eventually(lambda: usdc_of(rpc, payer.pubkey()), lambda units: units != before)
    assert before - after == price


async def test_a_deep_check_paid_in_wsol_is_answered_once_for_a_replayed_payment(
    api: httpx.AsyncClient, config: E2EConfig, payer: Keypair, rpc: Rpc, target: Target
) -> None:
    url = page(target, PRODUCT_PAGE, "deep-wsol")
    document = await pricing(api)
    requirement = choose_requirement({"accepts": await offered(api, url, "deep")}, DEVNET_CAIP2, ("SOL",))
    amount = int(requirement["amount"])
    quoted = document["sol"]["x402"]["deep_lamports"]
    assert abs(amount - quoted) <= quoted * 0.02, f"deep costs {amount} lamports, /v1/pricing says {quoted}"
    wrapper = Payer(
        api_url=config.base_url, agent=payer, rpc=rpc, network="solana-devnet", guard=SpendGuard(0, 0), prefer="SOL"
    )
    await wrapper.ensure_wsol(amount)
    before = await wsol_of(rpc, payer.pubkey())

    paid, header = await paid_check(api, url, "deep", requirement, payer, rpc)

    assert paid.status_code == 200, paid.text
    receipt = b64json(paid.headers["PAYMENT-RESPONSE"])
    assert receipt["success"] is True
    assert receipt["payer"] == str(payer.pubkey())
    assert receipt["network"] == DEVNET_CAIP2
    assert paid.json()["tier"] == "deep"
    assert await confirmed_on_chain(rpc, receipt["transaction"])
    after = await eventually(lambda: wsol_of(rpc, payer.pubkey()), lambda units: units != before)
    assert before - after == amount

    replay = await api.post(
        "/v1/check", json={"url": url, "tier": "deep", "policy": "balanced"}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert replay.status_code == 200
    assert replay.json() == paid.json()
    assert replay.headers["PAYMENT-RESPONSE"] == paid.headers["PAYMENT-RESPONSE"]
    await asyncio.sleep(5)  # a second debit would have landed by now
    assert await wsol_of(rpc, payer.pubkey()) == after


async def test_an_underpaid_check_is_refused_before_any_money_moves(
    api: httpx.AsyncClient, payer: Keypair, rpc: Rpc, target: Target
) -> None:
    url = page(target, PRODUCT_PAGE, "underpay")
    requirement = choose_requirement({"accepts": await offered(api, url, "quick")}, DEVNET_CAIP2, ("USDC",))
    before = await usdc_of(rpc, payer.pubkey())

    response, _ = await paid_check(
        api, url, "quick", requirement, payer, rpc, amount=str(int(requirement["amount"]) - 1)
    )

    assert response.status_code == 402, response.text
    assert response.json()["error"] == "wrong_amount"
    assert "PAYMENT-RESPONSE" not in response.headers
    assert await usdc_of(rpc, payer.pubkey()) == before


async def test_a_malformed_payment_header_is_a_bad_request(api: httpx.AsyncClient, target: Target) -> None:
    body = {"url": page(target, PRODUCT_PAGE, "malformed"), "tier": "quick", "policy": "balanced"}

    response = await api.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": "not base64 at all!"})

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_PAYMENT"


async def test_a_paid_check_of_a_bot_check_page_charges_nothing(
    api: httpx.AsyncClient, payer: Keypair, rpc: Rpc, target: Target
) -> None:
    url = page(target, BOT_CHECK_PAGE, "x402-bot-check")
    requirement = choose_requirement({"accepts": await offered(api, url, "quick")}, DEVNET_CAIP2, ("USDC",))
    before = await usdc_of(rpc, payer.pubkey())

    response, _ = await paid_check(api, url, "quick", requirement, payer, rpc)

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "PAGE_BLOCKED"
    assert "PAYMENT-RESPONSE" not in response.headers
    await asyncio.sleep(5)
    assert await usdc_of(rpc, payer.pubkey()) == before


# ── Buying credits with crypto ─────────────────────────────────────────────────────────────────────────────────


async def test_a_pack_bought_in_usdc_credits_exactly_its_price(
    api: httpx.AsyncClient, account: Account, payer: Keypair, rpc: Rpc
) -> None:
    pack = the_pack(await pricing(api))
    before = await key_balance(api, account.key)
    response = await api.post(
        "/v1/credits/purchase-tx",
        json={
            "address": str(payer.pubkey()),
            "currency": "USDC",
            "checks": PACK,
            "country": "US",
            "key_id": account.key_id,
        },
    )
    assert response.status_code == 200, response.text
    purchase = response.json()
    assert purchase["checks"] == PACK
    assert purchase["mint"] == str(DEVNET_USDC)
    assert purchase["price"]["vat_usd_micros"] == 0
    assert purchase["price"]["net_usd_micros"] == purchase["amount"] == pack["crypto_usd_micros"]
    assert purchase["solana_pay_url"].startswith(f"solana:{purchase['hot_wallet']}?")

    signature = await rpc.send_and_confirm(sign_purchase(purchase["transaction"], payer))

    assert (await quote_paid(api, purchase["quote_id"]))["paid"], f"not credited within {QUOTE_PAID_WITHIN_S} s"
    assert await confirmed_on_chain(rpc, signature)
    assert await key_balance(api, account.key) - before == pack["credited_usd_micros"]


async def test_a_pack_paid_in_sol_by_its_solana_pay_reference_alone(
    api: httpx.AsyncClient, account: Account, payer: Keypair, rpc: Rpc
) -> None:
    document = await pricing(api)
    pack = the_pack(document)
    before = await key_balance(api, account.key)
    qr = await api.get(
        "/v1/credits/solana-pay.svg",
        params={"checks": PACK, "country": "US", "currency": "SOL", "key_id": account.key_id},
    )
    assert qr.status_code == 200, qr.text
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert qr.headers["cache-control"] == "no-store"
    assert qr.headers["x-unfret-total-usd"] == pack["crypto_usd"]
    assert qr.headers["x-unfret-vat-rate"] == "0"
    assert qr.text.lstrip().startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in qr.text
    request = parse_solana_pay(qr.headers["x-unfret-pay-url"])
    assert str(request.recipient) == document["hot_wallet"]
    assert request.mint is None and request.reference is not None and request.memo

    # A wallet that drops the memo: only the reference names the quote.
    signature = await rpc.send_and_confirm(reference_only_transfer(request, payer, await rpc.latest_blockhash()))

    assert (await quote_paid(api, qr.headers["x-unfret-quote"]))["paid"], (
        f"not credited within {QUOTE_PAID_WITHIN_S} s"
    )
    assert await confirmed_on_chain(rpc, signature)
    credited = await key_balance(api, account.key) - before
    expected = pack["credited_usd_micros"]
    assert abs(credited - expected) <= expected * SOL_CREDIT_TOLERANCE, f"credited {credited}, the pack is {expected}"


async def test_an_unknown_quote_is_a_404(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/credits/quotes/" + "z" * 20)

    assert response.status_code == 404
    assert response.json()["error_code"] == "UNKNOWN_QUOTE"


# ── Card top-ups (Polar, LIVE on dev: never complete an order) ────────────────────────────────────────────────


def skip_without_fiat(config: E2EConfig) -> None:
    if config.expect_fiat == "off":
        pytest.skip("card payments are off on the target (UNFRET_E2E_EXPECT_FIAT=off)")


async def test_a_card_checkout_is_a_polar_checkout_page(
    api: httpx.AsyncClient, config: E2EConfig, account: Account
) -> None:
    skip_without_fiat(config)

    response = await api.post("/v1/credits/fiat-checkout", json={"key_id": account.key_id, "checks": PACK})

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "polar"
    url = urlsplit(response.json()["checkout_url"])
    assert url.scheme == "https"
    assert url.hostname == "polar.sh" or str(url.hostname).endswith(".polar.sh")


async def test_a_paid_card_order_credits_once_and_its_refund_takes_it_back(
    api: httpx.AsyncClient, config: E2EConfig, account: Account
) -> None:
    skip_without_fiat(config)
    secret = config.need("fiat_webhook_secret")
    product = config.polar_products.get(PACK) or pytest.fail(f"UNFRET_E2E_POLAR_PRODUCTS has no {PACK}-pack product")
    pack = the_pack(await pricing(api))
    cents = pack["price_usd_micros"] // 10_000
    order_id = f"e2e-{secrets.token_hex(12)}"  # made up, never a real order; not the run's marker (it is logged)
    account_id = f"key:{account.key_id}"
    before = await key_balance(api, account.key)

    body, headers = signed_webhook(
        secret, "order.paid", polar_order(order_id, account_id, product, cents), timestamp=int(time.time())
    )
    paid = await api.post("/v1/webhooks/fiat", content=body, headers=headers)
    assert paid.status_code == 200, paid.text
    assert paid.json() == {"status": "credited"}
    assert await key_balance(api, account.key) == before + pack["credited_usd_micros"]

    resent = await api.post("/v1/webhooks/fiat", content=body, headers=headers)
    assert resent.json() == {"status": "duplicate"}
    assert await key_balance(api, account.key) == before + pack["credited_usd_micros"]

    refund = polar_order(order_id, account_id, product, cents, refunded_cents=cents)
    body, headers = signed_webhook(secret, "order.refunded", refund, timestamp=int(time.time()))
    refunded = await api.post("/v1/webhooks/fiat", content=body, headers=headers)
    assert refunded.json() == {"status": "debited"}
    assert await key_balance(api, account.key) == before


async def test_a_forged_or_stale_card_webhook_is_refused(
    api: httpx.AsyncClient, config: E2EConfig, account: Account
) -> None:
    skip_without_fiat(config)
    secret = config.need("fiat_webhook_secret")
    product = config.polar_products.get(PACK, "unknown")
    order = polar_order(f"e2e-{secrets.token_hex(12)}", f"key:{account.key_id}", product, 600)
    forger = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    before = await key_balance(api, account.key)

    async def send(signing_secret: str, timestamp: int) -> httpx.Response:
        body, headers = signed_webhook(signing_secret, "order.paid", order, timestamp=timestamp)
        return await api.post("/v1/webhooks/fiat", content=body, headers=headers)

    forged = await send(forger, int(time.time()))
    stale = await send(secret, int(time.time()) - 3600)

    assert forged.status_code == 400
    assert forged.json()["error_code"] == "BAD_REQUEST"
    assert stale.status_code == 400
    assert await key_balance(api, account.key) == before


# ── Public pages and operator endpoints ───────────────────────────────────────────────────────────────────────


async def test_the_showcase_lists_published_cases_only(api: httpx.AsyncClient) -> None:
    listing = await api.get("/v1/showcase")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "public, max-age=300"
    items = listing.json()["items"]
    if items:  # dev may have no published case; the listing and the 404 are checked either way
        case = await api.get(f"/v1/showcase/{items[0]['id']}")
        assert case.status_code == 200
        assert case.json()["band"] == items[0]["band"]
        assert (await api.get(f"/showcase/{items[0]['id']}")).status_code == 200

    missing = await api.get("/v1/showcase/2147483647")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/admin/keys/aaaaaaaaaa/grant"),
        ("POST", "/v1/admin/keys/aaaaaaaaaa/debit"),
        ("GET", "/v1/admin/reports"),
        ("GET", "/v1/admin/showcase"),
        ("POST", "/v1/admin/x402/clear-alerts"),
    ],
)
async def test_operator_endpoints_need_the_operator_token(api: httpx.AsyncClient, method: str, path: str) -> None:
    anonymous = await api.request(method, path, json={"usd_micros": 1})
    wrong = await api.request(method, path, json={"usd_micros": 1}, headers=bearer("not-the-operator-token"))

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert anonymous.json()["error_code"] == "UNAUTHORIZED"


# ── Privacy: nothing of the checked pages stayed behind (last: after every check above) ───────────────────────


def _remote(config: E2EConfig, *words: str) -> str:
    done = subprocess.run(
        ssh_command(config.ssh_target, *words), capture_output=True, text=True, timeout=300, check=False
    )
    assert done.returncode == 0, f"ssh {' '.join(words[:3])}…: exit {done.returncode}: {done.stderr[-300:]}"
    return done.stdout + done.stderr


async def test_no_checked_url_is_in_devs_logs_or_database(config: E2EConfig, target: Target) -> None:
    if not config.ssh_target:
        pytest.skip("set UNFRET_E2E_SSH=kamal@git.kroshtan.com to search dev's logs and database")
    since = target.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    names = _remote(config, "docker", "ps", "--format", "{{.Names}}", *DEV_APP_FILTER).split()
    assert names, "no running dev app container found"
    logs = "".join(_remote(config, "docker", "logs", "--since", since, name) for name in names)
    dump = _remote(config, "docker", "exec", DEV_DB_CONTAINER, "pg_dump", "-U", "unfret", "unfret")

    assert "key_granted" in logs, "the logs searched don't cover this run"
    assert "PostgreSQL database dump complete" in dump
    assert nonce_hits(logs, target.nonce) == 0, "a checked URL is in dev's logs"
    assert nonce_hits(dump, target.nonce) == 0, "a checked URL is in dev's database"
