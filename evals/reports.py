"""
Work through the queue of reported results, labelling straight into the corpus: confident proposals in bulk.

After a preview, confident second opinions can be confirmed in one go; the rest are decided one by one with single
keypresses.

Every queued report carries a second opinion when the service could get one (``unfret.reports.second_opinion``): a
proposed label, a confidence and a one-line reason. A proposal is only a suggestion; it becomes a label when you
confirm it here, and only then is it training data. Each item also shows what the reports on that page said
(levels, findings called wrong or missing; never who sent them).

1. **Summary**: open items, the queue's cap, and how the second opinions side (with the reporter, with the score,
   neither, unsure, none yet).
2. **Bulk**: the items whose proposal is at least ``--min-confidence`` (0.9) sure are listed (shop, title, score,
   proposal, what it would pay). ``y`` confirms them all as your labels, ``n`` goes through them one by one.
3. **One by one**: the rest, worth most first (``--order confidence``: the surest proposals first; ``--agreement``
   picks one kind). Keys: ``Enter`` or ``a`` accept the proposal, ``0``-``4`` the page's level (higher is worse; it
   sets the label too), or ``l`` legit, ``j`` junk, ``s`` scam; ``r`` reject (not a product page, unusable, can't
   tell), ``k`` skip, ``q`` quit.

A label is written to the corpus first (``pages/report-<hash>.html`` with the scrubbed text and the product JSON-LD,
``pages/report-<hash>.facts.json`` with the client's facts, and a ``label_source="human"`` record in
``labels.jsonl`` with the second opinion in its ``llm_*`` fields), then sent to the service, which keeps the page as a
training sample, settles the page's reports on the level (contributors' reputations move) and deletes the item.
Quitting halfway loses nothing; an item whose label didn't reach the service shows up again and overwrites the same
corpus page.

Usage: ``make reports`` (or ``uv run python -m evals.reports [--min-confidence 0.9] [--no-bulk] [--order value]
[--agreement reporter]``). Environment: ``UNFRET_URL`` (default https://unfret.net) and ``UNFRET_ADMIN_TOKEN``, or
``UNFRET_ADMIN_TOKEN_FILE`` (default ``~/kroshtan_secrets/unfret-prod-admin-token``).
"""

import argparse
import html
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import httpx

from evals.corpus import Corpus, Label, LabelRecord, dump_json, legacy_label, now_iso, page_id
from evals.label import read_key

KEYS: dict[str, Label | None] = {"l": "legit", "j": "junk", "s": "scam", "r": None}
ACCEPT = {"a", "\r", "\n"}
LEVEL_KEYS: dict[str, int] = {str(n): n for n in range(5)}  # the page's level, higher is worse
LABELS: frozenset[str] = frozenset({"legit", "junk", "scam"})
_EXCERPT_CHARS = 1200
_PLATFORMS = {"amazon": "amazon", "bol.com": "bol.com"}


def _token() -> str:
    if token := os.getenv("UNFRET_ADMIN_TOKEN"):
        return token
    path = Path(os.getenv("UNFRET_ADMIN_TOKEN_FILE", "~/kroshtan_secrets/unfret-prod-admin-token")).expanduser()
    return path.read_text().strip()


def proposal(item: dict[str, Any]) -> Label | None:
    """
    The item's proposed label, if its second opinion has one (not unsure).

    :param item: The item from ``GET /v1/admin/reports``.
    :return: The label, or ``None``.
    """
    label = (item.get("second_opinion") or {}).get("label")
    return label if label in LABELS else None


def opinion_line(item: dict[str, Any]) -> str:
    """
    The second opinion in one line.

    :param item: The item.
    :return: The text.
    """
    opinion = item.get("second_opinion") or {}
    if opinion.get("state") != "done":
        return f"second opinion: none ({opinion.get('state', 'waiting')})"
    return (
        f"second opinion: {opinion['label']} ({opinion['confidence']:.2f}) · sides with {opinion['agreement']}"
        f" · {opinion['reason']}"
    )


def describe(item: dict[str, Any], n: int, total: int, in_corpus: str | None = None) -> str:
    """
    Render one queue item as a compact screen of text.

    :param item: The item from ``GET /v1/admin/reports``.
    :param n: Its position.
    :param total: The number of items fetched.
    :param in_corpus: The page's label in the corpus already, if it has one.
    :return: The text to print.
    """
    payload, facts = item["payload"], item["payload"]["facts"]
    excerpt = textwrap.shorten(" ".join(payload["text"].split()), _EXCERPT_CHARS, placeholder=" …")
    get = facts.get
    lines = [
        f"── {n}/{total} " + "─" * 60,
        (
            f"{item['domain']}  ·  {get('platform', 'generic')}  ·  reported as {item['reason']}"
            + (f" (level {item['level']})" if item.get("level") is not None else "")
            + f"  ·  {payload.get('tier', 'deep')} check  ·  +{item['corroborations']} confirmations"
        ),
        _reports_line(item),
        f"url: {payload['url'] or '(none)'}",
        f"model score: {item['score']}  ·  {payload['explanation'][:200]}",
        opinion_line(item),
        f"in the corpus already as {in_corpus}" if in_corpus else "new to the corpus",
        f"title: {get('title', '')[:150]}  ·  brand: {get('brand', '')}  ·  seller: {get('seller', '')}",
        (
            f"price: {get('price')} {get('currency', '')} (was {get('list_price')})  ·  "
            f"rating {get('rating')} ({get('rating_count')})"
        ),
        "",
        textwrap.fill(excerpt, 110),
        "",
        ("[Enter/a]ccept proposal  " if proposal(item) else "")
        + "level [0]-[4] (higher is worse)  or [l]egit  [j]unk  [s]cam  ·  [r]eject  [k] skip  [q]uit",
    ]
    return "\n".join(lines)


def _reports_line(item: dict[str, Any]) -> str:
    said = item.get("reports") or {}
    if not said.get("count"):
        return "reports: (none open)"
    parts = [f"{said['count']} report(s), {said.get('anonymous', 0)} anonymous"]
    if said.get("levels"):
        parts.append("levels " + ", ".join(f"{k}×{v}" for k, v in sorted(said["levels"].items())))
    if said.get("wrong"):
        parts.append("wrong: " + ", ".join(f"{k}×{v}" for k, v in said["wrong"].items()))
    if said.get("missing"):
        parts.append("missing: " + ", ".join(f"{k}×{v}" for k, v in said["missing"].items()))
    line = "reports: " + "  ·  ".join(parts)
    for comment in said.get("comments", [])[:3]:
        line += "\n  said: " + comment[:200]
    return line


def corpus_entry(
    item: dict[str, Any], label: Label, *, bulk: bool = False, level: int | None = None
) -> tuple[LabelRecord, str]:
    """
    A labelled report as a corpus record and page, in the same layout as every other page.

    :param item: The queue item.
    :param label: The human label.
    :param bulk: Whether it is a second opinion confirmed in bulk.
    :param level: The human level (0-4), when given.
    :return: The record and the page's HTML.
    """
    payload, facts = item["payload"], item["payload"]["facts"]
    record_id = page_id("report", item["page_key"])
    ld = "".join(
        f'<script type="application/ld+json">{json.dumps(node, ensure_ascii=False)}</script>'
        for node in facts.get("json_ld", [])
    )
    title = html.escape(facts.get("title", ""))
    text = html.escape(payload["text"])
    page = f"<html><head><title>{title}</title>{ld}</head><body><main><pre>{text}</pre></main></body></html>"
    opinion = item.get("second_opinion") or {}
    notes: dict[str, Any] = {"model_score": item["score"], "reported_as": item["reason"]}
    # With the tier whose score was reported (reports from before the tier was kept were all deep checks); the page
    # trains both the deep and the quick scorer either way.
    if "tier" in payload:
        notes["tier"] = payload["tier"]
    if bulk:
        notes["confirmed_in_bulk"] = True
    record = LabelRecord(
        id=record_id,
        label=label,
        label_source="human",
        confidence=1.0,
        platform=_PLATFORMS.get(facts.get("platform", ""), ""),
        source="report",
        origin_dataset="unfret-reports",
        origin_url=payload["url"],
        origin_id=item["page_key"],
        collected_at=now_iso(),
        notes=json.dumps(notes),
        llm_label=proposal(item),
        llm_confidence=opinion.get("confidence") or 0.0,
        llm_reason=opinion.get("reason") or "",
        level=level,
        level_source="human" if level is not None else None,
    )
    return record, page


def to_corpus(corpus: Corpus, item: dict[str, Any], label: Label, level: int | None = None) -> str:
    """
    Store a labelled report in the corpus.

    :param corpus: The corpus.
    :param item: The queue item.
    :param label: The human label.
    :param level: The human level, when given.
    :return: The corpus id.
    """
    record, page = corpus_entry(item, label, level=level)
    corpus.add(record, page)
    dump_json(corpus.pages_dir / f"{record.id}.facts.json", item["payload"]["facts"])
    return record.id


def confident(items: list[dict[str, Any]], min_confidence: float) -> list[dict[str, Any]]:
    """
    The items whose second opinion proposes a label at least this sure: the bulk candidates.

    :param items: The queue.
    :param min_confidence: The threshold.
    :return: Those items.
    """
    return [i for i in items if proposal(i) and (i["second_opinion"]["confidence"] or 0) >= min_confidence]


def preview(items: list[dict[str, Any]]) -> str:
    """
    The bulk candidates as a table to read before confirming.

    :param items: The candidates.
    :return: The text.
    """
    lines = []
    for n, item in enumerate(items, start=1):
        opinion, title = item["second_opinion"], item["payload"]["facts"].get("title", "")
        lines.append(
            f"{n:>3}. {item['domain'][:24]:<24} {title[:40]:<40} score {item['score']:>3} → {opinion['label']:<5} "
            f"{opinion['confidence']:.2f} {opinion['agreement']}"
        )
        lines.append(f"     {textwrap.shorten(opinion['reason'], 110, placeholder=' …')}")
    return "\n".join(lines)


def summary(queue: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """
    One paragraph about the queue: open items and how the second opinions side.

    :param queue: The ``GET /v1/admin/reports`` response.
    :param items: Its items.
    :return: The text.
    """
    sides: dict[str, int] = {}
    for item in items:
        side = (item.get("second_opinion") or {}).get("agreement", "none")
        sides[side] = sides.get(side, 0) + 1
    states = ", ".join(f"{k} {v}" for k, v in sorted(queue.get("second_opinions", {}).items()))
    return (
        f"{queue['open']} open (cap {queue.get('cap', '?')}); second opinions: {states or 'none'}; "
        f"sides with: " + ", ".join(f"{k} {v}" for k, v in sorted(sides.items()))
    )


def confirm_in_bulk(api: httpx.Client, corpus: Corpus, items: list[dict[str, Any]], min_confidence: float) -> int:
    """
    Store the previewed proposals in the corpus as your labels, then confirm them on the service.

    :param api: The admin client.
    :param corpus: The corpus.
    :param items: The previewed candidates.
    :param min_confidence: The preview's threshold (the service checks each item still carries it).
    :return: How many the service labelled.
    """
    entries = [corpus_entry(item, proposal(item) or "legit", bulk=True) for item in items]
    corpus.add_many(entries)
    for item, (record, _) in zip(items, entries, strict=True):
        dump_json(corpus.pages_dir / f"{record.id}.facts.json", item["payload"]["facts"])
    response = api.post(
        "/v1/admin/reports/confirm",
        json={"min_confidence": min_confidence, "items": [{"id": i["id"], "label": proposal(i)} for i in items]},
    )
    response.raise_for_status()
    outcome = response.json()
    print(  # noqa: T201 - interactive tool
        f"→ {outcome['labelled']} confirmed; {outcome['left']} left (handled meanwhile, or not carrying that proposal)"
    )
    return int(outcome["labelled"])


def _send(api: httpx.Client, item: dict[str, Any], label: Label | None, level: int | None = None) -> bool:
    body: dict[str, Any] = {"level": level} if level is not None else {"label": label or "reject"}
    response = api.post(f"/v1/admin/reports/{item['id']}/label", json=body)
    if response.status_code == 404:
        print("→ already handled or expired")  # noqa: T201 - interactive tool
        return False
    response.raise_for_status()
    outcome = response.json()
    print(f"→ {label or 'rejected'} (off by {outcome['gap']})")  # noqa: T201 - interactive tool
    return True


def one_by_one(api: httpx.Client, corpus: Corpus, items: list[dict[str, Any]]) -> int:
    """
    Decide items one at a time with single keypresses.

    :param api: The admin client.
    :param corpus: The corpus.
    :param items: The items.
    :return: How many were labelled or rejected.
    """
    known = corpus.labels()
    done = 0
    for n, item in enumerate(items, start=1):
        record = known.get(page_id("report", item["page_key"]))
        print("\n" + describe(item, n, len(items), record.label if record else None))  # noqa: T201 - interactive tool
        key = read_key().lower()
        if key == "q":
            break
        level: int | None = None
        if key in ACCEPT and proposal(item):
            label: Label | None = proposal(item)
        elif key in LEVEL_KEYS:
            level = LEVEL_KEYS[key]
            label = legacy_label(level)
        elif key in KEYS:
            label = KEYS[key]
        else:
            continue
        if label is not None:
            to_corpus(corpus, item, label, level)
        done += _send(api, item, label, level)
    return done


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--min-confidence", type=float, default=0.9, help="bulk-confirm proposals at least this sure")
    parser.add_argument("--no-bulk", action="store_true", help="go through every item one by one")
    parser.add_argument("--order", choices=["value", "confidence"], default="value")
    parser.add_argument("--agreement", choices=["reporter", "score", "neither", "unsure", "none"], default=None)
    args = parser.parse_args()
    base = os.getenv("UNFRET_URL", "https://unfret.net").rstrip("/")
    corpus = Corpus()
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {_token()}"}, timeout=60) as api:
        params: dict[str, Any] = {"limit": 1000, "order": args.order}
        if args.agreement:
            params["agreement"] = args.agreement
        queue = api.get("/v1/admin/reports", params=params)
        queue.raise_for_status()
        items = queue.json()["items"]
        print(summary(queue.json(), items))  # noqa: T201 - interactive tool
        if not items:
            print("Nothing to review.")  # noqa: T201 - interactive tool
            return
        done = 0
        bulk = [] if args.no_bulk else confident(items, args.min_confidence)
        if bulk:
            print(f"\n{len(bulk)} proposal(s) at least {args.min_confidence:.2f} sure:\n{preview(bulk)}")  # noqa: T201
            print("\nConfirm all of them as your labels? [y]es, [n]o: one by one, [q]uit")  # noqa: T201
            key = read_key().lower()
            if key == "q":
                return
            if key == "y":
                done += confirm_in_bulk(api, corpus, bulk, args.min_confidence)
                confirmed = {i["id"] for i in bulk}
                items = [i for i in items if i["id"] not in confirmed]
        # Pages the corpus has already go last: a new page teaches more.
        known = corpus.labels()
        items.sort(key=lambda i: page_id("report", i["page_key"]) in known)
        done += one_by_one(api, corpus, items)
    print(f"\n{done} decided this session.")  # noqa: T201 - interactive tool


if __name__ == "__main__":
    main()
