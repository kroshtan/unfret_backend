"""
The Base hot wallet: receives x402 payments on Base (it is ``payTo``) and sweeps its USDC, as USDC, to cold.

Every use of its key goes through one function, :meth:`BaseHotWallet.sign`, which accepts exactly one thing: a
:class:`BaseSweep` from :func:`build_usdc_sweep`, re-validated field by field here (:func:`check_sweep`): an EIP-1559
transaction on this chain to the USDC contract, sending no ETH, whose data is ``transfer(cold, amount)`` with a
positive amount, within the gas and fee caps. Nothing else, and no other destination. Payments need no signature from
it (the payer signs, the facilitator pays the gas), so its ETH only ever pays for sweeps.

The cold address is a plain public setting, not tied to the key: changing it (a software wallet now, a hardware wallet
later) needs no new key; see ``base_former_cold_addresses`` for the audit's side of such a change.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount

from unfret.x402.evm import TRANSFER_SELECTOR, parse_address, topic_address

_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_FIELDS = frozenset(
    {"type", "chainId", "nonce", "to", "value", "data", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "accessList"}
)
_SWEEP = object()  # only build_usdc_sweep can mint a BaseSweep


def load_hot_key(secret: str) -> LocalAccount:
    """
    Load the Base hot wallet's key: 32 bytes as 64 hex digits, with or without ``0x``.

    :param secret: The private key.
    :return: The account.
    :raises ValueError: If it isn't a valid key (the message never contains any of it).
    """
    secret = secret.strip()
    if not _KEY.match(secret):
        raise ValueError("invalid key")
    try:
        account: LocalAccount = Account.from_key(secret if secret.startswith("0x") else "0x" + secret)
    except Exception:  # noqa: BLE001 - e.g. zero or above the curve order
        # Deliberately unchained and without detail: parsing errors can echo part of the secret.
        raise ValueError("invalid key") from None
    return account


def transfer_data(to: str, amount: int) -> str:
    """
    The calldata of ERC-20 ``transfer(to, amount)``.

    :param to: The recipient.
    :param amount: Base units.
    :return: ``0x``-prefixed hex.
    """
    return TRANSFER_SELECTOR + topic_address(to)[2:] + amount.to_bytes(32, "big").hex()


@dataclass(frozen=True)
class BaseSweep:
    """An unsigned USDC sweep from :func:`build_usdc_sweep`."""

    tx: dict[str, Any]
    amount: int  # USDC base units to cold
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse construction anywhere but :func:`build_usdc_sweep`."""
        if self._token is not _SWEEP:
            raise TypeError("BaseSweep can only be created by build_usdc_sweep")


@dataclass(frozen=True)
class SignedSweep:
    """A signed sweep, ready to broadcast."""

    raw: str  # 0x hex
    hash: str  # 0x hex, lowercase
    nonce: int
    amount: int


def build_usdc_sweep(
    cold: str,
    usdc: str,
    *,
    chain_id: int,
    nonce: int,
    amount: int,
    gas: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> BaseSweep:
    """
    Build one USDC sweep: the hot wallet calls ``USDC.transfer(cold, amount)``, sending no ETH.

    :param cold: The cold address, the only allowed destination.
    :param usdc: The USDC contract.
    :param chain_id: The chain (84532 Base Sepolia, 8453 Base mainnet).
    :param nonce: The hot wallet's next nonce.
    :param amount: USDC base units to send (more than 0).
    :param gas: The gas limit.
    :param max_fee_per_gas: EIP-1559 fee cap, wei per gas.
    :param max_priority_fee_per_gas: EIP-1559 tip, wei per gas.
    :return: The unsigned sweep.
    :raises ValueError: If ``amount`` is not positive.
    """
    if amount <= 0:
        raise ValueError("a USDC sweep needs a positive amount")
    tx = {
        "type": 2,
        "chainId": chain_id,
        "nonce": nonce,
        "to": parse_address(usdc),
        "value": 0,
        "data": transfer_data(cold, amount),
        "gas": gas,
        "maxFeePerGas": max_fee_per_gas,
        "maxPriorityFeePerGas": max_priority_fee_per_gas,
        "accessList": [],
    }
    return BaseSweep(tx=tx, amount=amount, _token=_SWEEP)


def check_sweep(tx: dict[str, Any], cold: str, usdc: str, *, chain_id: int, max_gas: int, max_fee: int) -> int:  # noqa: C901
    """
    Validate a sweep's fields independently of how it was built.

    Allowed: exactly an EIP-1559 (type 2) transaction on ``chain_id`` to the USDC contract, with no ETH value, no
    access list, data ``transfer(cold, amount)`` with ``amount > 0``, a gas limit of at most ``max_gas`` and a fee cap
    of at most ``max_fee`` (tip not above the cap).

    :param tx: The transaction fields.
    :param cold: The cold address.
    :param usdc: The USDC contract.
    :param chain_id: The chain.
    :param max_gas: The largest gas limit allowed.
    :param max_fee: The largest ``maxFeePerGas`` allowed.
    :return: The USDC amount it sends to cold.
    :raises PermissionError: If anything else appears.
    """
    if set(tx) != _FIELDS:
        raise PermissionError("sweep: unexpected fields")
    integers = ("type", "chainId", "nonce", "value", "gas", "maxFeePerGas", "maxPriorityFeePerGas")
    if any(not isinstance(tx[k], int) or isinstance(tx[k], bool) or tx[k] < 0 for k in integers):
        raise PermissionError("sweep: malformed numbers")
    if tx["type"] != 2 or tx["chainId"] != chain_id:
        raise PermissionError("sweep: wrong transaction type or chain")
    if not isinstance(tx["to"], str) or tx["to"].lower() != usdc.lower():
        raise PermissionError("sweep: not a call to the USDC contract")
    if tx["value"] != 0 or tx["accessList"] != []:
        raise PermissionError("sweep: it may not send ETH or carry an access list")
    data = tx["data"]
    prefix = transfer_data(cold, 0)[:-64]
    if not isinstance(data, str) or len(data) != len(prefix) + 64 or data[: len(prefix)].lower() != prefix:
        raise PermissionError("sweep: data must be transfer(cold, amount)")
    try:
        amount = int(data[len(prefix) :], 16)
    except ValueError:
        raise PermissionError("sweep: data must be transfer(cold, amount)") from None
    if amount <= 0:
        raise PermissionError("sweep: nothing to send")
    if not 0 < tx["gas"] <= max_gas:
        raise PermissionError("sweep: gas limit above the cap")
    if not tx["maxPriorityFeePerGas"] <= tx["maxFeePerGas"] <= max_fee:
        raise PermissionError("sweep: fee above the cap")
    return amount


class BaseHotWallet:
    """The Base hot key. It never leaves this class, and :meth:`sign` is the only code path that uses it."""

    def __init__(
        self, account: LocalAccount, cold: str, usdc: str, *, chain_id: int, max_gas: int, max_fee: int
    ) -> None:
        """
        Wrap the hot key.

        :param account: The hot wallet's account (from :func:`load_hot_key`).
        :param cold: The cold address (sweep destination), which must be another address.
        :param usdc: The USDC contract.
        :param chain_id: The chain it signs for.
        :param max_gas: The largest gas limit a sweep may have.
        :param max_fee: The largest ``maxFeePerGas`` a sweep may offer.
        :raises ValueError: If the hot wallet is the cold address.
        """
        if account.address.lower() == cold.lower():
            raise ValueError("the hot wallet must not be the cold address")
        self._account = account
        self.address: str = parse_address(account.address)
        self.cold = parse_address(cold)
        self.usdc = parse_address(usdc)
        self.chain_id = chain_id
        self.max_gas = max_gas
        self.max_fee = max_fee

    def __repr__(self) -> str:
        """Show the address only."""
        return f"BaseHotWallet({self.address})"

    def sign(self, item: BaseSweep) -> SignedSweep:
        """
        Sign a USDC sweep to the cold address; nothing else.

        :param item: A sweep from :func:`build_usdc_sweep`.
        :return: The signed transaction.
        :raises PermissionError: If the item is anything else, or fails the re-checks.
        """
        if not isinstance(item, BaseSweep):
            raise PermissionError("refusing to sign: only USDC sweeps to cold")
        amount = check_sweep(
            item.tx, self.cold, self.usdc, chain_id=self.chain_id, max_gas=self.max_gas, max_fee=self.max_fee
        )
        signed = self._account.sign_transaction(item.tx)
        raw = signed.raw_transaction.hex()
        digest = signed.hash.hex()
        return SignedSweep(
            raw=raw if raw.startswith("0x") else "0x" + raw,
            hash=(digest if digest.startswith("0x") else "0x" + digest).lower(),
            nonce=int(item.tx["nonce"]),
            amount=amount,
        )
