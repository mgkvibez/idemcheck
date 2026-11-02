# Changelog

## v0.3.0 - 2026-11-02

- New module: `idemcheck.adapters`. `IdempotencyMiddleware` (ASGI: FastAPI, Starlette) and
  `WSGIMiddleware` (Flask, Django) read the `Idempotency-Key` header, run the app once, store the
  status, headers and body, and replay that response for every later delivery with
  `Idempotency-Replayed: true`.
- In-flight duplicate answers `409 idempotency_conflict`; the same key with a different body
  answers `422 idempotency_mismatch`. Neither reaches the handler.
- Only `POST` is intercepted by default; unhandled methods pass straight through with no
  buffering, so streaming and SSE endpoints are untouched.
- A body over `max_body` passes through intact but unprotected. The first cut read the body and
  then tried to pass through, which would have delivered a half-consumed request; the oversized
  path now chains the buffered prefix to the live stream.
- A 5xx or 429 releases the key rather than storing the error, so the retry actually retries.
  A 4xx is stored: it is a final answer for that payload.
- Neither middleware imports a framework, so nothing here pins FastAPI, Flask or Django.
- 18 new tests (51 total) driving bare ASGI and WSGI apps, so the suite needs no framework
  installed to prove the mechanics.

## v0.2.0 - 2026-11-02

- New module: `idemcheck.runtime`. `@idempotent(store=...)` runs a function once per key and
  replays the stored result for every retry after it. The linter reports the undefended
  handler; this defends it.
- Four outcomes: new key runs, completed key replays, in-flight key raises
  `IdempotencyConflict`, and a key reused with a different payload raises
  `IdempotencyMismatch`.
- A failure releases the key so the retry may try again. Caching an exception would make a
  transient error permanent.
- Two stores: `MemoryStore` (tests, single worker) and `SQLiteStore` (durable, shared across
  workers, atomic claim so two racing workers produce one charge and one conflict).
- Async handlers supported; `current_idempotency_key()` exposes the in-progress key for
  logging and audit. `on_conflict` can be `raise`, `wait` or `run`.
- An unserialisable result raises `IdempotencyStorageError` and stores nothing, because
  claiming completion while keeping no response would make the next replay a lie.
- 14 new tests (33 total), including a two-thread race and a restart test that reopens the
  SQLite file in a new store and proves the key still replays.

## v0.1.0 - 2026-11-02

First release.

- Finds mutating routes whose writes have no retry guard: `POST`/`PUT`/`PATCH`/`DELETE`
  handlers that call a write and never mention an idempotency key, dedupe store or unique
  constraint. One finding per handler, listing the calls that would repeat.
- Understands `@app.post`-style decorators and the `methods=[...]` keyword on `@app.route`
  and `@app.api_route`, so Flask and FastAPI are both read.
- The handler's own docstring is excluded from guard detection. A docstring saying
  "TODO: idempotency" is evidence of the bug, and the first cut counted it as the fix.
- Errs toward silence: anything that mentions `request_id`, `dedupe` or `on_conflict` counts
  as guarded. Documented as a false negative, because a checker that cries wolf gets
  uninstalled.
- `--fail-on high` by default, so a medium finding reports without failing CI.
- Zero runtime dependencies, 19 tests.
