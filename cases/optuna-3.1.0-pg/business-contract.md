# Optuna relational experiment-storage migration

Migrate SQLAlchemy 1.4.54 to 2.0.38 for the complete unmodified Optuna 3.1.0 package.
Only optuna/storages/_rdb/storage.py and models.py are editable. All dependency
versions other than SQLAlchemy, upstream migration history, checks and provenance
are fixed. The verifier provides real isolated PostgreSQL, never SQLite mocks.

RDBStorage must initialize a fresh database and reopen an existing one, keeping
Alembic revision and library schema checks operational. Do not disable checking,
force a revision onto incompatible databases, edit history or skip table creation.
Successful writes must be visible through independent database connections.

Preserve study name/direction ordering, duplicate-name errors, JSON attributes,
trial numbering per study, parameters/distribution compatibility, intermediate
values, state transitions, immutable completed trials and objective order. Keep
relationship-backed trial retrieval usable after sessions close. Keep best-trial
queries and state/study filters correct. Delete study dependants without deleting
another study. Preserve real high-level Study ask/tell and reopening behavior.

Session units must commit on success, roll back all uncommitted changes on errors,
preserve earlier committed records and support subsequent operations. Release
checked-out connections on read/write/error paths. Repeated operations in a
bounded pool must finish without leaking connections or retaining unintended
transactions that block schema operations. No swallowed database errors, fake
in-memory storage, disabled constraints, or changed preservation requirements.

Legacy Query and typed relationship loaders remain valid APIs. Do not rewrite
all old-looking ORM calls merely for style. Changes must preserve behavior,
including negative/error cases. Other optimization algorithms, distributed
throughput, optional integrations and migrations from historical Optuna schemas
are outside this case; do not claim full Optuna compatibility from selected tests.
