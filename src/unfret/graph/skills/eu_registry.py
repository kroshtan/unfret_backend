"""
EU merchant identity: a VAT ID checked against the official EU VIES register, and the legally required info.

EU law requires web shops to identify the trader (imprint) and to state the 14-day right of withdrawal; a VAT ID
that VIES knows, under a name matching the shop, is strong evidence of a real business. A shop outside the EU (a US
shop, or one elsewhere: :func:`unfret.market.shop_market`) is required to have none of these, so there a missing
imprint, VAT ID or withdrawal/returns policy is reported as not applicable (neutral for the scorer, no reason for
the shopper) rather than missing; what the page does show still counts as positive evidence, and a named company
(LLC, Inc., Ltd) or a US business address counts as its imprint. Results are cached per VAT number, never per caller.

A VAT number that fails its own check digit was never issued (:mod:`unfret.facts.vat_ids`): when every VAT number on
an EU shop's page is shown whole and is impossible, the page has no VAT number, only a made-up one. That earns no
credit as a VAT number (``vat_present`` is 0) and is reported as not valid (``vat_valid`` 0, a ``vat_fake``
finding) without a lookup. A number the page runs into other digits (a typo, "LT1000011381019") isn't judged, nor
is one from a country without a known check. A bank account that starts like a VAT number (``LT70 4010 …``, an
IBAN) is never looked up, judged or quoted as one; ``vat_present`` still counts it, as the scorer was trained.

Established shops keep their company details on a contact or legal page, not on every product page (ikea.nl,
bever.nl, fonq.nl). So when an EU shop's page shows no imprint but links to its contact page or terms on its own
site, those pages are read too (at most two, cached per shop domain like the other store-level facts): company
details there (a Chamber of Commerce number, a register entry, a VAT number) count as the imprint. Whether they
were read is ``legal_pages_checked``, so the shopper is told "No company details" only when they really aren't
there (:mod:`unfret.shopper`). A network lookup: off in evaluation runs and in the quick tier.
"""

import asyncio
import json
import re
from typing import Any

import httpx

from unfret import shop_pages
from unfret.config import settings
from unfret.fact_cache import cached_fact
from unfret.facts.vat_ids import check_digits_ok, is_iban_at
from unfret.graph.skills import NOT_APPLICABLE, SERVICE_SITES, STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.skills.us_identity import find_identity, names_any_company
from unfret.graph.state import AnalysisState
from unfret.market import shop_market
from unfret.net import hostname
from unfret.price_store import registrable_domain

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
# The shop's own pages that hold its company details when the product page doesn't, in the order they're tried.
_LEGAL_PAGE_KINDS = ("contact", "terms")
_SEPARATOR = r"[\s.]?"


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


def legal_page_urls(policy_links: dict[str, str], page_url: str) -> list[str]:
    """
    The shop's contact and terms pages worth reading for its company details: on its own site, and not a PDF.

    :param policy_links: The page's policy links (kind → URL).
    :param page_url: The analysed page's URL.
    :return: Up to two URLs, contact page first.
    """
    host = hostname(page_url)
    if not host:
        return []
    shop = registrable_domain(host)
    urls = []
    for kind in _LEGAL_PAGE_KINDS:
        url = policy_links.get(kind, "")
        target = hostname(url)
        if target and registrable_domain(target) == shop and not url.lower().split("?")[0].endswith(".pdf"):
            urls.append(url)
    return list(dict.fromkeys(urls))


def shows_imprint(html: str, url: str) -> bool:
    """
    Whether a page names the company behind the shop: a register entry (KvK, Handelsregister, …) or a VAT number.

    :param html: The page.
    :param url: Its URL.
    :return: ``True`` when it does.
    """
    return page_shows_imprint(shop_pages.read_html(html, url))


def page_shows_imprint(page: shop_pages.ReadPage) -> bool:
    """
    Whether one of the shop's pages, as read, names the company behind the shop (:func:`shows_imprint`).

    :param page: The page (:func:`unfret.shop_pages.read`).
    :return: ``True`` when it does.
    """
    return bool(_IMPRINT_RE.search(page.legal) or _IMPRINT_RE.search(page.text) or page.vat)


async def read_legal_pages(urls: list[str]) -> dict[str, Any] | None:
    """
    Read the shop's contact and terms pages for its company details.

    The pages are read through :func:`unfret.shop_pages.read`, so a page the claim audit reads too is fetched once.

    :param urls: From :func:`legal_page_urls`.
    :return: ``{"imprint": bool}``, or ``None`` when no page could be read (not a finding either way).
    """

    async def one(url: str) -> bool | None:
        page = await shop_pages.read(url)
        return None if page is None else page_shows_imprint(page)

    results = await asyncio.gather(*(one(u) for u in urls))
    if all(r is None for r in results):
        return None
    return {"imprint": any(r for r in results)}


def _written(vat_id: str) -> str:
    # The ID as the page may write it: spaces or dots between its characters, as the extraction allows.
    return rf"(?<![A-Z0-9]){_SEPARATOR.join(map(re.escape, vat_id))}"


def shown_whole(vat_id: str, text: str) -> bool:
    """
    Whether the page text shows this VAT number on its own, not run into more digits or letters.

    :param vat_id: An extracted VAT ID (no spaces).
    :param text: The page text.
    :return: ``True`` when it appears whole.
    """
    return re.search(rf"{_written(vat_id)}(?!{_SEPARATOR}[0-9A-Z])", text) is not None


def in_iban(vat_id: str, text: str) -> bool:
    """
    Whether this "VAT number" is the start of a bank account number (an IBAN) on the page.

    :param vat_id: An extracted VAT ID (no spaces).
    :param text: The page text.
    :return: ``True`` when it occurs only as the start of a valid IBAN.
    """
    starts = [m.start() for m in re.finditer(_written(vat_id), text)]
    return bool(starts) and all(is_iban_at(text, start) for start in starts)


def fake_vat(vat_ids: list[str], text: str) -> bool:
    """
    Whether the page's VAT numbers are all impossible ones.

    :param vat_ids: The VAT IDs found on the page.
    :param text: The page text they were found in.
    :return: ``True`` when there is at least one, and every one is shown whole and fails its check digit.
    """
    return bool(vat_ids) and all(shown_whole(v, text) and check_digits_ok(v) is False for v in vat_ids)


def _legal_observations(withdrawal: bool, imprint: bool, *, checked: bool) -> list[Observation]:
    observations = []
    if not withdrawal:
        observations.append(Observation(type="withdrawal_right", evidence="No return or withdrawal policy found."))
    if imprint and checked:
        evidence = "Company details are on the shop's contact or terms page."
        observations.append(Observation(type="imprint_on_legal_page", evidence=evidence))
    if not imprint:
        where = " on the page or its contact and terms pages" if checked else ""
        observations.append(Observation(type="imprint", evidence=f"No company identity (imprint) found{where}."))
    return observations


class EuRegistrySkill(Skill):
    """VAT ID presence and VIES validity, name match, withdrawal right and imprint, for storefronts."""

    name = "eu_registry"
    description = "Checks the shop's VAT ID against EU VIES and the presence of legally required information."
    page_types = STOREFRONTS | SERVICE_SITES
    retry = True
    features = ("vat_present", "vat_valid", "vat_name_match", "withdrawal_right", "imprint", "legal_pages_checked")

    def __init__(self, *, vies: bool = True, legal_pages: bool | None = None) -> None:
        """
        Configure the skill.

        :param vies: Whether to look the VAT ID up in VIES (an outbound request about the shop). The quick tier turns
            it off and keeps the local checks.
        :param legal_pages: Whether to read the shop's contact and terms pages when the page shows no imprint;
            follows ``vies`` unless given.
        """
        self.vies = vies
        self.legal_pages = vies if legal_pages is None else legal_pages

    async def _legal_pages(self, state: AnalysisState) -> dict[str, Any] | None:
        """
        What the shop's contact and terms pages say (cached per shop domain), when allowed and linked.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: :func:`read_legal_pages`' answer, or ``None`` when they weren't (or couldn't be) read.
        """
        if not (self.legal_pages and settings.network_lookups):
            return None
        urls = legal_page_urls(state["facts"].policy_links, state.get("url", ""))
        if not urls:
            return None
        shop = registrable_domain(hostname(state.get("url", "")))
        return await cached_fact("legal_pages", shop, lambda: read_legal_pages(urls))

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
        eu = shop_market(facts, state.get("url", ""), state.get("cleaned_content", "")) == "eu"
        legal_pages = await self._legal_pages(state) if eu and not imprint else None
        imprint = imprint or bool(legal_pages and legal_pages["imprint"])
        if not imprint and not eu:
            # Outside the EU there is no imprint by law; its equivalent is a named company or a US business address.
            # A copyright line names a company but is no imprint, so this reads neither ``copyright_lines`` nor
            # us_identity's choice of the shop's company (the scorer was trained on this reading).
            text = " ".join([state.get("cleaned_content", ""), legal])
            imprint = names_any_company(text) or bool(find_identity(text).street_addresses)
        features: dict[str, float | bool]
        text = " ".join([state.get("cleaned_content", ""), legal])
        vat_ids = [v for v in facts.vat_ids if not in_iban(v, text)]
        fake = eu and fake_vat(vat_ids, text)
        if eu:
            features = {
                "vat_present": bool(facts.vat_ids) and not fake,
                "withdrawal_right": withdrawal,
                "imprint": imprint,
            }
            if legal_pages is not None:
                features["legal_pages_checked"] = True
        else:
            # None of this is required outside the EU, so lacking it says nothing: only what the page shows counts.
            features = {f"{name}.{NOT_APPLICABLE}": True for name in ("vat_present", "vat_valid", "vat_name_match")}
            features["imprint" if imprint else f"imprint.{NOT_APPLICABLE}"] = True
            features["withdrawal_right" if withdrawal else f"withdrawal_right.{NOT_APPLICABLE}"] = True
        observations = _legal_observations(withdrawal, imprint, checked=legal_pages is not None) if eu else []

        if fake:
            features["vat_valid"] = False
            observations.append(
                Observation(type="vat_fake", evidence=f"VAT ID {vat_ids[0]} can't exist: its check digit is wrong.")
            )
        elif vat_ids and self.vies and settings.network_lookups:
            vat_id = vat_ids[0]
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
        elif vat_ids:
            observations.append(Observation(type="vat", evidence=f"VAT ID shown: {vat_ids[0]}."))
        return self.output(features, observations)
