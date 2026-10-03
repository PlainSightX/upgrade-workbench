# Persistent scheduler migration contract

Migrate the allowed SQLAlchemy job-store module from SQLAlchemy 1.4.54 to 2.0.38.
The unchanged application is APScheduler 3.9.1, not a generic CRUD fixture. Its
public Job, scheduler, trigger, serialization and error contracts remain valid.

Preserve durable add/update/delete/remove-all operations: successful return must
make writes visible to another database connection, including after reopening a
store or restarting a paused scheduler. Preserve job identity, callable/arguments,
trigger and next-run time. Preserve missing lookup as None, missing update/delete
as JobLookupError, and duplicate insertion as ConflictingIdError. Exceptions must
not erase previously committed jobs or poison the next operation.

Preserve due-job ordering, subsecond timestamps, paused-last sorting and earliest
nonpaused wake-up time. Corrupt serialized rows are logged/removed by listing,
without losing valid jobs; that cleanup is durable. Results must be consumed
while usable and connections returned after reads, writes and errors, including
when the caller uses a bounded pool. Do not replace real storage by memory,
weaken database validation, swallow errors or invent cross-method transactions.

Only apscheduler/jobstores/sqlalchemy.py is editable. Other stores and unrelated
scheduler internals remain unchanged. The verifier supplies an isolated real
PostgreSQL database. SQLAlchemy legacy API does not mean every old spelling is
removed: do not rewrite unrelated valid Query or connection APIs. Tests, dependency
locks, case provenance and runtime configuration are not candidate-editable.

This acceptance covers selected persistence behavior, not every backend, plugin,
distributed scheduler guarantee, ORM mapping, async path or production workload.
