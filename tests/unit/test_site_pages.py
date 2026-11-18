"""About, why, money, press, terms and the shop owners' page: strict CSP, nothing from elsewhere, true to the code."""

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml

import unfret
from unfret import market_prices, objections, scoring
from unfret.config import settings
from unfret.main import app
from unfret.routes.pages import SITEMAP_PATHS, STATIC_DIR, asset_tag
from unfret.x402.verdict import QUICK_SKILLS, tier_description

CONFIG = Path(__file__).parents[2] / "config"
PAGES = {"/about": "about.html", "/why": "why.html", "/money": "money.html", "/press": "press.html",
         "/terms": "terms.html", "/objection": "objection.html"}  # fmt: skip


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


async def test_the_privacy_statement_names_the_controller_with_the_terms_address(client: httpx.AsyncClient) -> None:
    privacy = " ".join((await client.get("/privacy")).text.split())
    terms = " ".join((await client.get("/terms")).text.split())
    address = "Kopenhagenstraat 2a, 9723 BB Groningen"
    assert f"Unfret is made by Kroshtan (Floris van Beers, {address}, the Netherlands)" in privacy
    assert "(the controller, in the words of the GDPR)" in privacy and address in terms


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
    assert 'class="placeholder"' not in draft  # the company details are filled in; the note points at none
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
    assert "<mark" not in section


def test_the_approved_terms_show_no_draft_note_on_dev_or_production() -> None:
    dev = yaml.safe_load((CONFIG / "deploy.dev.yml").read_text())["env"]["clear"]
    production = yaml.safe_load((CONFIG / "deploy.production.yml").read_text())["env"]["clear"]
    assert dev.get("UNFRET_TERMS_DRAFT", "false") == "false"
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
        "use the Unfret service, its API or its results to build, train or fill a competing service or dataset",
        "These rules are about the service at unfret.net and its results.",
        "Unfret's source code is open source under the MIT licence",
        "you may use it under that licence, including to run a service of your own.",
        "If you break these rules, we may block your pass",
        "free checks you aren't meant to have",
        "Dutch law applies",
        "hello@unfret.net",
    ):
        assert phrase in text, phrase
    assert "intro code" not in text and "gift code can't be used on the buyer's own pass" in text
    assert 'href="/affiliates"' in text and "[Affiliate terms" not in text
    # The company details as the owner gave them, with no placeholder left.
    assert 'class="placeholder"' not in text
    for detail in (
        "Unfret is run by Floris van Beers, trading as Kroshtan, in the Netherlands.",
        "Address: Kopenhagenstraat 2a, 9723 BB Groningen",
        "Chamber of Commerce (KvK): 42178948",
        "VAT: NL005344159B92",
        "Disputes go to the Rechtbank Noord-Nederland (Groningen), the Netherlands.",
        "you keep the protection of its mandatory law and can also go to a court there",
    ):
        assert detail in text, detail


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


# Comparative claims must be objective and verifiable: kinds of tools, never a named one (Fakespot, closed, aside).
_NAMED_TOOLS = re.compile(
    r"scam ?advis|trustpilot|scamdoc|scam detector|norton|mcafee|chatgpt|openai|gemini|copilot|perplexity|claude"
    r"|anthropic|\bgrok\b|mistral|honey|keepa|camelcamel|reviewmeta|trustedshops|sitejabber",
    re.IGNORECASE,
)


async def test_why_compares_with_kinds_of_tools_fairly(client: httpx.AsyncClient) -> None:
    page = (await client.get("/why")).text
    text = " ".join(re.sub(r"<[^>]+>", " ", page.split("<main", 1)[1].split("</main>", 1)[0]).split())

    assert not _NAMED_TOOLS.search(text), _NAMED_TOOLS.search(text)
    # The other side as "many" and "often", and what it does well, not as absolutes.
    assert "Many free website checkers are quick and useful" in text
    assert "a capable assistant can do much of a careful check" in text and "good second opinion" in text
    others = re.findall(r'<section class="compare-col" [^>]*>(.*?)</section>', page, re.DOTALL)
    assert len(others) == 2
    for column in others:
        assert not re.search(r"\b(?:never|always|all|none|can't|cannot|every)\b", column, re.IGNORECASE), column
    # Fakespot only as the closed service, with Mozilla's own announcement.
    assert "shut down on July 1, 2025" in text
    assert 'href="https://blog.mozilla.org/en/mozilla/building-whats-next/" rel="noopener"' in page
    # Each Unfret claim leads to where it can be checked.
    for path in ("/accuracy", "/money", "/privacy"):
        assert f'href="{path}"' in page, path
    assert "never calls a shop a scam" in text


async def test_why_names_unfrets_sources_as_results_do(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/why")).text.split())
    sources = json.loads((Path(unfret.__file__).parent / "copy" / "en.json").read_text())["sources"]
    for key in ("rdap", "vies", "price_history"):
        assert sources[key].lower() in text.lower(), sources[key]
    # The records it adds, as kept (privacy statement) and for as long as the stores keep them.
    assert f"over the last {market_prices.WINDOW_DAYS} days" in text
    assert market_prices.RETENTION_DAYS >= market_prices.WINDOW_DAYS
    assert "Safety Gate" in text and "shops that share an analytics ID, a payment account or contact details" in text
    # The scorer is deterministic: the same signals, the same verdict.
    assert "deterministic (same features, same score)" in (scoring.__doc__ or "")
    assert "The same signals always give the same verdict" in text


async def test_why_says_what_the_free_tier_covers_as_the_quick_tier_runs(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/why")).text.split())

    assert f"up to {settings.quick_checks_per_minute} a minute" in text
    assert f"comes with {settings.free_checks_per_pass} free deep checks, once per browser" in text
    assert "while the day's free checks last" in text  # the daily budget (FREE_CHECKS_PAUSED)
    assert 'href="/#price-heading"' in text and "no subscription" in text and "checks never expire" in text
    # What a quick check reads is what the quick tier runs; what it leaves to a deep check, the quick tier skips.
    for skill in ("listing", "dark_patterns", "shop_policies", "shop_links", "us_identity"):
        assert skill in QUICK_SKILLS, skill
    for skill in ("reviews", "domain_age", "reverse_image", "content"):
        assert skill not in QUICK_SKILLS, skill
    assert "No AI read of the page" in tier_description("quick")
    assert "with no AI and no lookups elsewhere" in text


async def test_why_says_what_is_kept_as_the_privacy_statement_does(client: httpx.AsyncClient) -> None:
    why = " ".join((await client.get("/why")).text.split())
    privacy = " ".join((await client.get("/privacy")).text.split())

    assert "Unfret remembers shops, not what you shop for" in why
    assert "the pages you check aren't stored, and they're never linked to you" in why
    assert (
        "Not the pages you check." in privacy and "aren't stored, on their own or with your wallet or pass" in privacy
    )
    # What it does keep about a pass, named as the statement names it.
    assert "a public ID, how many checks it has left and the day it was made" in why
    assert "the pass's public ID, its balance, its free checks left, the day it was made" in privacy
    # No IP addresses: no request log at Caddy or the proxy, limits as salted hashes in memory; Cloudflare's own logs.
    assert "No account, no IP addresses and no history of what you check" in why
    assert "Our server keeps no request log" in why and "Cloudflare, in front of the site, keeps its own logs" in why
    assert "Our server keeps <strong>no request log</strong>" in privacy
    assert "as a salted one-way hash, in memory only" in privacy and "Cloudflare keeps its own logs" in privacy


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


# ── /sitemap.xml ─────────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_sitemap_lists_the_public_pages_at_this_servers_address(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_base_url", "https://dev.example.test/")
    response = await client.get("/sitemap.xml")

    assert response.status_code == 200 and response.headers["content-type"].startswith("application/xml")
    assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns=')
    listed = re.findall(r"<loc>([^<]*)</loc>", response.text)
    assert listed == [f"https://dev.example.test{path}" for path in SITEMAP_PATHS]
    for path in ("/", "/nl", "/check", "/why", "/about", "/accuracy", "/developers", "/extension", "/money",
                 "/press", "/privacy", "/terms", "/objection", "/affiliates", "/connect"):  # fmt: skip
        assert path in SITEMAP_PATHS, path
    for path in ("/android", "/affiliates/stats", "/welcome", "/checkout", "/my-checks", "/test-shop"):
        assert path not in SITEMAP_PATHS, path


@pytest.mark.parametrize("path", SITEMAP_PATHS)
async def test_every_page_in_the_sitemap_can_be_indexed(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert "noindex" not in response.headers.get("x-robots-tag", "")
    assert re.search(r'<meta name="robots" content="[^"]*noindex', response.text) is None
