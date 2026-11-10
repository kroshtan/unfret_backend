"""Plain-language explanations of Unfret flag types, for agents to relay to their humans."""

_EXPLANATIONS = {
    "page_content": (
        "The listing's own text shows warning signs, such as an unknown or generic brand, pressure tactics like fake "
        "urgency or countdowns, an implausible discount, missing contact details or no clear return policy."
    ),
    "html_source_signals": (
        "The page's code reveals tools commonly used by dropshipping stores (for example AliExpress order apps or "
        "supplier image servers), which suggests the seller doesn't hold its own stock and ships generic goods."
    ),
    "claims_verification": (
        "The listing makes specific claims (certifications, performance figures, materials) that look implausible "
        "for the product type or price, or that are commonly made up in low-quality listings."
    ),
    "review_integrity": (
        "The reviews look manipulated: many arrived in a short time, they are uniformly five-star, they repeat the "
        "same phrasing, or they don't describe the product actually being sold."
    ),
    "domain_age": (
        "The shop's web address was registered recently or with hidden ownership, which is typical of short-lived "
        "pop-up stores that disappear after taking orders."
    ),
    "exif_check": (
        "The product photos' metadata doesn't match the listing, for example photos older than the product or taken "
        "by a factory or supplier rather than the seller."
    ),
    "reverse_image_search": (
        "The same product photos appear on bulk marketplaces such as AliExpress, Temu or DHgate, so the item is "
        "probably a generic product resold at a mark-up."
    ),
    "not_assessable": (
        "The page doesn't look like a product or shop page, so there was nothing to assess. Ask your human before "
        "buying."
    ),
}


def explain(flag_type: str) -> str:
    """
    Explain a flag type in one paragraph.

    :param flag_type: The flag's ``type``.
    :return: The explanation, or a generic one for unknown types.
    """
    return _EXPLANATIONS.get(
        flag_type,
        f"'{flag_type}' is a factual signal from Unfret's analysis; see its evidence text for the specifics.",
    )
