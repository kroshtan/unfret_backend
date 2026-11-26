"""
Label the deep-label queue with a strong LLM, after checking it against the pages Floris labelled himself.

The labelling queue (:mod:`evals.candidates`, ``deep_label_queue.jsonl``) is far longer than one person labels. This
labeller reads each page as Floris's labelling screen shows it, minus the scorer: the title, price, seller, brand,
where it is sold, every new finding with its evidence, the shop facts the other checks found (domain age, company
identity, payment, policies), and the cleaned page text (bounded, wrapped as untrusted, with the backend's
injection notice). No score, band or earlier label goes in, so nothing anchors it. It answers with a verdict per
finding (``correct`` / ``wrong``), a confidence and, from prompt v4 on, the page's OVERALL risk level (0 none, 1
manipulation, 2 overpaying or poor quality, 3 doesn't work, 4 won't arrive / unsafe / data theft; lower levels can
combine upward, protection moves it) with a short "because" list (:mod:`unfret.levels`; the older prompts answer
``legit`` / ``junk`` / ``scam``). The level is a proposal: it is stored as an LLM level label, Floris confirms or
corrects it in ``make deep-label`` (Enter accepts), and the labels train the level classifier. No rule turns the
findings into the level; the finding table (:data:`unfret.levels.FINDING_LEVELS`) only says what each finding is
evidence for. A few of Floris's labelled pages are the prompt's examples (from v4: his level labels, one per level,
the earliest he gave); a page being scored is never its own example (leave-one-out).

Three commands:

1. ``validate --model M [--model M2 …]``: label Floris's labelled pages (``label_decisions.jsonl``) and compare: page
   agreement overall, per class and on the blind pages, finding agreement against ``finding_labels.jsonl``, and the
   confusion. Per model and prompt version: ``reports/llm-label/validation-<model>-<prompt>.json``.
2. ``label --model M --pages 600``: reserve the human-only test set first (``deep_test_set.json``, a seeded random
   ``--test-size`` pages of the queue the LLM never labels: the evaluation set for the decision models), then label
   the next pages in queue order (``--relevel``: also the pages an older prompt labelled without a level). Labels go
   to ``llm_labels.jsonl`` (model, version, prompt version, the level and its "because", the finding verdicts, the
   label before) and into ``labels.jsonl`` with ``label_source`` and ``level_source`` ``llm:<model>:<prompt>`` (the
   legacy label follows from the level); a human label is never overwritten. Then the review queue for Floris
   (``llm_review_queue.jsonl``): every page the LLM is unsure of (confidence below ``--unsure``) plus a seeded random
   ``--audit`` share of the rest, which ``make deep-review`` serves (the audit pages blind: without the LLM's answer,
   so they measure it without anchoring). A level proposal also waits in ``make deep-label`` until a person
   confirms or corrects it.
3. ``review-queue``: rebuild that review queue only.

Every call is recorded in ``reports/llm-label/calls.jsonl`` with its tokens and cost (per model at the list prices
below; a model without a known price at the dearest), so a re-run replays instead of paying again, and the
labeller stops before the whole ledger (validation included) passes ``--budget-eur``.

Validation of a level prompt compares the level with Floris's level labels where he gave one (exact, within one) and
with his older labels' range everywhere (:data:`evals.corpus.LEGACY_LEVELS`), plus "any concern" and the findings.

Usage: ``uv run python -m evals.llm_label validate --model glm-5.3 --reasoning medium``,
``... label --model glm-5.3 --reasoning medium --pages 600 [--relevel]``, ``... review-queue``.
"""

import argparse
import asyncio
import hashlib
import json
import random
import time
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from evals.candidates import DECISIONS, FINDING_LABELS, QUEUE, is_new, label_rows, notes_of, read_queue
from evals.corpus import (
    LEGACY_LEVELS,
    LLM_SOURCE,
    Corpus,
    LabelRecord,
    QueueItem,
    dump_json,
    is_llm,
    legacy_label,
    now_iso,
)
from unfret import levels
from unfret.config import settings
from unfret.facts import parse_page
from unfret.html_cleaner import clean_html
from unfret.llm import UNTRUSTED_NOTICE, throttled, untrusted

logger = structlog.get_logger("unfret.evals")

PROMPT_VERSION = "llm-label-v4"  # the default; every version stays runnable (PROMPTS)
REPORTS = Path("reports") / "llm-label"
CALLS = REPORTS / "calls.jsonl"
LLM_LABELS = "llm_labels.jsonl"
REVIEW_QUEUE = "llm_review_queue.jsonl"
TEST_SET = "deep_test_set.json"
TEST_SEED = "unfret-deep-test-2026"
AUDIT_SEED = "unfret-llm-audit-2026"
AUDIT = "audit"  # a review-queue reason's prefix: a random page, reviewed blind
UNSURE = "unsure"  # a review-queue reason's prefix: the LLM was not confident
PAGE_CHARS = 8_000
SHOP_FACTS = 12
# Skills whose findings are the pipeline's own reading of the page (an opinion, not a fact): left out, so they don't
# anchor the label any more than the score would.
OPINION_SKILLS = frozenset({"content"})
# Euros per million input and output tokens. Scaleway's list prices as the backend's settings give them; a model not
# listed is charged at the dearest, so the ledger over- rather than under-states the spend.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "mistral-small-3.2-24b-instruct-2506": (Decimal("0.15"), Decimal("0.35")),
    "glm-5.3": (Decimal("1.80"), Decimal("5.50")),
}
DEAREST = (Decimal("1.80"), Decimal("5.50"))

DEFINITIONS = """\
Page labels:
- legit: a real seller, honestly sold. Small slips are fine; an obviously wrong data point nobody believes (e.g. \
0.12 g for a phone holder) is still legit.
- junk: real goods, but deceptively or badly represented. That covers generic goods sold as special, padded counts, \
inflated specs, and a listing so vague you can't tell what you'll get, e.g. key specs missing where they matter. Big \
brands too. Test: would a buyer actually be misled, or unable to tell what they'll get?
- scam: likely not to deliver; a fake shop or seller.

Per finding:
- correct: the finding really is on the page (a real contradiction, even a harmless slip).
- wrong: the check misread something (the contradiction isn't really there)."""

SYSTEM = f"""\
You label e-commerce product pages for a consumer-protection corpus, the way its human labeller does.

{DEFINITIONS}

The findings come from an automated check that reads the page and reports where it contradicts itself. The check \
can misread: a unit (fl oz versus weight), a per-pack versus total count, a field it took for another. Judge each \
finding on its own: is the contradiction really there? A finding can be correct and still harmless: whether it \
makes the page junk is the page label's question, not the finding's. A finding may quote text you can't see below \
(the page text is cut short, or the quote is from the shop's other pages): then take the quote as given and judge \
whether it really contradicts what the page says.

The shop facts are what other checks looked up (domain age, company identity, payment methods, policies). They \
matter most for scam: a page from a long-established shop or a known marketplace seller is rarely a fake shop.

{UNTRUSTED_NOTICE}

Confidence: how likely the human labeller gives the same page label (0-1). Use below 0.7 when the page is a close \
call between two labels. Reason: one or two plain sentences on what decided the label."""

# v2 (2026-11-21): v1 called 21 of Floris's 32 junk pages legit, nearly all as "an obviously wrong data point". How
# he applies the junk test, read off his non-blind labels of the first batch (the blind ones stay the honest check).
APPLYING = """How the human labeller applies the junk test:
- A correct finding on a fact a buyer chooses or uses the product by (a count, size or dimension, capacity, weight \
where it matters, compatibility, material or composition, a feature such as wet use, battery or range, shipping \
terms, the shop's claims about itself) makes the page junk, even when the right value is stated elsewhere on the \
page and a careful reader could work it out: a listing that contradicts itself on such a fact is badly \
represented, because the buyer can't tell which value is true.
- Who sells it is no excuse: big brands, long-established retailers and official shops get junk for this too. The \
seller's standing matters for scam, not for junk.
- "An obviously wrong data point nobody believes" means a value that carries no information at all: a placeholder \
or filler (an item weight of 0.01 oz or 0.01 g) or a template field that plainly doesn't apply. That alone leaves \
the page legit, with the finding correct.
- When every finding is wrong (the check misread) and nothing else on the page misleads, the page is legit."""

# v3 (Floris, 2026-11-21): the junk test is materiality to the purchase decision, not whether a value is believable.
DEFINITIONS_V3 = """\
Page labels:
- legit: a real seller, honestly sold.
- junk: real goods, but deceptively or badly represented. That covers generic goods sold as special, padded counts, \
inflated specs, and a listing so vague you can't tell what you'll get, e.g. key specs missing where they matter. Big \
brands too.
- scam: likely not to deliver; a fake shop or seller.

The junk test is materiality: does a wrong, contradictory or missing value affect how a buyer evaluates this \
product, i.e. is it a fact a buyer of this kind of product chooses by (a dryer's size, a charger's wattage, a \
set's piece count, a part's compatibility)? Then it is a junk marker, whether it looks like a data-entry slip, a \
lazy omission or a claim, and whether or not anyone would believe it. A wrong value in a field that plays no part \
in evaluating this product is not a junk marker. Decide per product type: what does a buyer of this product compare \
on? Who sells it doesn't change this: big brands and long-established shops get junk too; the seller's standing \
matters for scam, not for junk.

Per finding:
- correct: the finding really is on the page (a real contradiction, even a harmless one).
- wrong: the check misread something (the contradiction isn't really there)."""

# v4 (Floris, 2026-11-25): the page's OVERALL risk level instead of legit / junk / scam (the consumer protection
# pivot). The materiality test of v3 carries over to levels 2 and 3.
LEVELS_V4 = """\
The page's overall risk level, higher is worse:
- 4 won't arrive or isn't safe: likely a fake shop or seller that won't deliver, a page made to take card or \
personal details, or an unsafe product (recalled, banned, dangerous).
- 3 doesn't work: the product likely won't do what the page says: false or impossible claims, specs that contradict \
each other or the photos on a fact a buyer chooses by.
- 2 overpaying or poor quality: real goods that likely cost much more than they are worth or are worse than \
presented: generic goods sold as special or branded, dropship markup, hidden costs, costly or impossible returns, \
padded counts, a listing so vague you can't tell what you'll get (key specs missing where they matter). The size \
matters: a very large markup (five times or more) can count as level 3.
- 1 manipulation: pressure and tricks: fake countdowns or scarcity, fake viewer or sales counts, misleading discounts \
or reference prices, fake or inflated reviews.
- 0 none: a real seller, honestly sold. Slips that don't matter to a buyer of this product are fine.

The level is the page's OVERALL level, not simply its most serious single finding:
- Lower levels combine: a product that doesn't work, costs too much and is pushed with tricks can count as level 4 \
together. Manipulation is also evidence for the levels above it.
- Protection moves the level, it is not a level: recourse (card payment with chargeback, a platform's buyer \
protection, a real return address) lowers it by one; payment you can't get back (bank transfer, crypto, gift cards, \
cash up front) raises it by one. Recourse doesn't lower level 4: a chargeback doesn't undo stolen card details or an \
unsafe product.

Materiality decides levels 2 and 3: does a wrong, contradictory or missing value affect how a buyer evaluates this \
product, i.e. is it a fact a buyer of this kind of product chooses by (a dryer's size, a charger's wattage, a set's \
piece count, a part's compatibility)? Then it counts, whether it looks like a data-entry slip, a lazy omission or a \
claim. A wrong value in a field that plays no part in evaluating this product doesn't. Who sells it doesn't change \
this: big brands and long-established shops get levels 1-3 too; the seller's standing matters for level 4.

Per finding:
- correct: the finding really is on the page (a real contradiction, even a harmless one).
- wrong: the check misread something (the contradiction isn't really there)."""

SYSTEM_V4 = f"""\
You label e-commerce product pages for a consumer-protection corpus, the way its human labeller does.

{LEVELS_V4}

The findings come from automated checks. The ones numbered under "Findings to judge" read the page and report \
where it contradicts itself; they can misread: a unit (fl oz versus weight), a per-pack versus total count, a field \
it took for another. Judge each on its own: is it really there? A finding can be correct and still harmless: how \
much it matters is the level's question, not the finding's. A finding may quote text you can't see below (the page \
text is cut short, or the quote is from the shop's other pages): then take the quote as given. "(evidence for level \
N)" says what a finding of that type would point at if it is right and material; it is not the answer.

The shop facts are what other checks looked up (domain age, company identity, payment methods, policies). They \
matter most for level 4 and for protection: a page from a long-established shop or a known marketplace seller is \
rarely a fake shop.

{UNTRUSTED_NOTICE}

Because: one to four short items, most serious first, each saying what on the page drove the level and the level \
it points at ("3: capacity given as 5000 mAh and 3000 mAh"), plus the combining or protection step when one \
applied ("raised: bank transfer only"). For level 0, one item saying why nothing concerns a buyer. Never write \
that a shop is a scam: say what was found.
Confidence: how likely the human labeller gives the same level (0-1). Use below 0.7 when the page is a close call \
between two levels."""

PROMPTS: dict[str, str] = {
    "llm-label-v1": SYSTEM,
    "llm-label-v2": SYSTEM.replace(f"\n\n{UNTRUSTED_NOTICE}", f"\n\n{APPLYING}\n\n{UNTRUSTED_NOTICE}"),
    "llm-label-v3": SYSTEM.replace(DEFINITIONS, DEFINITIONS_V3),
    "llm-label-v4": SYSTEM_V4,
}
# The prompt versions that answer with a level (:class:`LevelLabel`); the others answer legit / junk / scam.
LEVEL_PROMPTS: frozenset[str] = frozenset({"llm-label-v4"})
LEVEL_EXAMPLES_PER_LEVEL = 1

# The prompt's examples: some of Floris's labelled pages (none blind), per prompt version. Kept in the private corpus
# (no corpus ids in this repository): ``{version: {"per_class": n, "legit": [ids], "junk": [ids]}}``; n of each class
# go in a prompt, never the page being scored. A level prompt may list ``{"ids": [ids]}`` (pages with a human level);
# without it, it takes his earliest non-blind level label per level, and before he has given any, the newest
# legacy version's pages with their finding verdicts only.
EXAMPLES = REPORTS / "examples.json"


class FindingVerdict(BaseModel):
    """The verdict on one numbered finding."""

    n: int = Field(description="The finding's number, as listed.")
    why: str = Field(description="A few words: what the page really says.")
    verdict: Literal["correct", "wrong"]
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="How likely the human labeller gives the same verdict (0-1)."
    )


class PageLabel(BaseModel):
    """The labeller's answer for one page."""

    findings: list[FindingVerdict] = Field(description="One verdict per numbered finding; empty when none listed.")
    reason: str = Field(description="One or two sentences: what decided the page label.")
    label: Literal["legit", "junk", "scam"]
    confidence: float = Field(ge=0, le=1, description="How likely the human labeller gives the same label (0-1).")


class LevelLabel(BaseModel):
    """The labeller's answer for one page, from prompt v4 on: the overall risk level."""

    findings: list[FindingVerdict] = Field(description="One verdict per numbered finding; empty when none listed.")
    because: list[str] = Field(
        min_length=1,
        max_length=6,
        description="One to four short items, most serious first: what drove the level and the level each points "
        "at, plus the combining or protection step when one applied.",
    )
    level: int = Field(
        ge=0,
        le=4,
        description="The page's OVERALL risk level, higher is worse: 0 none, 1 manipulation, 2 overpaying or poor "
        "quality, 3 doesn't work, 4 won't arrive / unsafe / data theft.",
    )
    confidence: float = Field(ge=0, le=1, description="How likely the human labeller gives the same level (0-1).")


def level_of(answer: dict[str, Any]) -> int | None:
    """An answer's level (``None`` for an older prompt's answer)."""
    level = answer.get("level")
    return int(level) if level is not None else None


def label_of(answer: dict[str, Any]) -> str:
    """An answer's legacy label: given by an older prompt, else following from the level."""
    if label := answer.get("label"):
        return str(label)
    return legacy_label(int(answer["level"]))


def reason_of(answer: dict[str, Any]) -> str:
    """An answer's reason: an older prompt's sentence, else the "because" items."""
    return str(answer.get("reason") or "; ".join(answer.get("because", [])))


def answer_text(answer: dict[str, Any]) -> str:
    """An answer in a few words: ``level 3`` or the legacy label."""
    level = level_of(answer)
    return f"level {level}" if level is not None else label_of(answer)


def price(model: str) -> tuple[Decimal, Decimal]:
    """Euros per million input and output tokens for a model (the dearest when unknown)."""
    return PRICES.get(model, DEAREST)


def call_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    What one call cost.

    :param model: The model.
    :param input_tokens: Prompt tokens.
    :param output_tokens: Answer tokens (reasoning included).
    :return: Euros.
    """
    rate_in, rate_out = price(model)
    return (rate_in * input_tokens + rate_out * output_tokens) / 1_000_000


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """The rows of a JSON-lines file (none when it doesn't exist)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Append rows to a JSON-lines file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def spent(corpus: Corpus) -> Decimal:
    """What every recorded call of this labeller has cost so far, in euros."""
    return sum((Decimal(str(r["cost_eur"])) for r in read_jsonl(corpus.root / CALLS)), Decimal(0))


def human_decisions(corpus: Corpus) -> dict[str, dict[str, Any]]:
    """Floris's latest page decision per page (``label_decisions.jsonl``), rejections left out."""
    out = {r["id"]: r for r in read_jsonl(corpus.root / DECISIONS)}
    return {k: v for k, v in out.items() if v.get("label")}


def human_findings(corpus: Corpus) -> dict[tuple[str, str], str]:
    """Floris's finding verdicts by page and evidence (``finding_labels.jsonl``), the latest per finding."""
    return {(r["id"], r["evidence"]): r["verdict"] for r in read_jsonl(corpus.root / FINDING_LABELS)}


def points_at(finding_type: str) -> str:
    """What a finding type is evidence for, as the level prompt shows it (empty when the table doesn't know it)."""
    level = levels.level_of(finding_type)
    if level is not None:
        return f" (evidence for level {level})"
    entry = levels.FINDING_LEVELS.get(finding_type)
    return " (raises the level: payment you can't get back)" if entry and entry.modifier == "raised" else ""


def shop_facts(row: dict[str, Any], *, with_levels: bool = False) -> list[str]:
    """
    The facts the other checks found about a page's shop: their findings' evidence, opinions left out.

    :param row: The page's deep-check (or replay) row.
    :param with_levels: Name each finding's type and what it is evidence for (the level prompts).
    :return: Up to :data:`SHOP_FACTS` distinct lines.
    """
    facts: list[str] = []
    for finding in row.get("findings", []):
        if is_new(finding["skill"]) or finding["skill"] in OPINION_SKILLS:
            continue
        kind = f" {finding['type']}{points_at(finding['type'])}:" if with_levels and finding.get("type") else ""
        line = f"[{finding['skill']}]{kind} {finding['evidence'][:200]}"
        if line not in facts:
            facts.append(line)
    return facts[:SHOP_FACTS]


def page_brief(
    corpus: Corpus, record: LabelRecord, row: dict[str, Any], *, with_levels: bool = False
) -> tuple[str, str, list[dict[str, Any]]]:
    """
    The facts block and the bounded page text the labeller reads, and the findings it judges.

    :param corpus: The corpus.
    :param record: The page's record.
    :param row: Its deep-check (or replay) row.
    :param with_levels: Say what each finding is evidence for (the level prompts; the older prompts' text is kept
        byte for byte, so their recorded answers replay).
    :return: The facts (trusted framing, page-derived values), the page text, the new findings in order.
    """
    html = corpus.read_page(record.id)
    facts, _ = parse_page(html, record.origin_url)
    notes = notes_of(record)
    where = notes.get("marketplace") or record.platform or "own shop"
    seller = notes.get("marketplace_seller") or facts.seller
    findings = list(row.get("new_findings", []))
    lines = [
        f"Domain: {urlsplit(record.origin_url).hostname or 'unknown'}  ·  sold on: {where}",
        f"Title: {facts.title[:200]}",
        (
            f"Price: {facts.price} {facts.currency} (list price {facts.list_price})  ·  seller: {seller}  ·  "
            f"brand: {facts.brand}"
        ),
    ]
    if row.get("piece_count"):
        lines.append(f"Piece count the check read: {json.dumps(row['piece_count'], ensure_ascii=False)[:300]}")
    lines.append(f"Findings to judge ({len(findings)}):" if findings else "Findings to judge: none")
    lines += [
        f"  {i}. [{f['skill']}] {f['type']}{points_at(f['type']) if with_levels else ''}: {f['evidence'][:500]}"
        for i, f in enumerate(findings, start=1)
    ]
    shop = shop_facts(row, with_levels=with_levels)
    lines.append("Shop facts:" if shop else "Shop facts: none found")
    lines += [f"  - {line}" for line in shop]
    text = " ".join(clean_html(html).split())[:PAGE_CHARS]
    return "\n".join(lines), text, findings


def example_block(corpus: Corpus, page: str, rows: dict[str, dict[str, Any]], prompt: str = PROMPT_VERSION) -> str:
    """
    Floris's labelled examples for a prompt, never ``page`` itself.

    The version's ``per_class`` of each class from the corpus's :data:`EXAMPLES` file.

    :param corpus: The corpus.
    :param page: The page being labelled (left out of its own examples).
    :param rows: Every page's row.
    :param prompt: The prompt version.
    :return: The examples as text.
    """
    if prompt in LEVEL_PROMPTS:
        return level_example_block(corpus, page, rows, prompt)
    chosen = json.loads((corpus.root / EXAMPLES).read_text())[prompt]
    left_out = page if page in (*chosen["legit"], *chosen["junk"]) else ""
    key = (str(corpus.root), prompt, left_out)
    if key not in _EXAMPLE_TEXT:  # the same text for every page but the examples themselves: built once per run
        _EXAMPLE_TEXT[key] = _examples(corpus, chosen, left_out, rows)
    return _EXAMPLE_TEXT[key]


_EXAMPLE_TEXT: dict[tuple[str, str, str], str] = {}


def level_examples(corpus: Corpus, rows: dict[str, dict[str, Any]], prompt: str) -> list[str]:
    """
    The pages a level prompt shows as examples: the corpus's list for it, else Floris's earliest level label per level.

    Only pages he gave a level, not blind, with a deep-check row; :data:`LEVEL_EXAMPLES_PER_LEVEL` per level, the
    earliest decided (so the examples don't change as he labels more).

    :param corpus: The corpus.
    :param rows: Every page's row.
    :param prompt: The level prompt version.
    :return: The page ids (empty before he has given a level).
    """
    path = corpus.root / EXAMPLES
    configured = json.loads(path.read_text()).get(prompt) if path.exists() else None
    if configured and configured.get("ids"):
        return [str(p) for p in configured["ids"]]
    first: dict[str, dict[str, Any]] = {}
    for decision in read_jsonl(corpus.root / DECISIONS):
        if decision.get("level") is not None and decision["id"] not in first:
            first[decision["id"]] = decision
    latest = human_decisions(corpus)
    chosen: list[str] = []
    for level in range(5):
        pages = [
            p
            for p, d in first.items()
            if d["level"] == level
            and not d.get("blind")
            and latest.get(p, {}).get("level") == level  # still his answer
            and p in rows
        ]
        chosen += pages[:LEVEL_EXAMPLES_PER_LEVEL]
    return chosen


def level_example_block(corpus: Corpus, page: str, rows: dict[str, dict[str, Any]], prompt: str) -> str:
    """
    A level prompt's examples, never ``page`` itself.

    Floris's level labels when he has given some (:func:`level_examples`); before that, the newest legacy prompt's
    examples with their finding verdicts only (no level: his old labels only bound it).

    :param corpus: The corpus.
    :param page: The page being labelled.
    :param rows: Every page's row.
    :param prompt: The level prompt version.
    :return: The examples as text.
    """
    chosen = level_examples(corpus, rows, prompt)
    with_level = bool(chosen)
    if not chosen:
        path = corpus.root / EXAMPLES
        legacy = json.loads(path.read_text()) if path.exists() else {}
        older = [v for v in sorted(PROMPTS) if v not in LEVEL_PROMPTS and v in legacy]
        if older:
            entry = legacy[older[-1]]
            chosen = [*entry["legit"][: entry["per_class"]], *entry["junk"][: entry["per_class"]]]
    left_out = page if page in chosen else ""
    key = (str(corpus.root), prompt, left_out)
    if key not in _EXAMPLE_TEXT:
        known = corpus.labels()
        verdicts = human_findings(corpus)
        decided = human_decisions(corpus)
        parts = []
        for example in [p for p in chosen if p != page and p in rows and p in known]:
            facts, _, _ = page_brief(corpus, known[example], rows[example], with_levels=True)
            judged = [
                f"  finding {i}: {verdicts.get((example, f['evidence']), 'unsure')}"
                for i, f in enumerate(rows[example].get("new_findings", []), start=1)
            ]
            if with_level:
                judged.append(f"  overall level: {decided[example]['level']}")
            parts.append(f"{facts}\nThe human labeller's answer:\n" + "\n".join(judged))
        intro = "" if with_level or not parts else "(finding verdicts only: these pages have no level label yet)\n\n"
        _EXAMPLE_TEXT[key] = intro + "\n\n---\n\n".join(parts)
    return _EXAMPLE_TEXT[key]


def _examples(corpus: Corpus, chosen: dict[str, Any], page: str, rows: dict[str, dict[str, Any]]) -> str:
    known = corpus.labels()
    decided = human_decisions(corpus)
    verdicts = human_findings(corpus)
    parts = []
    for label in ("legit", "junk"):
        for example in [p for p in chosen[label] if p != page][: chosen["per_class"]]:
            facts, _, _ = page_brief(corpus, known[example], rows[example])
            judged = [
                f"  finding {i}: {verdicts.get((example, f['evidence']), 'unsure')}"
                for i, f in enumerate(rows[example].get("new_findings", []), start=1)
            ]
            assert decided[example]["label"] == label, example
            parts.append(f"{facts}\nThe human labeller's answer:\n" + "\n".join(judged) + f"\n  page label: {label}")
    return "\n\n---\n\n".join(parts)


def messages(
    corpus: Corpus, record: LabelRecord, rows: dict[str, dict[str, Any]], prompt: str = PROMPT_VERSION
) -> list[Any]:
    """
    The prompt for one page.

    :param corpus: The corpus.
    :param record: The page's record.
    :param rows: Every page's row.
    :param prompt: The prompt version (:data:`PROMPTS`).
    :return: The system and user messages.
    """
    facts, text, _ = page_brief(corpus, record, rows[record.id], with_levels=prompt in LEVEL_PROMPTS)
    examples = example_block(corpus, record.id, rows, prompt)
    system = f"{PROMPTS[prompt]}\n\nExamples labelled by the human labeller (page text left out):\n\n{examples}"
    user = f"{facts}\n\nPage text (cleaned, cut at {PAGE_CHARS} characters):\n{untrusted(text)}"
    return [SystemMessage(content=system), HumanMessage(content=user)]


class Labeller:
    """One model, its call ledger and the spending cap."""

    def __init__(
        self,
        corpus: Corpus,
        model: str,
        reasoning: str,
        budget: Decimal,
        concurrency: int,
        *,
        prompt: str = PROMPT_VERSION,
    ) -> None:
        """
        Set up the client.

        :param corpus: The corpus (holds the ledger).
        :param model: The model name on the provider.
        :param reasoning: OpenAI ``reasoning_effort`` (``""``: the model's default).
        :param budget: Stop before the whole ledger passes this many euros.
        :param concurrency: Calls in flight at once.
        :param prompt: The prompt version (:data:`PROMPTS`).
        """
        self.corpus = corpus
        self.model = model
        self.reasoning = reasoning
        self.prompt = prompt
        self.budget = budget
        self.semaphore = asyncio.Semaphore(concurrency)
        self.ledger = {r["key"]: r for r in read_jsonl(corpus.root / CALLS)}
        self.spent = spent(corpus)
        client = ChatOpenAI(
            model=model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0,
            reasoning_effort=reasoning or None,
            timeout=300,
            max_retries=0,  # retried by throttled()
        )
        schema = LevelLabel if prompt in LEVEL_PROMPTS else PageLabel
        self.structured = client.with_structured_output(schema, method="function_calling", include_raw=True)

    @property
    def name(self) -> str:
        """The model and its reasoning setting, as recorded."""
        return f"{self.model}+{self.reasoning}" if self.reasoning else self.model

    def over_budget(self) -> bool:
        """Whether the next call could pass the cap (a margin of one dear call)."""
        return self.spent + Decimal("0.05") > self.budget

    async def label(self, record: LabelRecord, rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """
        Label one page (replayed from the ledger when this exact prompt was answered before).

        :param record: The page's record.
        :param rows: Every page's row.
        :return: The ledger row (answer, tokens, cost, model version), or ``None`` (failed, or over budget).
        """
        prompt = messages(self.corpus, record, rows, self.prompt)
        digest = hashlib.sha256("\n".join(str(m.content) for m in prompt).encode()).hexdigest()[:16]
        key = f"{self.name}:{self.prompt}:{record.id}:{digest}"
        if key in self.ledger and self.ledger[key].get("answer"):
            return self.ledger[key]  # an unparsed answer is asked again (and paid again)
        async with self.semaphore:
            if self.over_budget():
                return None
            started = time.monotonic()
            try:
                result = await throttled(lambda: self.structured.ainvoke(prompt), attempt_timeout_s=300)
            except Exception as exc:  # noqa: BLE001 - one page failing doesn't stop the run
                logger.warning("llm_label_failed", id=record.id, model=self.name, error=type(exc).__name__)
                return None
        raw = result["raw"]
        usage = raw.usage_metadata or {}
        tokens_in, tokens_out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cost = call_cost(self.model, tokens_in, tokens_out)
        self.spent += cost
        parsed: PageLabel | LevelLabel | None = result.get("parsed")
        row = {
            "key": key,
            "id": record.id,
            "model": self.model,
            "reasoning": self.reasoning,
            "model_version": str(raw.response_metadata.get("model_name") or self.model),
            "fingerprint": raw.response_metadata.get("system_fingerprint"),
            "prompt_version": self.prompt,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "reasoning_tokens": int((usage.get("output_token_details") or {}).get("reasoning", 0) or 0),
            "cost_eur": float(cost),
            "seconds": round(time.monotonic() - started, 1),
            "answer": parsed.model_dump() if parsed else None,
            "at": now_iso(),
        }
        self.ledger[key] = row
        append_jsonl(self.corpus.root / CALLS, [row])
        return row

    async def label_all(self, records: list[LabelRecord], rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Label several pages concurrently; stops asking once the budget would be passed.

        :param records: The pages.
        :param rows: Every page's row.
        :return: The ledger rows of those that got an answer.
        """
        done: list[dict[str, Any]] = []
        for n, task in enumerate(asyncio.as_completed([self.label(r, rows) for r in records]), start=1):
            row = await task
            if row and row.get("answer"):
                done.append(row)
            if n % 25 == 0:
                logger.info("llm_label_progress", model=self.name, done=n, total=len(records), spent=float(self.spent))
        return done


def verdicts_of(answer: dict[str, Any], findings: list[dict[str, Any]]) -> list[str | None]:
    """The LLM's verdict per finding, in order (``None`` where it gave none)."""
    by_n = {v["n"]: v["verdict"] for v in answer.get("findings", [])}
    return [by_n.get(i) for i in range(1, len(findings) + 1)]


def rate(hits: int, total: int) -> float | None:
    """A share, rounded (``None`` with nothing to compare)."""
    return round(hits / total, 3) if total else None


def compare(corpus: Corpus, answers: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    Agreement of the LLM's answers with Floris's labels.

    :param corpus: The corpus.
    :param answers: The LLM's ledger rows by page.
    :param rows: Every page's row.
    :return: Page agreement (overall, per human label, blind pages), finding agreement, confusions, disagreements;
        for level answers also the level's agreement (:func:`level_agreement`).
    """
    decided = human_decisions(corpus)
    verdicts = human_findings(corpus)
    pages: Counter[str] = Counter()
    confusion: Counter[str] = Counter()
    finding_confusion: Counter[str] = Counter()
    disagreements = []
    finding_disagreements = []
    for page, decision in decided.items():
        if (call := answers.get(page)) is None:
            pages["missing"] += 1
            continue
        answer = call["answer"]
        human, llm = decision["label"], label_of(answer)
        confusion[f"{human}->{llm}"] += 1
        for key in ("all", f"human_{human}", *(["blind"] if decision["blind"] else [])):
            pages[f"{key}_n"] += 1
            pages[f"{key}_agree"] += human == llm
        if human != llm:
            disagreements.append(
                {
                    "id": page,
                    "human": human,
                    "llm": llm,
                    "human_level": decision.get("level"),
                    "llm_level": level_of(answer),
                    "confidence": answer["confidence"],
                    "reason": reason_of(answer),
                }
            )
        findings = rows[page].get("new_findings", [])
        why = {v["n"]: v["why"] for v in answer.get("findings", [])}
        for i, (finding, mine) in enumerate(zip(findings, verdicts_of(answer, findings), strict=True), start=1):
            theirs = verdicts.get((page, finding["evidence"]))
            if theirs not in ("correct", "wrong"):
                continue
            finding_confusion[f"{theirs}->{mine}"] += 1
            pages["findings_n"] += 1
            pages["findings_agree"] += theirs == mine
            if theirs != mine:
                finding_disagreements.append(
                    {"id": page, "human": theirs, "llm": mine, "evidence": finding["evidence"], "why": why.get(i, "")}
                )
    return {
        "pages": pages["all_n"],
        "page_agreement": rate(pages["all_agree"], pages["all_n"]),
        "legit_agreement": rate(pages["human_legit_agree"], pages["human_legit_n"]),
        "junk_agreement": rate(pages["human_junk_agree"], pages["human_junk_n"]),
        "scam_agreement": rate(pages["human_scam_agree"], pages["human_scam_n"]),
        "blind_pages": pages["blind_n"],
        "blind_agreement": rate(pages["blind_agree"], pages["blind_n"]),
        "findings": pages["findings_n"],
        "finding_agreement": rate(pages["findings_agree"], pages["findings_n"]),
        "missing": pages["missing"],
        "confusion": dict(sorted(confusion.items())),
        "finding_confusion": dict(sorted(finding_confusion.items())),
        "disagreements": disagreements,
        "finding_disagreements": finding_disagreements,
    } | level_agreement(decided, answers)


def level_agreement(decided: dict[str, dict[str, Any]], answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    How the LLM's levels agree with Floris's labels: his level where he gave one, his older label's range everywhere.

    :param decided: His latest decision per page.
    :param answers: The LLM's ledger rows by page.
    :return: Nothing for answers without a level; else exact and within-one agreement on his level labels, how
        often the level falls in his older label's range (:data:`evals.corpus.LEGACY_LEVELS`), "any concern" (level
        above 0) agreement, and the confusion (his level or label -> the LLM's level).
    """
    tally: Counter[str] = Counter()
    confusion: Counter[str] = Counter()
    for page, decision in decided.items():
        if (call := answers.get(page)) is None or (llm := level_of(call["answer"])) is None:
            continue
        human = decision.get("level")
        low, high = (human, human) if human is not None else LEGACY_LEVELS[decision["label"]]
        confusion[f"{human if human is not None else decision['label']}->{llm}"] += 1
        tally["n"] += 1
        tally["in_range"] += low <= llm <= high
        if (low > 0) or (high == 0):  # his label settles "any concern" (an old legit label doesn't: 0 to 2)
            tally["concern_n"] += 1
            tally["concern_agree"] += (llm > 0) == (low > 0)
        if human is not None:
            tally["level_n"] += 1
            tally["exact"] += llm == human
            tally["within_one"] += abs(llm - human) <= 1
    if not tally["n"]:
        return {}
    return {
        "level_pages": tally["n"],
        "level_in_human_range": rate(tally["in_range"], tally["n"]),
        "human_level_pages": tally["level_n"],
        "level_exact": rate(tally["exact"], tally["level_n"]),
        "level_within_one": rate(tally["within_one"], tally["level_n"]),
        "any_concern_agreement": rate(tally["concern_agree"], tally["concern_n"]),
        "level_confusion": dict(sorted(confusion.items())),
    }


async def validate(corpus: Corpus, labeller: Labeller) -> dict[str, Any]:
    """
    Label Floris's labelled pages and compare.

    :param corpus: The corpus.
    :param labeller: The model.
    :return: The comparison, with the model, its cost and tokens.
    """
    rows = label_rows(corpus)
    known = corpus.labels()
    records = [known[p] for p in human_decisions(corpus)]
    done = await labeller.label_all(records, rows)
    answers = {r["id"]: r for r in done}
    report = {
        "model": labeller.name,
        "prompt_version": labeller.prompt,
        "cost_eur": round(sum(r["cost_eur"] for r in done), 4),
        "input_tokens": sum(r["input_tokens"] for r in done),
        "output_tokens": sum(r["output_tokens"] for r in done),
        "mean_confidence": rate(round(sum(r["answer"]["confidence"] for r in done) * 1000), 1000 * len(done)),
    } | compare(corpus, answers, rows)
    dump_json(corpus.root / REPORTS / f"validation-{labeller.name}-{labeller.prompt}.json", report)
    return report


def reserve_test_set(corpus: Corpus, size: int) -> list[str]:
    """
    The human-only test set: a seeded random ``size`` pages of the queue the LLM never labels (made once, then kept).

    Drawn from the queue's pages that have no human label yet.

    :param corpus: The corpus.
    :param size: Pages to reserve (only when the set doesn't exist yet).
    :return: The reserved page ids.
    """
    path = corpus.root / TEST_SET
    if path.exists():
        return list(json.loads(path.read_text())["ids"])
    known = corpus.labels()
    pool = sorted(i.id for i in read_queue(corpus) if i.id in known and known[i.id].label_source != "human")
    ids = sorted(random.Random(TEST_SEED).sample(pool, min(size, len(pool))))
    dump_json(
        path,
        {
            "purpose": "human-only test set: the LLM labeller never labels these; Floris labels them (make "
            "deep-label-test); the evaluation set for the decision-model comparison",
            "seed": TEST_SEED,
            "drawn_from": f"{QUEUE}, pages without a human label ({len(pool)})",
            "created_at": now_iso(),
            "size": len(ids),
            "ids": ids,
        },
    )
    return ids


def to_label(corpus: Corpus, pages: int, *, relevel: bool = False) -> list[LabelRecord]:
    """
    The next ``pages`` pages to label in queue order: no human or LLM label yet, not reserved, not rejected.

    :param corpus: The corpus.
    :param pages: How many.
    :param relevel: Also the pages an LLM labelled without a level (an older prompt), to give them one.
    :return: Their records.
    """
    known = corpus.labels()
    rows = label_rows(corpus)
    reserved = set(reserve_test_set(corpus, 0)) if (corpus.root / TEST_SET).exists() else set()
    latest = {r["id"]: r for r in read_jsonl(corpus.root / LLM_LABELS)}
    out = []
    for item in read_queue(corpus):
        record = known.get(item.id)
        if record is None:
            continue
        unlevelled = relevel and is_llm(record.label_source) and record.level is None
        if (
            item.id not in rows
            or item.id in reserved
            or (item.id in latest and not unlevelled)
            or record.label_source == "human"
            or (is_llm(record.label_source) and not unlevelled)
            or notes_of(record).get("rejected")
        ):
            continue
        out.append(record)
        if len(out) >= pages:
            break
    return out


def source_of(model: str, prompt: str) -> str:
    """The label source an LLM labeller's labels get: ``llm:<model>:<prompt version>``."""
    return f"{LLM_SOURCE}{model}:{prompt}"


def store(corpus: Corpus, done: list[dict[str, Any]], rows: dict[str, dict[str, Any]], source: str = "llm") -> int:
    """
    Write LLM labels: ``llm_labels.jsonl`` in full, ``labels.jsonl`` with ``label_source`` ``source``.

    A level answer also sets ``level`` and ``level_source`` (``source``), and the legacy label follows from the
    level (:func:`evals.corpus.legacy_label`), as for Floris's own level labels. A human label is never overwritten.

    :param corpus: The corpus.
    :param done: The ledger rows to store.
    :param rows: Every page's row (for the findings judged).
    :param source: The label source (``llm``, or ``llm:<model>:<prompt>``, :func:`source_of`).
    :return: The pages written.
    """
    with corpus.locked():
        known = corpus.labels()
        lines = []
        updated = []
        for call in done:
            record = known[call["id"]]
            if record.label_source == "human":
                continue
            answer = call["answer"]
            level = level_of(answer)
            findings = rows[call["id"]].get("new_findings", [])
            why = {v["n"]: v["why"] for v in answer.get("findings", [])}
            sure = {v["n"]: v.get("confidence") for v in answer.get("findings", [])}
            lines.append(
                {
                    "id": record.id,
                    "label": label_of(answer),
                    **({"level": level, "because": list(answer.get("because", []))} if level is not None else {}),
                    "label_source": source,
                    "confidence": answer["confidence"],
                    "reason": reason_of(answer),
                    "findings": [
                        {
                            "skill": f["skill"],
                            "type": f["type"],
                            "evidence": f["evidence"],
                            "verdict": v,
                            "why": why.get(i, ""),
                            "confidence": sure.get(i),
                        }
                        for i, (f, v) in enumerate(zip(findings, verdicts_of(answer, findings), strict=True), start=1)
                    ],
                    "model": call["model"],
                    "reasoning": call["reasoning"],
                    "model_version": call["model_version"],
                    "prompt_version": call["prompt_version"],
                    "before": record.label,
                    "before_source": record.label_source,
                    **({"level_before": record.level} if level is not None else {}),
                    "labelled_at": now_iso(),
                }
            )
            update: dict[str, Any] = {"label": label_of(answer), "label_source": source}
            update["confidence"] = answer["confidence"]
            if level is not None:
                update |= {"level": level, "level_source": source}
            updated.append(record.model_copy(update=update))
        known.update((r.id, r) for r in updated)
        corpus.write_labels(known.values())
    append_jsonl(corpus.root / LLM_LABELS, lines)
    return len(lines)


def least_sure(label: dict[str, Any]) -> float:
    """An LLM label's lowest confidence: the page's, or one of its finding verdicts'."""
    scores = [label["confidence"], *(f["confidence"] for f in label["findings"] if f.get("confidence") is not None)]
    return float(min(scores))


def build_review_queue(corpus: Corpus, unsure: float, audit: float) -> list[QueueItem]:
    """
    Floris's review queue of LLM labels: every unsure page, then a seeded random audit share of the rest.

    A page is unsure when its label or one of its finding verdicts has a confidence below ``unsure``. Pages he has
    labelled since drop out.

    :param corpus: The corpus.
    :param unsure: A confidence below this is unsure.
    :param audit: The share of the confident pages drawn at random for a blind audit.
    :return: The queue (unsure pages first, least confident first).
    """
    known = corpus.labels()
    latest = {r["id"]: r for r in read_jsonl(corpus.root / LLM_LABELS)}
    open_ = [r for k, r in sorted(latest.items()) if k in known and is_llm(known[k].label_source)]
    items = [
        QueueItem(
            id=r["id"],
            reason=f"{UNSURE}: LLM says {answer_text(r)} ({r['confidence']:.2f}, least sure {least_sure(r):.2f})",
            priority=2 - least_sure(r),
        )
        for r in open_
        if least_sure(r) < unsure
    ]
    confident = [r["id"] for r in open_ if least_sure(r) >= unsure]
    share = round(audit * len(open_))
    for n, page in enumerate(sorted(random.Random(AUDIT_SEED).sample(confident, min(share, len(confident))))):
        items.append(QueueItem(id=page, reason=f"{AUDIT}: random LLM label, reviewed blind", priority=0.9 - n / 1000))
    items.sort(key=lambda i: (-i.priority, i.id))
    (corpus.root / REVIEW_QUEUE).write_text("".join(i.model_dump_json() + "\n" for i in items))
    return items


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """A validation report without the per-page lists, for the terminal."""
    return {k: v for k, v in report.items() if k not in ("disagreements", "finding_disagreements")}


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "label"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--model", action="append", required=True, help="repeat to compare (validate)")
        cmd.add_argument("--reasoning", action="append", default=None, help="reasoning effort per model ('' default)")
        cmd.add_argument("--budget-eur", type=Decimal, default=Decimal(15), help="cap on the whole ledger")
        cmd.add_argument("--concurrency", type=int, default=4)
        cmd.add_argument("--prompt", choices=sorted(PROMPTS), default=PROMPT_VERSION, help="the prompt version")
    lab = sub.choices["label"]
    lab.add_argument("--pages", type=int, default=600)
    lab.add_argument("--test-size", type=int, default=150)
    lab.add_argument("--source", default=None, help="the label source (default llm:<model>:<prompt>)")
    lab.add_argument("--relevel", action="store_true", help="also give the older prompts' LLM labels a level")
    for cmd in (lab, sub.add_parser("review-queue")):
        cmd.add_argument("--unsure", type=float, default=0.7, help="a confidence below this goes to review")
        cmd.add_argument("--audit", type=float, default=0.05, help="random share of the rest, reviewed blind")
    args = parser.parse_args()
    corpus = Corpus()
    if args.command == "review-queue":
        items = build_review_queue(corpus, args.unsure, args.audit)
        print(json.dumps(Counter(i.reason.split(":")[0] for i in items)))  # noqa: T201 - command-line report
        return
    efforts = args.reasoning or [""] * len(args.model)
    if len(efforts) != len(args.model):
        parser.error("give --reasoning once per --model, or not at all")
    labellers = [
        Labeller(corpus, m, e, args.budget_eur, args.concurrency, prompt=args.prompt)
        for m, e in zip(args.model, efforts, strict=True)
    ]
    if args.command == "validate":
        for labeller in labellers:
            report = asyncio.run(validate(corpus, labeller))
            print(json.dumps(summary(report), indent=1))  # noqa: T201 - command-line report
        print(f"ledger total: €{spent(corpus):.2f}")  # noqa: T201 - command-line report
        return
    labeller = labellers[0]
    reserved = reserve_test_set(corpus, args.test_size)
    if args.relevel and args.prompt not in LEVEL_PROMPTS:
        parser.error("--relevel needs a level prompt")
    records = to_label(corpus, args.pages, relevel=args.relevel)
    rows = label_rows(corpus)
    done = asyncio.run(labeller.label_all(records, rows))
    source = args.source or source_of(labeller.model, labeller.prompt)
    written = store(corpus, done, rows, source)
    items = build_review_queue(corpus, args.unsure, args.audit)
    mine = [r for r in read_jsonl(corpus.root / LLM_LABELS) if r["label_source"] == source]
    report = {
        "source": source,
        "model": labeller.name,
        "prompt_version": labeller.prompt,
        "reserved_test_set": len(reserved),
        "asked": len(records),
        "labelled_now": written,
        "labelled_total": len(mine),
        "labels": dict(Counter(r["label"] for r in mine)),
        "levels": dict(Counter(str(r["level"]) for r in mine if r.get("level") is not None)),
        "finding_verdicts": dict(Counter(str(f["verdict"]) for r in mine for f in r["findings"])),
        "cost_eur": round(sum(r["cost_eur"] for r in done), 4),
        "ledger_eur": round(float(spent(corpus)), 2),
        "review_queue": dict(Counter(i.reason.split(":")[0] for i in items)),
        "low_confidence": sorted(r["id"] for r in mine if least_sure(r) < args.unsure),
    }
    dump_json(corpus.root / REPORTS / f"bulk-{labeller.name}-{labeller.prompt}.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "low_confidence"}, indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
