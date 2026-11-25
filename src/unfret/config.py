import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from solders.pubkey import Pubkey

from unfret.x402.evm import parse_address


def _pack_prices(value: str) -> dict[int, int]:
    """
    Pack prices as set: ``"25=6.00,100=20.00"`` → ``{25: 600, 100: 2000}``.

    :param value: The setting.
    :return: Pack size → cents; empty for an empty setting.
    :raises ValueError: If it is malformed (a size twice, an amount that isn't positive with at most cents).
    """
    prices: dict[int, int] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        size, sep, amount = (piece.strip() for piece in item.partition("="))
        whole = re.fullmatch(r"\d{1,6}(\.\d{1,2})?", amount)
        cents = int(Decimal(amount) * 100) if whole else 0
        if not sep or not size.isdigit() or int(size) in prices or cents <= 0:
            raise ValueError(f"malformed pack prices: {value!r}")
        prices[int(size)] = cents
    return prices


class Settings(BaseSettings):
    """
    Service configuration, read from ``UNFRET_``-prefixed environment variables or a git-ignored ``.env``.

    Secrets default to empty so the app can be imported (e.g. in tests) without them; each feature that needs
    one fails closed while it is unset.
    """

    model_config = SettingsConfigDict(env_prefix="UNFRET_", env_file=".env", extra="ignore")

    # LLM: any OpenAI-compatible chat completions API (Scaleway Generative APIs by default)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.scaleway.ai/v1"
    llm_model: str = "mistral-small-3.2-24b-instruct-2506"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 1
    # The judge's LLM writes the shopper headline; off (evaluation runs) → a template per verdict band.
    llm_explanations: bool = True
    # Embeddings for review/title similarity, from the same OpenAI-compatible provider.
    embedding_model: str = "bge-multilingual-gemma2"
    # At most this many LLM/embedding calls in flight per process; a rate-limited (429) call is retried after a
    # jittered exponential backoff (base seconds), up to llm_max_retries times (as are dropped connections, 5xx).
    llm_max_concurrency: int = Field(default=8, ge=1)
    llm_backoff_base_s: float = Field(default=1.0, ge=0)

    # Analysis limits
    max_page_bytes: int = 2 * 1024 * 1024
    skill_timeout_s: float = 45.0
    # One attempt of the content read (the page text's LLM call). It normally answers in 2-14 s, but now and then the
    # model gets lost in its JSON and writes whitespace until it is cut off; the attempt is abandoned after this and
    # tried once more (llm_max_retries), so two attempts and the backoff between them fit within skill_timeout_s
    # instead of one runaway using it all up and degrading the assessment.
    content_attempt_timeout_s: float = Field(default=20.0, gt=0)
    # One attempt of every other LLM call: the classification (one or two calls), the triage's question, the headline,
    # the LLM judge (without a scorer) and the review embeddings. Each asks for a short answer (a page type, a yes/no,
    # one sentence) and normally takes a few seconds (the headline 4-8 s on dev); a runaway attempt is abandoned and
    # tried once more, so two attempts and the backoff fit within skill_timeout_s, and within analysis_timeout_s
    # alongside a skill round that runs to its own limit.
    llm_attempt_timeout_s: float = Field(default=15.0, gt=0)
    analysis_timeout_s: float = 90.0
    # The claim audit (unfret.graph.skills.claim_audit), deep checks only: every checkable claim on a product page
    # (specs, counts, materials, certifications, origin, reference prices) and the ones that clearly don't add up, read
    # by a stronger model than llm_model from the same OpenAI-compatible provider. Off: the skill reports nothing and
    # costs nothing. A problem the model is less sure of (0-1) than claim_audit_min_confidence is dropped. One attempt
    # may take claim_audit_attempt_timeout_s and is tried once more (llm_max_retries) within skill_timeout_s. The
    # prices (euros per million tokens, the provider's list prices for the model) are only for the cost it logs.
    # The level judge (unfret.level_judge): the claim audit's model sets the overall risk level of a deep check from
    # the page, the findings, the scorer's risk and a few labelled examples (a private JSON-lines file, mounted like
    # the scorer). Off: the level is the band translated (unfret.levels.from_band). Any failure keeps that.
    level_judge: bool = False
    level_judge_examples_path: str | None = None
    claim_audit: bool = True
    claim_audit_model: str = "glm-5.3"
    claim_audit_min_confidence: float = Field(default=0.85, ge=0, le=1)
    claim_audit_attempt_timeout_s: float = Field(default=20.0, gt=0)
    claim_audit_max_output_tokens: int = Field(default=3_000, ge=256)
    # Sent as the OpenAI "reasoning_effort" for a model that reasons before it answers: "none" (GLM-5.3 then answers
    # in 1-5 s, as well on the measured pages as with its reasoning); "": the model's default.
    claim_audit_reasoning_effort: str = "none"
    # The variant check (unfret.graph.skills.variant_check), deep checks only: whether a listing's options are one
    # product or different products grouped as its sizes, and whether the price shown is a different item's. One call
    # to the claim audit's model, only for pages whose options aren't plainly sizes, amounts and colours; a grouping
    # the model is less sure of than variant_check_min_confidence is dropped. Off: it reports nothing, costs nothing.
    variant_check: bool = True
    variant_check_min_confidence: float = Field(default=0.85, ge=0, le=1)
    claim_audit_input_eur_per_mtok: Decimal = Field(default=Decimal("1.80"), ge=0)
    claim_audit_output_eur_per_mtok: Decimal = Field(default=Decimal("5.50"), ge=0)
    # On a web shop's own site, the claim audit also reads the shop's returns, shipping, about and contact pages (at
    # most four, unfret.shop_pages) and holds the claims against them; off reads the product page only.
    claim_audit_shop_pages: bool = True
    # Pages the server fetches per account per minute for requests that send only a URL (the web link checker);
    # a page it can't read is free, so this bounds the free fetches.
    analyze_fetch_per_minute: int = Field(default=10, ge=1)
    # Analyses per account per hour that end without a charge although the analysis ran: refunded because the client
    # hung up, or because the page couldn't be assessed (score -1). Each still costs LLM calls; past this, the
    # account's next analyses answer 429 until the hour has passed.
    analyze_free_outcomes_per_hour: int = Field(default=30, ge=1)
    # Attempts (including the first) for graph nodes that call the LLM or fetch outside data, when they fail with a
    # transient error (dropped connection, provider 5xx, rate limit): LangGraph's RetryPolicy. 1 turns retries off.
    graph_retry_attempts: int = Field(default=2, ge=1, le=5)

    # The page classes beyond product pages: peer-to-peer listings (Marktplaats, Vinted, eBay private sellers),
    # service providers on a platform (Werkspot, Thumbtack) and service companies' own sites. Off: classification
    # behaves exactly as before, and those pages are treated as they always were (as a product page, or not analysed).
    # Dev and production turn it on (config/deploy.*.yml).
    expansion_classes: bool = False

    # Shopper verdict bands (0–100 reliability score): reliable ≥ this, concerns ≥, careful ≥, else junk_or_scam
    verdict_reliable_min: int = 80
    verdict_concerns_min: int = 50
    verdict_careful_min: int = 25
    # Below this scorer confidence, a page whose only reasons are things missing from it (no imprint, no return
    # policy, …) is shown as "concerns", never "careful" or worse (0 turns this off)
    verdict_absence_only_min_confidence: float = Field(default=0.2, ge=0, le=1)
    # "Serious concerns" (junk_or_scam) only when the scorer is at least this sure (its ``confidence``, 0–1); below it
    # a page scored as junk_or_scam is shown as "Think twice" (careful), still a warning. One per scorer, since their
    # confidences differ: the deep tier's and the quick tier's (model-quick.json). 0 turns the rule off. Set where
    # "Serious concerns" is right more often, not less (corpus replay with URLs, all 1,582 product pages: deep 0.937
    # → 0.947, legit pages called "Serious concerns" 25 → 15; quick, its scorer on the same pages' features, 0.717 →
    # 0.950, 26 → 1). See shopper.build.
    verdict_serious_min_confidence: float = Field(default=0.6, ge=0, le=1)
    verdict_serious_min_confidence_quick: float = Field(default=0.7, ge=0, le=1)
    # An LLM judgement (0–1) this strong is shown to the shopper as a reason, whatever its model weight
    verdict_llm_signal_min: float = 0.6
    # Tell the content read which policy pages a shop's page links to (returns, contact, …): its text has the footer
    # stripped. Off until measured: it changes the prompt (every recorded eval response for those pages misses) and
    # the meaning of content.missing_trust_info, which the shipped scorer was trained on without it.
    content_policy_links: bool = False

    # Outbound lookups about the shop (RDAP, certificate transparency, EU VIES). Off in offline evaluation, where
    # today's registry data says nothing about a page captured years ago.
    network_lookups: bool = True
    rdap_bootstrap_url: str = "https://rdap.org/domain/"
    crtsh_url: str = "https://crt.sh/"
    vies_url: str = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
    # The deep check's look at a shop's earlier copies in the web archives (unfret.graph.skills.domain_history):
    # only when its address is old and the page already looks doubtful. Off: the triage never plans it, and no
    # archive is asked for an address's first copy either.
    domain_history: bool = True
    # Common Crawl first (unfret.graph.skills.common_crawl): its per-crawl index and its WARC files. Its servers are
    # shared by everyone: at most this many requests a minute per process (index and data together; a look takes up
    # to seven), and none for this long after a 403, 429 or 503 ("slow down").
    commoncrawl_index_url: str = "https://index.commoncrawl.org"
    commoncrawl_data_url: str = "https://data.commoncrawl.org"
    commoncrawl_per_minute: int = Field(default=20, ge=1)
    commoncrawl_pause_s: float = Field(default=600.0, ge=0)
    # The Internet Archive only when Common Crawl has no usable copy. It blocks clients that ask too often: at most
    # this many requests a minute and a UTC day per process (a look takes up to four; 0 a day: never asked), and none
    # for this long after it refused a connection or said "too many requests".
    wayback_url: str = "https://web.archive.org"
    wayback_per_minute: int = Field(default=3, ge=1)
    wayback_daily_cap: int = Field(default=100, ge=0)
    wayback_pause_s: float = Field(default=900.0, ge=0)

    # Anonymous price observations (unfret.price_store): today's price of every analysed product page with a
    # public product ID, kept 90 days. The "was price never charged" check needs this many days of observations.
    price_observations: bool = True
    price_history_min_days: int = Field(default=30, ge=1)
    # Shop fingerprints (unfret.shop_store): facts about web shops seen by analyses, to link shops of one operator.
    # Contacts are stored as keyed hashes with this key (derived from the session secret when empty; without
    # either, contacts aren't stored).
    fingerprints: bool = True
    # Guardrails for those stores (unfret.observations). A photo, price or review fingerprint seen only in content
    # a client supplied, or only by quick checks of such content, counts in a verdict once observation_min_sources
    # independent sources (a pass and a network; counted in memory, per day, as salted hashes: nothing links a pass
    # to a page) reported the same; one from a page Unfret fetched itself counts at once. Each visitor network
    # records from at most observation_pages_per_network_per_day quick checks or client-supplied pages a day (in
    # memory); past it a check answers as always and records nothing. After a quick check has answered, the page's
    # photo, price and reviews are recorded in the background, at most quick_record_queue pages at a time (more are
    # skipped), with the photo check's own size and time limits.
    observation_min_sources: int = Field(default=3, ge=1, le=1000)
    observation_pages_per_network_per_day: int = Field(default=300, ge=0)
    quick_record_queue: int = Field(default=16, ge=0, le=1000)
    # Unfret's own image index (unfret.image_index): the embedding model file and its CPU threads; the photo
    # check is off while the file is missing (`python -m unfret.image_index <path>` downloads it).
    image_model_path: str = "models/dinov2-small-int8.onnx"
    # The feature scorer (unfret.scoring): a trained artifact of your own instead of the open baseline this
    # repository ships (src/unfret/scoring/model.json, model-quick.json). Empty: the baseline. A path that is set but
    # doesn't hold a valid artifact stops the service at start-up rather than score with something else.
    model_path: str = ""
    model_quick_path: str = ""
    image_threads: int = Field(default=1, ge=1, le=8)
    # The photo check against the listing's own claims (unfret.graph.skills.photo_claims), deep checks only: when the
    # title or product data states a piece count ("52 pcs") or a size ("30 x 40 cm"), a vision-capable model from the
    # same OpenAI-compatible provider is asked plain questions about the first few product photos (how many of the
    # main items are visible, is there a person or object for scale). Off: the skill reports nothing and costs
    # nothing. The model is Scaleway's Mistral Small 3.2 by default (it reads images; the same model as llm_model),
    # one call per photo (plus one contact sheet to choose them from a bigger gallery), each photo downscaled to
    # photo_claims_image_px on its longest side. The prices (euros per million tokens) are only for the cost the skill
    # logs per check.
    photo_claims: bool = True
    photo_claims_model: str = "mistral-small-3.2-24b-instruct-2506"
    photo_claims_max_images: int = Field(default=3, ge=1, le=5)
    photo_claims_image_px: int = Field(default=768, ge=256, le=1536)
    # An answer the model gives with less confidence (0–1) than this is dropped.
    photo_claims_min_confidence: float = Field(default=0.8, ge=0, le=1)
    photo_claims_input_eur_per_mtok: Decimal = Field(default=Decimal("0.15"), ge=0)
    photo_claims_output_eur_per_mtok: Decimal = Field(default=Decimal("0.35"), ge=0)
    contact_hash_key: SecretStr = SecretStr("")

    # Expensive skill runs the triage may spend on one analysis.
    triage_max_expensive_runs: int = 2

    # Database
    postgres_uri: str = "postgresql://unfret:unfret@localhost:5432/unfret"

    # Credits door: wallets sign in with SIWS and spend a balance of micro-dollars (POST /v1/analyze). Sign-in is
    # off (503) until the session secret is set; buying credits also needs the x402 hot wallet, which receives them.
    session_secret: SecretStr = SecretStr("")
    # Bearer token for the operator endpoints (/v1/admin/…) and the detailed readiness report; off while empty.
    admin_token: SecretStr = SecretStr("")
    siws_domain: str = "unfret.net"
    session_ttl_s: int = 30 * 24 * 3600
    nonce_ttl_s: int = 300
    pairing_ttl_s: int = 300  # phone sign-in pairings (in memory)
    pairing_per_ip: int = 10  # pairings a visitor may start per 10 minutes
    # Prices are canonical in US dollars. ONE setting sets them all: the price of a check, which is what an
    # analysis costs in credits, what an x402 deep check costs, and what each pack is made of (unfret.credits.packs).
    # Balances and amounts are integer micro-dollars (1 USD = 1_000_000 = USDC's base unit). USDC is charged exactly;
    # SOL (wSOL) is converted at quote and payment time from Pyth's SOL/USD price, read on chain through our RPC, and
    # refused while that price is stale or uncertain.
    price_per_check_usd: Decimal = Field(default=Decimal("0.20"), gt=0, decimal_places=6)
    # Credits are sold in packs of this many checks. With crypto a pack costs checks × price (VAT included); by card
    # (the Merchant of Record) it costs its card_prices_usd price, or (a pack not named there) that plus the card
    # costs, rounded up (packs.card_price). Packs taken off sale keep what they credited: a pass keeps its checks.
    packs: list[int] = [25, 100]
    card_fee_pct: Decimal = Field(default=Decimal("6.5"), ge=0, lt=50)  # the provider's cut, worst case (non-US card)
    card_fee_flat_usd: Decimal = Field(default=Decimal("0.50"), ge=0)
    card_price_step_usd: Decimal = Field(default=Decimal("0.50"), gt=0)  # card prices are rounded up to this
    # Card prices in dollars (unfret.credits.packs): "25=6.50,100=21.00". Set by hand, like the euro prices; a pack
    # not named here costs its derived card price. Each Polar product needs a USD price equal to its pack's, or its
    # checkout is refused before anyone pays. card_prices_usd_tax_inclusive (the default): these prices include sales
    # tax/VAT (the provider takes it out of the price); false: tax is added at checkout.
    card_prices_usd: str = "25=6.50,100=21.00"
    card_prices_usd_tax_inclusive: bool = True
    # Card prices in euros, for visitors from the euro area (unfret.credits.packs): "25=6.00,100=20.00", one per
    # pack. Set by hand (not derived from the dollar price, so they stay round); each Polar product needs a EUR price
    # equal to its pack's. A EUR checkout is what shows "iDEAL | Wero" to Dutch buyers. Empty: everyone pays in
    # dollars. card_prices_eur_tax_inclusive (the default): these prices include VAT, the same for every buyer (the
    # provider takes each country's VAT out of it), so the EUR price of each Polar product must be set to "inclusive"
    # tax behaviour, or its checkout is refused; false: VAT is added on top at checkout, like the dollar prices.
    card_prices_eur: str = "25=6.50,100=21.00"
    card_prices_eur_tax_inclusive: bool = True
    # Whether Polar's organisation-wide tax default is "inclusive": a price whose tax_behavior is null (the
    # organisation's default) then counts as inclusive (Polar sets tax behaviour for the whole account).
    polar_org_tax_inclusive: bool = True
    # Free checks (unfret.credits.free). A new pass made with "free": true (the browser extension on install, the
    # link checker on a first visit) comes with free_checks_per_pass deep checks, for at most
    # free_passes_per_ip_per_day new passes per visitor network (in memory, salted hashes); past that the pass is
    # made empty. Every free deep check counts towards one global budget a UTC day: free_daily_budget_eur of
    # estimated cost, free_check_cost_eur per deep check (LLM and embedding tokens, retries included); then "back
    # tomorrow" until 00:00 UTC. A budget of 0 turns free deep checks off. Quick checks (rules only, no LLM: the x402
    # quick tier) cost nothing and are free for everyone, within quick_checks_per_minute per account or network.
    free_checks_per_pass: int = Field(default=5, ge=0)
    free_passes_per_ip_per_day: int = Field(default=2, ge=0)
    # Developer passes ("developer": true on POST /v1/keys, for building against /v1/check, the MCP server or the
    # clients): developer_free_checks free deep checks instead (they count on /v1/check only), for at most
    # developer_passes_per_ip_per_day per visitor network (in memory, salted hashes, like the free passes). Their free
    # checks come out of their own budget a UTC day, developer_free_daily_budget_eur over free_check_cost_eur (200
    # checks by default: ten full developer passes), never the shoppers' free_daily_budget_eur, so a script that
    # keeps making developer passes can't use up the shoppers' free checks. Their proof of work is
    # developer_pow_extra_bits harder than an ordinary pass's (16 times the work at 4).
    developer_free_checks: int = Field(default=20, ge=0)
    developer_passes_per_ip_per_day: int = Field(default=1, ge=0)
    developer_free_daily_budget_eur: Decimal = Field(default=Decimal("1.00"), ge=0, decimal_places=2)
    developer_pow_extra_bits: int = Field(default=4, ge=0, le=8)
    free_daily_budget_eur: Decimal = Field(default=Decimal("5.00"), ge=0, decimal_places=2)
    free_check_cost_eur: Decimal = Field(default=Decimal("0.005"), gt=0, decimal_places=6)
    quick_checks_per_minute: int = Field(default=10, ge=1)
    # VAT on crypto purchases (unfret.credits.vat). "nl_under_threshold": below the EU's €10,000 threshold for
    # cross-border sales, every EU buyer pays Dutch VAT and everyone else none; "per_country": each EU buyer pays
    # their own country's rate (vendored from the EU's TEDB). Card purchases: the Merchant of Record handles VAT.
    vat_mode: Literal["nl_under_threshold", "per_country"] = "nl_under_threshold"
    vat_quote_ttl_s: int = 24 * 3600  # a priced purchase (country, VAT) stays payable this long
    vat_records_retention_days: int = 3653  # sales records for the bookkeeping: ten years
    pyth_sol_usd_account: str = "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"  # sponsored push feed, shard 0
    # Where the oracle is read: empty means our own RPC on mainnet and mainnet's public RPC on devnet (Pyth's devnet
    # copy of the feed updates too rarely to pass the staleness check; the price is the same real SOL/USD price).
    pyth_rpc_url: str = ""
    pyth_max_age_s: float = 60.0
    pyth_max_conf_bps: int = 100  # confidence interval at most 1% of the price
    # How far an x402 wSOL payment may differ from the current quote (SOL moves between 402 and payment). USDC
    # amounts are exact.
    quote_tolerance_bps: int = 200
    # USDC mint; empty picks Circle's mint for the configured network.
    usdc_mint: str = ""

    # USDC received by the hot wallet is swept to the cold address's USDC account as USDC (the product never swaps;
    # converting it is the operator's business), on mainnet and devnet alike.
    usdc_sweep_threshold_units: int = 5_000_000  # sweep once the hot wallet holds this much USDC ($5)
    usdc_sweep_dust_units: int = 500_000  # …or at least this much and nothing was swept for usdc_sweep_max_interval_s
    usdc_sweep_max_interval_s: float = 24 * 3600.0
    credits_scan_interval_s: float = 20.0
    # API keys (they start empty, like wallets). Creating one takes a small proof of work in the browser (a few
    # seconds at 18 bits) and is limited per visitor network (IPv6 /64), as are recovery attempts. Past
    # key_create_daily_cap new keys in a UTC day, each doubling of the day's count adds a bit of work (at most 6).
    key_pow_bits: int = 18
    key_pow_ttl_s: int = 600
    key_create_per_ip_per_day: int = 5
    key_create_daily_cap: int = 500
    key_recover_per_ip_per_day: int = 10
    # Price quotes (POST /v1/credits/purchase-tx, the Solana Pay QR) per visitor network per minute: each is stored
    # for a day and asks the RPC for a blockhash.
    quotes_per_ip_per_minute: int = Field(default=20, ge=1)
    # Fiat top-ups for API keys through a Merchant of Record (Polar). Prices in USD; webhooks are Standard Webhooks,
    # signed with this whsec_ secret and refused outside the replay window.
    fiat_webhook_secret: SecretStr = SecretStr("")
    # Polar (unfret.credits.polar): on only when the access token, a product for every pack and the webhook secret
    # are all set. polar_products maps pack sizes to Polar product ids: "25=<uuid>,100=<uuid>". A product for a pack
    # that is no longer on sale (an old "50=<uuid>") is ignored: no checkout starts for it, and an order for it is
    # left for a person to look at.
    polar_access_token: SecretStr = SecretStr("")
    polar_environment: Literal["sandbox", "production"] = "sandbox"
    polar_products: str = ""
    polar_timeout_s: float = 15.0
    fiat_webhook_tolerance_s: int = 300
    fiat_retention_days: int = 180

    # Reports of wrong results ("this is wrong", POST /v1/reports). A report can be made within report_window_s of
    # the check, reports_per_month per pass, wallet or API key. Reports pay nothing. A labelled page counts as a
    # training sample when its score was at least report_training_gap points on the wrong side of the label's zone
    # (legit >= 70, junk/scam <= 40).
    reports_per_month: int = Field(default=15, ge=1)
    report_window_s: int = 7 * 24 * 3600
    report_expiry_days: int = 30
    report_known_days: int = 90
    report_training_gap: int = 10
    # Open items in the review queue. When it is full, a new report takes the place of the open item worth least
    # (unfret.reports.value: how far the reporter says the score is off, pages and shops not labelled yet,
    # confirmations), if it is worth more; otherwise it is refused (QUEUE_FULL).
    report_queue_cap: int = Field(default=1000, ge=1)
    # The second opinion (unfret.reports.second_opinion): one call to this model per queued report proposes a label
    # for the operator to confirm or overrule; never a label by itself. A different model from llm_model, or it only
    # grades itself. At most report_second_opinions_per_day calls per UTC day; past it, items wait for the next day
    # (or for a person). Empty model: off.
    report_second_opinion_model: str = "qwen3.5-397b-a17b"
    report_second_opinions_per_day: int = Field(default=200, ge=0)
    report_second_opinion_timeout_s: float = Field(default=120.0, gt=0)

    # Shared results ("Share this result", POST /v1/shares). A result's share_token can be used for
    # share_token_ttl_s after the check; a shared result stays up share_days. At most shares_per_visitor_per_day per
    # visitor network (in memory) and shares_daily_cap in total per UTC day.
    share_token_ttl_s: int = 7 * 24 * 3600
    share_days: int = 90
    shares_per_visitor_per_day: int = 20
    shares_daily_cap: int = 1000

    # Labels from the product (unfret.crowd): a result's label token is good for crowd_label_token_ttl_s. Anonymous
    # labels weigh crowd_anonymous_weight and count only where another agrees, at most crowd_anonymous_daily_cap a day
    # in all; a contributor key is on probation (crowd_new_key_weight) until crowd_probation_settled of its labels
    # settled, then weighs its reputation (up crowd_reputation_up per agreement, down crowd_reputation_down per
    # disagreement, at most crowd_max_reputation; under crowd_mute_reputation its labels weigh nothing), with
    # crowd_key_daily_labels a day. A page settles at crowd_settle_weight; a burst over crowd_burst_labels a day that
    # lowers the level is held for a person. Links to contributors go after crowd_link_days, open labels after
    # crowd_open_days.
    crowd_labels: bool = True
    crowd_label_token_ttl_s: int = Field(default=7 * 24 * 3600, ge=60)
    crowd_anonymous_weight: float = Field(default=0.2, ge=0)
    crowd_anonymous_daily_cap: int = Field(default=5_000, ge=0)
    crowd_new_key_weight: float = Field(default=0.5, ge=0)
    crowd_probation_settled: int = Field(default=5, ge=0)
    crowd_key_daily_labels: int = Field(default=50, ge=1)
    crowd_reputation_up: float = Field(default=0.05, ge=0)
    crowd_reputation_down: float = Field(default=0.15, ge=0)
    crowd_max_reputation: float = Field(default=3.0, ge=1)
    crowd_mute_reputation: float = Field(default=0.3, ge=0)
    crowd_settle_weight: float = Field(default=2.0, gt=0)
    crowd_burst_labels: int = Field(default=20, ge=2)
    crowd_link_days: int = Field(default=30, ge=1)
    crowd_open_days: int = Field(default=180, ge=1)

    # Requests from shop owners and sellers (POST /v1/objections, the /objection page): at most
    # objections_per_visitor_per_day per visitor network (in memory) and objections_daily_cap in total per UTC day; an
    # open request is deleted after objection_days at the latest (the operator deletes it once handled).
    objections_per_visitor_per_day: int = Field(default=5, ge=1)
    objections_daily_cap: int = Field(default=200, ge=1)
    objection_days: int = Field(default=90, ge=1)

    # Anonymous usage events (unfret.usage): a time, a kind and coarse enums, nothing that names a person, a pass or a
    # page. Off: nothing is recorded. Deleted after usage_events_retention_days, with the uptime probes.
    usage_events_enabled: bool = True
    usage_events_retention_days: int = Field(default=400, ge=1)

    # /terms shows a visible "draft" note at the top, for a new version of the terms that isn't approved yet.
    terms_draft: bool = False

    # OAuth for the remote MCP server (unfret.oauth, /oauth/*, the /.well-known/oauth-* documents): hosted clients
    # such as Claude's connectors sign in and pick an Unfret pass instead of sending one in a header. Off: /mcp takes
    # passes only, as before. Codes go only to mcp_oauth_redirect_uris (exactly) and, with
    # mcp_oauth_loopback_redirects, to loopback addresses on any port (Claude Code and other local clients). Access
    # tokens live mcp_oauth_access_ttl_s; refresh tokens rotate on use and lapse after mcp_oauth_refresh_ttl_s unused.
    # Dynamic client registrations: at most mcp_oauth_registrations_per_minute per network (Claude registers from
    # Anthropic's servers, so this is generous) and mcp_oauth_registrations_daily_cap a UTC day in all; a registered
    # client with no grant left is deleted after mcp_oauth_client_idle_days.
    mcp_oauth_enabled: bool = False
    mcp_oauth_redirect_uris: list[str] = [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    ]
    mcp_oauth_loopback_redirects: bool = True
    mcp_oauth_access_ttl_s: int = Field(default=3600, ge=60)
    mcp_oauth_refresh_ttl_s: int = Field(default=30 * 24 * 3600, ge=3600)
    mcp_oauth_code_ttl_s: int = Field(default=300, ge=30, le=600)
    mcp_oauth_registrations_per_minute: int = Field(default=60, ge=1)
    mcp_oauth_registrations_daily_cap: int = Field(default=20_000, ge=1)
    mcp_oauth_client_idle_days: int = Field(default=30, ge=1)

    # x402 pay-per-check (POST /v1/check). Off (503) until both the hot wallet key and the cold address are set.
    public_base_url: str = "https://unfret.net"
    # Links the link checker (/check) offers to try, as [label, address] pairs (an address on this site may be a
    # path). The test shop first, then its sister shop and the clean shop: three verdicts, no real shop.
    check_examples: list[tuple[str, str]] = [
        ("The Unfret test shop, with every trick Unfret catches", "/test-shop"),
        ("Its sister shop: the same product under another brand, for less", "/test-shop/sister-shop"),
        ("A made-up shop with none of the tricks, to compare", "/test-shop/clean-shop"),
    ]
    # Where the home page and /extension send people to install the extension.
    chrome_store_url: str = "https://chromewebstore.google.com/detail/unfret/giiengbpechhnphignbicmlcbklmcpjo"
    firefox_store_url: str = "https://addons.mozilla.org/en-US/firefox/addon/unfret/"
    solana_network: Literal["solana-devnet", "solana-mainnet"] = "solana-devnet"
    solana_rpc_url: str = "https://api.devnet.solana.com"
    # Mainnet stays refused until this is set deliberately.
    x402_allow_mainnet: bool = False
    # The hot wallet receives payments (its wSOL account) and pays their fees; base58 secret or a Solana CLI JSON
    # byte array. The only transfers it ever originates are sweeps to the cold address.
    hot_wallet_key: SecretStr = SecretStr("")
    # The cold wallet (e.g. a Ledger): sweep destination only. The server never holds its key.
    cold_address: str = ""
    # Earlier cold addresses (comma-separated), after the cold address changed: the outflow audit accepts sweeps to
    # them (SOL, and USDC into their USDC accounts) made before former_cold_until (and before this process started),
    # so a restart that reads the hot wallet's history doesn't flag the old sweeps. Anything later to them alarms.
    # former_cold_until (ISO 8601, e.g. 2026-11-16T06:00:00Z: when sweeps to them stopped) is required with them.
    former_cold_addresses: Annotated[list[str], NoDecode] = []
    former_cold_until: datetime | None = None
    # Sweep to cold once the hot wallet holds more than this (SOL + wSOL), keeping the reserve for fees.
    sweep_threshold_lamports: int = 100_000_000
    sweep_reserve_lamports: int = 10_000_000
    sweep_interval_s: float = 60.0
    # Payments are refused (503) while the hot wallet's SOL (for fees) is below this. Kept under the reserve, and
    # a sweep refills it from received wSOL, so normal operation never trips it.
    x402_min_fee_lamports: int = 2_000_000
    x402_price_quick_usd_micros: int = 10_000  # $0.01; the deep tier costs price_per_check_usd
    x402_max_compute_units: int = 400_000
    x402_max_compute_unit_price_microlamports: int = 50_000
    x402_confirm_timeout_s: float = 30.0
    x402_analysis_timeout_s: float = 60.0  # agents wait synchronously: a paid check answers within a minute
    x402_check_per_minute: int = 30  # requests to /v1/check per visitor network (IPv6 /64)
    # The priority fee a payment may make the hot wallet pay, at most this share of the tier's price (wSOL quote);
    # the fallback applies while SOL can't be priced.
    x402_max_priority_fee_bps: int = 500
    x402_priority_fee_fallback_lamports: int = 5_000
    # Programs a payment may run besides the built-in wallet programs (base58), e.g. another smart-wallet program
    # once it is known not to depend on the slot.
    x402_extra_programs: list[str] = []
    # Failed settlements (the hot wallet paid a fee and received nothing) per rolling hour before /v1/check refuses
    # payments with 503: per paying wallet, per visitor address and in total.
    x402_breaker_per_payer: int = 3
    x402_breaker_per_ip: int = 5
    x402_breaker_global: int = 20

    # x402 on Base: USDC with the `exact` scheme (EIP-3009), settled by a facilitator that pays the gas, offered
    # next to Solana. Off until x402_base_enabled is set and both the Base hot key and the Base cold address are; Base
    # Sepolia is the default, and Base mainnet needs x402_base_allow_mainnet as well.
    x402_base_enabled: bool = False
    base_network: Literal["base-sepolia", "base-mainnet"] = "base-sepolia"
    x402_base_allow_mainnet: bool = False
    base_rpc_url: str = ""  # empty: the network's public RPC (rate-limited; set a provider's for production)
    # The Base hot wallet's private key (0x and 64 hex digits): its address is payTo, and the only transactions it
    # ever signs are USDC sweeps to the cold address, paying their gas from its own small ETH balance.
    x402_base_hot_key: SecretStr = SecretStr("")
    # Optional: the hot wallet's address, as a check that the right key is deployed (a mismatch leaves Base off,
    # misconfigured). payTo is always the key's own address.
    base_pay_to: str = ""
    # The Base cold wallet: sweep destination only, the server never holds its key. A plain public address, not tied
    # to the hot key: swapping it (software wallet now, hardware wallet later) needs only this setting, plus the old
    # one in base_former_cold_addresses.
    base_cold_address: str = ""
    # Earlier Base cold addresses (comma-separated), after base_cold_address changed: the outflow audit accepts USDC
    # sweeps to them made before base_former_cold_until (and before this process started), as Solana's does.
    base_former_cold_addresses: Annotated[list[str], NoDecode] = []
    base_former_cold_until: datetime | None = None
    # Sweep the hot wallet's USDC to cold once it holds this much ($5), or at least the dust amount ($0.50) when
    # nothing was swept for base_usdc_sweep_max_interval_s.
    base_usdc_sweep_threshold_units: int = Field(default=5_000_000, gt=0)
    base_usdc_sweep_dust_units: int = Field(default=500_000, gt=0)
    base_usdc_sweep_max_interval_s: float = Field(default=24 * 3600.0, gt=0)
    # Gas: alert (fee_reserve_low) while the hot wallet holds less ETH than this (0.00001 ETH: several sweeps on
    # Base); a sweep waits for gas above base_max_fee_per_gas_wei (1 gwei) and never sets a gas limit above
    # base_sweep_max_gas.
    base_min_gas_wei: int = Field(default=10_000_000_000_000, ge=0)
    base_max_fee_per_gas_wei: int = Field(default=1_000_000_000, gt=0)
    base_sweep_max_gas: int = Field(default=120_000, ge=21_000)
    base_usdc_address: str = ""  # override Circle's USDC contract for the network (tests)
    # Coinbase's facilitator (CDP) needs an API key (cdp_api_key_id and an Ed25519 cdp_api_key_secret); another
    # facilitator URL (https://x402.org/facilitator: testnet only, no key) needs none.
    base_facilitator_url: str = "https://api.cdp.coinbase.com/platform/v2/x402"
    cdp_api_key_id: str = ""
    cdp_api_key_secret: SecretStr = SecretStr("")
    # The outflow audit reads the hot wallet's USDC logs up to this many blocks behind the head (reorg margin).
    base_audit_confirmations: int = 5

    log_json: bool = True  # one JSON object per line (False: human-readable, for local development)
    # The commit the image was built from (the Dockerfile's GIT_SHA build argument), reported by /v1/health so a
    # deploy can tell which build is serving. Empty outside a built image.
    git_sha: str = ""

    @field_validator("polar_environment", mode="before")
    @classmethod
    def _polar_environment(cls, value: Any) -> Any:
        """
        Read an empty ``UNFRET_POLAR_ENVIRONMENT`` (an unset deploy secret) as the default, sandbox.

        :param value: The raw value.
        :return: The value, or ``"sandbox"`` if empty.
        """
        return value or "sandbox"

    @field_validator("former_cold_addresses", mode="before")
    @classmethod
    def _former_cold_addresses(cls, value: Any) -> Any:
        """
        Read ``UNFRET_FORMER_COLD_ADDRESSES`` as a comma-separated list, and refuse anything but public keys.

        :param value: The raw value: a string from the environment, or a list.
        :return: The addresses, stripped, without empty entries or duplicates.
        :raises ValueError: If an entry isn't a base58 public key.
        """
        items = value.split(",") if isinstance(value, str) else list(value or [])
        addresses: list[str] = []
        for item in (str(i).strip() for i in items):
            if not item:
                continue
            try:
                Pubkey.from_string(item)
            except ValueError:
                raise ValueError(f"former_cold_addresses: {item[:60]!r} is not a Solana address") from None
            if item not in addresses:
                addresses.append(item)
        return addresses

    @field_validator("base_former_cold_addresses", mode="before")
    @classmethod
    def _base_former_cold_addresses(cls, value: Any) -> Any:
        """
        Read ``UNFRET_BASE_FORMER_COLD_ADDRESSES`` as a comma-separated list, and refuse anything but EVM addresses.

        :param value: The raw value: a string from the environment, or a list.
        :return: The addresses, checksummed, without empty entries or duplicates.
        :raises ValueError: If an entry isn't an EVM address (or its checksum is wrong).
        """
        items = value.split(",") if isinstance(value, str) else list(value or [])
        addresses: list[str] = []
        for item in (str(i).strip() for i in items):
            if not item:
                continue
            try:
                address = parse_address(item)
            except ValueError:
                raise ValueError(f"base_former_cold_addresses: {item[:60]!r} is not an EVM address") from None
            if address not in addresses:
                addresses.append(address)
        return addresses

    @field_validator("former_cold_until", "base_former_cold_until", mode="before")
    @classmethod
    def _former_cold_until_empty(cls, value: Any) -> Any:
        """
        Read an empty ``UNFRET_FORMER_COLD_UNTIL`` as unset.

        :param value: The raw value.
        :return: The value, or ``None`` if empty.
        """
        return value or None

    @field_validator("former_cold_until", "base_former_cold_until", mode="after")
    @classmethod
    def _former_cold_until(cls, value: datetime | None) -> datetime | None:
        """
        Refuse a cut-off without a time zone: it is compared with block times, which are UTC.

        :param value: The parsed cut-off.
        :return: The cut-off, in UTC.
        :raises ValueError: If it has no time zone.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError("former_cold_until needs a time zone, e.g. 2026-11-16T06:00:00Z")
        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """
        Refuse settings that contradict each other, at start-up rather than at the first payment.

        :return: The settings.
        :raises ValueError: Listing every contradiction found.
        """
        problems = []
        prices = (self.analysis_price_usd_micros, self.x402_price_quick_usd_micros, self.x402_price_deep_usd_micros)
        if min(prices) <= 0:
            problems.append("prices must be positive")
        if not self.packs or self.packs != sorted(set(self.packs)) or self.packs[0] <= 0:
            problems.append("packs must be distinct positive check counts, ascending")
        if not 0 <= self.quote_tolerance_bps <= 1_000:
            problems.append("quote_tolerance_bps must be 0-1000 (at most 10%)")
        if not 0 < self.x402_max_priority_fee_bps <= 5_000:
            problems.append("x402_max_priority_fee_bps must be 1-5000")
        if not self.x402_min_fee_lamports <= self.sweep_reserve_lamports < self.sweep_threshold_lamports:
            problems.append("need x402_min_fee_lamports <= sweep_reserve_lamports < sweep_threshold_lamports")
        if self.x402_analysis_timeout_s <= 0 or self.x402_confirm_timeout_s <= 0:
            problems.append("timeouts must be positive")
        if self.former_cold_addresses and self.former_cold_until is None:
            problems.append("former_cold_addresses needs former_cold_until (when sweeps to them stopped)")
        if self.cold_address in self.former_cold_addresses:
            problems.append("the cold address can't also be a former cold address")
        if problems:
            raise ValueError("inconsistent settings: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _base_consistent(self) -> Self:
        """
        Refuse contradictory settings for x402 on Base.

        :return: The settings.
        :raises ValueError: Listing every contradiction found.
        """
        problems = []
        if self.base_former_cold_addresses and self.base_former_cold_until is None:
            problems.append("base_former_cold_addresses needs base_former_cold_until (when sweeps to them stopped)")
        if self.base_cold_address.strip().lower() in {a.lower() for a in self.base_former_cold_addresses}:
            problems.append("the Base cold address can't also be a former cold address")
        if self.base_usdc_sweep_dust_units > self.base_usdc_sweep_threshold_units:
            problems.append("need base_usdc_sweep_dust_units <= base_usdc_sweep_threshold_units")
        if problems:
            raise ValueError("inconsistent settings: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _accounts_consistent(self) -> Self:
        """
        Refuse contradictory API key and report settings.

        :return: The settings.
        :raises ValueError: Listing every contradiction found.
        """
        problems = []
        if not 0 < self.key_pow_bits <= 32:
            problems.append("key_pow_bits must be 1-32")
        if self.report_training_gap <= 0:
            problems.append("need 0 < report_training_gap")
        if problems:
            raise ValueError("inconsistent settings: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _card_prices_consistent(self) -> Self:
        """
        Refuse malformed card prices and Polar products, and prices that leave a pack on sale unpriced.

        :return: The settings.
        :raises ValueError: Listing every contradiction found.
        """
        problems: list[str] = []
        try:
            self.polar_product_ids  # noqa: B018 - parsed for its errors; products of packs off sale are ignored
        except ValueError as exc:
            problems.append(str(exc))
        try:
            eur = self.card_eur_cents
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if eur and not set(self.packs) <= set(eur):
                problems.append("card_prices_eur must price every pack on sale (or be empty: dollars only)")
        try:
            usd = self.card_usd_cents  # a pack it doesn't name gets the derived price
        except ValueError as exc:
            problems.append(str(exc))
        else:
            # A card price under the pack's value would make crypto the dearer way to pay.
            if cheap := sorted(n for n, cents in usd.items() if cents * 10_000 < n * self.analysis_price_usd_micros):
                problems.append(f"card_prices_usd is below the value of the packs {cheap} (checks × price_per_check)")
        if problems:
            raise ValueError("inconsistent settings: " + "; ".join(problems))
        return self

    def summary(self) -> dict[str, Any]:
        """
        What this instance runs with, for the start-up log: no secrets, only whether each is set.

        :return: A flat dict.
        """
        return {
            "network": self.solana_network,
            "x402": bool(self.hot_wallet_key.get_secret_value() and self.cold_address),
            "mainnet_allowed": self.x402_allow_mainnet,
            "x402_base": self.base_network if self.base_configured else "off",
            "base_mainnet_allowed": self.x402_base_allow_mainnet,
            "sign_in": bool(self.session_secret.get_secret_value()),
            "admin_token": bool(self.admin_token.get_secret_value()),
            "fiat": self.polar_environment if self.polar_configured else "off",
            "mcp_oauth": self.mcp_oauth_enabled,
            "usage_events": self.usage_events_enabled,
            "prices_usd_micros": {
                "analysis": self.analysis_price_usd_micros,
                "quick": self.x402_price_quick_usd_micros,
                "deep": self.x402_price_deep_usd_micros,
            },
            "quote_tolerance_bps": self.quote_tolerance_bps,
            "pyth_account": self.pyth_sol_usd_account,
        }

    @property
    def base_configured(self) -> bool:
        """
        Whether x402 on Base is switched on with its hot key and cold address (it may still refuse to run).

        :return: ``True`` if all three are set.
        """
        return bool(
            self.x402_base_enabled and self.x402_base_hot_key.get_secret_value().strip() and self.base_cold_address
        )

    @property
    def polar_product_ids(self) -> dict[int, str]:
        """
        The Polar product of each pack, from :attr:`polar_products` (``"25=<uuid>,100=<uuid>"``).

        :return: Pack size → product id.
        :raises ValueError: If the setting is malformed.
        """
        products: dict[int, str] = {}
        for item in filter(None, (part.strip() for part in self.polar_products.split(","))):
            size, sep, product = (piece.strip() for piece in item.partition("="))
            if not sep or not size.isdigit() or not product or int(size) in products:
                raise ValueError("polar_products must read like 25=<product id>,100=<product id>")
            products[int(size)] = product
        return products

    @property
    def card_eur_cents(self) -> dict[int, int]:
        """
        Each pack's card price in euro cents, from :attr:`card_prices_eur` (``"25=6.00,100=20.00"``).

        :return: Pack size → euro cents; empty when euro prices are off.
        :raises ValueError: If the setting is malformed.
        """
        try:
            return _pack_prices(self.card_prices_eur)
        except ValueError:
            raise ValueError(
                "card_prices_eur must read like 25=6.00,100=20.00 (positive euros, at most cents)"
            ) from None

    @property
    def card_usd_cents(self) -> dict[int, int]:
        """
        The packs' card prices in dollar cents set by hand, from :attr:`card_prices_usd` (``"25=6.00,100=20.00"``).

        :return: Pack size → cents; a pack not named here gets the derived card price.
        :raises ValueError: If the setting is malformed.
        """
        try:
            return _pack_prices(self.card_prices_usd)
        except ValueError:
            raise ValueError(
                "card_prices_usd must read like 25=6.00,100=20.00 (positive dollars, at most cents)"
            ) from None

    @property
    def polar_configured(self) -> bool:
        """
        Whether card payments through Polar are on: the token, the webhook secret and a product for every pack.

        :return: True when all are set.
        """
        return bool(
            self.polar_access_token.get_secret_value()
            and self.fiat_webhook_secret.get_secret_value()
            and set(self.packs) <= set(self.polar_product_ids)
        )

    @property
    def analysis_price_usd_micros(self) -> int:
        """
        The price of one check in micro-dollars: an analysis's cost in credits (from :attr:`price_per_check_usd`).

        :return: Micro-dollars.
        """
        return int(self.price_per_check_usd * 1_000_000)

    @property
    def x402_price_deep_usd_micros(self) -> int:
        """
        The x402 deep tier's price: one check (from :attr:`price_per_check_usd`).

        :return: Micro-dollars.
        """
        return self.analysis_price_usd_micros

    @property
    def free_daily_checks_cap(self) -> int:
        """
        How many free deep checks fit in a UTC day's budget (from :attr:`free_daily_budget_eur`).

        :return: The budget over the estimated cost of one deep check, rounded down.
        """
        return int(self.free_daily_budget_eur // self.free_check_cost_eur)

    @property
    def developer_free_daily_checks_cap(self) -> int:
        """
        How many developer passes' free deep checks fit in a UTC day (from :attr:`developer_free_daily_budget_eur`).

        :return: That budget over the estimated cost of one deep check, rounded down.
        """
        return int(self.developer_free_daily_budget_eur // self.free_check_cost_eur)

    @property
    def analysis_price_lamports(self) -> int:
        """
        Deprecated alias of :attr:`analysis_price_usd_micros`: the analysis price in balance units (micro-dollars).

        Kept so code that still says "lamports" for the balance keeps debiting the right amount until it's renamed.

        :return: Micro-dollars.
        """
        return self.analysis_price_usd_micros


settings = Settings()
