"""
Work through the queue of reported results with single keypresses, labelling straight into the corpus.

Keys: ``l`` legit, ``j`` junk, ``s`` scam, ``r`` reject (not a product page, unusable, can't tell), ``k`` skip,
``q`` quit. The queue comes from the running service (``GET /v1/admin/reports``): the most corroborated pages first,
then those the model was least sure about. Duplicates never reach it (they are closed when reported), so every item
is a different page.

A label is written to the corpus first (``pages/report-<hash>.html`` with the scrubbed text and the product JSON-LD,
``pages/report-<hash>.facts.json`` with the client's facts, and a ``label_source="human"`` record in
``labels.jsonl``), then sent to the service, which pays the reporter if the score was far off, keeps the page as a
training sample and deletes the report (its only link to the account). Quitting halfway loses nothing; an item whose
label didn't reach the service shows up again and overwrites the same corpus page.

When the model was right (the score inside the label's zone), the page can be promoted to a "Spotted recently" draft
(``p``): the server checks the public page again itself, as the operator, and keeps only that new check; nothing from
the report (its result, text or reporter) goes into the case. Review and publish it with ``make showcase``.

Usage: ``make reports`` (or ``uv run python -m evals.reports``). Environment: ``UNFRET_URL`` (default
https://unfret.net) and ``UNFRET_ADMIN_TOKEN``, or ``UNFRET_ADMIN_TOKEN_FILE`` (default
``~/kroshtan_secrets/unfret-prod-admin-token``).
"""

import html
import json
import os
import textwrap
from pathlib import Path
from typing import Any

import httpx

from evals.corpus import Corpus, Label, LabelRecord, dump_json, now_iso, page_id
from evals.label import read_key
from unfret.showcase.cli import promote

KEYS: dict[str, Label | None] = {"l": "legit", "j": "junk", "s": "scam", "r": None}
_EXCERPT_CHARS = 1200
_PLATFORMS = {"amazon": "amazon", "bol.com": "bol.com"}


def _token() -> str:
    if token := os.getenv("UNFRET_ADMIN_TOKEN"):
        return token
    path = Path(os.getenv("UNFRET_ADMIN_TOKEN_FILE", "~/kroshtan_secrets/unfret-prod-admin-token")).expanduser()
    return path.read_text().strip()


def describe(item: dict[str, Any], n: int, total: int) -> str:
    """
    Render one queue item as a compact screen of text.

    :param item: The item from ``GET /v1/admin/reports``.
    :param n: Its position.
    :param total: The number of items fetched.
    :return: The text to print.
    """
    payload, facts = item["payload"], item["payload"]["facts"]
    excerpt = textwrap.shorten(" ".join(payload["text"].split()), _EXCERPT_CHARS, placeholder=" …")
    get = facts.get
    lines = [
        f"── {n}/{total} " + "─" * 60,
        (
            f"{item['domain']}  ·  {get('platform', 'generic')}  ·  reported as {item['reason']}  ·  "
            f"+{item['corroborations']} confirmations"
        ),
        f"url: {payload['url'] or '(none)'}",
        f"model score: {item['score']}  ·  {payload['explanation'][:200]}",
        f"title: {get('title', '')[:150]}  ·  brand: {get('brand', '')}  ·  seller: {get('seller', '')}",
        (
            f"price: {get('price')} {get('currency', '')} (was {get('list_price')})  ·  "
            f"rating {get('rating')} ({get('rating_count')})"
        ),
        "",
        textwrap.fill(excerpt, 110),
        "",
        "[l]egit  [j]unk  [s]cam  [r]eject  [k] skip  [q]uit",
    ]
    return "\n".join(lines)


def to_corpus(corpus: Corpus, item: dict[str, Any], label: Label) -> str:
    """
    Store a labelled report in the corpus, in the same layout as every other page.

    :param corpus: The corpus.
    :param item: The queue item.
    :param label: The human label.
    :return: The corpus id.
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
        notes=json.dumps({"model_score": item["score"], "reported_as": item["reason"]}),
    )
    corpus.add(record, page)
    dump_json(corpus.pages_dir / f"{record_id}.facts.json", facts)
    return record_id


def offer_promotion(api: httpx.Client, item: dict[str, Any], gap: int) -> None:
    """
    When the model was right about a page with an address, offer to make a showcase draft of it.

    :param api: The admin client.
    :param item: The labelled queue item.
    :param gap: How far the model's score was outside the label's zone.
    """
    url = item["payload"].get("url") or ""
    if gap != 0 or not url.startswith(("http://", "https://")):
        return
    print("   [p] promote to a showcase draft (re-checked by the server)  · any other key: no")  # noqa: T201
    if read_key().lower() != "p":
        return
    response = promote(api, url)
    if response.is_success:
        print(f"→ showcase draft #{response.json()['id']}; review it with make showcase")  # noqa: T201
    else:
        print(f"→ no showcase draft ({response.status_code}): {response.text[:200]}")  # noqa: T201


def main() -> None:
    """Command-line entry point."""
    base = os.getenv("UNFRET_URL", "https://unfret.net").rstrip("/")
    corpus = Corpus()
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {_token()}"}, timeout=30) as api:
        queue = api.get("/v1/admin/reports", params={"limit": 200})
        queue.raise_for_status()
        items = queue.json()["items"]
        if not items:
            print("The report queue is empty.")  # noqa: T201 - interactive tool
            return
        done = 0
        for n, item in enumerate(items, start=1):
            print("\n" + describe(item, n, len(items)))  # noqa: T201 - interactive tool
            key = read_key().lower()
            if key == "q":
                break
            if key not in KEYS:
                continue
            label = KEYS[key]
            if label is not None:
                to_corpus(corpus, item, label)
            response = api.post(f"/v1/admin/reports/{item['id']}/label", json={"label": label or "reject"})
            if response.status_code == 404:
                print("→ already handled or expired")  # noqa: T201 - interactive tool
                continue
            response.raise_for_status()
            outcome = response.json()
            reward = (
                f", reporter rewarded ${outcome['reward_usd_micros'] / 1e6:.2f}"
                if outcome["reward_usd_micros"]
                else ""
            )
            print(f"→ {label or 'rejected'} (off by {outcome['gap']}{reward})")  # noqa: T201 - interactive tool
            done += 1
            if label is not None:
                offer_promotion(api, item, outcome["gap"])
    print(f"\n{done} labelled; {len(items) - done} left in this batch.")  # noqa: T201 - interactive tool


if __name__ == "__main__":
    main()
