"""Adapters tested against bare ASGI and WSGI apps, so no framework has to be installed."""

import asyncio
import io

from idemcheck.adapters import (
    IdempotencyMiddleware,
    WSGIMiddleware,
    _body_fingerprint,
)
from idemcheck.runtime import MemoryStore


class FakeASGI:
    """Minimal ASGI app that records how many times it was actually reached."""

    def __init__(self, status=200, body=b'{"ok":true}'):
        self.status = status
        self.body = body
        self.calls = 0
        self.seen_body = b""

    async def __call__(self, scope, receive, send):
        self.calls += 1
        while True:
            message = await receive()
            self.seen_body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": self.status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": self.body})


def call_asgi(app, method="POST", path="/charge", headers=(), body=b"{}"):
    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": list(headers)}
    sent = []
    chunks = [body]

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    out_headers = dict(next((m.get("headers", []) for m in sent
                             if m["type"] == "http.response.start"), []))
    return status, payload, out_headers


def _key(k):
    return [(b"idempotency-key", k.encode())]


def test_a_request_without_the_header_is_untouched():
    inner = FakeASGI()
    mw = IdempotencyMiddleware(inner)
    status, _, _ = call_asgi(mw)
    assert status == 200 and inner.calls == 1


def test_the_second_delivery_replays_without_reaching_the_app():
    inner = FakeASGI()
    store = MemoryStore()
    mw = IdempotencyMiddleware(inner, store=store)

    first = call_asgi(mw, headers=_key("k1"))
    second = call_asgi(mw, headers=_key("k1"))

    assert inner.calls == 1, "the handler ran twice"
    assert first[1] == second[1]
    assert first[2].get(b"idempotency-replayed") is None
    assert second[2][b"idempotency-replayed"] == b"true"


def test_a_non_idempotent_method_passes_through():
    inner = FakeASGI()
    mw = IdempotencyMiddleware(inner, store=MemoryStore())
    call_asgi(mw, method="GET", headers=_key("k1"))
    call_asgi(mw, method="GET", headers=_key("k1"))
    assert inner.calls == 2


def test_a_request_still_in_flight_gets_409_and_does_not_run():
    inner = FakeASGI()
    store = MemoryStore()
    store.claim("k1", _body_fingerprint("POST", "/charge", b"", b"{}"))
    mw = IdempotencyMiddleware(inner, store=store)
    status, payload, _ = call_asgi(mw, headers=_key("k1"))
    assert status == 409
    assert b"idempotency_conflict" in payload
    assert inner.calls == 0


def test_the_same_key_with_a_different_body_gets_422():
    inner = FakeASGI()
    store = MemoryStore()
    store.claim("k1", _body_fingerprint("POST", "/charge", b"", b'{"amount":1}'))
    mw = IdempotencyMiddleware(inner, store=store)
    status, payload, _ = call_asgi(mw, headers=_key("k1"), body=b'{"amount":999}')
    assert status == 422
    assert b"idempotency_mismatch" in payload
    assert inner.calls == 0


def test_a_server_error_is_not_remembered_so_the_retry_can_work():
    inner = FakeASGI(status=503)
    store = MemoryStore()
    mw = IdempotencyMiddleware(inner, store=store)
    call_asgi(mw, headers=_key("k1"))
    call_asgi(mw, headers=_key("k1"))
    assert inner.calls == 2, "a 503 was cached, so the retry got a stale failure"
    assert len(store) == 0


def test_a_429_is_not_remembered_either():
    inner = FakeASGI(status=429)
    store = MemoryStore()
    mw = IdempotencyMiddleware(inner, store=store)
    call_asgi(mw, headers=_key("k1"))
    call_asgi(mw, headers=_key("k1"))
    assert inner.calls == 2


def test_a_client_error_is_remembered():
    """A 400 is a final answer for that payload, so replaying it is correct."""
    inner = FakeASGI(status=400)
    store = MemoryStore()
    mw = IdempotencyMiddleware(inner, store=store)
    call_asgi(mw, headers=_key("k1"))
    status, _, _ = call_asgi(mw, headers=_key("k1"))
    assert inner.calls == 1 and status == 400


def test_require_key_refuses_a_missing_header():
    inner = FakeASGI()
    mw = IdempotencyMiddleware(inner, store=MemoryStore(), require_key=True)
    status, payload, _ = call_asgi(mw)
    assert status == 400 and b"idempotency_key_required" in payload
    assert inner.calls == 0


def test_skip_paths_is_honoured():
    inner = FakeASGI()
    mw = IdempotencyMiddleware(inner, store=MemoryStore(), skip_paths=("/webhooks",))
    call_asgi(mw, path="/webhooks/stripe", headers=_key("k1"))
    call_asgi(mw, path="/webhooks/stripe", headers=_key("k1"))
    assert inner.calls == 2


def test_the_body_still_reaches_the_app_intact():
    inner = FakeASGI()
    mw = IdempotencyMiddleware(inner, store=MemoryStore())
    call_asgi(mw, headers=_key("k1"), body=b'{"amount":2500}')
    assert inner.seen_body == b'{"amount":2500}'


def test_an_oversized_body_passes_through_unbroken_and_unprotected():
    """Buffering is skipped, but the app must still receive every byte."""
    inner = FakeASGI()
    store = MemoryStore()
    mw = IdempotencyMiddleware(inner, store=store, max_body=4)
    status, _, _ = call_asgi(mw, headers=_key("k1"), body=b"0123456789")
    assert status == 200
    assert inner.seen_body == b"0123456789"
    assert len(store) == 0


def test_the_stored_response_keeps_its_status_and_content_type():
    inner = FakeASGI(status=201, body=b'{"id":7}')
    mw = IdempotencyMiddleware(inner, store=MemoryStore())
    call_asgi(mw, headers=_key("k1"))
    status, payload, headers = call_asgi(mw, headers=_key("k1"))
    assert status == 201
    assert payload == b'{"id":7}'
    assert headers[b"content-type"] == b"application/json"


# --- WSGI ---------------------------------------------------------------------


class FakeWSGI:
    def __init__(self, status="200 OK", body=b'{"ok":true}'):
        self.status = status
        self.body = body
        self.calls = 0
        self.seen_body = b""

    def __call__(self, environ, start_response):
        self.calls += 1
        self.seen_body = environ["wsgi.input"].read()
        start_response(self.status, [("content-type", "application/json")])
        return [self.body]


def call_wsgi(app, method="POST", path="/charge", headers=None, body=b"{}"):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    captured = {}

    def start_response(status, pairs, exc_info=None):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(pairs)

    payload = b"".join(app(environ, start_response))
    return captured["status"], payload, captured["headers"]


def test_wsgi_replays_the_second_delivery():
    inner = FakeWSGI()
    mw = WSGIMiddleware(inner, store=MemoryStore())
    first = call_wsgi(mw, headers={"Idempotency-Key": "k1"})
    second = call_wsgi(mw, headers={"Idempotency-Key": "k1"})
    assert inner.calls == 1
    assert first[1] == second[1]
    assert second[2].get("idempotency-replayed") == "true"


def test_wsgi_leaves_other_methods_alone():
    inner = FakeWSGI()
    mw = WSGIMiddleware(inner, store=MemoryStore())
    call_wsgi(mw, method="GET", headers={"Idempotency-Key": "k1"})
    call_wsgi(mw, method="GET", headers={"Idempotency-Key": "k1"})
    assert inner.calls == 2


def test_wsgi_conflict_returns_409_without_running():
    inner = FakeWSGI()
    store = MemoryStore()
    store.claim("k1", _body_fingerprint("POST", "/charge", b"", b"{}"))
    mw = WSGIMiddleware(inner, store=store)
    status, payload, _ = call_wsgi(mw, headers={"Idempotency-Key": "k1"})
    assert status == 409 and b"idempotency_conflict" in payload
    assert inner.calls == 0


def test_wsgi_body_reaches_the_app_intact():
    inner = FakeWSGI()
    mw = WSGIMiddleware(inner, store=MemoryStore())
    call_wsgi(mw, headers={"Idempotency-Key": "k1"}, body=b'{"amount":2500}')
    assert inner.seen_body == b'{"amount":2500}'


def test_wsgi_server_error_is_released():
    inner = FakeWSGI(status="503 Service Unavailable")
    store = MemoryStore()
    mw = WSGIMiddleware(inner, store=store)
    call_wsgi(mw, headers={"Idempotency-Key": "k1"})
    call_wsgi(mw, headers={"Idempotency-Key": "k1"})
    assert inner.calls == 2
    assert len(store) == 0
