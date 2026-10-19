# Flask-Pydantic Pydantic 2 migration contract

The application is the `flask_pydantic` library at upstream tag v0.11.0. Users decorate Flask
view functions with `@validate(...)` and declare their own `pydantic.BaseModel` classes. The
migration must keep the documented behavior below while moving the runtime from Pydantic
1.10.15 to 2.6.4. Python 3.9, Flask 2.3.3, Werkzeug 2.3.8, `pytest` and `typing-extensions`
stay pinned at the same versions.

- Inputs: query, JSON body and form models are given either as `validate(query=..., body=...,
  form=...)` or as annotations of the view's `query`, `body` and `form` parameters. Parsed
  models are available as `request.query_params`, `request.body_params` and
  `request.form_params`, and as those keyword arguments. Repeated query keys fill `List`
  fields. Annotated URL path parameters are parsed and converted to their annotated types.
  `request_body_many=True` validates a JSON array into a list of models and rejects a
  non-array body with a `root` error. `get_json_params` is passed to `request.get_json`.
- Validation errors: without raising, the response is
  `{"validation_error": {"query_params"|"body_params"|"form_params"|"path_params": [...]}}`
  with status 400, or `FLASK_PYDANTIC_VALIDATION_ERROR_STATUS_CODE` if configured. Each item
  is a JSON object with `loc` (a list of field names), `msg` and `type`; custom validator
  messages appear in `msg`. With `FLASK_PYDANTIC_VALIDATION_ERROR_RAISE`, a
  `flask_pydantic.ValidationError` carrying the same error lists in `body_params`,
  `form_params`, `path_params` and `query_params` is raised instead.
- Responses: a returned model is serialized to JSON with status `on_success_status` (default
  200) and `application/json` mimetype; `exclude_none` and `response_by_alias` are honored.
  `response_many=True` serializes an iterable of models as a JSON array and raises
  `InvalidIterableOfModelsException` for other content. A `(model, status)`,
  `(model, headers)` or `(model, status, headers)` tuple sets status and headers. Other return
  values pass through to Flask unchanged.

Only Python modules under `flask_pydantic/` may change. The README and license are not part of
the repair. Do not change the public names or signatures listed above.

Public feedback checks are visible during repair. Independent acceptance checks are frozen,
not visible to the solver, and run only after the task stops. Passing public checks is not
sufficient for acceptance.
