# sqlalchemy-filters SQLAlchemy 2.0 migration contract

The application is the `sqlalchemy_filters` library at upstream tag v0.13.0. Users pass their
own ORM `Query` objects (from `Session.query`) to the four public functions. The migration must
keep the documented behavior below while moving the runtime from SQLAlchemy 1.4.54 to 2.0.38.
Python 3.9, `six` and `pytest` stay pinned at the same versions.

- `apply_filters(query, filter_spec, do_auto_join=True)` accepts one dict or a list of dicts
  with `field`, optional `model`, optional `op` (default `==`) and `value`, and the nested
  `and`/`or`/`not` boolean forms. All listed operators work on columns, hybrid properties and
  hybrid methods. The model may be omitted when the query has one model; the query's models
  are found from entities, columns, aggregates, `select_from` and joins. A spec naming a model
  that is not in the query is joined automatically when SQLAlchemy can infer the join, unless
  `do_auto_join=False`. The result is a new `Query`.
- `apply_sort(query, sort_spec)` applies `asc`/`desc` ordering with optional `nullsfirst` /
  `nullslast`, on columns and hybrid attributes, with the same model resolution and automatic
  join.
- `apply_loads(query, load_spec)` restricts the columns loaded for each named model (the list of
  field names shorthand is accepted for a single model) with the same model resolution and
  automatic join; restricted columns are not selected by the query.
- `apply_pagination(query, page_number=None, page_size=None)` returns the limited/offset query
  and a `(page_number, page_size, num_pages, total_results)` named tuple with the documented
  defaults.
- Errors keep their types: `BadFilterFormat`, `BadSortFormat`, `BadLoadFormat`, `BadSpec`
  (ambiguous spec or a model not in the query), `FieldNotFound`, `BadQuery` (no models) and
  `InvalidPage`.

Only Python modules under `sqlalchemy_filters/` may change. The README and license are not part
of the repair. Do not change the public names or signatures listed above, and do not execute
or commit on the caller's behalf except where the documented functions already query the
database (`apply_pagination` counts results).

Public feedback checks are visible during repair. Independent acceptance checks are frozen,
not visible to the solver, and run only after the task stops. Passing public checks is not
sufficient for acceptance.
