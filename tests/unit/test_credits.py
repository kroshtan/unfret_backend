"""SIWS messages, nonces and sessions; purchase pricing; the trial rules; reading transfers for the scanner."""

from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from veridex.credits import siws
from veridex.credits.pricing import purchase_transaction, sol_string, solana_pay_url, with_bonus
from veridex.credits.scanner import credit_beneficiary, credits_for, incoming_transfers
from veridex.credits.trial import TrialLimiter, has_history
from veridex.x402.solana import MEMO_PROGRAM

SECRET = "unit-secret"
NOW = 1_800_000_000.0
DOMAIN = "unfret.kroshtan.com"


def signed(keypair: Keypair, **overrides: str) -> tuple[bytes, bytes]:
    nonce, expires = siws.issue_nonce(SECRET, 300, NOW)
    fields = {
        "domain": DOMAIN,
        "address": str(keypair.pubkey()),
        "statement": siws.STATEMENT,
        "uri": "https://unfret.kroshtan.com",
        "version": "1",
        "nonce": nonce,
        "issued_at": "2027-01-15T08:00:00Z",
        "expiration_time": "2027-01-15T08:05:00Z",
    } | overrides
    text = siws.message_text(siws.SignInFields(**fields)).encode()
    return text, bytes(keypair.sign_message(text))


def verify(keypair: Keypair, message: bytes, signature: bytes, now: float = NOW) -> str:
    return siws.verify_sign_in(
        SECRET, domain=DOMAIN, address=str(keypair.pubkey()), signed_message=message, signature=signature, now=now
    )


def test_message_has_the_wallet_standard_layout() -> None:
    message, _ = signed(Keypair())
    lines = message.decode().split("\n")
    assert lines[0] == f"{DOMAIN} wants you to sign in with your Solana account:"
    assert lines[3] == siws.STATEMENT
    assert lines[5].startswith("URI: ") and lines[-1].startswith("Expiration Time: ")


def test_a_valid_sign_in_returns_the_address() -> None:
    keypair = Keypair()
    assert verify(keypair, *signed(keypair)) == str(keypair.pubkey())


def test_wallet_added_optional_fields_are_tolerated() -> None:
    keypair = Keypair()
    message, _ = signed(keypair)
    extended = message + b"\nRequest ID: 42\nResources:\n- https://unfret.kroshtan.com"
    assert verify(keypair, extended, bytes(keypair.sign_message(extended))) == str(keypair.pubkey())


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"domain": "evil.example"}, "another site"),
        ({"address": str(Keypair().pubkey())}, "another wallet"),
        ({"nonce": "0" * 58}, "Unknown nonce"),
        ({"expiration_time": "2020-01-01T00:00:00Z"}, "expired"),
        ({"expiration_time": "soon"}, "Malformed time"),
    ],
)
def test_bad_messages_are_refused(overrides: dict[str, str], error: str) -> None:
    keypair = Keypair()
    with pytest.raises(siws.SignInError, match=error):
        verify(keypair, *signed(keypair, **overrides))


def test_a_signature_from_another_key_is_refused() -> None:
    keypair, other = Keypair(), Keypair()
    message, _ = signed(keypair)
    with pytest.raises(siws.SignInError, match="does not match"):
        verify(keypair, message, bytes(other.sign_message(message)))


def test_a_tampered_message_is_refused() -> None:
    keypair = Keypair()
    message, signature = signed(keypair)
    with pytest.raises(siws.SignInError, match="does not match"):
        verify(keypair, message.replace(b"Version: 1", b"Version: 2"), signature)


def test_not_a_sign_in_message() -> None:
    keypair = Keypair()
    message = b"Transfer all my SOL"
    with pytest.raises(siws.SignInError, match="Not a sign-in message"):
        verify(keypair, message, bytes(keypair.sign_message(message)))


def test_nonce_expires_and_resists_tampering() -> None:
    nonce, _ = siws.issue_nonce(SECRET, 300, NOW)
    assert nonce.isalnum() and len(nonce) == 58
    siws.check_nonce(SECRET, nonce, NOW + 299)
    with pytest.raises(siws.SignInError, match="expired"):
        siws.check_nonce(SECRET, nonce, NOW + 301)
    later = f"{int(NOW) + 10**6:010d}" + nonce[10:]  # extend the expiry without the key
    with pytest.raises(siws.SignInError, match="Unknown"):
        siws.check_nonce(SECRET, later, NOW)
    with pytest.raises(siws.SignInError, match="Unknown"):
        siws.check_nonce("other-secret", nonce, NOW)


def test_session_tokens() -> None:
    token, expires = siws.issue_token(SECRET, "Wallet111", 3600, NOW)
    assert expires == NOW + 3600
    assert siws.check_token(SECRET, token, NOW + 10) == "Wallet111"
    with pytest.raises(siws.SignInError, match="expired"):
        siws.check_token(SECRET, token, NOW + 3601)
    body, mac = token.split(".")
    forged = siws._b64(b'{"a":"Victim","e":9999999999}') + "." + mac
    for bad in (forged, body, f"{body}.00", "", "x.y.z"):
        with pytest.raises(siws.SignInError):
            siws.check_token(SECRET, bad, NOW)
    with pytest.raises(siws.SignInError):
        siws.check_token("other-secret", token, NOW)


def test_nonce_response_fields() -> None:
    fields = siws.nonce_response(SECRET, domain=DOMAIN, uri="https://x", ttl_s=300)
    assert set(fields) == {
        "domain", "uri", "statement", "nonce", "issued_at", "expiration_time", "version"
    }  # fmt: skip
    siws.check_nonce(SECRET, fields["nonce"])


# ── Pricing ───────────────────────────────────────────────────────────────────

TIERS = [(50_000_000, 10), (250_000_000, 20)]


@pytest.mark.parametrize(
    ("paid", "credited"),
    [(10_000_000, 10_000_000), (50_000_000, 55_000_000), (249_999_999, 274_999_998), (250_000_000, 300_000_000)],
)
def test_bonus_tiers(paid: int, credited: int) -> None:
    assert with_bonus(paid, TIERS) == credited


def test_sol_amounts_for_solana_pay() -> None:
    hot = Keypair().pubkey()
    assert sol_string(15_000_000) == "0.015"
    assert sol_string(1_000_000_000) == "1"
    assert sol_string(1) == "0.000000001"
    assert solana_pay_url(hot, 1_500_000) == f"solana:{hot}?amount=0.0015&label=Veridex&message=Veridex%20credits"


def test_purchase_transaction_is_an_unsigned_transfer_with_a_credit_memo() -> None:
    payer, hot, member = Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey()
    raw = purchase_transaction(payer, hot, 7, "11111111111111111111111111111111", member)
    message = VersionedTransaction.from_bytes(raw).message
    assert message.account_keys[0] == payer  # fee payer and the only signer
    assert message.header.num_required_signatures == 1
    pay, memo = message.instructions
    assert message.account_keys[pay.program_id_index] == Pubkey.from_string("11111111111111111111111111111111")
    assert int.from_bytes(bytes(pay.data)[4:12], "little") == 7
    assert message.account_keys[memo.program_id_index] == MEMO_PROGRAM
    assert bytes(memo.data) == f"veridex:credit:{member}".encode()
    assert list(memo.accounts) == []


def test_solana_pay_url_carries_the_beneficiary_memo() -> None:
    hot, member = Keypair().pubkey(), Keypair().pubkey()
    assert solana_pay_url(hot, 1_000_000, member).endswith(f"&memo=veridex%3Acredit%3A{member}")


# ── Trial ─────────────────────────────────────────────────────────────────────


def test_trial_limiter_per_ip_and_daily_cap() -> None:
    now = [NOW]
    limiter = TrialLimiter(per_ip_per_day=2, daily_cap=3, clock=lambda: now[0])
    assert limiter.allow("1.1.1.1") and limiter.allow("1.1.1.1")
    assert not limiter.allow("1.1.1.1")
    assert limiter.allow("2.2.2.2")
    assert not limiter.allow("3.3.3.3")  # daily cap
    now[0] += 86_400 + 1
    assert limiter.allow("1.1.1.1") and limiter.allow("3.3.3.3")
    assert all(len(k) == 32 for k in limiter._hits)  # salted hashes, never the addresses


class HistoryRpc:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    async def signature_page(self, address: Pubkey, **_: Any) -> list[dict[str, Any]]:
        return self.entries


@pytest.mark.parametrize(
    ("entries", "eligible"),
    [
        ([], False),
        ([{"blockTime": int(NOW) - 86_400}], False),  # one day old, one transaction
        ([{"blockTime": int(NOW) - 31 * 86_400}], True),  # old enough
        ([{"blockTime": int(NOW)}] * 10, True),  # busy enough
        ([{"blockTime": None}], False),
    ],
)
async def test_history_check(entries: list[dict[str, Any]], eligible: bool) -> None:
    rpc: Any = HistoryRpc(entries)
    address = str(Keypair().pubkey())
    assert await has_history(rpc, address, min_age_days=30, min_signatures=10, now=NOW) is eligible


# ── Reading transfers ────────────────────────────────────────────────────────

HOT = "Hot1111111111111111111111111111111111111111"


def tx(*instructions: dict[str, Any], inner: list[dict[str, Any]] | None = None, err: Any = None) -> dict[str, Any]:
    return {
        "transaction": {"message": {"instructions": list(instructions)}},
        "meta": {"err": err, "innerInstructions": [{"index": 0, "instructions": inner or []}]},
    }


def system(kind: str, **info: Any) -> dict[str, Any]:
    return {"program": "system", "parsed": {"type": kind, "info": info}}


def test_incoming_transfers_by_sender() -> None:
    parsed = tx(
        system("transfer", source="alice", destination=HOT, lamports=5),
        system("transfer", source="alice", destination=HOT, lamports=7),
        system("transfer", source=HOT, destination="cold", lamports=100),  # a sweep
        system("transfer", source="bob", destination="someone-else", lamports=9),
        system("createAccount", source=HOT, newAccount="ata", lamports=2),
        {"program": "spl-token", "parsed": {"type": "transferChecked", "info": {"destination": HOT}}},
        {"programId": "Unparsed111", "data": "abc"},
        inner=[
            system("transfer", source="vault", destination=HOT, lamports=11),
            system("transferWithSeed", source="derived", sourceBase="carol", destination=HOT, lamports=13),
        ],
    )
    assert incoming_transfers(parsed, HOT) == {"alice": 12, "vault": 11, "carol": 13}


def test_failed_transactions_credit_nothing() -> None:
    parsed = tx(system("transfer", source="alice", destination=HOT, lamports=5), err={"InstructionError": [0, 1]})
    assert incoming_transfers(parsed, HOT) == {}


def memo(text: str) -> dict[str, Any]:
    return {"program": "spl-memo", "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "parsed": text}


MEMBER = str(Keypair().pubkey())


def test_a_memo_names_who_is_credited() -> None:
    # A smart-wallet vault pays through a program; its member key (which can sign in) gets the credits.
    parsed = tx(
        memo(f"veridex:credit:{MEMBER}"), inner=[system("transfer", source="vault", destination=HOT, lamports=9)]
    )
    assert credit_beneficiary(parsed) == MEMBER
    assert credits_for(parsed, HOT) == {MEMBER: 9}


@pytest.mark.parametrize(
    "memos",
    [
        [],
        ["veridex:credit:not-a-pubkey"],
        [f"veridex:credit:{MEMBER}", f"veridex:credit:{Keypair().pubkey()}"],
        [f"veridex:credit:{MEMBER}", f"veridex:credit:{MEMBER}"],
        [f"thanks! {MEMBER}"],
    ],
)
def test_without_exactly_one_good_memo_the_sender_is_credited(memos: list[str]) -> None:
    parsed = tx(*(memo(m) for m in memos), system("transfer", source="alice", destination=HOT, lamports=5))
    assert credits_for(parsed, HOT) == {"alice": 5}


def test_a_memo_without_a_payment_credits_nothing() -> None:
    assert credits_for(tx(memo(f"veridex:credit:{MEMBER}")), HOT) == {}


def test_a_memo_naming_the_hot_wallet_is_ignored() -> None:
    hot = str(Keypair().pubkey())
    parsed = tx(memo(f"veridex:credit:{hot}"), system("transfer", source="alice", destination=hot, lamports=5))
    assert credits_for(parsed, hot) == {"alice": 5}
