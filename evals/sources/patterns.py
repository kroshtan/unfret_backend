"""
Documented scam and normal patterns for the new page classes, each tied to the public pages that describe it.

The synthetic peer-to-peer ads and service pages (:mod:`evals.sources.p2p_ads`, :mod:`evals.sources.service_pages`)
are built only from patterns that a public consumer-protection page documents: Fraudehelpdesk warnings, the
Marktplaats safety centre, ACM decisions and news, Consumentenbond and Kassa articles, and FTC consumer advice.
Each pattern lists its sources and a few phrases (regular expressions, case-insensitive) that the source's text must
contain for the source to count as confirming it.

``uv run python -m evals.sources.patterns`` fetches every source (robots.txt respected, ~1 request a second, public
addresses only) and writes ``raw/pattern_sources.json``: per source, when it was read and which patterns it
confirmed. Only the pages' verdicts are kept, never their text. The generators refuse patterns no source confirmed.
"""

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from evals.corpus import Corpus, now_iso
from evals.sources.eu_shops import robots_allows
from unfret.html_cleaner import clean_html
from unfret.page_fetch import PageUnreadableError, fetch_page

logger = structlog.get_logger("unfret.evals")

Side = Literal["bad", "good"]
Family = Literal["p2p", "service"]

# Public pages that document the patterns (fetched by ``main``).
MP_PAYMENT_FRAUD = "https://www.marktplaats.nl/m/betaalfraude/"
FHD_ONE_CENT = "https://www.fraudehelpdesk.nl/fraude/ik-krijg-een-betaalverzoek-voor-1-cent/"
FHD_SOLD_UNPAID = "https://www.fraudehelpdesk.nl/fraude/ik-heb-iets-verkocht-maar-geen-geld-ontvangen/"
FHD_FAKE_COURIER = "https://www.fraudehelpdesk.nl/fraude/nep-koeriersdienst-na-verkoop-op-handelsplaats/"
FHD_FIREWOOD = "https://www.fraudehelpdesk.nl/alert/fraude-met-verkoop-haardhout-via-paysafecards/"
FHD_TRADES = "https://www.fraudehelpdesk.nl/alert/pas-op-voor-misleidende-klusbedrijven-en-slotenmakers/"
FTC_RENTAL = "https://consumer.ftc.gov/articles/rental-listing-scams"
FTC_RENTAL_2024 = "https://consumer.ftc.gov/consumer-alerts/2024/07/avoiding-rental-listing-scams"
FTC_MARKETPLACE = "https://consumer.ftc.gov/articles/buying-online-marketplace"
FTC_PETS = "https://consumer.ftc.gov/consumer-alerts/2024/12/getting-pet-avoid-scams"
FTC_SELLING = "https://consumer.ftc.gov/consumer-alerts/2022/07/selling-stuff-online-heres-how-avoid-scam"
FTC_HOME_IMPROVEMENT = "https://consumer.ftc.gov/articles/how-avoid-home-improvement-scam"
FTC_HOME_REPAIR = "https://consumer.ftc.gov/features/pass-it-on/home-repair-scams"
# The FTC retired its locksmith page; the Internet Archive keeps the 2020 version.
FTC_LOCKSMITH = "https://web.archive.org/web/2020id_/https://www.consumer.ftc.gov/articles/0089-finding-locksmith"
KASSA_TRADES = "https://www.bnnvara.nl/kassa/artikelen/vaker-oplichting-door-malafide-klussers-zo-trap-je-er-niet-in"
CB_LOCKSMITHS = "https://www.consumentenbond.nl/verbouwen-en-klussen/slotenmakers"
CB_EMERGENCY = "https://www.consumentenbond.nl/juridisch-advies/rechten-bij-aankoop/help-ik-heb-teveel-voor-een-spoeddienst-betaald"
ACM_KEYSERVICE = (
    "https://consument.acm.nl/nieuws/acm-geeft-slotenmaker-boetes-voor-misleidende-en-agressieve-praktijken"
)
ACM_LEADS = "https://consument.acm.nl/nieuws/acm-dwingt-2-leadbedrijven-te-stoppen-met-online-misleiding"
ACM_LOCKED_OUT = "https://consument.acm.nl/nieuws/buitengesloten-let-op-als-u-een-slotenmaker-inschakelt"


@dataclass(frozen=True)
class Pattern:
    """One documented way a page of a class looks when it is a scam (``bad``) or ordinary (``good``)."""

    key: str
    family: Family
    side: Side
    description: str
    sources: tuple[str, ...]
    evidence: tuple[str, ...]


PATTERNS: tuple[Pattern, ...] = (
    # ── peer-to-peer: scams ──
    Pattern(
        "off_platform_contact",
        "p2p",
        "bad",
        "The ad moves contact off the platform: WhatsApp, e-mail or a phone number to text.",
        (MP_PAYMENT_FRAUD, FHD_FIREWOOD),
        (r"whatsapp",),
    ),
    Pattern(
        "seller_payment_link",
        "p2p",
        "bad",
        "The seller sends their own payment link or request instead of the platform's payment.",
        (MP_PAYMENT_FRAUD, FHD_ONE_CENT),
        (r"betaalverzoek|betaallink",),
    ),
    Pattern(
        "untraceable_prepayment",
        "p2p",
        "bad",
        "Payment up front by wire, gift card, prepaid card, payment app or crypto.",
        (FTC_PETS, FTC_RENTAL_2024, FHD_FIREWOOD),
        (r"gift card|wire transfer|cryptocurrency|paysafecard",),
    ),
    Pattern(
        "too_good_price",
        "p2p",
        "bad",
        "A sought-after item at a fraction of what it sells for.",
        (FTC_PETS, FTC_MARKETPLACE, FHD_FIREWOOD),
        (r"few hundred dollars when they often cost thousands|very low price|aantrekkelijke prijs",),
    ),
    Pattern(
        "pressure",
        "p2p",
        "bad",
        "Pressure to decide and pay now.",
        (FTC_PETS, FTC_RENTAL_2024),
        (r"act fast|rush you",),
    ),
    Pattern(
        "rental_no_viewing",
        "p2p",
        "bad",
        "A rental with deposit or rent before any viewing, keys promised after payment.",
        (FTC_RENTAL, FTC_RENTAL_2024),
        (r"never seen|before you pay|keys right away",),
    ),
    Pattern(
        "copied_listing",
        "p2p",
        "bad",
        "Photos and text copied from a real listing, the contact details swapped.",
        (FTC_RENTAL,),
        (r"copy the pictures",),
    ),
    Pattern(
        "courier_only",
        "p2p",
        "bad",
        "No pickup: the goods only travel through a courier the seller arranges.",
        (FHD_FAKE_COURIER, FHD_SOLD_UNPAID),
        (r"koerier",),
    ),
    # ── peer-to-peer: ordinary ads ──
    Pattern(
        "platform_payment",
        "p2p",
        "good",
        "Payment and shipping through the platform, with its buyer protection.",
        (MP_PAYMENT_FRAUD, FHD_ONE_CENT, FTC_MARKETPLACE),
        (r"betalen en verzenden via marktplaats|beveiligde betaalmethode|safe payment method",),
    ),
    Pattern(
        "meet_in_person",
        "p2p",
        "good",
        "Pickup or meeting in person, paying on the spot.",
        (FTC_SELLING, FHD_ONE_CENT),
        (r"meet in person|contant",),
    ),
    Pattern(
        "seller_history",
        "p2p",
        "good",
        "A seller with ratings and a history on the platform.",
        (FTC_MARKETPLACE,),
        (r"rating|review",),
    ),
    # ── service providers: rogue traders ──
    Pattern(
        "fake_local",
        "service",
        "bad",
        "Pages and domains named after towns, posing as a local business while one call centre answers.",
        (ACM_KEYSERVICE, ACM_LEADS, FTC_LOCKSMITH),
        (r"lokale slotenmaker|call cent(er|re)|spoedservice",),
    ),
    Pattern(
        "emergency_247",
        "service",
        "bad",
        "24/7, there right away, an emergency service everywhere.",
        (KASSA_TRADES, FHD_TRADES, CB_EMERGENCY),
        (r"24 uur per dag|spoeddienst",),
    ),
    Pattern(
        "vague_prices",
        "service",
        "bad",
        "No clear price before the job: only a call-out fee, a low price per metre or per minute.",
        (FHD_TRADES, CB_EMERGENCY, ACM_KEYSERVICE, FTC_LOCKSMITH),
        (r"prijsafspraken|prijs per meter|duidelijke informatie over de prijs|estimate",),
    ),
    Pattern(
        "no_identity",
        "service",
        "bad",
        "No company address, KvK number or licence on the site.",
        (ACM_LOCKED_OUT, CB_LOCKSMITHS),
        (r"geen bedrijfsadres|adresgegevens",),
    ),
    Pattern(
        "fake_reviews",
        "service",
        "bad",
        "Glowing reviews that are made up or templated.",
        (ACM_KEYSERVICE, KASSA_TRADES),
        (r"neprecensies",),
    ),
    Pattern(
        "pay_on_the_spot",
        "service",
        "bad",
        "Payment on the spot, up front, in cash or on a mobile card reader.",
        (FHD_TRADES, FTC_HOME_IMPROVEMENT, FTC_HOME_REPAIR),
        (r"directe betaling|up front|in cash",),
    ),
    Pattern(
        "fake_quality_claims",
        "service",
        "bad",
        "Claims of vetted tradesmen and certifications that nobody checks.",
        (ACM_KEYSERVICE,),
        (r"controleerde op kwaliteit",),
    ),
    # ── service providers: established firms ──
    Pattern(
        "trade_registration",
        "service",
        "good",
        "A KvK number, licence or trade association membership that can be checked.",
        (ACM_LOCKED_OUT, CB_LOCKSMITHS, FTC_HOME_IMPROVEMENT),
        (r"kamer van koophandel|licensed",),
    ),
    Pattern(
        "written_quote",
        "service",
        "good",
        "Prices published or a written quote before the work.",
        (CB_LOCKSMITHS, FTC_HOME_IMPROVEMENT, FTC_HOME_REPAIR),
        (r"prijsindicatie|written estimate",),
    ),
    Pattern(
        "real_address",
        "service",
        "good",
        "A real business address and a local presence.",
        (ACM_LOCKED_OUT, CB_LOCKSMITHS),
        (r"fysieke winkel|google maps",),
    ),
)
BY_KEY: dict[str, Pattern] = {p.key: p for p in PATTERNS}


def confirmed_by(text: str, patterns: tuple[Pattern, ...], url: str) -> list[str]:
    """
    The patterns a source page confirms: those that cite ``url`` and have an evidence phrase in its text.

    :param text: The source page's visible text.
    :param patterns: Candidate patterns.
    :param url: The source page's address.
    :return: Keys of the confirmed patterns, in pattern order.
    """
    return [p.key for p in patterns if url in p.sources and any(re.search(e, text, re.IGNORECASE) for e in p.evidence)]


def sources_path(corpus: Corpus) -> Path:
    """Where the source check is recorded."""
    return corpus.root / "raw" / "pattern_sources.json"


def confirmed_patterns(corpus: Corpus, family: Family) -> dict[str, list[str]]:
    """
    The patterns of a family that at least one fetched source confirmed (``raw/pattern_sources.json`` must exist).

    :param corpus: The corpus (``raw/pattern_sources.json``).
    :param family: ``p2p`` or ``service``.
    :return: ``{pattern key: [source URLs]}``.
    """
    checked = json.loads(sources_path(corpus).read_text())
    out: dict[str, list[str]] = {}
    for url, row in checked.items():
        for key in row.get("confirmed", []):
            if key in BY_KEY and BY_KEY[key].family == family:
                out.setdefault(key, []).append(url)
    return {k: sorted(v) for k, v in out.items()}


async def check_sources(corpus: Corpus) -> dict[str, dict[str, object]]:
    """
    Fetch every source page once and record which patterns it confirms.

    :param corpus: The corpus to record in.
    :return: The recorded rows, by source URL.
    """
    urls = sorted({u for p in PATTERNS for u in p.sources})
    rows: dict[str, dict[str, object]] = {}
    for url in urls:
        row: dict[str, object] = {"checked_at": now_iso(), "confirmed": []}
        if not await robots_allows(url):
            row["error"] = "robots"
        else:
            try:
                text = clean_html(await fetch_page(url))
            except PageUnreadableError as exc:
                row["error"] = exc.reason
            else:
                row["confirmed"] = confirmed_by(text, PATTERNS, url)
        rows[url] = row
        logger.info("pattern_source", url=url, **{k: v for k, v in row.items() if k != "checked_at"})
        await asyncio.sleep(1.0)
    path = sources_path(corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    return rows


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.parse_args()
    rows = asyncio.run(check_sources(Corpus()))
    confirmed = {k for row in rows.values() for k in row["confirmed"]}  # type: ignore[attr-defined]
    missing = sorted(p.key for p in PATTERNS if p.key not in confirmed)
    logger.info("pattern_sources_done", sources=len(rows), confirmed=len(confirmed), unconfirmed=missing)


if __name__ == "__main__":
    main()
