"""
x402 on Base for the agent side: USDC paid with an EIP-3009 ``transferWithAuthorization`` (the ``exact`` scheme).

The agent signs an authorization for exactly the offered amount to Unfret's ``payTo``; Unfret's facilitator submits
it and pays the gas, so the agent needs USDC on Base and no ETH. Only Circle's USDC on the configured network is
paid, signed with the EIP-712 domain this module knows for that contract (never one taken from the offer). The
private key stays inside :class:`BaseWallet`: it is never logged, printed or put in an error message.
"""

import secrets
import time
from typing import Any, Literal

from eth_account import Account
from eth_account.signers.local import LocalAccount

BaseNetwork = Literal["base-sepolia", "base-mainnet"]

CAIP2: dict[BaseNetwork, str] = {"base-sepolia": "eip155:84532", "base-mainnet": "eip155:8453"}
CHAIN_IDS: dict[BaseNetwork, int] = {"base-sepolia": 84532, "base-mainnet": 8453}
# Circle's USDC on Base and its EIP-712 domain (name() and version() as each contract answers them).
USDC: dict[BaseNetwork, str] = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base-mainnet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}
USDC_DOMAIN: dict[BaseNetwork, tuple[str, str]] = {"base-sepolia": ("USDC", "2"), "base-mainnet": ("USD Coin", "2")}
MAINNET = CAIP2["base-mainnet"]

_VALID_AFTER_SLACK_S = 600  # validAfter this far back, so a payer clock running ahead is still accepted
_MAX_VALIDITY_S = 600  # never sign an authorization valid for longer than this, whatever the offer says
_DEFAULT_VALIDITY_S = 300

_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


class BaseKeyError(RuntimeError):
    """``UNFRET_MCP_BASE_KEY`` is not a usable private key (the message never contains the key)."""


class BaseWallet:
    """The agent's EVM key for one Base network. Its ``repr`` shows only the address."""

    def __init__(self, key: str, network: BaseNetwork) -> None:
        """
        Load the key.

        :param key: The private key, 64 hex digits with or without ``0x``.
        :param network: The only Base network this wallet pays on.
        :raises BaseKeyError: If the key isn't a valid private key or the network is unknown.
        """
        if network not in CAIP2:
            raise BaseKeyError(f"unknown Base network {network!r}")
        text = key.strip()
        try:
            account: LocalAccount = Account.from_key(text if text.startswith("0x") else "0x" + text)
        except Exception:  # noqa: BLE001 - the library's message may echo the key
            raise BaseKeyError("UNFRET_MCP_BASE_KEY is not an EVM private key (64 hex digits)") from None
        self._account = account
        self.network: BaseNetwork = network
        self.address: str = account.address

    def __repr__(self) -> str:
        """
        Describe the wallet without its key.

        :return: The address and network.
        """
        return f"BaseWallet({self.address}, {self.network})"

    @property
    def caip2(self) -> str:
        """
        The CAIP-2 id of this wallet's network.

        :return: ``eip155:84532`` or ``eip155:8453``.
        """
        return CAIP2[self.network]

    @property
    def usdc(self) -> str:
        """
        Circle's USDC contract on this wallet's network.

        :return: The address.
        """
        return USDC[self.network]

    def sign(self, requirement: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """
        Sign a ``transferWithAuthorization`` of exactly the requirement's amount to its ``payTo``.

        :param requirement: A requirement from :func:`choose_base_requirement`.
        :param now: The current unix time (tests).
        :return: The x402 ``payload``: ``{"signature", "authorization"}`` (numbers as decimal strings).
        """
        clock = int(time.time() if now is None else now)
        validity = min(int(requirement.get("maxTimeoutSeconds") or _DEFAULT_VALIDITY_S), _MAX_VALIDITY_S)
        message: dict[str, Any] = {
            "from": self.address,
            "to": requirement["payTo"],
            "value": int(requirement["amount"]),
            "validAfter": max(0, clock - _VALID_AFTER_SLACK_S),
            "validBefore": clock + validity,
            "nonce": "0x" + secrets.token_hex(32),
        }
        name, version = USDC_DOMAIN[self.network]
        typed = {
            "types": _TYPES,
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": name,
                "version": version,
                "chainId": CHAIN_IDS[self.network],
                "verifyingContract": self.usdc,
            },
            "message": message,
        }
        signature = self._account.sign_typed_data(full_message=typed).signature.hex()
        authorization = {k: str(v) if k in {"value", "validAfter", "validBefore"} else v for k, v in message.items()}
        return {
            "signature": signature if signature.startswith("0x") else "0x" + signature,
            "authorization": authorization,
        }


def _is_address(value: Any) -> bool:
    text = str(value)
    return len(text) == 42 and text.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in text[2:])


def offered_base_networks(payment_required: dict[str, Any]) -> set[str]:
    """
    The Base networks (CAIP-2) a 402 offers.

    :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
    :return: The ``eip155:`` networks among its ``accepts``.
    """
    networks = {str(option.get("network")) for option in payment_required.get("accepts", [])}
    return {network for network in networks if network.startswith("eip155:")}


def choose_base_requirement(payment_required: dict[str, Any], wallet: BaseWallet) -> dict[str, Any]:
    """
    Pick the ``exact`` USDC requirement on the wallet's Base network.

    Base mainnet is paid only by a wallet configured for it (``UNFRET_MCP_BASE_NETWORK=base-mainnet``): an offer on
    ``eip155:8453`` is refused otherwise, with the setting to change.

    :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
    :param wallet: The agent's Base wallet.
    :return: The requirement to pay.
    :raises ValueError: If none is acceptable (the reason says why).
    """
    name, version = USDC_DOMAIN[wallet.network]
    for option in payment_required.get("accepts", []):
        extra = option.get("extra") or {}
        if (
            option.get("scheme") == "exact"
            and option.get("network") == wallet.caip2
            and str(option.get("asset", "")).lower() == wallet.usdc.lower()
            and extra.get("assetTransferMethod", "eip3009") == "eip3009"
            and (extra.get("name"), extra.get("version")) in {(name, version), (None, None)}
            and _is_address(option.get("payTo"))
            and str(option.get("amount", "")).isdigit()
        ):
            return dict(option)
    offered = offered_base_networks(payment_required)
    if MAINNET in offered and wallet.caip2 != MAINNET:
        raise ValueError(
            "Unfret offers Base mainnet (real USDC) and this server pays on Base Sepolia only: set "
            "UNFRET_MCP_BASE_NETWORK=base-mainnet to pay real USDC on Base"
        )
    raise ValueError(
        f"no USDC on {wallet.network} offered (Unfret offers {', '.join(sorted(offered)) or 'no Base option'}): check "
        "that UNFRET_MCP_BASE_NETWORK matches UNFRET_API_URL (base-mainnet for unfret.net, base-sepolia for "
        "dev.unfret.net)"
    )
