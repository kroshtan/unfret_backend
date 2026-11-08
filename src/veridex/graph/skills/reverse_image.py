"""
Reverse image search for the main product photo (TinEye), to spot supplier photos from Temu, AliExpress & co.

Expensive (paid per search), so the triage decides when to run it. Main image only; the result is cached by the
image's perceptual hash, so a photo reused across many listings costs one search.
"""

import asyncio
import math
from datetime import UTC, date, datetime

from veridex import tineye
from veridex.fact_cache import cached_fact
from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState
from veridex.image_hash import dhash
from veridex.net import fetch_public

_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ReverseImageSkill(Skill):
    """Where else the main product photo appears, and since when (TinEye)."""

    name = "reverse_image"
    description = "Reverse-searches the main product photo for copies on supplier marketplaces (TinEye)."
    tier = "expensive"
    always_run = False
    features = ("searched", "supplier_hits", "log_matches", "image_age_years", "photo_predates_listing")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Hash the next product image, then search it on TinEye unless that hash is cached.

        The first run takes the main image; a re-triage run takes the next one, and the features keep the
        strongest result of all runs.

        :param state: Current graph state; uses ``facts``, ``expensive_runs`` and earlier ``features``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        if not tineye.is_enabled():
            return self.output({"searched": False}, note="Reverse image search is not configured.")
        index = max(state.get("expensive_runs", []).count(self.name) - 1, 0)
        if index >= len(facts.image_urls):
            return self.output({"searched": False}, note="No product image found.")
        image_url = facts.image_urls[index]
        key = await asyncio.to_thread(dhash, await fetch_public(image_url, max_bytes=_MAX_IMAGE_BYTES))
        summary = await cached_fact("tineye", key, lambda: tineye.search(image_url))
        if summary is None:
            return self.output({"searched": False}, note="Reverse image search returned nothing.")

        observations: list[Observation] = []
        if summary["supplier_domains"]:
            observations.append(
                Observation(
                    type="supplier_photo",
                    evidence=f"The main photo also appears on {', '.join(summary['supplier_domains'][:5])}.",
                )
            )
        earliest = date.fromisoformat(summary["earliest_crawl"]) if summary["earliest_crawl"] else None
        predates = bool(earliest and facts.date_first_available and earliest < facts.date_first_available)
        if earliest and predates:
            observations.append(
                Observation(
                    type="photo_age",
                    evidence=f"The photo was online by {earliest.isoformat()}, before this listing "
                    f"({facts.date_first_available.isoformat() if facts.date_first_available else '?'}).",
                )
            )
        today = datetime.now(tz=UTC).date()
        new = {
            "searched": 1.0,
            "supplier_hits": float(len(summary["supplier_domains"])),
            "log_matches": math.log10(1 + summary["total"]),
            "image_age_years": (today - earliest).days / 365 if earliest else 0.0,
            "photo_predates_listing": float(predates),
        }
        before = state.get("features", {})
        strongest = {k: max(v, before.get(f"{self.name}.{k}", 0.0)) for k, v in new.items()}
        return self.output(strongest, observations)
