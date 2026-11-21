"""
Variant pickers: the options a listing groups under one page (sizes, colours, styles, amounts), on any platform.

Every option of a listing shares its star rating and review pool, and a "from" price is its cheapest option's. A
seller who groups different products as "sizes" (a phone case, a screen protector and a cable) shows them as one
well-reviewed product at the cable's price. These are the facts the check of that needs: each dimension the page
names, each option with its price when the page gives it, the option the page has selected, and which option the
headline price belongs to (:class:`~unfret.facts.schema.Variants`).

Read from the first source that names at least two options, the platform's own data first:

- **Amazon** (every locale): the twister's script data (``dimensions``, ``variationDisplayLabels``,
  ``variationValues``, ``dimensionValuesDisplayData`` per ASIN, ``selectedVariationValues``, ``currentAsin``), with
  each option's price from its swatch when the page shows one; without the script, a picker with one dimension.
- **Walmart**: ``__NEXT_DATA__``'s ``variantCriteria`` and ``variantsMap``.
- **Shopify** (any shop with its product JSON, headless too): the product's ``options`` and ``variants`` (prices in
  cents); the option on show is the ``?variant=`` in the URL, else the first available one, as Shopify's themes do.
- **WooCommerce**: the variations form's ``data-product_variations`` and its selects' labels.
- **schema.org**: a ``ProductGroup``'s ``hasVariant`` and ``variesBy`` (Google's variant markup, which many shop
  systems and marketplaces emit).
- **The page's own picker**: a select or a group of radio buttons named as a size, colour, style or option (never
  quantity, country, currency or sorting), outside the header, navigation and footer; a price in an option's label
  is its price.

The browser extension reads the same (``src/extract/variants.ts``); the parity tests keep the two in step.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_price
from unfret.facts.schema import HeadlineBasis, PageFacts, VariantDimension, VariantKind, VariantOption, Variants

MAX_OPTIONS = 100
MAX_DIMENSIONS = 5
MAX_VALUES = 100
_WS_RE = re.compile(r"\s+")
# What a dimension varies, from its key and name (lower case), first match wins: an amount before a size ("number
# of items", "Packungsgröße"), a colour before a style ("colour style"). Substrings, so any language's compounds match.
_KINDS: tuple[tuple[VariantKind, re.Pattern[str]], ...] = (
    (
        "count",
        re.compile(
            r"number of items|number_of_items|item_package_quantity|unit_count|quantity|anzahl|stückzahl|packung"
            r"|aantal|nombre d|cantidad|quantità|pack|stuks|pieces|pcs"
        ),
    ),
    ("colour", re.compile(r"colou?r|farbe|couleur|kleur|colore|kolor|färg|farve|väri|renk")),
    (
        "size",
        re.compile(
            r"size|größe|grösse|groesse|taille|maat|talla|tamaño|taglia|rozmiar|storlek|størrelse|koko|boyut"
            r"|length|länge|lengte|longueur|capacity|kapazität|capaciteit|dimension|abmessung"
        ),
    ),
    ("pattern", re.compile(r"pattern|muster|motif|patroon|patrón|motivo|design")),
    ("material", re.compile(r"material|materiaal|matériau|materiale")),
    ("flavour", re.compile(r"flavou?r|geschmack|smaak|saveur|sabor|gusto|scent|duft|geur|parfum")),
    (
        "style",
        re.compile(
            r"style|stil|stijl|estilo|stile|styl|model|modèle|modelo|modello|type|typ|soort|version|edition"
            r"|configuration|variant|ausführung|uitvoering"
        ),
    ),
)


def kind_of(key: str, name: str = "") -> VariantKind:
    """
    What a picker's dimension varies, from its key and its label.

    :param key: The page's key for it (Amazon's ``size_name``, a select's ``name``), or "".
    :param name: Its label ("Größe", "Number of Items").
    :return: The kind, ``other`` when neither says.
    """
    words = f"{key} {name}".lower()
    return next((kind for kind, pattern in _KINDS if pattern.search(words)), "other")


def extract(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    """
    Read the page's variant options into ``facts.variants`` (left ``None`` when it has fewer than two).

    :param soup: The parsed page.
    :param url: The page's URL (Shopify's ``?variant=``).
    :param facts: The facts so far (platform, price); updated in place.
    """
    products = _json_ld_nodes(soup)
    found: Variants | None = None
    if facts.platform == "amazon":
        found = _amazon(soup)
    elif facts.platform == "walmart":
        found = _walmart(soup)
    for reader in (lambda: _shopify(soup, url), lambda: _woocommerce(soup), lambda: _schema_org(products)):
        if found is None:
            found = reader()
    if found is None and facts.platform not in {"amazon", "walmart"}:
        found = _picker(soup)
    if found is None:
        return
    from_price = _amazon_range(soup) if facts.platform == "amazon" else _aggregate_range(products)
    facts.variants = _with_headline(found, facts.price, from_price=from_price)


def _with_headline(found: Variants, price: float | None, *, from_price: bool) -> Variants:
    """
    Name the option the headline price belongs to.

    The cheapest under a "from" price, else the selected one, else the one whose price it is.

    :param found: The options.
    :param price: The page's headline price.
    :param from_price: Whether the page shows a "from" price or a range.
    :return: The options with ``headline``, ``headline_basis`` and ``from_price``.
    """
    options = found.options
    priced = sorted((o.price, i) for i, o in enumerate(options) if o.price is not None)
    same = [i for p, i in priced if price is not None and abs(p - price) < 0.005]
    selected = [i for i, o in enumerate(options) if o.selected]
    # A selection whose own price isn't the price on show (Shopify's first available variant, when the page shows
    # another's) doesn't say whose price it is.
    shown = len(selected) == 1 and (price is None or options[selected[0]].price is None or selected[0] in same)
    headline: int | None = None
    basis: HeadlineBasis = ""
    if from_price and priced:
        headline, basis = priced[0][1], "from"
    elif shown:
        headline, basis = selected[0], "selected"
    elif same:
        headline, basis = min(same), "price"
    return found.model_copy(update={"headline": headline, "headline_basis": basis, "from_price": from_price})


def _variants(source: str, dimensions: list[VariantDimension], options: list[VariantOption]) -> Variants | None:
    kept = [o for o in options if o.label][:MAX_OPTIONS]
    if len({o.label for o in kept}) < 2:
        return None
    return Variants.model_validate({"source": source, "dimensions": dimensions[:MAX_DIMENSIONS], "options": kept})


def _dimension(key: str, name: str, values: list[str]) -> VariantDimension:
    return VariantDimension(
        name=_cut(name, 100), kind=kind_of(key, name), values=_dedupe(_cut(v, 200) for v in values)[:MAX_VALUES]
    )


def _option(
    values: list[str], price: float | None, *, label: str = "", available: bool | None = None, selected: bool = False
) -> VariantOption:
    values = [_cut(v, 200) for v in values][:5]
    return VariantOption(
        label=_cut(label or " / ".join(v for v in values if v), 300),
        values=values,
        price=price if price is not None and price > 0 else None,  # 0 is a placeholder, never a price
        available=available,
        selected=selected,
    )


# ── Amazon ──────────────────────────────────────────────────────────────────────────────────────────────────────

_TWISTER_SELECTORS = (
    '#twister_feature_div li, #twister li, [id^="inline-twister-row"] li, #inline-twister li, #twisterContainer li'
)
_SWATCH_PRICE_SELECTORS = ".a-price .a-offscreen, .twisterSwatchPrice, .twister_swatch_price, .a-color-price"
_SWATCH_LABEL_SELECTORS = ".swatch-title-text-display, .swatch-title-text, .twisterTextDiv, .a-button-text"
_DP_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
_CURRENT_ASIN_RE = re.compile(r'"currentAsin"\s*:\s*"([A-Z0-9]{10})"')
_CLICK_TO_SELECT_RE = re.compile(r"^Click to select ")
_RANGE_SELECTORS = (
    "#corePrice_desktop .a-price-range, #corePrice_feature_div .a-price-range, "
    "#corePriceDisplay_desktop_feature_div .a-price-range"
)


def _amazon(soup: BeautifulSoup) -> Variants | None:
    prices = _swatch_prices(soup)
    for script in soup.find_all("script"):
        text = script.string or ""
        if "dimensionValuesDisplayData" in text:
            return _twister(text, prices)
    return _amazon_picker(soup, prices)


def _twister(text: str, prices: dict[str, float]) -> Variants | None:
    keys = [k for k in _list(_after(text, "dimensions")) if isinstance(k, str)]
    labels = _dict(_after(text, "variationDisplayLabels"))
    values = _dict(_after(text, "variationValues"))
    combos = _dict(_after(text, "dimensionValuesDisplayData"))
    chosen = _dict(_after(text, "selectedVariationValues"))
    current = m.group(1) if (m := _CURRENT_ASIN_RE.search(text)) else ""
    all_chosen = all(isinstance(v, int) and v >= 0 for v in chosen.values())
    dimensions = [
        _dimension(key, _str(labels.get(key)) or key, [v for v in _list(values.get(key)) if isinstance(v, str)])
        for key in keys
    ]
    order = [d.values for d in dimensions]
    options: list[tuple[list[int], VariantOption]] = []
    for asin, combo in combos.items():
        names = [v for v in _list(combo) if isinstance(v, str)]
        if not names:
            continue
        rank = [order[i].index(v) if i < len(order) and v in order[i] else MAX_VALUES for i, v in enumerate(names)]
        option = _option(names, prices.get(asin), selected=asin == current and all_chosen)
        options.append((rank, option))
    options.sort(key=lambda pair: (pair[0], pair[1].label))
    return _variants("amazon", dimensions, [o for _, o in options])


def _swatch_prices(soup: BeautifulSoup) -> dict[str, float]:
    prices: dict[str, float] = {}
    for li in soup.select(_TWISTER_SELECTORS):
        asin = _swatch_asin(li)
        node = li.select_one(_SWATCH_PRICE_SELECTORS)
        if not asin or asin in prices or node is None:
            continue
        text = _text(node)
        if re.search(r"\d", text) and (price := parse_price(text)[0]) is not None:
            prices[asin] = price
    return prices


def _swatch_asin(li: Tag) -> str:
    for name in ("data-asin", "data-defaultasin"):
        if (value := str(li.get(name) or "")) and re.fullmatch(r"[A-Z0-9]{10}", value):
            return value
    m = _DP_ASIN_RE.search(str(li.get("data-dp-url") or ""))
    return m.group(1) if m else ""


def _amazon_picker(soup: BeautifulSoup, prices: dict[str, float]) -> Variants | None:
    # Without the twister's data (a page cut short, a layout without it): one dimension's swatches.
    rows = soup.select('[id^="inline-twister-row-"], [id^="variation_"]')
    if len(rows) != 1:
        return None
    row = rows[0]
    key = str(row.get("id") or "").removeprefix("inline-twister-row-").removeprefix("variation_")
    options = []
    for li in row.select("li"):
        label = _swatch_label(li)
        selected = "swatchSelected" in (li.get("class") or []) or li.get("data-initiallyselected") == "true"
        options.append(_option([label], prices.get(_swatch_asin(li)), selected=selected))
    return _variants("amazon", [_dimension(key, key, [o.label for o in options])], options)


def _swatch_label(li: Tag) -> str:
    if (node := li.select_one(_SWATCH_LABEL_SELECTORS)) is not None and (label := _text(node)):
        return _strip_price(label)
    image = li.find("img")
    if isinstance(image, Tag) and (alt := str(image.get("alt") or "").strip()):
        return alt
    return _CLICK_TO_SELECT_RE.sub("", str(li.get("title") or "")).strip()


def _amazon_range(soup: BeautifulSoup) -> bool:
    return soup.select_one(_RANGE_SELECTORS) is not None


# ── Walmart ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _walmart(soup: BeautifulSoup) -> Variants | None:
    script = soup.find("script", id="__NEXT_DATA__")
    try:
        data = json.loads(script.string or "") if isinstance(script, Tag) else {}
    except ValueError:
        return None
    product = _dict(
        _dict(_dict(_dict(_dict(_dict(data).get("props")).get("pageProps")).get("initialData")).get("data")).get(
            "product"
        )
    )
    criteria = [_dict(c) for c in _list(product.get("variantCriteria")) if _dict(c)]
    if not criteria:
        return None
    current = _str(product.get("usItemId"))
    dimensions: list[VariantDimension] = []
    names: list[dict[str, tuple[int, str]]] = []  # per dimension: value id → (position, name)
    for criterion in criteria[:MAX_DIMENSIONS]:
        values = [_dict(v) for v in _list(criterion.get("variantList"))]
        names.append({_str(v.get("id")): (i, _str(v.get("name"))) for i, v in enumerate(values) if _str(v.get("id"))})
        dimensions.append(
            _dimension(_str(criterion.get("id")), _str(criterion.get("name")), [_str(v.get("name")) for v in values])
        )
    options: list[tuple[list[int], VariantOption]] = []
    for item_id, entry in _dict(product.get("variantsMap")).items():
        variant = _dict(entry)
        ids = {_str(i) for i in _list(variant.get("variants"))}
        picked = [next((n for vid, n in dim.items() if vid in ids), (MAX_VALUES, "")) for dim in names]
        if not any(name for _, name in picked):
            continue
        price = _dict(_dict(variant.get("priceInfo")).get("currentPrice")).get("price")
        status = _str(variant.get("availabilityStatus"))
        option = _option(
            [name for _, name in picked],
            float(price) if isinstance(price, int | float) and not isinstance(price, bool) else None,
            available=status == "IN_STOCK" if status else None,
            selected=item_id == current,
        )
        options.append(([i for i, _ in picked], option))
    options.sort(key=lambda pair: (pair[0], pair[1].label))
    return _variants("walmart", dimensions, [o for _, o in options])


# ── Shopify ─────────────────────────────────────────────────────────────────────────────────────────────────────

_SHOPIFY_META_RE = re.compile(r"var meta = (?=\{)")


def _shopify(soup: BeautifulSoup, url: str) -> Variants | None:
    candidates: list[dict[str, Any]] = []
    for script in soup.find_all("script"):
        text = script.string or ""
        if '"variants"' not in text:
            continue
        if str(script.get("type") or "").lower() == "application/json":
            try:
                candidates += _shopify_products(json.loads(text), 0)
            except ValueError:
                continue
        elif m := _SHOPIFY_META_RE.search(text):
            meta = _raw_decode(text, m.end())
            candidates += _shopify_products(_dict(meta).get("product"), 0)
    named = [p for p in candidates if _option_names(p)]
    chosen = named or candidates
    return _shopify_variants(chosen[0], url) if chosen else None


def _shopify_products(node: object, depth: int) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(node, list):
        return [p for item in node for p in _shopify_products(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    variants = node.get("variants")
    if isinstance(variants, list) and variants and all(isinstance(v, dict) and "price" in v for v in variants):
        return [node]
    return [p for value in node.values() for p in _shopify_products(value, depth + 1)]


def _option_names(product: dict[str, Any]) -> list[str]:
    for key in ("options_with_values", "options"):
        items = _list(product.get(key))
        names = [_str(o.get("name")) if isinstance(o, dict) else o if isinstance(o, str) else "" for o in items]
        if names and all(names):
            return names
    return []


def _shopify_variants(product: dict[str, Any], url: str) -> Variants | None:
    names = _option_names(product)
    wanted = parse_qs(urlsplit(url).query).get("variant", [""])[0]
    rows: list[tuple[str, list[str], float | None, bool | None]] = []
    for entry in _list(product.get("variants")):
        variant = _dict(entry)
        values = [v for v in _list(variant.get("options")) if isinstance(v, str)]
        if not values:
            values = [o for k in ("option1", "option2", "option3") if isinstance(o := variant.get(k), str) and o]
        title = _str(variant.get("public_title")) or _str(variant.get("title"))
        if not values and title:
            values = title.split(" / ")
        if not values or values == ["Default Title"]:
            continue
        available = variant.get("available")
        rows.append(
            (
                _str(variant.get("id")),
                values,
                _shopify_price(variant.get("price")),
                available if isinstance(available, bool) else None,
            )
        )
    on_show = next((i for i, row in enumerate(rows) if wanted and row[0] == wanted), None)
    if on_show is None:
        on_show = next((i for i, row in enumerate(rows) if row[3] is not False), None)
    options = [
        _option(values, price, available=available, selected=i == on_show)
        for i, (_, values, price, available) in enumerate(rows)
    ]
    width = max((len(o.values) for o in options), default=0)
    dimensions = [
        _dimension(
            names[i] if i < len(names) else "",
            names[i] if i < len(names) else "",
            [o.values[i] for o in options if i < len(o.values)],
        )
        for i in range(width)
    ]
    return _variants("shopify", dimensions, options)


def _shopify_price(value: object) -> float | None:
    # Shopify's product JSON states amounts in cents ("price": 3849); a decimal string is in the currency already.
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return round(value / 100, 2) if float(value).is_integer() else float(value)
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value) if "." in value else round(int(value) / 100, 2)
    return None


# ── WooCommerce ─────────────────────────────────────────────────────────────────────────────────────────────────


def _woocommerce(soup: BeautifulSoup) -> Variants | None:
    form = soup.select_one("form.variations_form[data-product_variations]")
    if form is None:
        return None
    try:
        variations = json.loads(str(form.get("data-product_variations") or ""))
    except ValueError:
        return None
    if not isinstance(variations, list):
        return None
    labels = {str(label.get("for")): _text(label) for label in form.select("label[for]")}
    keys: list[str] = []
    texts: list[dict[str, str]] = []
    defaults: list[str] = []
    dimensions: list[VariantDimension] = []
    for select in form.select('select[name^="attribute_"]')[:MAX_DIMENSIONS]:
        key = str(select.get("name"))
        entries = [(str(o.get("value") or ""), _text(o)) for o in select.find_all("option")]
        entries = [(value, text) for value, text in entries if value]
        chosen = select.select_one("option[selected]")
        keys.append(key)
        texts.append(dict(entries))
        defaults.append(str(chosen.get("value") or "") if chosen is not None else "")
        name = labels.get(str(select.get("id") or "")) or key.removeprefix("attribute_").removeprefix("pa_")
        dimensions.append(_dimension(key, name, [text for _, text in entries]))
    options = []
    for entry in variations:
        variation = _dict(entry)
        attributes = _dict(variation.get("attributes"))
        picked = [_str(attributes.get(key)) for key in keys]
        values = [texts[i].get(v, v) if v else "Any" for i, v in enumerate(picked)]
        price = variation.get("display_price")
        stock = variation.get("is_in_stock")
        options.append(
            _option(
                values,
                float(price) if isinstance(price, int | float) and not isinstance(price, bool) else None,
                available=stock if isinstance(stock, bool) else None,
                selected=bool(keys) and all(defaults) and picked == defaults,
            )
        )
    return _variants("woocommerce", dimensions, options)


# ── schema.org ProductGroup ─────────────────────────────────────────────────────────────────────────────────────

_VARIES_BY = ("size", "color", "pattern", "material", "suggestedAge", "suggestedGender")


def _json_ld_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            _walk(json.loads(script.string or ""), nodes, 0)
        except ValueError:
            continue
    return nodes


def _walk(node: object, nodes: list[dict[str, Any]], depth: int) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, nodes, depth + 1)
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Product" in kinds or "ProductGroup" in kinds:
            nodes.append(node)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                _walk(node[key], nodes, depth + 1)


def _schema_org(nodes: list[dict[str, Any]]) -> Variants | None:
    for node in nodes:
        variants = [_dict(v) for v in _list(node.get("hasVariant")) if _dict(v)]
        if len(variants) < 2:
            continue
        terms = [_str(t).rstrip("/").rsplit("/", 1)[-1] for t in _list(node.get("variesBy"))]
        terms = [t for t in terms if t] or [t for t in _VARIES_BY if any(v.get(t) for v in variants)]
        options = []
        for variant in variants:
            values = [_named(variant.get(t)) for t in terms]
            offer = _first(variant.get("offers"))
            price = offer.get("price")
            availability = _str(offer.get("availability"))
            options.append(
                _option(
                    values if any(values) else [_str(variant.get("name"))],
                    _amount(price),
                    available=availability.endswith("InStock") if availability else None,
                )
            )
        dimensions = [
            _dimension(t, t, [o.values[i] for o in options if i < len(o.values)]) for i, t in enumerate(terms)
        ]
        if found := _variants("schema_org", dimensions, options):
            return found
    return None


def _aggregate_range(nodes: list[dict[str, Any]]) -> bool:
    # An AggregateOffer from a lowest to a higher price: the page's price is a "from" price (WooCommerce's variable
    # products, many others).
    for node in nodes:
        offers = node.get("offers")
        for entry in offers if isinstance(offers, list) else [offers]:
            offer = _dict(entry)
            low, high = _amount(offer.get("lowPrice")), _amount(offer.get("highPrice"))
            if low is not None and high is not None and high > low:
                return True
    return False


# ── The page's own picker ───────────────────────────────────────────────────────────────────────────────────────

# A picker's name, id or label that says it picks a variant, and the ones that pick something else.
_PICKER_RE = re.compile(
    r"variant|size|colou?r|style|größe|farbe|maat|kleur|taille|couleur|talla|taglia|modell|model"
    r"|pattern|flavou?r|scent|pack",
    re.IGNORECASE,
)
_NOT_PICKER_RE = re.compile(
    r"quantity|qty|country|currency|language|locale|sort|filter|ship|deliver|pickup|payment|region|province|state"
    r"|search|amount|menge|aantal",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"^(?:choose|select|pick|please|kies|wähle|bitte|choisi|sélection|elige|selecciona|scegli|-+)", re.IGNORECASE
)
_TRAILING_PRICE_RE = re.compile(
    r"\s*[-–—:(]?\s*(?:[€$£]|EUR|USD|GBP)\s?\d[\d.,]*\)?\s*$|\s*[-–—:(]?\s*\d[\d.,]*\s?(?:€|EUR|zł|kr)\)?\s*$"
)
_ID_RE = re.compile(r"^\d{5,}$")
_PRICE_IN_LABEL_RE = re.compile(r"[€$£]|EUR|USD|GBP|zł|kr")


def _picker(soup: BeautifulSoup) -> Variants | None:
    labels = {str(label.get("for")): _text(label) for label in soup.find_all("label", attrs={"for": True})}
    dimensions: list[VariantDimension] = []
    groups: list[list[tuple[str, bool]]] = []  # per dimension: (label, selected)
    for select in soup.find_all("select"):
        name = _picker_name(select, labels)
        if name is None:
            continue
        entries = [
            (_text(o), o.has_attr("selected"))
            for o in select.find_all("option")
            if str(o.get("value", _text(o))) and not _PLACEHOLDER_RE.search(_text(o))
        ]
        _add_group(dimensions, groups, name, entries)
    radios: dict[str, list[Tag]] = {}
    for radio in soup.find_all("input", attrs={"type": re.compile("^radio$", re.IGNORECASE)}):
        if radio.get("name"):
            radios.setdefault(str(radio["name"]), []).append(radio)
    for key, inputs in radios.items():
        name = _picker_name(inputs[0], labels, key=key)
        if name is None:
            continue
        entries = [(_radio_label(r, labels), r.has_attr("checked")) for r in inputs]
        _add_group(dimensions, groups, name, entries)
    if not groups:
        return None
    # Options combine dimensions only where the page's data says how; a picker doesn't, so its first dimension's
    # values are the options.
    options = []
    for label, selected in groups[0]:
        # Only a price at the end of the label ("Single 140 x 200 cm – €89.00"): never a size's number.
        tail = _TRAILING_PRICE_RE.search(label)
        price = parse_price(tail.group())[0] if tail and _PRICE_IN_LABEL_RE.search(tail.group()) else None
        options.append(_option([_strip_price(label)], price, selected=selected))
    return _variants("picker", dimensions, options)


def _picker_name(tag: Tag, labels: dict[str, str], key: str = "") -> str | None:
    if tag.find_parent(["header", "nav", "footer"]) is not None:
        return None
    key = key or str(tag.get("name") or "")
    label = labels.get(str(tag.get("id") or ""), "") if not key or tag.name == "select" else ""
    names = f"{key} {tag.get('id') or ''} {tag.get('aria-label') or ''} {label}"
    if not _PICKER_RE.search(names) or _NOT_PICKER_RE.search(names):
        return None
    group = tag.find_parent("fieldset")
    legend = group.find("legend") if group is not None else None
    return _text(legend) if isinstance(legend, Tag) and _text(legend) else label or str(tag.get("aria-label") or key)


def _radio_label(radio: Tag, labels: dict[str, str]) -> str:
    if label := labels.get(str(radio.get("id") or "")):
        return label
    parent = radio.find_parent("label")
    return _text(parent) if parent is not None else str(radio.get("value") or "")


def _add_group(
    dimensions: list[VariantDimension],
    groups: list[list[tuple[str, bool]]],
    name: str,
    entries: list[tuple[str, bool]],
) -> None:
    # A label that is only a long number is a product ID the page didn't name.
    entries = [(_cut(label, 300), sel) for label, sel in entries if label and not _ID_RE.match(label)][:MAX_OPTIONS]
    if len({label for label, _ in entries}) < 2 or len(dimensions) >= MAX_DIMENSIONS:
        return
    dimensions.append(_dimension(name, name, [_strip_price(label) for label, _ in entries]))
    groups.append(entries)


def _strip_price(label: str) -> str:
    return _TRAILING_PRICE_RE.sub("", label).strip() or label


# ── Helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _after(text: str, key: str) -> object:
    m = re.search(rf'"{key}"\s*:\s*(?=[\[{{])', text)
    return _raw_decode(text, m.end()) if m else None


def _raw_decode(text: str, start: int) -> object:
    try:
        return json.JSONDecoder().raw_decode(text, start)[0]
    except ValueError:
        return None


def _first(value: object) -> dict[str, Any]:
    return _dict(value[0] if isinstance(value, list) and value else value)


def _named(value: object) -> str:
    if isinstance(value, dict):
        return _str(value.get("name") or value.get("value"))
    return _str(value)


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        return float(value.strip())
    return None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str:
    if isinstance(value, str):
        return _WS_RE.sub(" ", value).strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""


def _cut(value: str, limit: int) -> str:
    return value[:limit]


def _dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
