# Oracle sources

The contract and checks were written on 2026-10-19 before any model request for this case.
They are derived from the pinned v0.13.0 `README.rst`, the function docstrings and the upstream
tests kept under `upstream/test/` (provenance only; not run as checks and not copied
verbatim). The data set mirrors the upstream fixtures' shape; expected rows were derived by
hand from the documented semantics.

Known conflicts resolved before the model run:

- The upstream tests use `sqlalchemy.ext.declarative.declarative_base` and PostgreSQL/MySQL
  fixtures. The checks use `sqlalchemy.orm.declarative_base` (valid in 1.4 and 2.0) and an
  in-memory SQLite engine; the PostgreSQL-only `any`/`not_any` operators are not checked.
- The README `apply_loads` examples pass strings to `load_only`; the contract specifies the
  observable result (restricted columns are not selected), not the API used.
- NULL ordering without `nullsfirst`/`nullslast` follows SQLite (NULLs sort first ascending,
  last descending); the checks rely on that only where both environments use SQLite.

The upstream repository has no release supporting SQLAlchemy 2.0 at the time of writing (the
v0.13.0 CI states 2.0 is not yet supported). No later upstream branch, fork, issue or pull
request was read while writing the contract or checks.
