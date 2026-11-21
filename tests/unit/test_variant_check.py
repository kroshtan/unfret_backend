"""The variant check: options read from any platform, what needs the model, what is told, and the band."""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda

from unfret import shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.extract import parse_page
from unfret.facts.schema import VariantDimension, VariantOption, Variants
from unfret.facts.variants import kind_of
from unfret.graph.skills import NOT_APPLICABLE, variant_check
from unfret.graph.skills.variant_check import (
    OptionRead,
    VariantAnswer,
    VariantCheckSkill,
    findings,
    plain,
    price_spread,
    worth_asking,
)
from unfret.graph.state import Finding
from unfret.llm import UNTRUSTED_NOTICE

PAGES = Path(__file__).parents[1] / "fixtures" / "pages"
URLS = {
    "amazon-de-variants-bait": "https://www.amazon.de/dp/B0VARDE001",
    "amazon-com-variants-counts": "https://www.amazon.com/dp/B0VARUS016",
    "shopify-variants-refill": "https://brightwell.example/products/sonicle-pro",
    "woocommerce-variants-sizes": "https://wolhuis.example/product/merino-t-shirt/",
    "generic-productgroup-colours": "https://fernhill.example/products/trail-bottle",
    "walmart-variants-grouped": "https://www.walmart.com/ip/Harbor-Pine-Patio-Dining-Set/9900000001",
    "generic-select-picker": "https://oakmere.example/duvet-cover",
}


def _page(name: str) -> tuple[PageFacts, str]:
    return parse_page((PAGES / f"{name}.html").read_text(), URLS[name])


def _options(variants: Variants | None) -> list[tuple[str, float | None]]:
    assert variants is not None
    return [(o.label, o.price) for o in variants.options]


# ── The facts ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_amazon_twister_options_their_swatch_prices_and_the_from_price() -> None:
    facts, _ = _page("amazon-de-variants-bait")
    v = facts.variants
    assert v is not None and v.source == "amazon"
    assert v.dimensions == [
        VariantDimension(
            name="Größe",
            kind="size",
            values=["Hülle", "Panzerglas 2 Stück", "Ladekabel USB-C 1 m", "Hülle + Panzerglas"],
        )
    ]
    assert _options(v) == [
        ("Hülle", 19.99),
        ("Panzerglas 2 Stück", 8.99),
        ("Ladekabel USB-C 1 m", 6.99),
        ("Hülle + Panzerglas", 24.99),
    ]
    # Nothing selected (a dimension at -1), a price range: the headline is the cheapest option's, the cable's.
    assert (v.from_price, v.headline_basis, v.headline) == (True, "from", 2)
    assert not any(o.selected for o in v.options)


def test_amazon_classic_twister_selection_and_prices_by_asin() -> None:
    facts, _ = _page("amazon-com-variants-counts")
    v = facts.variants
    assert v is not None
    assert _options(v) == [("8 Count", 6.99), ("16 Count", 11.49), ("48 Count", 27.99)]
    assert [o.selected for o in v.options] == [False, True, False]
    assert (v.headline_basis, v.headline) == ("selected", 1)


def test_shopify_options_cents_and_the_first_available_variant_on_show() -> None:
    facts, _ = _page("shopify-variants-refill")
    v = facts.variants
    assert v is not None and v.source == "shopify"
    assert v.dimensions[0].name == "Style" and v.dimensions[0].kind == "style"
    assert _options(v) == [
        ("Brush head refill (4 pack)", 4.99),
        ("Toothbrush", 39.99),
        ("Toothbrush + travel case", 49.99),
    ]
    assert [o.available for o in v.options] == [True, True, False]
    assert (v.headline_basis, v.headline) == ("selected", 0)


def test_shopify_variant_in_the_url_is_on_show() -> None:
    html = (PAGES / "shopify-variants-refill.html").read_text()
    facts, _ = parse_page(html, "https://brightwell.example/products/sonicle-pro?variant=4410000000002")
    assert facts.variants is not None
    assert [o.selected for o in facts.variants.options] == [False, True, False]
    # The page's price (4.99) isn't the selected option's: it belongs to the option whose price it is.
    assert (facts.variants.headline_basis, facts.variants.headline) == ("price", 0)


def test_woocommerce_variations_with_the_selects_labels_and_an_aggregate_offer() -> None:
    facts, _ = _page("woocommerce-variants-sizes")
    v = facts.variants
    assert v is not None and v.source == "woocommerce"
    assert (v.dimensions[0].name, v.dimensions[0].kind) == ("Maat", "size")
    assert _options(v) == [("S", 49.0), ("M", 49.0), ("L", 52.0), ("XL", 55.0)]
    assert [o.selected for o in v.options] == [False, True, False, False]
    assert (v.from_price, v.headline_basis, v.headline) == (True, "from", 0)


def test_schema_org_product_group() -> None:
    facts, _ = _page("generic-productgroup-colours")
    v = facts.variants
    assert v is not None and v.source == "schema_org"
    assert (v.dimensions[0].name, v.dimensions[0].kind) == ("color", "colour")
    assert _options(v) == [("Moss", 24.0), ("Slate", 24.0), ("Sand", 26.0)]
    assert [o.available for o in v.options] == [True, True, False]
    assert (v.headline_basis, v.headline) == ("price", 0)


def test_walmart_variant_criteria() -> None:
    facts, _ = _page("walmart-variants-grouped")
    v = facts.variants
    assert v is not None and v.source == "walmart"
    assert _options(v) == [("5-Piece Set", 329.0), ("Single Chair", 69.0), ("Seat Cushion", 19.0)]
    assert [o.selected for o in v.options] == [True, False, False]


def test_a_select_picker_outside_the_header_and_not_the_quantity() -> None:
    facts, _ = _page("generic-select-picker")
    v = facts.variants
    assert v is not None and v.source == "picker"
    assert [d.name for d in v.dimensions] == ["Size"]
    # The price at the end of each label, never the size's numbers.
    assert _options(v) == [("Single 140 x 200 cm", 89.0), ("Double 200 x 200 cm", 119.0), ("King 240 x 220 cm", 139.0)]
    assert (v.headline_basis, v.headline) == ("price", 0)


def test_a_page_without_a_picker_has_no_variants() -> None:
    facts, _ = parse_page(
        "<html><body><h1>Lamp</h1><select name='quantity'><option>1</option><option>2</option></select></body></html>",
        "https://shop.example/lamp",
    )
    assert facts.variants is None


@pytest.mark.parametrize(
    ("key", "name", "kind"),
    [
        ("size_name", "Maat", "size"),
        ("", "Farbe", "colour"),
        ("item_package_quantity", "Packungsgröße", "count"),
        ("style_name", "Stijlnaam", "style"),
        ("pattern_name", "Naam van patroon", "pattern"),
        ("scent_name", "Duft", "flavour"),
        ("", "Edition", "style"),
        ("", "", "other"),
    ],
)
def test_kind_of_a_dimension_in_any_language(key: str, name: str, kind: str) -> None:
    assert kind_of(key, name) == kind


# ── What needs the model ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["XL", "42 2/3", "500 ml", "Pack of 2", "2 stuks", "16 Count", "W32 L34", "$25.00"])
def test_plain_sizes_and_amounts(value: str) -> None:
    assert plain(value)


@pytest.mark.parametrize(
    ("item", "said"),
    [
        ("usb-c cable", "a usb-c cable"),
        ("accessory", "an accessory"),
        ("umbrella", "an umbrella"),
        ("unit", "a unit"),
        ("lid", "a lid"),
        ("one chair", "a one chair"),
        ("brush heads", "brush heads"),
        ("glass", "a glass"),
    ],
)
def test_the_article_goes_by_the_sound(item: str, said: str) -> None:
    assert variant_check._a(item) == said


@pytest.mark.parametrize("value", ["Screen protector", "Hülle", "Brush head refill (4 pack)", "Single Chair"])
def test_items_are_not_plain(value: str) -> None:
    assert not plain(value)


def test_plain_sizes_amounts_and_colours_cost_nothing() -> None:
    for name in ("amazon-com-variants-counts", "woocommerce-variants-sizes", "generic-productgroup-colours"):
        facts, _ = _page(name)
        assert facts.variants is not None and not worth_asking(facts.variants), name
    for name in ("amazon-de-variants-bait", "shopify-variants-refill", "walmart-variants-grouped"):
        facts, _ = _page(name)
        assert facts.variants is not None and worth_asking(facts.variants), name


def test_a_price_spread_beyond_the_amounts() -> None:
    def options(*pairs: tuple[str, float]) -> list[VariantOption]:
        return [VariantOption(label=label, price=price) for label, price in pairs]

    # 2 to 20 pieces at 4 to 30: explained by the amounts.
    assert price_spread(options(("2 stuks", 4.0), ("20 stuks", 30.0))) == 1.0
    assert price_spread(options(("S", 9.99), ("M", 59.95))) == 6.0
    assert worth_asking(
        Variants(
            options=options(("S", 9.99), ("M", 59.95)),
            dimensions=[VariantDimension(name="Size", kind="size", values=["S", "M"])],
        )
    )


# ── What is told ───────────────────────────────────────────────────────────────────────────────────────────────

PHONE = VariantAnswer(
    product="phone case",
    grouping="different_products",
    confidence=0.95,
    options=[
        OptionRead(label="Hülle", item="phone case", relation="same"),
        OptionRead(label="Panzerglas 2 Stück", item="screen protector", relation="accessory"),
        OptionRead(label="Ladekabel USB-C 1 m", item="charging cable", relation="different"),
        OptionRead(label="Hülle + Panzerglas", item="phone case", relation="same"),
    ],
)


def test_a_from_price_for_a_cable_under_a_phone_case() -> None:
    facts, _ = _page("amazon-de-variants-bait")
    assert facts.variants is not None
    grouped, bait = findings(PHONE, facts.variants, facts)
    assert grouped == (
        "This listing groups different products as sizes: “Hülle” (€19.99), “Panzerglas 2 Stück” (€8.99) and "
        "“Ladekabel USB-C 1 m” (€6.99); the €8.99 option is a screen protector; the €6.99 option is a charging cable. "
        "Its star rating covers all of them."
    )
    assert bait == (
        "The price shown, €6.99, is for “Ladekabel USB-C 1 m”, a charging cable, not the phone case in the title; "
        f"the phone case itself is from €19.99. {grouped}"
    )


def test_a_refill_on_show_on_a_shop() -> None:
    facts, _ = _page("shopify-variants-refill")
    assert facts.variants is not None
    answer = VariantAnswer(
        product="electric toothbrush",
        grouping="different_products",
        confidence=0.9,
        options=[
            OptionRead(label="Brush head refill (4 pack)", item="brush heads", relation="refill"),
            OptionRead(label="Toothbrush", item="electric toothbrush", relation="same"),
            OptionRead(label="Toothbrush + travel case", item="electric toothbrush", relation="same"),
        ],
    )
    grouped, bait = findings(answer, facts.variants, facts)
    assert bait.startswith(
        "The option on show, “Brush head refill (4 pack)”, is brush heads, not the electric toothbrush in the "
        "title; the electric toothbrush itself is from $39.99."
    )
    assert grouped.startswith("This listing groups different products as styles: “Toothbrush” ($39.99) and")


def test_grouping_alone_where_the_price_shown_is_the_product() -> None:
    facts, _ = _page("walmart-variants-grouped")
    assert facts.variants is not None
    answer = VariantAnswer(
        product="patio dining set",
        grouping="different_products",
        confidence=0.9,
        options=[
            OptionRead(label="5-Piece Set", item="patio dining set", relation="same"),
            OptionRead(label="Single Chair", item="patio chair", relation="part"),
            OptionRead(label="Seat Cushion", item="seat cushion", relation="accessory"),
        ],
    )
    grouped, bait = findings(answer, facts.variants, facts)
    assert grouped.startswith("This listing groups different products as sizes: “5-Piece Set” ($329.00), “Single")
    assert grouped.endswith("Its star rating covers all of them.")
    assert bait == ""


def test_a_bait_must_be_clearly_cheaper_than_the_product() -> None:
    facts, _ = _page("amazon-de-variants-bait")
    assert facts.variants is not None
    pricier = facts.variants.model_copy(
        update={
            "options": [
                o.model_copy(update={"price": 15.0}) if i == 2 else o for i, o in enumerate(facts.variants.options)
            ]
        }
    )
    _, bait = findings(PHONE, pricier, facts)
    assert bait == ""  # €15 against a €19.99 case is no bait price


@pytest.mark.parametrize(
    "answer",
    [
        PHONE.model_copy(update={"grouping": "same_product"}),
        PHONE.model_copy(update={"confidence": 0.6}),
        # Every option the title's product: nothing grouped, whatever the model's grouping says.
        PHONE.model_copy(update={"options": [o.model_copy(update={"relation": "same"}) for o in PHONE.options]}),
        # Options the page doesn't have (misquoted, or made up) don't count.
        PHONE.model_copy(
            update={"options": [PHONE.options[0], OptionRead(label="Cable", item="cable", relation="different")]}
        ),
        # A noun that isn't plain words never reaches the shopper.
        PHONE.model_copy(
            update={
                "options": [
                    PHONE.options[0],
                    OptionRead(label="Ladekabel USB-C 1 m", item="see www.x.com", relation="different"),
                ]
            }
        ),
    ],
)
def test_nothing_is_told_unless_it_holds_up(answer: VariantAnswer) -> None:
    facts, _ = _page("amazon-de-variants-bait")
    assert facts.variants is not None
    assert findings(answer, facts.variants, facts) == ("", "")


# ── The skill ──────────────────────────────────────────────────────────────────────────────────────────────────


class FakeModel:
    """Answers every check with the same canned answer and keeps the calls' prompts."""

    def __init__(self, answer: VariantAnswer) -> None:
        self.answer = answer
        self.calls: list[list[BaseMessage]] = []

    def __call__(self, schema: type[Any]) -> RunnableLambda:
        assert schema is VariantAnswer

        async def answer(messages: list[BaseMessage]) -> dict[str, Any]:
            self.calls.append(messages)
            raw = AIMessage(
                content="", usage_metadata={"input_tokens": 1200, "output_tokens": 250, "total_tokens": 1450}
            )
            return {"raw": raw, "parsed": self.answer, "parsing_error": None}

        return RunnableLambda(lambda _: None, afunc=answer)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel(PHONE)
    monkeypatch.setattr(settings, "variant_check", True)
    monkeypatch.setattr(variant_check, "get_structured_claims_llm", fake)
    return fake


def _state(name: str) -> dict[str, Any]:
    facts, text = _page(name)
    return {"facts": facts, "cleaned_content": text, "page_type": "aggregate_listing", "url": "", "flags": []}


async def test_the_skill_reports_the_bait_and_the_grouping(model: FakeModel) -> None:
    out = await VariantCheckSkill().run(_state("amazon-de-variants-bait"))  # type: ignore[arg-type]
    assert out["features"] == {
        "variant_check.options": 4.0,
        "variant_check.asked": 1.0,
        "variant_check.grouped": 1.0,
        "variant_check.bait_price": 1.0,
        "variant_check.price_spread": 3.58,
    }
    assert [f.type for f in out["findings"]] == ["variant_bait_price", "variants_grouped"]
    system, human = model.calls[0]
    assert UNTRUSTED_NOTICE in str(system.content)
    content = str(human.content)
    assert content.startswith("<untrusted_page>") and content.endswith("</untrusted_page>")
    assert "- Ladekabel USB-C 1 m (€6.99, the price shown is this option's)" in content
    assert "Price shown: €6.99" in content


async def test_plain_sizes_never_call_the_model(model: FakeModel) -> None:
    out = await VariantCheckSkill().run(_state("amazon-com-variants-counts"))  # type: ignore[arg-type]
    assert model.calls == []
    assert out["features"]["variant_check.asked"] == 0.0 and out["findings"] == []


async def test_off_is_not_applicable(model: FakeModel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "variant_check", False)
    out = await VariantCheckSkill().run(_state("amazon-de-variants-bait"))  # type: ignore[arg-type]
    assert out["features"] == {f"variant_check.{NOT_APPLICABLE}": 1.0} and model.calls == []


# ── The verdict ────────────────────────────────────────────────────────────────────────────────────────────────


def _verdict(*types: str, brand_known: bool = False) -> shopper.Verdict:
    facts, _ = _page("amazon-de-variants-bait")
    assert facts.variants is not None
    grouped, bait = findings(PHONE, facts.variants, facts)
    features = {"variant_check.grouped": 1.0}
    found = [Finding(skill="variant_check", type="variants_grouped", evidence=grouped)]
    if "bait" in types:
        features["variant_check.bait_price"] = 1.0
        found.insert(0, Finding(skill="variant_check", type="variant_bait_price", evidence=bait))
    if brand_known:
        features["content.brand_recognized"] = 1.0
    return shopper.build(90, features=features, findings=found, facts=facts, page_type="aggregate_listing")


def test_a_bait_price_is_worth_a_look_whoever_sells_it() -> None:
    for known in (False, True):
        verdict = _verdict("bait", brand_known=known)
        assert verdict.band == "concerns"
        [reason] = [r for r in verdict.reasons if r.type.startswith("variant")]
        assert reason.type == "variant_bait_price"
        assert reason.title == "The price shown is for a different item"
        assert reason.detail.startswith("The price shown, €6.99, is for “Ladekabel USB-C 1 m”, a charging cable")


def test_grouping_alone_is_a_note() -> None:
    verdict = _verdict()
    assert verdict.band == "reliable"
    assert [r.type for r in verdict.reasons] == ["variants_grouped"]
    assert verdict.reasons[0].title == "Different products under one listing"
