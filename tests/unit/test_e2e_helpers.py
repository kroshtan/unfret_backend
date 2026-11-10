"""The e2e sweep's own logic (tests/e2e/helpers.py), checked without any network."""

import base64
import json
import time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM
from solders.transaction import VersionedTransaction

from tests.e2e.helpers import (
    DEV_URL,
    DEVNET_CAIP2,
    FORGE_RAW,
    MAINNET_CAIP2,
    MIN_SOL_LAMPORTS,
    MIN_USDC_UNITS,
    E2EConfig,
    E2EConfigError,
    fixture_url,
    funding_problem,
    netting,
    nonce_hits,
    parse_keypair,
    parse_solana_pay,
    parse_sse,
    polar_order,
    reference_only_transfer,
    refuse_mainnet,
    run_nonce,
    sign_purchase,
    signed_webhook,
    ssh_command,
    x402_header,
)
from unfret.config import Settings
from unfret.credits import polar
from unfret.credits.pricing import purchase_transaction, solana_pay_url
from unfret.verdict_store import listing_key
from unfret.x402.solana import USDC_MINTS

ROOT = Path(__file__).resolve().parents[2]
SECRET = "whsec_" + base64.b64encode(b"e2e-helpers-webhook-secret-32byt").decode()
PRODUCT = "2f1c9a6e-6c1b-4a51-9a1e-3f4f0c1d2b25"
BLOCKHASH = "EZ3rST5dvHmbanh75jc4PuLfV96vp9fEYBVeNk4FfM1k"


# ── Settings ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_defaults_point_at_dev() -> None:
    config = E2EConfig.from_env({})
    assert config.base_url == DEV_URL == "https://dev.unfret.net"
    assert config.expect_fiat == "polar:production"
    assert not config.no_skip
    assert not config.ssh_target


def test_settings_are_read_and_secrets_kept_out_of_repr() -> None:
    config = E2EConfig.from_env(
        {
            "UNFRET_E2E_URL": "http://localhost:8080/ ",
            "UNFRET_E2E_KEY": "ufk_secret",
            "UNFRET_E2E_ADMIN_TOKEN": "operator",
            "UNFRET_E2E_POLAR_PRODUCTS": f"25={PRODUCT}, 50=other",
            "UNFRET_E2E_NO_SKIP": "1",
            "UNFRET_E2E_COMMIT": "abc1234",
        }
    )
    assert config.base_url == "http://localhost:8080"
    assert config.polar_products == {25: PRODUCT, 50: "other"}
    assert config.no_skip and config.expected_commit == "abc1234"
    assert "ufk_secret" not in repr(config) and "operator" not in repr(config)
    assert config.need("api_key") == "ufk_secret"
    with pytest.raises(E2EConfigError, match="UNFRET_E2E_PAYER_KEY"):
        config.need("payer_key")


def test_malformed_polar_products_are_refused() -> None:
    with pytest.raises(E2EConfigError, match="POLAR_PRODUCTS"):
        E2EConfig.from_env({"UNFRET_E2E_POLAR_PRODUCTS": "25"})


# ── Never mainnet ─────────────────────────────────────────────────────────────────────────────────────────────


def test_devnet_is_accepted() -> None:
    refuse_mainnet({"network": DEVNET_CAIP2, "network_name": "solana-devnet"})


@pytest.mark.parametrize(
    "discovery",
    [
        {"network": MAINNET_CAIP2, "network_name": "solana-mainnet"},
        {"network": MAINNET_CAIP2, "network_name": "solana-devnet"},  # a lying name doesn't help
        {"network": DEVNET_CAIP2, "network_name": "solana-mainnet"},
    ],
)
def test_mainnet_is_refused(discovery: dict[str, str]) -> None:
    with pytest.raises(E2EConfigError, match="MAINNET"):
        refuse_mainnet(discovery)


@pytest.mark.parametrize("discovery", [{}, {"network": "solana:testnet", "network_name": "solana-testnet"}])
def test_anything_but_devnet_is_refused(discovery: dict[str, str]) -> None:
    with pytest.raises(E2EConfigError, match="not on Solana devnet"):
        refuse_mainnet(discovery)


# ── The payer ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_payer_key_is_a_cli_byte_array_or_base58() -> None:
    keypair = Keypair()
    assert parse_keypair(json.dumps(list(bytes(keypair)))).pubkey() == keypair.pubkey()
    assert parse_keypair(f" {keypair} ").pubkey() == keypair.pubkey()
    with pytest.raises(E2EConfigError, match="not a keypair"):
        parse_keypair("[1, 2, 3]")


def test_an_unfunded_payer_is_told_where_to_send_what() -> None:
    assert funding_problem("Payer111", MIN_SOL_LAMPORTS, MIN_USDC_UNITS) is None
    message = funding_problem("Payer111", MIN_SOL_LAMPORTS - 1, 1_000_000)
    assert message is not None
    assert message.startswith("fund Payer111 on devnet")
    assert "1.5 SOL" in message and "5.01 devnet USDC (has 1)" in message


# ── Fixture URLs and the run's marker ─────────────────────────────────────────────────────────────────────────


def test_fixture_urls_are_immutable_unique_and_keep_the_marker() -> None:
    nonce = run_nonce()
    url = fixture_url("d441b63f", "script-shell.html", nonce, "too thin")
    assert url == f"{FORGE_RAW}/d441b63f/tests/fixtures/pages/script-shell.html?e2e={nonce}-too%20thin"
    assert nonce != run_nonce() and len(nonce) == 27
    # The marker isn't dropped as a tracking parameter, so each use is its own listing (no fast-path answer).
    assert listing_key(url) != listing_key(fixture_url("d441b63f", "script-shell.html", nonce, "other"))
    with pytest.raises(E2EConfigError):
        fixture_url("main", "script-shell.html", nonce, "x")


@pytest.mark.parametrize("name", ["shopify-productgroup-us.html", "bot-check-interstitial.html", "script-shell.html"])
def test_the_fixture_pages_exist(name: str) -> None:
    assert (ROOT / "tests" / "fixtures" / "pages" / name).is_file()


def test_the_fixture_pages_read_as_intended() -> None:
    from unfret.page_fetch import is_challenge_page, is_too_thin  # noqa: PLC0415

    pages = ROOT / "tests" / "fixtures" / "pages"
    product = (pages / "shopify-productgroup-us.html").read_text()
    blocked = (pages / "bot-check-interstitial.html").read_text()
    shell = (pages / "script-shell.html").read_text()
    url = f"{FORGE_RAW}/abc1234/tests/fixtures/pages/page.html"
    assert not is_challenge_page(product) and not is_too_thin(product, url)
    assert is_challenge_page(blocked)
    assert not is_challenge_page(shell) and is_too_thin(shell, url)


def test_the_marker_is_counted() -> None:
    assert nonce_hits("a e2eabc b e2eabc", "e2eabc") == 2
    assert nonce_hits("nothing here", "e2eabc") == 0


# ── Putting balances back ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("delta", "steps"),
    [
        (0, []),
        (400_000, [("debit", 400_000)]),
        (-200_000, [("grant", 200_000)]),
        (24_600_000, [("debit", 10_000_000), ("debit", 10_000_000), ("debit", 4_600_000)]),
    ],
)
def test_netting_undoes_the_runs_balance_change(delta: int, steps: list[tuple[str, int]]) -> None:
    assert netting(delta) == steps


# ── Streams, x402 headers, purchases ──────────────────────────────────────────────────────────────────────────


def test_server_sent_events_are_parsed_in_order() -> None:
    text = 'event: step\ndata: {"skill": "a", "findings": []}\n\nevent: result\ndata: {"score": 80}\n\n'
    assert parse_sse(text) == [("step", {"skill": "a", "findings": []}), ("result", {"score": 80})]


def test_the_payment_header_carries_the_offered_requirement_and_the_transaction() -> None:
    from unfret.x402.payment import decode_header  # noqa: PLC0415

    keypair = Keypair()
    raw = purchase_transaction(keypair.pubkey(), Keypair().pubkey(), 1, BLOCKHASH, "memo")
    tx = VersionedTransaction.from_bytes(raw)
    requirement = {"scheme": "exact", "amount": "10000"}
    decoded = decode_header(x402_header(requirement, tx))
    assert decoded["x402Version"] == 2
    assert decoded["accepted"] == requirement
    assert base64.b64decode(decoded["payload"]["transaction"]) == bytes(tx)


def test_the_buyer_signs_the_purchase_transaction() -> None:
    buyer, hot = Keypair(), Keypair().pubkey()
    raw = purchase_transaction(
        buyer.pubkey(), hot, 5_000_000, BLOCKHASH, "unfret:quote:x", mint=USDC_MINTS["solana-devnet"]
    )
    signed = VersionedTransaction.from_bytes(sign_purchase(base64.b64encode(raw).decode(), buyer))
    assert signed.verify_with_results() == [True]
    with pytest.raises(E2EConfigError):
        sign_purchase(base64.b64encode(raw).decode(), Keypair())


@pytest.mark.parametrize("mint", [None, USDC_MINTS["solana-devnet"]])
def test_a_solana_pay_link_is_read_back_exactly(mint: Pubkey | None) -> None:
    hot, reference = Keypair().pubkey(), Keypair().pubkey()
    url = solana_pay_url(hot, 59_280_123, "unfret:quote:abc", mint, reference=str(reference))

    request = parse_solana_pay(url)

    assert request.recipient == hot
    assert request.amount == 59_280_123
    assert request.mint == mint
    assert request.reference == reference
    assert request.memo == "unfret:quote:abc"


def test_a_reference_only_payment_names_the_quote_by_its_reference_and_has_no_memo() -> None:
    from unfret.credits.scanner import account_keys  # noqa: PLC0415

    payer, hot, reference = Keypair(), Keypair().pubkey(), Keypair().pubkey()
    request = parse_solana_pay(solana_pay_url(hot, 1_000, "unfret:quote:abc", reference=str(reference)))

    tx = VersionedTransaction.from_bytes(reference_only_transfer(request, payer, BLOCKHASH))

    keys = tx.message.account_keys
    assert tx.verify_with_results() == [True]
    assert [keys[ix.program_id_index] for ix in tx.message.instructions] == [SYSTEM_PROGRAM]
    assert reference in keys and hot in keys
    parsed = {"transaction": {"message": {"accountKeys": [{"pubkey": str(k)} for k in keys]}}}
    assert str(reference) in account_keys(parsed)
    with pytest.raises(E2EConfigError, match="reference"):
        reference_only_transfer(parse_solana_pay(solana_pay_url(hot, 1_000)), payer, BLOCKHASH)


def test_non_solana_pay_links_are_refused() -> None:
    with pytest.raises(E2EConfigError):
        parse_solana_pay("https://example.com/?amount=1")
    with pytest.raises(E2EConfigError, match="decimals"):
        parse_solana_pay(f"solana:{Keypair().pubkey()}?amount=0.0000000001")
    assert parse_solana_pay(f"solana:{Keypair().pubkey()}?amount=1.5").amount == int(Decimal("1.5") * 10**9)


# ── Card webhooks ─────────────────────────────────────────────────────────────────────────────────────────────


def provider() -> polar.PolarProvider:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        polar_access_token=SecretStr("polar_oat_test"),
        polar_products=f"25={PRODUCT},50=b,100=c",
        fiat_webhook_secret=SecretStr(SECRET),
    )
    built = polar.from_settings(settings)
    assert built is not None
    return built


def test_the_made_up_orders_are_read_by_the_real_polar_provider() -> None:
    now = int(time.time())
    body, headers = signed_webhook(
        SECRET, "order.paid", polar_order("e2e-1", "key:abcdefghij", PRODUCT, 600), timestamp=now
    )
    paid = provider().parse_webhook(headers, body, now)
    assert paid is not None
    assert (paid.kind, paid.order_id, paid.account, paid.amount_cents) == ("paid", "e2e-1", "key:abcdefghij", 600)

    refund = polar_order("e2e-1", "key:abcdefghij", PRODUCT, 600, refunded_cents=600)
    body, headers = signed_webhook(SECRET, "order.refunded", refund, timestamp=now)
    refunded = provider().parse_webhook(headers, body, now)
    assert refunded is not None
    assert (refunded.kind, refunded.refunded_cents) == ("refunded", 600)


def test_forged_and_stale_webhooks_fail_the_real_check() -> None:
    from unfret.credits.fiat import FiatWebhookError  # noqa: PLC0415

    now = int(time.time())
    order = polar_order("e2e-2", "key:abcdefghij", PRODUCT, 600)
    other = "whsec_" + base64.b64encode(b"another-secret-of-thirty-two-byt").decode()
    for secret, stamp in ((other, now), (SECRET, now - 3600)):
        body, headers = signed_webhook(secret, "order.paid", order, timestamp=stamp)
        with pytest.raises(FiatWebhookError):
            provider().parse_webhook(headers, body, now)


# ── ssh ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_ssh_never_prompts_never_rewrites_known_hosts_and_quotes_the_remote_words() -> None:
    argv = ssh_command("kamal@git.kroshtan.com", "docker", "ps", "--format", "{{.Names}}")
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no"]
    assert argv[7:9] == ["kamal@git.kroshtan.com", "--"]
    assert argv[9] == "docker ps --format '{{.Names}}'"


@pytest.mark.parametrize("target", ["-oProxyCommand=evil", "git.kroshtan.com", "kamal@host;rm"])
def test_ssh_targets_that_arent_user_at_host_are_refused(target: str) -> None:
    with pytest.raises(E2EConfigError):
        ssh_command(target, "true")
