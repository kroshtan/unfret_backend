"""EU shops: made-up VAT numbers, bank accounts that look like VAT numbers, and the per-market evaluation tooling."""

import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import markets
from evals.corpus import Corpus, LabelRecord
from evals.sources import eu_shops
from evals.train import build, fit, write_artifact
from unfret import fact_cache, scoring, shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.facts.vat_ids import check_digits_ok, is_iban_at
from unfret.graph.skills.eu_registry import EuRegistrySkill, fake_vat, in_iban, shown_whole
from unfret.graph.state import Finding

# Valid numbers from the member states' published examples and two large shops (Coolblue, Zalando).
VALID = [
    "ATU13585627",
    "BE0403019261",
    "CZ25123891",
    "DE136695976",
    "DE260543043",
    "DK13585628",
    "FI20774740",
    "FR40303265045",
    "IT00743110157",
    "LT119511515",
    "LT100001919017",
    "LU15027442",
    "NL004495445B01",
    "NL810433941B01",
    "PL8567346215",
    "PT501964843",
    "SE123456789701",
]


def _state(facts: PageFacts, text: str = "", url: str = "https://winkel.example.nl/") -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": "storefront",
    }


@pytest.fixture(autouse=True)
def fresh_fact_cache() -> None:
    fact_cache.use_store(fact_cache.MemoryStore())


# ── Check digits ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("vat_id", VALID)
def test_real_vat_numbers_pass_and_one_wrong_digit_fails(vat_id: str) -> None:
    assert check_digits_ok(vat_id) is True
    # Change a digit the check covers (the Dutch "B01" suffix isn't part of it).
    i = len(vat_id) - (4 if vat_id.startswith("NL") else 1)
    wrong = vat_id[:i] + str((int(vat_id[i]) + 1) % 10) + vat_id[i + 1 :]
    assert check_digits_ok(wrong) is False


@pytest.mark.parametrize("vat_id", ["BE0464203198", "BE1634866999", "BE0580845700"])
def test_belgian_check_digits_98_99_and_00_are_not_called_made_up(vat_id: str) -> None:
    # The official rule never issues them, but mod 97 can't tell them from 1, 2 and 97: never flag what may be real.
    assert check_digits_ok(vat_id) is True


def test_dutch_sole_trader_numbers_use_mod_97() -> None:
    # Since 2020 a sole trader's VAT ID is checked with mod 97 over the whole ID, not the "elfproef".
    assert check_digits_ok("NL123456789B13") is True  # fails the elfproef, passes mod 97
    assert check_digits_ok("NL123456789B14") is False
    assert check_digits_ok("NL123456789B01") is False  # neither


@pytest.mark.parametrize("vat_id", ["ESB12345678", "IE6388047V", "FRXX303265045", "CZ9001011234", "HU12345678"])
def test_countries_or_formats_without_a_known_check_are_not_judged(vat_id: str) -> None:
    assert check_digits_ok(vat_id) is None


def test_lithuanian_numbers_need_a_one_in_the_second_to_last_place() -> None:
    assert check_digits_ok("LT119511525") is False


def test_iban_detection_checks_length_and_mod_97() -> None:
    assert is_iban_at("LT12 1000 0111 0100 1000", 0)
    assert is_iban_at("Bank: NL91ABNA0417164300", 6)
    assert not is_iban_at("LT12 1000 0111 0100 1001", 0)  # one digit off
    assert not is_iban_at("LT12 1000 0111", 0)  # too short
    assert not is_iban_at("PVM kodas LT100001919017", 0)


# ── eu_registry ───────────────────────────────────────────────────────────────


def test_a_vat_number_run_into_other_digits_is_not_shown_whole() -> None:
    assert shown_whole("DE136695976", "USt-IdNr.: DE 136 695 976. Berlin")
    assert shown_whole("NL810433941B01", "btw NL8104.33.941.B01")
    assert not shown_whole("LT100001138101", "PVM m/k: LT1000011381019 Bankas")  # a typo on the page
    assert not fake_vat(["LT100001138101"], "PVM m/k: LT1000011381019 Bankas")


def test_fake_only_when_every_number_is_impossible() -> None:
    text = "VAT DE136695977 and VAT FR40303265045"
    assert not fake_vat(["DE136695977", "FR40303265045"], text)  # one real number is enough
    assert fake_vat(["DE136695977"], "VAT DE136695977.")
    assert not fake_vat(["ESB12345678"], "CIF ESB12345678")  # can't tell
    assert not fake_vat([], "")


async def test_a_made_up_vat_number_earns_no_credit_and_is_never_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", True)

    async def no_lookup(*_: Any, **__: Any) -> None:
        raise AssertionError("an impossible number must not be looked up")

    monkeypatch.setattr("unfret.graph.skills.eu_registry.cached_fact", no_lookup)
    facts = PageFacts(vat_ids=["NL123456789B01"], legal_mentions=["KvK 12345678. 14 dagen bedenktijd."])
    out = await EuRegistrySkill().run(_state(facts, "Btw-nummer: NL123456789B01. KvK 12345678."))

    f = out["features"]
    assert (f["eu_registry.vat_present"], f["eu_registry.vat_valid"], f["eu_registry.imprint"]) == (0.0, 0.0, 1.0)
    assert [x.type for x in out["findings"]] == ["vat_fake"]


async def test_a_real_vat_number_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", False)
    facts = PageFacts(vat_ids=["NL810433941B01"])
    out = await EuRegistrySkill().run(_state(facts, "Btw: NL810433941B01"))
    assert out["features"]["eu_registry.vat_present"] == 1.0
    assert "eu_registry.vat_valid" not in out["features"]
    assert not any(x.type == "vat_fake" for x in out["findings"])


async def test_a_bank_account_is_not_looked_up_as_a_vat_number(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Lithuanian shop's footer: the extraction reads the start of its IBAN as a VAT ID; VIES would call it invalid.
    monkeypatch.setattr(settings, "network_lookups", True)
    looked_up: list[str] = []

    async def lookup(kind: str, key: str, _: Any) -> dict[str, Any]:
        looked_up.append(key)
        return {"valid": True, "name": "UAB PAVYZDYS"}

    monkeypatch.setattr("unfret.graph.skills.eu_registry.cached_fact", lookup)
    text = "UAB Pavyzdys, a. s. LT12 1000 0111 0100 1000, PVM kodas LT100001919017"
    facts, _ = parse_page(f"<html><body><footer>{text}</footer></body></html>", "https://pavyzdys.lt/")
    assert facts.vat_ids[0] != "LT100001919017"  # the IBAN comes first
    assert in_iban(facts.vat_ids[0], text)
    out = await EuRegistrySkill().run(_state(facts, text, "https://pavyzdys.lt/"))
    assert looked_up == ["LT100001919017"]
    assert out["features"]["eu_registry.vat_valid"] == 1.0


# ── The shopper verdict ───────────────────────────────────────────────────────


def _verdict(score: int, features: dict[str, float], findings: list[Finding], **kw: Any) -> shopper.Verdict:
    return shopper.build(
        score, features=features, findings=findings, facts=PageFacts(), page_type="storefront", market="eu", **kw
    )


def test_a_made_up_vat_number_is_a_concrete_warning() -> None:
    features = {"eu_registry.vat_present": 0.0, "eu_registry.vat_valid": 0.0, "eu_registry.imprint": 1.0}
    fake = [Finding(skill="eu_registry", type="vat_fake", evidence="VAT ID NL123456789B01 can't exist.")]
    for score in (95, 60, 40):
        verdict = _verdict(score, features, fake, confidence=0.1, concrete_only=True)
        assert verdict.band in {"careful", "junk_or_scam"}, score  # "careful" at best, even for a quick check
        assert verdict.reasons[0].type == "invalid_vat"
    assert _verdict(10, features, fake).band == "junk_or_scam"  # never raised, only floored
    # Not in VIES (a real number that isn't registered for cross-border trade) is a reason, not a floor.
    vies = {"eu_registry.vat_present": 1.0, "eu_registry.vat_valid": 0.0}
    vies_verdict = _verdict(95, vies, [])
    assert vies_verdict.band == "reliable"
    assert [r.type for r in vies_verdict.reasons] == ["invalid_vat"]


def test_no_vat_check_no_vat_reason() -> None:
    assert "invalid_vat" not in {r.type for r in _verdict(40, {"eu_registry.vat_present": 1.0}, []).reasons}


# ── Evaluation tooling ────────────────────────────────────────────────────────


def _record(i: int, label: str, split: str = "test", source: str = "wayback") -> LabelRecord:
    return LabelRecord.model_validate(
        {
            "id": f"p{i}",
            "label": label,
            "label_source": "dataset",
            "page_type": "storefront",
            "source": source,
            "origin_dataset": "test",
            "split": split,
        }
    )


def test_market_groups_and_metrics() -> None:
    model = scoring.Model(
        version="t",
        features=["dark_patterns.timer_resets"],
        means=[0.5],
        scales=[0.5],
        coefficients=[3.0],
        intercept=0.0,
        calibration=[],
    )
    reset = {"dark_patterns.timer_resets": 1.0}
    clean = {"dark_patterns.timer_resets": 0.0}
    pages = [
        markets.Page(_record(1, "scam"), reset, PageFacts(), "eu", "storefront", True),
        markets.Page(_record(2, "legit"), clean, PageFacts(), "eu", "storefront", True),
        markets.Page(_record(3, "legit", source="live"), reset, PageFacts(), "eu", "storefront", False),
        markets.Page(_record(4, "scam"), reset, PageFacts(), "us", "storefront", True),
    ]
    result = markets.by_market(pages, model, quick=False)
    assert {k: v["pages"] for k, v in result.items()} == {
        "all": 4,
        "eu": 3,
        "eu_archived": 2,
        "eu_live": 1,
        "non_eu": 1,
        "us": 1,
        "other": 0,
    }
    eu = result["eu"]
    assert (eu["legit_warned"], eu["legit"], eu["unreliable_caught"], eu["unreliable"]) == (1, 2, 1, 1)
    assert eu["roc_auc"] == 0.75 and result["eu_archived"]["roc_auc"] == 1.0
    assert result["us"]["roc_auc"] is None and result["us"]["recall"] == 1.0


async def test_recompute_merges_page_skills_into_the_run(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    record = _record(1, "legit")
    corpus.add(record, "<html><body><p>USt-IdNr. DE136695976 Impressum</p><p>€ 19,95</p></body></html>")
    old = {"content.generic_brand": 0.9, "eu_registry.imprint": 0.0, "listing.is_marketplace": 0.0}
    page = await markets.recompute(corpus, record, old)
    assert page.from_run and page.market == "eu" and page.page_type == "storefront"
    assert page.features["content.generic_brand"] == 0.9  # the LLM's read is kept
    assert page.features["eu_registry.imprint"] == 1.0 and page.features["eu_registry.vat_present"] == 1.0
    fresh = await markets.recompute(corpus, record, None)
    assert not fresh.from_run and "content.generic_brand" not in fresh.features


def test_write_artifact_is_loadable(tmp_path: Path) -> None:
    rng = random.Random(3)
    names = ["dark_patterns.timer_resets"]
    records = [_record(i, "scam" if i % 2 else "legit", "train" if i < 60 else "test") for i in range(80)]
    features = {r.id: {names[0]: float(r.label == "scam" or rng.random() < 0.2)} for r in records}
    train = build([r for r in records if r.split == "train"], features, names)
    test = build([r for r in records if r.split == "test"], features, names)
    model = fit(train, names, "v-test")
    path = tmp_path / "model.json"
    write_artifact(model, path, features_file="f.jsonl", train_n=60, prior=0.5, test=test)
    data = json.loads(path.read_text())
    assert data["trained_on"] == {"features_file": "f.jsonl", "train_n": 60, "prior": 0.5}
    assert set(data["test"]) == {"roc_auc", "brier", "thresholds"}
    assert scoring.Model.from_dict(data).version == "v-test"  # what load_model reads


# ── The live EU shop source ───────────────────────────────────────────────────


def test_the_shop_list_has_no_duplicates_and_covers_the_eu() -> None:
    urls = [url for _, url in eu_shops.SHOPS]
    assert len(urls) == len(set(urls))
    assert all(url.startswith("https://") for url in urls)
    assert {"NL", "DE", "FR", "BE", "ES", "IT"} <= {country for country, _ in eu_shops.SHOPS}


async def test_robots_txt_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def robots(url: str, **_: Any) -> bytes:
        assert url == "https://shop.example/robots.txt"
        return b"User-agent: *\nDisallow: /private/\n\nUser-agent: Unfret\nDisallow: /\n"

    monkeypatch.setattr(eu_shops, "fetch_public", robots)
    assert not await eu_shops.robots_allows("https://shop.example/")
    assert await eu_shops.collect("https://shop.example/") == (None, "robots")

    async def none(url: str, **_: Any) -> bytes:
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))

    monkeypatch.setattr(eu_shops, "fetch_public", none)
    assert await eu_shops.robots_allows("https://shop.example/")  # no robots.txt: allowed


async def test_collected_shops_become_legit_dataset_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shop = "<html><body>" + "Echte winkel met veel tekst. " * 20 + "€ 19,95 € 24,95 In winkelwagen</body></html>"

    async def collect(url: str) -> tuple[str | None, str]:
        return (shop, "") if "good" in url else (None, "PAGE_BLOCKED")

    monkeypatch.setattr(eu_shops, "collect", collect)
    monkeypatch.setattr(eu_shops, "SHOPS", (("NL", "https://good.example/"), ("DE", "https://walled.example/")))
    corpus = Corpus(tmp_path)
    await eu_shops.build(corpus, None, retry=False)

    [record] = corpus.labels().values()
    assert (record.label, record.label_source, record.source, record.page_type) == (
        "legit",
        "dataset",
        "live",
        "storefront",
    )
    assert record.origin_dataset == eu_shops.DATASET
    assert (tmp_path / "raw" / "eu_shops_misses.tsv").read_text() == "https://walled.example/\tPAGE_BLOCKED\n"
    monkeypatch.setattr(eu_shops, "collect", lambda url: pytest.fail("misses are skipped on a re-run"))
    await eu_shops.build(corpus, None, retry=False)
