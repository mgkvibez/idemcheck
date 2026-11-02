"""Stop a retry from doing the work twice: a key, a store, and conflict detection.

`idemcheck` lint tells you at CI time which handlers are undefended. This is the runtime that
defends them: wrap the function, give each attempt a key, and the second delivery of the same
key returns the first result instead of charging the card again.

Three outcomes matter, and they are different:

* the key is new -> run the function and remember the result
* the key completed already -> return the stored result, do not run anything
* the key is in flight -> another worker is mid-call, so refuse rather than duplicate
* the key was reused with a different payload -> a client bug worth surfacing loudly

A failure is never remembered. If the function raises, the key is released so the retry may
try again, because caching an exception makes a transient error permanent.

Stdlib only: an in-memory store for tests and a SQLite store for anything durable.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import inspect
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Outcome(Enum):
    """What happened when a key was claimed."""

    ACQUIRED = "acquired"      # nobody holds this key: run the function
    REPLAY = "replay"          # it completed before: return the stored result
    CONFLICT = "conflict"      # it is in flight: refuse rather than duplicate
    MISMATCH = "mismatch"      # same key, different payload: a client bug


class IdempotencyError(Exception):
    """Base for everything this module raises."""


class IdempotencyConflict(IdempotencyError):
    """The same key is being processed right now by another caller."""


class IdempotencyMismatch(IdempotencyError):
    """The key was reused with a different payload."""


class IdempotencyStorageError(IdempotencyError):
    """The store could not keep the result, so a replay would be a lie."""


@dataclass
class Record:
    """One key and what became of it."""

    key: str
    fingerprint: str
    status: str = "in_flight"          # in_flight | completed
    response: Any = None
    created: float = field(default_factory=time.time)
    completed: Optional[float] = None

    def expired(self, ttl: float, now: Optional[float] = None) -> bool:
        if ttl <= 0:
            return False
        return (now or time.time()) - self.created > ttl


@dataclass
class Claim:
    """The answer to "may I run this?"."""

    outcome: Outcome
    record: Optional[Record] = None


class MemoryStore:
    """A process-local store. Fine for tests and single workers, useless across workers."""

    def __init__(self, ttl: float = 86400.0) -> None:
        self.ttl = ttl
        self._records: Dict[str, Record] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, fingerprint: str) -> Claim:
        with self._lock:
            record = self._records.get(key)
            if record is None or record.expired(self.ttl):
                self._records[key] = Record(key, fingerprint)
                return Claim(Outcome.ACQUIRED)
            return self._judge(record, fingerprint)

    def _judge(self, record: Record, fingerprint: str) -> Claim:
        if record.fingerprint != fingerprint:
            return Claim(Outcome.MISMATCH, record)
        if record.status == "completed":
            return Claim(Outcome.REPLAY, record)
        return Claim(Outcome.CONFLICT, record)

    def complete(self, key: str, response: Any) -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                raise IdempotencyStorageError("cannot complete an unclaimed key: %s" % key)
            record.status = "completed"
            record.response = response
            record.completed = time.time()

    def release(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)

    def get(self, key: str) -> Optional[Record]:
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.expired(self.ttl):
                return None
            return record

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class SQLiteStore:
    """Durable store: survives a restart and is visible to every worker sharing the file.

    The claim is a single INSERT inside a transaction, so two workers racing on one key
    produce exactly one winner and one `conflict`, not two charges.
    """

    def __init__(self, path: str = "idempotency.db", ttl: float = 86400.0) -> None:
        self.ttl = ttl
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS idempotency ("
            " key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,"
            " response TEXT, created REAL NOT NULL, completed REAL)"
        )
        self._conn.commit()

    def claim(self, key: str, fingerprint: str) -> Claim:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, fingerprint, status, response, created, completed"
                " FROM idempotency WHERE key = ?", (key,)).fetchone()
            if row is not None:
                record = Record(row[0], row[1], row[2],
                                json.loads(row[3]) if row[3] is not None else None,
                                row[4], row[5])
                if not record.expired(self.ttl):
                    if record.fingerprint != fingerprint:
                        return Claim(Outcome.MISMATCH, record)
                    if record.status == "completed":
                        return Claim(Outcome.REPLAY, record)
                    return Claim(Outcome.CONFLICT, record)
                self._conn.execute("DELETE FROM idempotency WHERE key = ?", (key,))
            self._conn.execute(
                "INSERT INTO idempotency (key, fingerprint, status, created)"
                " VALUES (?, ?, 'in_flight', ?)", (key, fingerprint, time.time()))
            self._conn.commit()
            return Claim(Outcome.ACQUIRED)

    def complete(self, key: str, response: Any) -> None:
        try:
            payload = json.dumps(response)
        except (TypeError, ValueError) as exc:
            # Storing nothing but claiming success would make the next replay a lie.
            self.release(key)
            raise IdempotencyStorageError(
                "the result of %r is not JSON-serialisable (%s); pass a serializer or return "
                "a plain structure" % (key, exc))
        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET status = 'completed', response = ?, completed = ?"
                " WHERE key = ?", (payload, time.time(), key))
            self._conn.commit()

    def release(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM idempotency WHERE key = ? AND status = 'in_flight'", (key,))
            self._conn.commit()

    def get(self, key: str) -> Optional[Record]:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, fingerprint, status, response, created, completed"
                " FROM idempotency WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return Record(row[0], row[1], row[2],
                      json.loads(row[3]) if row[3] is not None else None, row[4], row[5])

    def __len__(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]

    def close(self) -> None:
        self._conn.close()


#: The key of the call being executed right now, for logging and audit trails.
_current_key: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "idemcheck_key", default=None)


def current_idempotency_key() -> Optional[str]:
    """The key of the call in progress in this context, if any."""
    return _current_key.get()


def _fingerprint(args: tuple, kwargs: dict, ignore: str) -> str:
    """A stable hash of the request, so a key reused for different work is caught."""
    payload = json.dumps({"args": args, "kwargs": {k: v for k, v in kwargs.items()
                                                  if k != ignore}}, default=str,
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _resolve_key(extractor, args: tuple, kwargs: dict, param: str) -> str:
    if extractor is not None:
        key = extractor(*args, **kwargs)
    else:
        key = kwargs.get(param)
        if key is None and args:
            key = getattr(args[0], param, None)
    if not key:
        raise IdempotencyError(
            "no idempotency key: pass %s=... to the call or give the decorator a key=" % param)
    return str(key)


DEFAULT_STORE: Optional[object] = None


def _default_store() -> MemoryStore:
    """A shared process-local store, created on first use. Tests and single workers only."""
    global DEFAULT_STORE
    if DEFAULT_STORE is None:
        DEFAULT_STORE = MemoryStore()
    return DEFAULT_STORE  # type: ignore[return-value]


def idempotent(store=None, *, key=None, key_param: str = "idempotency_key",
               ttl: float = 86400.0, on_conflict: str = "raise"):
    """Run this function at most once per key.

    ``store`` is a MemoryStore or SQLiteStore. ``key`` is a callable that pulls the key out of
    the arguments; by default the call must carry ``idempotency_key=...``.

    ``on_conflict`` chooses what happens when another caller holds the key: ``"raise"``
    (default) raises IdempotencyConflict, ``"wait"`` blocks for the in-flight call to finish
    and then replays it, and ``"run"`` lets it through anyway, which is what you want only
    when the work is already safe to repeat.
    """
    store = store if store is not None else _default_store()
    if ttl:
        store.ttl = ttl

    def decorator(func):
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                token_key = _resolve_key(key, args, kwargs, key_param)
                claim = store.claim(token_key, _fingerprint(args, kwargs, key_param))
                claim = await _settle_async(store, token_key, claim, on_conflict)
                if claim.outcome is not Outcome.ACQUIRED:
                    return claim.record.response
                token = _current_key.set(token_key)
                try:
                    result = await func(*args, **kwargs)
                except BaseException:
                    store.release(token_key)
                    raise
                finally:
                    _current_key.reset(token)
                store.complete(token_key, result)
                return result
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            token_key = _resolve_key(key, args, kwargs, key_param)
            claim = store.claim(token_key, _fingerprint(args, kwargs, key_param))
            claim = _settle(store, token_key, claim, on_conflict)
            if claim.outcome is not Outcome.ACQUIRED:
                return claim.record.response
            token = _current_key.set(token_key)
            try:
                result = func(*args, **kwargs)
            except BaseException:
                store.release(token_key)
                raise
            finally:
                _current_key.reset(token)
            store.complete(token_key, result)
            return result
        return wrapper
    return decorator


WAIT_TIMEOUT = 30.0        # seconds "wait" will block for an in-flight call to finish
POLL = 0.02


def _conflict(key: str) -> IdempotencyConflict:
    return IdempotencyConflict(
        "another caller is processing key %r right now; the retry was refused rather than "
        "repeating the work" % key)


def _mismatch(key: str) -> IdempotencyMismatch:
    return IdempotencyMismatch(
        "key %r was already used with a different payload; a key identifies one operation, "
        "so reusing it for another request is a client bug" % key)


def _settle(store, key: str, claim: Claim, on_conflict: str) -> Claim:
    """Turn a conflict into whatever the caller asked for."""
    if claim.outcome is Outcome.MISMATCH:
        raise _mismatch(key)
    if claim.outcome is not Outcome.CONFLICT:
        return claim
    if on_conflict == "run":
        return Claim(Outcome.ACQUIRED)
    if on_conflict != "wait":
        raise _conflict(key)
    deadline = time.time() + WAIT_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL)
        record = store.get(key)
        if record is not None and record.status == "completed":
            return Claim(Outcome.REPLAY, record)
    raise _conflict(key)


async def _settle_async(store, key: str, claim: Claim, on_conflict: str) -> Claim:
    if claim.outcome is Outcome.MISMATCH:
        raise _mismatch(key)
    if claim.outcome is not Outcome.CONFLICT:
        return claim
    if on_conflict == "run":
        return Claim(Outcome.ACQUIRED)
    if on_conflict != "wait":
        raise _conflict(key)
    deadline = time.time() + WAIT_TIMEOUT
    while time.time() < deadline:
        await asyncio.sleep(POLL)
        record = store.get(key)
        if record is not None and record.status == "completed":
            return Claim(Outcome.REPLAY, record)
    raise _conflict(key)
