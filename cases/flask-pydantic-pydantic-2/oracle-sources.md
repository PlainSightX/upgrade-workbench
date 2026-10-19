# Oracle sources

The contract and checks were written on 2026-10-19 before any model request for this case.
They are derived from the pinned v0.11.0 `README.md`, the `validate` docstring and the
upstream tests kept under `upstream/tests/` (provenance only; not run as checks and not
copied verbatim).

Known conflicts resolved before the model run:

- Pydantic's error `msg` and `type` strings differ between versions, and Pydantic 2 adds keys
  such as `input`, `ctx` and `url`. The contract fixes the error envelope, the `loc` list and
  that custom validator messages appear in `msg`; it does not fix version-specific wording or
  extra keys.
- In Pydantic 1 a bare `Optional[str]` annotation defaults to `None`; in Pydantic 2 it is
  required. The checks write `Optional[...] = None` explicitly.
- The README pins no Flask version; the case freezes Flask 2.3.3 / Werkzeug 2.3.8 on both sides
  so only Pydantic changes.

Upstream later released Pydantic 2 support (v0.12.0, 2024-01-08). Its diff, commits and
issues were not read while writing the contract or checks, and are excluded from solver input.
The Pydantic migration guide sections in `evidence/` reuse the hashed document already
registered by an earlier case.
