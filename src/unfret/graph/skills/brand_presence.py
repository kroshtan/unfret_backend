"""
Whether the product's brand exists beyond this page: on Wikidata, and on a domain of its own.

Two free, open lookups, each cached as a store-level fact (:mod:`unfret.fact_cache`, keyed by the brand or the
domain, never by who asked):

- **Wikidata** (the community-run open knowledge base, CC0): an item whose label or alias is the brand, and that is a
  business or brand (an official website, an industry, products, or "instance of" a company or brand). Found means an
  established brand.
- **The brand's own domain**: ``<brand>.com`` and ``<brand>.nl`` in RDAP (the same lookup as
  :mod:`~unfret.graph.skills.domain_age`): registered, and how long ago.

"No presence" needs both to have answered: no Wikidata item, and neither domain registered. On its own that is true of
many small honest brands, so the shopper hears about it only on a marketplace listing with other signs of generic
white-label goods (:mod:`unfret.shopper`). A network lookup, so off in evaluation runs and not in the scorer yet.
"""

import asyncio
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from unfret.config import settings
from unfret.fact_cache import cached_fact
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.skills.domain_age import rdap_lookup
from unfret.graph.state import AnalysisState
from unfret.net import fetch_public, hostname
from unfret.price_store import registrable_domain

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_TIMEOUT_S = 10.0
_TLDS = ("com", "nl")
# Words that aren't a brand.
_NOT_BRANDS = frozenset({"generic", "unbranded", "no brand", "nobrand", "unknown", "n a", "none", "other", "diverse"})
# "Instance of" classes that make an item a business or a brand.
_BUSINESS_CLASSES = frozenset(
    {
        "Q4830453",  # business
        "Q783794",  # company
        "Q6881511",  # enterprise
        "Q891723",  # public company
        "Q431289",  # brand
        "Q167037",  # corporation
        "Q1589009",  # privately held company
        "Q658255",  # subsidiary
    }
)
# Properties only a business or brand item has: official website, industry, product, legal form, parent, owner.
_BUSINESS_PROPERTIES = ("P856", "P452", "P1056", "P1454", "P749", "P127")


def _fold(text: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def brand_key(brand: str) -> str | None:
    """
    The brand's comparable name, or ``None`` when it isn't a brand.

    :param brand: The brand as the page writes it.
    :return: Lower case, accents folded, letters and digits separated by single spaces.
    """
    key = _fold(brand)
    if len(key) < 2 or key in _NOT_BRANDS or len(key) > 60:
        return None
    return key


def brand_domains(key: str) -> list[str]:
    """
    The brand's likely own domains: its name without spaces, ``.com`` and ``.nl``.

    :param key: From :func:`brand_key`.
    :return: The domains (none for a name too short or long to be a domain of its own).
    """
    slug = key.replace(" ", "")
    return [f"{slug}.{tld}" for tld in _TLDS] if 3 <= len(slug) <= 30 else []


def _business(entity: dict[str, Any]) -> bool:
    claims = entity.get("claims") or {}
    if any(p in claims for p in _BUSINESS_PROPERTIES):
        return True
    kinds = {(c.get("mainsnak", {}).get("datavalue", {}).get("value") or {}).get("id") for c in claims.get("P31", [])}
    return bool(kinds & _BUSINESS_CLASSES)


def _websites(entity: dict[str, Any]) -> list[str]:
    sites = []
    for claim in (entity.get("claims") or {}).get("P856", []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str) and (host := hostname(value)):
            sites.append(registrable_domain(host))
    return list(dict.fromkeys(sites))[:20]


async def _api(params: str) -> dict[str, Any]:
    body = await fetch_public(f"{WIKIDATA_API}?{params}&format=json", timeout=_TIMEOUT_S, max_bytes=5 * 1024 * 1024)
    data: dict[str, Any] = await asyncio.to_thread(json.loads, body)
    if "error" in data:  # "search is too busy" and the like: not a fact worth caching
        raise RuntimeError(f"Wikidata: {data['error'].get('code', 'error')}")
    return data


async def wikidata_lookup(brand: str) -> dict[str, Any] | None:
    """
    The Wikidata item of a business or brand whose label or alias is ``brand``.

    :param brand: The brand.
    :return: ``{"id": "Q…", "label": …, "websites": [registrable domains]}``, or ``None`` when there is none. When
        Wikidata fails, the error is raised (not cached; the next check asks again).
    """
    key = brand_key(brand)
    if key is None:
        return None
    found = await _api(f"action=wbsearchentities&search={quote(brand)}&language=en&uselang=en&type=item&limit=10")
    ids = [
        hit["id"]
        for hit in found.get("search", [])
        if isinstance(hit.get("id"), str)
        and key in {_fold(str(hit.get("label", ""))), _fold(str((hit.get("match") or {}).get("text", "")))}
    ][:10]
    if not ids:
        return None
    entities = (await _api(f"action=wbgetentities&ids={'|'.join(ids)}&props=claims|labels&languages=en")).get(
        "entities", {}
    )
    for item_id in ids:
        entity = entities.get(item_id) or {}
        if _business(entity):
            label = ((entity.get("labels") or {}).get("en") or {}).get("value", brand)
            return {"id": item_id, "label": str(label)[:200], "websites": _websites(entity)}
    return None


class _Failed:
    """A lookup that failed (so neither found nor not found)."""


async def _lookup(kind: str, key: str, load: Any) -> Any:
    try:
        return await cached_fact(kind, key, load)
    except Exception:  # noqa: BLE001 - one failed lookup costs its own signal
        return _Failed()


async def on_wikidata(brand: str) -> bool | None:
    """
    Whether a brand is on Wikidata as a business or brand, from the same cached lookup as the skill's.

    For another brand than the page's (the photo check asks it of the catalogue brand a photo was found under).

    :param brand: The brand.
    :return: ``True`` or ``False``, or ``None`` when it isn't a brand, lookups are off or Wikidata didn't answer.
    """
    key = brand_key(brand)
    if key is None or not settings.network_lookups:
        return None
    found = await _lookup("wikidata_brand", key, lambda: wikidata_lookup(brand))
    return None if isinstance(found, _Failed) else isinstance(found, dict)


class BrandPresenceSkill(Skill):
    """The brand on Wikidata and on a domain of its own."""

    name = "brand_presence"
    description = "Looks the brand up on Wikidata and checks whether its own .com/.nl domain exists, and how old."
    features = ("wikidata", "own_domain", "own_domain_age_years", "no_presence", "shop_is_brand")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look the brand up (cached per brand and per domain).

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        key = brand_key(facts.brand)
        if key is None:
            return self.output({}, note="No brand to look up.")
        if not settings.network_lookups:
            return self.output({}, note="Lookups are off.")
        domains = brand_domains(key)
        wiki, *rdaps = await asyncio.gather(
            _lookup("wikidata_brand", key, lambda: wikidata_lookup(facts.brand)),
            *(_lookup("rdap", d, lambda d=d: rdap_lookup(d)) for d in domains),
        )
        today = datetime.now(tz=UTC).date()
        host = hostname(state.get("url", ""))
        shop = registrable_domain(host) if host else ""
        features: dict[str, float | bool] = {}
        observations: list[Observation] = []

        if isinstance(wiki, dict):
            features["wikidata"] = True
            observations.append(
                Observation(type="brand_on_wikidata", evidence=f'"{facts.brand}" is on Wikidata ({wiki["id"]}).')
            )
        registered = [(d, r) for d, r in zip(domains, rdaps, strict=True) if isinstance(r, dict)]
        ages = [(today - date.fromisoformat(r["registered"])).days / 365 for _, r in registered if r.get("registered")]
        if registered:
            features["own_domain"] = True
            features["own_domain_age_years"] = round(max(ages), 2) if ages else 0.0
            first = registered[0]
            when = f"registered {first[1]['registered']}" if first[1].get("registered") else "registered"
            observations.append(Observation(type="brand_domain", evidence=f"{first[0]} is {when}."))
        websites = set(wiki["websites"]) if isinstance(wiki, dict) else set()
        features["shop_is_brand"] = bool(shop) and (shop in websites or shop in domains)
        answered = not isinstance(wiki, _Failed) and not any(isinstance(r, _Failed) for r in rdaps)
        if answered and wiki is None and not registered and domains:
            features["no_presence"] = True
            observations.append(
                Observation(
                    type="brand_no_presence",
                    evidence=f'No Wikidata entry for "{facts.brand}", and {" and ".join(domains)} aren\'t registered.',
                )
            )
        return self.output(features, observations)
