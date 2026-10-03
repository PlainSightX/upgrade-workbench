"""核验影响定位、版本依据与待审阅请求的连接，不调用模型或容器。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from upgrade_workbench import planning
from upgrade_workbench.cases import CaseValidationError
from upgrade_workbench.generation import provider

CASES = Path(__file__).parents[2] / "cases"
CALIBRATION = CASES / "pydantic-field-contract" / "manifest.json"
APPLICATION = CASES / "bump-my-version-0.5.0" / "manifest.json"


@pytest.fixture(autouse=True)
def prevent_external_work(monkeypatch):
    sender = Mock(side_effect=AssertionError("Unexpected provider request"))
    process = Mock(side_effect=AssertionError("Unexpected subprocess execution"))
    monkeypatch.setattr(provider, "_send_once", sender)
    monkeypatch.setattr(subprocess, "run", process)
    yield
    sender.assert_not_called()
    process.assert_not_called()


def test_legacy_analysis_is_available_but_proposal_requires_bound_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    report = planning.create_analysis(CALIBRATION, tmp_path / "work")
    assert report["status"] == "analysis_completed"
    assert report["target_code_executed"] is False
    assert report["version_evidence"] is None
    assert report["findings"]
    assert json.loads(Path(report["report_path"]).read_text(encoding="utf-8")) == report
    prepare = Mock(side_effect=AssertionError("Unbound analysis must not reach generation"))
    monkeypatch.setattr(planning, "prepare_request", prepare)
    with pytest.raises(CaseValidationError, match="registered evidence/bundle.json"):
        planning.prepare_case_proposal(CALIBRATION, tmp_path / "work")
    prepare.assert_not_called()


def test_real_application_binds_actual_findings_to_locked_version_evidence(tmp_path: Path) -> None:
    report = planning.create_analysis(APPLICATION, tmp_path / "work")
    evidence = report["version_evidence"]
    assert evidence["binding"]["old_version"] == "1.10.24"
    assert evidence["binding"]["new_version"] == "2.11.7"
    assert evidence["case_fingerprint"] == report["case_fingerprint"]
    assert any(item["rule"] == "nullable_without_default" for item in report["findings"])
    keys = {item["evidence_key"] for item in evidence["entries"]}
    assert {item["evidence_key"] for item in report["findings"]} <= keys


def test_proposal_preparation_freezes_public_context_without_calls(tmp_path: Path) -> None:
    report = planning.prepare_case_proposal(
        APPLICATION, tmp_path / "work", model="unit-test-model",
        endpoint="https://provider.example/v1/chat/completions", max_output_tokens=1234,
        timeout_seconds=37, public_source_ack=True, thinking_mode="disabled",
    )
    assert report["status"] == "request_prepared"
    assert report["calls"] == 0
    assert report["target_code_executed"] is False
    assert report["analysis_findings_included"] > 0
    assert report["max_output_tokens"] == 1234
    assert report["timeout_seconds"] == 37
    request = json.loads(Path(report["request_path"]).read_text(encoding="utf-8"))
    assert request["model"] == "unit-test-model"
    assert request["max_tokens"] == 1234
    assert request["thinking"] == {"type": "disabled"}
    context = json.loads(request["messages"][1]["content"])
    assert context["business_contract"]["path"] == "business-contract.md"
    assert context["potential_impacts"]
    assert context["version_evidence"]
    assert all(not item["path"].startswith("checks/") for item in context["source_files"])


def test_analysis_cannot_write_inside_case_through_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "nested"
    root.mkdir()
    immutable_root = tmp_path / "immutable"
    immutable_root.mkdir()
    case = SimpleNamespace(root=immutable_root, manifest=SimpleNamespace(file_hashes={}))

    work_root = root / ".." / "immutable" / "outputs"
    with patch.object(planning, "load_case", return_value=case), patch.object(
        planning, "analyze_case", return_value={"findings": []}
    ), pytest.raises(CaseValidationError, match="outside the immutable case"):
        planning.create_analysis(CALIBRATION, work_root)
    assert not (immutable_root / "outputs").exists()


def test_generic_agent_still_receives_frozen_migration_versions(tmp_path: Path) -> None:
    report = planning.prepare_case_proposal(
        APPLICATION, tmp_path / "work", model="unit-test-model",
        endpoint="https://provider.example/v1/chat/completions", max_output_tokens=1234,
        timeout_seconds=37, public_source_ack=True, output_format="agent_actions",
        experiment_arm="generic", task_context={"task_id": "task-1", "attempt_index": 1},
    )
    request = json.loads(Path(report["request_path"]).read_text(encoding="utf-8"))
    context = json.loads(request["messages"][1]["content"])
    assert context["migration_versions"] == {
        "package": "pydantic", "old_version": "1.10.24", "new_version": "2.11.7",
    }
    assert not context["potential_impacts"]
    assert not context["version_evidence"]
    assert report["analysis_findings_validated"] > 0
    assert report["experiment_arm"] == "generic"
