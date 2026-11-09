"""
Where else the product photo appears (Google Cloud Vision web detection), to spot supplier photos.

Runs on every extension analysis and deep check (not the quick tier), when a Vision key is configured. The main
photo first; the second only when the first is inconclusive (no supplier pages). Results are cached by the
image's perceptual hash, so a photo reused across many listings costs one request.
"""

import asyncio
import math

import structlog

from veridex import vision
from veridex.fact_cache import cached_fact
from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState
from veridex.image_hash import dhash
from veridex.net import fetch_public

logger = structlog.get_logger("veridex")

_MAX_PHOTOS = 2


class ReverseImageSkill(Skill):
    """How often the product photo appears on supplier marketplaces, exactly or cropped (Google Cloud Vision)."""

    name = "reverse_image"
    description = "Looks up the product photo on the web for copies on supplier marketplaces (Google Cloud Vision)."
    features = ("searched", "supplier_hits", "supplier_full", "log_matches", "log_full_matches")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look up the main photo, and the second one if the first found no supplier pages.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        if not vision.is_enabled():
            return self.output({"searched": False}, note="Photo lookup is not configured.")
        if not facts.image_urls:
            return self.output({"searched": False}, note="No product image found.")

        best: dict[str, object] | None = None
        for image_url in facts.image_urls[:_MAX_PHOTOS]:
            summary = await _lookup(image_url)
            if summary is None:
                continue
            if best is None or summary["supplier_pages"] > best["supplier_pages"]:  # type: ignore[operator]
                best = summary
            if summary["supplier_pages"]:
                break  # conclusive: no need for another photo
        if best is None:
            return self.output({"searched": False}, note="The photo couldn't be looked up.")

        pages, full = int(best["supplier_pages"]), int(best["supplier_full"])  # type: ignore[call-overload]
        suppliers: list[str] = best["suppliers"]  # type: ignore[assignment]
        observations: list[Observation] = []
        if pages:
            observations.append(
                Observation(
                    type="supplier_photo",
                    evidence=f"This photo also appears on {pages} {'/'.join(suppliers[:2])} "
                    f"listing{'s' if pages != 1 else ''}" + (f" ({full} identical)" if full else "") + ".",
                )
            )
        return self.output(
            {
                "searched": True,
                "supplier_hits": float(pages),
                "supplier_full": float(full),
                "log_matches": math.log10(1 + int(best["pages"])),  # type: ignore[call-overload]
                "log_full_matches": math.log10(1 + int(best["full"])),  # type: ignore[call-overload]
            },
            observations,
        )


async def _lookup(image_url: str) -> dict[str, object] | None:
    # One photo: fetched by us (Google only ever sees the bytes), hashed, then looked up unless the hash is cached.
    # A failed fetch, a full budget or an API error costs this photo, not the analysis; errors aren't cached.
    try:
        image = await fetch_public(image_url, max_bytes=vision.MAX_IMAGE_BYTES)
        key = await asyncio.to_thread(dhash, image)
        return await cached_fact("vision", key, lambda: vision.web_detection(image))
    except vision.BudgetExceededError:
        logger.warning("vision_budget_exhausted")
    except Exception as exc:  # noqa: BLE001 - one optional signal
        logger.warning("vision_lookup_failed", error=type(exc).__name__)
    return None
