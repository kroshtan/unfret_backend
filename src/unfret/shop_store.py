"""
The shop fingerprint store: what Unfret has seen of web shops, so shops run by one operator can be linked.

Shops only, never shoppers. A row is keyed by the shop's registrable domain and holds facts any visitor can see:
the first and last day an analysis saw it (days, never times), its platform and apps, its template hash, the
registration and first-certificate dates already looked up, and the account identifiers on its pages
(:mod:`unfret.fingerprint`). Nothing records who checked it, which page, or what Unfret concluded: no URL, caller,
wallet, IP address, score or verdict is stored, so the store can't feed its own verdicts back as evidence.

Service companies' own sites are recorded the same way, so one call centre behind many "local" brands shows up as
one operator: their published phone numbers (as the facts read them, national formats included) and the domain of
their e-mail addresses (not webmail) count as identifiers too.

Contact e-mail addresses, phone numbers, e-mail domains and postal addresses are personal data (a sole trader's
address and domain are their own): they are stored only as keyed hashes (HMAC-SHA256 with a server secret), never
shown, deleted 180 days after they were last seen, and removable on request (:func:`forget_contact`). Everything else
is deleted 400 days after the shop was last seen.

Postal addresses never link shops: a virtual office or a mail-forwarding address serves hundreds of unrelated
businesses. They are only counted (:meth:`ShopStore.address_shops`), to tell a shared address from a shop's own.

Links are one hop: two shops are linked when they share an identifier, never because both share one with a third
shop. An identifier shared by more than :data:`MAX_SHARE` shops is an agency's or a template's, not an operator's,
and links nobody.
"""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, get_args

import asyncpg
import structlog

from unfret import background, fact_cache
from unfret.config import settings
from unfret.facts.business import WEBMAIL_DOMAINS
from unfret.facts.schema import PageFacts, Platform
from unfret.fingerprint import MAX_IDS_PER_KIND, contact_hash, fingerprint, normalise_address, normalise_phone
from unfret.graph.skills.html_source_signals import page_source, scan
from unfret.net import hostname
from unfret.price_store import registrable_domain
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 400
CONTACT_RETENTION_DAYS = 180
MAX_ADDRESSES = 5
MAX_SHARE = 25  # an identifier on more shops than this is shared infrastructure, not one operator
# Kinds stored only as keyed hashes, kept 180 days. Addresses are counted, never used as links (NOT_LINKING).
CONTACT_KINDS = frozenset({"email", "phone", "email_domain", "address"})
NOT_LINKING = frozenset({"address"})
# Shops are web shops: marketplaces and platforms (peer-to-peer, service providers) are one domain for millions of
# sellers, so their pages say nothing about a shop. Every recognised platform but the shop systems.
_MARKETPLACES = frozenset(get_args(Platform)) - {"shopify", "woocommerce", "generic"}
# The page types whose site is the business's own: a shop, or (looked up, not recorded) a service company's site.
_OWN_SITES = frozenset({"storefront", "service_site"})


@dataclass(frozen=True)
class ShopRecord:
    """One analysis's observation of a shop, ready to store (contacts already hashed)."""

    domain: str
    day: date
    platform: str = "generic"
    apps: tuple[str, ...] = ()
    skeleton: int | None = None
    registered: date | None = None
    registrar: str | None = None
    first_cert: date | None = None
    links: dict[str, set[str]] = field(default_factory=dict)  # kind → values; contact values are hex HMACs
    fingerprint: bytes | None = None  # digest of this observation's identifiers, apps and template


@dataclass(frozen=True)
class Linked:
    """Another shop sharing at least one identifier with this one, and that shop's own raw facts."""

    domain: str
    kinds: tuple[str, ...]
    shared: tuple[str, ...]  # the shared public identifiers (contacts are never included)
    first_seen: date
    registered: date | None
    first_cert: date | None


class ShopStore(Protocol):
    """Where shop fingerprints live: Postgres in production, memory in tests."""

    async def record(self, rec: ShopRecord) -> None:
        """
        Add or refresh a shop and its identifiers.

        :param rec: The observation.
        """

    async def linked(self, domain: str, current: dict[str, set[str]]) -> list[Linked]:
        """
        The shops sharing an identifier with ``domain`` (one hop): one stored for it, or one on its page now.

        :param domain: A registrable domain.
        :param current: The identifiers on the page being analysed (kind → values).
        :return: The linked shops, most identifiers first.
        """

    async def address_shops(self, domain: str, addresses: set[str]) -> set[str]:
        """
        The other shops that give one of these (hashed) postal addresses.

        :param domain: This shop (left out).
        :param addresses: The address hashes on its page now.
        :return: Their registrable domains.
        """
        ...

    async def purge(self, today: date) -> None:
        """
        Delete what is past its retention.

        :param today: Today.
        """

    async def fingerprint(self, domain: str) -> bytes | None:
        """
        The digest of the shop's latest observation (see :func:`fingerprint_digest`).

        :param domain: A registrable domain.
        :return: 32 bytes, or ``None`` for an unknown shop.
        """

    async def forget_shop(self, domain: str) -> bool:
        """
        Delete a shop and its identifiers.

        :param domain: A registrable domain.
        :return: Whether it was there.
        """

    async def forget_contact(self, kind: str, value: str) -> int:
        """
        Delete a contact hash wherever it is linked.

        :param kind: ``"email"`` or ``"phone"``.
        :param value: The hex HMAC.
        :return: How many links were deleted.
        """


@dataclass
class _Shop:
    first_seen: date
    last_seen: date
    rec: ShopRecord


class MemoryShopStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.shops: dict[str, _Shop] = {}
        self.links: dict[tuple[str, str, str], tuple[date, date]] = {}  # (kind, value, domain) → first, last seen

    async def record(self, rec: ShopRecord) -> None:
        """
        Add or refresh a shop.

        :param rec: The observation.
        """
        old = self.shops.get(rec.domain)
        self.shops[rec.domain] = _Shop(
            old.first_seen if old else rec.day, rec.day, _merged(old.rec, rec) if old else rec
        )
        for kind, values in rec.links.items():
            for value in values:
                first, _ = self.links.get((kind, value, rec.domain), (rec.day, rec.day))
                self.links[(kind, value, rec.domain)] = (first, rec.day)

    async def linked(self, domain: str, current: dict[str, set[str]]) -> list[Linked]:
        """
        One-hop links.

        :param domain: The shop.
        :param current: The identifiers on its page now.
        :return: The linked shops.
        """
        mine = {(k, v) for (k, v, d) in self.links if d == domain} | _pairs(current)
        mine = {(k, v) for k, v in mine if k not in NOT_LINKING}
        found: dict[str, list[tuple[str, str]]] = {}
        for kind, value in mine:
            holders = {d for (k, v, d) in self.links if (k, v) == (kind, value)} | {domain}
            if len(holders) > MAX_SHARE:
                continue
            for other in holders - {domain}:
                found.setdefault(other, []).append((kind, value))
        return _sorted(
            [
                Linked(
                    other,
                    tuple(sorted({k for k, _ in pairs})),
                    tuple(sorted(v for k, v in pairs if k not in CONTACT_KINDS)),
                    self.shops[other].first_seen,
                    self.shops[other].rec.registered,
                    self.shops[other].rec.first_cert,
                )
                for other, pairs in found.items()
            ]
        )

    async def address_shops(self, domain: str, addresses: set[str]) -> set[str]:
        """
        Other shops with one of these addresses.

        :param domain: This shop.
        :param addresses: Address hashes.
        :return: Their domains.
        """
        return {d for (k, v, d) in self.links if k == "address" and v in addresses and d != domain}

    async def purge(self, today: date) -> None:
        """
        Apply the retention limits.

        :param today: Today.
        """
        self.links = {
            key: seen for key, seen in self.links.items() if seen[1] >= _cutoff(today, contact=key[0] in CONTACT_KINDS)
        }
        self.shops = {d: s for d, s in self.shops.items() if s.last_seen >= _cutoff(today, contact=False)}
        self.links = {key: seen for key, seen in self.links.items() if key[2] in self.shops}

    async def fingerprint(self, domain: str) -> bytes | None:
        """
        The latest digest.

        :param domain: The shop.
        :return: The digest.
        """
        shop = self.shops.get(domain)
        return shop.rec.fingerprint if shop else None

    async def forget_shop(self, domain: str) -> bool:
        """
        Delete a shop.

        :param domain: The shop.
        :return: Whether it was there.
        """
        self.links = {key: seen for key, seen in self.links.items() if key[2] != domain}
        return self.shops.pop(domain, None) is not None

    async def forget_contact(self, kind: str, value: str) -> int:
        """
        Delete a contact everywhere.

        :param kind: The contact kind.
        :param value: Its hash.
        :return: Links deleted.
        """
        gone = [key for key in self.links if key[:2] == (kind, value)]
        for key in gone:
            del self.links[key]
        return len(gone)


class PostgresShopStore:
    """Fingerprints in the ``shops`` and ``shop_links`` tables (migration 7)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(self, rec: ShopRecord) -> None:
        """
        Upsert the shop and its identifiers in one transaction.

        :param rec: The observation.
        """
        rows = [(kind, value) for kind, values in rec.links.items() for value in values]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO shops (domain, first_seen, last_seen, platform, apps, skeleton, registered, registrar,"
                " first_cert, fingerprint) VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8, $9) "
                "ON CONFLICT (domain) DO UPDATE SET last_seen = GREATEST(shops.last_seen, EXCLUDED.last_seen),"
                " platform = EXCLUDED.platform, apps = EXCLUDED.apps,"
                " skeleton = COALESCE(EXCLUDED.skeleton, shops.skeleton),"
                " registered = COALESCE(EXCLUDED.registered, shops.registered),"
                " registrar = COALESCE(EXCLUDED.registrar, shops.registrar),"
                " first_cert = COALESCE(EXCLUDED.first_cert, shops.first_cert), fingerprint = EXCLUDED.fingerprint",
                rec.domain,
                rec.day,
                rec.platform,
                list(rec.apps),
                rec.skeleton,
                rec.registered,
                rec.registrar,
                rec.first_cert,
                rec.fingerprint,
            )
            if rows:
                await conn.executemany(
                    "INSERT INTO shop_links (kind, value, domain, first_seen, last_seen) VALUES ($1, $2, $3, $4, $4) "
                    "ON CONFLICT (kind, value, domain) DO UPDATE SET last_seen = EXCLUDED.last_seen",
                    [(kind, value, rec.domain, rec.day) for kind, value in rows],
                )

    async def linked(self, domain: str, current: dict[str, set[str]]) -> list[Linked]:
        """
        One-hop links, skipping identifiers held by more than :data:`MAX_SHARE` shops.

        :param domain: The shop.
        :param current: The identifiers on its page now.
        :return: The linked shops.
        """
        pairs = sorted((k, v) for k, v in _pairs(current) if k not in NOT_LINKING)
        rows: list[Any] = await self.pool.fetch(
            "WITH mine AS ("
            "  SELECT kind, value FROM shop_links WHERE domain = $1 AND kind <> ALL($5::text[])"
            "  UNION SELECT * FROM unnest($3::text[], $4::text[]) AS c (kind, value)"
            "), shared AS ("
            "  SELECT mine.kind, mine.value FROM mine WHERE ("
            "    SELECT COUNT(*) FROM shop_links c"
            "    WHERE c.kind = mine.kind AND c.value = mine.value AND c.domain <> $1"
            "  ) + 1 <= $2"
            ") "
            "SELECT o.domain, o.kind, o.value, s.first_seen, s.registered, s.first_cert "
            "FROM shared JOIN shop_links o ON o.kind = shared.kind AND o.value = shared.value AND o.domain <> $1 "
            "JOIN shops s ON s.domain = o.domain",
            domain,
            MAX_SHARE,
            [k for k, _ in pairs],
            [v for _, v in pairs],
            sorted(NOT_LINKING),
        )
        found: dict[str, list[Any]] = {}
        for row in rows:
            found.setdefault(row["domain"], []).append(row)
        return _sorted(
            [
                Linked(
                    other,
                    tuple(sorted({r["kind"] for r in group})),
                    tuple(sorted(r["value"] for r in group if r["kind"] not in CONTACT_KINDS)),
                    group[0]["first_seen"],
                    group[0]["registered"],
                    group[0]["first_cert"],
                )
                for other, group in found.items()
            ]
        )

    async def address_shops(self, domain: str, addresses: set[str]) -> set[str]:
        """
        Other shops with one of these addresses.

        :param domain: This shop.
        :param addresses: Address hashes.
        :return: Their domains.
        """
        rows: list[Any] = await self.pool.fetch(
            "SELECT DISTINCT domain FROM shop_links "
            "WHERE kind = 'address' AND value = ANY($1::text[]) AND domain <> $2",
            sorted(addresses),
            domain,
        )
        return {r["domain"] for r in rows}

    async def purge(self, today: date) -> None:
        """
        Apply the retention limits.

        :param today: Today.
        """
        await self.pool.execute(
            "DELETE FROM shop_links WHERE kind = ANY($1::text[]) AND last_seen < $2",
            sorted(CONTACT_KINDS),
            _cutoff(today, contact=True),
        )
        await self.pool.execute("DELETE FROM shops WHERE last_seen < $1", _cutoff(today, contact=False))

    async def fingerprint(self, domain: str) -> bytes | None:
        """
        The latest digest.

        :param domain: The shop.
        :return: The digest.
        """
        value = await self.pool.fetchval("SELECT fingerprint FROM shops WHERE domain = $1", domain)
        return bytes(value) if value is not None else None

    async def forget_shop(self, domain: str) -> bool:
        """
        Delete a shop (its identifiers go with it).

        :param domain: The shop.
        :return: Whether it was there.
        """
        status: str = await self.pool.execute("DELETE FROM shops WHERE domain = $1", domain)
        return status != "DELETE 0"

    async def forget_contact(self, kind: str, value: str) -> int:
        """
        Delete a contact everywhere.

        :param kind: The contact kind.
        :param value: Its hash.
        :return: Links deleted.
        """
        status = await self.pool.execute("DELETE FROM shop_links WHERE kind = $1 AND value = $2", kind, value)
        return int(status.split()[-1])


def _merged(old: ShopRecord, new: ShopRecord) -> ShopRecord:
    # Later observations win, but a fact once looked up isn't forgotten by an analysis that didn't look it up.
    return ShopRecord(
        new.domain,
        new.day,
        new.platform,
        new.apps,
        new.skeleton if new.skeleton is not None else old.skeleton,
        new.registered or old.registered,
        new.registrar or old.registrar,
        new.first_cert or old.first_cert,
        new.links,
        new.fingerprint,
    )


def _pairs(links: dict[str, set[str]]) -> set[tuple[str, str]]:
    return {(kind, value) for kind, values in links.items() for value in values}


def _sorted(links: list[Linked]) -> list[Linked]:
    return sorted(links, key=lambda link: (-len(link.kinds), link.domain))


def _cutoff(today: date, *, contact: bool) -> date:
    return today - timedelta(days=CONTACT_RETENTION_DAYS if contact else RETENTION_DAYS)


_store: ShopStore = MemoryShopStore()
_purged_on: date | None = None
_pending: set[asyncio.Task[None]] = set()


def use_store(store: ShopStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresShopStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def store() -> ShopStore:
    """
    The store in use.

    :return: The store.
    """
    return _store


def today() -> date:
    """
    Today in UTC: the only time the store keeps.

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


def contact_key() -> bytes | None:
    """
    The secret key for contact hashes.

    ``UNFRET_CONTACT_HASH_KEY`` when set, else derived from the session secret; without either, contacts aren't
    stored at all.

    :return: The key, or ``None``.
    """
    if key := settings.contact_hash_key.get_secret_value():
        return key.encode()
    if session := settings.session_secret.get_secret_value():
        return hmac.new(session.encode(), b"unfret-contact-hash", hashlib.sha256).digest()
    return None


def shop_domain(state: dict[str, Any]) -> str | None:
    """
    The registrable domain of the web shop an analysis looked at, or ``None`` when it wasn't a web shop.

    :param state: The analysis state (``url``, ``facts``, ``page_type``).
    :return: E.g. ``glowlamp.shop``.
    """
    host = hostname(state.get("url", ""))
    facts = state.get("facts")
    if not host or "." not in host or facts is None or facts.platform in _MARKETPLACES:
        return None
    if state.get("page_type") not in _OWN_SITES:
        return None
    return registrable_domain(host)


@dataclass(frozen=True)
class PageLinks:
    """A web shop page's identifiers in stored form (contacts hashed), with what else was read from the page."""

    domain: str
    links: dict[str, set[str]]
    skeleton: int | None
    apps: tuple[str, ...]


async def page_links(state: dict[str, Any]) -> PageLinks | None:
    """
    The identifiers on a web shop's page, as they would be stored.

    :param state: The analysis state (``url``, ``facts``, ``page_type``, ``page_content``).
    :return: The identifiers, or ``None`` when the page isn't a web shop.
    """
    domain = shop_domain(state)
    if domain is None:
        return None
    facts = state["facts"]
    source = page_source(state.get("page_content", ""), facts)
    fp = await asyncio.to_thread(fingerprint, source)
    links = dict(fp.links)
    emails, phones, domains = fp.emails, fp.phones, set[str]()
    if state.get("page_type") == "service_site":
        emails, phones, domains = _service_contacts(facts, emails, phones)
    if (key := contact_key()) is not None:
        addresses = {a for a in map(normalise_address, facts.addresses[:MAX_ADDRESSES]) if a}
        hashed = (("email", emails), ("phone", phones), ("email_domain", domains), ("address", addresses))
        for kind, values in hashed:
            if values:
                links[kind] = {contact_hash(kind, v, key).hex() for v in values}
    apps = tuple(sorted({label for category, label in scan(source) if category != "supplier_trace"}))
    return PageLinks(domain, links, fp.skeleton, apps)


def _service_contacts(facts: PageFacts, emails: set[str], phones: set[str]) -> tuple[set[str], set[str], set[str]]:
    """
    A service site's contacts: the page's, the numbers its facts read, and its e-mail domains.

    The facts read national formats too ("030 234 5678"); webmail domains, which millions share, are left out.

    :param facts: The page facts.
    :param emails: The e-mail addresses the fingerprint found.
    :param phones: The phone numbers the fingerprint found.
    :return: E-mail addresses, phone numbers and e-mail domains, each at most :data:`MAX_IDS_PER_KIND`.
    """
    numbers = set(phones) | {n for p in facts.phones if p.number.startswith("+") and (n := normalise_phone(p.number))}
    addresses = set(emails) | {e.address for e in facts.emails}
    domains = {a.rsplit("@", 1)[1] for a in addresses if "@" in a} - WEBMAIL_DOMAINS
    return (
        set(sorted(addresses)[:MAX_IDS_PER_KIND]),
        set(sorted(numbers)[:MAX_IDS_PER_KIND]),
        set(sorted(domains)[:MAX_IDS_PER_KIND]),
    )


def fingerprint_digest(page: PageLinks) -> bytes:
    """
    A digest of what a page shows about its shop: any change in identifiers, apps or template changes it.

    :param page: The page's identifiers.
    :return: 32 bytes.
    """
    parts = [f"{k}={','.join(sorted(v))}" for k, v in sorted(page.links.items())]
    parts += [f"apps={','.join(page.apps)}", f"skeleton={page.skeleton}"]
    return hashlib.sha256("\n".join(parts).encode()).digest()


async def observation(state: dict[str, Any]) -> ShopRecord | None:
    """
    What a finished analysis saw of the shop, from the page and the lookups it already made (none are added).

    :param state: The final analysis state.
    :return: The record, or ``None`` when the page wasn't a web shop.
    """
    page = await page_links(state)
    if page is None:
        return None
    facts, host = state["facts"], hostname(state.get("url", ""))
    rdap = await fact_cache.peek("rdap", host) or {}
    cert = await fact_cache.peek("crtsh", host)
    return ShopRecord(
        domain=page.domain,
        day=today(),
        platform=facts.platform,
        apps=page.apps,
        skeleton=page.skeleton,
        registered=_date(rdap.get("registered")),
        registrar=(str(rdap["registrar"])[:200] if rdap.get("registrar") else None),
        first_cert=_date(cert),
        links=page.links,
        fingerprint=fingerprint_digest(page),
    )


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


async def observe(state: dict[str, Any]) -> None:
    """
    Record a finished analysis's shop. Never raises: a failing store costs the fingerprint, not the analysis.

    :param state: The final analysis state.
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    try:
        rec = await observation(state)
        if rec is None:
            return
        await _store.record(rec)
        if _purged_on != rec.day:
            _purged_on = rec.day
            await _store.purge(rec.day)
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        logger.warning("shop_store_failed", error=type(exc).__name__)


def observe_later(state: dict[str, Any]) -> None:
    """
    Record the shop in the background, after the analysis has answered (off when ``fingerprints`` is off).

    Never the test shop (:mod:`unfret.test_shop`): its tricks are made up.

    :param state: The final analysis state.
    """
    if not settings.fingerprints or is_test_shop(str(state.get("url") or "")):
        return
    task = asyncio.create_task(observe(dict(state)))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for background writes (tests and shutdown)."""
    await background.drain(_pending)
