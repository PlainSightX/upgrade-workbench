# Oracle sources

The contract and checks were written on 2026-10-17 before any model request for this case.
They are derived from the pinned v1.5.3 `README.md`, module docstrings and the upstream tests
kept under `upstream/sqlalchemy_mixins/tests/` (provenance only; not run as checks and not
copied verbatim). The check data and query-count expectations follow the upstream tests'
documented behavior; the transaction-boundary check follows `save()`'s docstring and the
code's use of `flush()` without `commit()`.

Known conflicts resolved before the model run:

- The README "Autocommit" section recommends `autocommit=True` sessions, which SQLAlchemy 2.0
  removed. The contract does not require that mode; checks use ordinary sessions with explicit
  caller commits, which the upstream tests also use.
- `Query.get()` is legacy in 2.0 but still exists; `find` behavior is specified by result, not
  by the API used.

Upstream released SQLAlchemy 2.0 support later (v2.0.0, commit
`2c78dfd7408e64c6f1f67fff304f007b801b93e3`). Its diff, commits and issues were not read while
writing the contract or checks, and are excluded from solver input. The SQLAlchemy migration
guide sections in `evidence/` reuse the hashed document already registered by an earlier case.
