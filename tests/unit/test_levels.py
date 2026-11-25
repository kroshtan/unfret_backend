from unfret import levels
from unfret.shopper import catalogue


def test_every_card_reason_has_an_entry_in_the_level_table() -> None:
    assert set(catalogue()["reasons"]) == set(levels.FINDING_LEVELS)
    assert set(catalogue()["positives"]) >= levels.RECOURSE_POSITIVES


def test_looks_fine_is_no_concern() -> None:
    risk = levels.from_band("reliable", ["countdown"])
    assert (risk.level, risk.key, risk.because, risk.source) == (0, "none", [], "band")
    assert risk.explanation == "No concerns found."


def test_the_level_is_the_most_serious_finding_with_its_reasons_in_level_order() -> None:
    risk = levels.from_band("careful", ["timer_resets", "claim_contradicted", "big_discount"])
    assert risk.level == 3 and risk.key == "doesnt_work"
    assert [r.type for r in risk.because] == ["claim_contradicted", "timer_resets", "big_discount"]
    assert risk.explanation.startswith("Level 3 because: A claim that doesn't add up + Fake countdown")
    assert risk.combined is None and risk.modifier is None


def test_smaller_problems_combine_upward_and_say_so() -> None:
    risk = levels.from_band("careful", ["claim_contradicted", "generic_brand", "timer_resets"])
    assert risk.level == 4 and risk.key == "wont_arrive"
    assert risk.combined and "A claim that doesn't add up" in risk.combined and "Fake countdown" in risk.combined
    assert "add up" in risk.explanation


def test_payment_you_cant_get_back_raises_and_recourse_lowers() -> None:
    raised = levels.from_band("careful", ["generic_brand", "irreversible_payment_only"])
    assert raised.level == 3 and raised.modifier and raised.modifier.direction == "raised"
    assert raised.explanation.endswith("Raised: Only payments you can't get back.")
    lowered = levels.from_band("careful", ["claim_contradicted"], positive_types=["protected_payment"])
    assert lowered.level == 2 and lowered.modifier and lowered.modifier.direction == "lowered"
    # A chargeback doesn't undo stolen card details or an unsafe product.
    kept = levels.from_band("careful", ["safety_gate_alert"], positive_types=["protected_payment"])
    assert kept.level == 4 and kept.modifier is None


def test_the_translation_is_as_mild_as_the_band() -> None:
    assert levels.from_band("concerns", ["new_domain"]).level == 3  # "Worth a look" is never level 4
    assert levels.from_band("junk_or_scam", ["countdown"]).level == 4  # the scorer is sure
    assert levels.from_band("careful", []).level == 1  # only small signs
    assert levels.from_band("careful", ["data_inconsistency"]).level == 1  # a note is never the reason


def test_a_higher_level_is_always_worse() -> None:
    assert levels.KEYS == {0: "none", 1: "manipulation", 2: "overpaying", 3: "doesnt_work", 4: "wont_arrive"}
    assert levels.level_of("safety_gate_alert") == 4 and levels.level_of("timer_resets") == 1
    assert min(["countdown", "new_domain", "generic_brand"], key=levels.order_key) == "new_domain"


def test_no_level_text_calls_a_shop_a_scam() -> None:
    text = str(catalogue()["levels"]).lower()
    assert "scam" not in text and "fraud" not in text


async def test_the_findings_catalogue_lists_what_a_shopper_can_mark_missing() -> None:
    import httpx  # noqa: PLC0415

    from unfret.main import app  # noqa: PLC0415

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/catalogue/findings")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    findings = response.json()["findings"]
    assert {f["type"] for f in findings} == set(levels.FINDING_LEVELS) - {"overall"}
    countdown = next(f for f in findings if f["type"] == "timer_resets")
    assert countdown == {"type": "timer_resets", "title": "Fake countdown", "level": 1, "key": "manipulation"}
    assert findings[0]["level"] == 4 and findings[-1]["level"] is None  # worst first, payment modifiers last
    assert all(f["title"] and "scam" not in f["title"].lower() for f in findings)


def test_every_finding_behind_the_level_says_where_its_evidence_comes_from() -> None:
    from unfret import card  # noqa: PLC0415
    from unfret.shopper import Reason, Verdict  # noqa: PLC0415

    verdict = Verdict(
        band="careful",
        label="Think twice",
        headline="x",
        reasons=[Reason(type="timer_resets", title="Fake countdown", detail="d", severity="high", source="The page")],
        risk=levels.from_band("careful", ["timer_resets", "new_domain"]),
    )
    card._because_sources(verdict, {})
    assert verdict.risk is not None
    sources = {b.type: b.source for b in verdict.risk.because}
    assert sources["timer_resets"] == "The page" and sources["new_domain"]
