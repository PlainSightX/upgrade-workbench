"""依赖查询只读取已登记字节，并以静态限制替代猜测或目标导入。"""

import hashlib
from dataclasses import replace

import pytest

from upgrade_workbench.execution import (
    DependencyEnvironmentIdentity,
    bind_dependency_root,
    execute_dependency_query,
)


def environment() -> DependencyEnvironmentIdentity:
    return DependencyEnvironmentIdentity(
        role="new",
        lock_sha256="a" * 64,
        image_id="sha256:" + "b" * 64,
        base_image_id="sha256:" + "c" * 64,
        python_version="3.12.10",
    )


@pytest.fixture
def dependency(tmp_path):
    root = tmp_path / "dependency"
    root.mkdir()
    module = root / "row.py"
    module.write_bytes(
        b"raise RuntimeError('must not import dependency source')\n"
        b"class Row(BaseRow):\n"
        b"    kind = 'row'\n"
        b"    def __init__(self, values: tuple[str, ...]):\n"
        b"        self.values = values\n"
        b"    def keys(self) -> tuple[str, ...]:\n"
        b"        return self.values\n"
    )
    helper = root / "helper.py"
    helper.write_bytes(b"VALUE = 'Row'\n")
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (module, helper)
    }
    return bind_dependency_root(
        root,
        environment=environment(),
        distribution="SQLAlchemy",
        installed_version="2.0.7",
        file_hashes=hashes,
    )


def test_fixed_queries_read_only_registered_dependency_files(dependency):
    listing = execute_dependency_query(dependency, {"operation": "list_files"})
    assert [row["path"] for row in listing["result"]["files"]] == ["helper.py", "row.py"]

    found = execute_dependency_query(
        dependency,
        {"operation": "search_text", "query": "Row", "paths": ["row.py"], "max_results": 10},
    )
    assert found["result"]["matches"][0]["line"] == 2

    read = execute_dependency_query(
        dependency,
        {"operation": "read_file", "path": "helper.py", "start_line": 1, "end_line": 2},
    )
    assert read["result"]["text"] == "VALUE = 'Row'\n"
    assert read["environment"] == environment().as_dict()


def test_inspect_symbol_is_static_and_reports_runtime_limits(dependency):
    result = execute_dependency_query(
        dependency,
        {"operation": "inspect_symbol", "path": "row.py", "qualname": "Row"},
    )

    assert result["result"]["kind"] == "ClassDef"
    assert result["result"]["declared_bases"] == ["BaseRow"]
    assert result["result"]["mro"] is None
    assert result["result"]["signature"] == "(self, values: tuple[str, ...])"
    assert result["result"]["attributes"] == ["__init__", "keys", "kind", "values"]
    assert any("does not import" in item for item in result["limitations"])
    assert any("Runtime MRO" in item for item in result["limitations"])


def test_unknown_information_returns_limitation_instead_of_guessing(dependency):
    missing = execute_dependency_query(
        dependency,
        {"operation": "inspect_symbol", "path": "row.py", "qualname": "Missing"},
    )
    assert missing["result"] is None
    assert missing["limitations"] == [
        "Symbol was not found in the registered dependency source file."
    ]


@pytest.mark.parametrize(
    "action",
    [
        {"operation": "read_file", "path": "../secret", "start_line": 1, "end_line": 1},
        {"operation": "inspect_symbol", "path": "row.py", "qualname": "Row()"},
        {"operation": "shell", "command": "python -c 'import sqlalchemy'"},
        {
            "operation": "read_file",
            "path": "row.py",
            "start_line": 1,
            "end_line": 1,
            "url": "https://example.invalid",
        },
    ],
)
def test_shell_network_expressions_and_unsafe_paths_are_rejected(dependency, action):
    with pytest.raises(ValueError):
        execute_dependency_query(dependency, action)


def test_unregistered_host_file_is_not_read(dependency):
    secret = dependency.root / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    result = execute_dependency_query(
        dependency,
        {"operation": "read_file", "path": "secret.txt", "start_line": 1, "end_line": 1},
    )
    assert result["result"] is None
    assert "not present" in result["limitations"][0]


def test_file_or_environment_binding_changes_are_rejected(dependency):
    (dependency.root / "row.py").write_text("class Row: pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after verification"):
        execute_dependency_query(
            dependency,
            {"operation": "read_file", "path": "row.py", "start_line": 1, "end_line": 1},
        )

    changed_identity = replace(dependency, distribution="Other")
    with pytest.raises(ValueError, match="binding identity changed"):
        execute_dependency_query(changed_identity, {"operation": "list_files"})
