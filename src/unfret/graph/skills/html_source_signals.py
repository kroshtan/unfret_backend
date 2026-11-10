"""
Known apps and supplier traces in the page source, by pattern: deterministic, fast, high precision.

The page source is the raw HTML when the server has it, plus the script URLs and meta tags of the page facts, which
is all the browser extension sends (it never uploads raw HTML). Matching lower-cases the source once and uses plain
lower-case patterns, which is several times faster than case-insensitive regexes on a 2 MB page, and runs off the
event loop.
"""

import asyncio
import re

from unfret.facts import PageFacts
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

# (category, pattern, label). Patterns are lower-case and matched against the lower-cased source.
_SIGNALS: list[tuple[str, re.Pattern[str], str]] = [
    (cat, re.compile(pattern), label)
    for cat, pattern, label in [
        # Dropshipping fulfilment apps and platforms
        ("dropship_app", r"oberlo", "Oberlo (Shopify dropship fulfilment)"),
        ("dropship_app", r"dsers[.\-/]", "DSers (AliExpress order management)"),
        ("dropship_app", r"autods", "AutoDS (automated dropshipping)"),
        ("dropship_app", r"spocket\.co", "Spocket (dropship supplier marketplace)"),
        ("dropship_app", r"dropified", "Dropified (dropshipping platform)"),
        ("dropship_app", r"cjdropshipping|cjpacket", "CJ Dropshipping / CJPacket"),
        ("dropship_app", r"zendrop", "Zendrop (dropshipping platform)"),
        ("dropship_app", r"eprolo", "Eprolo (dropshipping platform)"),
        ("dropship_app", r"yakkyofy", "Yakkyofy (dropshipping platform)"),
        ("dropship_app", r"syncee", "Syncee (dropship supplier sync)"),
        ("dropship_app", r"modalyst", "Modalyst (dropship supplier marketplace)"),
        ("dropship_app", r"sup-?dropshipping", "Sup Dropshipping"),
        ("dropship_app", r"dropshipman", "Dropshipman"),
        ("dropship_app", r"hyperspace-?ai|hyperspace\.ai", "Hyperspace AI (dropship product import)"),
        # Supplier marketplaces and their CDNs
        ("supplier_trace", r"ae\d{2}\.alicdn\.com", "AliExpress image CDN (aeNN.alicdn.com)"),
        ("supplier_trace", r"aliexpress\.(?:com|us)/item/", "Direct AliExpress product link"),
        ("supplier_trace", r"img\.kwcdn\.com|temu\.com/", "Temu link or image CDN"),
        ("supplier_trace", r"dhgate\.com/product", "DHgate product link"),
        ("supplier_trace", r"alibaba\.com/product-detail", "Alibaba product link"),
        ("supplier_trace", r"cbu01\.alicdn\.com", "1688.com image CDN"),
        # Review apps that import reviews from AliExpress
        ("review_import_app", r"loox\.io|loox-reviews", "Loox (reviews with AliExpress import)"),
        ("review_import_app", r"ali-?reviews|alireviews", "Ali Reviews (AliExpress review import)"),
        ("review_import_app", r"rivyo", "Rivyo (AliExpress review import)"),
        ("review_import_app", r"ryviu", "Ryviu (AliExpress review import)"),
        ("review_import_app", r"vitals\.co|vitals-app", "Vitals (bundle incl. AliExpress review import)"),
        # Paid "shipping protection" add-ons, often pre-ticked
        ("shipping_protection_app", r"route\.com/|routeapp|route-widget", "Route shipping protection"),
        ("shipping_protection_app", r"seel\.com|seel-widget|seel_", "Seel worry-free purchase"),
        ("shipping_protection_app", r"shipinsure", "ShipInsure shipping protection"),
        ("shipping_protection_app", r"extend\.com/|helloextend", "Extend product protection"),
        ("shipping_protection_app", r"corso\.com|corso-", "Corso shipping protection"),
        # Countdown / urgency apps
        ("timer_app", r"hurrify", "Hurrify countdown timer"),
        ("timer_app", r"essential-?countdown|essential_countdown", "Essential Countdown Timer"),
        ("timer_app", r"countdown-?timer-?bar|timer-?bar\.", "Countdown timer bar app"),
        ("timer_app", r"urgency-?bear|urgencybear", "Urgency Bear"),
        ("timer_app", r"sales-?pop|salespop|fomo\.com|proof-?factor", "Fake 'someone just bought' pop-ups"),
    ]
]
_CATEGORIES = ("dropship_app", "supplier_trace", "review_import_app", "shipping_protection_app", "timer_app")


def page_source(html: str, facts: PageFacts) -> str:
    """
    Everything the patterns are matched against, lower-cased once.

    :param html: The raw HTML (or the cleaned text an extension sent).
    :param facts: The page facts; their script URLs and meta tags carry the app traces when there is no HTML.
    :return: The lower-cased source.
    """
    extra = "\n".join([*facts.script_srcs, *(f"{k}={v}" for k, v in facts.meta.items())])
    return f"{html}\n{extra}".lower()


def scan(source: str) -> list[tuple[str, str]]:
    """
    The (category, label) pairs whose pattern occurs in ``source``.

    :param source: From :func:`page_source`.
    :return: The hits, in catalogue order.
    """
    return [(category, label) for category, pattern, label in _SIGNALS if pattern.search(source)]


class HtmlSourceSignalsSkill(Skill):
    """Scan the raw HTML for dropship apps, supplier CDNs, review importers, protection and urgency apps."""

    name = "html_source_signals"
    description = "Scans page HTML source for dropship apps, supplier links, review import and urgency apps."
    features = _CATEGORIES

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Match the known patterns against the raw HTML and the page facts' script URLs and meta tags.

        :param state: Current graph state; uses ``page_content`` and ``facts``.
        :return: Counts per category and one finding per distinct hit.
        """
        html, facts = state["page_content"], state["facts"]
        hits = await asyncio.to_thread(lambda: scan(page_source(html, facts)))
        counts = {category: sum(1 for c, _ in hits if c == category) for category in _CATEGORIES}
        observations = [Observation(type=category, evidence=label) for category, label in hits]
        return self.output(counts, observations)
