"""About, money, press, terms and the shop owners' page: strict CSP, nothing from elsewhere, true to the code."""

import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml

from unfret import objections
from unfret.config import settings
from unfret.main import app
from unfret.routes.pages import STATIC_DIR, asset_tag

CONFIG = Path(__file__).parents[2] / "config"
PAGES = {"/about": "about.html", "/money": "money.html", "/press": "press.html", "/terms": "terms.html",
         "/objection": "objection.html"}  # fmt: skip


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("path", PAGES)
async def test_the_pages_have_a_strict_policy_and_the_shared_frame(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "form-action 'none'", "frame-ancestors 'none'"):
        assert directive in csp, directive
    page = response.text
    assert "{{" not in page and "}}" not in page
    assert page.count('<header class="site-header">') == 1 and page.count('<footer class="site-footer">') == 1
    nav = page.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-current="page"' not in nav  # in the footer, not the menu


@pytest.mark.parametrize("path", PAGES)
async def test_the_pages_load_nothing_from_elsewhere(client: httpx.AsyncClient, path: str) -> None:
    page = (await client.get(path)).text

    # Scripts, styles and images come from this build's folder; outside addresses are only links to follow.
    for tag in re.findall(r"<(?:script|img|link)\b[^>]*>", page):
        address = re.search(r'\b(?:src|href)="([^"]*)"', tag)
        assert address and address.group(1).startswith(f"/static/{asset_tag()}/"), tag
    assert "<style" not in page and " style=" not in page and "<iframe" not in page
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page) is None  # no inline scripts


def test_the_shop_owners_script_only_talks_to_this_api_and_keeps_nothing() -> None:
    script = (STATIC_DIR / "objection.js").read_text()
    assert re.findall(r'fetch\("([^"]+)"', script) == ["/v1/objections"]
    assert "localStorage" not in script and "sessionStorage" not in script and "cookie" not in script
    assert "https://" not in script and "http://" not in script


def test_the_footer_leads_to_every_page() -> None:
    footer = (STATIC_DIR / "partials" / "footer.html").read_text()
    for path in (*PAGES, "/privacy", "/developers"):
        assert f'href="{path}"' in footer, path


@pytest.mark.parametrize("name", ["check.html", "shared.html"])
def test_results_lead_shop_owners_to_the_objection_page(name: str) -> None:
    page = (STATIC_DIR / name).read_text()
    result = page.split('id="result"', 1)[1] if name == "check.html" else page.split('id="object"', 1)[1]
    assert 'href="/objection"' in result


async def test_the_shop_owners_page_has_the_form_and_says_what_is_kept(client: httpx.AsyncClient) -> None:
    page = (await client.get("/objection")).text
    assert f'<script type="module" src="/static/{asset_tag()}/objection.js"></script>' in page
    for field in ('name="kind" value="correction"', 'name="kind" value="removal"', 'name="kind" value="other"',
                  'name="site"', 'name="message"', 'name="reply_to"'):  # fmt: skip
        assert field in page, field
    assert f'maxlength="{objections.MAX_SITE_CHARS}"' in page and f'maxlength="{objections.MAX_MESSAGE_CHARS}"' in page
    text = " ".join(page.split())
    assert "no IP address" in text and f"after {settings.objection_days} days at the latest" in text
    assert "never calls a shop a scam" in text and "mailto:hello@unfret.net" in page
    # The retention it names is the stores' own.
    assert "Kept 400 days after the last check" in text and "Kept 180 days after the last check" in text


def test_the_retention_on_the_shop_owners_page_matches_the_store() -> None:
    from unfret import shop_store  # noqa: PLC0415 - only here

    assert shop_store.RETENTION_DAYS == 400 and shop_store.CONTACT_RETENTION_DAYS == 180


async def test_the_privacy_statement_names_the_shop_owners_form(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/privacy")).text.split())
    assert "Requests from shop owners" in text and 'href="/objection"' in text


async def test_the_terms_are_marked_as_a_draft_only_while_the_setting_is_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "terms_draft", True)
    draft = (await client.get("/terms")).text
    monkeypatch.setattr(settings, "terms_draft", False)
    final = (await client.get("/terms")).text

    assert "Draft, not in force." in draft and 'class="panel notice draft-note"' in draft
    assert draft.index("draft-note") < draft.index("<h1>")  # at the top
    assert "Draft, not in force." not in final and "draft-note" not in final


async def test_the_terms_spell_out_the_affiliate_program_from_the_settings(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/terms")).text.split())
    section = text[text.index("<h2>7. Affiliates</h2>") : text.index("<h2>8.")]
    assert "{{" not in section
    minimum = settings.affiliate_min_payout_cents // 100
    for phrase in (
        'apply by writing to <a href="mailto:hello@unfret.net">hello@unfret.net</a>',
        f"{settings.affiliate_bonus_pct}% more checks on their first pack",
        f"earns {settings.affiliate_commission_pct}% of the net revenue",
        f"for {settings.affiliate_months} months after the pass was attributed",
        "after VAT and the payment provider's fees",
        f"payable {settings.affiliate_hold_days} days after the purchase",
        "monthly, by hand",
        f"${minimum} or €{minimum} is payable in a currency",
        "never converted",
        "A refund or a chargeback reverses the commission",
        "comes off your next payouts",
        "everything earned is paid out",
        "FTC",
        "ACM",
        "Reclame Code",
        "misleading claims",
        "calling a shop a scam",
        "spam",
        "bid on Unfret's name",
        "self-referral rings",
        "end the affiliate program",
    ):
        assert phrase in section, phrase
    assert "<mark" not in section  # the affiliate terms are written; only the company details wait for the owner


def test_dev_shows_the_terms_as_a_draft_and_production_doesnt() -> None:
    dev = yaml.safe_load((CONFIG / "deploy.dev.yml").read_text())["env"]["clear"]
    production = yaml.safe_load((CONFIG / "deploy.production.yml").read_text())["env"]["clear"]
    assert dev["UNFRET_TERMS_DRAFT"] == "true"
    assert production.get("UNFRET_TERMS_DRAFT", "false") == "false"


async def test_the_terms_cover_what_was_asked(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/terms")).text.split())
    for phrase in (
        "Unfret has no accounts",
        "merchant of record",
        "Polar's refund rules apply",
        "Crypto purchases are final and not refundable",
        "Unfret never calls a shop a scam",
        "not a statement of fact",
        "competing service or dataset",
        "free checks you aren't meant to have",
        "Dutch law applies",
        "hello@unfret.net",
    ):
        assert phrase in text, phrase
    # Company details are left for the owner to fill in, never made up.
    assert "intro code" not in text and "gift code can't be used on the buyer's own pass" in text
    assert 'href="/affiliates"' in text and "[Affiliate terms" not in text
    for placeholder in ("[business address]", "[KvK number]", "[VAT number]", "[court]"):
        assert f'<mark class="placeholder">{placeholder}</mark>' in text, placeholder


async def test_money_is_paid_checks_and_nothing_else(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/money")).text.split())
    for phrase in ("No ads", "No affiliate links to shops; Unfret never earns from shops", "No selling data",
                   "No money from shops"):  # fmt: skip
        assert phrase in text, phrase
    assert "paid for by the checks people buy" in text and "x402" in text
    # Promoters may earn a share of the sales they bring in (the affiliate program); shops never pay.
    assert 'href="/affiliates"' in text
    # Packs come with gift codes for friends; the operator's intro codes are being retired.
    assert "gift codes" in text and "intro code" not in text.lower()


async def test_about_says_who_and_where_without_made_up_numbers(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/about")).text.split())
    assert "Floris van Beers" in text and "one-person company in the Netherlands" in text
    assert "Scaleway" in text and "MIT licence" in text and 'href="/money"' in text
    assert not re.search(r"\b\d[\d,.]*\s*(?:users|shoppers|checks a|installs|customers)\b", text)


async def test_the_press_page_offers_the_logo_and_test_shop_screenshots(client: httpx.AsyncClient) -> None:
    page = (await client.get("/press")).text
    tag = asset_tag()
    for logo in ("icon.svg", "icon-512.png", "icon-maskable-512.png"):
        assert f'href="/static/{tag}/icons/{logo}"' in page, logo
    shots = re.findall(rf'<img src="/static/{tag}/press/([^"]+)"', page)
    assert shots, "no screenshots"
    for shot in shots:
        file = STATIC_DIR / "press" / shot
        assert file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", shot
        assert file.stat().st_size < 600_000, shot  # small enough for a phone
        served = await client.get(f"/static/{tag}/press/{shot}")
        assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert 'href="/test-shop"' in page and "mailto:hello@unfret.net" in page
    assert f'href="{settings.chrome_store_url}"' in page and f'href="{settings.firefox_store_url}"' in page


@pytest.mark.parametrize(
    ("typed", "site"),
    [
        ("https://www.Shop.example/p/1/?ref=x#top", "shop.example/p/1"),
        ("shop.example", "shop.example"),
        ("shop.example/", "shop.example"),
        ("http://sub.shop.example/a b", "sub.shop.example/a b"),
    ],
)
def test_a_site_is_kept_without_query_or_fragment(typed: str, site: str) -> None:
    assert objections.site_of(typed) == site


@pytest.mark.parametrize(
    "typed", ["", "shop", "ftp://shop.example", "https://user@/x", "http://[::1]/", "a b.example"]
)
def test_what_isnt_a_site_is_refused(typed: str) -> None:
    with pytest.raises(objections.ObjectionError) as refused:
        objections.site_of(typed)
    assert refused.value.code == "BAD_SITE"


def test_a_reply_address_is_optional_but_must_be_one() -> None:
    assert objections.reply_address("  ") is None
    assert objections.reply_address(" owner@shop.example ") == "owner@shop.example"
    for wrong in ("owner", "owner@shop", "<a@b.c>", "a b@shop.example", "x" * 250 + "@shop.example"):
        with pytest.raises(objections.ObjectionError):
            objections.reply_address(wrong)


async def test_without_a_database_the_form_points_to_email(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/objections", json={"kind": "other", "site": "shop.example"})
    assert response.status_code == 503 and "hello@unfret.net" in response.json()["detail"]
