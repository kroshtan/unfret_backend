"""
Who ships and who sells, from Amazon's buybox labels in its languages, including the offer box's combined label.

The same cases as the extension's test/seller-labels.test.ts; all names and pages are made up.
"""

import pytest

from unfret.facts import parse_page

URL = "https://www.amazon.de/dp/B0FAKE0002"


def _page(label: str, value: str) -> str:
    return f"""<html><head><link rel="canonical" href="{URL}"></head><body>
<span id="productTitle">Nasenspreizer aus Silikon</span>
<div class="offer-display-feature-label" offer-display-feature-name="desktop-merchant-info"><span>{label}</span></div>
<div class="offer-display-feature-text" offer-display-feature-name="desktop-merchant-info">
<span class="offer-display-feature-text-message">{value}</span></div></body></html>"""


@pytest.mark.parametrize(
    ("label", "value", "ships"),
    [
        ("Verzender / Verkoper", "Slaapwel Handel", True),
        ("Versender / Verkäufer", "Gutschlaf GmbH", True),
        ("Expéditeur / Vendeur", "Nuit Calme SARL", True),
        ("Shipper / Seller", "Restful Goods LLC", True),
        ("Mittente / Venditore", "Notte Calma Srl", True),
        ("Remitente / Vendedor", "Noche Tranquila SL", True),
        ("Nadawca / Sprzedawca", "Spokojny Sen", True),
        ("Avsändare / Säljare", "Sovgott Handel", True),
        ("Gönderici / Satıcı", "Rahat Uyku", True),
        ("Verkocht door", "Slaapwel Handel", False),
        ("Verkauf durch", "Gutschlaf GmbH", False),
        ("Vendu par", "Nuit Calme SARL", False),
        ("Venduto da", "Notte Calma Srl", False),
        ("Vendido por", "Noche Tranquila SL", False),
        ("Sprzedawca", "Spokojny Sen", False),
        ("Säljs av", "Sovgott Handel", False),
        ("Satıcı", "Rahat Uyku", False),
        ("Sold by", "Restful Goods LLC", False),
    ],
)
def test_seller_labels(label: str, value: str, ships: bool) -> None:
    facts, _ = parse_page(_page(label, value), URL)
    assert facts.seller == value
    assert facts.ships_from == (value if ships else "")


def test_no_seller_label_leaves_the_seller_out() -> None:
    facts, _ = parse_page(_page("Rückgaben", "Rücksendung innerhalb von 30 Tagen"), URL)
    assert (facts.seller, facts.ships_from) == ("", "")
