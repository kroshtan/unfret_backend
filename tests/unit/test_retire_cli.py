"""The operator's ``retire-passes`` command: what it sends, what it prints (counts only), its exit status."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from unfret import admin_cli

KEY_ID = "wlmgabcdef"
REPORT: dict[str, Any] = {
    "before": "2026-11-20T09:30:00+00:00",
    "cutoff_day": "2026-11-20",
    "same_day": True,
    "confirm": False,
    "passes": 7,
    "passes_earlier_days": 6,
    "passes_same_day": 1,
    "kept_same_day_free": 2,
    "kept_named": 1,
    "keep_named": 1,
    "keep_unknown": 0,
    "passes_later": 0,
    "with_balance": 5,
    "balance_usd_micros": 5_000_000,
    "balance_checks": 25,
    "free_checks": 0,
    "purchases": 0,
    "card_checkouts": 0,
    "crypto_quotes": 0,
    "open_reports": 2,
    "report_limits": 2,
    "wallets": 0,
    "wallets_with_balance": 0,
    "wallet_balance_usd_micros": 0,
    "wallet_balance_checks": 0,
    "orphan_rows": 0,
    "refused": [],
    "retired": False,
}


def api(answer: dict[str, Any], sent: list[dict[str, Any]], status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/passes/retire"
        sent.append(json.loads(request.content))
        return httpx.Response(status, json=answer)

    return httpx.Client(base_url="https://dev.example", transport=httpx.MockTransport(handle))


def test_a_dry_run_sends_the_moment_and_the_kept_ids_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reviewer = tmp_path / "reviewer.json"
    reviewer.write_text(json.dumps({"key_id": KEY_ID, "key": "ufk_" + KEY_ID + "_" + "a" * 52, "recovery_code": "x"}))
    sent: list[dict[str, Any]] = []

    status = admin_cli.run(
        api(REPORT, sent), ["retire-passes", "--before", "2026-11-20T09:30", "--keep-from", str(reviewer)], "dev"
    )

    assert status == 0
    assert sent == [
        {
            "before": "2026-11-20T09:30:00+00:00",  # no zone: UTC
            "keep": [KEY_ID],
            "confirm": False,
            "include_wallet_balances": False,
            "include_purchases": False,
        }
    ]
    out = capsys.readouterr().out
    assert "Retire passes on dev: made before 2026-11-20, or on 2026-11-20 without free checks (dry run" in out
    assert "passes to retire                                 7" in out
    assert "(25 checks)" in out
    assert "Run again with --confirm" in out
    assert KEY_ID not in out and "ufk_" not in out


def test_a_refusal_exits_1_and_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    refused = REPORT | {"confirm": True, "wallets_with_balance": 1, "refused": ["1 wallets hold a balance: ..."]}

    status = admin_cli.run(api(refused, []), ["retire-passes", "--before", "2026-11-20", "--confirm"], "dev")

    assert status == 1
    out = capsys.readouterr().out
    assert "REFUSED, nothing changed" in out and "1 wallets hold a balance" in out


def test_keep_takes_a_key_id_or_a_whole_key_and_sends_only_the_id() -> None:
    sent: list[dict[str, Any]] = []
    key = f"ufk_{KEY_ID}_" + "b" * 52

    admin_cli.run(api(REPORT, sent), ["retire-passes", "--before", "2026-11-20", "--keep", key, "--confirm"])

    assert sent[0]["keep"] == [KEY_ID] and sent[0]["confirm"] is True
    assert key not in json.dumps(sent)


def test_a_keep_file_without_a_key_id_stops_before_sending(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"key": "secret"}')
    sent: list[dict[str, Any]] = []

    with pytest.raises(SystemExit, match="no readable key_id"):
        admin_cli.run(api(REPORT, sent), ["retire-passes", "--before", "2026-11-20", "--keep-from", str(bad)])

    assert sent == []


def test_a_malformed_moment_is_refused_by_the_command_line() -> None:
    with pytest.raises(SystemExit):
        admin_cli.run(api(REPORT, []), ["retire-passes", "--before", "next tuesday"])
