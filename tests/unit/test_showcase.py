"""The showcase's public wording, its case page, the review CLI and the promotion offer in ``make reports``."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import reports as labelling
from unfret import showcase
from unfret.routes.pages import STATIC_DIR
from unfret.routes.showcase import render_case_page
from unfret.showcase import cli

CHECKED = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)


def row(**extra: Any) -> dict[str, Any]:
    response = {
        "score": 15,
        "explanation": "A scam shop.",
        "verdict": {
            "band": "junk_or_scam",
            "label": "Many warning signs",
            "headline": "This is a scam.",
            "reasons": [
                {
                    "type": "new_domain",
                    "title": "A brand-new shop",
                    "detail": "Registered 3 weeks ago.",
                    "severity": "high",
                },
                {"type": "pushy_copy", "title": "Pushy sales talk", "detail": '"Not a SCAM!"', "severity": "medium"},
                {"type": "x", "title": "Fraudulent?", "detail": "", "severity": "low"},
            ],
            "positives": [{"type": "vat_registered", "title": "A valid VAT number", "detail": "NL123"}],
        },
        "assessment": {
            "risk_score": 0.9,
            "confidence": 0.75,
            "model_version": "v7",
            "flags": [
                {"type": "content.scam_copy", "label": "Pushy sales talk", "evidence": "scam", "weight": 1.2},
                {"type": "domain_age.age_years", "label": "Domain age", "evidence": "0.1", "kind": "context"},
            ],
        },
    }
    return {
        "id": 3,
        "title": "LED <mask>",
        "host": "shop.example",
        "product_url": "https://shop.example/p?id=1&x=2",
        "checked_at": CHECKED,
        "published_at": CHECKED,
        "screenshot_type": None,
        "response": response,
        **extra,
    }


@pytest.mark.parametrize(
    ("band", "public", "symbol", "label"),
    [
        ("reliable", "reliable", "✓", "Unfret found nothing worrying on this listing"),
        ("concerns", "concerns", "!", "Unfret would suggest a closer look at this listing"),
        ("careful", "careful", "⚠", "Unfret would point out warning signs on this listing"),
        ("junk_or_scam", "warning", "✕", "Unfret would warn you about this listing"),
    ],
)
def test_each_band_describes_the_listing(band: str, public: str, symbol: str, label: str) -> None:
    result = showcase.public_result({"score": 50, "verdict": {"band": band, "headline": "Fine.", "reasons": []}})
    assert result["verdict"] | {"reasons": [], "positives": []} == {
        "band": public,
        "label": label,
        "symbol": symbol,
        "headline": "Fine.",
        "reasons": [],
        "positives": [],
    }
    assert "scam" not in json.dumps(result).lower()


def test_the_public_view_never_says_scam_and_shows_no_score() -> None:
    left_out: list[str] = []
    view = showcase.public_view(row(), left_out)

    text = json.dumps(view, default=str)
    assert "scam" not in text.lower() and "fraud" not in text.lower()
    assert "score" not in text and "explanation" not in text and "risk" not in text
    verdict = view["result"]["verdict"]
    assert verdict["headline"].startswith("Many warning signs: ")  # the catalogue's headline for the band
    assert [r["title"] for r in verdict["reasons"]] == ["A brand-new shop", "Pushy sales talk"]
    assert verdict["reasons"][1]["detail"] == ""  # the quote is left out, the finding stays
    assert verdict["positives"] == [{"title": "A valid VAT number", "detail": "NL123"}]
    assert view["result"]["assessment"]["flags"][0] == {"label": "Pushy sales talk", "evidence": "", "kind": "signal"}
    assert left_out == [
        "headline",
        "detail evidence: Pushy sales talk",
        "reason detail: Pushy sales talk",
        "reason x",
    ]
    assert view["checked_on"] == "9 November 2026"


def test_a_band_without_a_verdict_follows_the_score() -> None:
    assert showcase.public_result({"score": 90})["band"] == "reliable"
    assert showcase.public_result({"score": 5})["band"] == "warning"


def test_the_case_page_escapes_everything_from_the_page() -> None:
    data = row(title="</script><script>alert(1)</script>")
    data["response"]["verdict"]["reasons"][0]["detail"] = "</script><img src=x onerror=alert(1)>"

    page = render_case_page(showcase.public_view(data))

    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert page.count("</script>") == 2  # the data block's and the module script's own
    assert "&lt;/script&gt;&lt;script&gt;alert(1)" in page
    assert 'href="https://shop.example/p?id=1&amp;x=2" rel="nofollow noopener"' in page
    assert "Checked on 9 November 2026; the page may have changed since." in page
    assert "scam" not in page.lower()


def test_the_case_page_template_and_scripts_never_say_scam() -> None:
    for name in ("showcase.html", "showcase.js", "showcase-list.js", "check-render.js"):
        assert "scam" not in (STATIC_DIR / name).read_text().lower(), name


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\x89PNG\r\n\x1a\n...", "image/png"),
        (b"\xff\xd8\xff\xe0...", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"<svg/>", None),
        (b"GIF89a", None),
    ],
)
def test_screenshots_are_recognised_by_their_bytes(data: bytes, kind: str | None) -> None:
    assert showcase.image_type(data) == kind


def test_urls_lose_their_fragment_and_must_be_web_addresses() -> None:
    assert showcase.clean_url(" https://Shop.example/p?id=1#reviews ") == "https://Shop.example/p?id=1"
    assert showcase.host_of("https://www.Shop.example/p") == "shop.example"
    with pytest.raises(showcase.ShowcaseError):
        showcase.clean_url("javascript:alert(1)")


# ── The CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────


def operator_case(**extra: Any) -> dict[str, Any]:
    view = showcase.public_view(row())
    return {
        "id": 3,
        "status": "draft",
        "origin": "operator",
        "title": "LED <mask>",
        "host": "shop.example",
        "product_url": "https://shop.example/p",
        "checked_at": CHECKED.isoformat(),
        "published_at": None,
        "removed_at": None,
        "removed_reason": None,
        "screenshot": None,
        "response": {"score": 15},
        "public": {k: v for k, v in view.items() if k not in {"checked_at", "published_at"}},
        "left_out": ["headline"],
        **extra,
    }


def recording_api(seen: list[httpx.Request], answer: httpx.Response) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return answer

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


@pytest.mark.parametrize(
    ("argv", "method", "path", "body"),
    [
        (["add", "https://shop.example/p", "--title", "LED mask"], "POST", "/v1/admin/showcase",
         {"url": "https://shop.example/p", "origin": "operator", "title": "LED mask"}),
        (["publish", "3"], "POST", "/v1/admin/showcase/3/publish", None),
        (["remove", "3", "--reason", "owner asked"], "POST", "/v1/admin/showcase/3/remove", {"reason": "owner asked"}),
        (["show", "3"], "GET", "/v1/admin/showcase/3", None),
        (["title", "3", "LED face mask"], "POST", "/v1/admin/showcase/3/title", {"title": "LED face mask"}),
    ],
)  # fmt: skip
def test_cli_commands_call_the_admin_api(
    argv: list[str], method: str, path: str, body: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    with recording_api(seen, httpx.Response(200, json=operator_case())) as api:
        assert cli.run(api, argv) == 0

    [request] = seen
    assert (request.method, request.url.path) == (method, path)
    assert (json.loads(request.content) if request.content else None) == body
    out = capsys.readouterr().out
    assert "Unfret would warn you about this listing" in out and "Left out of the public page" in out


def test_cli_uploads_a_screenshot_and_reports_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nxyz")
    seen: list[httpx.Request] = []
    with recording_api(seen, httpx.Response(422, json={"detail": "too big"})) as api:
        assert cli.run(api, ["screenshot", "3", str(shot)]) == 1

    assert seen[0].method == "PUT" and seen[0].content == shot.read_bytes()
    assert "Failed (422): too big" in capsys.readouterr().err


def test_cli_lists_cases(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[httpx.Request] = []
    with recording_api(seen, httpx.Response(200, json={"items": [operator_case(status="published")]})) as api:
        assert cli.run(api, ["list", "--status", "published"]) == 0
    assert seen[0].url.params["status"] == "published"
    assert "#3" in capsys.readouterr().out


# ── Promotion from `make reports` ─────────────────────────────────────────────────────────────────────────────────

ITEM = {"id": 7, "payload": {"url": "https://shop.example/p", "text": "secret", "explanation": "x"}, "score": 12}


@pytest.mark.parametrize(("gap", "key", "promoted"), [(0, "p", True), (0, "n", False), (35, "p", False)])
def test_a_right_answer_can_be_promoted_with_only_its_address(
    monkeypatch: pytest.MonkeyPatch, gap: int, key: str, promoted: bool
) -> None:
    monkeypatch.setattr(labelling, "read_key", lambda: key)
    seen: list[httpx.Request] = []
    with recording_api(seen, httpx.Response(201, json={"id": 4})) as api:
        labelling.offer_promotion(api, ITEM, gap)

    if not promoted:
        assert seen == []
        return
    [request] = seen
    assert request.url.path == "/v1/admin/showcase"
    assert json.loads(request.content) == {"url": "https://shop.example/p", "origin": "report"}  # nothing else


def test_a_report_without_an_address_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labelling, "read_key", lambda: pytest.fail("asked"))
    with recording_api([], httpx.Response(201, json={"id": 4})) as api:
        labelling.offer_promotion(api, {"payload": {"url": ""}}, 0)
