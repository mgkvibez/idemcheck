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
python -m pytest -q      # 19 tests
bash scripts/verify.sh   # the release gate
```

## Contributing

Adding a framework means teaching the decorator reader one more name in `analyze.py`, and
adding a fixture that proves both the flagged and the guarded case. A rule that cannot show
both is not ready.

## License

MIT.
