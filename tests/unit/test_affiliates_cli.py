"""The operator's ``make affiliates`` and ``make bonus-codes``: what each command sends and prints."""

import json
from typing import Any

import httpx
import pytest

from unfret.credits.affiliates_cli import describe_report, money, run


def api(seen: list[httpx.Request], reply: dict[str, Any], status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=reply)

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="https://unfret.test")


def test_money_is_cents_as_an_amount() -> None:
    assert (money(13250), money(-132), money(5)) == ("132.50", "-1.32", "0.05")


def test_create_prints_both_links(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx.Request] = []
    links = {"ref": "https://unfret.net/?ref=ANNA", "stats": "https://unfret.net/affiliates/stats#ufa_x"}
    reply = {"id": 1, "code": "ANNA", "name": "Anna", "links": links}

    assert run(api(seen, reply), ["affiliate", "create", "anna", "--name", "Anna", "--note", "IBAN"]) == 0

    assert json.loads(seen[0].content) == {"code": "anna", "name": "Anna", "payout_note": "IBAN"}
    out = capsys.readouterr().out
    assert "https://unfret.net/?ref=ANNA" in out and "#ufa_x" in out and "shown once" in out


def test_paid_and_bonus_codes_send_what_was_asked(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx.Request] = []
    assert run(api(seen, {"code": "ANNA", "paid": {"usd": 13200}}), ["affiliate", "paid", "anna"]) == 0
    assert str(seen[-1].url).endswith("/v1/admin/affiliates/ANNA/paid")
    assert json.loads(seen[-1].content) == {"note": ""}
    assert "recorded a payout of 132.00 USD" in capsys.readouterr().out
    made = {"code": "MORE20", "label": "+20% checks"}
    assert run(api(seen, made), ["bonus", "create", "MORE20", "--bonus", "20", "--uses", "5"]) == 0
    assert str(seen[-1].url).endswith("/v1/admin/bonus-codes")
    assert json.loads(seen[-1].content) == {
        "code": "MORE20",
        "bonus_pct": 20,
        "expires_on": None,
        "max_uses": 5,
        "affiliate": None,
    }
    assert "MORE20: +20% checks" in capsys.readouterr().out


def test_a_refusal_exits_with_the_reason(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(api([], {"detail": "ANNA is taken."}, status=409), ["affiliate", "create", "ANNA", "--name", "A"]) == 1
    assert "ANNA is taken." in capsys.readouterr().err


def test_the_report_lists_months_and_what_is_owed() -> None:
    body = {
        "affiliates": {
            "1": {
                "codes": ["ANNA"],
                "name": "Anna",
                "active": True,
                "balance": {"usd": {"payable": 13200, "held": 500, "due": 13200, "carried_over": 0}},
            }
        },
        "rules": {"hold_days": 30, "min_payout_cents": 2500},
        "months": [
            {
                "affiliate_id": 1,
                "month": "2026-12",
                "passes": 4,
                "first_checks": 3,
                "purchases": 1,
                "money": {"usd": {"net": 53100, "commission": 13275, "reversed": 75, "paid": 0}},
            }
        ],
    }
    text = describe_report(body)
    assert "2026-12" in text and "ANNA (Anna)" in text
    assert "USD net 531.00 earned 132.75 reversed 0.75 paid 0.00" in text
    assert "ANNA (Anna): USD: due 132.00, carried over 0.00, held 5.00" in text
    assert "a payout needs 25.00 per currency" in text
