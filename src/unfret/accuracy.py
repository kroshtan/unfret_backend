"""
The ``/accuracy`` page: how often Unfret's verdicts are right, from the numbers ``make accuracy`` measured.

Two summaries of a corpus replay, aggregates only (``evals/accuracy.py`` writes them; never a URL or a corpus
record): ``static/accuracy.json`` for the open baseline scorer in this repository (what a self-hosted server runs),
and ``static/accuracy-service.json`` for the retrained scorer unfret.net runs (``make accuracy
ACCURACY_ARGS="--publish VERSION"``). The page shows the one this server runs (:func:`page_summary`), and both side by
side. ``static/accuracy.html`` is the page's prose with placeholders that this module fills from it:

- ``{{num:path}}``: a count (``1,582``); ``{{pct:path}}``: a share as a percentage (``82%``, ``6.9%``);
  ``{{text:path}}``: a string; ``{{date:path}}``: an ISO date as "November 17, 2026". ``path`` is a dotted path
  into the JSON (``groups.products.caught``).
- ``{{block:name}}``: a generated piece (a verdict table, a coverage note), see :data:`BLOCKS`.

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
SERVICE_PATH = ACCURACY_PATH.with_name("accuracy-service.json")

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


@cache
def summary() -> dict[str, Any]:
    """
    The open baseline's measured numbers.

    :return: The parsed ``accuracy.json``.
    """
    data: dict[str, Any] = json.loads(ACCURACY_PATH.read_text(encoding="utf-8"))
    return data


@cache
def service_summary() -> dict[str, Any] | None:
    """
    The measured numbers of the retrained scorer unfret.net runs.

    :return: The parsed ``accuracy-service.json``, or ``None`` when none is published.
    """
    if not SERVICE_PATH.is_file():
        return None
    data: dict[str, Any] = json.loads(SERVICE_PATH.read_text(encoding="utf-8"))
    return data


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

    :return: The retrained scorer's summary on a server that runs it (unfret.net), else the open baseline's (a
        self-hosted server, or one running a scorer whose numbers aren't published: the page then says so).
    """
    service = service_summary()
    if service is not None and service.get("scorer_version") == running_version():
        return service
    return summary()


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
            f"<strong>{num(c['missing_recordings'])} of the {num(c['labelled'])} labelled pages couldn't be "
            f"measured</strong> ({origins}): we replay recorded answers from the language model, and those pages' "
            "answers aren't in the recording yet. They are left out rather than guessed."
        )
    return f'<p class="small">{" ".join(parts)}</p>'


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
    :return: The retrained scorer unfret.net runs, the open baseline, or just "a scorer".
    """
    version = data.get("scorer_version")
    service = service_summary()
    if service is not None and version == service.get("scorer_version"):
        return "the retrained scorer unfret.net runs"
    if version == summary().get("scorer_version"):
        return "the open baseline, the scorer in the source code that a self-hosted Unfret runs"
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


def scorers_table(_: dict[str, Any] | None = None) -> str:
    """
    The retrained scorer unfret.net runs and the open baseline, side by side, on the same replay of product pages.

    :return: The table's HTML (the baseline alone when no retrained scorer's numbers are published).
    """
    running = running_version()
    rows = []
    for name, data in (
        ("Retrained, runs on unfret.net", service_summary()),
        ("Open baseline, self-hosted", summary()),
    ):
        if data is None:
            continue
        products = lookup(data, "groups.products") or {}
        version = str(data.get("scorer_version"))
        here = ' <span class="muted">(this server)</span>' if version == running else ""
        rows.append(
            f'<tr><th scope="row">{html.escape(name)}{here}<br><code>{html.escape(version)}</code></th>'
            f"<td>{pct(products.get('caught'))}</td><td>{pct(products.get('warning_precision'))}</td>"
            f"<td>{pct(lookup(data, 'groups.products.bands.junk_or_scam.unreliable_share'))}</td>"
            f"<td>{pct(lookup(data, 'legit_shops.all.warned_share'))}</td>"
            f"<td>{pct(lookup(data, 'legit_shops.all.worth_a_look_share'))}</td>"
            f"<td>{pct(lookup(data, 'legit_shops.all.serious_share'))}</td>"
            f"<td>{products.get('roc_auc') if isinstance(products.get('roc_auc'), float) else NOT_MEASURED}</td></tr>"
        )
    careful, concerns, serious = (html.escape(band_label(b)) for b in ("careful", "concerns", "junk_or_scam"))
    return (
        '<div class="acc-table-wrap"><table class="acc-table">'
        '<thead><tr><th scope="col">Scorer</th><th scope="col">Unreliable pages warned about</th>'
        f'<th scope="col">Warnings right</th><th scope="col">{serious} right</th>'
        f'<th scope="col">Legitimate shops: {careful} or worse</th>'
        f'<th scope="col">Legitimate shops: {concerns}</th><th scope="col">Legitimate shops: {serious}</th>'
        f'<th scope="col">ROC AUC</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


BLOCKS: dict[str, Callable[[dict[str, Any]], str]] = {
    "bands:products": lambda d: bands_table(d, "products"),
    "bands:ads": lambda d: bands_table(d, "ads"),
    "bands:trades": lambda d: bands_table(d, "trades"),
    "coverage:products": lambda d: coverage_note(d, "products"),
    "coverage:ads": lambda d: coverage_note(d, "ads"),
    "coverage:trades": lambda d: coverage_note(d, "trades"),
    "kinds": kinds_table,
    "origins": origins_list,
    "legit_shops": legit_shops_table,
    "markets": markets_sentence,
    "version_note": version_note,
    "scorer_name": lambda d: html.escape(scorer_name(d)),
    "scorers": scorers_table,
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
