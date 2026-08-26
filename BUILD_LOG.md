# BUILD_LOG.md — Honest incident log

This file tracks what broke during development, what we diagnosed, and what we did about it.
It is a submission requirement — judges specifically evaluate failure recovery.

Entries are added chronologically as incidents happen. Nothing is cleaned up or omitted.

---

## 2026-08-26 — Phase 1: get_db() signature mismatch

**What broke:** `seed.py` called `next(get_db())` expecting a generator, but `get_db()` in
`session.py` is decorated with `@contextmanager`, so it returns a context manager object
rather than a plain generator. Python raised `TypeError: '_GeneratorContextManager' object
is not an iterator`.

**Diagnosis:** The two common patterns for SQLAlchemy sessions in FastAPI projects are
(a) a `yield`-based generator for `Depends()` injection and (b) a `@contextmanager` for
scripts. We chose (b) for `get_db()` because scripts need explicit resource management.
`next()` only works on plain generators — not on context managers, even though both use
`yield` internally.

**Fix:** Changed the `__main__` block in `seed.py` to `with get_db() as db:`, which is
the correct pattern for a `@contextmanager`-decorated function. One-line change.

**Note:** `get_db_dependency()` in `session.py` is the separate generator for FastAPI's
`Depends()` injection — that one still uses `next()` internally (FastAPI handles it).
The two functions exist for different callers and must stay separate.
