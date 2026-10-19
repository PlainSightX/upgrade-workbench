# Oracle sources

The contract and checks were written on 2026-10-19 before any model request for this case.
They are derived from the pinned v4.3.0 `README.md`, the module docstrings and the upstream
examples and tests kept under `upstream/pydantic_cli/` (provenance only; not run as checks
and not copied verbatim). Expected exit codes follow `_runner`, which maps any exception
before the runner call to the exception handler's code.

Known conflicts resolved before the model run:

- In Pydantic 1 a bare `Optional[bool]` annotation is optional with default `None`; in
  Pydantic 2 it is required. The checks write `Optional[...] = None` explicitly, so they test
  the library rather than the changed default of user models.
- The README examples use `class Config` with `CLI_*` attributes and `Field(cli=...)`. Both are
  still accepted by Pydantic 2.6 (with deprecation warnings), so the contract keeps them as the
  user-facing configuration surface.
- Validation error message text differs between Pydantic versions; checks only assert exit
  codes and whether the runner was called.

Upstream later released Pydantic 2 support (v7.0.0 on PyPI, 2024-08-23; no 5.x or 6.x
release was published). Its diff, commits and issues were not read while writing the contract
or checks, and are excluded from solver input. The Pydantic migration guide sections in
`evidence/` reuse the hashed document already registered by an earlier case.
