"""
Other web shops run with the same accounts: the same analytics or pixel ID, payment account or contact details.

Reads the shop fingerprint store (:mod:`unfret.shop_store`); no outside lookups, so it runs in the quick tier too.
Links are one hop and only on identifiers unique to one account. The features are computed from the linked shops'
own facts (when Unfret first saw them, their registration and first-certificate dates), never from what Unfret
concluded about them. Contact details are counted, never shown.
"""

from datetime import date, timedelta

from unfret import shop_store
from unfret.config import settings
from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

YOUNG_DAYS = 180  # a linked shop registered (or first certified) this recently is a young shop
_SHOWN = 3

KIND_LABELS = {
    "ga": "Google Analytics ID",
    "gtm": "Google Tag Manager container",
    "google_ads": "Google Ads ID",
    "adsense": "AdSense publisher ID",
    "meta_pixel": "Meta pixel ID",
    "tiktok_pixel": "TikTok pixel ID",
    "pinterest_tag": "Pinterest tag ID",
    "stripe": "Stripe account",
    "paypal": "PayPal merchant app",
    "myshopify": "Shopify store",
    "email": "contact e-mail address",
    "phone": "contact phone number",
}
_PAYMENT_KINDS = frozenset({"stripe", "paypal", "myshopify"})


def _young(link: shop_store.Linked, today: date) -> bool:
    born = min((d for d in (link.registered, link.first_cert) if d), default=None)
    return born is not None and today - born <= timedelta(days=YOUNG_DAYS)


class ShopLinksSkill(Skill):
    """Shops sharing an account identifier with this one, and how young those shops are."""

    name = "shop_links"
    description = "Finds other web shops using the same analytics, pixel, payment or contact identifiers."
    page_types = STOREFRONTS
    features = ("linked_shops", "linked_young_shops", "shared_payment", "shared_contact")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look up the page's identifiers and the shop's stored ones.

        :param state: Current graph state; uses ``url``, ``facts``, ``page_type`` and ``page_content``.
        :return: The skill's features and findings.
        """
        if not settings.fingerprints:
            return self.output({}, note="Shop fingerprints are off.")
        page = await shop_store.page_links(dict(state))
        if page is None:
            return self.output({}, note="Not a web shop.")
        linked = await shop_store.store().linked(page.domain, page.links)
        if not linked:
            return self.output({"linked_shops": 0}, note="No other shops share its identifiers.")

        today = shop_store.today()
        kinds = {k for link in linked for k in link.kinds}
        observations: list[Observation] = []
        for kind in sorted(kinds, key=lambda k: (k in shop_store.CONTACT_KINDS, k)):
            shops = [link for link in linked if kind in link.kinds]
            names = ", ".join(link.domain for link in shops[:_SHOWN]) + (" and others" if len(shops) > _SHOWN else "")
            shared = sorted({v for link in shops for v in link.shared if v and kind not in shop_store.CONTACT_KINDS})
            label = KIND_LABELS.get(kind, kind) + (f" {shared[0]}" if len(shared) == 1 else "")
            observations.append(
                Observation(
                    type="shared_identifier",
                    evidence=f"Shares its {label} with {len(shops)} other shop{'s' if len(shops) != 1 else ''}: "
                    f"{names}."[:500],
                )
            )
        young = [link for link in linked if _young(link, today)]
        if young:
            observations.append(
                Observation(
                    type="linked_young_shops",
                    evidence=f"{len(young)} of the linked shops were registered in the last six months: "
                    + ", ".join(link.domain for link in young[:_SHOWN])
                    + ".",
                )
            )
        return self.output(
            {
                "linked_shops": len(linked),
                "linked_young_shops": len(young),
                "shared_payment": bool(kinds & _PAYMENT_KINDS),
                "shared_contact": bool(kinds & shop_store.CONTACT_KINDS),
            },
            observations,
        )
