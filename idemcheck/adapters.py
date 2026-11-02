"""Framework adapters: the runtime behind a header, not behind a changed signature.

`idemcheck.runtime` needs the key passed to the function. A real app receives it as an HTTP
header and will not rewrite every handler, so these middlewares do the mechanics: read
`Idempotency-Key`, claim it, run the app once, store the response, and replay it for every
later delivery of the same key.

Both are dependency-free on purpose: an ASGI middleware for FastAPI and Starlette, and a WSGI
middleware for Flask and Django. Nothing imports those frameworks, so installing this cannot
pin a version of one.

What is deliberately not done here:

* Requests whose method is not in ``methods`` (POST by default) pass straight through, and
  their response is never buffered, so streaming and SSE endpoints are untouched.
* A body larger than ``max_body`` passes through unprotected rather than being buffered, since
  a middleware that silently eats a 200 MB upload is worse than a gap the linter will flag.
* A 5xx (and 429) releases the key instead of storing it: a server error is exactly the case a
  client should be allowed to retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .runtime import MemoryStore, Outcome, _current_key

DEFAULT_HEADER = "idempotency-key"
REPLAYED_HEADER = "idempotency-replayed"

#: Statuses that must not be remembered, because they invite a retry rather than describe a
#: final answer.
TRANSIENT = frozenset({429, 500, 502, 503, 504})


def _body_fingerprint(method: str, path: str, query: bytes, body: bytes) -> str:
    """Everything that makes this a different request, hashed."""
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query or b"", body or b""):
        digest.update(part)
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _header_value(headers: Iterable[Tuple[bytes, bytes]], name: str) -> Optional[str]:
    wanted = name.lower().encode()
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def _encode_response(status: int, headers: Sequence[Tuple[bytes, bytes]],
                     body: bytes) -> Dict[str, Any]:
    return {
        "status": status,
        "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in headers],
        "body": base64.b64encode(body).decode("ascii"),
    }


def _decode_body(payload: Dict[str, Any]) -> bytes:
    return base64.b64decode(payload["body"].encode("ascii"))


def _json_response(status: int, payload: Dict[str, Any]):
    body = json.dumps(payload).encode()
    return status, [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())], body


def _conflict_body(key: str) -> Tuple[int, List[Tuple[bytes, bytes]], bytes]:
    return _json_response(409, {
        "error": "idempotency_conflict",
        "message": "another request with this Idempotency-Key is still being processed; "
                   "retry after it completes",
        "key": key,
    })


def _mismatch_body(key: str) -> Tuple[int, List[Tuple[bytes, bytes]], bytes]:
    return _json_response(422, {
        "error": "idempotency_mismatch",
        "message": "this Idempotency-Key was already used with a different request body",
        "key": key,
    })


def _missing_body(header: str) -> Tuple[int, List[Tuple[bytes, bytes]], bytes]:
    return _json_response(400, {
        "error": "idempotency_key_required",
        "message": "this endpoint requires the %s header" % header,
    })


class IdempotencyMiddleware:
    """ASGI middleware. Works with FastAPI and Starlette without importing either.

    .. code-block:: python

        from idemcheck.adapters import IdempotencyMiddleware
        from idemcheck.runtime import SQLiteStore

        app.add_middleware(IdempotencyMiddleware, store=SQLiteStore("idem.db"))
    """

    def __init__(self, app, store=None, header: str = DEFAULT_HEADER,
                 methods: Sequence[str] = ("POST",), require_key: bool = False,
                 skip_paths: Sequence[str] = (), max_body: int = 1_000_000,
                 key_scope=None) -> None:
        self.app = app
        self.store = store if store is not None else MemoryStore()
        self.header = header
        self.methods = tuple(m.upper() for m in methods)
        self.require_key = require_key
        self.skip_paths = tuple(skip_paths)
        self.max_body = max_body
        self.key_scope = key_scope

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._applies(scope):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        key = _header_value(headers, self.header)
        if not key:
            if not self.require_key:
                await self.app(scope, receive, send)
                return
            status, out_headers, body = _missing_body(self.header)
            await _send_response(send, status, out_headers, body)
            return

        body, oversized = await _read_body(receive, self.max_body)
        if oversized:                         # too large to buffer: intact, just unprotected
            await self.app(scope, _chain_body(body, receive), send)
            return

        path = scope.get("path", "")
        query = scope.get("query_string", b"")
        fingerprint = _body_fingerprint(scope.get("method", ""), path, query, body)
        scoped_key = self.key_scope(path, key, scope) if self.key_scope else key
        claim = self.store.claim(scoped_key, fingerprint)

        if claim.outcome is Outcome.MISMATCH:
            status, out_headers, payload = _mismatch_body(scoped_key)
            await _send_response(send, status, out_headers, payload)
            return
        if claim.outcome is Outcome.CONFLICT:
            status, out_headers, payload = _conflict_body(scoped_key)
            await _send_response(send, status, out_headers, payload)
            return
        if claim.outcome is Outcome.REPLAY:
            stored = claim.record.response
            await _send_response(send, stored["status"],
                                 _with_replay_header(stored["headers"]),
                                 _decode_body(stored))
            return

        captured = {"status": 0, "headers": [], "chunks": []}

        async def capturing_send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await send(message)

        token = _current_key.set(scoped_key)
        try:
            await self.app(scope, _replay_body(body), capturing_send)
        except BaseException:
            self.store.release(scoped_key)
            raise
        finally:
            _current_key.reset(token)

        if captured["status"] in TRANSIENT:
            self.store.release(scoped_key)     # a retry is the right response to this
            return
        self.store.complete(scoped_key, _encode_response(
            captured["status"], captured["headers"], b"".join(captured["chunks"])))

    def _applies(self, scope) -> bool:
        if scope.get("method", "").upper() not in self.methods:
            return False
        path = scope.get("path", "")
        return not any(path.startswith(prefix) for prefix in self.skip_paths)


async def _read_body(receive, max_body: int):
    """Buffer the request body. Returns (bytes, oversized)."""
    chunks: List[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        chunks.append(chunk)
        if size > max_body or not message.get("more_body"):
            break
    return b"".join(chunks), size > max_body


def _replay_body(body: bytes):
    """A receive that hands the buffered body downstream exactly once."""
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _chain_body(prefix: bytes, receive):
    """Buffered bytes first, then whatever the real client is still sending.

    Used when a body had no content-length and outgrew the buffer: the request must reach the
    app intact even though it is not being protected.
    """
    state = {"sent": False}

    async def chained():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": prefix, "more_body": True}
        return await receive()

    return chained


async def _send_response(send, status: int, headers: Sequence, body: bytes) -> None:
    pairs = headers if headers and isinstance(headers[0], tuple) else [
        (k.encode() if isinstance(k, str) else k, v.encode() if isinstance(v, str) else v)
        for k, v in headers]
    await send({"type": "http.response.start", "status": status, "headers": list(pairs)})
    await send({"type": "http.response.body", "body": body})


def _with_replay_header(headers: Sequence[Sequence[str]]) -> List[Tuple[bytes, bytes]]:
    pairs = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    pairs = [(k, v) for k, v in pairs if k.lower() != REPLAYED_HEADER.encode()]
    pairs.append((REPLAYED_HEADER.encode(), b"true"))
    return pairs


class WSGIMiddleware:
    """WSGI middleware for Flask and Django. No framework import, so no version pin.

    .. code-block:: python

        from idemcheck.adapters import WSGIMiddleware
        app.wsgi_app = WSGIMiddleware(app.wsgi_app, store=store)
    """

    def __init__(self, app, store=None, header: str = DEFAULT_HEADER,
                 methods: Sequence[str] = ("POST",), require_key: bool = False,
                 skip_paths: Sequence[str] = (), max_body: int = 1_000_000,
                 key_scope=None) -> None:
        self.app = app
        self.store = store if store is not None else MemoryStore()
        self.header = header
        self.methods = tuple(m.upper() for m in methods)
        self.require_key = require_key
        self.skip_paths = tuple(skip_paths)
        self.max_body = max_body
        self.key_scope = key_scope

    def _header(self, environ) -> Optional[str]:
        return environ.get("HTTP_" + self.header.upper().replace("-", "_"))

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "").upper()
        path = environ.get("PATH_INFO", "")
        if method not in self.methods or any(path.startswith(p) for p in self.skip_paths):
            return self.app(environ, start_response)

        key = self._header(environ)
        if not key:
            if not self.require_key:
                return self.app(environ, start_response)
            status, headers, body = _missing_body(self.header)
            return _wsgi_send(start_response, status, headers, body)

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > self.max_body:
            return self.app(environ, start_response)     # too large: intact, unprotected

        stream = environ.get("wsgi.input")
        body = stream.read(self.max_body + 1) if stream is not None else b""
        if len(body) > self.max_body:
            environ["wsgi.input"] = _PrefixedBody(body, stream)
            return self.app(environ, start_response)
        environ["wsgi.input"] = _PrefixedBody(body, None)

        query = environ.get("QUERY_STRING", "").encode()
        fingerprint = _body_fingerprint(method, path, query, body)
        scoped_key = self.key_scope(path, key, environ) if self.key_scope else key
        claim = self.store.claim(scoped_key, fingerprint)

        if claim.outcome is Outcome.MISMATCH:
            status, headers, payload = _mismatch_body(scoped_key)
            return _wsgi_send(start_response, status, headers, payload)
        if claim.outcome is Outcome.CONFLICT:
            status, headers, payload = _conflict_body(scoped_key)
            return _wsgi_send(start_response, status, headers, payload)
        if claim.outcome is Outcome.REPLAY:
            stored = claim.record.response
            pairs = _with_replay_header(stored["headers"])
            start_response(str(stored["status"]),
                           [(k.decode("latin-1"), v.decode("latin-1")) for k, v in pairs])
            return [_decode_body(stored)]

        captured: Dict[str, Any] = {"status": "200 OK", "headers": []}

        def capture_start(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            return lambda chunk: None

        token = _current_key.set(scoped_key)
        try:
            iterable = self.app(environ, capture_start)
            payload = b"".join(iterable)
            close = getattr(iterable, "close", None)
            if callable(close):
                close()
        except BaseException:
            self.store.release(scoped_key)
            raise
        finally:
            _current_key.reset(token)

        code = int(str(captured["status"]).split()[0])
        body_out = b"" if isinstance(payload, dict) else payload
        if code in TRANSIENT:
            self.store.release(scoped_key)
            start_response(captured["status"], captured["headers"])
            return [body_out]

        self.store.complete(scoped_key, _encode_response(
            code, [(k.encode("latin-1"), v.encode("latin-1"))
                   for k, v in captured["headers"]], body_out))
        start_response(captured["status"], captured["headers"])
        return [body_out]


class _PrefixedBody:
    """A wsgi.input whose first read returns the bytes we already consumed."""

    def __init__(self, prefix: bytes, stream) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if self._prefix:
            prefix, self._prefix = self._prefix, b""
            return prefix
        return self._stream.read(size) if self._stream is not None else b""

    def readline(self, size: int = -1) -> bytes:
        return self.read(size)


def _wsgi_send(start_response, status: int, headers: Sequence[Tuple[bytes, bytes]],
               body: bytes):
    start_response(str(status), [(k.decode("latin-1"), v.decode("latin-1"))
                                 for k, v in headers])
    return [body]
