"""
Manipulative interface patterns: fake countdown timers, pre-ticked paid add-ons and drip pricing.

Pre-ticked boxes for extra payments are prohibited in the EU (Consumer Rights Directive, art. 22), so that one is
reported as a plain fact. Drip pricing is fully visible only at checkout; when the caller supplies the checkout
page (an agent can), its total is compared with the listed price.
"""

import re

from bs4 import BeautifulSoup

from veridex.facts._parse import parse_price
from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState

_ADDON_RE = re.compile(
    r"insurance|protect|warrant|guarantee|priority|express|donat|gift ?wrap|tip\b|verzeker|bescherm|garantie"
    r"|versicherung|schutz|assurance|seguro|assicura|\+\s*[$€£]|[$€£]\s*\d",
    re.IGNORECASE,
)
_CONSENT_RE = re.compile(r"newsletter|terms|privacy|remember|subscribe|agree|voorwaarden|agb|cookie", re.IGNORECASE)
# A script countdown that keeps its deadline per visitor (storage) or makes one up (random) restarts for everyone.
# Cookies alone prove nothing: every big site's scripts mention them.
_EVERGREEN_RE = re.compile(r"localstorage|sessionstorage|math\.random|evergreen", re.IGNORECASE)
# The extension reads each countdown twice; this note means it went up between the readings.
_LIVE_RESTART = "[live: countdown restarted"
_LIVE_RESTART_RE = re.compile(r"\[live: countdown restarted \(([\d:]+) → ([\d:]+)\)")
# Platform deal widgets (Amazon lightning / limited-time deals, bol.com deals) count down to real deadlines.
_PLATFORM_TIMER_RE = re.compile(
    r"deals?_countdown|dealbadge|lightning|limited-time-deal|tijdelijke deal|blitzangebot|offre [ée]clair"
    r"|oferta flash|offerta lampo|deal of the day|dotd|dagdeal|bol-?deal",
    re.IGNORECASE,
)
# Unrendered template text (a screen-reader label template, not a running timer).
_TEMPLATE_RE = re.compile(r"NO_OF_[A-Z]+|\{\{|\$\{")


def shop_timers(timers: list[str], page_type: str) -> list[str]:
    """
    The countdowns that are the shop's own.

    None on marketplace listings (sellers can't add timers there, so they are the platform's real deal countdowns),
    and never platform deal widgets or unrendered template text.

    :param timers: ``PageFacts.timers``.
    :param page_type: The classified page type.
    :return: The timers worth reporting.
    """
    if page_type == "aggregate_listing":
        return []
    return [t for t in timers if not _PLATFORM_TIMER_RE.search(t) and not _TEMPLATE_RE.search(t)]


_TOTAL_RE = re.compile(
    r"(?:order total|grand total|total due|total to pay|totaal|gesamtsumme|gesamtbetrag|total)\s*:?\s*"
    r"((?:[A-Z]{3}|[$€£])?\s*\d[\d., ]*\s*(?:[A-Z]{3}|[$€£])?)",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def checkout_total(html: str) -> float | None:
    """
    The order total on a checkout page: the last amount labelled as a total.

    :param html: The checkout page HTML.
    :return: The total, or ``None`` when none is labelled.
    """
    text = _WS_RE.sub(" ", BeautifulSoup(html, "html.parser").get_text(" "))
    totals = [parse_price(m.group(1))[0] for m in _TOTAL_RE.finditer(text)]
    found = [t for t in totals if t is not None]
    return found[-1] if found else None


class DarkPatternsSkill(Skill):
    """Countdown timers (and whether they reset), pre-ticked paid add-ons, and hidden or late fees."""

    name = "dark_patterns"
    description = "Detects fake countdown timers, pre-ticked paid add-ons and drip pricing."
    features = ("timer", "timer_resets", "preticked_addons", "fee_mentions", "checkout_markup_pct")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Inspect the timers, checkboxes and fee wording in ``facts`` (and the checkout page, if supplied).

        :param state: Current graph state; uses ``facts`` and the optional ``checkout_content``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        observations: list[Observation] = []

        timers = shop_timers(facts.timers, state.get("page_type", "storefront"))
        resets = any(
            _LIVE_RESTART in t or (t.startswith("script:") and _EVERGREEN_RE.search(t) is not None) for t in timers
        )
        if timers:
            live = next((m for t in timers if (m := _LIVE_RESTART_RE.search(t))), None)
            if live:
                # The extension saw it go up between two readings: say so in words, not markup.
                evidence = f"went up from {live.group(1)} to {live.group(2)} while it was watched"
            else:
                evidence = timers[0][:200]
            kind = "A countdown timer that restarts" if resets else "A countdown timer"
            observations.append(Observation(type="timer", evidence=f"{kind}: {evidence}"))

        preticked = [
            c for c in facts.checkboxes if c.checked and _ADDON_RE.search(c.label) and not _CONSENT_RE.search(c.label)
        ]
        observations += [
            Observation(type="preticked_addon", evidence=f'Pre-ticked paid add-on: "{c.label[:150]}".')
            for c in preticked
        ]
        observations += [Observation(type="fee", evidence=f[:200]) for f in facts.fee_mentions[:3]]

        markup = 0.0
        if (checkout := state.get("checkout_content")) and facts.price:
            total = checkout_total(checkout)
            if total is not None and total > facts.price:
                markup = (total - facts.price) / facts.price
                if markup >= 0.1:
                    observations.append(
                        Observation(
                            type="drip_pricing",
                            evidence=f"Listed at {facts.price:g}, checkout total {total:g} (+{markup:.0%}).",
                        )
                    )

        return self.output(
            {
                "timer": bool(timers),
                "timer_resets": resets,
                "preticked_addons": len(preticked),
                "fee_mentions": min(len(facts.fee_mentions), 5),
                "checkout_markup_pct": markup,
            },
            observations,
        )
