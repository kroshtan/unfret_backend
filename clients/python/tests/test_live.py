"""
Against a real server: ``UNFRET_LIVE_URL=https://dev.unfret.net uv run pytest -m live``.

Makes a developer pass (one a day per network; without one the deep checks are skipped) and checks the test shop:
one deep check (one of the pass's free checks) and one quick check (free).
"""

import os

import pytest

from unfret_client import AsyncUnfret, NoChecksLeftError, Unfret

pytestmark = pytest.mark.live
LIVE = os.environ.get("UNFRET_LIVE_URL", "")


@pytest.fixture(scope="module")
def client() -> Unfret:
    if not LIVE:
        pytest.skip("UNFRET_LIVE_URL not set")
    client = Unfret(os.environ.get("UNFRET_LIVE_PASS"), base_url=LIVE)
    if not client.unfret_pass:
        issued = client.new_developer_pass()
        print(f"developer pass {issued['key_id']}: {issued['free_checks']} free checks")
    return client


def test_a_deep_check_of_the_test_shop(client: Unfret) -> None:
    try:
        decision = client.check(f"{LIVE}/test-shop")
    except NoChecksLeftError:
        pytest.skip("this network already had its developer pass today: set UNFRET_LIVE_PASS")
    assert decision.action in {"allow", "warn", "block"}
    assert decision.verdict["tier"] == "deep" and decision.verdict["charged"] in {True, False}
    print(decision.action, decision.label, decision.reasons)


def test_a_quick_check_is_free(client: Unfret) -> None:
    before = client.checks_left()["checks_remaining"]
    decision = client.check(f"{LIVE}/test-shop", tier="quick")
    assert decision.verdict["charged"] is False
    assert client.checks_left()["checks_remaining"] == before


async def test_the_async_client(client: Unfret) -> None:
    async with AsyncUnfret(client.unfret_pass, base_url=LIVE) as unfret:
        assert (await unfret.checks_left())["checks_remaining"] >= 0
