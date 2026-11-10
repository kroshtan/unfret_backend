"""Fewer false alarms: agents warned no more than shoppers, EU-only absences outside the EU, the content links."""

from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda

from unfret import shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph.skills import content
from unfret.graph.skills.content import ContentSkill
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.market import shop_market
from unfret.x402.verdict import action_for, to_verdict


def _state(facts: PageFacts, text: str = "", url: str = "https://shop.example.com/p", **extra: Any) -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": "storefront",
        **extra,
    }


# ── Agents are never warned about a page a shopper isn't warned about ────────


@pytest.mark.parametrize(
    ("risk", "policy", "band", "action"),
    [
        (0.45, "balanced", "concerns", "allow"),  # was "warn": the shopper sees "a few things to check"
        (0.45, "balanced", "careful", "warn"),
        (0.45, "strict", "concerns", "warn"),  # strict callers asked for it
        (0.30, "strict", "reliable", "warn"),
        (0.65, "lenient", "concerns", "allow"),  # a capped band (absences only, unsure scorer)
        (0.65, "lenient", "careful", "warn"),
        (0.80, "balanced", "junk_or_scam", "block"),
        (0.45, "balanced", None, "warn"),  # no shopper band (LLM judge): the thresholds alone
    ],
)
def test_action_follows_the_shopper_band(risk: float, policy: Any, band: str | None, action: str) -> None:
    assert action_for(risk, policy, band) == action


def test_verdict_uses_the_band_but_a_degraded_analysis_still_warns() -> None:
    result: dict[str, Any] = {
        "score": 55,
        "assessment": {"risk_score": 0.45, "confidence": 0.1, "model_version": "t", "degraded": False},
        "verdict": {"band": "concerns", "reasons": [], "headline": "A few things to check."},
    }
    assert to_verdict(result, policy="balanced", tier="quick").action == "allow"
    assert to_verdict(result, policy="strict", tier="quick").action == "warn"
    result["assessment"]["degraded"] = True  # the content read failed: never "allow" on partial evidence
    assert to_verdict(result, policy="balanced", tier="quick").action == "warn"


# ── Markets ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("facts", "url", "market"),
    [
        (PageFacts(currency="CAD"), "https://shop.example.com/p", "other"),
        (PageFacts(currency="AUD"), "https://shop.example.com/p", "other"),
        (PageFacts(), "https://shop.example.com.au/p", "other"),
        (PageFacts(currency="USD"), "https://shop.example.ca/p", "other"),  # a Canadian domain pricing in dollars
        (PageFacts(currency="CHF"), "https://shop.example.com/p", "eu"),  # Swiss and EEA shops: EU-like rules
        (PageFacts(currency="$"), "https://shop.example.com/p", "eu"),  # not a currency code: no evidence
        (PageFacts(), "https://shop.example.com/p", "eu"),  # no evidence at all: EU rules, as before
        (PageFacts(currency="AUD"), "https://shop.example.de/p", "eu"),
    ],
)
def test_other_market_needs_positive_evidence(facts: PageFacts, url: str, market: str) -> None:
    assert shop_market(facts, url) == market


@pytest.mark.parametrize("currency", ["USD", "CAD"])
async def test_outside_the_eu_a_missing_return_policy_is_not_applicable(currency: str) -> None:
    facts = PageFacts(currency=currency, price=20.0)
    out = await EuRegistrySkill(vies=False).run(_state(facts, "Great shoes."))
    f = out["features"]
    assert "eu_registry.withdrawal_right" not in f and f["eu_registry.withdrawal_right.not_applicable"] == 1.0
    assert out["findings"] == []
    linked = PageFacts(currency=currency, price=20.0, policy_links={"refund": "https://shop.example.com/returns"})
    assert (await EuRegistrySkill(vies=False).run(_state(linked)))["features"]["eu_registry.withdrawal_right"] == 1.0
    verdict = shopper.build(40, features=f, findings=[], facts=facts, page_type="storefront", market="us")
    assert not {r.type for r in verdict.reasons} & {"no_return_policy", "no_withdrawal", "no_imprint"}


async def test_an_eu_shop_still_needs_its_withdrawal_right() -> None:
    out = await EuRegistrySkill(vies=False).run(_state(PageFacts(currency="EUR", price=20.0), url="https://a.nl/p"))
    assert out["features"]["eu_registry.withdrawal_right"] == 0.0


# ── The content read and the shop's policy links ──────────────────────────────


async def _prompt(monkeypatch: pytest.MonkeyPatch, on: bool) -> str:
    prompts: list[Any] = []

    def read(messages: Any) -> Any:
        prompts.append(messages)
        return content._Signals(
            scam_copy=0,
            generic_product=0,
            brand_recognized=False,
            implausible_claims=0,
            missing_trust_info=0,
            addresses_the_reader_ai=False,
        )

    monkeypatch.setattr(content, "get_structured_llm", lambda schema: RunnableLambda(read))
    monkeypatch.setattr(settings, "content_policy_links", on)
    facts = PageFacts(title="Tee", policy_links={"refund": "https://s/r", "contact": "https://s/c"})
    await ContentSkill().run(_state(facts, "A tee."))
    return str(prompts[0][1].content)


async def test_policy_links_reach_the_content_read_only_when_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    off = await _prompt(monkeypatch, on=False)
    assert "Links to" not in off  # the default prompt is unchanged, so recorded eval responses still replay
    on = await _prompt(monkeypatch, on=True)
    assert "Links to: contact page, returns/refund policy" in on
