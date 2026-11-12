from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    analysis_timeout_s: float = 90.0
    # Pages the server fetches per account per minute for requests that send only a URL (the web link checker);
    # a page it can't read is free, so this bounds the free fetches.
    analyze_fetch_per_minute: int = Field(default=10, ge=1)

    # Shopper verdict bands (0–100 reliability score): reliable ≥ this, concerns ≥, careful ≥, else junk_or_scam
    verdict_reliable_min: int = 80
    verdict_concerns_min: int = 50
    verdict_careful_min: int = 25
    # Below this scorer confidence, a page whose only reasons are things missing from it (no imprint, no return
    # policy, …) is shown as "concerns", never "careful" or worse (0 turns this off)
    verdict_absence_only_min_confidence: float = Field(default=0.2, ge=0, le=1)
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

    # Anonymous price observations (unfret.price_store): today's price of every analysed product page with a
    # public product ID, kept 90 days. The "was price never charged" check needs this many days of observations.
    price_observations: bool = True
    price_history_min_days: int = Field(default=30, ge=1)
    # Shop fingerprints (unfret.shop_store): facts about web shops seen by analyses, to link shops of one operator.
    # Contacts are stored as keyed hashes with this key (derived from the session secret when empty; without
    # either, contacts aren't stored).
    fingerprints: bool = True
    # Unfret's own image index (unfret.image_index): the embedding model file and its CPU threads; the photo
    # check is off while the file is missing (`python -m unfret.image_index <path>` downloads it).
    image_model_path: str = "models/dinov2-small-int8.onnx"
    image_threads: int = Field(default=1, ge=1, le=8)
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
    # Credits are sold in packs of this many checks. With crypto a pack costs checks × price (plus VAT where it
    # applies); by card (the Merchant of Record) it costs that plus the card costs, rounded up (packs.card_price).
    packs: list[int] = [25, 50, 100]
    card_fee_pct: Decimal = Field(default=Decimal("6.5"), ge=0, lt=50)  # the provider's cut, worst case (non-US card)
    card_fee_flat_usd: Decimal = Field(default=Decimal("0.50"), ge=0)
    card_price_step_usd: Decimal = Field(default=Decimal("0.50"), gt=0)  # card prices are rounded up to this
    # Invites (unfret.credits.invites): every credited pack comes with one invite per invite_per_checks checks bought
    # (25 → 1, 50 → 2, 100 → 4), each worth invite_checks checks to an account that never had any credit. 0 turns
    # issuing off; invites already issued stay redeemable.
    invite_per_checks: int = Field(default=25, ge=0)
    invite_checks: int = Field(default=3, gt=0)
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
    # seconds at 18 bits) and is limited per visitor network (IPv6 /64) and in total per day; recovery attempts too.
    key_pow_bits: int = 18
    key_pow_ttl_s: int = 600
    key_create_per_ip_per_day: int = 5
    key_create_daily_cap: int = 500
    key_recover_per_ip_per_day: int = 10
    # Fiat top-ups for API keys through a Merchant of Record (Polar). Prices in USD; webhooks are Standard Webhooks,
    # signed with this whsec_ secret and refused outside the replay window.
    fiat_webhook_secret: SecretStr = SecretStr("")
    # Polar (unfret.credits.polar): on only when the access token, a product for every pack and the webhook secret
    # are all set. polar_products maps pack sizes to Polar product ids: "25=<uuid>,50=<uuid>,100=<uuid>".
    polar_access_token: SecretStr = SecretStr("")
    polar_environment: Literal["sandbox", "production"] = "sandbox"
    polar_products: str = ""
    polar_timeout_s: float = 15.0
    fiat_webhook_tolerance_s: int = 300
    fiat_retention_days: int = 180

    # Reports of wrong results ("this is wrong", POST /v1/reports). A report can be made within report_window_s of
    # the check. It pays report_reward_checks to the first reporter when the human label shows the score was at
    # least report_reward_gap points on the wrong side of the label's zone (legit >= 70, junk/scam <= 40).
    reports_per_month: int = 5
    report_queue_cap: int = 200
    report_window_s: int = 7 * 24 * 3600
    report_expiry_days: int = 30
    report_known_days: int = 90
    report_reward_checks: int = 3
    report_reward_gap: int = 30
    report_training_gap: int = 10

    # Shared results ("Share this result", POST /v1/shares). A result's share_token can be used for
    # share_token_ttl_s after the check; a shared result stays up share_days. At most shares_per_visitor_per_day per
    # visitor network (in memory) and shares_daily_cap in total per UTC day.
    share_token_ttl_s: int = 7 * 24 * 3600
    share_days: int = 90
    shares_per_visitor_per_day: int = 20
    shares_daily_cap: int = 1000

    # x402 pay-per-check (POST /v1/check). Off (503) until both the hot wallet key and the cold address are set.
    public_base_url: str = "https://unfret.net"
    # Where /extension sends people to install the extension.
    chrome_store_url: str = "https://chromewebstore.google.com/detail/unfret/giiengbpechhnphignbicmlcbklmcpjo"
    solana_network: Literal["solana-devnet", "solana-mainnet"] = "solana-devnet"
    solana_rpc_url: str = "https://api.devnet.solana.com"
    # Mainnet stays refused until this is set deliberately.
    x402_allow_mainnet: bool = False
    # The hot wallet receives payments (its wSOL account) and pays their fees; base58 secret or a Solana CLI JSON
    # byte array. The only transfers it ever originates are sweeps to the cold address.
    hot_wallet_key: SecretStr = SecretStr("")
    # The cold wallet (e.g. a Ledger): sweep destination only. The server never holds its key.
    cold_address: str = ""
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
        if not 0 <= self.report_training_gap <= self.report_reward_gap:
            problems.append("need 0 <= report_training_gap <= report_reward_gap")
        try:
            unknown = sorted(set(self.polar_product_ids) - set(self.packs))
        except ValueError as exc:
            problems.append(str(exc))
        else:
            if unknown:
                problems.append(f"polar_products names packs that aren't on sale: {unknown}")
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
            "sign_in": bool(self.session_secret.get_secret_value()),
            "admin_token": bool(self.admin_token.get_secret_value()),
            "fiat": self.polar_environment if self.polar_configured else "off",
            "prices_usd_micros": {
                "analysis": self.analysis_price_usd_micros,
                "quick": self.x402_price_quick_usd_micros,
                "deep": self.x402_price_deep_usd_micros,
            },
            "quote_tolerance_bps": self.quote_tolerance_bps,
            "pyth_account": self.pyth_sol_usd_account,
        }

    @property
    def polar_product_ids(self) -> dict[int, str]:
        """
        The Polar product of each pack, from :attr:`polar_products` (``"25=<uuid>,50=<uuid>"``).

        :return: Pack size → product id.
        :raises ValueError: If the setting is malformed.
        """
        products: dict[int, str] = {}
        for item in filter(None, (part.strip() for part in self.polar_products.split(","))):
            size, sep, product = (piece.strip() for piece in item.partition("="))
            if not sep or not size.isdigit() or not product or int(size) in products:
                raise ValueError("polar_products must read like 25=<product id>,50=<product id>")
            products[int(size)] = product
        return products

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
    def analysis_price_lamports(self) -> int:
        """
        Deprecated alias of :attr:`analysis_price_usd_micros`: the analysis price in balance units (micro-dollars).

        Kept so code that still says "lamports" for the balance keeps debiting the right amount until it's renamed.

        :return: Micro-dollars.
        """
        return self.analysis_price_usd_micros


settings = Settings()
