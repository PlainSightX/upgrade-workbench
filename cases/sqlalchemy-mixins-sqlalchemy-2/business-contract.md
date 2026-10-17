# sqlalchemy-mixins SQLAlchemy 2.0 migration contract

The application is the `sqlalchemy_mixins` library at upstream tag v1.5.3. Users declare their
own models on a `declarative_base()` and combine the mixins. The migration must keep the
documented behavior below while moving the runtime from SQLAlchemy 1.4.54 to 2.0.38. Python
3.9, `six` and `pytest` stay pinned at the same versions.

- Active record: `set_session` binds a session to the model hierarchy. `create`, `fill`,
  `update`, `save`, `delete` and `destroy` persist through that session with a flush; they do
  not commit, so the caller's commit and rollback still decide what other connections see.
  Unknown attribute names raise `KeyError`. `all`, `first`, `find` (by primary key, `None`
  when absent) and `find_or_fail` (`ModelNotFoundError` when absent) keep their results.
- Inspection: `columns`, `primary_keys`, `relations`, `settable_relations` (excluding viewonly
  relations), `hybrid_properties`, `hybrid_methods` and `settable_attributes` keep their names.
- Eager loading: `with_`, `with_joined` and `with_subquery` accept relationship names as
  strings (including dotted nested paths such as `'posts.comments'`) and class attributes,
  and the nested `(SUBQUERY, {...})` / dict schema forms. Loaded relations do not cause
  additional SQL when accessed; the documented number of queries is preserved.
- Smart query: `where`, `sort`, `smart_query` and the standalone `smart_query(query, ...)`
  keep Django-like filters (`___` for relations, `__` for operators), descending sort with
  `-`, outer joins through relations, automatic eager loading of joined relations, nested
  `or_`/`and_` filter structures, hybrid properties and hybrid methods, and `filter_expr` /
  `order_expr` on both classes and `aliased()` entities. Unknown names raise `KeyError`.
  `smart_query` must continue to work on a `lazy="dynamic"` relationship query.
- `ReprMixin`, `SerializeMixin.to_dict` (flat, `nested`, `hybrid_attributes`, `exclude`) and
  `TimestampsMixin` keep their output.

Only Python modules under `sqlalchemy_mixins/` may change. The type stub `.pyi` files, the
README and the license are not part of the repair. Do not change the public names or
signatures listed above, and do not make the library commit on the caller's behalf.

Public feedback checks are visible during repair. Independent acceptance checks are frozen,
not visible to the solver, and run only after the task stops. Passing public checks is not
sufficient for acceptance.
