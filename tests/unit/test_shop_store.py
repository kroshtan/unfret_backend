"""Shop fingerprints: what links shops (unique IDs only, one hop), what is stored, and what never is."""

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from unfret import fact_cache, shop_store
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.fingerprint import contacts, fingerprint, hamming, link_ids, skeleton_hash
from unfret.graph import _recording
from unfret.graph.skills.shop_links import ShopLinksSkill
from unfret.main import app
from unfret.shop_store import MemoryShopStore, ShopRecord

TODAY = date(2026, 11, 9)
PIXEL = "1234509876543"

SHOP_HTML = f"""<html><head>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-7QX2KL9ZPM"></script>
<script>fbq('init', '{PIXEL}'); ttq.load('C4ABCD1234EFGH5678IJ');</script>
<script src="https://cdn.shopify.com/s/files/hurrify.js"></script>
<script>Shopify.shop = "glow-lamp-store.myshopify.com";</script>
</head><body>
<a href="mailto:Support@GlowLamp.shop">Mail us</a> <a href="tel:+31 (0)20-123 4567">Call</a>
<p>Only €19.99, was €49.99. Order 20241109. Example: name@example.com</p>
<script>gtag('config', 'G-XXXXXXXXXX'); pk_live_51HxYzQ7mR2kT9vL4nP8sW3cJ6bF1gD5hK0aEqq</script>
</body></html>"""


def shop_state(
    html: str = SHOP_HTML, url: str = "https://www.glowlamp.shop/products/lamp?ref=abc", **extra: Any
) -> Any:
    return {
        "page_content": html,
        "url": url,
        "flags": [],
        "facts": PageFacts(platform="shopify"),
        "cleaned_content": "",
        "page_type": "storefront",
        **extra,
    }


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> MemoryShopStore:
    store = MemoryShopStore()
    shop_store.use_store(store)
    fact_cache.use_store(fact_cache.MemoryStore())
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("test-contact-key"))
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    return store


def test_link_ids_finds_account_ids_and_drops_placeholders() -> None:
    ids = link_ids(SHOP_HTML)
    assert ids["ga"] == {"G-7QX2KL9ZPM"}  # G-XXXXXXXXXX is a theme placeholder
    assert ids["meta_pixel"] == {PIXEL}
    assert ids["tiktok_pixel"] == {"C4ABCD1234EFGH5678IJ"}
    assert ids["myshopify"] == {"glow-lamp-store.myshopify.com"}
    assert ids["stripe"] == {"pk_live_51HxYzQ7mR2kT9vL4nP8sW3cJ6bF1gD5hK0aEqq"}


def test_a_page_with_many_ids_of_a_kind_is_an_agency_not_a_shop() -> None:
    many = " ".join(f"GTM-ABC{i:03d}Z" for i in range(6))
    assert "gtm" not in link_ids(many)
    assert link_ids("GTM-ABC001Z")["gtm"] == {"GTM-ABC001Z"}


def test_contacts_are_the_shops_own_and_prices_are_not_phone_numbers() -> None:
    emails, phones = contacts(SHOP_HTML)
    assert emails == {"support@glowlamp.shop"}  # example.com is documentation
    assert phones == {"+31201234567"}  # the price and order number aren't phones


def test_skeleton_hash_follows_the_template_not_the_text() -> None:
    def page(title: str, extra: str = "") -> str:
        items = "".join(f'<li class="menu__item"><a class="link">{title} {i}</a></li>' for i in range(30))
        return f'<html><body><header class="header"><ul class="menu">{items}</ul></header>{extra}</body></html>'

    same, other_text = skeleton_hash(page("Lamps")), skeleton_hash(page("Shoes"))
    other = skeleton_hash(
        "<html><body>" + '<div class="grid"><span class="tile">x</span></div>' * 40 + "</body></html>"
    )
    assert same is not None and other_text is not None and other is not None
    assert hamming(same, other_text) == 0
    assert hamming(same, other) > 10
    assert skeleton_hash("just text without markup") is None


async def test_observation_stores_only_shop_facts_and_hashed_contacts() -> None:
    await fact_cache._store.put("rdap", "glowlamp.shop", {"registered": "2026-10-01", "registrar": "NameCheap"}, 60)
    rec = await shop_store.observation(shop_state())
    assert rec is not None
    assert (rec.domain, rec.day, rec.registered, rec.registrar) == (
        "glowlamp.shop",
        TODAY,
        date(2026, 10, 1),
        "NameCheap",
    )
    assert "Hurrify countdown timer" in rec.apps
    assert all(len(v) == 64 for v in rec.links["email"] | rec.links["phone"])
    stored = repr(asdict(rec)).lower()
    for secret in ("support@glowlamp.shop", "glowlamp.shop/products", "ref=abc", "201234567", "lamp?"):
        assert secret not in stored


async def test_without_a_contact_key_no_contacts_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr(""))
    monkeypatch.setattr(settings, "session_secret", SecretStr(""))
    rec = await shop_store.observation(shop_state())
    assert rec is not None and "email" not in rec.links and "phone" not in rec.links


@pytest.mark.parametrize(
    "change",
    [
        {"page_type": "aggregate_listing"},
        {"facts": PageFacts(platform="amazon")},
        {"url": "https://localhost/p"},
        {"page_type": "article"},
    ],
)
async def test_only_web_shops_are_recorded(change: dict[str, Any]) -> None:
    assert await shop_store.observation(shop_state(**change)) is None


def record(domain: str, day: date = TODAY, registered: date | None = None, **links: set[str]) -> ShopRecord:
    return ShopRecord(domain=domain, day=day, registered=registered, links=links)


async def test_links_are_one_hop_never_transitive(fresh: MemoryShopStore) -> None:
    await fresh.record(record("a.shop", meta_pixel={"111"}))
    await fresh.record(record("b.shop", meta_pixel={"111"}, ga={"G-BBBBBBBB1"}))
    await fresh.record(record("c.shop", ga={"G-BBBBBBBB1"}))
    assert [link.domain for link in await fresh.linked("a.shop", {})] == ["b.shop"]  # not c.shop via b.shop
    assert {link.domain for link in await fresh.linked("b.shop", {})} == {"a.shop", "c.shop"}


async def test_the_current_page_links_before_the_shop_is_stored(fresh: MemoryShopStore) -> None:
    await fresh.record(record("old.shop", meta_pixel={"111"}))
    assert [link.domain for link in await fresh.linked("new.shop", {"meta_pixel": {"111"}})] == ["old.shop"]


async def test_an_identifier_shared_by_many_shops_links_nobody(fresh: MemoryShopStore) -> None:
    for i in range(shop_store.MAX_SHARE + 1):
        await fresh.record(record(f"s{i}.shop", gtm={"GTM-AGENCY1"}))
    assert await fresh.linked("s0.shop", {}) == []  # held by more than MAX_SHARE shops
    assert len(await fresh.linked("s0.shop", {})) == len(await fresh.linked("s1.shop", {}))


async def test_contact_values_are_never_part_of_a_link(fresh: MemoryShopStore) -> None:
    await fresh.record(record("a.shop", email={"f" * 64}))
    await fresh.record(record("b.shop", email={"f" * 64}))
    (link,) = await fresh.linked("a.shop", {})
    assert link.kinds == ("email",) and link.shared == ()


async def test_retention_and_removal(fresh: MemoryShopStore) -> None:
    await fresh.record(record("old.shop", day=TODAY - timedelta(days=200), phone={"a" * 64}, ga={"G-OLDOLD123"}))
    await fresh.purge(TODAY)
    assert ("phone", "a" * 64, "old.shop") not in fresh.links  # contacts: 180 days
    assert ("ga", "G-OLDOLD123", "old.shop") in fresh.links  # the rest: 400 days
    await fresh.purge(TODAY + timedelta(days=250))
    assert fresh.shops == {} and fresh.links == {}

    await fresh.record(record("x.shop", email={"b" * 64}))
    await fresh.record(record("y.shop", email={"b" * 64}))
    assert await fresh.forget_contact("email", "b" * 64) == 2
    assert await fresh.forget_shop("x.shop") is True and await fresh.forget_shop("x.shop") is False


async def test_skill_evidence_is_factual_and_from_raw_facts(fresh: MemoryShopStore) -> None:
    young, old = TODAY - timedelta(days=30), date(2015, 1, 1)
    await fresh.record(record("sister-one.shop", registered=young, meta_pixel={PIXEL}))
    await fresh.record(record("sister-two.shop", registered=old, email={"c" * 64}, meta_pixel={PIXEL}))
    out = await ShopLinksSkill().run(shop_state())
    f = out["features"]
    assert (f["shop_links.linked_shops"], f["shop_links.linked_young_shops"]) == (2, 1)
    assert f["shop_links.shared_contact"] == 0.0  # sister-two's contact isn't on this page
    evidence = [finding.evidence for finding in out["findings"]]
    assert evidence[0] == f"Shares its Meta pixel ID {PIXEL} with 2 other shops: sister-one.shop, sister-two.shop."
    assert evidence[-1] == "1 of the linked shops were registered in the last six months: sister-one.shop."


async def test_skill_shows_no_contact_values(fresh: MemoryShopStore) -> None:
    rec = await shop_store.observation(shop_state())
    assert rec is not None
    await fresh.record(ShopRecord("sibling.shop", TODAY, links={"email": rec.links["email"]}))
    out = await ShopLinksSkill().run(shop_state(html='<a href="mailto:support@glowlamp.shop">mail</a>'))
    assert out["features"]["shop_links.shared_contact"] == 1.0
    (finding,) = out["findings"]
    assert finding.evidence == "Shares its contact e-mail address with 1 other shop: sibling.shop."
    assert "glowlamp" not in finding.evidence


async def test_the_judge_records_the_shop_in_the_background(fresh: MemoryShopStore) -> None:
    async def judge(state: Any) -> dict[str, Any]:
        return {"score": 40}

    assert await _recording(judge)(shop_state()) == {"score": 40}
    await shop_store.drain()
    assert set(fresh.shops) == {"glowlamp.shop"}
    assert fresh.shops["glowlamp.shop"].first_seen == TODAY


async def test_fingerprints_off_records_nothing(fresh: MemoryShopStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fingerprints", False)

    async def judge(state: Any) -> dict[str, Any]:
        return {"score": 40}

    await _recording(judge)(shop_state())
    await shop_store.drain()
    assert fresh.shops == {}
    assert (await ShopLinksSkill().run(shop_state()))["features"] == {}


async def test_a_failing_store_costs_only_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(MemoryShopStore):
        async def record(self, rec: ShopRecord) -> None:
            raise OSError("db down")

    shop_store.use_store(Broken())
    await shop_store.observe(shop_state())  # logged, not raised


async def test_admin_removes_shops_and_contacts(fresh: MemoryShopStore, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = await shop_store.observation(shop_state())
    assert rec is not None
    await fresh.record(rec)
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    auth = {"Authorization": "Bearer operator-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.delete("/v1/admin/shops/glowlamp.shop")).status_code == 401
        body = {"kind": "email", "value": " Support@GlowLamp.shop "}
        assert (await client.post("/v1/admin/contacts/forget", json=body, headers=auth)).json() == {"removed": 1}
        bad = {"kind": "phone", "value": "123"}
        assert (await client.post("/v1/admin/contacts/forget", json=bad, headers=auth)).status_code == 422
        response = await client.delete("/v1/admin/shops/www.glowlamp.shop", headers=auth)
    assert response.json() == {"domain": "glowlamp.shop", "removed": True}
    assert fresh.shops == {}


def test_fingerprint_bundles_everything() -> None:
    fp = fingerprint(SHOP_HTML)
    assert fp.emails == {"support@glowlamp.shop"} and "meta_pixel" in fp.links
