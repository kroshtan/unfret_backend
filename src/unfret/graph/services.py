"""
The verdict for service providers: a company's own site, or its profile on a platform.

Those are ``service_site`` and ``service_listing`` pages. No scorer is trained for them yet, so the band comes
from cautious hand-written rules over the skills' findings, to be replaced by a trained model once labelled pages
come in:

- **Many warning signs** only for the combinations behind fake local businesses: town landing pages without an
  address *and* links to other sites run with the same phone numbers or tracking IDs; a premium-rate number *and*
  payment up front or in cash only; work photos copied from other sites together with doorway pages, operator links
  or a brand-new site; a page saying its tradesperson isn't necessarily local together with another mark of a call
  centre (a trade-and-town name, only a national number, an address in another town, doorway pages, linked sites);
  or three or more serious signals of any kind.
- **Some warning signs** for one serious signal (doorway pages, copied photos, text aimed at AI tools, a VAT
  number that can't exist, a page saying its tradesperson isn't necessarily local) or two of the medium ones
  (payment up front, cash only, price set on the doorstep, a premium-rate number, copied testimonials, a malformed
  KvK number, "licensed" without a licence number, a site named after a trade and a town, only a national number on
  a local page, an address in another town, company details "on request", …). Also for the anonymous local site:
  a trade-and-town name or only a national number on a page that shows no way to tell who the company is (NL: no
  KvK number and no street address; US: no street address, licence number or legal company name). And on a
  platform, a profile without a track record (new, under five reviews) that makes emergency, call-out, payment or
  pricing promises, or gives its company details only on request.
- **Looks fine** only with concrete positives and nothing of medium weight: on a site, an established domain, a
  complete identity (NL: KvK number, company name, street address; US: company name, street address and a licence
  number when it says it is licensed), a street address and no doorway pattern, and no upfront or cash-only terms;
  on a platform profile, two years or more of track record with twenty or more reviews averaging four stars.
- **A few things to check** otherwise, including whenever all we have is something missing (no KvK number found).

The wording is about a business: what the page shows, never an accusation, and never the word "scam". The reasons
and positives come from the copy catalogue (``copy/en.json``), so the extension and ``/check`` show them unchanged.
The headline is the catalogue's template (no LLM writes about a business).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from unfret import shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph.state import AnalysisState, Finding

REASON_SEVERITY: dict[str, shopper.Severity] = {
    "doorway_pages": "high",
    "copied_photo": "high",
    "hidden_ai_text": "high",
    "invalid_vat": "high",
    "timer_resets": "high",
    "not_local": "high",
    "linked_sites": "medium",
    "shared_address_sites": "medium",
    "trade_town_name": "medium",
    "address_elsewhere": "medium",
    "premium_number": "medium",
    "national_number_only": "medium",
    "details_on_request": "medium",
    "unproven_profile": "medium",
    "low_platform_rating": "medium",
    "upfront_payment": "medium",
    "cash_only": "medium",
    "quote_on_site": "medium",
    "new_site": "medium",
    "copied_testimonials": "medium",
    "placeholder_names": "medium",
    "kvk_malformed": "medium",
    "licence_no_number": "medium",
    "pushy_copy": "medium",
    "ai_text": "medium",
    "kvk_missing": "medium",
    "testimonial_dates": "low",
    "all_five_testimonials": "low",
    "names_differ": "low",
    "bait_callout": "low",
    "emergency_pressure": "low",
    "vague_pricing": "low",
    "no_street_address": "low",
    "new_on_platform": "low",
    "few_platform_reviews": "low",
    "low_response_rate": "low",
}
# The positives a service verdict can show (besides the shops' VAT check, ``vat_registered``).
POSITIVE_KEYS = (
    "service_established",
    "service_identity_nl",
    "service_identity_us",
    "licence_shown",
    "written_quote",
    "platform_track_record",
    "platform_verified",
    "service_nothing_found",
)
# Only say something is missing: never more than "a few things to check" on their own.
ABSENCE_REASONS = frozenset({"kvk_missing", "no_street_address"})
# Reasons that tell the same story; only the first of each group is shown.
_GROUPS = (
    ("doorway_pages", "no_street_address"),
    ("kvk_malformed", "kvk_missing"),
    ("hidden_ai_text", "ai_text"),
    ("copied_testimonials", "placeholder_names", "testimonial_dates", "all_five_testimonials"),
    ("premium_number", "national_number_only"),
    ("unproven_profile", "new_on_platform", "few_platform_reviews"),
)
# Payment, pricing and pressure terms: on a profile without a track record, any of them is a warning sign.
_TERMS = frozenset(
    {
        "upfront_payment",
        "cash_only",
        "quote_on_site",
        "bait_callout",
        "emergency_pressure",
        "vague_pricing",
        "premium_number",
        "details_on_request",
    }
)
# The marks of a call centre posing as a local business, which a "not necessarily local" statement confirms.
_CALL_CENTRE = frozenset(
    {
        "trade_town_name",
        "national_number_only",
        "address_elsewhere",
        "doorway_pages",
        "linked_sites",
        "shared_address_sites",
    }
)
_SHARED_ADDRESS_SITES = 2
_RANK = {"high": 0, "medium": 1, "low": 2}
_ESTABLISHED_YEARS = 2
_PLATFORM_YEARS = 2
_PLATFORM_REVIEWS = 20
_PLATFORM_RATING = 4.0
_PLATFORM_NAMES = {
    "werkspot": "Werkspot",
    "thumbtack": "Thumbtack",
    "angi": "Angi",
    "homeadvisor": "HomeAdvisor",
    "checkatrade": "Checkatrade",
    "taskrabbit": "TaskRabbit",
    "bark": "Bark",
    "houzz": "Houzz",
    "myhammer": "MyHammer",
    "homeguide": "HomeGuide",
}


@dataclass
class _Evidence:
    features: dict[str, float]
    findings: dict[str, list[Finding]]
    facts: PageFacts
    page_type: str

    def f(self, name: str) -> float:
        return float(self.features.get(name, 0.0))

    def has(self, name: str) -> bool:
        return name in self.features

    def first(self, finding_type: str) -> Finding | None:
        found = self.findings.get(finding_type)
        return found[0] if found else None

    @property
    def site(self) -> bool:
        return self.page_type == "service_site"


def _listed(evidence: str) -> str:
    return evidence.partition(": ")[2].rstrip(".")[:160]


def _quoted(finding: Finding | None) -> str:
    return finding.evidence[:160] if finding is not None else "…"


def _site_reasons(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """A company's own site: doorway pages, operator links, identity, domain age."""
    if e.f("doorway_pages.doorway") >= 1:
        fired["doorway_pages"] = {"count": str(int(e.f("doorway_pages.city_pages")))}
    linked = e.first("shared_identifier")
    if linked is not None and e.f("shop_links.linked_shops") >= 1:
        count = int(e.f("shop_links.linked_shops"))
        names = ", ".join(_listed(f.evidence) for f in e.findings.get("shared_identifier", [])[:1])
        fired["linked_sites"] = {"count": str(count), "sites": "site" if count == 1 else "sites", "names": names}
    if e.f("service_identity.kvk_malformed") >= 1:
        fired["kvk_malformed"] = {}
    elif e.has("service_identity.kvk_shown") and e.f("service_identity.kvk_shown") < 1:
        fired["kvk_missing"] = {}
    if e.f("service_identity.licence_claim_no_number") >= 1:
        fired["licence_no_number"] = {}
    if e.f("service_identity.names_differ") >= 1 and (two := e.first("names_differ")):
        fired["names_differ"] = {"names": two.evidence.partition(": ")[2].rstrip(".")}
    if e.has("service_identity.address_shown") and e.f("service_identity.address_shown") < 1:
        fired["no_street_address"] = {}
    if e.f("domain_age.under_6_months") >= 1:
        days = max(round(e.f("domain_age.age_years") * 365), 1)
        fired["new_site"] = {"age": _age(days)}
    if e.has("eu_registry.vat_valid") and e.f("eu_registry.vat_valid") < 1:
        fired["invalid_vat"] = {}
    _call_centre(e, fired)


def _call_centre(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """The single-page marks of a call centre posing as a local business."""
    if e.f("doorway_pages.not_local") >= 1:
        fired["not_local"] = {"quote": _quoted(e.first("not_local"))}
    # A keyword domain is worth noting only when the site doesn't say which company it is.
    complete = e.f("service_identity.complete") >= 1
    if e.f("doorway_pages.trade_town_name") >= 1 and not complete and (named := e.first("trade_town_name")):
        fired["trade_town_name"] = {"site": named.evidence.partition(": ")[2].rstrip(".")}
    if e.f("doorway_pages.national_number_only") >= 1:
        fired["national_number_only"] = {}
    if e.f("doorway_pages.address_elsewhere") >= 1 and (where := e.first("address_elsewhere")):
        fired["address_elsewhere"] = {"where": where.evidence.rstrip(".")}
    if e.f("shop_links.shared_address_shops") >= _SHARED_ADDRESS_SITES:
        fired["shared_address_sites"] = {"count": str(int(e.f("shop_links.shared_address_shops")))}


def _anonymous(e: _Evidence) -> bool:
    """A site that shows no way to tell who the company is (NL: no KvK, no street address; US: nor a licence)."""
    no_address = e.has("service_identity.address_shown") and e.f("service_identity.address_shown") < 1
    if e.f("service_identity.market_nl") >= 1:
        return no_address and e.has("service_identity.kvk_shown") and e.f("service_identity.kvk_shown") < 1
    return (
        e.f("service_identity.market_us") >= 1
        and no_address
        and e.f("service_identity.licence_shown") < 1
        and not e.facts.legal_form
    )


def _age(days: int) -> str:
    units = shopper.catalogue()["age_units"]
    for limit, size, unit in ((14, 1, "day"), (60, 7, "week"), (730, 30, "month")):
        if days < limit:
            n = max(1, round(days / size))
            return f"{n} {units[unit][0 if n == 1 else 1]}"
    n = round(days / 365)
    return f"{n} {units['year'][0 if n == 1 else 1]}"


def _terms(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Payment, quote and call-out terms, emergency pressure, premium-rate numbers."""
    for feature, key in (
        ("details_on_request", "details_on_request"),
        ("premium_number", "premium_number"),
        ("upfront_payment", "upfront_payment"),
        ("cash_only", "cash_only"),
        ("quote_on_site", "quote_on_site"),
        ("bait_callout", "bait_callout"),
        ("emergency_pressure", "emergency_pressure"),
        ("vague_pricing", "vague_pricing"),
    ):
        if e.f(f"service_terms.{feature}") >= 1:
            fired[key] = {"quote": _quoted(e.first(feature))}


def _testimonials(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Copied, templated or bunched testimonials."""
    count = str(int(e.f("testimonials.count")))
    if e.f("testimonials.copied_share") >= 0.5:
        fired["copied_testimonials"] = {"count": count}
    if e.f("testimonials.placeholder_names") >= 1:
        fired["placeholder_names"] = {}
    if e.f("testimonials.dates_clustered") >= 1:
        fired["testimonial_dates"] = {"count": count}
    if e.f("testimonials.all_five") >= 1:
        fired["all_five_testimonials"] = {"count": count}


def _everywhere(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Copied photos, pressure, text aimed at AI tools, and a platform profile's track record."""
    photo = e.first("photo_on_other_shops")
    if photo is not None and e.f("reverse_image.other_shops") >= 1:
        count = int(e.f("reverse_image.other_shops"))
        fired["copied_photo"] = {
            "count": str(count),
            "sites": "site" if count == 1 else "sites",
            "names": _listed(photo.evidence),
            "thumbnails": " ".join(photo.thumbnails),
        }
    if e.f("dark_patterns.timer_resets") >= 1:
        fired["timer_resets"] = {}
    if e.f("content.scam_copy") >= 0.6:
        fired["pushy_copy"] = {}
    if e.f("injection.instruction_hidden") >= 1:
        fired["hidden_ai_text"] = {}
    # Only the quoted phrase the injection skill found, as on product pages: the content read's yes/no
    # ``addresses_ai`` has no quote to show and fires on ordinary text.
    if e.f("injection.instruction_visible") >= 1:
        fired["ai_text"] = {}
    _profile(e, fired)


def _profile(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """A platform profile's track record: new, few reviews, a low rating, slow to answer."""
    if e.f("platform_profile.unproven") >= 1:
        fired["unproven_profile"] = {"count": str(int(e.f("platform_profile.rating_count")))}
    if e.f("platform_profile.low_rating") >= 1 and e.facts.seller_profile is not None:
        fired["low_platform_rating"] = {
            "rating": f"{e.facts.seller_profile.rating or 0:g}",
            "count": f"{e.facts.seller_profile.rating_count or 0:,}",
        }
    if e.f("platform_profile.new_member") >= 1:
        fired["new_on_platform"] = {}
    if e.f("platform_profile.few_reviews") >= 1:
        fired["few_platform_reviews"] = {"count": str(int(e.f("platform_profile.rating_count")))}
    if e.f("platform_profile.low_response_rate") >= 1 and e.facts.seller_profile is not None:
        fired["low_response_rate"] = {"pct": f"{e.facts.seller_profile.response_rate or 0:g}%"}


def _band(e: _Evidence, fired: dict[str, dict[str, str]]) -> shopper.Band:
    concrete = {k for k in fired if k not in ABSENCE_REASONS}
    serious = {k for k in concrete if REASON_SEVERITY[k] in {"high", "medium"}}
    combos = (
        {"doorway_pages", "linked_sites"} <= concrete,
        "premium_number" in concrete and bool({"upfront_payment", "cash_only"} & concrete),
        "copied_photo" in concrete and bool({"doorway_pages", "linked_sites", "new_site"} & concrete),
        "not_local" in concrete and bool(_CALL_CENTRE & concrete),
    )
    if any(combos) or len(serious) >= 3:
        return "junk_or_scam"
    warning_combos = (
        e.site and bool({"trade_town_name", "national_number_only"} & concrete) and _anonymous(e),
        "unproven_profile" in concrete and bool(_TERMS & concrete),
    )
    if any(REASON_SEVERITY[k] == "high" for k in concrete) or len(serious) >= 2 or any(warning_combos):
        return "careful"
    if serious or len([k for k in fired if REASON_SEVERITY[k] == "low"]) > 1 or fired.keys() & ABSENCE_REASONS:
        return "concerns"
    return "reliable" if _positives(e) and _fine(e) else "concerns"


def _fine(e: _Evidence) -> bool:
    """The concrete positives a "looks fine" needs."""
    if e.site:
        return (
            e.f("domain_age.age_years") >= _ESTABLISHED_YEARS
            and e.f("service_identity.complete") >= 1
            and e.f("doorway_pages.street_address") >= 1
            and e.f("doorway_pages.doorway") < 1
            and e.f("service_terms.upfront_payment") < 1
            and e.f("service_terms.cash_only") < 1
        )
    profile = e.facts.seller_profile
    years = max(e.f("platform_profile.member_years"), e.f("platform_profile.years_in_business"))
    return (
        profile is not None
        and years >= _PLATFORM_YEARS
        and e.f("platform_profile.rating_count") >= _PLATFORM_REVIEWS
        and e.f("platform_profile.rating") >= _PLATFORM_RATING
        and e.f("service_terms.upfront_payment") < 1
        and e.f("service_terms.cash_only") < 1
    )


def _positives(e: _Evidence) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    if e.site and e.f("domain_age.age_years") >= _ESTABLISHED_YEARS:
        found["service_established"] = {"years": str(int(e.f("domain_age.age_years")))}
    if e.f("service_identity.complete") >= 1 and e.f("service_identity.market_nl") >= 1:
        found["service_identity_nl"] = {}
    named = e.first("company")
    if e.f("service_identity.complete") >= 1 and e.f("service_identity.market_us") >= 1 and named:
        found["service_identity_us"] = {"company": named.evidence.removeprefix("Company: ").rstrip(".")}
    licence = e.first("licence")
    if e.f("service_identity.licence_shown") >= 1 and licence:
        found["licence_shown"] = {"licence": _listed(licence.evidence)}
    if e.f("eu_registry.vat_valid") >= 1:
        found["vat_registered"] = {}
    if e.f("service_terms.written_quote") >= 1:
        found["written_quote"] = {}
    profile = e.facts.seller_profile
    if not e.site and profile is not None and _fine(e) and profile.rating is not None:
        since = profile.member_since.year if profile.member_since else None
        found["platform_track_record"] = {
            "platform": _PLATFORM_NAMES.get(e.facts.platform, "the platform"),
            "since": f" since {since}" if since else "",
            "count": f"{profile.rating_count or 0:,}",
            "rating": f"{profile.rating:g}",
        }
    if profile is not None and e.f("platform_profile.verified_badges") >= 1:
        found["platform_verified"] = {"badges": ", ".join(profile.badges[:3])}
    return found


def _scores() -> dict[shopper.Band, int]:
    reliable, concerns, careful = (
        settings.verdict_reliable_min,
        settings.verdict_concerns_min,
        settings.verdict_careful_min,
    )
    return {
        "reliable": min(max(85, reliable), 100),
        "concerns": (concerns + reliable - 1) // 2,
        "careful": (careful + concerns - 1) // 2,
        "junk_or_scam": max(careful // 2, 0),
    }


def _reason(key: str, params: dict[str, str]) -> shopper.Reason:
    copy = shopper.catalogue()["reasons"][key]
    return shopper.Reason(
        type=key,
        title=copy["title"],
        detail=copy["detail"].format(**params),
        severity=REASON_SEVERITY[key],
        thumbnails=params.get("thumbnails", "").split(),
    )


def _positive(key: str, params: dict[str, str]) -> shopper.Positive:
    copy = shopper.catalogue()["positives"][key]
    return shopper.Positive(type=key, title=copy["title"], detail=copy["detail"].format(**params))


def build(
    *, features: dict[str, float], findings: list[Finding], facts: PageFacts, page_type: str
) -> tuple[int, shopper.Verdict]:
    """
    The verdict for a service provider's page.

    :param features: The skill features.
    :param findings: The skill findings.
    :param facts: The page facts.
    :param page_type: ``service_site`` or ``service_listing``.
    :return: The band's score and the verdict.
    """
    by_type: dict[str, list[Finding]] = {}
    for finding in findings:
        by_type.setdefault(finding.type, []).append(finding)
    e = _Evidence(features, by_type, facts, page_type)
    fired: dict[str, dict[str, str]] = {}
    if e.site:
        _site_reasons(e, fired)
    _terms(e, fired)
    _testimonials(e, fired)
    _everywhere(e, fired)
    band = _band(e, fired)
    for group in _GROUPS:
        for key in [k for k in group if k in fired][1:]:
            del fired[key]
    order = list(REASON_SEVERITY)
    keys = sorted(fired, key=lambda k: (_RANK[REASON_SEVERITY[k]], order.index(k)))
    reasons = [_reason(k, fired[k]) for k in keys]
    positives = [_positive(k, v) for k, v in _positives(e).items()]
    if band == "reliable":
        reasons = reasons[:1]
        positives = positives[:3] or ([] if reasons else [_positive("service_nothing_found", {})])
    else:
        reasons = reasons[:3]
        positives = positives[: 2 if band == "concerns" else 1]
    copy = shopper.catalogue()
    headline = shopper.template_headline(band, keys[:2], reasons, copy | {"headlines": copy["service_headlines"]})
    verdict = shopper.Verdict(
        band=band, label=copy["labels"][band], headline=headline, reasons=reasons, positives=positives
    )
    return _scores()[band], verdict


def service_verdict(state: AnalysisState) -> dict[str, Any]:
    """
    The verdict for a service provider's page, from the graph state.

    :param state: The graph state after the class's skills.
    :return: ``score``, ``explanation`` and ``verdict`` (no ``assessment``: there is no scorer).
    """
    facts = state.get("facts") or PageFacts()
    page_type = state.get("page_type", "service_site")
    score, verdict = build(
        features=state.get("features", {}), findings=state.get("findings", []), facts=facts, page_type=page_type
    )
    if page_type == "service_listing" and nothing_read(facts, verdict):
        # A platform page without the provider's profile (Houzz's frame without its data): "a few things to check"
        # would be about nothing. Not analysed, so not charged; the extension reads the page the user sees.
        return {"score": -1, "explanation": PROFILE_NOT_READ}
    return {"score": score, "explanation": verdict.headline, "verdict": verdict.model_dump()}


PROFILE_NOT_READ = "The provider's profile on this page couldn't be read, so it wasn't checked."


def nothing_read(facts: PageFacts, verdict: shopper.Verdict) -> bool:
    """
    Whether a platform profile check found nothing at all to go on: no profile, no reviews, no reason, no positive.

    :param facts: The page facts.
    :param verdict: The verdict built from them.
    :return: ``True`` when the verdict would be about nothing.
    """
    return facts.seller_profile is None and not facts.reviews and not verdict.reasons and not verdict.positives


def make_service_judge() -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    """
    The service judge as a graph node.

    :return: The node.
    """

    async def judge(state: AnalysisState) -> dict[str, Any]:
        """
        Judge a service provider's page (see :func:`service_verdict`).

        :param state: Current graph state.
        :return: ``score``, ``explanation`` and ``verdict``.
        """
        return service_verdict(state)

    return judge


def suspicion(features: dict[str, float]) -> bool:
    """
    Whether a service provider's page is worth a look-up of its photo (the triage's rule; no LLM).

    :param features: The cheap skills' features.
    :return: ``True`` on doorway pages, operator links, a brand-new site, copied testimonials, payment up front,
        cash only or a premium-rate number, a new platform profile, or a page saying its tradesperson isn't local.
    """
    f = features.get
    return any(
        (
            f("doorway_pages.doorway", 0.0) >= 1,
            f("shop_links.linked_shops", 0.0) >= 1,
            f("domain_age.under_6_months", 0.0) >= 1,
            f("testimonials.copied_share", 0.0) >= 0.5,
            f("service_terms.upfront_payment", 0.0) >= 1,
            f("service_terms.cash_only", 0.0) >= 1,
            f("service_terms.premium_number", 0.0) >= 1,
            f("platform_profile.new_member", 0.0) >= 1,
            f("doorway_pages.not_local", 0.0) >= 1,
        )
    )
