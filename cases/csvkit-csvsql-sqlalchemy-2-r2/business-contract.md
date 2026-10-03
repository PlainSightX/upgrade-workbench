# csvsql behavior after SQLAlchemy migration

Migrate the pinned csvkit 1.1.1 `csvsql` utility from SQLAlchemy 1.4.49 to
2.0.25 without changing the supplied dependency locks or behavior checks.

- `csvsql --query` loads CSV input into its SQL database, executes the query,
  and returns the selected CSV rows and header in the requested order.
- With `--db --insert`, the imported rows remain visible to a fresh SQLite
  connection after the utility exits. A query on that database returns the
  matching rows rather than merely exiting successfully.
- `--before-insert` and `--after-insert` execute their supplied statements in
  the same database workflow; their effects remain visible after commit.
- Preserve ordinary CSV parsing, table names and SQL result values. Do not
  replace the utility with a test-specific exporter or bypass its SQL path.

The public checks use small sales and inventory data; independent checks use
different schemas and values. File-backed SQLite is the tested dialect.
Other database drivers, large inputs and every csvkit command are outside this
case's acceptance claim.
