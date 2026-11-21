"""
The variant check: a listing that groups different products as its "sizes", and a price shown for the cheapest one.

On marketplaces with a variant picker (Amazon, Walmart, bol.com, eBay, Temu-style listings) and on web shops alike, a
seller can put different products under one listing as its sizes, colours or styles: a phone case, a screen
protector and a charging cable as "Size". All of them then share one star rating and one review pool, and the price
the listing shows is the cheapest option's: the cable's, under the case's title and photos. This skill reads the
page's options (:class:`~unfret.facts.schema.Variants`, from any platform's variant data or the page's own picker):

1. **Deterministic first.** Fewer than two options, or options that are plainly one product's sizes and colours (S,
   M, L; 40, 42; 500 ml; "2 stuks"; colour names) with prices that don't spread far, cost nothing and say nothing.
2. **One structured call** to the claim audit's model (``settings.claim_audit_model``, same provider, same timeout
   and concurrency, temperature 0) for the rest: what the title sells, what each option is (in a few English words)
   and whether it is that product (in another size, colour, amount, model or bundle) or a part, an accessory, a
   refill, a single unit or a different item; and whether the listing as a whole groups different products.
3. **Checked, then told only when clear.** Every option the model names must be one of the page's own options; its
   nouns must be plain words; the grouping must be confident (``settings.variant_check_min_confidence``) and have at
   least one option that is the title's product and one that isn't. Then:

   - ``variants_grouped`` (a note): "This listing groups different products as sizes: … Its star rating covers all of
     them." It never changes the band by itself.
   - ``variant_bait_price``: the price the listing shows belongs to an option that isn't the product in the title (a
     "from" price, the selected option, or the option the price is): "The price shown, €9.99, is for “Cable”, a
     charging cable, not the phone case in the title …". A page the scorer calls fine is "Worth a look" with it,
     whoever sells it (:mod:`unfret.shopper`). When the page shows the title product's options' prices too, the bait
     must be clearly cheaper (:data:`BAIT_SHARE`).

   The spread of the prices against what the size dimension explains (:func:`price_spread`) goes to the model as
   context and is a feature; on its own it is never told.

Deep checks only (the quick tier runs no model), product pages only; off with ``settings.variant_check``. The page
text goes to the model as untrusted data. Tokens and cost are logged (``variant_check_usage``), without the page.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.schema import VariantOption, Variants
from unfret.graph.skills import PRODUCT_PAGES, Observation, Skill, SkillOutput
from unfret.graph.skills.claim_audit import Usage, normalized
from unfret.graph.state import AnalysisState
from unfret.llm import UNTRUSTED_NOTICE, get_structured_claims_llm, untrusted

logger = structlog.get_logger("unfret")

Relation = Literal["same", "part", "accessory", "refill", "single_unit", "different"]

# Options shown to the model, at most (the cheapest, the selected and the headline's always among them).
MAX_OPTIONS = 40
# The page text the model reads after the options: what the title, bullets and description say the product is.
MAX_TEXT_CHARS = 3_000
# A bait option's price, at most this share of the cheapest option that is the title's product (when both are known).
BAIT_SHARE = 0.6
# Prices spread at least this far (highest over lowest), beyond what the options' own amounts explain.
SPREAD_RATIO = 5.0

_SYSTEM = f"""\
You check how a product listing's variant picker is used, for a consumer protection service. A listing groups its \
options (sizes, colours, styles, amounts) under one title, one set of photos and one star rating. Some sellers put \
DIFFERENT products under one listing as its "sizes" or "styles", e.g. "Phone case" / "Screen protector" / "USB-C \
cable" under "Size", or a refill, a spare part or one single piece next to the full product, so that the cheap item \
shows as the listing's price and every item shares the reviews.

You get the title, the price shown, the options (with their prices when the page gives them) and some page text. \
Answer:
- product: what the title sells, in English, one to four plain words ("phone case", "electric toothbrush").
- options: for each option (at most {MAX_OPTIONS}), its label VERBATIM as given, what it is in English (one to four \
plain words) and its relation to the product in the title:
  same: the title's product, in another size, colour, capacity, amount or pack size, model or edition, or a bundle \
or set that includes it (a case for another phone model is still a phone case: same);
  part: a spare or replacement part of it (a brush head, a strap, a lid);
  accessory: something used with it but not it (a charger, a cable, a stand, a protector, a bag);
  refill: a consumable for it (cartridges, pods, filters, refills);
  single_unit: one piece where the title sells a set or a multi-pack;
  different: another kind of product altogether.
- grouping: same_product when every option is the title's product in another size, colour, amount, model or \
bundle; different_products when any option is a part, an accessory, a refill, a single unit or another kind of item \
than the title's product.
- confidence: how sure you are of the grouping (0-1).

Be conservative. Sizes, colours, flavours, scents, pack sizes, capacities, models for different devices, editions \
and bundles of the same product are same_product. Product lines sold as one family (a 2-person and a 4-person tent; \
a 500 ml and a 1 l bottle) are same_product. Only say different_products when an option is plainly a different kind \
of item than the title's product. A refill, a spare part or an accessory of the title's product is NOT the same \
product, even from the same brand and product line (brush heads under an electric toothbrush's title, a strap under a \
watch's). When unsure, say same_product.

{UNTRUSTED_NOTICE}\
"""


def _cap(value: object, limit: int) -> object:
    return " ".join(value.split())[:limit] if isinstance(value, str) else value


class OptionRead(BaseModel):
    """What one option is, as the model reads it."""

    label: str = Field(description="The option's label, verbatim as given.")
    item: str = Field(description="What it is, in English, one to four plain words.")
    relation: Relation = Field(description="same, part, accessory, refill, single_unit or different.")

    @field_validator("label", mode="before")
    @classmethod
    def _short_label(cls, value: object) -> object:
        return _cap(value, 300)

    @field_validator("item", mode="before")
    @classmethod
    def _short_item(cls, value: object) -> object:
        return _cap(value, 40)


class VariantAnswer(BaseModel):
    """The model's answer: the title's product, each option, and the grouping."""

    product: str = Field(description="What the title sells, in English, one to four plain words.")
    options: list[OptionRead] = Field(default_factory=list, description=f"At most {MAX_OPTIONS} options.")
    grouping: Literal["same_product", "different_products"] = "same_product"
    confidence: float = Field(default=0.0, ge=0, le=1)

    @field_validator("product", mode="before")
    @classmethod
    def _short_product(cls, value: object) -> object:
        return _cap(value, 40)

    @field_validator("options", mode="before")
    @classmethod
    def _few(cls, value: object) -> object:
        return value[:MAX_OPTIONS] if isinstance(value, list) else value

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> object:
        return min(max(value, 0.0), 1.0) if isinstance(value, int | float) and not isinstance(value, bool) else value


# ── Deterministic: what needs no model ─────────────────────────────────────────────────────────────────────────

# A value that is plainly a size or an amount: clothing and shoe sizes, numbers with a unit or a count word, ranges.
_PLAIN_RE = re.compile(
    r"^(?:(?:[0-9]*x{0,3}[sml]|x{1,3}l|[2-6]xl|xxs|xs|small|medium|large|x-large|xx-large|one size|os|onesize"
    r"|petite|regular|tall|long|short|wide|narrow|standard|twin|twin xl|full|queen|king|california king|single"
    r"|double|eu|us|uk|w|l|size|maat|größe|taille|talla|taglia|\(|\)|/|-|,|x|×|[€$£]?[0-9]+(?:[.,][0-9]+)?(?:/[0-9]+)?"
    r"|[0-9]+(?:[.,][0-9]+)?\s?(?:mm|cm|m|in|inch|inches|ft|ml|cl|l|oz|fl oz|g|kg|lb|lbs|mah|wh|w|v|gb|tb|pcs|pc"
    r"|pieces|piece|pack|packs|count|ct|stuks|stuk|stück|pièces|piezas|pezzi|sets?|pairs?|paar|rolls?|sheets?"
    r"|tablets?|capsules?|bags?|bottles?|cans?|jars?|boxes?|units?|x))\s*)+$",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)")


def plain(value: str) -> bool:
    """
    Whether an option's value is plainly a size or an amount ("XL", "42 2/3", "500 ml", "Pack of 2", "2 stuks").

    :param value: One dimension value.
    :return: ``True`` for a size or an amount.
    """
    text = re.sub(r"\bpack of\b|\bset of\b|\bsize\b", " ", value.lower())
    return bool(_PLAIN_RE.match(text.strip()))


def _colourish(value: str) -> bool:
    # A colour's name: a few words, no digits ("Sea Glass Aura", "Dark Smoke Grey/Black/Cool Grey/Chrome Yellow").
    return not re.search(r"[0-9]", value) and len(re.split(r"[\s/-]+", value)) <= 8


def worth_asking(variants: Variants) -> bool:
    """
    Whether the options need the model: anything but one product's plain sizes, amounts and colour names.

    :param variants: The page's options.
    :return: ``True`` when at least two options exist and some value isn't plain, or the prices spread far.
    """
    if len({o.label for o in variants.options}) < 2:
        return False
    if price_spread(variants.options) >= SPREAD_RATIO:
        return True
    for dimension in variants.dimensions:
        for value in dimension.values:
            if dimension.kind == "colour" and _colourish(value):
                continue
            if not plain(value):
                return True
    if not variants.dimensions:
        return any(not plain(o.label) for o in variants.options)
    return False


def price_spread(options: list[VariantOption]) -> float:
    """
    How far the options' prices spread beyond what their own amounts explain.

    Highest over lowest, divided by the ratio of the largest to the smallest number in their labels when every priced
    label has one ("2 stuks" at €4 and "20 stuks" at €30 spread 0.75 times: explained).

    :param options: The options.
    :return: The unexplained spread (1.0 without at least two prices).
    """
    priced = [o for o in options if o.price]
    if len(priced) < 2:
        return 1.0
    prices = [o.price or 0.0 for o in priced]
    ratio = max(prices) / min(prices)
    amounts = [_amount(o.label) for o in priced]
    if all(a for a in amounts):
        known = [a for a in amounts if a]
        ratio /= max(known) / min(known)
    return round(max(ratio, 1.0), 2)


def _amount(label: str) -> float | None:
    m = _QUANTITY_RE.search(label)
    if m is None:
        return None
    value = float(m.group(1).replace(",", "."))
    return value if value > 0 else None


# ── The model's read, checked ──────────────────────────────────────────────────────────────────────────────────

_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
_WORDS_RE = re.compile(r"^[^\W\d_]+(?:['’-][^\W\d_]+)?(?: [^\W\d_]+(?:['’-][^\W\d_]+)?){0,3}$")
_UNSAFE_RE = re.compile(
    r"https?://|www\.|<[a-z/]|\b(?:scam\w*|fraud\w*|fake\w*|counterfeit\w*|illegal\w*|rip-?off)\b", re.IGNORECASE
)
_KIND_WORDS = {
    "size": "sizes",
    "colour": "colours",
    "style": "styles",
    "count": "amounts",
    "pattern": "patterns",
    "flavour": "flavours",
    "material": "materials",
}


def _words(text: str) -> str:
    """A model's noun, only when it is one to four plain words (it goes into what the shopper reads)."""
    text = " ".join(text.split()).lower()
    return text if len(text) <= 40 and _WORDS_RE.match(text) and not _UNSAFE_RE.search(text) else ""


def money(amount: float, currency: str) -> str:
    """
    A price as the shopper reads it: "€9.99", "$1,299.00", "12.50 PLN".

    :param amount: The amount.
    :param currency: The page's ISO currency.
    :return: The amount with its symbol, or with its code after it.
    """
    symbol = _SYMBOLS.get(currency.upper(), "")
    text = f"{amount:,.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency.upper()}".strip()


@dataclass(frozen=True)
class Read:
    """One option the model read, matched to the page's option (its index in ``Variants.options``)."""

    index: int
    item: str
    relation: Relation


@dataclass(frozen=True)
class VariantFindings:
    """What the check found: the grouping and the bait price, each as the sentence the shopper reads ('' if not)."""

    grouped: str = ""
    bait: str = ""
    asked: bool = False
    spread: float = 1.0
    usage: Usage = Usage()


def matched(answer: VariantAnswer, options: list[VariantOption]) -> list[Read]:
    """
    The model's reads of options that are really on the page, each once, with a plain noun.

    :param answer: The model's answer.
    :param options: The page's options.
    :return: The reads, in the page's order.
    """
    by_label: dict[str, int] = {}
    for i, option in enumerate(options):
        by_label.setdefault(normalized(option.label), i)
    reads: dict[int, Read] = {}
    for read in answer.options:
        index = by_label.get(normalized(read.label))
        item = _words(read.item)
        if index is not None and index not in reads and item:
            reads[index] = Read(index, item, read.relation)
    return [reads[i] for i in sorted(reads)]


def _label(option: VariantOption, currency: str) -> str:
    label = option.label.replace('"', "'").replace("“", "'").replace("”", "'")[:80]
    return f"“{label}” ({money(option.price, currency)})" if option.price else f"“{label}”"


def _a(item: str) -> str:
    # "an accessory", "a USB-C cable", "a unit", "brush head refills": by the sound, none for a plural.
    if item.endswith("s") and not item.endswith(("ss", "us", "is")):
        return item
    sounds_vowel = item[:1] in "aeio" and not item.startswith(("one", "eu"))
    vowel = sounds_vowel or (item[:1] == "u" and not item.startswith(("us", "uni", "use", "ut")))
    return f"an {item}" if vowel else f"a {item}"


def _is(option: VariantOption, item: str, currency: str) -> str:
    # "the €9.99 option is a charging cable"; nothing when the label already says what it is.
    if normalized(option.label).strip(" .") == item:
        return ""
    if option.price:
        return f"the {money(option.price, currency)} option is {_a(item)}"
    return f"“{option.label.replace('"', "'")[:80]}” is {_a(item)}"


def _listed(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def findings(answer: VariantAnswer, variants: Variants, facts: PageFacts) -> tuple[str, str]:
    """
    The grouping note and the bait price, worded for the shopper, when the answer holds up.

    :param answer: The model's answer.
    :param variants: The page's options.
    :param facts: The page facts (title, currency, rating count).
    :return: The grouping sentence and the bait sentence ('' each when not clear).
    """
    options = variants.options
    reads = matched(answer, options)
    same = [r for r in reads if r.relation == "same"]
    other = [r for r in reads if r.relation != "same"]
    if (
        answer.grouping != "different_products"
        or answer.confidence < settings.variant_check_min_confidence
        or not same
        or not other
        or any(_UNSAFE_RE.search(options[r.index].label) for r in reads)
    ):
        return "", ""
    currency = facts.currency
    product = _words(answer.product) or "product"
    # The title's product first (its cheapest option), then each different item once (its cheapest option).
    groups = [same] + [[r for r in other if r.item == item] for item in dict.fromkeys(r.item for r in other)]
    shown = [min(g, key=lambda r: (options[r.index].price or float("inf"), r.index)) for g in groups][:4]
    dimension = variants.dimensions[0].kind if variants.dimensions else "other"
    how = f"as {_KIND_WORDS[dimension]}" if dimension in _KIND_WORDS else "under one listing"
    parts = [_label(options[r.index], currency) for r in shown]
    what = [_is(options[r.index], r.item, currency) for r in shown[1:3]]
    said = "; " + "; ".join(w for w in what if w) if any(what) else ""
    pooled = (
        " Its star rating covers all of them."
        if facts.rating_count
        else " Its reviews cover all of them."
        if facts.reviews
        else ""
    )
    grouped = f"This listing groups different products {how}: {_listed(parts)}{said}.{pooled}"
    bait = _bait(variants, reads, same, product, currency)
    return grouped[:500], (f"{bait} {grouped}"[:500] if bait else "")


def _bait(variants: Variants, reads: list[Read], same: list[Read], product: str, currency: str) -> str:
    # The headline price's option is a different item than the title's product, and clearly cheaper when both known.
    if variants.headline is None:
        return ""
    head = next((r for r in reads if r.index == variants.headline), None)
    if head is None or head.relation == "same":
        return ""
    options = variants.options
    price = options[head.index].price
    same_prices = [p for r in same if (p := options[r.index].price)]
    if price and same_prices and price > BAIT_SHARE * min(same_prices):
        return ""
    label = options[head.index].label.replace('"', "'")[:80]
    tail = f"; the {product} itself is from {money(min(same_prices), currency)}" if same_prices else ""
    if price and variants.headline_basis in {"from", "price"}:
        shown = money(price, currency)
        return f"The price shown, {shown}, is for “{label}”, {_a(head.item)}, not the {product} in the title{tail}."
    return f"The option on show, “{label}”, is {_a(head.item)}, not the {product} in the title{tail}."


def document(facts: PageFacts, variants: Variants, text: str) -> str:
    """
    What the model reads: the title and price, the options, then the start of the page text.

    :param facts: The page facts.
    :param variants: The page's options.
    :param text: The cleaned page text.
    :return: The document (wrapped as untrusted by the caller).
    """
    options = variants.options
    keep = set(range(min(len(options), MAX_OPTIONS)))
    priced = sorted((o.price, i) for i, o in enumerate(options) if o.price)
    keep |= {i for i in (variants.headline, priced[0][1] if priced else None) if i is not None}
    keep |= {i for i, o in enumerate(options) if o.selected}
    lines = [f"Title: {facts.title[:300]}"]
    if facts.brand:
        lines.append(f"Brand: {facts.brand[:100]}")
    if facts.price is not None:
        lines.append(f"Price shown: {money(facts.price, facts.currency)}")
    lines += [f"Picker: {d.name or '(unnamed)'} ({len(d.values)} values)" for d in variants.dimensions]
    lines.append("Options:")
    for i in sorted(keep)[: MAX_OPTIONS + 3]:
        o = options[i]
        notes = [money(o.price, facts.currency)] if o.price else []
        if o.selected:
            notes.append("selected")
        if i == variants.headline:
            notes.append("the price shown is this option's")
        lines.append(f"- {o.label}" + (f" ({', '.join(notes)})" if notes else ""))
    if (spread := price_spread(options)) >= SPREAD_RATIO:
        lines.append(f"The prices spread {spread:g} times beyond what the options' own amounts explain.")
    return "\n".join(lines) + f"\n\nPage text:\n{text[:MAX_TEXT_CHARS]}"


async def check(facts: PageFacts, text: str) -> VariantFindings:
    """
    Check the page's options: deterministically first, then with one model call when it could matter.

    :param facts: The page facts (``variants``).
    :param text: The cleaned page text.
    :return: What was found.
    :raises ValueError: When the model's answer can't be read.
    """
    variants = facts.variants
    if variants is None or not worth_asking(variants):
        return VariantFindings(spread=price_spread(variants.options) if variants else 1.0)
    started = time.monotonic()
    out: dict[str, Any] = await get_structured_claims_llm(VariantAnswer).ainvoke(
        [SystemMessage(content=_SYSTEM), HumanMessage(content=untrusted(document(facts, variants, text)))]
    )
    meta = getattr(out.get("raw"), "usage_metadata", None) or {}
    spent = Usage(int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0)), time.monotonic() - started)
    answer = out.get("parsed")
    if not isinstance(answer, VariantAnswer):
        raise ValueError("The variant check's answer couldn't be read.")
    grouped, bait = findings(answer, variants, facts)
    logger.info(
        "variant_check_usage",
        model=settings.claim_audit_model,
        input_tokens=spent.input_tokens,
        output_tokens=spent.output_tokens,
        seconds=round(spent.seconds, 2),
        cost_eur=str(round(spent.cost_eur(), 5)),
        options=len(variants.options),
        grouping=answer.grouping,
        told=bool(grouped),
    )
    return VariantFindings(grouped, bait, asked=True, spread=price_spread(variants.options), usage=spent)


class VariantCheckSkill(Skill):
    """Options that are different products grouped as sizes, and a price shown for one that isn't the product."""

    name = "variant_check"
    description = "Checks whether the listing's options are one product or different products grouped as sizes."
    page_types = PRODUCT_PAGES
    retry = True
    features = ("options", "asked", "grouped", "bait_price", "price_spread")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Check the page's variant options (:func:`check`) and report the clear cases.

        :param state: Current graph state; uses ``facts`` and ``cleaned_content``.
        :return: The skill's features and findings.
        """
        if not settings.variant_check:
            return self.not_applicable("The variant check is off.")
        facts = state["facts"]
        found = await check(facts, state.get("cleaned_content", ""))
        observations = []
        if found.bait:
            observations.append(Observation(type="variant_bait_price", evidence=found.bait))
        if found.grouped:
            observations.append(Observation(type="variants_grouped", evidence=found.grouped))
        options = len(facts.variants.options) if facts.variants else 0
        return self.output(
            {
                "options": options,
                "asked": found.asked,
                "grouped": bool(found.grouped),
                "bait_price": bool(found.bait),
                "price_spread": min(found.spread, 100.0),
            },
            observations,
            note=f"{options} variant options; none that looks like a different product." if options else "No options.",
        )
