# Changelog

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
