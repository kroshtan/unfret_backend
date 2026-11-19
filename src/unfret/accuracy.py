"""
The ``/accuracy`` page: how often Unfret's verdicts are right, from the numbers ``make accuracy`` measured.

Summaries of a corpus replay, aggregates only (``evals/accuracy.py`` writes them; never a URL or a corpus record):

- ``static/accuracy.json``: the open model, the baseline scorer in this repository (what a self-hosted server runs).
- ``static/accuracy-history.json``: every published scorer version, ``{"entries": [...]}``, one entry per version
  (:func:`entry`): ``version``, ``kind`` (``private``: a retrained scorer unfret.net runs or ran, never published;
  ``public``: an open model, committed in this repository now or in an earlier release), ``released`` and
  ``measured`` (ISO dates), ``corpus_pages`` (the labelled pages it was measured on), ``metrics`` (the side-by-side
  table's numbers, :func:`metrics`) and ``summary`` (the whole replay summary, as ``accuracy.json``).
  ``make accuracy ACCURACY_ARGS="--publish VERSION"`` adds or updates a retrained scorer's entry from the model store
  (``--kind public`` an earlier open model's, replayed afterwards); ``make accuracy`` for the baseline updates the
  baseline's.

The page names the two kinds :data:`KIND_LABELS`. It leads with the summary of the scorer this server runs
(:func:`page_summary`), shows the newest private model and the open model side by side, and every earlier version
below them, newest first, folded.
``static/accuracy.html`` is the page's prose with placeholders that this module fills:

- ``{{num:path}}``: a count (``1,582``); ``{{pct:path}}``: a share as a percentage (``82%``, ``6.9%``);
  ``{{text:path}}``: a string; ``{{date:path}}``: an ISO date as "November 17, 2026". ``path`` is a dotted path
  into the JSON (``groups.products.caught``).
- ``{{block:name}}``: a generated piece (a verdict table, a coverage note, a warning that a group's measured pages
  are mostly synthetic), see :data:`BLOCKS`.

A number the replay couldn't measure renders as "not measured", never as a guess.
"""

import html
import json
import re
from collections.abc import Callable
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from unfret.scoring import load_model
from unfret.shopper import catalogue

ACCURACY_PATH = Path(__file__).resolve().parent / "static" / "accuracy.json"
HISTORY_PATH = ACCURACY_PATH.with_name("accuracy-history.json")

KINDS = ("private", "public")

# What the page calls each kind: the side-by-side table's headings and the earlier versions' boxes; the prose (the
# page's, :func:`scorer_name`) uses the same words. ``private``: retrained, runs on unfret.net, not published;
# ``public``: in this repository, what a self-hosted server runs.
KIND_LABELS = {"private": "Private model (unfret.net)", "public": "Open model (in the code)"}

BANDS = ("reliable", "concerns", "careful", "junk_or_scam")
NOT_MEASURED = "not measured"

_PLACEHOLDER_RE = re.compile(r"\{\{(num|pct|text|date|block):([A-Za-z0-9_.:]+)\}\}")

KIND_NAMES = {
    "shop": "Shops' own product pages",
    "marketplace_product": "Product pages on a marketplace",
    "ad": "Second-hand ads",
    "trade_site": "Tradespeople's own websites",
    "trade_profile": "Tradespeople's profiles on a platform",
}

ORIGIN_NAMES = {
    "consumer_authority": (
        "Consumer-authority warning lists",
        (
            "shops on the Czech Trade Inspection Authority's list of risky web shops, and rogue trade websites named "
            "in decisions of the Dutch consumer authority (ACM), as archived while the warning applied"
        ),
    ),
    "research_dataset": (
        "Published research datasets",
        "fraudulent-shop and phishing datasets from academic studies, with the legitimate shops one of them lists",
    ),
    "established_business": (
        "Long-established businesses",
        (
            "shops and trade firms with many years of history (from public sources such as Wikidata and "
            "OpenStreetMap, and a hand-picked list of EU retailers)"
        ),
    ),
    "synthetic": (
        "Synthetic pages",
        (
            "pages we generated: marketplace product pages built from public review data, and ads and trade pages "
            "built only from scam and normal patterns that public consumer warnings document"
        ),
    ),
}

MARKET_NAMES = {"eu": "Europe", "us": "US", "both": "Europe and US", "other": "elsewhere"}

# What a group's pages are called in its coverage note.
GROUP_NOUNS = {"products": "product pages", "ads": "ads", "trades": "trade pages"}

# From this share of synthetic pages among those measured, a group's numbers say little about real pages.
MOSTLY_SYNTHETIC = 0.5


@cache
def summary() -> dict[str, Any]:
    """
    The open model's measured numbers (the baseline scorer in this repository).

    :return: The parsed ``accuracy.json``.
    """
    data: dict[str, Any] = json.loads(ACCURACY_PATH.read_text(encoding="utf-8"))
    return data


def metrics(data: dict[str, Any]) -> dict[str, Any]:
    """
    The numbers the scorers' table shows for a replay summary, under fixed names.

    :param data: A replay summary.
    :return: Product pages that got a verdict (``products_scored``), unreliable product pages warned about
        (``caught``), warnings right (``warning_precision``), "Serious concerns" right (``serious_right``), legitimate
        shops warned about, at "Worth a look" and at "Serious concerns" (``legit_shops_warned``,
        ``legit_shops_worth_a_look``, ``legit_shops_serious``) and the ROC AUC; ``None`` where not measured.
    """
    return {
        "products_scored": lookup(data, "groups.products.scored"),
        "caught": lookup(data, "groups.products.caught"),
        "warning_precision": lookup(data, "groups.products.warning_precision"),
        "serious_right": lookup(data, "groups.products.bands.junk_or_scam.unreliable_share"),
        "legit_shops_warned": lookup(data, "legit_shops.all.warned_share"),
        "legit_shops_worth_a_look": lookup(data, "legit_shops.all.worth_a_look_share"),
        "legit_shops_serious": lookup(data, "legit_shops.all.serious_share"),
        "roc_auc": lookup(data, "groups.products.roc_auc"),
    }


def entry(data: dict[str, Any], kind: str, released: str | None) -> dict[str, Any]:
    """
    A scorer version's entry in the history, from its replay summary.

    :param data: The replay summary.
    :param kind: ``private`` (retrained) or ``public`` (an open baseline).
    :param released: The day it was released, ISO; ``None`` when unknown.
    :return: The entry.
    """
    return {
        "version": data.get("scorer_version"),
        "kind": kind,
        "released": released,
        "measured": data.get("generated"),
        "corpus_pages": lookup(data, "corpus.pages"),
        "metrics": metrics(data),
        "summary": data,
    }


def newest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    History entries, the most recently released first.

    :param entries: The entries.
    :return: Sorted by release date, then measuring date, then version, all descending.
    """
    return sorted(
        entries,
        key=lambda e: (str(e.get("released") or ""), str(e.get("measured") or ""), str(e["version"])),
        reverse=True,
    )


@cache
def history() -> list[dict[str, Any]]:
    """
    Every published scorer version's numbers.

    :return: ``accuracy-history.json``'s entries, newest first (none when the file is missing).
    """
    if not HISTORY_PATH.is_file():
        return []
    entries: list[dict[str, Any]] = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))["entries"]
    return newest_first(entries)


def service_entry() -> dict[str, Any] | None:
    """
    The newest private model's entry: the retrained scorer unfret.net runs.

    :return: The entry, or ``None`` when no retrained scorer's numbers are published.
    """
    return next((e for e in history() if e["kind"] == "private"), None)


def service_summary() -> dict[str, Any] | None:
    """
    The measured numbers of the retrained scorer unfret.net runs.

    :return: The newest retrained scorer's replay summary, or ``None`` when none is published.
    """
    service = service_entry()
    return service["summary"] if service else None


def baseline_entry() -> dict[str, Any]:
    """
    The open model's entry: the baseline scorer in this repository.

    :return: Its history entry, or one made from ``accuracy.json`` (release date unknown) when it has none.
    """
    version = summary().get("scorer_version")
    found = next((e for e in history() if e["kind"] == "public" and e["version"] == version), None)
    return found or entry(summary(), "public", None)


def earlier_entries() -> list[dict[str, Any]]:
    """
    The versions below the side-by-side table: every one but the newest private model and the open model.

    :return: Their entries, newest first.
    """
    leading = {e["version"] for e in (baseline_entry(), service_entry()) if e}
    return [e for e in history() if e["version"] not in leading]


def running_version() -> str | None:
    """
    The scorer this server runs.

    :return: Its version (``UNFRET_MODEL_PATH``'s, or the baseline's).
    """
    model = load_model()
    return model.version if model else None


def page_summary() -> dict[str, Any]:
    """
    The numbers the page leads with: those of the scorer this server runs, when published.

    :return: That version's summary (a retrained scorer on unfret.net), else the open baseline's (a self-hosted
        server, or one running a scorer whose numbers aren't published: the page then says so).
    """
    running = running_version()
    if running == summary().get("scorer_version"):
        return summary()
    found = next((e for e in history() if e["version"] == running), None)
    return found["summary"] if found else summary()


def lookup(data: dict[str, Any], path: str) -> Any:
    """
    The value at a dotted path.

    :param data: The summary.
    :param path: Keys separated by dots.
    :return: The value, or ``None`` when a key is missing.
    """
    value: Any = data
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def num(value: Any) -> str:
    """
    A count with thousands separators.

    :param value: The count.
    :return: The text, or "not measured".
    """
    return f"{int(value):,}" if isinstance(value, int | float) else NOT_MEASURED


def pct(value: Any) -> str:
    """
    A share as a percentage: whole percents, one decimal below 10% and just under 100%.

    :param value: The share, 0–1.
    :return: The text, or "not measured".
    """
    if not isinstance(value, int | float):
        return NOT_MEASURED
    percent = value * 100
    if 0 < percent < 9.95 or 99.5 <= percent < 100:
        return f"{percent:.1f}%"
    return f"{round(percent)}%"


def long_date(value: Any) -> str:
    """
    An ISO date written out.

    :param value: ``"2026-11-17"``.
    :return: ``"November 17, 2026"`` (US English, as the site), or "not measured".
    """
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return NOT_MEASURED
    return f"{day:%B} {day.day}, {day.year}"


def band_label(band: str) -> str:
    """
    A band's name as shoppers see it (the copy catalogue's).

    :param band: The band identifier.
    :return: The label.
    """
    return str(catalogue()["labels"][band])


def _band_cell(band: str) -> str:
    return f'<span class="band-dot {band}"></span>{html.escape(band_label(band))}'


def bands_table(data: dict[str, Any], group: str) -> str:
    """
    One group's verdict table: per verdict, how many pages, how many of them unreliable, and its share of them.

    :param data: The summary.
    :param group: ``products``, ``ads`` or ``trades``.
    :return: The table's HTML, or a sentence when the group wasn't measured.
    """
    g = lookup(data, f"groups.{group}")
    if not g or not g.get("scored"):
        return f"<p>{NOT_MEASURED.capitalize()}.</p>"
    rows = []
    for band in BANDS:
        b = g["bands"][band]
        rows.append(
            f'<tr><th scope="row" class="band-cell">{_band_cell(band)}</th><td>{num(b["pages"])}</td>'
            f'<td>{pct(b["unreliable_share"])} <span class="muted">({num(b["unreliable"])})</span></td>'
            f"<td>{pct(b['share_of_unreliable'])}</td></tr>"
        )
    return (
        '<div class="acc-table-wrap"><table class="acc-table">'
        '<thead><tr><th scope="col">Verdict</th><th scope="col">Pages</th>'
        '<th scope="col">Actually unreliable</th><th scope="col">Of all unreliable pages</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def coverage_note(data: dict[str, Any], group: str) -> str:
    """
    What the group's numbers rest on, and which pages couldn't be measured and why.

    :param data: The summary.
    :param group: ``products``, ``ads`` or ``trades``.
    :return: A paragraph of HTML.
    """
    g = lookup(data, f"groups.{group}")
    if not g:
        return ""
    c = g["coverage"]
    parts = [
        (
            f"Based on {num(g['scored'])} pages that got a verdict: {num(g['unreliable'])} unreliable and "
            f"{num(g['legit'])} legitimate."
        )
    ]
    synthetic = (g.get("scored_by_label_origin") or {}).get("synthetic", 0)
    if synthetic:
        parts.append(f"{num(synthetic)} of those {num(g['scored'])} pages are synthetic.")
    if c["not_assessed"]:
        parts.append(
            f"{num(c['not_assessed'])} more were set aside as not the kind of page Unfret assesses (an article, "
            "say), as the real service would."
        )
    if c.get("without_llm_read"):
        parts.append(
            f"<strong>{num(c['without_llm_read'])} of them were measured without the language model's reading of "
            "the text</strong>: its answers for these pages aren't in the recording we replay. The rule checks on "
            "everything else ran as usual, but in real use that reading can add a warning (scam-like wording, a push "
            "to pay outside the platform) and can hold back a <strong>Looks fine</strong>, so read these numbers as "
            "rough."
        )
    if c["missing_recordings"]:
        origins = ", ".join(
            f"{num(n)} from {ORIGIN_NAMES.get(o, (o,))[0].lower()}" for o, n in g["missing_by_label_origin"].items()
        )
        parts.append(
            f"<strong>{num(c['missing_recordings'])} of the {num(c['labelled'])} labelled "
            f"{GROUP_NOUNS.get(group, 'pages')} couldn't be "
            f"measured</strong> ({origins}): we replay recorded answers from the language model, and those pages' "
            "answers aren't in the recording yet. They are left out rather than guessed."
        )
    return f'<p class="small">{" ".join(parts)}</p>'


def products_scope(data: dict[str, Any]) -> str:
    """
    Which of the test pages the product numbers are about: the shops' and marketplaces' product pages, and the rest.

    :param data: The summary.
    :return: A sentence of HTML.
    """
    kinds = lookup(data, "corpus.kinds") or {}
    products = lookup(data, "groups.products.coverage.labelled")
    total = lookup(data, "corpus.pages")
    ads = lookup(data, "groups.ads.coverage.labelled")
    trades = lookup(data, "groups.trades.coverage.labelled")
    rest = total - products if isinstance(total, int) and isinstance(products, int) else None
    return (
        f"This section covers the {num(products)} product pages in the test set: "
        f"{num((kinds.get('shop') or {}).get('pages'))} from shops' own websites and "
        f"{num((kinds.get('marketplace_product') or {}).get('pages'))} on marketplaces. They are part of all "
        f"{num(total)} test pages; the other {num(rest)} are second-hand ads ({num(ads)}) and tradespeople's "
        f'websites and profiles ({num(trades)}), each with <a href="#ads">their own numbers below</a>.'
    )


def synthetic_note(data: dict[str, Any], group: str) -> str:
    """
    A plain warning when most of a group's measured pages are synthetic: its numbers say little about real pages.

    :param data: The summary.
    :param group: ``products``, ``ads`` or ``trades``.
    :return: A paragraph of HTML, or nothing when most of its measured pages are real.
    """
    g = lookup(data, f"groups.{group}")
    if not g or not g.get("scored"):
        return ""
    synthetic = (g.get("scored_by_label_origin") or {}).get("synthetic", 0)
    if synthetic / g["scored"] < MOSTLY_SYNTHETIC:
        return ""
    real = g["scored"] - synthetic
    real_missing = sum(n for o, n in (g.get("missing_by_label_origin") or {}).items() if o != "synthetic")
    waiting = f", and {num(real_missing)} more real ones couldn't be measured yet (see below)" if real_missing else ""
    return (
        f"<p><strong>{num(synthetic)} of the {num(g['scored'])} pages measured here "
        f"({pct(synthetic / g['scored'])}) are synthetic</strong>: test pages we made up from the patterns that "
        f"consumer warnings document. Only {num(real)} are real{waiting}. So these numbers mostly show that the rules "
        "recognise the patterns those pages were built from, and say little yet about real sites.</p>"
    )


def kinds_table(data: dict[str, Any]) -> str:
    """
    The test set by kind of page: pages, legitimate and unreliable.

    :param data: The summary.
    :return: The table's HTML.
    """
    kinds = lookup(data, "corpus.kinds") or {}
    rows = "".join(
        f'<tr><th scope="row">{html.escape(KIND_NAMES.get(k, k))}</th><td>{num(v.get("pages"))}</td>'
        f"<td>{num(v.get('legit', 0))}</td><td>{num(v.get('unreliable', 0))}</td></tr>"
        for k, v in sorted(kinds.items(), key=lambda kv: list(KIND_NAMES).index(kv[0]) if kv[0] in KIND_NAMES else 99)
    )
    return (
        '<div class="acc-table-wrap"><table class="acc-table">'
        '<thead><tr><th scope="col">Kind of page</th><th scope="col">Pages</th><th scope="col">Legitimate</th>'
        f'<th scope="col">Unreliable</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def origins_list(data: dict[str, Any]) -> str:
    """
    Where the labels come from, in general terms, with how many pages each gave.

    :param data: The summary.
    :return: A list's HTML.
    """
    origins = lookup(data, "corpus.label_origins") or {}
    items = []
    for key, (name, what) in ORIGIN_NAMES.items():
        if key not in origins:
            continue
        o = origins[key]
        counts = ", ".join(f"{num(o[side])} {side}" for side in ("legit", "unreliable") if o.get(side)).replace(
            "legit", "legitimate"
        )
        items.append(f"<li><strong>{html.escape(name)}</strong> ({counts}): {html.escape(what)}.</li>")
    return f"<ul>{''.join(items)}</ul>"


def legit_shops_table(data: dict[str, Any]) -> str:
    """
    False alarms on legitimate shops, overall and for European and US shops.

    :param data: The summary.
    :return: The table's HTML.
    """
    shops = lookup(data, "legit_shops") or {}
    names = {"all": "All legitimate shops", "eu": "European shops", "us": "US shops"}
    rows = "".join(
        f'<tr><th scope="row">{names[scope]}</th><td>{num(s["pages"])}</td><td>{pct(s["warned_share"])}</td>'
        f"<td>{pct(s['worth_a_look_share'])}</td></tr>"
        for scope in ("all", "eu", "us")
        if (s := shops.get(scope)) and s.get("pages")
    )
    return (
        '<div class="acc-table-wrap"><table class="acc-table">'
        '<thead><tr><th scope="col">Shops</th><th scope="col">Pages</th>'
        f'<th scope="col">{html.escape(band_label("careful"))} or worse</th>'
        f'<th scope="col">{html.escape(band_label("concerns"))}</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def markets_sentence(data: dict[str, Any]) -> str:
    """
    Where the test pages are from, per kind of page.

    :param data: The summary.
    :return: A sentence of HTML.
    """
    markets = lookup(data, "corpus.markets") or {}
    parts = []
    for k, name in KIND_NAMES.items():
        counts = markets.get(k)
        if not counts:
            continue
        mix = ", ".join(f"{num(n)} {MARKET_NAMES.get(m, m)}" for m, n in sorted(counts.items(), key=lambda mn: -mn[1]))
        parts.append(f"{html.escape(name.lower())}: {mix}")
    return "; ".join(parts)


def scorer_name(data: dict[str, Any]) -> str:
    """
    Which scorer a summary is for, in words.

    :param data: A summary.
    :return: The private model unfret.net runs, the open model, an earlier one of either, or just "a scorer".
    """
    version = data.get("scorer_version")
    service = service_entry()
    if service is not None and version == service["version"]:
        return "the private model unfret.net runs (retrained, not published)"
    if version == summary().get("scorer_version"):
        return "the open model, the scorer in the source code that a self-hosted Unfret runs"
    kind = next((e["kind"] for e in history() if e["version"] == version), None)
    if kind == "private":
        return "an earlier private model"
    if kind == "public":
        return "an earlier open model"
    return "a scorer"


def version_note(data: dict[str, Any]) -> str:
    """
    Whether the running scorer is the one these numbers were measured with.

    :param data: The summary.
    :return: A sentence.
    """
    running = running_version()
    if running == data.get("scorer_version"):
        return "It is the scorer this server runs."
    return f"This server runs scorer <code>{html.escape(str(running))}</code>; its numbers aren't published here yet."


def _columns() -> tuple[tuple[str, str], ...]:
    """
    The scorers' numbers, as (heading, :func:`metrics` field), in the side-by-side table's order.

    :return: The rows.
    """
    careful, concerns, serious = (band_label(b) for b in ("careful", "concerns", "junk_or_scam"))
    return (
        ("Unreliable pages warned about", "caught"),
        ("Warnings right", "warning_precision"),
        (f"{serious} right", "serious_right"),
        (f"Legitimate shops: {careful} or worse", "legit_shops_warned"),
        (f"Legitimate shops: {concerns}", "legit_shops_worth_a_look"),
        (f"Legitimate shops: {serious}", "legit_shops_serious"),
        ("ROC AUC", "roc_auc"),
    )


def _counts(data: dict[str, Any], field: str) -> tuple[Any, Any]:
    """
    The pages a share in the scorers' table is counted from.

    :param data: A replay summary.
    :param field: A :func:`metrics` field.
    :return: (part, whole); ``(None, None)`` for a field that isn't a share of pages.
    """
    bands = lookup(data, "groups.products.bands") or {}
    warned = [bands.get(b) or {} for b in ("careful", "junk_or_scam")]
    shops = lookup(data, "legit_shops.all") or {}
    serious = bands.get("junk_or_scam") or {}
    counts: dict[str, tuple[Any, Any]] = {
        "caught": (sum(b.get("unreliable", 0) for b in warned), lookup(data, "groups.products.unreliable")),
        "warning_precision": (sum(b.get("unreliable", 0) for b in warned), sum(b.get("pages", 0) for b in warned)),
        "serious_right": (serious.get("unreliable"), serious.get("pages")),
        "legit_shops_warned": (shops.get("warned"), shops.get("pages")),
        "legit_shops_worth_a_look": (shops.get("worth_a_look"), shops.get("pages")),
        "legit_shops_serious": (shops.get("serious"), shops.get("pages")),
    }
    return counts.get(field, (None, None))


def _metric(e: dict[str, Any], field: str) -> str:
    value = e["metrics"].get(field)
    if field == "roc_auc":
        return f"{value:.3f}" if isinstance(value, float) else NOT_MEASURED
    part, whole = _counts(e.get("summary") or {}, field)
    if not isinstance(value, int | float) or not isinstance(part, int) or not isinstance(whole, int) or not whole:
        return pct(value)
    return f'{pct(value)} <span class="muted">({num(part)} of {num(whole)})</span>'


def _released(e: dict[str, Any]) -> str:
    return f"Released {long_date(e['released'])}" if e.get("released") else "Release date not recorded"


def measured_afterwards(e: dict[str, Any]) -> bool:
    """
    Whether a version's numbers were measured after its release (an older open model, replayed later).

    :param e: A history entry.
    :return: Whether it was measured on a later day than it was released.
    """
    return bool(e.get("released") and e.get("measured") and str(e["measured"]) > str(e["released"]))


def _measured(e: dict[str, Any]) -> str:
    after = " afterwards" if measured_afterwards(e) else ""
    return f"measured{after} on {num(e.get('corpus_pages'))} labelled pages"


def scorers_table(_: dict[str, Any] | None = None) -> str:
    """
    The newest private model (the one unfret.net runs) and the open model, side by side: a column each.

    Each column's heading names the model (:data:`KIND_LABELS`), its version, its release date and the number of
    labelled pages it was measured on; each row is one of the numbers (:func:`_columns`).

    :return: The table's HTML (the open model alone when no private model's numbers are published).
    """
    running = running_version()
    shown = [(KIND_LABELS[e["kind"]], e) for e in (service_entry(), baseline_entry()) if e is not None]
    here = ' <span class="muted">(this server)</span>'
    heads = "".join(
        f'<th scope="col">{html.escape(name)}{here if e["version"] == running else ""}'
        f"<code>{html.escape(str(e['version']))}</code>"
        f'<span class="acc-scorer-meta">{_released(e)}, {_measured(e)}</span></th>'
        for name, e in shown
    )
    rows = "".join(
        f'<tr><th scope="row">{html.escape(heading)}</th>'
        + "".join(f"<td>{_metric(e, field)}</td>" for _, e in shown)
        + "</tr>"
        for heading, field in _columns()
    )
    return (
        '<div class="acc-table-wrap"><table class="acc-table acc-scorers">'
        f'<thead><tr><th scope="col">Measure</th>{heads}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def history_block(_: dict[str, Any] | None = None) -> str:
    """
    Every earlier version of either model, newest first, each folded under its kind, version and release date.

    A plain ``<details>`` per version: closed until opened, by keyboard too, with no script. A version measured
    after its release (an older open model, replayed later) says so, in its box and in the intro.

    :return: The HTML, or a sentence saying no earlier version was measured this way yet (the changelog's older
        versions were replaced before this page measured each version, so only their changes are listed).
    """
    earlier = earlier_entries()
    if not earlier:
        return (
            '<p class="acc-history-empty">None yet. The older versions in the <a href="#changelog">changelog</a> were '
            "replaced before we started measuring each version on this test set, so only their changes are listed "
            "there, without these numbers. From now on, each version that is replaced will appear here with its "
            "numbers and release date.</p>"
        )
    later = (
        " The older open models were measured afterwards, on a later day's test set: their numbers show that "
        "scorer inside Unfret as it was on that day, not exactly what it did when it was released."
        if any(measured_afterwards(e) for e in earlier)
        else ""
    )
    intro = (
        f"<p>The earlier versions of both models, newest first: each says whether it was the "
        f"<strong>{html.escape(KIND_LABELS['public'])}</strong> or the "
        f"<strong>{html.escape(KIND_LABELS['private'])}</strong>, with its release date and the same numbers as the "
        "table above. Open one to see them.</p>"
        f'<p class="small">Each says when it was measured and on how many labelled pages.{later} We keep adding '
        "pages, so numbers measured on different numbers of pages aren't strictly comparable.</p>"
    )
    boxes = []
    for e in earlier:
        rows = "".join(
            f'<tr><th scope="row">{html.escape(heading)}</th><td>{_metric(e, field)}</td></tr>'
            for heading, field in _columns()
        )
        version = html.escape(str(e["version"]))
        when = (
            f"Measured afterwards, on {long_date(e.get('measured'))}: this scorer inside Unfret as it was that day, "
            f"on the {num(e.get('corpus_pages'))} labelled pages the test set had then"
            if measured_afterwards(e)
            else f"Measured on {long_date(e.get('measured'))}, on {num(e.get('corpus_pages'))} labelled pages"
        )
        boxes.append(
            f'<details class="acc-history-item" id="scorer-{version}"><summary><span>'
            f"{html.escape(KIND_LABELS.get(e['kind'], 'Scorer'))} <code>{version}</code>"
            f'<span class="acc-scorer-meta">{_released(e)}, {_measured(e)}</span></span></summary>'
            f'<p class="small">{when}; {num(e["metrics"].get("products_scored"))} product pages got a verdict.</p>'
            '<div class="acc-table-wrap"><table class="acc-table acc-history-table">'
            f"<tbody>{rows}</tbody></table></div></details>"
        )
    return f'{intro}<div class="acc-history">{"".join(boxes)}</div>'


BLOCKS: dict[str, Callable[[dict[str, Any]], str]] = {
    "bands:products": lambda d: bands_table(d, "products"),
    "bands:ads": lambda d: bands_table(d, "ads"),
    "bands:trades": lambda d: bands_table(d, "trades"),
    "coverage:products": lambda d: coverage_note(d, "products"),
    "coverage:ads": lambda d: coverage_note(d, "ads"),
    "coverage:trades": lambda d: coverage_note(d, "trades"),
    "synthetic:trades": lambda d: synthetic_note(d, "trades"),
    "products_scope": products_scope,
    "kinds": kinds_table,
    "origins": origins_list,
    "legit_shops": legit_shops_table,
    "markets": markets_sentence,
    "version_note": version_note,
    "scorer_name": lambda d: html.escape(scorer_name(d)),
    "scorers": scorers_table,
    "history": history_block,
}


def render(page: str, data: dict[str, Any] | None = None) -> str:
    """
    Fill the page's placeholders from the measured numbers.

    :param page: The page's HTML with placeholders.
    :param data: The summary; defaults to :func:`page_summary`.
    :return: The HTML.
    """
    values = page_summary() if data is None else data

    def fill(match: re.Match[str]) -> str:
        kind, path = match.groups()
        if kind == "block":
            return BLOCKS[path](values)
        value = lookup(values, path)
        if kind == "num":
            return num(value)
        if kind == "pct":
            return pct(value)
        if kind == "date":
            return long_date(value)
        return html.escape(str(value)) if value is not None else NOT_MEASURED

    return _PLACEHOLDER_RE.sub(fill, page)
