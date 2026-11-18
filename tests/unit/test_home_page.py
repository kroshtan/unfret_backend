"""The home page and its Dutch counterpart (/nl): what they say, in which order, and how they load."""

import json
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.unit.test_test_shop import DEEP_VERDICT, QUICK_VERDICT
from unfret.card import REASON_SOURCES, source_label
from unfret.config import settings
from unfret.main import app
from unfret.prices import format_usd
from unfret.routes.pages import STATIC_DIR, asset_tag

CATALOGUE = json.loads((STATIC_DIR.parent / "copy" / "en.json").read_text(encoding="utf-8"))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── Both pages: strict policy, nothing from elsewhere, this build's assets, each other's language ───────────────


@pytest.mark.parametrize("path", ["/", "/nl"])
async def test_the_page_has_a_strict_policy_and_no_inline_code(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    for part in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'"):
        assert part in csp, part
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    page = response.text
    assert "{{" not in page and "}}" not in page
    # Every script is a file of this site: no inline code, no event-handler attributes, no inline styles.
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts and all(re.search(r'\bsrc="/static/', tag) for tag in scripts), scripts
    assert not re.search(r"<script\b[^>]*>(?!</script>)", page)
    assert not re.search(r"\son[a-z]+=", page) and "style=" not in page


@pytest.mark.parametrize("path", ["/", "/nl"])
async def test_the_page_loads_nothing_from_elsewhere(client: httpx.AsyncClient, path: str) -> None:
    page = (await client.get(path)).text
    # Whatever the page loads (scripts, styles, icons, images): from here. Links elsewhere are navigations only.
    loaded = re.findall(r'<(?:script|img|source|iframe)\b[^>]*\bsrc="([^"]+)"', page)
    loaded += re.findall(
        r'<link\b(?=[^>]*\brel="(?:stylesheet|icon|preload|modulepreload)")[^>]*\bhref="([^"]+)"', page
    )
    assert loaded
    assert all(url.startswith("/static/") for url in loaded), loaded
    assert "@import" not in page and "url(http" not in page


@pytest.mark.parametrize(("path", "script"), [("/", "home.js"), ("/nl", "nl.js")])
async def test_the_page_names_its_assets_under_the_build(client: httpx.AsyncClient, path: str, script: str) -> None:
    page = (await client.get(path)).text
    tag = asset_tag()
    assert f'<script type="module" src="/static/{tag}/{script}"></script>' in page
    assert f'<script type="module" src="/static/{tag}/ref.js"></script>' in page  # an affiliate's ?ref= is kept
    assert f'href="/static/{tag}/site.css"' in page
    assert '"/static/site.css"' not in page and f'"/static/{script}"' not in page
    for name in (script, "start-check.js", "check-core.js", "prices.js"):
        assert (await client.get(f"/static/{tag}/{name}")).status_code == 200, name


@pytest.mark.parametrize("path", ["/", "/nl"])
async def test_the_home_page_and_the_dutch_page_name_each_other(client: httpx.AsyncClient, path: str) -> None:
    page = (await client.get(path)).text
    base = settings.public_base_url.rstrip("/")
    head = page.split("</head>", 1)[0]
    alternates = dict(re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)">', head))
    assert alternates == {"en": f"{base}/", "nl": f"{base}/nl", "x-default": f"{base}/"}


# ── The home page, in the order it was asked for ──────────────────────────────────────────────────────────────────


async def test_the_home_page_goes_from_the_question_to_the_developers(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    assert re.search(r"<h1>Check before <span class=\"soft\">you pay\.</span></h1>", page)
    hero = page.split('<section class="hero', 1)[1].split("</section>", 1)[0]
    assert 'id="start-check"' in hero and 'class="sample"' in hero  # the field and the test shop's result, on top
    order = [
        page.index("<h1>"),
        page.index('id="start-check"'),
        page.index('class="sample"'),
        page.index('class="proof'),
        page.index('id="tricks"'),
        page.index('aria-labelledby="price-heading"'),
        page.index('aria-labelledby="faq-heading"'),
        page.index('class="dev-line'),
    ]
    assert order == sorted(order)
    tail = page[page.index('class="dev-line') : page.index("</main>")]
    assert tail.count("<a ") == 1 and 'href="/developers"' in tail  # the last thing: one line to /developers


async def test_the_link_field_checks_on_the_link_checker_with_free_checks(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    form = page.split('id="start-check"', 1)[1].split("</form>", 1)[0]
    assert re.search(r'<input id="start-link" type="text" inputmode="url"', form)
    assert '<label class="visually-hidden" for="start-link">' in page
    assert 'type="submit">Check</button>' in form and "data-error" in form
    assert "deep checks are free, then quick checks stay free" in _flat(form) and "No key, no account" in form
    assert 'data-example="/test-shop"' in form
    home = (STATIC_DIR / "home.js").read_text()
    assert "wireStartForm(" in home
    # The link reaches /check through this tab's session storage, and /check starts the check at once.
    check = (STATIC_DIR / "check.js").read_text()
    assert "takeHandedLink()" in check and "requestSubmit" in check


async def test_the_test_shop_result_is_the_real_one(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    sample = page.split('class="sample"', 1)[1].split("</figure>", 1)[0]
    band, reasons = DEEP_VERDICT
    assert (band, reasons) == QUICK_VERDICT  # free (quick) or deep, the test shop gets the same answer
    assert f'<div class="verdict {band}">' in sample
    assert f'<p class="label">{CATALOGUE["labels"][band]}</p>' in sample
    cards = re.findall(
        r'<li class="card high"><span class="title">(.*?)</span><p class="detail">(.*?)</p>'
        r'<p class="source">Source: (.*?)</p></li>',
        _flat(sample),
    )
    assert [title for title, _, _ in cards] == [CATALOGUE["reasons"][r]["title"] for r in reasons]
    # Where each finding comes from, as the result card says it (card.py).
    assert [source for _, _, source in cards] == [source_label(REASON_SOURCES[r]) for r in reasons]
    for (_, detail, _), reason in zip(cards, reasons, strict=True):
        # The catalogue's words, with the test shop's own details where the catalogue has a placeholder.
        pattern = re.escape(CATALOGUE["reasons"][reason]["detail"])
        pattern = re.sub(r"\\\{\w+\\\}", ".+", pattern)
        assert re.fullmatch(pattern, detail.replace("&#x27;", "'")), (reason, detail)
    assert 'href="/test-shop"' in sample and "made-up shop" in sample
    assert "<img" not in sample  # drawn from the result, so it follows light and dark and the phone's width


async def test_the_proof_strip_says_what_unfret_stands_for(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    strip = page.split('class="proof', 1)[1].split("</ul>", 1)[0]
    items = [re.sub(r"<[^>]+>", "", item).strip() for item in re.findall(r"<li>(.*?)</li>", strip)]
    assert items == [
        "Open source",
        "AI in the EU, at Scaleway",
        "Reads only when you click",
        "No account",
        "No ads or affiliate links to shops",
    ]


async def test_the_home_page_keeps_what_it_had(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    # The main button for the visitor's browser, and the link checker (first on a phone: install-button.js).
    assert page.count('data-get="main"') == 1 and page.count('data-get="link"') == 1
    assert f'href="{settings.chrome_store_url}"' in page and f'href="{settings.firefox_store_url}"' in page
    home = (STATIC_DIR / "home.js").read_text()
    assert "chooseButtons()" in home
    assert 'euros ? "€0" : "$0"' in home  # the free tier in the packs' currency
    # The honest note about the big shops that only the extension and the app can read.
    assert "turn automatic checks away" in page and "Android" in page
    # The patterns Unfret catches (#159), the free tier first, and ads and tradespeople lower down.
    tricks = page[page.index('id="tricks"') : page.index("</section>", page.index('id="tricks"'))]
    assert len(re.findall(r'<li class="pattern">', tricks)) >= 8
    assert page.index('aria-labelledby="price-heading"') < page.index('aria-labelledby="beyond-heading"')
    assert page.index('id="tricks"') < page.index('aria-labelledby="beyond-heading"')


async def test_agents_have_their_own_way_in_near_the_top(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    agents = page.split('id="agents"', 1)[1].split("</section>", 1)[0]
    assert "x402" in agents and "/v1/check" in agents and "unfret-mcp" in agents
    assert 'href="/developers">Developer guide</a>' in agents
    assert format_usd(settings.x402_price_quick_usd_micros) in agents
    assert format_usd(settings.x402_price_deep_usd_micros) in agents
    assert f"{settings.public_base_url.rstrip('/')}/v1/check" in agents
    assert page.index('id="agents"') < page.index('id="tricks"')  # before the shopper's details, not a footnote


async def test_the_questions_are_answered_honestly(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    faq = _flat(page.split('class="faq"', 1)[1].split("</section>", 1)[0])
    questions = re.findall(r"<summary>(.*?)</summary>", faq)
    assert len(questions) >= 5
    assert "never calls a shop a scam" in faq and 'href="/accuracy"' in faq
    assert 'href="/privacy"' in faq and 'href="/money"' in faq
    assert "asks for your email for the receipt" in faq  # card payments need an e-mail; crypto doesn't
    # The AI assistant question: fair to assistants, and the comparison on /why.
    assert "Why not just ask an AI assistant?" in questions and 'href="/why"' in faq
    assert "good second opinion" in faq


# ── /nl: one Dutch page ──────────────────────────────────────────────────────────────────────────────────────────


async def test_the_dutch_page_is_dutch_and_sends_the_rest_to_the_english_pages(client: httpx.AsyncClient) -> None:
    response = await client.get("/nl")
    assert response.status_code == 200
    page = response.text
    assert page.startswith('<!doctype html>\n<html lang="nl">')
    assert '<h1>Check de verkoper <span class="soft">voordat je betaalt.</span></h1>' in page
    text = _flat(page)
    assert "iDEAL | Wero" in text and "Marktplaats" in text
    assert "uitgebreide checks zijn gratis" in text and "snelle checks gratis" in text
    assert "Unfret antwoordt in het Engels" in text  # results come in English: said up front
    # The shared header stays English, and says so; the footer is Dutch, with the English footer's links.
    assert '<header class="site-header" lang="en">' in page
    footer = page.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
    assert "<h2>Unfret gebruiken</h2>" in footer and "Gemaakt in Nederland door" in footer
    english = (await client.get("/")).text.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
    assert re.findall(r'href="([^"]*)"', footer) == re.findall(r'href="([^"]*)"', english)
    # The English pages for the rest, marked as English; no translated legal pages.
    main = page.split("<main>", 1)[1].split("</main>", 1)[0]
    for path in ("/", "/privacy", "/terms", "/money", "/checkout?checks=25", "/extension"):
        assert f'href="{path}" hreflang="en"' in main, path
    for path in ("/nl/terms", "/nl/privacy", "/nl/check"):
        assert (await client.get(path)).status_code == 404
    assert 'id="start-check"' in main and 'data-empty="Plak eerst een link."' in main


async def test_the_dutch_page_shows_euro_prices_with_dutch_vat(client: httpx.AsyncClient) -> None:
    page = (await client.get("/nl")).text
    prices = page.split('id="prices"', 1)[1].split("</ul>", 1)[0]
    assert prices.index('class="free"') < prices.index('data-checks="25"') < prices.index('data-checks="100"')
    assert "€ 6,66" in prices and "€ 21,18" in prices  # €5.50 and €17.50 with 21% VAT, as the defaults say
    script = (STATIC_DIR / "nl.js").read_text()
    assert '"/v1/pricing"' in script and 'cardPrice(body, pack, "eur", "NL")' in script


# ── start-check.js: the hand-off to /check ───────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("node")
def test_a_link_is_handed_to_the_link_checker_once_and_briefly(tmp_path: Path) -> None:
    for name in ("start-check.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = f"""
import {{ handOff, takeHandedLink }} from {json.dumps((tmp_path / "start-check.js").as_uri())};
const store = () => {{ const m = new Map(); return {{
  getItem: (k) => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k),
  size: () => m.size }}; }};
const out = [];
const a = store();
out.push(handOff("https://shop.example/item", a), takeHandedLink(a), takeHandedLink(a), a.size());
const stale = store();
stale.setItem("unfret-start-check", JSON.stringify({{ link: "https://shop.example/x", at: Date.now() - 61000 }}));
out.push(takeHandedLink(stale), stale.size());
const junk = store();
junk.setItem("unfret-start-check", JSON.stringify({{ link: "javascript:alert(1)", at: Date.now() }}));
out.push(takeHandedLink(junk));
const off = {{ getItem() {{ throw new Error("off"); }}, setItem() {{ throw new Error("off"); }}, removeItem() {{}} }};
out.push(handOff("https://shop.example/item", off), takeHandedLink(off), takeHandedLink(undefined));
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [True, "https://shop.example/item", "", 0, "", 0, "", False, "", ""]


def test_the_privacy_statement_names_the_handed_link() -> None:
    privacy = _flat((STATIC_DIR / "privacy.html").read_text())
    assert "a link you paste on the home page, for the moment it takes to open the link checker" in privacy


# ── Site feedback: words that mean what they say ─────────────────────────────────────────────────────────────────


async def test_the_hero_offers_the_link_checker_without_install_next_to_the_install_button(
    client: httpx.AsyncClient,
) -> None:
    hero = _flat((await client.get("/")).text.split('<section class="hero', 1)[1].split("</section>", 1)[0])
    assert "nothing to install to try it." in hero  # next to "Add to Chrome": installing is the other way in
    assert "nothing to install." not in hero


async def test_the_questions_say_what_a_pass_is_and_that_it_does_not_expire(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    faq = _flat(page.split('class="faq"', 1)[1].split("</section>", 1)[0])
    # Where "pass" first shows up on the page, so "My checks" in the menu doesn't read like an account.
    main = _flat(re.sub(r"<[^>]+>", " ", page[page.index("<main>") :]))
    first = re.search(r"\bpass\b", main)
    assert first and "Your pass is a code saved in your browser." in main[first.start() : first.start() + 200]
    assert "Your pass is a code saved in your browser. It holds your checks. No name, no email." in faq
    assert "Do passes or checks expire?" in faq
    assert "don't expire" in faq and "doesn't delete a pass for not being used" in faq
    assert "refuses to touch passes that bought checks" in faq  # what retire-passes does by default


@pytest.mark.parametrize("path", ["/", "/nl"])
async def test_the_prices_read_as_separate_words(client: httpx.AsyncClient, path: str) -> None:
    page = (await client.get(path)).text
    prices = page.split('id="prices"', 1)[1].split("</ul>", 1)[0]
    assert "</span><span" not in prices  # a screen reader or an agent reads "Free 5 deep checks", not "Free5 deep"
    text = " ".join(re.sub(r"<[^>]+>", "", prices).split())
    assert ("Free 5 deep checks" in text and "$0 Start" in text) or ("Gratis 5 uitgebreide" in text)


async def test_the_prices_say_what_they_include(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    pricing = _flat(page.split('class="pricing"', 1)[1].split('class="code-box"', 1)[0])
    note = pricing.split('id="price-note">', 1)[1].split("</p>", 1)[0]
    # The defaults in the page are the prices before tax (home.js puts in the VAT once it knows the country).
    assert "These card prices are before tax. In the EU, card prices include VAT" in note
    assert "sales tax is added at checkout where it applies" in note
    assert "the crypto price is plus VAT where it applies" in pricing
    prices = (STATIC_DIR / "prices.js").read_text()
    assert "`Card prices include ${pct}% VAT for ${countryPhrase(country)}.`" in prices


async def test_the_test_shops_discount_is_the_same_everywhere(client: httpx.AsyncClient) -> None:
    shop = (STATIC_DIR / "test-shop" / "product.html").read_text()
    now = re.search(r'<span class="price-now">\$([\d.]+)</span>', shop)
    was = re.search(r"Compare at <s>\$([\d.]+)</s>", shop)
    assert now and was
    pct = round(100 * (1 - float(now.group(1)) / float(was.group(1))))
    assert f"({pct}%)" in shop
    page = (await client.get("/")).text
    assert f"{pct}% off" in page.split('class="sample"', 1)[1].split("</figure>", 1)[0]  # the real result
    mock = re.search(r'<span class="mock-price">(.*?)</span>', page)
    assert mock and mock.group(1) == f"<s>${was.group(1)}</s> <b>${now.group(1)}</b> <i>−{pct}%</i>"


@pytest.mark.parametrize("name", ["footer.html", "footer-nl.html"])
def test_the_footer_has_the_contact_address_and_the_scope_the_hero_has(name: str) -> None:
    footer = _flat((STATIC_DIR / "partials" / name).read_text())
    assert '<a href="mailto:hello@unfret.net">hello@unfret.net</a>' in footer
    assert "company's website" not in footer and "website van een bedrijf" not in footer
    if name == "footer.html":
        assert "a product on a marketplace or a web shop, a second-hand ad, or the website of a tradesperson or" in (
            footer
        )
    else:
        assert "tweedehands advertentie, of de website van een vakman of lokale dienstverlener" in footer


async def test_the_scope_is_the_same_wherever_it_is_listed(client: httpx.AsyncClient) -> None:
    for path in ("/", "/check", "/checkout", "/welcome", "/welcome?free=5", "/press", "/about", "/why"):
        page = _flat((await client.get(path)).text)
        assert "company's website" not in page and "companies' websites" not in page, path
        assert "local service" in page, path
    assert "companies' websites" not in (STATIC_DIR / "check.js").read_text()
