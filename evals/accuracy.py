"""
The numbers on the public ``/accuracy`` page: how often each verdict is right, from a replay of the labelled corpus.

Usage::

    uv run python -m evals.accuracy [--reuse STAMP] [--out src/unfret/static/accuracy.json] [--concurrency 8]
    uv run python -m evals.accuracy --publish VERSION

Replays every labelled page (train and test, ``--with-url``, recorded LLM responses only, as ``evals.run --replay
--split all --with-url``) twice: every page class with the ads and tradespeople checks on, and the product pages
again with them off. Each run's outcomes go to ``<corpus>/reports/accuracy-<stamp>-{on,off}-outcomes.jsonl``
(private, next to the corpus), so ``--reuse <stamp>`` can redo the summary without replaying.

The summary written to ``--out`` holds **aggregates only**: counts per kind of page, label origin and market,
per-verdict counts and shares, and the scorer's version. Never a URL, a shop's or seller's name, a page id or any
other corpus record; :func:`check_aggregates_only` refuses to write a summary that holds anything that looks like
one. A page whose recorded LLM responses are missing from the cache fails the replay and is counted as such, so the
page can say what couldn't be measured rather than hide it.

It measures the scorer the server would run: with ``UNFRET_MODEL_PATH`` set to a retrained (private) scorer, the
summary goes next to it by default (``<store>/<version>/accuracy.json``, see :mod:`evals.release`) and its
``RELEASE.md`` is updated with the numbers; the page's ``static/accuracy.json`` stays the open baseline's.

``--publish VERSION [--released DATE]`` then puts that retrained scorer's summary on the page, as the numbers of the
scorer unfret.net runs: after checking ``<store>/<version>/accuracy.json`` again (:func:`check_publishable`), it adds
it to ``static/accuracy-history.json`` as that version's entry, or replaces the entry it already has (keeping its
release date unless ``--released`` gives one; a new entry's is today). The history keeps every version published,
so the page can show the earlier ones. Only that summary leaves the model store, never the model or anything of
the corpus. A replay of the open baseline (the default ``--out``) updates the baseline's entry the same way.
"""

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from evals import release
from evals.corpus import PRODUCT_CLASS, UNRELIABLE, Corpus, LabelRecord, page_class
from evals.metrics import BANDS, Outcome, roc_auc
from evals.run import DEFAULT_EXCLUDED, configure, evaluate
from unfret.accuracy import KINDS, entry, newest_first
from unfret.config import settings
from unfret.facts import parse_page
from unfret.graph import SKILLS
from unfret.market import shop_market
from unfret.scoring import MODEL_PATH, configured_path, load_model

logger = structlog.get_logger("unfret.evals")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "unfret" / "static" / "accuracy.json"
HISTORY_OUT = DEFAULT_OUT.with_name("accuracy-history.json")

# Where each dataset's labels come from, in the general terms the page uses. A new dataset must be placed here
# before the page can count it (the script refuses unknown ones rather than mislabel them).
LABEL_ORIGINS: dict[str, str] = {
    "coi-risky-eshops": "consumer_authority",
    "acm-rogue-traders": "consumer_authority",
    "beyondphish-sp2023": "research_dataset",
    "mendeley-m7xtkx7g5m": "research_dataset",
    "us-established-shops": "established_business",
    "eu-legit-shops-2026": "established_business",
    "golden-well-known-sellers": "established_business",
    "osm-established-trades": "established_business",
    "amazon-reviews-2023": "synthetic",
    "p2p-synthetic-2026": "synthetic",
    "service-synthetic-2026": "synthetic",
}

# The groups the page reports on, by page class.
GROUPS: dict[str, tuple[str, ...]] = {
    "products": (PRODUCT_CLASS,),
    "ads": ("p2p_listing",),
    "trades": ("service_site", "service_listing"),
}

# Checks a group's pages may miss in the replay and still count, said on the page. The cache snapshot has no recorded
# LLM read (``content``) of the ads and tradespeople's pages; their verdicts come from rules, which still run on
# everything else. Product pages must have every check: their scorer weighs the LLM read heavily.
TOLERATED_MISSING: dict[str, frozenset[str]] = {
    "products": frozenset(),
    "ads": frozenset({"content"}),
    "trades": frozenset({"content"}),
}

# Ad platforms by the market their users are in (eBay and Facebook Marketplace serve both).
AD_PLATFORM_MARKETS: dict[str, str] = {
    "Marktplaats": "eu",
    "2dehands": "eu",
    "Vinted": "eu",
    "Craigslist": "us",
    "eBay": "both",
    "Facebook Marketplace": "both",
}

# Datasets whose market is known from where they come from, not read from the page: the marketplace product pages are
# rendered from Amazon's US review data, whatever currency the rendering shows.
DATASET_MARKETS: dict[str, str] = {"amazon-reviews-2023": "us"}

WARNING_BANDS = frozenset({"careful", "junk_or_scam"})


def missing_skills(outcome: Outcome) -> frozenset[str]:
    """
    The checks a page's replay ran without (their recorded LLM answer was missing).

    :param outcome: A pipeline result; its note reads ``"2 skill(s) unavailable: content, reviews"``.
    :return: The skills' names (``"?"`` for a note that doesn't name them).
    """
    if not outcome.note:
        return frozenset()
    _, _, names = outcome.note.partition(":")
    return frozenset(n.strip() for n in names.split(",") if n.strip()) or frozenset({"?"})


def complete(outcome: Outcome, tolerated: frozenset[str] = frozenset()) -> bool:
    """
    Whether a page counts: it got a verdict, and every check ran but the ``tolerated`` ones.

    A page whose recorded LLM answer is missing still gets a verdict in replay, with that check "unavailable"; the
    real service would have run it, so such a verdict rests on less evidence than a real one and counts only where
    the page says so.

    :param outcome: A pipeline result.
    :param tolerated: Checks the page may have missed.
    :return: Whether it counts.
    """
    return outcome.scored and outcome.band in BANDS and missing_skills(outcome) <= tolerated


def kind(record: LabelRecord) -> str:
    """
    The kind of page a record is, as the page names it.

    :param record: The record.
    :return: ``shop`` (a shop's own product page), ``marketplace_product``, ``ad``, ``trade_site`` or
        ``trade_profile``.
    """
    cls = page_class(record)
    if cls == PRODUCT_CLASS:
        return "marketplace_product" if record.page_type == "aggregate_listing" else "shop"
    return {"p2p_listing": "ad", "service_site": "trade_site", "service_listing": "trade_profile"}[cls]


def label_origin(record: LabelRecord) -> str:
    """
    Where a record's label comes from, in general terms.

    :param record: The record.
    :return: One of the :data:`LABEL_ORIGINS` values.
    :raises ValueError: For a dataset not in :data:`LABEL_ORIGINS`.
    """
    try:
        return LABEL_ORIGINS[record.origin_dataset]
    except KeyError:
        raise ValueError(f"place dataset {record.origin_dataset!r} in evals.accuracy.LABEL_ORIGINS") from None


def _notes(record: LabelRecord) -> dict[str, Any]:
    try:
        notes = json.loads(record.notes) if record.notes else {}
    except json.JSONDecodeError:
        return {}
    return notes if isinstance(notes, dict) else {}


async def market(corpus: Corpus, record: LabelRecord) -> str:
    """
    The market a page is from: ``eu``, ``us``, ``both`` (an ad platform serving both) or ``other``.

    Datasets in :data:`DATASET_MARKETS`: theirs. Product pages: :func:`unfret.market.shop_market` on the page, as the
    service decides it. Ads: the platform's.
    Tradespeople: the country the source recorded (the Dutch authority's decisions are Dutch).

    :param corpus: The corpus holding the page.
    :param record: The record.
    :return: The market.
    """
    cls = page_class(record)
    if record.origin_dataset in DATASET_MARKETS:
        return DATASET_MARKETS[record.origin_dataset]
    if cls == PRODUCT_CLASS:
        facts, text = await asyncio.to_thread(parse_page, corpus.read_page(record.id), record.origin_url)
        return shop_market(facts, record.origin_url, text)
    if cls == "p2p_listing":
        return AD_PLATFORM_MARKETS.get(record.platform, "other")
    notes = _notes(record)
    country = str(notes.get("country") or notes.get("market") or "").lower()
    if record.origin_dataset == "acm-rogue-traders" or country == "nl":
        return "eu"
    return "us" if country == "us" else "other"


def share(part: int, whole: int) -> float | None:
    """
    ``part / whole`` rounded to three decimals.

    :param part: The count.
    :param whole: The total.
    :return: The share, or ``None`` for an empty total.
    """
    return round(part / whole, 3) if whole else None


def band_summary(outcomes: Sequence[Outcome], tolerated: frozenset[str] = frozenset()) -> dict[str, Any]:
    """
    Per verdict band: how many pages, how many of them unreliable, and what share of all unreliable pages it holds.

    Only scored pages count. ``caught`` is the share of unreliable pages warned about ("Think twice" or "Serious
    concerns"), ``caught_with_worth_a_look`` adds "Worth a look"; ``warning_precision`` is the share of warned
    pages that are unreliable, and ``legit_warned`` the share of legit pages warned about.

    :param outcomes: Pipeline results.
    :param tolerated: Checks a page may have missed and still count (:func:`complete`).
    :return: The summary.
    """
    scored = [o for o in outcomes if complete(o, tolerated)]
    unreliable_total = sum(o.label in UNRELIABLE for o in scored)
    legit_total = len(scored) - unreliable_total
    bands = {}
    for band in BANDS:
        members = [o for o in scored if o.band == band]
        unreliable = sum(o.label in UNRELIABLE for o in members)
        bands[band] = {
            "pages": len(members),
            "unreliable": unreliable,
            "legit": len(members) - unreliable,
            "unreliable_share": share(unreliable, len(members)),
            "share_of_unreliable": share(unreliable, unreliable_total),
            "share_of_legit": share(len(members) - unreliable, legit_total),
        }
    warned = [o for o in scored if o.band in WARNING_BANDS]
    warned_unreliable = sum(o.label in UNRELIABLE for o in warned)
    looked = sum(o.label in UNRELIABLE for o in scored if o.band != "reliable")
    auc = roc_auc(scored)
    return {
        "scored": len(scored),
        "unreliable": unreliable_total,
        "legit": legit_total,
        "bands": bands,
        "caught": share(warned_unreliable, unreliable_total),
        "caught_with_worth_a_look": share(looked, unreliable_total),
        "warning_precision": share(warned_unreliable, len(warned)),
        "legit_warned": share(len(warned) - warned_unreliable, legit_total),
        "roc_auc": round(auc, 3) if auc is not None else None,
    }


def coverage(outcomes: Sequence[Outcome], tolerated: frozenset[str] = frozenset()) -> dict[str, int]:
    """
    How many pages got a verdict, were set aside as not a page Unfret assesses, or couldn't be measured.

    :param outcomes: Pipeline results.
    :param tolerated: Checks a page may have missed and still count (:func:`complete`).
    :return: The counts. ``missing_recordings`` are pages that don't count for want of a recorded LLM answer (the
        replay failed, or it ran without a check that isn't tolerated); ``without_llm_read`` are counted pages that
        ran without the LLM read of the page.
    """
    return {
        "labelled": len(outcomes),
        "scored": sum(complete(o, tolerated) for o in outcomes),
        "not_assessed": sum(1 for o in outcomes if not o.error and o.score == -1),
        "missing_recordings": sum(1 for o in outcomes if _missing(o, tolerated)),
        "without_llm_read": sum(1 for o in outcomes if complete(o, tolerated) and "content" in missing_skills(o)),
    }


def _missing(outcome: Outcome, tolerated: frozenset[str]) -> bool:
    return bool(outcome.error) or (outcome.scored and not complete(outcome, tolerated))


def legit_shop_alarms(outcomes: Sequence[Outcome], records: dict[str, LabelRecord], markets: dict[str, str]) -> Any:
    """
    False alarms on legitimate shops: the share of legit shop pages shown each verdict, overall and per market.

    :param outcomes: Pipeline results (product pages).
    :param records: The records, by id.
    :param markets: Each page's market, by id.
    :return: Per scope (``all``, ``eu``, ``us``): ``pages``, ``warned`` (Think twice or Serious concerns),
        ``worth_a_look``, ``serious`` and their shares.
    """
    shops = [o for o in outcomes if complete(o) and o.label == "legit" and kind(records[o.id]) == "shop"]
    result: dict[str, Any] = {}
    for scope in ("all", "eu", "us"):
        members = [o for o in shops if scope == "all" or markets.get(o.id) == scope]
        warned = sum(o.band in WARNING_BANDS for o in members)
        worth = sum(o.band == "concerns" for o in members)
        serious = sum(o.band == "junk_or_scam" for o in members)
        result[scope] = {
            "pages": len(members),
            "warned": warned,
            "warned_share": share(warned, len(members)),
            "worth_a_look": worth,
            "worth_a_look_share": share(worth, len(members)),
            "serious": serious,
            "serious_share": share(serious, len(members)),
        }
    return result


def composition(records: Sequence[LabelRecord], markets: dict[str, str], unlabelled: int) -> dict[str, Any]:
    """
    What the eval set is made of, in counts only.

    :param records: The labelled records.
    :param markets: Each page's market, by id.
    :param unlabelled: Pages collected but left without a label (sources disagreed).
    :return: Totals per kind of page (with labels), label origin, market, split, and synthetic pages.
    """
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    origins: dict[str, Counter[str]] = defaultdict(Counter)
    market_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        verdict = "unreliable" if r.label in UNRELIABLE else "legit"
        kinds[kind(r)][verdict] += 1
        origins[label_origin(r)][verdict] += 1
        market_counts[kind(r)][markets.get(r.id, "other")] += 1
    split = Counter(r.split or "none" for r in records)
    return {
        "pages": len(records),
        "unlabelled_set_aside": unlabelled,
        "kinds": {k: {"pages": c.total(), **dict(sorted(c.items()))} for k, c in sorted(kinds.items())},
        "label_origins": {k: {"pages": c.total(), **dict(sorted(c.items()))} for k, c in sorted(origins.items())},
        "markets": {k: dict(sorted(c.items())) for k, c in sorted(market_counts.items())},
        "split": {"train": split["train"], "test": split["test"]},
        "synthetic": sum(1 for r in records if r.source == "synthetic"),
        "second_opinion_agreed": sum(1 for r in records if r.label_source in {"agreed", "human"}),
    }


def group_summary(
    outcomes: Sequence[Outcome],
    records: dict[str, LabelRecord],
    classes: Iterable[str],
    tolerated: frozenset[str] = frozenset(),
) -> Any:
    """
    The coverage and verdict summary of one group of page classes, with a per-class coverage breakdown.

    :param outcomes: Pipeline results.
    :param records: The records, by id.
    :param classes: The group's page classes.
    :param tolerated: Checks a page may have missed and still count (:func:`complete`).
    :return: ``coverage``, ``per_class`` coverage, the pages left out and those counted by label origin
        (``missing_by_label_origin``, ``scored_by_label_origin``), and the :func:`band_summary` fields.
    """
    wanted = set(classes)
    members = [o for o in outcomes if page_class(records[o.id]) in wanted]
    per_class = {
        cls: coverage([o for o in members if page_class(records[o.id]) == cls], tolerated) for cls in sorted(wanted)
    }
    # Pages that couldn't be measured, by where their labels come from, so the page can say what's left out.
    missing = Counter(label_origin(records[o.id]) for o in members if _missing(o, tolerated))
    measured = Counter(label_origin(records[o.id]) for o in members if complete(o, tolerated))
    return {
        "coverage": coverage(members, tolerated),
        "per_class": per_class,
        "missing_by_label_origin": dict(sorted(missing.items())),
        "scored_by_label_origin": dict(sorted(measured.items())),
        **band_summary(members, tolerated),
    }


_FORBIDDEN = re.compile(
    r"https?://|www\.|\.(com|nl|de|net|org|eu|shop|store|be|cz)\b|@|-[0-9a-f]{12}\b", re.IGNORECASE
)


def check_aggregates_only(summary: object) -> None:
    """
    Refuse a summary that holds anything but aggregate numbers and the fixed labels this module writes.

    :param summary: The summary.
    :raises ValueError: On a URL, domain, e-mail address or corpus page id anywhere in it.
    """
    text = json.dumps(summary)
    if match := _FORBIDDEN.search(text):
        raise ValueError(f"the accuracy summary must hold aggregates only; found {match.group(0)!r}")


_KEY_RE = re.compile(r"[a-z0-9_]+")  # the fixed field names this module writes


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [k for k in value if not _KEY_RE.fullmatch(str(k))] + [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def check_publishable(summary: dict[str, Any], version: str) -> None:
    """
    Refuse a retrained scorer's summary for the public page unless it is aggregates only, for that version.

    Beyond :func:`check_aggregates_only`: the only strings in it are the date, the scorer's version, the language
    model's name and the replayed split, its keys are field names (no names, records or text), and its version is
    the one asked for.

    :param summary: The summary :func:`summarise` wrote next to the scorer.
    :param version: The scorer's version.
    :raises ValueError: On anything else.
    """
    check_aggregates_only(summary)
    if summary.get("scorer_version") != version:
        raise ValueError(f"the summary is for scorer {summary.get('scorer_version')!r}, not {version!r}")
    allowed = {summary.get("generated"), version, summary.get("llm_model"), "all"}
    if extra := [s for s in _strings(summary) if s not in allowed]:
        raise ValueError(f"the accuracy summary must hold aggregates only; found {extra[0]!r}")


ENTRY_FIELDS = frozenset({"version", "kind", "released", "measured", "corpus_pages", "metrics", "summary"})
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def check_history(history: dict[str, Any]) -> None:
    """
    Refuse a history for the public page unless every entry is a checked summary under the fixed fields.

    Each entry has exactly :data:`ENTRY_FIELDS`, a known ``kind``, ISO dates, a summary that passes
    :func:`check_publishable` for the entry's version, and the ``metrics``, ``corpus_pages`` and ``measured`` that
    summary gives (they can't say anything the summary doesn't). No version twice.

    :param history: The parsed ``accuracy-history.json``.
    :raises ValueError: On anything else.
    """
    if set(history) != {"entries"} or not isinstance(history["entries"], list):
        raise ValueError('the accuracy history must be {"entries": [...]}')
    seen: set[str] = set()
    for e in history["entries"]:
        if not isinstance(e, dict) or set(e) != ENTRY_FIELDS:
            raise ValueError(f"a history entry must have exactly the fields {sorted(ENTRY_FIELDS)}")
        version = e["version"]
        if not isinstance(version, str) or version in seen:
            raise ValueError(f"version {version!r} is not a version or is in the history twice")
        seen.add(version)
        if e["kind"] not in KINDS:
            raise ValueError(f"{version}: kind {e['kind']!r} is not one of {KINDS}")
        if not (e["released"] is None or (isinstance(e["released"], str) and _DATE_RE.fullmatch(e["released"]))):
            raise ValueError(f"{version}: released {e['released']!r} is not an ISO date")
        check_publishable(e["summary"], version)
        derived = entry(e["summary"], e["kind"], e["released"])
        for field in ("measured", "corpus_pages", "metrics"):
            if e[field] != derived[field]:
                raise ValueError(f"{version}: {field} isn't what its summary gives")
    check_aggregates_only(history)


def record(summary: dict[str, Any], kind: str, released: str | None = None, out: Path = HISTORY_OUT) -> Path:
    """
    Add a version's replay summary to the history, or replace the entry that version already has.

    :param summary: The checked replay summary.
    :param kind: ``private`` (a retrained scorer) or ``public`` (an open baseline).
    :param released: Its release date, ISO; by default the existing entry's, or today for a new one.
    :param out: The history file.
    :return: ``out``.
    """
    entries = json.loads(out.read_text(encoding="utf-8"))["entries"] if out.is_file() else []
    version = summary.get("scorer_version")
    previous = next((e for e in entries if e.get("version") == version), None)
    day = released or (previous or {}).get("released") or datetime.now(UTC).date().isoformat()
    entries = [e for e in entries if e.get("version") != version] + [entry(summary, kind, day)]
    history = {"entries": newest_first(entries)}
    check_history(history)
    out.write_text(json.dumps(history, indent=2) + "\n")
    return out


def publish(version: str, root: Path | None = None, out: Path = HISTORY_OUT, released: str | None = None) -> Path:
    """
    Put a retrained scorer's replay numbers on the page, as those of the scorer unfret.net runs.

    :param version: The scorer's version, in the model store.
    :param root: The model store; :func:`evals.release.store` by default.
    :param out: The history the page reads.
    :param released: Its release date, ISO (see :func:`record`).
    :return: ``out``.
    :raises FileNotFoundError: When the version has no replay summary yet (run ``make accuracy`` with it first).
    """
    source = (root or release.store()) / version / release.ACCURACY
    if not source.is_file():
        raise FileNotFoundError(
            f"{source}: replay the corpus with this scorer first (UNFRET_MODEL_PATH=… make accuracy)"
        )
    summary = json.loads(source.read_text(encoding="utf-8"))
    check_publishable(summary, version)
    return record(summary, "private", released, out)


def summarise(
    on: Sequence[Outcome],
    off: Sequence[Outcome],
    records: dict[str, LabelRecord],
    markets: dict[str, str],
    unlabelled: int,
) -> dict[str, Any]:
    """
    The page's whole summary.

    :param on: Outcomes with the ads and tradespeople checks on, every class.
    :param off: Outcomes with them off, product pages only.
    :param records: The labelled records, by id.
    :param markets: Each page's market, by id.
    :param unlabelled: Pages collected but left without a label.
    :return: The summary (aggregates only).
    """
    model = load_model()
    products_on = [o for o in on if page_class(records[o.id]) == PRODUCT_CLASS]
    test = [o for o in products_on if records[o.id].split == "test"]
    summary: dict[str, Any] = {
        "generated": datetime.now(UTC).date().isoformat(),
        "scorer_version": model.version if model else None,
        "llm_model": settings.llm_model,
        "replay": {"split": "all", "with_url": True, "network_lookups": False},
        "corpus": composition(list(records.values()), markets, unlabelled),
        "groups": {
            name: group_summary(on, records, classes, TOLERATED_MISSING[name]) for name, classes in GROUPS.items()
        },
        "products_test_only": {"coverage": coverage(test), **band_summary(test)},
        "products_checks_off": {"coverage": coverage(off), **band_summary(off)},
        "legit_shops": legit_shop_alarms(products_on, records, markets),
    }
    check_aggregates_only(summary)
    return summary


def _outcomes_path(corpus: Corpus, stamp: str, mode: str) -> Path:
    return corpus.root / "reports" / f"accuracy-{stamp}-{mode}-outcomes.jsonl"


def _read_outcomes(path: Path) -> list[Outcome]:
    return [Outcome(**json.loads(line)) for line in path.read_text().splitlines() if line]


def _write_outcomes(path: Path, outcomes: Sequence[Outcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(o)) + "\n" for o in sorted(outcomes, key=lambda o: o.id)))


async def _markets(corpus: Corpus, records: Sequence[LabelRecord]) -> dict[str, str]:
    semaphore = asyncio.Semaphore(8)

    async def one(record: LabelRecord) -> tuple[str, str]:
        async with semaphore:
            return record.id, await market(corpus, record)

    return dict(await asyncio.gather(*(one(r) for r in records)))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--reuse", metavar="STAMP", help="summarise an earlier replay's outcomes instead of replaying")
    # A retrained scorer's numbers go next to it, not into the repository's page.
    configured = configured_path(MODEL_PATH)
    default_out = configured.parent / release.ACCURACY if configured != MODEL_PATH else DEFAULT_OUT
    parser.add_argument("--out", type=Path, default=default_out)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--publish",
        metavar="VERSION",
        help="put a retrained scorer's replay numbers on the page (its entry in accuracy-history.json)",
    )
    parser.add_argument("--released", metavar="DATE", help="with --publish: its release date (default: today)")
    args = parser.parse_args()
    if args.publish:
        out = publish(args.publish, released=args.released)
        entries = json.loads(out.read_text())["entries"]
        print(json.dumps(next(e for e in entries if e["version"] == args.publish), indent=2))  # noqa: T201
        return

    corpus = Corpus()
    all_records = corpus.labels()
    records = {r.id: r for r in all_records.values() if r.label}
    unlabelled = len(all_records) - len(records)
    for r in records.values():
        label_origin(r)  # fail early on an unplaced dataset

    if args.reuse:
        on = _read_outcomes(_outcomes_path(corpus, args.reuse, "on"))
        off = _read_outcomes(_outcomes_path(corpus, args.reuse, "off"))
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        configure(corpus, replay=True)
        skills = [s.name for s in SKILLS if s.name not in DEFAULT_EXCLUDED]
        products = [r for r in records.values() if page_class(r) == PRODUCT_CLASS]
        settings.expansion_classes = False
        logger.info("accuracy_replay", mode="off", pages=len(products))
        off = asyncio.run(evaluate(corpus, products, skills, True, args.concurrency))
        _write_outcomes(_outcomes_path(corpus, stamp, "off"), off)
        settings.expansion_classes = True
        logger.info("accuracy_replay", mode="on", pages=len(records))
        on = asyncio.run(evaluate(corpus, list(records.values()), skills, True, args.concurrency))
        _write_outcomes(_outcomes_path(corpus, stamp, "on"), on)
        logger.info("accuracy_outcomes", stamp=stamp)

    markets = asyncio.run(_markets(corpus, list(records.values())))
    summary = summarise(on, off, records, markets, unlabelled)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    if args.out.resolve() == DEFAULT_OUT.resolve():
        record(summary, "public")  # the open baseline's entry in the page's history
    if (args.out.parent / release.ARTIFACT).exists() and args.out.name == release.ACCURACY:
        release.write_note(args.out.parent)  # a private release: its note gets the replay's numbers
    print(json.dumps(summary, indent=2))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
