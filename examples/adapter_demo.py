"""Two deliveries of the same Idempotency-Key, through the middleware, with a bare ASGI app.

No FastAPI needed to see the mechanic: the middleware is the part being demonstrated.
"""

import asyncio

from idemcheck.adapters import IdempotencyMiddleware
from idemcheck.runtime import SQLiteStore

gateway_calls = []


async def charge_app(scope, receive, send):
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    gateway_calls.append("charged 2500")
    await send({"type": "http.response.start", "status": 201,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"charged": 2500}'})


app = IdempotencyMiddleware(charge_app, store=SQLiteStore("demo-adapters.db"))


async def delivery(key):
    scope = {"type": "http", "method": "POST", "path": "/charge", "query_string": b"",
             "headers": [(b"idempotency-key", key.encode())]}
    sent = []
    chunks = [b'{"amount": 2500}']

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = [m["status"] for m in sent if m["type"] == "http.response.start"][0]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    replayed = any(k == b"idempotency-replayed"
                   for m in sent if m["type"] == "http.response.start"
                   for k, _ in m.get("headers", []))
    return status, body, replayed


if __name__ == "__main__":
    for attempt in (1, 2):
        status, body, replayed = asyncio.run(delivery("order-41"))
        print("delivery %d: %s %s  replayed=%s"
              % (attempt, status, body.decode(), replayed))
    print("times the gateway was charged:", len(gateway_calls))
