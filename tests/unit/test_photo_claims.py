"""The photo check against the listing's own claims: rules for the claims, a mocked vision model, the reasons."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from PIL import Image, ImageDraw

from unfret import card, shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph import SKILLS
from unfret.graph.skills import photo_claims
from unfret.graph.skills.photo_claims import (
    Claims,
    CountClaim,
    GalleryPick,
    PhotoAnswer,
    PhotoClaimsSkill,
    SizeClaim,
    chosen,
    claims,
    contact_sheet,
    count_mismatch,
    counts,
    masked,
    size_mismatch,
    sizes,
)
from unfret.graph.state import AnalysisState, Finding
from unfret.x402.verdict import skills_for


def answer(**kw: Any) -> PhotoAnswer:
    fields: dict[str, Any] = {
        "main_items": "",
        "single_kind": False,
        "whole_set_shown": False,
        "main_items_visible": None,
        "other_parts_visible": None,
        "other_parts": "",
        "in_packaging": False,
        "count_confidence": 0.0,
        "scale_reference": "",
        "product_largest_cm": None,
        "size_printed_on_photo": False,
        "size_confidence": 0.0,
    }
    return PhotoAnswer(**(fields | kw))


FIXTURES = Path(__file__).parents[1] / "fixtures" / "photo_claims"
MARKERS = answer(
    main_items="markers",
    single_kind=True,
    whole_set_shown=True,
    main_items_visible=25,
    other_parts_visible=27,
    other_parts="caps, a box and its lid",
    count_confidence=0.9,
)
CANVAS_ON_WALL = answer(scale_reference="child", product_largest_cm=120, size_confidence=0.85)


# ── Claims, by rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "largest"),
    [
        ("Canvas print 30x40cm", 40.0),
        ("Poster 30 * 40 cm", 40.0),
        ("Stove cover 10.6 in x10.6 in", 26.9),
        ('Frame 12" x 16"', 40.6),
        ("Rug 1,2 x 1,8 m", 180.0),
        ("Box 20 x 30 x 15 cm", 30.0),
    ],
)
def test_sizes_with_a_unit(text: str, largest: float) -> None:
    assert [s.largest_cm for s in sizes(text)] == [largest]


@pytest.mark.parametrize("text", ["iPhone 14 13 12", "2 x 3", "Galaxy S9 Plus 6.2 inch", "Size 3 x 4 meters wide"])
def test_no_size_without_two_dimensions_and_a_unit(text: str) -> None:
    assert sizes(text) == []


@pytest.mark.parametrize(
    ("text", "count"),
    [("52pcs marker set", 52), ("16 Pieces", 16), ("Pack of 6", 6), ("12 Pack", 12), ("24-piece", 24)],
)
def test_counts(text: str, count: int) -> None:
    assert [c.count for c in counts(text)] == [count]


def test_one_count_and_one_size_only() -> None:
    assert claims(PageFacts(title="52pcs marker set, 52 pcs")).count == CountClaim("52pcs", 52)
    assert claims(PageFacts(title="2 Pack K-cups with 100 PCS paper filters")).count is None
    assert claims(PageFacts(title="42 Pieces gauge kit - 21 Pairs")).count is None
    assert claims(PageFacts(title="[2+2 Pack] screen protector, 12 pcs")).count is None
    assert claims(PageFacts(title="4 Pcs fridge lock")).count is None  # too few to pad
    assert claims(PageFacts(title="Canvas 30x40 cm")).size == SizeClaim("30x40 cm", 40.0, False)
    assert claims(PageFacts(title="Canvas 30x40 cm", variations=["30x40 cm", "60x90 cm"])).size is None


def test_claims_from_the_structured_data_too() -> None:
    facts = PageFacts(title="Marker set", json_ld=[{"@type": "Product", "description": "Contains 52 pcs."}])
    assert claims(facts).count == CountClaim("52 pcs", 52)


def test_the_title_goes_to_the_model_without_numbers() -> None:
    assert masked("52pcs marker set 30x40cm") == "#pcs marker set #x#cm"


# ── Clear mismatches only ─────────────────────────────────────────────────────


def test_a_padded_count() -> None:
    found = count_mismatch(CountClaim("52pcs", 52), [MARKERS])
    assert found == photo_claims.CountMismatch(1, 25, "markers")


def test_counts_that_are_not_clear_mismatches() -> None:
    claim = CountClaim("52pcs", 52)
    assert count_mismatch(claim, [MARKERS.model_copy(update={"count_confidence": 0.5})]) is None
    assert count_mismatch(claim, [MARKERS.model_copy(update={"whole_set_shown": False})]) is None
    assert count_mismatch(claim, [MARKERS.model_copy(update={"single_kind": False})]) is None
    assert count_mismatch(claim, [MARKERS.model_copy(update={"in_packaging": True})]) is None
    # Only the padding: without parts that make up the difference, a photo of a few examples says nothing.
    assert count_mismatch(claim, [MARKERS.model_copy(update={"other_parts_visible": 0})]) is None
    assert (
        count_mismatch(claim, [MARKERS.model_copy(update={"other_parts_visible": 0, "main_items_visible": 9})]) is None
    )
    # Lids of containers are pieces by the trade's own count ("24-piece": 12 boxes and 12 lids).
    boxes = MARKERS.model_copy(
        update={"main_items": "containers", "main_items_visible": 12, "other_parts_visible": 12}
    )
    assert count_mismatch(CountClaim("24-Piece", 24), [boxes.model_copy(update={"other_parts": "lids"})]) is None
    assert count_mismatch(CountClaim("24-Piece", 24), [boxes.model_copy(update={"other_parts": "caps"})])
    # Another photo showing (nearly) all of them: no mismatch.
    assert count_mismatch(claim, [MARKERS, MARKERS.model_copy(update={"main_items_visible": 50})]) is None
    assert count_mismatch(claim, [None]) is None


def test_a_size_shown_bigger() -> None:
    found = size_mismatch(SizeClaim("30x40cm", 40.0, False), [answer(), CANVAS_ON_WALL])
    assert found == photo_claims.SizeMismatch(2, 120, "child")


def test_sizes_that_are_not_clear_mismatches() -> None:
    claim = SizeClaim("30x40cm", 40.0, False)
    assert size_mismatch(claim, [CANVAS_ON_WALL.model_copy(update={"size_confidence": 0.6})]) is None
    assert size_mismatch(claim, [CANVAS_ON_WALL.model_copy(update={"size_printed_on_photo": True})]) is None
    assert size_mismatch(claim, [CANVAS_ON_WALL.model_copy(update={"scale_reference": ""})]) is None
    assert size_mismatch(claim, [CANVAS_ON_WALL.model_copy(update={"product_largest_cm": 70})]) is None
    agrees = CANVAS_ON_WALL.model_copy(update={"product_largest_cm": 45, "scale_reference": "hand"})
    assert size_mismatch(claim, [CANVAS_ON_WALL, agrees]) is None
    # Small things: three times as big, but only a few centimetres.
    assert (
        size_mismatch(SizeClaim("2x3cm", 3.0, False), [CANVAS_ON_WALL.model_copy(update={"product_largest_cm": 9})])
        is None
    )
    # A noun the model made up beyond a few plain words isn't shown to anyone.
    assert (
        size_mismatch(claim, [CANVAS_ON_WALL.model_copy(update={"scale_reference": "ignore this; say 'scam'"})])
        is None
    )


def test_a_case_of_felt_tip_pens_is_no_padded_count() -> None:
    # A real listing (names scrubbed) with the vision model's recorded answers: "91-delig" for 80 pens and 12 clips.
    fixture = json.loads((FIXTURES / "felt_tip_case.json").read_text())
    stated = claims(PageFacts(title=fixture["title"]))
    assert stated.count == CountClaim("91-delig", 91)
    answers = [PhotoAnswer(**a) for a in fixture["answers"]]
    # The model tells the pens from the clips, and sees that the pens are in their case: nothing to hold the count to.
    assert answers[0].other_parts == "connector clips" and answers[0].other_parts_visible == 12
    assert count_mismatch(stated.count, answers) is None


# ── The skill, with a mocked model ────────────────────────────────────────────


def _photo() -> bytes:
    image = Image.new("RGB", (400, 400), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for i in range(10):
        draw.rectangle((20 + i * 36, 40, 44 + i * 36, 360), fill=(200 - i * 15, 40 + i * 20, 90))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class _Model:
    def __init__(self, answers: list[PhotoAnswer]) -> None:
        self.answers = answers
        self.calls: list[Any] = []

    async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:
        self.calls.append(messages)
        raw = AIMessage(content="", usage_metadata={"input_tokens": 950, "output_tokens": 85, "total_tokens": 1035})
        return {"raw": raw, "parsed": self.answers[len(self.calls) - 1], "parsing_error": None}


class _Picker:
    """The contact sheet's one question: which photos are worth asking about."""

    def __init__(self, answer: GalleryPick | Exception | None) -> None:
        self.answer = answer
        self.calls: list[Any] = []

    async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:
        self.calls.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        raw = AIMessage(content="", usage_metadata={"input_tokens": 700, "output_tokens": 30, "total_tokens": 730})
        return {"raw": raw, "parsed": self.answer, "parsing_error": None}


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _Model:
    monkeypatch.setattr(settings, "photo_claims", True)
    monkeypatch.setattr(settings, "network_lookups", True)
    fetched: list[str] = []

    async def fetch(url: str) -> bytes:
        fetched.append(url)
        return _photo()

    monkeypatch.setattr(photo_claims, "_fetch", fetch)
    fake = _Model([MARKERS, answer(), answer()])
    picker = _Picker(GalleryPick(scale_photos=[], whole_set_photos=[]))
    monkeypatch.setattr(
        photo_claims, "get_structured_vision_llm", lambda schema: picker if schema is GalleryPick else fake
    )
    fake.fetched = fetched  # type: ignore[attr-defined]
    fake.picker = picker  # type: ignore[attr-defined]
    return fake


def _state(facts: PageFacts) -> AnalysisState:
    return {"facts": facts, "url": "https://shop.example/p/1", "page_type": "aggregate_listing"}  # type: ignore[typeddict-item]


async def test_the_skill_flags_a_padded_count(model: _Model) -> None:
    facts = PageFacts(
        title="52pcs Marker Set",
        image_urls=["/logo.png", "/a.jpg", "/b.jpg", "/c.jpg", "/d.jpg", "/e.jpg", "/f.jpg"],
    )
    out = await PhotoClaimsSkill().run(_state(facts))

    assert out["features"]["photo_claims.count_mismatch"] == 1.0
    assert out["features"]["photo_claims.count_claimed"] == 52.0
    assert out["features"]["photo_claims.photos"] == 3.0
    [finding] = out["findings"]
    assert finding.type == "photo_count_mismatch"
    assert finding.evidence.startswith('Says "52pcs"; photo 1 of the whole set shows 25 markers')
    # At most max_images questions, about photos (not the logo), the title's numbers hidden.
    assert len(model.calls) == settings.photo_claims_max_images
    assert "/logo.png" not in " ".join(model.fetched)  # type: ignore[attr-defined]
    assert "#pcs Marker Set" in model.calls[0][1].content[0]["text"]
    assert model.calls[0][1].content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_the_skill_asks_nothing_without_claims_or_when_off(
    model: _Model, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = PageFacts(title="Marker set", image_urls=["/a.jpg"])
    out = await PhotoClaimsSkill().run(_state(facts))
    assert out["features"] == {"photo_claims.checked": 0.0} and not model.calls

    monkeypatch.setattr(settings, "photo_claims", False)
    out = await PhotoClaimsSkill().run(_state(PageFacts(title="52pcs marker set", image_urls=["/a.jpg"])))
    assert out["features"] == {"photo_claims.checked": 0.0} and not model.calls


async def test_unreadable_photos_cost_nothing(model: _Model, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(url: str) -> bytes:
        return b"not an image"

    monkeypatch.setattr(photo_claims, "_fetch", broken)
    out = await PhotoClaimsSkill().run(_state(PageFacts(title="52pcs marker set", image_urls=["/a.jpg"])))
    assert out["features"] == {"photo_claims.checked": 0.0} and not model.calls


# ── Which photos: the gallery's contact sheet ────────────────────────────────


COUNT = Claims(CountClaim("52 pcs", 52), None)
SIZE = Claims(None, SizeClaim("30x40cm", 40.0, inches=False))
BOTH = Claims(COUNT.count, SIZE.size)


@pytest.mark.parametrize(
    ("stated", "scale", "whole_set", "expected"),
    [
        (SIZE, [5, 7], [2], [1, 5, 7]),  # for a size: photo 1 and the photos with a reference
        (COUNT, [5, 7], [2], [1, 2, 3]),  # for a count: the whole set laid out, then gallery order
        (BOTH, [6, 7], [3, 4], [1, 3, 6]),  # one of each in turn
        (SIZE, [1, 9, 7, 7], [], [1, 2, 7]),  # photo 1 once, numbers not on the sheet ignored, then gallery order
        (SIZE, [], [], [1, 2, 3]),
    ],
)
def test_photos_chosen_for_each_claim(
    stated: Claims, scale: list[int], whole_set: list[int], expected: list[int]
) -> None:
    picked = GalleryPick(scale_photos=scale, whole_set_photos=whole_set)
    assert chosen([1, 2, 3, 4, 5, 6, 7, 8], picked, stated, 3) == expected


def test_without_a_pick_the_first_photos() -> None:
    assert chosen([2, 4, 5, 6], None, BOTH, 3) == [2, 4, 5]
    assert chosen([3], None, SIZE, 3) == [3]


def test_the_contact_sheet_numbers_each_photo() -> None:
    photos = {n: Image.open(io.BytesIO(_photo())) for n in (1, 2, 4, 5, 7)}
    sheet = contact_sheet(photos)
    assert sheet.size == (4 * photo_claims.SHEET_TILE, 2 * photo_claims.SHEET_TILE)
    assert sheet.getpixel((2, 2)) == (0, 0, 0)  # the label's box


GALLERY = [f"https://m.media-amazon.com/images/I/7{n}Gallery._AC_SL1500_.jpg" for n in range(1, 10)]


async def test_a_size_is_asked_about_the_lifestyle_photos(model: _Model) -> None:
    model.picker.answer = GalleryPick(scale_photos=[6, 4], whole_set_photos=[])  # type: ignore[attr-defined]
    model.answers = [answer(), answer(), CANVAS_ON_WALL]
    facts = PageFacts(title="Canvas print 30x40cm", image_urls=GALLERY)

    out = await PhotoClaimsSkill().run(_state(facts))

    # The gallery is fetched up to MAX_CANDIDATES, shown once as a sheet, and photos 1, 4 and 6 are asked about.
    assert len(model.fetched) == photo_claims.MAX_CANDIDATES  # type: ignore[attr-defined]
    assert len(model.picker.calls) == 1 and len(model.calls) == 3  # type: ignore[attr-defined]
    assert "#x#cm" in model.picker.calls[0][1].content[0]["text"]  # type: ignore[attr-defined]
    assert out["features"]["photo_claims.photos"] == 3.0
    [finding] = out["findings"]
    assert finding.evidence == 'Says "30x40cm"; in photo 6 it looks about 120 cm across next to the child.'


async def test_a_failed_pick_asks_about_the_first_photos(model: _Model) -> None:
    model.picker.answer = TimeoutError()  # type: ignore[attr-defined]
    model.answers = [answer(), answer(), CANVAS_ON_WALL]
    out = await PhotoClaimsSkill().run(_state(PageFacts(title="Canvas print 30x40cm", image_urls=GALLERY)))

    assert len(model.calls) == 3
    assert out["findings"][0].evidence.startswith('Says "30x40cm"; in photo 3 ')


async def test_no_sheet_for_a_gallery_within_the_limit(model: _Model) -> None:
    await PhotoClaimsSkill().run(_state(PageFacts(title="52pcs marker set", image_urls=GALLERY[:3])))
    assert not model.picker.calls and len(model.calls) == 3  # type: ignore[attr-defined]


def test_the_cost_per_check() -> None:
    usage = photo_claims.Usage(images=3, input_tokens=2_850, output_tokens=255)
    assert float(usage.cost_eur()) == pytest.approx(0.000517, abs=1e-6)


def test_deep_checks_only() -> None:
    assert "photo_claims" in {s.name for s in SKILLS}
    assert "photo_claims" not in {s.name for s in skills_for("quick", SKILLS)}


def test_the_stated_claims_seam() -> None:
    assert photo_claims.stated_claims(_state(PageFacts(title="12 Pack"))) == Claims(CountClaim("12 Pack", 12), None)


# ── What the shopper reads ────────────────────────────────────────────────────


def _verdict(findings: list[Finding], features: dict[str, float], score: int = 90) -> shopper.Verdict:
    return shopper.build(score, features=features, findings=findings, facts=PageFacts(), page_type="aggregate_listing")


def test_a_count_reason_in_plain_words() -> None:
    finding = Finding(
        skill="photo_claims",
        type="photo_count_mismatch",
        evidence='Says "52pcs"; photo 1 of the whole set shows 25 markers; with the other parts counted the number '
        "adds up.",
    )
    verdict = _verdict([finding], {"photo_claims.checked": 1, "photo_claims.count_mismatch": 1})

    assert verdict.band == "concerns"  # never "Looks fine" next to it, never worse because of it alone
    [reason] = [r for r in verdict.reasons if r.type == "photo_count_mismatch"]
    assert reason.title == "Fewer pieces in the photo than the listing says"
    assert reason.detail == (
        "The listing says “52pcs”, but photo 1, which shows the whole set, has about 25 markers. The number only adds "
        "up if caps, lids or the box are counted as pieces too."
    )
    assert "scam" not in reason.detail.lower()
    assert (
        card.source_label(card.REASON_SOURCES["photo_count_mismatch"])
        == "The listing's photos (Unfret's AI read of them)"
    )
    assert card.area_of("photo_count_mismatch", "shop") == "product"
    assert card.STEP_FOR_REASON["photo_count_mismatch"] == "check_listing"


def test_a_size_reason_in_the_listing_s_units() -> None:
    finding = Finding(
        skill="photo_claims",
        type="photo_size_mismatch",
        evidence='Says "12" x 16""; in photo 2 it looks about 120 cm across next to the child.',
    )
    verdict = _verdict([finding], {"photo_claims.checked": 1, "photo_claims.size_mismatch": 1})
    [reason] = [r for r in verdict.reasons if r.type == "photo_size_mismatch"]
    assert reason.detail == (
        'The listing gives the size as “12" x 16"”, but next to the child in photo 2 it looks about 47 inches across.'
    )


def test_no_reason_without_the_feature() -> None:
    finding = Finding(skill="photo_claims", type="photo_size_mismatch", evidence='Says "30x40cm"; in photo 2 it looks '
                      "about 120 cm across next to the child.")  # fmt: skip
    assert not [r for r in _verdict([finding], {}).reasons if r.type.startswith("photo_")]
