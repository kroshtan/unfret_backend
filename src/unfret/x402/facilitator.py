"""
An x402 facilitator over HTTP (``POST /verify``, ``POST /settle``), with Coinbase Developer Platform (CDP) auth.

The facilitator verifies a payer's signed EIP-3009 authorization and submits it on chain, paying the gas: Unfret
holds no key on Base. It can't change the amount or the recipient (both are in what the payer signed), so the worst a
faulty facilitator can do is claim a settlement that didn't happen, which the service checks on chain itself.

CDP authenticates each request with a short-lived JWT (``Authorization: Bearer``) signed by the API key's secret.
Only Ed25519 keys (CDP's default for new secret API keys: the base64 of the 64-byte seed and public key) are
supported; an ECDSA key leaves Base payments off as misconfigured. A facilitator without auth (the public testnet
one at x402.org) needs no key.
"""

import base64
import binascii
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from solders.keypair import Keypair

_JWT_LIFETIME_S = 120
CDP_HOST = "api.cdp.coinbase.com"


class FacilitatorError(RuntimeError):
    """The facilitator couldn't be reached, or answered something other than a verify/settle response."""


class FacilitatorUnreachableError(FacilitatorError):
    """No connection to the facilitator: the request was never sent, so nothing can have been settled."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class CdpAuth:
    """Signs CDP request JWTs with an Ed25519 API key secret."""

    def __init__(self, key_id: str, secret: str) -> None:
        """
        Load the key.

        :param key_id: The API key's id (a UUID, or ``organizations/…/apiKeys/…``).
        :param secret: The base64 secret CDP showed when the key was made (64 bytes: seed, then public key).
        :raises ValueError: If the secret isn't an Ed25519 key in that form.
        """
        try:
            raw = base64.b64decode(secret.strip(), validate=True)
        except binascii.Error, ValueError:
            raise ValueError("CDP API key secret is not base64 (only Ed25519 keys are supported)") from None
        if len(raw) != 64:
            raise ValueError("CDP API key secret is not an Ed25519 key (only Ed25519 keys are supported)")
        keypair = Keypair.from_seed(raw[:32])
        if bytes(keypair.pubkey()) != raw[32:]:
            raise ValueError("CDP API key secret is inconsistent")
        if not key_id.strip():
            raise ValueError("CDP API key id is empty")
        self.key_id = key_id.strip()
        self._keypair = keypair

    def token(self, method: str, host: str, path: str, now: float | None = None) -> str:
        """
        A JWT for one request, valid for two minutes.

        :param method: The HTTP method.
        :param host: The request host.
        :param path: The request path.
        :param now: The current time (tests).
        :return: The compact JWT.
        """
        issued = int(time.time() if now is None else now)
        header = {"alg": "EdDSA", "kid": self.key_id, "typ": "JWT", "nonce": secrets.token_hex(8)}
        claims = {
            "sub": self.key_id,
            "iss": "cdp",
            "nbf": issued,
            "exp": issued + _JWT_LIFETIME_S,
            "uris": [f"{method} {host}{path}"],
        }
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        )
        signature = bytes(self._keypair.sign_message(signing_input.encode()))
        return f"{signing_input}.{_b64url(signature)}"


@dataclass(frozen=True)
class FacilitatorAnswer:
    """What the facilitator said about one payment (verify or settle)."""

    ok: bool
    reason: str | None = None  # invalidReason / errorReason
    payer: str | None = None
    transaction: str = ""
    network: str | None = None
    extension_responses: dict[str, Any] | None = None  # the EXTENSION-RESPONSES sidechannel (bazaar status)


class Facilitator:
    """Client for an x402 v2 facilitator."""

    def __init__(
        self,
        url: str,
        auth: CdpAuth | None = None,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Create a client.

        :param url: The facilitator's base URL (``…/verify`` and ``…/settle`` below it).
        :param auth: CDP auth, or ``None`` for a facilitator without auth.
        :param timeout_s: Request timeout (settle waits for the transaction to confirm).
        :param transport: Optional httpx transport (tests).
        """
        self.url = url.rstrip("/")
        parts = urlsplit(self.url)
        self._host, self._path = parts.netloc, parts.path
        self._auth = auth
        self._timeout = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Close the connection pool (on shutdown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth is not None:
            token = self._auth.token(method, self._host, f"{self._path}/{endpoint}")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _post(self, endpoint: str, payload: dict[str, Any], requirements: dict[str, Any]) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        body = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements}
        try:
            return await self._client.post(
                f"{self.url}/{endpoint}", json=body, headers=self._headers("POST", endpoint)
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise FacilitatorUnreachableError(f"{endpoint}: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise FacilitatorError(f"{endpoint}: {type(exc).__name__}") from exc

    @staticmethod
    def _decode(endpoint: str, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise FacilitatorError(f"{endpoint}: HTTP {response.status_code}, not JSON")
        return body

    @staticmethod
    def _extension_responses(response: httpx.Response) -> dict[str, Any] | None:
        raw = response.headers.get("EXTENSION-RESPONSES")
        if not raw:
            return None
        try:
            decoded = json.loads(base64.b64decode(raw))
        except binascii.Error, ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None

    async def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> FacilitatorAnswer:
        """
        Ask whether a payment is valid, without moving anything.

        :param payload: The client's ``PaymentPayload``.
        :param requirements: The ``PaymentRequirements`` it answers.
        :return: ``ok`` and, if not, the facilitator's ``invalidReason``.
        :raises FacilitatorError: If there is no verify response (unreachable, an auth failure, a server error).
        """
        response = await self._post("verify", payload, requirements)
        body = self._decode("verify", response)
        if not isinstance(body.get("isValid"), bool):
            raise FacilitatorError(f"verify: HTTP {response.status_code}, {str(body.get('errorMessage', ''))[:120]}")
        return FacilitatorAnswer(
            ok=body["isValid"],
            reason=None if body["isValid"] else str(body.get("invalidReason") or "invalid_payment"),
            payer=body.get("payer") if isinstance(body.get("payer"), str) else None,
            extension_responses=self._extension_responses(response),
        )

    async def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> FacilitatorAnswer:
        """
        Submit a payment on chain.

        :param payload: The client's ``PaymentPayload`` (with Unfret's resource and discovery metadata).
        :param requirements: The ``PaymentRequirements`` it answers.
        :return: ``ok``, the transaction hash, and, if not ok, the ``errorReason`` (``settlement_pending`` means it
            was broadcast and may still land).
        :raises FacilitatorError: If there is no settle response (the payment's outcome is then unknown).
        """
        response = await self._post("settle", payload, requirements)
        body = self._decode("settle", response)
        if not isinstance(body.get("success"), bool):
            raise FacilitatorError(f"settle: HTTP {response.status_code}, {str(body.get('errorMessage', ''))[:120]}")
        transaction = body.get("transaction")
        return FacilitatorAnswer(
            ok=body["success"],
            reason=None if body["success"] else str(body.get("errorReason") or "settlement_failed"),
            payer=body.get("payer") if isinstance(body.get("payer"), str) else None,
            transaction=transaction if isinstance(transaction, str) else "",
            network=body.get("network") if isinstance(body.get("network"), str) else None,
            extension_responses=self._extension_responses(response),
        )


def facilitator_auth(url: str, key_id: str, secret: str) -> CdpAuth | None:
    """
    The auth a facilitator URL needs: CDP's JWT for Coinbase's facilitator, none for others.

    :param url: The facilitator's base URL.
    :param key_id: The CDP API key id (may be empty).
    :param secret: The CDP API key secret (may be empty).
    :return: The auth, or ``None`` for a facilitator other than CDP.
    :raises ValueError: If CDP is used without a (valid) key.
    """
    if urlsplit(url).netloc != CDP_HOST:
        return None
    if not key_id or not secret:
        raise ValueError("the CDP facilitator needs cdp_api_key_id and cdp_api_key_secret")
    return CdpAuth(key_id, secret)
