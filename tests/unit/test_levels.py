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
    assert risk.level == 2 and risk.key == "doesnt_work"
    assert [r.type for r in risk.because] == ["claim_contradicted", "timer_resets", "big_discount"]
    assert risk.explanation.startswith("Level 2 because: A claim that doesn't add up + Fake countdown")
    assert risk.combined is None and risk.modifier is None


def test_lower_levels_combine_upward_and_say_so() -> None:
    risk = levels.from_band("careful", ["claim_contradicted", "generic_brand", "timer_resets"])
    assert risk.level == 1
    assert risk.combined and "A claim that doesn't add up" in risk.combined and "Fake countdown" in risk.combined
    assert "add up" in risk.explanation


def test_payment_you_cant_get_back_raises_and_recourse_lowers() -> None:
    raised = levels.from_band("careful", ["generic_brand", "irreversible_payment_only"])
    assert raised.level == 2 and raised.modifier and raised.modifier.direction == "raised"
    assert raised.explanation.endswith("Raised: Only payments you can't get back.")
    lowered = levels.from_band("careful", ["claim_contradicted"], positive_types=["protected_payment"])
    assert lowered.level == 3 and lowered.modifier and lowered.modifier.direction == "lowered"
    # A chargeback doesn't undo stolen card details or an unsafe product.
    kept = levels.from_band("careful", ["safety_gate_alert"], positive_types=["protected_payment"])
    assert kept.level == 1 and kept.modifier is None


def test_the_translation_is_as_mild_as_the_band() -> None:
    assert levels.from_band("concerns", ["new_domain"]).level == 2  # "Worth a look" is never level 1
    assert levels.from_band("junk_or_scam", ["countdown"]).level == 1  # the scorer is sure
    assert levels.from_band("careful", []).level == 4  # only small signs
    assert levels.from_band("careful", ["data_inconsistency"]).level == 4  # a note is never the reason


def test_no_level_text_calls_a_shop_a_scam() -> None:
    text = str(catalogue()["levels"]).lower()
    assert "scam" not in text and "fraud" not in text
