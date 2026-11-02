# idemcheck

Find the HTTP handlers that will double-charge when a client retries.

A client retries because the network lied. If your handler writes before it can answer,
the retry writes again. Idempotency libraries exist for every framework, and nothing tells
you which of your routes is missing the guard.

```
$ idemcheck examples/
idemcheck: 1 finding(s) across 2 mutating route(s) in 1 file(s), worst high

[HIGH] examples/app.py:12 (missing-idempotency)
  route: POST /charge
  why:   charge with no idempotency key, dedupe store or unique constraint
  calls: db.orders.insert_one, db.session.commit
  fix:   read an Idempotency-Key and store it before the first write, or make the write
         itself unique so a second delivery cannot land twice
```

The example file contains two POST handlers: `/charge` writes and is flagged, `/charge-safe`
reads an `Idempotency-Key` first and is not.

## Install

```bash
pipx install idemcheck     # once it is on PyPI
# or straight from the repo:
pipx install git+https://github.com/mgkvibez/idemcheck
```

Python 3.9 or newer. No runtime dependencies.

## Usage

```bash
idemcheck                      # scan the current directory
idemcheck src/ app.py          # scan specific paths
idemcheck --json | jq .        # machine-readable
idemcheck --fail-on medium     # stricter gate, for a payments service
idemcheck --include-tests      # include tests/ (skipped by default)
```

Exit codes: `0` nothing at or above the threshold, `1` findings, `2` usage error.

`--fail-on` defaults to `high`, so a `PUT` that writes without a guard reports and exits 0
until you ask otherwise.

## The runtime: stop the second charge, not just report it

The linter tells you which handlers are undefended. `idemcheck.runtime` defends them.

```python
from idemcheck.runtime import SQLiteStore, idempotent

store = SQLiteStore("idempotency.db")

@idempotent(store=store)
def charge_card(amount, idempotency_key=None):
    gateway.charge(amount)                       # runs once per key, ever
    return {"charged": amount}
```

```
$ python examples/runtime_demo.py
first:   {'charged': 2500, 'key': 'order-41'}
retry:   {'charged': 2500, 'key': 'order-41'}
charges that reached the gateway: []
```

Four outcomes, and they are genuinely different:

| Situation | What happens |
|---|---|
| the key is new | the function runs, and the result is stored |
| the key completed already | the stored result is returned, nothing runs |
| the key is in flight | `IdempotencyConflict`, rather than a second charge |
| the key came back with a different payload | `IdempotencyMismatch`, because a key identifies one operation |

A failure is never remembered: if the function raises, the key is released so the retry can
work. Caching an exception would make a transient gateway timeout permanent.

Two stores ship: `MemoryStore` for tests and single workers, `SQLiteStore` for anything that
has to survive a restart, shared by every worker that opens the same file. The claim is one
atomic insert, so two workers racing on one key produce one winner and one conflict, which
`tests/test_runtime.py` proves with two real threads. Async handlers work the same way, and
`current_idempotency_key()` gives you the key of the call in progress for logs and audit.

`on_conflict="raise"` is the default. `"wait"` blocks for the in-flight call and replays it;
`"run"` lets the work through, which is only right when repeating it is already harmless.

## Middleware: the header, not a changed signature

The runtime needs the key passed to the function. A real app receives it as a header and will
not rewrite every handler, so `idemcheck.adapters` does the mechanics for you.

```python
from idemcheck.adapters import IdempotencyMiddleware
from idemcheck.runtime import SQLiteStore

app.add_middleware(IdempotencyMiddleware, store=SQLiteStore("idempotency.db"))
```

```python
# Flask, or anything WSGI
from idemcheck.adapters import WSGIMiddleware

app.wsgi_app = WSGIMiddleware(app.wsgi_app, store=store)
```

```
$ python examples/adapter_demo.py
delivery 1: 201 {"charged": 2500}  replayed=False
delivery 2: 201 {"charged": 2500}  replayed=True
times the gateway was charged: 1
```

What the middleware does with each delivery of an `Idempotency-Key`:

| Situation | Response |
|---|---|
| no header, `require_key=False` | passes through untouched, nothing stored |
| first delivery | runs the app, stores the status, headers and body |
| duplicate of a completed call | the stored response, plus `Idempotency-Replayed: true` |
| another call is still in flight | `409 idempotency_conflict`, the handler never runs |
| same key, different body | `422 idempotency_mismatch` |
| the app answered 5xx or 429 | key released, nothing stored, so the retry really retries |

Neither middleware imports FastAPI, Starlette, Flask or Django, so installing this cannot pin a
version of one. Three behaviours are deliberate rather than incidental:

* only `POST` is intercepted by default (`methods=(...)` to change it), and anything unhandled
  is passed straight to the app without its response being buffered, so SSE and streaming
  endpoints behave exactly as before
* a body over `max_body` (1 MB) is passed through **intact but unprotected**, rather than
  silently swallowed, because a request the app never sees is worse than an unprotected one
* a 5xx or 429 releases the key instead of storing it. Caching a server error turns a transient
  failure into a permanent one, which is the opposite of what a retry is for

## What it looks for

A handler is a finding when it answers a mutating method **and** calls something that
writes **and** nothing in it mentions a guard.

| Severity | When |
|---|---|
| high | `POST`, or any outbound money/HTTP send, with unguarded writes |
| medium | `PUT`/`PATCH`/`DELETE` with unguarded writes (they should already be idempotent) |

Writes are recognised by method name: `commit`, `flush`, `insert*`, `update*`, `delete*`,
`save`, `upsert`, `execute`, `add`, plus money movement like `charge`, `capture`,
`transfer`, `refund`, and queue sends like `delay` and `enqueue`.

A guard is recognised by any of: `idempotency`, `idempotent`, `idem_key`, `request_id`,
`dedupe`, `dedup_key`, `nonce`, `client_reference`, `get_or_create`, `update_or_create`,
`on_conflict`, `unique_together`.

Routes are read from the decorator: `@app.post`, `@router.put`, and the
`methods=[...]` keyword on `@app.route` and `@app.api_route` (Flask and FastAPI).

## What this deliberately does not do

Static analysis cannot prove a handler is safe, and this tool does not claim to.

- A guard installed by a decorator defined in another module is invisible here.
- An ORM that commits on your behalf, or a write hidden inside a helper function, is
  invisible here.
- Django class-based views, generic routers and dynamically constructed handlers are not
  detected at all.
- A handler that mentions `request_id` incidentally is treated as guarded, even when it is
  not. That is a false negative, and it is the direction this tool errs on purpose: a
  checker that cries wolf gets uninstalled after one afternoon.

The clean line is exactly what it says: no unguarded write was **recognised**.

## Tests

```bash
python -m pytest -q      # 51 tests
bash scripts/verify.sh   # the release gate
```

## Contributing

Adding a framework means teaching the decorator reader one more name in `analyze.py`, and
adding a fixture that proves both the flagged and the guarded case. A rule that cannot show
both is not ready.

## License

MIT.
