"""The runtime's promises, tested where they matter: retries, races, failures, restarts."""

import asyncio
import threading
import time

import pytest

from idemcheck.runtime import (
    IdempotencyConflict,
    IdempotencyError,
    IdempotencyMismatch,
    IdempotencyStorageError,
    MemoryStore,
    SQLiteStore,
    _fingerprint,
    current_idempotency_key,
    idempotent,
)


@pytest.fixture
def calls():
    return []


def test_the_second_delivery_replays_instead_of_running_again(calls):
    store = MemoryStore()

    @idempotent(store=store)
    def charge(amount, idempotency_key=None):
        calls.append(amount)
        return {"charged": amount}

    first = charge(100, idempotency_key="k1")
    second = charge(100, idempotency_key="k1")
    assert first == second == {"charged": 100}
    assert calls == [100]                      # ran once


def test_a_different_key_runs_again(calls):
    store = MemoryStore()

    @idempotent(store=store)
    def charge(amount, idempotency_key=None):
        calls.append(amount)
        return amount

    charge(100, idempotency_key="k1")
    charge(100, idempotency_key="k2")
    assert calls == [100, 100]


def test_reusing_a_key_for_different_work_is_a_client_bug(calls):
    store = MemoryStore()

    @idempotent(store=store)
    def charge(amount, idempotency_key=None):
        calls.append(amount)
        return amount

    charge(100, idempotency_key="k1")
    with pytest.raises(IdempotencyMismatch):
        charge(999, idempotency_key="k1")
    assert calls == [100]                      # the second request never ran


def test_a_failure_is_released_so_the_retry_can_work(calls):
    """Caching an exception would make a transient error permanent."""
    store = MemoryStore()
    attempts = {"n": 0}

    @idempotent(store=store)
    def flaky(idempotency_key=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("gateway timeout")
        calls.append("ok")
        return "done"

    with pytest.raises(RuntimeError):
        flaky(idempotency_key="k1")
    assert flaky(idempotency_key="k1") == "done"
    assert calls == ["ok"]
    assert len(store) == 1


def test_a_call_still_in_flight_is_refused_not_duplicated():
    store = MemoryStore()
    store.claim("k1", _fingerprint((), {}, "idempotency_key"))

    @idempotent(store=store, key=lambda **kw: "k1")
    def charge():
        raise AssertionError("must not run while another caller holds the key")

    with pytest.raises(IdempotencyConflict):
        charge()


def test_on_conflict_run_lets_it_through_when_the_work_is_safe_to_repeat(calls):
    store = MemoryStore()
    store.claim("k1", _fingerprint((), {}, "idempotency_key"))

    @idempotent(store=store, key=lambda **kw: "k1", on_conflict="run")
    def charge():
        calls.append("ran")
        return "done"

    assert charge() == "done"
    assert calls == ["ran"]


def test_two_threads_racing_produce_one_charge():
    store = MemoryStore()
    ran = []
    barrier = threading.Barrier(2)
    outcomes = []

    @idempotent(store=store, key_param="idempotency_key")
    def charge(idempotency_key=None):
        ran.append(1)
        time.sleep(0.2)
        return "charged"

    def worker():
        barrier.wait()
        try:
            outcomes.append(charge(idempotency_key="race"))
        except IdempotencyConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ran) == 1, "the card was charged twice"
    assert "conflict" in outcomes


def test_sqlite_survives_a_new_store_instance(tmp_path, calls):
    """A restart must not re-charge: the record lives in the file, not the process."""
    path = str(tmp_path / "idem.db")
    store = SQLiteStore(path)

    @idempotent(store=store)
    def charge(amount, idempotency_key=None):
        calls.append(amount)
        return {"charged": amount}

    first = charge(50, idempotency_key="k1")
    store.close()

    reopened = SQLiteStore(path)

    @idempotent(store=reopened)
    def charge_again(amount, idempotency_key=None):
        calls.append(amount)
        return {"charged": amount}

    assert charge_again(50, idempotency_key="k1") == first
    assert calls == [50]


def test_an_expired_record_may_run_again(tmp_path, calls):
    store = MemoryStore(ttl=0.05)

    @idempotent(store=store, ttl=0.05)
    def charge(idempotency_key=None):
        calls.append(1)
        return "ok"

    charge(idempotency_key="k1")
    time.sleep(0.1)
    charge(idempotency_key="k1")
    assert calls == [1, 1]


def test_async_handlers_get_the_same_guarantee(calls):
    store = MemoryStore()

    @idempotent(store=store)
    async def charge(amount, idempotency_key=None):
        calls.append(amount)
        return {"charged": amount}

    async def scenario():
        return await charge(10, idempotency_key="k1"), await charge(10, idempotency_key="k1")

    first, second = asyncio.run(scenario())
    assert first == second
    assert calls == [10]


def test_a_missing_key_names_the_problem(calls):
    store = MemoryStore()

    @idempotent(store=store)
    def charge(amount):
        calls.append(amount)

    with pytest.raises(IdempotencyError) as exc:
        charge(10)
    assert "idempotency key" in str(exc.value)


def test_a_key_extractor_can_read_whatever_the_caller_has(calls):
    store = MemoryStore()

    @idempotent(store=store, key=lambda order: "order-%s" % order["id"])
    def charge(order):
        calls.append(order["id"])
        return "ok"

    charge({"id": 7})
    charge({"id": 7})
    assert calls == [7]


def test_an_unserialisable_result_is_an_error_not_a_phantom_success(tmp_path):
    """Claiming completion while storing nothing would make the replay a lie."""
    store = SQLiteStore(str(tmp_path / "idem.db"))

    @idempotent(store=store)
    def charge(idempotency_key=None):
        return object()

    with pytest.raises(IdempotencyStorageError):
        charge(idempotency_key="k1")
    assert len(store) == 0


def test_the_key_of_the_call_in_progress_is_readable(calls):
    store = MemoryStore()
    seen = []

    @idempotent(store=store)
    def charge(idempotency_key=None):
        seen.append(current_idempotency_key())
        return "ok"

    assert current_idempotency_key() is None
    charge(idempotency_key="k9")
    assert seen == ["k9"]
