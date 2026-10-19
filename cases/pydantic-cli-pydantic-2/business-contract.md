# pydantic-cli Pydantic 2 migration contract

The application is the `pydantic_cli` library at upstream tag v4.3.0. Users declare their own
`pydantic.BaseModel` option classes and turn them into command-line tools. The migration must
keep the documented behavior below while moving the runtime from Pydantic 1.10.15 to 2.6.4.
Python 3.9, `pytest` and `typing-extensions` stay pinned at the same versions.

- Runners: `to_runner(model, func, ...)(args)` parses the argument list, builds one instance
  of the model and calls `func` with it, returning the function's integer exit code.
  `run_and_exit` does the same and exits with that code. Missing required values, values that
  fail Pydantic validation (field constraints and `@validator` checks) and unknown or malformed
  options do not call `func`; they return a non-zero exit code through the exception handler.
  An exception raised by `func` is mapped by the supplied `exception_handler`.
  `prologue_handler(opts)` runs before `func`; `epilogue_handler(exit_code, seconds)` runs after.
- Flags: each field becomes `--<field_name>`. `Field(..., cli=(short, long))` or `cli=(long,)`
  customizes the flags, and `Field(description=...)` feeds the help text, which also shows the
  default value. `List`/`Set` fields accept one or more values; `Enum` fields accept the member
  value.
- Booleans follow the README "Defining Boolean Flags" section: a `bool` field with a default
  gets one flag that negates the default (`--enable-x` for `False`, `--disable-x` for `True`,
  or the custom flag); required `bool` and `Optional[bool]` fields get an
  `(--enable-x, --disable-x)` pair or the custom pair; `Optional[bool] = None` stays `None`
  when no flag is given. `Config.CLI_BOOL_PREFIX` replaces the default prefixes.
- JSON presets: `Config.CLI_JSON_ENABLE = True` adds `--json-config` (renamed by
  `CLI_JSON_KEY`). Values from the file satisfy required fields; explicit command-line values
  override them. `CLI_JSON_CONFIG_ENV_VAR` names an environment variable that supplies the
  default path. A missing file given on the command line fails without calling `func`.
- `--help` and `--version` print and return 0 without calling `func`. `pydantic_class_to_parser`
  returns the `argparse` parser for a model. `to_runner_sp` / `run_sp_and_exit` with
  `SubParser` entries dispatch to the selected sub-command's model and function.

Only Python modules under `pydantic_cli/` may change. Option models are written by users;
the library must keep accepting the documented `class Config` with `CLI_*` attributes and
the `cli=` keyword on `Field`. The README and license are not part of the repair. Do not
change the public names or signatures listed above.

Public feedback checks are visible during repair. Independent acceptance checks are frozen,
not visible to the solver, and run only after the task stops. Passing public checks is not
sufficient for acceptance.
