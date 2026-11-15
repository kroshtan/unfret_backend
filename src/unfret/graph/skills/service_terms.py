"""
A service provider's payment and pricing terms: what the page says about paying, quoting and turning up.

The patterns behind most complaints about emergency trades (locksmiths, plumbers, pest control): pay in full
before the work, cash only, a call-out "from €0" that turns into a large bill, a price set only once the technician
is at the door, and "24/7, there in 20 minutes" pressure, often with a premium-rate (0900, 1-900) phone number. A
written quote or fixed price up front is the reassuring opposite. Read from the page text and the pricing and
promise snippets in the facts (on a platform profile, from the profile's own text only).
"""

import re

from unfret.graph.skills import SERVICE_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

_UPFRONT_RE = re.compile(
    r"(?:vooraf|vooruit)\s+(?:te\s+)?betalen|vooruitbetaling|betaling\s+vooraf|betalen\s+voor(?:dat|af)\s+"
    r"|(?:pay|payment)(?:\s+is)?(?:\s+due)?\s+(?:in\s+full\s+)?(?:upfront|up\s+front|in\s+advance"
    r"|before\s+(?:we\s+start|work\s+starts|the\s+work|starting|any\s+work))"
    r"|(?:full|100\s?%)\s+(?:payment|betaling)\s+(?:upfront|up\s+front|in\s+advance|vooraf)",
    re.IGNORECASE,
)
_DEPOSIT_RE = re.compile(
    r"(\d{2,3})\s?%\s+(?:aanbetaling|deposit|vooraf|upfront|down\s+payment|in\s+advance)"
    r"|(?:aanbetaling|deposit|down\s+payment)\s+(?:van|of)\s+(\d{2,3})\s?%",
    re.IGNORECASE,
)
_CASH_ONLY_RE = re.compile(
    r"cash\s+only|only\s+(?:accept\s+)?cash|cash\s+payment\s+only|(?:alleen|uitsluitend|enkel)\s+(?:contant|cash)"
    r"|contant\s+betalen\s+verplicht",
    re.IGNORECASE,
)
_CALLOUT = r"call-?out(?:\s+(?:fee|charge))?|voorrijkosten|service\s+call|trip\s+(?:fee|charge)"
_BAIT_CALLOUT_RE = re.compile(
    rf"(?:{_CALLOUT})\s*(?:from|vanaf|van|:)?\s*(?:€|\$)\s?(\d{{1,2}})(?:[.,]\d\d)?(?![0-9.,])"
    rf"|(?:€|\$)\s?(\d{{1,2}})(?:[.,]\d\d)?\s*(?:{_CALLOUT})"
    rf"|(?:geen|no|free|gratis)\s+(?:{_CALLOUT})",
    re.IGNORECASE,
)
_BAIT_MAX = 29  # a call-out fee this low (or none) is the advertised bait, not the bill
_QUOTE_ON_SITE_RE = re.compile(
    r"prijs\s+(?:wordt\s+)?ter\s+plaatse\s+(?:bepaald|vastgesteld|afgesproken)"
    r"|(?:price|prices|cost)\s+(?:is\s+)?(?:quoted|determined|given|set|confirmed)\s+on\s+(?:site|arrival)"
    r"|quote\s+on\s+arrival|geen\s+offerte\s+(?:nodig|vooraf)"
    r"|no\s+(?:written\s+)?(?:quotes?|estimates?)\s+(?:needed|required|necessary)",
    re.IGNORECASE,
)
_VAGUE_RE = re.compile(
    r"prijs\s+op\s+aanvraag|price\s+on\s+request|call\s+for\s+(?:a\s+)?price|bel\s+voor\s+(?:de\s+)?prijs"
    r"|prices\s+vary",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:€|\$)\s?\d{2,}")
_WRITTEN_QUOTE_RE = re.compile(
    r"schriftelijke\s+offerte|offerte\s+vooraf|vaste\s+prijs|written\s+(?:quote|estimate)|fixed\s+price"
    r"|upfront\s+pricing|free\s+(?:written\s+)?estimates?|gratis\s+offerte|vrijblijvende\s+offerte",
    re.IGNORECASE,
)
_ALWAYS_OPEN_RE = re.compile(r"24/7|24 uur|24 hours|24h(?![a-z0-9])|dag en nacht|day or night", re.IGNORECASE)
_FAST_RE = re.compile(r"(?:within|in|binnen)\s+(\d{1,2})\s+(?:minutes|minuten|min)(?![a-z])", re.IGNORECASE)
_FAST_MAX = 30


def _quote(m: re.Match[str], text: str) -> str:
    lo = max(text.rfind(".", 0, m.start()) + 1, m.start() - 80)
    hi = text.find(".", m.end())
    hi = min(hi if hi >= 0 else len(text), m.end() + 80)
    return '"' + " ".join(text[lo:hi].split())[:150] + '"'


def _large_deposit(text: str) -> re.Match[str] | None:
    # A deposit of half the price or more ("50% aanbetaling", "a deposit of 60%").
    return next((m for m in _DEPOSIT_RE.finditer(text) if int(m.group(1) or m.group(2)) >= 50), None)


def _bait(text: str) -> re.Match[str] | None:
    # A call-out fee of nothing or next to nothing ("voorrijkosten €0", "$19 service call", "no call-out fee").
    for m in _BAIT_CALLOUT_RE.finditer(text):
        amount = m.group(1) or m.group(2)
        if amount is None or int(amount) <= _BAIT_MAX:
            return m
    return None


class ServiceTermsSkill(Skill):
    """Upfront payment, cash only, bait call-out fees, prices set on the doorstep, emergency pressure, 0900 numbers."""

    name = "service_terms"
    description = "Reads a service provider's payment, quote and call-out terms and its emergency promises."
    page_types = SERVICE_PAGES
    features = (
        "upfront_payment",
        "cash_only",
        "bait_callout",
        "quote_on_site",
        "vague_pricing",
        "written_quote",
        "emergency_pressure",
        "premium_number",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Match the terms in the page text and the facts' pricing and promise snippets.

        :param state: Current graph state; uses ``facts``, ``cleaned_content`` and ``page_type``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        parts = [state.get("cleaned_content", "")]
        if state.get("page_type") == "service_site":
            parts += [*facts.pricing_terms, *facts.promises]
        text = " ".join(" ".join(parts).split())
        observations: list[Observation] = []

        def found(pattern: re.Pattern[str], kind: str) -> re.Match[str] | None:
            m = pattern.search(text)
            if m:
                observations.append(Observation(type=kind, evidence=_quote(m, text)))
            return m

        upfront = found(_UPFRONT_RE, "upfront_payment") is not None
        if not upfront and (deposit := _large_deposit(text)):
            upfront = True
            observations.append(Observation(type="upfront_payment", evidence=_quote(deposit, text)))
        cash_only = found(_CASH_ONLY_RE, "cash_only") is not None
        bait = _bait(text)
        if bait:
            observations.append(Observation(type="bait_callout", evidence=_quote(bait, text)))
        on_site = found(_QUOTE_ON_SITE_RE, "quote_on_site") is not None
        vague = (
            not on_site
            and not any(_AMOUNT_RE.search(t) for t in facts.pricing_terms)
            and found(_VAGUE_RE, "vague_pricing") is not None
        )
        written = found(_WRITTEN_QUOTE_RE, "written_quote") is not None
        fast = [m for m in _FAST_RE.finditer(text) if int(m.group(1)) <= _FAST_MAX]
        emergency = bool(fast) and _ALWAYS_OPEN_RE.search(text) is not None
        if emergency:
            observations.append(Observation(type="emergency_pressure", evidence=_quote(fast[0], text)))
        premium = [p.number for p in facts.phones if p.kind == "premium"]
        if premium:
            observations.append(Observation(type="premium_number", evidence="Gives a premium-rate phone number."))
        return self.output(
            {
                "upfront_payment": upfront,
                "cash_only": cash_only,
                "bait_callout": bait is not None,
                "quote_on_site": on_site,
                "vague_pricing": vague,
                "written_quote": written,
                "emergency_pressure": emergency,
                "premium_number": bool(premium),
            },
            observations,
        )
