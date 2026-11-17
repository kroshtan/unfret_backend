"""
Duplicate ads: the same ad text, or the same phone number or e-mail address, behind other ads.

Scam ads are posted in bulk: one text under many accounts and cities, one phone number behind dozens of ads. The
ad's text is fingerprinted (a simhash of its word shingles) and its contact details are hashed with a server key,
then looked up in the ad store (:mod:`unfret.ad_store`) among the ads checked in the last 90 days, and recorded
there in the background. Nothing else about the ad or the seller is kept. Copied photos are the reverse image
skill's part (an ad's photo is compared with other ads' there).

An honest seller who reposts an ad, or puts it on two platforms, has one or two copies; the verdict only counts
copies as a warning with other signs, or when there are several.
"""

import asyncio
from datetime import date

from unfret import ad_store, background, shop_store
from unfret.config import settings
from unfret.facts.p2p import EMAIL_RE, phones_in
from unfret.fingerprint import contact_hash, normalise_email
from unfret.graph.platforms import PLATFORMS
from unfret.graph.skills import P2P_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

_pending: set[asyncio.Task[None]] = set()


async def drain() -> None:
    """Wait for background writes (tests and shutdown)."""
    await background.drain(_pending)


def _record_later(ad: ad_store.AdPrint, day: date) -> None:
    task = asyncio.create_task(ad_store.record(ad, day))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def contact_hashes(text: str) -> tuple[bytes, ...]:
    """
    The keyed hashes of the phone numbers and e-mail addresses written in an ad.

    :param text: The ad text.
    :return: The hashes (none without a contact hash key).
    """
    key = shop_store.contact_key()
    if key is None:
        return ()
    values = [("phone", p) for raw in phones_in(text) if (p := ad_store.comparable_phone(raw))]
    values += [("email", e) for m in EMAIL_RE.finditer(text) if (e := normalise_email(m.group()))]
    return tuple(dict.fromkeys(contact_hash(kind, value, key) for kind, value in values))


def _names(platforms: dict[str, int]) -> str:
    names = [PLATFORMS[p].name if p in PLATFORMS else p for p in sorted(platforms, key=lambda p: -platforms[p])]
    return ", ".join(names)


class DuplicateAdsSkill(Skill):
    """The ad's text and contact details, looked up among the other ads checked lately."""

    name = "duplicate_ads"
    description = "Looks for the same ad text, or the same phone number, in other ads checked in the last 90 days."
    page_types = P2P_PAGES
    features = ("text_print", "text_copies", "copy_platforms", "contacts", "contact_ads")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Fingerprint the ad, look it up, and record it.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        text = facts.listing.description if facts.listing is not None else ""
        url = state.get("url", "") or facts.url
        if not text or not url:
            return self.output({"text_print": False}, note="No ad text to compare.")
        ad = ad_store.AdPrint(
            platform=facts.platform,
            listing_key=ad_store.listing_key(facts.platform, url),
            simhash=ad_store.simhash(text),
            contacts=contact_hashes(text),
        )
        day = shop_store.today()
        seen = await ad_store.seen(ad, day)
        if settings.fingerprints:
            _record_later(ad, day)
        if seen is None:
            return self.unavailable()
        observations: list[Observation] = []
        if seen.copies:
            ads = f"{seen.copies} other ad{'s' if seen.copies != 1 else ''}"
            observations.append(
                Observation(
                    type="duplicate_text",
                    evidence=f"The same ad text is in {ads} checked in the last {ad_store.RETENTION_DAYS} days "
                    f"({_names(seen.text_copies)}).",
                )
            )
        if seen.contact_ads:
            ads = f"{seen.contact_ads} other ad{'s' if seen.contact_ads != 1 else ''}"
            observations.append(
                Observation(
                    type="shared_contact",
                    evidence=f"The phone number or e-mail address in this ad is also in {ads} checked in the last "
                    f"{ad_store.RETENTION_DAYS} days.",
                )
            )
        return self.output(
            {
                "text_print": ad.simhash is not None,
                "text_copies": seen.copies,
                "copy_platforms": len(seen.text_copies),
                "contacts": len(ad.contacts),
                "contact_ads": seen.contact_ads,
            },
            observations,
            note="" if observations else "No other ad with this text or contact seen lately.",
        )
