"""
How a web shop lets you pay: only by methods that can't be reversed, or with a card or buyer-protected method too.

A bank transfer, crypto, gift cards or a money transfer service (Western Union, MoneyGram) can't be charged back:
once paid, the money is gone. Fake shops prefer them. Honest shops offer them too, but next to a card, PayPal,
iDEAL, Klarna or another method with a way back. So the signal is the combination: irreversible methods mentioned,
and no protected method anywhere on the page (the text, logo alt texts, or a payment provider's script).

Gift cards alone don't count: most shops that mention them sell them. Shops on Shopify are left out of "only
irreversible": its checkout always takes cards, whatever the product page shows. Reads ``PageFacts.payment_methods``
(:mod:`unfret.facts.business`; the extension fills the same field), no lookups.
"""

from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

# Methods with no way back once paid.
IRREVERSIBLE = frozenset({"bank_transfer", "crypto", "gift_card", "western_union", "moneygram"})
# Irreversible methods that count on their own (a "gift card" mention is usually a shop selling them).
_IRREVERSIBLE_ALONE = IRREVERSIBLE - {"gift_card"}
# Methods with a chargeback, a buyer-protection programme, or payment after delivery.
PROTECTED = frozenset(
    {
        "paypal",
        "credit_card",
        "visa",
        "mastercard",
        "amex",
        "maestro",
        "ideal",
        "bancontact",
        "klarna",
        "afterpay",
        "sofort",
        "giropay",
        "apple_pay",
        "google_pay",
        "cash_on_delivery",
    }
)
# Payment providers whose scripts on a page mean card (or wallet) payments at checkout.
_PROVIDER_SCRIPTS = (
    "js.stripe.com",
    "paypal.com/sdk",
    "paypalobjects.com",
    "klarna",
    "adyen",
    "mollie",
    "braintreegateway",
    "checkout.com",
    "pay.google.com",
    "applepay",
    "afterpay",
    "clearpay",
    "riverty",
)
LABELS = {
    "bank_transfer": "bank transfer",
    "crypto": "crypto",
    "gift_card": "gift cards",
    "western_union": "Western Union",
    "moneygram": "MoneyGram",
    "paypal": "PayPal",
    "credit_card": "credit card",
    "visa": "Visa",
    "mastercard": "Mastercard",
    "amex": "American Express",
    "maestro": "Maestro",
    "ideal": "iDEAL",
    "bancontact": "Bancontact",
    "klarna": "Klarna",
    "afterpay": "Afterpay",
    "sofort": "Sofort",
    "giropay": "giropay",
    "apple_pay": "Apple Pay",
    "google_pay": "Google Pay",
    "cash_on_delivery": "cash on delivery",
}


def listed(methods: list[str]) -> str:
    """
    Payment methods in words, in the order given: "bank transfer and crypto".

    :param methods: Method keys (:data:`LABELS`).
    :return: The phrase.
    """
    names = [LABELS.get(m, m.replace("_", " ")) for m in methods]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class PaymentMethodsSkill(Skill):
    """Irreversible payment methods without a protected one, and buyer-protected methods offered."""

    name = "payment_methods"
    description = "Checks whether a web shop takes only payments that can't be reversed (bank transfer, crypto)."
    page_types = STOREFRONTS
    features = ("irreversible_only", "irreversible", "protected")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Compare the payment methods the page mentions.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        methods = list(dict.fromkeys(facts.payment_methods))
        irreversible = [m for m in methods if m in IRREVERSIBLE]
        protected = [m for m in methods if m in PROTECTED]
        provider = any(p in src.lower() for src in facts.script_srcs for p in _PROVIDER_SCRIPTS)
        only = (
            any(m in _IRREVERSIBLE_ALONE for m in irreversible)
            and not protected
            and not provider
            and facts.platform != "shopify"
        )
        observations: list[Observation] = []
        if only:
            observations.append(
                Observation(
                    type="irreversible_payment_only",
                    evidence=f"Payment methods mentioned: {listed(irreversible)}; no card, PayPal or other "
                    "buyer-protected method.",
                )
            )
        elif protected:
            observations.append(
                Observation(type="protected_payment", evidence=f"Payment methods mentioned: {listed(protected[:4])}.")
            )
        return self.output(
            {"irreversible_only": only, "irreversible": bool(irreversible), "protected": bool(protected) or provider},
            observations,
            note="" if methods else "No payment methods mentioned.",
        )
