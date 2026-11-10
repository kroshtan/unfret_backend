"""
Fixtures of the dev sweep: the target (refused unless it is on devnet), the payer, and the long-lived e2e key.

Nothing here runs unless the sweep is selected (``pytest -m e2e tests/e2e``; ``make e2e``): the default test run
neither collects ``tests/e2e`` (``testpaths``) nor selects the marker (``addopts``).
"""

from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from solders.keypair import Keypair
from unfret_mcp.solana import USDC_MINTS, Rpc, associated_token_address  # on sys.path: tests/e2e/__init__

from tests.e2e.helpers import (
    E2EConfig,
    E2EConfigError,
    funding_problem,
    netting,
    parse_keypair,
    refuse_mainnet,
    run_nonce,
)
from unfret.credits import keys

# The working credit granted to the e2e key for the run (two charged analyses and the error paths need a balance);
# taken back at the end, with everything else the run added, so the key ends where it started.
WORKING_CREDIT_USD_MICROS = 1_000_000


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, Any]:
    """With ``UNFRET_E2E_NO_SKIP`` (the production gate), a skipped part of the sweep fails it instead."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped and "e2e" in item.keywords and E2EConfig.from_env().no_skip:
        report.outcome = "failed"
        report.longrepr = f"skipped, but UNFRET_E2E_NO_SKIP is set: {report.longrepr}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class Target:
    """The deployment under test, as it described itself before anything was spent."""

    discovery: dict[str, Any]
    health: dict[str, Any]
    fixture_ref: str  # the commit the fixture pages are served from
    nonce: str  # this run's marker in every fixture URL
    started_at: datetime


@dataclass(frozen=True)
class Account:
    """The long-lived e2e API key."""

    key: str
    key_id: str
    start_usd_micros: int


@pytest.fixture(scope="session")
def config() -> E2EConfig:
    try:
        return E2EConfig.from_env()
    except E2EConfigError as exc:
        pytest.fail(str(exc))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api(config: E2EConfig) -> AsyncIterator[httpx.AsyncClient]:
    # Analyses answer within analysis_timeout_s (90 s); a paid deep check within about the same.
    timeout = httpx.Timeout(150, connect=15)
    async with httpx.AsyncClient(base_url=config.base_url, timeout=timeout, headers={"User-Agent": "unfret-e2e"}) as c:
        yield c


@pytest.fixture(scope="session")
def payer(config: E2EConfig) -> Keypair:
    try:
        return parse_keypair(config.need("payer_key"))
    except E2EConfigError as exc:
        pytest.fail(str(exc))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def target(api: httpx.AsyncClient, config: E2EConfig) -> Target:
    """Refuse anything but devnet, check the commit, and pick the fixtures' commit, before any test spends."""
    started = datetime.now(UTC) - timedelta(seconds=30)  # some slack for the server's clock
    discovery = await api.get("/.well-known/x402")
    assert discovery.status_code == 200, f"{config.base_url}/.well-known/x402: {discovery.status_code}"
    try:
        refuse_mainnet(discovery.json())
    except E2EConfigError as exc:
        pytest.exit(str(exc), returncode=3)
    health = (await api.get("/v1/health")).json()
    commit = health.get("commit") or ""
    if config.expected_commit and commit != config.expected_commit:
        pytest.fail(f"{config.base_url} runs commit {commit or 'unknown'}, not {config.expected_commit}")
    ref = config.fixture_ref or commit
    if not ref:
        pytest.fail(f"{config.base_url} reports no commit; set UNFRET_E2E_FIXTURE_REF to a pushed commit")
    return Target(discovery.json(), health, ref, run_nonce(), started)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def preflight(target: Target, payer: Keypair, config: E2EConfig) -> None:
    """Every test starts behind the devnet check and a funded payer (never topped up from a faucet here)."""
    rpc = Rpc(config.rpc_url)
    sol = await rpc.balance(payer.pubkey())
    usdc = await rpc.token_balance(associated_token_address(payer.pubkey(), USDC_MINTS["solana-devnet"]))
    if problem := funding_problem(str(payer.pubkey()), sol, usdc):
        pytest.fail(problem)


async def key_balance(api: httpx.AsyncClient, key: str) -> int:
    response = await api.get("/v1/credits", headers=bearer(key))
    assert response.status_code == 200, f"GET /v1/credits: {response.status_code} {response.text[:200]}"
    balance: int = response.json()["balance_usd_micros"]
    return balance


async def admin_adjust(api: httpx.AsyncClient, config: E2EConfig, key_id: str, kind: str, usd_micros: int) -> int:
    response = await api.post(
        f"/v1/admin/keys/{key_id}/{kind}", json={"usd_micros": usd_micros}, headers=bearer(config.need("admin_token"))
    )
    assert response.status_code == 200, f"admin {kind} {usd_micros}: {response.status_code} {response.text[:200]}"
    balance: int = response.json()["balance_usd_micros"]
    return balance


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def account(api: httpx.AsyncClient, config: E2EConfig) -> AsyncIterator[Account]:
    """The e2e key with a working credit for the run; at the end its balance is put back exactly where it was."""
    try:
        key = config.need("api_key")
        key_id = keys.key_id_from(key)
    except (E2EConfigError, keys.ApiKeyError) as exc:
        pytest.fail(f"UNFRET_E2E_KEY: {exc}")
    start = await key_balance(api, key)
    await admin_adjust(api, config, key_id, "grant", WORKING_CREDIT_USD_MICROS)
    yield Account(key, key_id, start)
    for kind, amount in netting(await key_balance(api, key) - start):
        await admin_adjust(api, config, key_id, kind, amount)
    end = await key_balance(api, key)
    assert end == start, f"the e2e key ends at {end} µ$, not where it started ({start} µ$)"
