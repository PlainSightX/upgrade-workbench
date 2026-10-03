"""官方静态工具的输入范围与产物边界；不运行Docker或上游代码。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from upgrade_workbench import codemod as baseline
from upgrade_workbench.cases import load_case

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("case_name", "package", "count"),
    [
        ("bump-my-version-0.5.0", "bumpversion", 14),
        ("copier-6.2.0", "copier", 11),
        ("openapi-python-client-0.10.0", "openapi_python_client", 47),
    ],
)
def test_registered_complete_application_is_selected(case_name, package, count):
    case = load_case(ROOT / "cases" / case_name / "manifest.json")
    inputs, roots = baseline.application_inputs(case)
    assert roots == [package]
    assert len(inputs) == count
    assert all(name.startswith(package + "/") and name.endswith(".py") for name in inputs)
    assert set(case.manifest.allowed_changes) <= set(inputs)
    if package == "bumpversion":
        assert "bumpversion/cli.py" in inputs
        assert "bumpversion/cli.py" not in case.manifest.allowed_changes
        assert not any(name.startswith("tests/") for name in inputs)


def _case(files, allowed):
    return SimpleNamespace(
        manifest=SimpleNamespace(
            snapshot_dir="source",
            allowed_changes=allowed,
            file_hashes=dict.fromkeys(files, "digest"),
        )
    )


def test_unclassified_sibling_source_is_not_silently_omitted():
    case = _case(["source/app/main.py", "source/another/domain.py"], ["app/main.py"])
    with pytest.raises(ValueError, match="Unclassified"):
        baseline.application_inputs(case)


def test_flat_application_keeps_helper_modules_but_excludes_registered_checks():
    case = _case(
        ["source/main.py", "source/helpers/value.py", "source/tests/test_value.py"], ["main.py"]
    )
    inputs, roots = baseline.application_inputs(case)
    assert inputs == ["helpers/value.py", "main.py"]
    assert roots == ["."]


def test_test_directory_cannot_be_an_editable_application_root():
    case = _case(["source/tests/test_value.py"], ["tests/test_value.py"])
    with pytest.raises(ValueError, match="test or check"):
        baseline.application_inputs(case)


def test_official_output_preserves_noneditable_context_and_raw_bytes(tmp_path):
    (tmp_path / "app").mkdir()
    original = {"app/model.py": b"old = 1\n", "app/helper.py": b"untouched = 2\n"}
    (tmp_path / "app/model.py").write_bytes(b"new = 1\r\n")
    (tmp_path / "app/helper.py").write_bytes(original["app/helper.py"])
    patch, changed, hashes = baseline.collect_output_patch(tmp_path, original, ["app/model.py"])
    assert changed == ["app/model.py"]
    assert "-old = 1\n+new = 1\n" in patch
    assert set(hashes) == set(original)
    assert (tmp_path / "app/model.py").read_bytes() == b"new = 1\r\n"


def test_illegal_output_is_rejected_without_deleting_raw_evidence(tmp_path):
    (tmp_path / "app").mkdir()
    original = {"app/model.py": b"old = 1\n", "app/helper.py": b"untouched = 2\n"}
    (tmp_path / "app/model.py").write_bytes(original["app/model.py"])
    (tmp_path / "app/helper.py").write_bytes(b"rewritten = 2\n")
    with pytest.raises(ValueError, match="outside the candidate allowlist"):
        baseline.collect_output_patch(tmp_path, original, ["app/model.py"])
    assert (tmp_path / "app/helper.py").read_bytes() == b"rewritten = 2\n"


def test_added_file_is_rejected(tmp_path):
    (tmp_path / "original.py").write_bytes(b"value = 1\n")
    (tmp_path / "new.py").write_bytes(b"value = 2\n")
    with pytest.raises(ValueError, match="added or removed"):
        baseline.collect_output_patch(tmp_path, {"original.py": b"value = 1\n"}, ["original.py"])


def test_no_semantic_output_change_is_not_fabricated_as_candidate(tmp_path):
    (tmp_path / "original.py").write_bytes(b"value = 1\r\n")
    with pytest.raises(ValueError, match="no candidate diff"):
        baseline.collect_output_patch(tmp_path, {"original.py": b"value = 1\n"}, ["original.py"])
