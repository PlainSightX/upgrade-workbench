"""评价文件分组与工作流输入隔离；假执行器不运行应用代码。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.evaluation import load_evaluation, select_checks
from upgrade_workbench.workflow import run_comparison


@pytest.fixture
def evaluated_case(tmp_path):
    root = tmp_path / "owned-case"
    evaluation = {
        "schema_version": 1, "case_id": "owned-evaluation", "split": "development",
        "groups": {
            "feedback": {"paths": ["test_public.py"],
                         "nodeids": ["test_public.py::test_public"], "expected_count": 1},
            "acceptance": {"paths": ["private/test_private.py"],
                           "nodeids": ["private/test_private.py::test_private"],
                           "expected_count": 1},
        },
    }
    files = {
        "source/owned.py": b"VALUE = 1\n",
        "checks/test_public.py": b"def test_public():\n    assert True\n",
        "checks/private/test_private.py": b"def test_private():\n    assert 'private-oracle'\n",
        "requirements/old.txt": b"owned==1\n",
        "requirements/new.txt": b"owned==2\n",
        "SOURCE.json": b'{"origin":"owned"}\n',
        "evaluation.json": json.dumps(evaluation).encode(),
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "schema_version": 1, "case_id": "owned-evaluation", "title": "Owned checks fixture",
        "source": {"kind": "synthetic_calibration", "repository": "local-fixture",
                   "revision": "v1", "license": "MIT", "description": "Owned metadata"},
        "review": {"status": "reviewed", "note": "Metadata test only"},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "allowed_changes": ["owned.py"], "expected_new_original": "failed",
        "file_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def amend_evaluation(path: Path, change):
    evaluation_path = path.parent / "evaluation.json"
    data = json.loads(evaluation_path.read_text(encoding="utf-8"))
    change(data)
    evaluation_path.write_text(json.dumps(data), encoding="utf-8")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["evaluation.json"] = hashlib.sha256(
        evaluation_path.read_bytes()
    ).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_feedback_materialization_contains_no_private_checks(evaluated_case, tmp_path):
    case = load_case(evaluated_case)
    destination = tmp_path / "feedback"
    nodes = select_checks(case, "feedback", destination)
    assert nodes == ["test_public.py::test_public"]
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")) == [
        "test_public.py",
    ]
    assert b"private-oracle" not in (destination / "test_public.py").read_bytes()


def test_final_materialization_has_complete_disjoint_identity(evaluated_case, tmp_path):
    case = load_case(evaluated_case)
    result = select_checks(case, "all", tmp_path / "all")
    assert result == ["private/test_private.py::test_private", "test_public.py::test_public"]
    private = select_checks(case, "acceptance", tmp_path / "private")
    assert private == ["private/test_private.py::test_private"]
    assert not (tmp_path / "private/test_public.py").exists()


@pytest.mark.parametrize("change", [
    lambda data: data.update(case_id="different"),
    lambda data: data.update(split="unregistered"),
    lambda data: data["groups"]["feedback"].update(expected_count=2),
    lambda data: data["groups"]["feedback"].update(expected_count=True),
    lambda data: data["groups"]["feedback"].update(nodeids=["private/test_private.py::test_private"]),
    lambda data: data["groups"]["feedback"].update(paths=["../test_public.py"]),
    lambda data: data["groups"]["feedback"].update(paths=["test_public.py", "test_public.py"]),
    lambda data: data["groups"].update(acceptance=deepcopy(data["groups"]["feedback"])),
])
def test_invalid_group_ownership_is_rejected(evaluated_case, change):
    amend_evaluation(evaluated_case, change)
    with pytest.raises(CaseValidationError):
        load_evaluation(load_case(evaluated_case))


def test_ungrouped_check_file_is_rejected(evaluated_case):
    extra = evaluated_case.parent / "checks/test_unowned.py"
    extra.write_bytes(b"def test_unowned():\n    assert True\n")
    manifest = json.loads(evaluated_case.read_text(encoding="utf-8"))
    manifest["file_hashes"]["checks/test_unowned.py"] = hashlib.sha256(extra.read_bytes()).hexdigest()
    evaluated_case.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CaseValidationError, match="exactly one"):
        load_evaluation(load_case(evaluated_case))


def test_evaluation_change_after_loading_is_rejected(evaluated_case, tmp_path):
    loaded = load_case(evaluated_case)
    (loaded.root / "evaluation.json").write_bytes(b"{}")
    with pytest.raises(CaseValidationError, match="SHA-256"):
        select_checks(loaded, "feedback", tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


class InspectionExecutor:
    """检查调用端实际传入哪些文件，仅返回元数据。"""

    def __init__(self, *, wrong_identity=False):
        self.inputs = []
        self.wrong_identity = wrong_identity

    def prepare_environment(self, lock_path, **kwargs):
        return {"image_id": "owned-fixture"}

    def verify(self, image_id, source_dir, checks_dir, **kwargs):
        self.inputs.append({
            path.relative_to(checks_dir).as_posix(): path.read_bytes()
            for path in checks_dir.rglob("*") if path.is_file()
        })
        nodes = ["test_public.py::test_public"]
        if self.wrong_identity:
            nodes = ["test_public.py::different"]
        passed = len(self.inputs) == 1
        return {
            "status": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
            "nodeids": nodes,
            "tests": {"collected": 1, "passed": int(passed), "failed": int(not passed),
                      "errors": 0, "skipped": 0},
        }


def test_workflow_passes_only_public_group_to_executor(evaluated_case, tmp_path):
    executor = InspectionExecutor()
    report = run_comparison(evaluated_case, tmp_path / "work", check_group="feedback",
                            executor=executor)
    assert report["status"] == "comparison_only"
    assert len(executor.inputs) == 2
    assert all(set(files) == {"test_public.py"} for files in executor.inputs)
    assert report["expected_nodeids"] == ["test_public.py::test_public"]


def test_old_baseline_cannot_silently_change_frozen_nodeids(evaluated_case, tmp_path):
    executor = InspectionExecutor(wrong_identity=True)
    report = run_comparison(evaluated_case, tmp_path / "work", check_group="feedback",
                            executor=executor)
    assert report["status"] == "test_set_changed"
    assert len(executor.inputs) == 1
