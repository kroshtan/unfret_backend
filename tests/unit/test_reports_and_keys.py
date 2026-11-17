import pytest

from unfret import reports
from unfret.credits import keys
from unfret.credits.keys import KeyGuard
from unfret.facts import PageFacts
from unfret.reports.scrub import clean_url, page_identity, page_of, scrub_text
from unfret.schemas.requests import AnalyzeRequest
from unfret.visitors import DailyLimit

# ── Scrubbing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello, Floris", "Hello, [name]"),
        ("Hallo Jan de Vries\nWol", "Hallo [name]\nWol"),
        ("Welkom terug, Anna!", "Welkom terug, [name]!"),
        ("Hello, sign in", "Hello, sign in"),
        ("Deliver to Floris - Amsterdam 1012 AB\nPrice", "Deliver to [address]\nPrice"),
        ("Bezorgen aan: Kerkstraat 1, Utrecht", "Bezorgen aan: [address]"),
        ("mail me at a.b+c@mail.example.com", "mail me at [email]"),
        ("bel 06-12345678 of +31 20 123 4567", "bel [phone] of [phone]"),
        ("IBAN NL91 ABNA 0417 1643 00", "IBAN [iban]"),
        ("card 4111 1111 1111 1111", "card [card]"),
        ("Floris's Amazon", "[name]'s Amazon"),
        # Product data stays: EANs, prices, model numbers.
        ("EAN 8711253012345, € 1.299,00, model 1080-HD", "EAN 8711253012345, € 1.299,00, model 1080-HD"),
    ],
)
def test_scrub_text(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.Amazon.nl/x/dp/B0ABCDEFGH/ref=sr_1_1?keywords=a&qid=1#reviews",
            "https://www.amazon.nl/x/dp/B0ABCDEFGH",
        ),
        ("https://shop.test/p/sweater/?utm_source=x&sid=123", "https://shop.test/p/sweater"),
        ("javascript:alert(1)", ""),
        ("", ""),
    ],
)
def test_clean_url(url: str, expected: str) -> None:
    assert clean_url(url) == expected


def test_page_identity_prefers_the_public_product_id() -> None:
    def key(body: AnalyzeRequest) -> str:
        facts, text, url = page_of(body)
        return page_identity(body, facts, url, text)[0]

    amazon = PageFacts(platform="amazon")
    one = AnalyzeRequest(url="https://www.amazon.nl/a/dp/B0ABCDEFGH?tag=x", page_facts=amazon, page_content="a")
    two = AnalyzeRequest(url="https://amazon.nl/dp/B0ABCDEFGH/ref=x", page_facts=amazon, page_content="b")
    shop = AnalyzeRequest(url="https://shop.test/p/sweater?utm=1", page_content="<p>x</p>")
    same_shop = AnalyzeRequest(url="https://www.shop.test/p/sweater#top", page_content="<p>y</p>")

    assert key(one) == key(two) == "amazon.nl|asin:B0ABCDEFGH"
    assert key(shop) == key(same_shop)
    assert key(shop).startswith("shop.test|url:")


def test_raw_html_is_reduced_to_facts_and_text() -> None:
    html = "<html><nav>Hello, Floris</nav><form>secret</form><main><h1>Wool sweater</h1></main></html>"
    facts, text, _ = page_of(AnalyzeRequest(url="https://shop.test/p", page_content=html))
    assert "Wool sweater" in text
    assert "Floris" not in text
    assert "secret" not in text
    assert "<" not in text


# ── Report tokens ─────────────────────────────────────────────────────────────

BODY = AnalyzeRequest(url="https://shop.test/p", page_content="Wool sweater")


def test_report_tokens_bind_account_input_and_result() -> None:
    token = reports.issue_token("s", "acct", BODY, score=80, explanation="Fine.", ttl_s=60, now=1000)
    assert token is not None

    claim = reports.check_token("s", token, account="acct", body=BODY, explanation="Fine.", now=1050)
    assert claim.score == 80
    for kwargs in (
        {"account": "other"},
        {"body": AnalyzeRequest(url="https://shop.test/p", page_content="Other")},
        {"explanation": "Scam."},
        {"now": 1061},
    ):
        args = {"account": "acct", "body": BODY, "explanation": "Fine.", "now": 1050, **kwargs}
        with pytest.raises(reports.ReportError):
            reports.check_token("s", token, **args)  # type: ignore[arg-type]
    with pytest.raises(reports.ReportError):
        reports.check_token("other-secret", token, account="acct", body=BODY, explanation="Fine.", now=1050)


def test_no_token_without_a_score_or_a_secret() -> None:
    assert reports.issue_token("s", "a", BODY, score=-1, explanation="", ttl_s=60) is None
    assert reports.issue_token("", "a", BODY, score=50, explanation="", ttl_s=60) is None


@pytest.mark.parametrize(
    ("score", "label", "gap"),
    [(80, "scam", 40), (40, "junk", 0), (41, "junk", 1), (20, "legit", 50), (70, "legit", 0), (100, "legit", 0)],
)
def test_gap(score: int, label: reports.Label, gap: int) -> None:
    assert reports.gap(score, label) == gap


# ── API keys ──────────────────────────────────────────────────────────────────


def test_key_parsing() -> None:
    key = "ufk_abcdefghij_" + "a" * 52
    assert keys.parse(key) == ("abcdefghij", key)
    assert keys.key_id_from(key) == keys.key_id_from("abcdefghij") == keys.key_id_from("key:abcdefghij")
    for bad in ("ufk_ABCDEFGHIJ_" + "a" * 52, "ufr_abcdefghij_" + "a" * 52, "ufk_abcdefghij_short", "abc"):
        with pytest.raises(keys.ApiKeyError):
            keys.parse(bad)
    assert keys.is_key_account("key:abcdefghij")
    assert not keys.is_key_account("key:nope")
    assert not keys.is_key_account("So11111111111111111111111111111111111111112")


def guard(now: list[float]) -> KeyGuard:
    return KeyGuard(
        secret="s",
        bits=6,
        ttl_s=60,
        creations=DailyLimit(5, 100),
        recoveries=DailyLimit(5, 100),
        clock=lambda: now[0],
    )


def solve(challenge: str, bits: int) -> str:
    return next(str(n) for n in range(1_000_000) if keys.pow_ok(challenge, str(n), bits))


def test_proof_of_work_is_checked_once_and_expires() -> None:
    now = [1000.0]
    g = guard(now)
    challenge, bits, _ = g.challenge()
    solution = solve(challenge, bits)

    assert g.redeem(challenge, solution)
    assert not g.redeem(challenge, solution)  # used
    late, bits, _ = g.challenge()
    now[0] += 61
    assert not g.redeem(late, solve(late, bits))  # expired
    easier = challenge.replace(".6.", ".1.")
    assert not g.redeem(easier, solve(easier, 1))  # the difficulty is under the HMAC


def test_past_the_daily_cap_keys_get_harder_to_make_not_impossible() -> None:
    # A hard daily cap let anyone with a few hundred addresses (one IPv6 /56) stop every new pass for the day: the
    # card checkout makes its pass first. Past the cap, the proof of work grows instead.
    now = [1000.0]
    g = KeyGuard(secret="s", bits=6, ttl_s=60, creations=DailyLimit(1, 4), recoveries=DailyLimit(5, 100),
                 clock=lambda: now[0])  # fmt: skip
    for n in range(4):
        assert g.creations.allow(f"10.0.0.{n}", daily_cap=False)  # as POST /v1/keys counts them
    assert g.challenge()[1] == 7  # at the cap: one bit more
    for n in range(4, 8):
        assert g.creations.allow(f"10.0.0.{n}", daily_cap=False)  # still allowed, one per network
    assert g.challenge()[1] == 8  # twice the cap: two bits more
    assert not g.creations.allow("10.0.0.1", daily_cap=False)  # the per-network limit stays


def test_recovery_attempts_have_no_global_cap() -> None:
    # Backup codes are 256 random bits: only the per-network limit matters, and a global one could be used up by a
    # stranger to keep everyone from recovering (or picking up a card purchase) for the day.
    from pydantic import SecretStr  # noqa: PLC0415

    from unfret.config import Settings  # noqa: PLC0415
    from unfret.credits import CreditsDoor  # noqa: PLC0415
    from unfret.x402.solana import SolanaRpc  # noqa: PLC0415

    door = CreditsDoor(
        Settings(session_secret=SecretStr("s"), key_create_daily_cap=2), SolanaRpc("http://rpc.invalid"), None
    )
    assert all(door.key_guard.recoveries.allow(f"10.0.{n // 250}.{n % 250}") for n in range(1000))
