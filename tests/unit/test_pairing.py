"""The phone-pairing store: ids, codes, expiry, one-time delivery, binding rules and the per-visitor limit."""

import pytest

from unfret.credits import siws
from unfret.credits.pairing import PairingError, PairingStore, PairState, new_code, pairing_statement


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> PairingStore:
    return PairingStore(ttl_s=300, clock=clock)


def created(store: PairingStore, ip: str = "203.0.113.7") -> tuple[str, str]:
    result = store.create(ip)
    assert result is not None
    pair_id, pairing = result
    return pair_id, pairing.code


def test_ids_are_high_entropy_and_codes_unambiguous(store: PairingStore) -> None:
    ids = {created(store, f"198.51.100.{i}")[0] for i in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 22 for i in ids)  # 16 random bytes, URL-safe base64
    for _ in range(200):
        code = new_code()
        assert len(code) == 5 and code[2] == "-"
        assert not set(code.replace("-", "")) & set("0O1IL5S2Z8B")


def test_pending_until_signed_in_then_delivered_exactly_once(store: PairingStore) -> None:
    pair_id, code = created(store)
    assert store.poll(pair_id) == (PairState.PENDING, None)

    store.bind(pair_id, code, "token-1", "Addr", 1_900_000_000.0)
    state, pairing = store.poll(pair_id)
    assert state is PairState.READY and pairing is not None
    assert (pairing.token, pairing.address) == ("token-1", "Addr")

    assert store.poll(pair_id) == (PairState.UNKNOWN, None)  # a replayed poll gets nothing


def test_a_signed_in_pairing_cannot_be_rebound(store: PairingStore) -> None:
    pair_id, code = created(store)
    store.bind(pair_id, code, "victim-token", "Victim", 1.0)
    with pytest.raises(PairingError) as exc:
        store.bind(pair_id, code, "attacker-token", "Attacker", 1.0)
    assert exc.value.kind == "bound"
    state, pairing = store.poll(pair_id)
    assert pairing is not None and pairing.token == "victim-token"


def test_a_wrong_code_is_refused(store: PairingStore) -> None:
    pair_id, code = created(store)
    other = next(c for c in iter(new_code, None) if c != code)
    with pytest.raises(PairingError) as exc:
        store.bind(pair_id, other, "t", "A", 1.0)
    assert exc.value.kind == "mismatch"
    assert store.poll(pair_id)[0] is PairState.PENDING


def test_expiry_is_distinct_from_unknown(store: PairingStore, clock: Clock) -> None:
    pair_id, code = created(store)
    clock.now += 301
    assert store.poll(pair_id) == (PairState.EXPIRED, None)
    with pytest.raises(PairingError) as exc:
        store.bind(pair_id, code, "t", "A", 1.0)
    assert exc.value.kind == "expired"
    assert store.poll("not-a-pairing") == (PairState.UNKNOWN, None)

    clock.now += 3600  # long gone: swept on the next creation
    created(store, "192.0.2.1")
    assert store.poll(pair_id) == (PairState.UNKNOWN, None)


def test_creation_is_limited_per_visitor_and_in_total(clock: Clock) -> None:
    store = PairingStore(per_ip=3, max_pairings=5, clock=clock)
    assert [store.create("203.0.113.7") is not None for _ in range(4)] == [True, True, True, False]
    assert store.create("198.51.100.1") is not None
    assert store.create("198.51.100.2") is not None
    assert store.create("198.51.100.3") is None  # full
    clock.now += 901  # past the rate window and the expired pairings' grace period
    assert store.create("203.0.113.7") is not None


def test_ids_are_not_kept_in_the_clear(store: PairingStore) -> None:
    pair_id, _ = created(store)
    assert pair_id not in store._pairings


def test_the_statement_names_the_code() -> None:
    statement = pairing_statement(siws.STATEMENT, "K7-4Q")
    assert statement.startswith(siws.STATEMENT)
    assert statement.endswith("For the device showing K7-4Q.")
