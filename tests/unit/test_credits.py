"""SIWS messages, nonces and sessions; purchase pricing; reading transfers for the scanner."""

from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from unfret.credits import siws
from unfret.credits.pricing import purchase_transaction, sol_string, solana_pay_url
from unfret.credits.scanner import credit_beneficiary, credits_for, incoming_transfers
from unfret.x402.solana import MEMO_PROGRAM

SECRET = "unit-secret"
NOW = 1_800_000_000.0
DOMAIN = "unfret.net"


def signed(keypair: Keypair, **overrides: str) -> tuple[bytes, bytes]:
    nonce, expires = siws.issue_nonce(SECRET, 300, NOW)
    fields = {
        "domain": DOMAIN,
        "address": str(keypair.pubkey()),
        "statement": siws.STATEMENT,
        "uri": "https://unfret.net",
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
    extended = message + b"\nRequest ID: 42\nResources:\n- https://unfret.net"
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


def test_an_oversized_message_is_refused_before_anything_else() -> None:
    keypair = Keypair()
    message, _ = signed(keypair)
    huge = message + b"\nResources:\n- " + b"x" * 2048
    with pytest.raises(siws.SignInError, match="too long"):
        verify(keypair, huge, bytes(keypair.sign_message(huge)))


@pytest.mark.parametrize("part", ["address", "signature", "message"])
def test_malformed_parts_are_refused(part: str) -> None:
    keypair = Keypair()
    message, signature = signed(keypair)
    address = str(keypair.pubkey())
    if part == "address":
        address = "not-base58!"
    elif part == "signature":
        signature = signature[:10]
    else:
        message = b"\xff\xfe" + message  # not UTF-8
        signature = bytes(keypair.sign_message(message))
    with pytest.raises(siws.SignInError, match="Malformed address, signature or message"):
        siws.verify_sign_in(
            SECRET, domain=DOMAIN, address=address, signed_message=message, signature=signature, now=NOW
        )


def test_a_pairing_sign_in_needs_its_own_statement() -> None:
    keypair = Keypair()
    message, signature = signed(keypair)  # the ordinary statement
    with pytest.raises(siws.SignInError, match="another device"):
        siws.verify_sign_in(
            SECRET,
            domain=DOMAIN,
            address=str(keypair.pubkey()),
            signed_message=message,
            signature=signature,
            statement="Pair this phone: 1234",
            now=NOW,
        )
    paired, paired_signature = signed(keypair, statement="Pair this phone: 1234")
    assert siws.verify_sign_in(
        SECRET,
        domain=DOMAIN,
        address=str(keypair.pubkey()),
        signed_message=paired,
        signature=paired_signature,
        statement="Pair this phone: 1234",
        now=NOW,
    ) == str(keypair.pubkey())


def test_a_message_without_a_statement_has_none() -> None:
    assert siws.statement_of(f"{DOMAIN} wants you to sign in with your Solana account:\nWallet111") == ""
    assert siws.statement_of(signed(Keypair())[0].decode()) == siws.STATEMENT


def test_a_message_without_a_nonce_is_refused() -> None:
    keypair = Keypair()
    message, _ = signed(keypair)
    stripped = b"\n".join(line for line in message.split(b"\n") if not line.startswith(b"Nonce: "))
    with pytest.raises(siws.SignInError, match="no nonce"):
        verify(keypair, stripped, bytes(keypair.sign_message(stripped)))


@pytest.mark.parametrize(
    ("not_before", "error"),
    [("2027-01-15T08:01:00Z", "not valid yet"), ("tomorrow", "Malformed time")],
)
def test_a_message_not_valid_yet_is_refused(not_before: str, error: str) -> None:
    keypair = Keypair()
    message, _ = signed(keypair)
    later = message + f"\nNot Before: {not_before}".encode()  # NOW is 2027-01-15T08:00:00Z
    with pytest.raises(siws.SignInError, match=error):
        verify(keypair, later, bytes(keypair.sign_message(later)))
    earlier = message + b"\nNot Before: 2020-01-01T00:00:00Z"
    assert verify(keypair, earlier, bytes(keypair.sign_message(earlier))) == str(keypair.pubkey())


@pytest.mark.parametrize("claims", [b"not json", b'{"a": "Wallet111"}', b'{"a": "W", "e": "soon"}', b"[1]"])
def test_a_correctly_signed_but_malformed_session_is_invalid(claims: bytes) -> None:
    body = siws._b64(claims)
    token = f"{body}.{siws._mac(SECRET, 'session:' + body)}"
    with pytest.raises(siws.SignInError, match="Invalid session"):
        siws.check_token(SECRET, token, NOW)


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


def test_sol_amounts_for_solana_pay() -> None:
    hot = Keypair().pubkey()
    assert sol_string(15_000_000) == "0.015"
    assert sol_string(1_000_000_000) == "1"
    assert sol_string(1) == "0.000000001"
    assert solana_pay_url(hot, 1_500_000) == f"solana:{hot}?amount=0.0015&label=Unfret&message=Unfret%20checks"


def test_purchase_transaction_is_an_unsigned_transfer_with_the_quote_memo() -> None:
    payer, hot = Keypair().pubkey(), Keypair().pubkey()
    raw = purchase_transaction(payer, hot, 7, "11111111111111111111111111111111", "unfret:quote:abcdefghijklmnopqrst")
    message = VersionedTransaction.from_bytes(raw).message
    assert message.account_keys[0] == payer  # fee payer and the only signer
    assert message.header.num_required_signatures == 1
    pay, memo = message.instructions
    assert message.account_keys[pay.program_id_index] == Pubkey.from_string("11111111111111111111111111111111")
    assert int.from_bytes(bytes(pay.data)[4:12], "little") == 7
    assert message.account_keys[memo.program_id_index] == MEMO_PROGRAM
    assert bytes(memo.data) == b"unfret:quote:abcdefghijklmnopqrst"
    assert list(memo.accounts) == []


def test_the_reference_rides_on_the_sol_transfer_and_the_link() -> None:
    payer, hot, reference = Keypair().pubkey(), Keypair().pubkey(), Keypair().pubkey()
    memo = "unfret:quote:abcdefghijklmnopqrst"
    raw = purchase_transaction(payer, hot, 7, "11111111111111111111111111111111", memo, reference=reference)
    message = VersionedTransaction.from_bytes(raw).message
    pay, note = message.instructions
    keys = message.account_keys
    # A trailing read-only, non-signing account: the System program ignores it (as in Solana Pay).
    assert [keys[i] for i in bytes(pay.accounts)] == [payer, hot, reference]
    index = keys.index(reference)
    assert index >= len(keys) - message.header.num_readonly_unsigned_accounts  # read-only, not a signer
    assert message.header.num_required_signatures == 1
    assert bytes(note.data) == memo.encode()
    url = solana_pay_url(hot, 1_000_000, memo, reference=str(reference))
    assert url == (
        f"solana:{hot}?amount=0.001&reference={reference}&label=Unfret&message=Unfret%20checks"
        "&memo=unfret%3Aquote%3Aabcdefghijklmnopqrst"
    )


def test_solana_pay_url_carries_the_quote_memo() -> None:
    hot = Keypair().pubkey()
    url = solana_pay_url(hot, 1_000_000, "unfret:quote:abcdefghijklmnopqrst")
    assert url.endswith("&memo=unfret%3Aquote%3Aabcdefghijklmnopqrst")


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
        memo(f"unfret:credit:{MEMBER}"), inner=[system("transfer", source="vault", destination=HOT, lamports=9)]
    )
    assert credit_beneficiary(parsed) == MEMBER
    assert credits_for(parsed, HOT) == {MEMBER: 9}


@pytest.mark.parametrize(
    "memos",
    [
        [],
        ["unfret:credit:not-a-pubkey"],
        [f"unfret:credit:{MEMBER}", f"unfret:credit:{Keypair().pubkey()}"],
        [f"unfret:credit:{MEMBER}", f"unfret:credit:{MEMBER}"],
        [f"thanks! {MEMBER}"],
    ],
)
def test_without_exactly_one_good_memo_the_sender_is_credited(memos: list[str]) -> None:
    parsed = tx(*(memo(m) for m in memos), system("transfer", source="alice", destination=HOT, lamports=5))
    assert credits_for(parsed, HOT) == {"alice": 5}


def test_a_memo_without_a_payment_credits_nothing() -> None:
    assert credits_for(tx(memo(f"unfret:credit:{MEMBER}")), HOT) == {}


def test_a_memo_naming_the_hot_wallet_is_ignored() -> None:
    hot = str(Keypair().pubkey())
    parsed = tx(memo(f"unfret:credit:{hot}"), system("transfer", source="alice", destination=hot, lamports=5))
    assert credits_for(parsed, hot) == {"alice": 5}
