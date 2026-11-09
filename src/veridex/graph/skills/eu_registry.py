"""
EU merchant identity: a VAT ID checked against the official EU VIES register, and the legally required info.

EU law requires web shops to identify the trader (imprint) and to state the 14-day right of withdrawal; a VAT ID
that VIES knows, under a name matching the shop, is strong evidence of a real business. A US shop has neither an
imprint nor a VAT ID by law, so on a US shop both are reported as not applicable (neutral for the scorer, no reason
for the shopper) rather than missing; a named company (LLC, Inc.) or a US business address the page does show still
counts as its imprint, as positive evidence. Results are cached per VAT number, never per caller.
"""

import json
import re
from typing import Any

import httpx

from veridex.config import settings
from veridex.fact_cache import cached_fact
from veridex.graph.skills import NOT_APPLICABLE, STOREFRONTS, Observation, Skill, SkillOutput
from veridex.graph.skills.us_identity import find_identity
from veridex.graph.state import AnalysisState
from veridex.market import shop_market
from veridex.net import hostname

_WITHDRAWAL_RE = re.compile(
    r"withdraw|widerruf|herroeping|bedenktijd|rétractation|desistimiento|recesso|14[ -](?:day|dagen|tage|jours|días)",
    re.IGNORECASE,
)
_IMPRINT_RE = re.compile(
    r"impressum|imprint|kvk|kamer van koophandel|handelsregister|registre du commerce|company number|registered in",
    re.IGNORECASE,
)
_NAME_NOISE_RE = re.compile(r"\b(?:b\.?v\.?|gmbh|ltd|limited|s\.?a\.?r?\.?l?|s\.?l\.?|srl|spa|ug|ag|bv|nv|inc|co)\b")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_VIES_TIMEOUT_S = 10.0


async def check_vat(vat_id: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any] | None:
    """
    Validate a VAT number with the EU VIES REST API.

    :param vat_id: A VAT ID with its country prefix, e.g. ``NL123456789B01``.
    :param transport: Optional httpx transport, for tests.
    :return: ``{"valid": bool, "name": str}``, or ``None`` when VIES couldn't answer (member state down).
    """
    country, number = vat_id[:2], vat_id[2:]
    try:
        async with httpx.AsyncClient(timeout=_VIES_TIMEOUT_S, transport=transport) as client:
            r = await client.post(settings.vies_url, json={"countryCode": country, "vatNumber": number})
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError, json.JSONDecodeError:
        return None
    if "valid" not in data:
        return None
    name = str(data.get("name") or "").strip()
    return {"valid": bool(data["valid"]), "name": "" if name in {"---", "-"} else name[:200]}


def _name_tokens(text: str) -> set[str]:
    cleaned = _NAME_NOISE_RE.sub(" ", text.lower())
    return {t for t in _NON_ALNUM_RE.split(cleaned) if len(t) >= 3}


def name_matches(registered: str, shop_names: list[str]) -> bool:
    """
    Whether the VIES-registered name shares a distinctive word with the shop's name, domain or brand.

    :param registered: The name VIES returned.
    :param shop_names: Names the shop goes by (domain, brand, seller); empty ones are skipped.
    :return: ``True`` on a shared word of at least three letters (legal forms like "B.V." ignored).
    """
    registered_tokens = _name_tokens(registered)
    return any(registered_tokens & _name_tokens(name) for name in shop_names if name)


class EuRegistrySkill(Skill):
    """VAT ID presence and VIES validity, name match, withdrawal right and imprint, for storefronts."""

    name = "eu_registry"
    description = "Checks the shop's VAT ID against EU VIES and the presence of legally required information."
    page_types = STOREFRONTS
    features = ("vat_present", "vat_valid", "vat_name_match", "withdrawal_right", "imprint")

    def __init__(self, *, vies: bool = True) -> None:
        """
        Configure the skill.

        :param vies: Whether to look the VAT ID up in VIES (an outbound request about the shop). The quick tier turns
            it off and keeps the local checks.
        """
        self.vies = vies

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Check the legally required information, and the VAT ID with VIES when network lookups are on.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        legal = " ".join(facts.legal_mentions)
        withdrawal = bool(_WITHDRAWAL_RE.search(legal)) or "refund" in facts.policy_links
        imprint = bool(_IMPRINT_RE.search(legal)) or "imprint" in facts.policy_links
        us = shop_market(facts, state.get("url", ""), state.get("cleaned_content", "")) == "us"
        if not imprint and us:
            # A US shop has no imprint by law; its equivalent is a named company or a US business address.
            found = find_identity(" ".join([state.get("cleaned_content", ""), legal]))
            imprint = bool(found.entity or found.street_addresses)
        features: dict[str, float | bool] = {"withdrawal_right": withdrawal}
        if us:
            # No VAT ID or imprint is required of a US shop, so lacking one says nothing: only what it shows counts.
            features |= {f"{name}.{NOT_APPLICABLE}": True for name in ("vat_present", "vat_valid", "vat_name_match")}
            features["imprint" if imprint else f"imprint.{NOT_APPLICABLE}"] = True
        else:
            features |= {"vat_present": bool(facts.vat_ids), "imprint": imprint}
        observations: list[Observation] = []
        if not withdrawal:
            observations.append(Observation(type="withdrawal_right", evidence="No return or withdrawal policy found."))
        if not imprint and not us:
            observations.append(Observation(type="imprint", evidence="No company identity (imprint) found."))

        if facts.vat_ids and self.vies and settings.network_lookups:
            vat_id = facts.vat_ids[0]
            result = await cached_fact("vies", vat_id, lambda: check_vat(vat_id))
            if result is not None:
                features["vat_valid"] = result["valid"]
                domain = hostname(state.get("url", "")).split(".")[0]
                matches = name_matches(result["name"], [domain, facts.brand, facts.seller])
                features["vat_name_match"] = result["valid"] and matches
                if result["valid"]:
                    who = f" to {result['name']}" if result["name"] else ""
                    observations.append(
                        Observation(type="vat", evidence=f"VAT ID {vat_id} is registered{who} (VIES).")
                    )
                else:
                    observations.append(Observation(type="vat", evidence=f"VAT ID {vat_id} is not valid in VIES."))
        elif facts.vat_ids:
            observations.append(Observation(type="vat", evidence=f"VAT ID shown: {facts.vat_ids[0]}."))
        return self.output(features, observations)
