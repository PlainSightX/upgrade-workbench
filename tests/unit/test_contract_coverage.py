"""Protocol 5 的确定性合同门禁；这些测试不执行目标代码或模型调用。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.cases.requirements import ContractRequirement
from upgrade_workbench.diagnostics import (
    accept_action,
    context_from_reference,
    finish_contract_decision,
    freeze_context,
    public_fact,
    resolve_refs,
)
from upgrade_workbench.generation import ProposalInputError, complete_request
from upgrade_workbench.generation.actions import AgentActionError
from upgrade_workbench.generation.protocol_v5 import validate as validate_protocol_5
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.tasks import advance_task, create_operation, inspect_task

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CASE = ROOT / "cases/bump-my-version-0.5.0-r2"


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _write_v2_case(tmp_path: Path) -> Path:
    root = tmp_path / "case-v2"
    contract = b"# Public contract\n\nAccept list and tuple inputs.\n"
    catalog = {
        "schema_version": 1,
        "contract_path": "business-contract.md",
        "contract_sha256": _digest(contract),
        "requirements": [
            {
                "id": "input.list",
                "statement": "A list input remains valid.",
                "public_check_nodeids": ["feedback/test_contract.py::test_list_input"],
            },
            {
                "id": "input.tuple",
                "statement": "A tuple input remains valid.",
                "public_check_nodeids": ["feedback/test_contract.py::test_tuple_input"],
            },
        ],
    }
    files = {
        "source/demo.py": b"def normalize(value):\n    return value\n",
        "checks/feedback/test_contract.py": (
            b"def test_list_input():\n    assert True\n\n"
            b"def test_tuple_input():\n    assert True\n"
        ),
        "requirements/old.txt": b"pydantic==1.10.15\n",
        "requirements/new.txt": b"pydantic==2.6.4\n",
        "SOURCE.json": b'{"kind":"synthetic_calibration"}\n',
        "business-contract.md": contract,
        "contract-requirements.json": json.dumps(catalog, sort_keys=True).encode(),
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    manifest = {
        "schema_version": 2,
        "case_id": "contract-v2-fixture",
        "title": "Protocol 5 temporary contract fixture",
        "source": {
            "kind": "synthetic_calibration",
            "repository": "local-fixture",
            "revision": "fixture-v2",
            "license": "MIT",
            "description": "Offline protocol validation only",
        },
        "review": {"status": "reviewed", "note": "Tests only; no capability claim"},
        "snapshot_dir": "source",
        "checks_dir": "checks",
        "old_lock": "requirements/old.txt",
        "new_lock": "requirements/new.txt",
        "allowed_changes": ["demo.py"],
        "file_hashes": {name: _digest(contents) for name, contents in files.items()},
        "expected_new_original": "failed",
        "requirements_path": "contract-requirements.json",
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _replace_catalog(manifest_path: Path, update) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = manifest_path.parent / "contract-requirements.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    update(catalog)
    contents = json.dumps(catalog, sort_keys=True).encode()
    path.write_bytes(contents)
    manifest["file_hashes"]["contract-requirements.json"] = _digest(contents)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _copy_as_v2_case(tmp_path: Path) -> Path:
    root = tmp_path / "copied-v2-case"
    shutil.copytree(LEGACY_CASE, root)
    check_name = "checks/feedback/test_protocol_v5.py"
    check = root / check_name
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text("def test_registered_behavior():\n    assert True\n", encoding="utf-8")
    contract = (root / "business-contract.md").read_bytes()
    catalog = {
        "schema_version": 1,
        "contract_path": "business-contract.md",
        "contract_sha256": _digest(contract),
        "requirements": [
            {
                "id": "release.registered-behavior",
                "statement": "The registered public behavior remains valid.",
                "public_check_nodeids": ["feedback/test_protocol_v5.py::test_registered_behavior"],
            }
        ],
    }
    catalog_bytes = json.dumps(catalog, sort_keys=True).encode()
    (root / "contract-requirements.json").write_bytes(catalog_bytes)
    # 复制出的夹具新增公开检查后，同步唯一分组登记；原案例保持不变。
    evaluation_path = root / "evaluation.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    feedback = evaluation["groups"]["feedback"]
    feedback["paths"].append(check_name.removeprefix("checks/"))
    feedback["nodeids"].append("feedback/test_protocol_v5.py::test_registered_behavior")
    feedback["expected_count"] = len(feedback["nodeids"])
    evaluation_bytes = json.dumps(evaluation, sort_keys=True).encode()
    evaluation_path.write_bytes(evaluation_bytes)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(schema_version=2, requirements_path="contract-requirements.json")
    manifest["file_hashes"].update(
        {
            check_name: _digest(check.read_bytes()),
            "contract-requirements.json": _digest(catalog_bytes),
            "evaluation.json": _digest(evaluation_bytes),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _ledger(path: Path) -> None:
    BudgetLedger(
        path,
        {
            "mode": "user_managed",
            "limit_usd": None,
            "model": "owned-model",
            "input_per_million": "1",
            "output_per_million": "1",
            "pricing_source": "https://example.test/pricing",
            "pricing_checked_at": "2026-09-20",
        },
    )


def _create_protocol_5_task(
    manifest: Path,
    tmp_path: Path,
    *,
    service_owner: str | None = None,
    max_calls: int = 8,
    contract_audit_policy: str | None = None,
) -> dict:
    ledger = tmp_path / "ledger.sqlite"
    _ledger(ledger)
    return create_operation(
        manifest,
        tmp_path / "work",
        budget_path=ledger,
        seed_strategy="none",
        protocol_revision=5,
        generation={
            "model": "owned-model",
            "endpoint": "https://provider.example/chat/completions",
            "thinking_mode": "disabled",
            "max_output_tokens": 1024,
            "timeout_seconds": 10,
        },
        max_calls=max_calls,
        service_owner=service_owner,
        contract_audit_policy=contract_audit_policy,
    )


def _create_protocol_6_task(
    manifest: Path,
    tmp_path: Path,
    *,
    max_calls: int = 8,
    investigator_policy: str = "disabled",
    workflow_profile: str = "workbench",
    service_owner: str | None = None,
) -> dict:
    ledger = tmp_path / "ledger-v6.sqlite"
    _ledger(ledger)
    return create_operation(
        manifest,
        tmp_path / "work-v6",
        budget_path=ledger,
        seed_strategy="none",
        protocol_revision=6,
        generation={
            "model": "owned-model",
            "endpoint": "https://provider.example/chat/completions",
            "thinking_mode": "disabled",
            "max_output_tokens": 1024,
            "timeout_seconds": 10,
        },
        max_calls=max_calls,
        contract_audit_policy="disabled",
        investigator_policy=investigator_policy,
        workflow_profile=workflow_profile,
        service_owner=service_owner,
        **({"context_assistance": "version_only", "navigation_assistance": "baseline"}
           if workflow_profile == "simple_tools" else {}),
    )


@pytest.mark.parametrize(
    "update,match",
    [
        (
            lambda catalog: catalog["requirements"].append(dict(catalog["requirements"][0])),
            "duplicate contract requirement ID",
        ),
        (
            lambda catalog: catalog["requirements"][0].update(
                public_check_nodeids=["feedback/missing.py::test_missing"]
            ),
            "Unknown public check nodeid path",
        ),
        (
            lambda catalog: catalog.update(contract_sha256="0" * 64),
            "contract SHA does not match",
        ),
    ],
)
def test_manifest_v2_rejects_invalid_catalog(tmp_path, update, match):
    manifest = _write_v2_case(tmp_path)
    _replace_catalog(manifest, update)

    with pytest.raises(CaseValidationError, match=match):
        load_case(manifest)


def _requirements() -> list[ContractRequirement]:
    return [
        ContractRequirement(
            id="input.list",
            statement="A list input remains valid.",
            public_check_nodeids=["feedback/test_contract.py::test_list_input"],
        ),
        ContractRequirement(
            id="input.tuple",
            statement="A tuple input remains valid.",
            public_check_nodeids=["feedback/test_contract.py::test_tuple_input"],
        ),
    ]


def _context(*, runs: int = 8) -> dict:
    return {
        "contract_requirements": {"sha256": "a" * 64},
        "remaining_diagnostic_runs": runs,
        "diagnostic_options": {
            "new_probe_definition_available": runs > 0,
            "revisable_probe_ids": [],
            "reusable_probe_ids": [],
        },
    }


def _coverage(*, state: str = "supported", refs: dict[str, list[str]] | None = None) -> list[dict]:
    refs = refs or {}
    return [
        {
            "requirement_id": requirement.id,
            "state": state,
            "evidence_refs": refs.get(requirement.id, ["business_contract"]),
            "tool_limitation": None,
        }
        for requirement in _requirements()
    ]


def _finish(coverage: list[dict], *, reason: str = "candidate_ready") -> dict:
    return {
        "type": "finish",
        "reason": reason,
        "explanation": "Offline contract decision",
        "evidence_refs": [],
        "contract_coverage": coverage,
    }


@pytest.mark.parametrize(
    "coverage,field,value",
    [
        (_coverage()[:1], "missing_requirement_ids", ["input.tuple"]),
        (
            _coverage()
            + [{"requirement_id": "input.unknown", "state": "unobserved", "evidence_refs": [], "tool_limitation": None}],
            "unknown_requirement_ids",
            ["input.unknown"],
        ),
        (_coverage() + [_coverage()[0]], "duplicate_requirement_ids", ["input.list"]),
    ],
)
def test_finish_requires_exact_catalog_set(coverage, field, value):
    result = finish_contract_decision(_finish(coverage), _context(), _requirements(), [], "candidate:r1")

    assert result["accepted"] is False
    assert result["code"] == "contract_requirement_set_mismatch"
    assert result[field] == value


def test_fastapi_utils_style_solver_contradiction_is_blocked():
    observations = [
        {
            "id": "counterexample-1",
            "kind": "probe",
            "revision": "candidate:r1",
            "action": {"requirement_id": "input.list"},
            "result": {
                "status": "passed",
                "assessment": {"conclusion": "counterexample_observed"},
            },
        }
    ]
    result = finish_contract_decision(
        _finish(_coverage()), _context(), _requirements(), observations, "candidate:r1"
    )

    assert result["accepted"] is False
    assert result["code"] == "contract_not_ready_for_success"
    assert result["counterexample_requirement_ids"] == ["input.list"]


def test_jwt_style_narrow_public_observation_does_not_prove_broader_requirements():
    observations = [
        {
            "id": "public-1",
            "kind": "public_checks",
            "revision": "candidate:r1",
            "action": {"type": "run_public_checks"},
            "result": {
                "status": "passed",
                "stages": {
                    "new_candidate": {
                        "status": "passed",
                        "nodeids": ["test_contract.py::test_list_input"],
                    }
                },
            },
        }
    ]
    coverage = _coverage(
        refs={"input.list": ["observation:public-1"], "input.tuple": ["observation:public-1"]}
    )
    result = finish_contract_decision(
        _finish(coverage), _context(), _requirements(), observations, "candidate:r1"
    )

    assert result["accepted"] is False
    assert result["unobserved_requirement_ids"] == ["input.tuple"]


def test_final_only_unobserved_requirement_does_not_block_candidate_submission():
    requirements = [
        _requirements()[0],
        ContractRequirement(
            id="input.final-only",
            statement="An independently scored behavior remains valid.",
            public_check_nodeids=[],
        ),
    ]
    observation = {
        "id": "public-ready",
        "kind": "public_checks",
        "revision": "candidate:r1",
        "action": {"type": "run_public_checks"},
        "result": {
            "status": "passed",
            "stages": {
                "new_candidate": {
                    "status": "passed",
                    "nodeids": ["test_contract.py::test_list_input"],
                }
            },
        },
    }
    coverage = [
        {
            "requirement_id": "input.list",
            "state": "supported",
            "evidence_refs": ["observation:public-ready"],
            "tool_limitation": None,
        },
        {
            "requirement_id": "input.final-only",
            "state": "unobserved",
            "evidence_refs": [],
            "tool_limitation": None,
        },
    ]

    result = finish_contract_decision(
        _finish(coverage), _context(), requirements, [observation], "candidate:r1"
    )

    assert result["accepted"] is True
    assert result["code"] == "development_requirements_structurally_supported"
    assert result["unobserved_development_requirement_ids"] == []
    assert result["unobserved_final_acceptance_requirement_ids"] == ["input.final-only"]


def test_final_only_current_counterexample_still_blocks_candidate_submission():
    requirements = [
        _requirements()[0],
        ContractRequirement(
            id="input.final-only",
            statement="An independently scored behavior remains valid.",
            public_check_nodeids=[],
        ),
    ]
    observations = [
        {
            "id": "public-ready",
            "kind": "public_checks",
            "revision": "candidate:r1",
            "action": {"type": "run_public_checks"},
            "result": {
                "status": "passed",
                "stages": {
                    "new_candidate": {
                        "status": "passed",
                        "nodeids": ["test_contract.py::test_list_input"],
                    }
                },
            },
        },
        {
            "id": "final-counterexample",
            "kind": "probe",
            "revision": "candidate:r1",
            "action": {"requirement_id": "input.final-only"},
            "result": {
                "status": "passed",
                "assessment": {"conclusion": "counterexample_observed"},
            },
        },
    ]
    coverage = [
        {
            "requirement_id": "input.list",
            "state": "supported",
            "evidence_refs": ["observation:public-ready"],
            "tool_limitation": None,
        },
        {
            "requirement_id": "input.final-only",
            "state": "counterexample",
            "evidence_refs": ["observation:final-counterexample"],
            "tool_limitation": None,
        },
    ]

    result = finish_contract_decision(
        _finish(coverage), _context(), requirements, observations, "candidate:r1"
    )

    assert result["accepted"] is False
    assert result["counterexample_requirement_ids"] == ["input.final-only"]


@pytest.mark.parametrize(
    "observation",
    [
        {
            "id": "stale",
            "kind": "probe",
            "revision": "candidate:old",
            "action": {"requirement_id": "input.list"},
            "result": {"status": "passed", "assessment": {"conclusion": "no_counterexample_observed"}},
        },
        {
            "id": "wrong-requirement",
            "kind": "probe",
            "revision": "candidate:r1",
            "action": {"requirement_id": "input.tuple"},
            "result": {"status": "passed", "assessment": {"conclusion": "no_counterexample_observed"}},
        },
    ],
)
def test_stale_or_wrong_requirement_observation_cannot_support_claim(observation):
    coverage = _coverage(refs={"input.list": ["observation:" + observation["id"]]})
    result = finish_contract_decision(
        _finish(coverage), _context(), _requirements(), [observation], "candidate:r1"
    )

    assert result["accepted"] is False
    assert "input.list" in result["unobserved_requirement_ids"]


def test_current_prebound_public_checks_accept_clean_copier_style_candidate():
    observation = {
        "id": "public-clean",
        "kind": "public_checks",
        "revision": "candidate:r1",
        "action": {"type": "run_public_checks"},
        "result": {
            "status": "passed",
            "stages": {
                "new_candidate": {
                    "status": "passed",
                    "nodeids": [
                        "test_contract.py::test_list_input",
                        "test_contract.py::test_tuple_input",
                    ],
                }
            },
        },
    }
    refs = {requirement.id: ["observation:public-clean"] for requirement in _requirements()}
    result = finish_contract_decision(
        _finish(_coverage(refs=refs)), _context(), _requirements(), [observation], "candidate:r1"
    )

    assert result["accepted"] is True
    assert result["code"] == "all_requirements_structurally_supported"
    assert result["unobserved_requirement_ids"] == []
    assert result["counterexample_requirement_ids"] == []


def test_protocol_5_operation_freezes_manifest_v2_catalog(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path)

    loaded = inspect_task(Path(task["task_path"]))
    assert loaded["schema_version"] == 4
    assert loaded["protocol"]["protocol_revision"] == 5
    assert loaded["contract_requirements"]["requirement_ids"] == ["release.registered-behavior"]
    assert loaded["contract_requirements"] == loaded["protocol"]["contract_requirements"]
    assert loaded["protocol"]["contract_audit_policy"] == "bounded"
    assert loaded["attempts"] == []


def test_protocol_5_can_freeze_disabled_contract_audit_policy(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, contract_audit_policy="disabled")

    loaded = inspect_task(Path(task["task_path"]))
    assert loaded["protocol"]["contract_audit_policy"] == "disabled"


def test_protocol_5_rejects_unknown_contract_audit_policy_before_registration(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    with pytest.raises(ValueError, match="contract audit policy"):
        _create_protocol_5_task(manifest, tmp_path, contract_audit_policy="sometimes")
    assert not list((tmp_path / "work").rglob("task.json"))


def _prepared_protocol_5_request(tmp_path: Path) -> tuple[dict, bytes]:
    from upgrade_workbench.diagnostics import freeze_context

    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path)
    protocol = task["protocol"]
    prepared = prepare_case_proposal(
        manifest,
        Path(task["work_root"]),
        public_source_ack=True,
        experiment_arm="full",
        output_format="contract_actions",
        candidate_reference=task["current_candidate"],
        task_context={"task_id": task["task_id"], "attempt_index": 1, "remaining_calls": 8},
        source_policy=protocol["source_policy"],
        diagnostic_context_reference=freeze_context(task),
        **protocol["generation"],
    )
    contents = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, contents)
    return prepared, contents


def _replace_context(contents: bytes, update) -> bytes:
    payload = json.loads(contents)
    context = json.loads(payload["messages"][1]["content"])
    update(context)
    payload["messages"][1]["content"] = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_request_rejects_tampered_catalog_receipt_hash(tmp_path):
    prepared, contents = _prepared_protocol_5_request(tmp_path)
    prepared["input_hashes"]["contract-requirements.json"] = "0" * 64

    with pytest.raises(ProposalInputError, match="contract requirements"):
        _verify_payload(prepared, contents)


def test_request_rejects_tampered_catalog_content(tmp_path):
    prepared, contents = _prepared_protocol_5_request(tmp_path)
    tampered = _replace_context(
        contents,
        lambda context: context["contract_requirements"]["requirements"][0].update(
            statement="A broader behavior that the registered catalog never stated."
        ),
    )

    with pytest.raises(ProposalInputError, match="contract requirements"):
        _verify_payload(prepared, tampered)


def test_request_rejects_tampered_candidate_revision(tmp_path):
    prepared, contents = _prepared_protocol_5_request(tmp_path)
    tampered = _replace_context(
        contents,
        lambda context: context["candidate"].update(revision="candidate:forged"),
    )

    with pytest.raises(ProposalInputError, match="candidate metadata"):
        _verify_payload(prepared, tampered)


def _provider_response(content: dict) -> bytes:
    return json.dumps(
        {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(content)},
                }
            ],
        }
    ).encode()


def _submit_candidate(task: dict, label: str) -> dict:
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()
    action = {
        "type": "submit_candidate",
        "base_revision": snapshot.revision,
        "edits": [{"path": name, "old": old, "new": f"# {label}\n" + old}],
    }
    return advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response({"summary": label, "action": action}),
    )


def _feedback_comparator(*, passed: bool):
    def comparator(manifest, directory, **kwargs):
        candidate_sha = kwargs["expected_candidate_sha256"]
        output = directory / "offline-restore-feedback" / candidate_sha[:16]
        output.mkdir(parents=True, exist_ok=False)
        nodeid = "feedback/test_protocol_v5.py::test_registered_behavior"
        passing = {
            "status": "passed",
            "exit_code": 0,
            "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "nodeids": [nodeid],
        }
        failing = {
            "status": "failed",
            "exit_code": 1,
            "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
            "nodeids": [nodeid],
            "failure_details": [
                {
                    "nodeid": nodeid,
                    "outcome": "failed",
                    "when": "call",
                    "message": "Registered behavior still fails on this candidate revision.",
                }
            ],
        }
        report = {
            "case_fingerprint": load_case(manifest).fingerprint,
            "check_group": kwargs["check_group"],
            "status": "candidate_verified" if passed else "candidate_not_accepted",
            "stages": {
                "old_original": passing,
                "new_original": failing,
                "new_candidate": passing if passed else failing,
            },
            "candidate": {"supplied_sha256": candidate_sha},
            "report_path": str(output / "report.json"),
        }
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    return comparator


def _review_candidate(task: dict, *, passed: bool) -> dict:
    candidate = task["candidate"]
    return advance_task(
        Path(task["task_path"]),
        reviewed_revision=candidate["revision"],
        reviewed_sha256=candidate["sha256"],
        reviewer="offline-reviewer",
        review_note="Exact offline candidate review for restoration regression coverage.",
        review_only=True,
        comparator=_feedback_comparator(passed=passed),
    )


def _dependency_action(*, operation: str = "inspect_symbol", qualname: str = "Row") -> dict:
    if operation == "list_files":
        return {
            "type": "query_dependency",
            "environment": "old",
            "operation": "list_files",
            "distribution": "demo-dependency",
            "prefix": "demo",
        }
    return {
        "type": "query_dependency",
        "environment": "old",
        "operation": "inspect_symbol",
        "distribution": "demo-dependency",
        "module": "demo.api",
        "qualname": qualname,
    }


def _dependency_runner(image: str, *, status: str = "observed", tamper: bool = False):
    def run(case, action, directory, **_kwargs):
        lock = case.old_lock if action["environment"] == "old" else case.new_lock
        lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
        directory.mkdir(parents=True, exist_ok=False)
        public = {
            "status": status,
            "environment": {
                "role": action["environment"],
                "lock_sha256": lock_sha256,
                "image_id": "sha256:" + image * 64,
                "base_image_id": "sha256:" + "f" * 64,
                "python_version": "3.11.9",
                "cache_key": "c" * 64,
                "preparation_sha256": "d" * 64,
            },
            "distribution": action["distribution"],
            "installed_version": "1.0.0",
            "operation": action["operation"],
            "result": {"operation_confirmed": action["operation"]},
            "limitations": ["Offline injected runner; no runtime import was performed."],
        }
        if status == "unavailable":
            public.update(
                installed_version=None,
                result=None,
                code="distribution_not_installed",
                limitations=["The selected distribution is unavailable in the owned fixture."],
            )
        report_path = directory / "report.json"
        report = {
            "schema_version": 1,
            "kind": "dependency_query",
            "case_fingerprint": case.fingerprint,
            "action": action,
            "lock_sha256": lock_sha256,
            "query_id": hashlib.sha256(
                json.dumps(action, sort_keys=True).encode()
            ).hexdigest(),
            "report_path": str(report_path),
            "status": status,
            "public": None if status == "execution_incomplete" else public,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        if tamper:
            report_path.write_text(json.dumps(report | {"status": "tampered"}), encoding="utf-8")
        return report

    return run


def test_protocol_6_dependency_fact_is_idempotent_and_bound_to_environment(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    first_action = _dependency_action()
    before_runs = len(task["diagnostic_runs"])
    before_probes = len(task["probes"])

    task = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Inspect the locked dependency.", "action": first_action}
        ),
    )
    assert task["status"] == "pending_dependency_query"
    assert len(task["diagnostic_runs"]) == before_runs
    assert len(task["probes"]) == before_probes

    task = advance_task(
        Path(task["task_path"]), dependency_query_runner=_dependency_runner("a")
    )
    first_reference = task["dependency_facts"][0]
    assert task["status"] == "ready"
    assert public_fact(task, first_reference)["stale"] is False
    resolve_refs(task, ["fact:" + first_reference["id"]])

    task = _submit_candidate(task, "fact-does-not-follow-candidate-revision")
    assert public_fact(task, first_reference)["stale"] is False
    task = _review_candidate(task, passed=False)

    second_action = _dependency_action(operation="list_files")
    task = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Refresh the registered environment.", "action": second_action}
        ),
    )
    task = advance_task(
        Path(task["task_path"]), dependency_query_runner=_dependency_runner("b")
    )
    assert public_fact(task, first_reference)["stale"] is True
    assert public_fact(task, task["dependency_facts"][-1])["stale"] is False

    query_count = len(task["dependency_queries"])
    assert accept_action(
        task,
        {"action": first_action, "report_path": task["task_path"]},
        load_case(manifest),
    ) is True
    assert len(task["dependency_queries"]) == query_count
    assert task["dependency_query_reused"] is True
    assert accept_action(
        task,
        {
            "action": {"type": "get_fact", "fact_id": first_reference["id"]},
            "report_path": task["task_path"],
        },
        load_case(manifest),
    ) is True
    assert task["latest_fact"] == first_reference["id"]
    with pytest.raises(ValueError, match="Unknown public evidence"):
        resolve_refs(task, ["fact:" + "0" * 64])


def test_protocol_6_pending_query_uses_runner_not_model_and_terminal_never_replays(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    action = _dependency_action()
    model_calls = 0
    runner_calls = 0

    def transport(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return _provider_response({"summary": "Queue dependency query.", "action": action})

    task = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert task["status"] == "pending_dependency_query" and model_calls == 1

    def runner(*_args, **_kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return _dependency_runner("c")(*_args, **_kwargs)

    completed = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=transport,
        dependency_query_runner=runner,
    )
    assert completed["status"] == "ready"
    assert model_calls == 1 and runner_calls == 1

    completed["status"] = "unresolved"
    Path(completed["task_path"]).write_text(
        json.dumps(completed, ensure_ascii=False), encoding="utf-8"
    )
    terminal = advance_task(
        Path(completed["task_path"]),
        api_key="unused",
        transport=transport,
        dependency_query_runner=runner,
    )
    assert terminal["status"] == "unresolved"
    assert model_calls == 1 and runner_calls == 1


@pytest.mark.parametrize(
    "runner,expected_status,has_fact",
    [
        (_dependency_runner("d", status="unavailable"), "ready", True),
        (_dependency_runner("e", status="execution_incomplete"), "ready", False),
        (_dependency_runner("f", tamper=True), "ready", False),
    ],
)
def test_protocol_6_dependency_report_status_and_receipt_integrity(
    tmp_path, runner, expected_status, has_fact,
):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    task = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Inspect dependency state.", "action": _dependency_action()}
        ),
    )
    task = advance_task(Path(task["task_path"]), dependency_query_runner=runner)

    assert task["status"] == expected_status
    assert bool(task["dependency_facts"]) is has_fact
    if has_fact:
        fact = public_fact(task, task["dependency_facts"][0])
        assert fact["status"] == "unavailable"
        assert fact["code"] == "distribution_not_installed"
    else:
        assert task["dependency_query_feedback"]["code"] == (
            "dependency_query_failed_no_automatic_replay"
        )
        failure = json.loads(
            Path(task["dependency_queries"][0]["failure_receipt"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        assert failure["error_type"]


@pytest.mark.parametrize("status", ["observed", "unavailable"])
def test_protocol_6_recovers_persisted_dependency_report_without_replay(tmp_path, status):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    action = _dependency_action()
    task = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Inspect dependency state.", "action": action}
        ),
    )
    request_id = task["pending_dependency_query"]["request"]["id"]
    directory = Path(task["task_path"]).parent / "dependency_query_runs" / request_id
    _dependency_runner("a", status=status)(load_case(manifest), action, directory)
    runner_calls = 0

    def forbidden_runner(*_args, **_kwargs):
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("Persisted dependency report must not be replayed")

    recovered = advance_task(
        Path(task["task_path"]), dependency_query_runner=forbidden_runner
    )

    assert runner_calls == 0
    assert recovered["status"] == "ready"
    assert recovered["dependency_queries"][0]["status"] == "completed"
    fact = public_fact(recovered, recovered["dependency_facts"][0])
    assert fact["status"] == status


@pytest.mark.parametrize(
    "damage",
    ["running", "partial", "execution_incomplete", "incomplete", "tampered", "identity"],
)
def test_protocol_6_persisted_nonterminal_or_invalid_report_requires_reconciliation(
    tmp_path, damage,
):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    action = _dependency_action()
    task = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Inspect dependency state.", "action": action}
        ),
    )
    request_id = task["pending_dependency_query"]["request"]["id"]
    directory = Path(task["task_path"]).parent / "dependency_query_runs" / request_id
    report = _dependency_runner("b", status="execution_incomplete")(
        load_case(manifest), action, directory
    )
    report_path = directory / "report.json"
    if damage in {"running", "partial"}:
        report["status"] = damage
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif damage == "incomplete":
        report.pop("public")
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif damage == "tampered":
        report_path.write_text("{not-json", encoding="utf-8")
    elif damage == "identity":
        report["case_fingerprint"] = "0" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")

    runner_calls = 0

    def forbidden_runner(*_args, **_kwargs):
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("Interrupted dependency report must not be replayed")

    recovered = advance_task(
        Path(task["task_path"]), dependency_query_runner=forbidden_runner
    )

    assert runner_calls == 0
    assert recovered["status"] == "execution_incomplete"
    assert recovered["pending_dependency_query"] is None
    assert recovered["dependency_facts"] == []
    row = recovered["dependency_queries"][0]
    assert row["status"] == "failed"
    assert row["reconciliation_required"] is True
    assert recovered["dependency_query_feedback"]["code"] == (
        "dependency_query_reconciliation_required"
    )
    failure = json.loads(Path(row["failure_receipt"]["path"]).read_text(encoding="utf-8"))
    assert failure["reconciliation_required"] is True


def test_protocol_6_solver_fact_investigator_handoff_and_solver_consumption(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(
        manifest, tmp_path, max_calls=5, investigator_policy="required_once"
    )
    case = load_case(manifest)
    initial = load_candidate(case, task["current_candidate"])
    dependency_action = _dependency_action()

    first = advance_task(
        Path(task["task_path"]),
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Solver requests a locked dependency fact.", "action": dependency_action}
        ),
    )
    assert first["attempts"][0]["role"] == "solver"
    assert first["status"] == "pending_dependency_query"

    first = advance_task(
        Path(first["task_path"]), dependency_query_runner=_dependency_runner("a")
    )
    fact_id = first["dependency_facts"][0]["id"]
    seen_roles = []
    seen_version_refs = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        role = (
            "investigator"
            if "You are an independent read-only Investigator."
            in payload["messages"][0]["content"]
            else "solver"
        )
        seen_roles.append(role)
        if seen_roles == ["investigator"]:
            assert context["diagnostic_state"]["latest_fact_id"] == fact_id
            seen_version_refs.append(
                "version:" + context["version_evidence"][0]["evidence_key"]
            )
            return _provider_response(
                {
                    "summary": "Investigator rereads the immutable fact.",
                    "action": {"type": "get_fact", "fact_id": fact_id},
                }
            )
        if seen_roles == ["investigator", "investigator"]:
            return _provider_response(
                {
                    "summary": "Investigator hands the sourced result back.",
                    "action": {
                        "type": "handoff",
                        "observed_facts": [
                            {
                                "statement": "The locked environment produced a dependency fact.",
                                "scope": "Only the registered dependency environment was queried.",
                                "evidence_refs": ["fact:" + fact_id, seen_version_refs[0]],
                            }
                        ],
                        "scope_limits": ["No application behavior was executed."],
                        "remaining_hypotheses": [],
                        "conflicts": [],
                        "next_discriminating_action": None,
                    },
                }
            )
        assert role == "solver"
        handoffs = context["diagnostic_state"]["investigator_handoffs"]
        if seen_roles == ["investigator", "investigator", "solver"]:
            assert len(handoffs) == 1
            assert handoffs[0]["underlying_evidence_refs"] == [
                "fact:" + fact_id,
                seen_version_refs[0],
            ]
        else:
            assert seen_roles == ["investigator", "investigator", "solver", "solver"]
            assert handoffs == []
        return _provider_response(
            {
                "summary": "Solver consumes the handed-off fact.",
                "action": {"type": "get_fact", "fact_id": fact_id},
            }
        )

    finished = advance_task(
        Path(first["task_path"]), api_key="unused", transport=transport
    )

    assert seen_roles == ["investigator", "investigator", "solver", "solver"]
    assert [attempt["role"] for attempt in finished["attempts"]] == [
        "solver",
        "investigator",
        "investigator",
        "solver",
        "solver",
    ]
    assert [attempt["output_format"] for attempt in finished["attempts"]] == [
        "protocol_v6_actions",
        "investigator_actions",
        "investigator_actions",
        "protocol_v6_actions",
        "protocol_v6_actions",
    ]
    assert finished["status"] == "unresolved"
    assert finished["stop_reason"] == "call_limit"
    current = load_candidate(case, finished["current_candidate"])
    assert current.revision == initial.revision
    assert current.sha256 == initial.sha256
    assert finished["candidate"] is None
    assert len(finished["investigator_handoffs"]) == 1
    assert len(finished["consumed_investigator_handoffs"]) == 1
    consumed = finished["consumed_investigator_handoffs"][0]
    assert consumed["handoff_id"] == finished["investigator_handoffs"][0]["id"]
    solver_receipt = json.loads(
        Path(finished["attempts"][-2]["receipt"]).read_text(encoding="utf-8")
    )
    assert consumed["context_id"] == solver_receipt["diagnostic_context_reference"]["id"]


def test_protocol_5_restore_candidate_requires_exact_public_evidence(tmp_path):
    manifest = _write_v2_case(tmp_path)
    case = load_case(manifest)
    snapshot = load_candidate(case)

    with pytest.raises(AgentActionError, match="requires one exposed public-pass observation"):
        validate_protocol_5(
            case,
            {
                "type": "restore_candidate",
                "revision": "a" * 64,
                "reason": "Recover from a later regression.",
                "evidence_refs": [],
            },
            snapshot,
        )


def test_protocol_5_finish_error_explains_enum_and_rejects_issues(tmp_path):
    manifest = _write_v2_case(tmp_path)
    case = load_case(manifest)
    snapshot = load_candidate(case)
    coverage = [{
        "requirement_id": "release.registered-behavior",
        "state": "unobserved",
        "evidence_refs": [],
        "tool_limitation": "No reviewed execution route remains in this bounded fixture.",
    }]

    with pytest.raises(AgentActionError, match="enum token.*candidate_ready"):
        validate_protocol_5(
            case,
            {
                "type": "finish",
                "reason": "submitted",
                "explanation": "Bounded finish.",
                "evidence_refs": [],
                "contract_coverage": coverage,
            },
            snapshot,
        )

    with pytest.raises(AgentActionError, match="does not accept issues"):
        validate_protocol_5(
            case,
            {
                "type": "finish",
                "reason": "unresolved",
                "explanation": "Bounded finish.",
                "evidence_refs": [],
                "contract_coverage": coverage,
                "issues": [],
            },
            snapshot,
        )


def test_protocol_5_exposes_and_restores_reviewed_public_passing_history(tmp_path):
    from upgrade_workbench.tasks import _save

    manifest = _copy_as_v2_case(tmp_path)
    task = _review_candidate(_submit_candidate(_create_protocol_5_task(manifest, tmp_path), "good"), passed=True)
    good = dict(task["candidate"])
    good_observation = "observation:" + task["latest_observation"]
    task = _review_candidate(_submit_candidate(task, "regression"), passed=False)
    bad = dict(task["candidate"])
    bad_observation = "observation:" + task["latest_observation"]
    case = load_case(manifest)
    current = load_candidate(case, task["current_candidate"])
    context = context_from_reference(freeze_context(task), case, current)

    assert context["restore_candidates"] == [{
        "revision": good["revision"],
        "patch_sha256": good["sha256"],
        "origin": "agent_candidate",
        "public_pass_observation_refs": [good_observation],
        "scope_limit": "Public feedback passed for these immutable bytes; independent final acceptance remains separate.",
    }]

    action = {
        "type": "restore_candidate",
        "revision": good["revision"],
        "reason": "The later reviewed revision regressed a public behavior already passed by this history entry.",
        "evidence_refs": [bad_observation],
    }
    with pytest.raises(ValueError, match="must cite one exposed public-pass observation"):
        accept_action(task, {"action": action}, case)

    history = json.loads(json.dumps(task["candidate_history"]))
    task["tool_results"] = [{"action": {"type": "read_source"}, "result": {"stale": True}}]
    action["evidence_refs"] = [good_observation]
    assert accept_action(task, {"action": action}, case) is True
    _save(task, Path(task["task_path"]))
    restored = inspect_task(Path(task["task_path"]))

    assert restored["current_candidate"]["revision"] == good["revision"]
    assert restored["candidate"]["revision"] == good["revision"]
    assert restored["candidate"]["sha256"] == good["sha256"]
    assert restored["candidate"]["reviewed"] is True
    assert restored["candidate_history"][:-1] == history
    assert restored["candidate_history"][-1]["restoration"] == {
        "from_revision": bad["revision"],
        "reason": action["reason"],
        "evidence_ref": good_observation,
    }
    assert restored["feedback"] is None
    assert restored["tool_results"] == []
    assert restored["latest_observation"] == good_observation.removeprefix("observation:")
    assert len(restored["observations"]) == 2


def test_schema_5_exposes_partial_options_and_restores_exact_observed_base(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_6_task(manifest, tmp_path)
    nodeids = [f"feedback/test_protocol_v5.py::test_case_{index}" for index in range(6)]

    def comparator(failed_indexes):
        failed = {nodeids[index] for index in failed_indexes}

        def run(manifest_path, directory, **kwargs):
            candidate_sha = kwargs["expected_candidate_sha256"]
            output = directory / "schema-5-feedback" / candidate_sha[:16]
            output.mkdir(parents=True, exist_ok=False)

            def stage(stage_failed):
                failures = [nodeid for nodeid in nodeids if nodeid in stage_failed]
                return {
                    "status": "failed" if failures else "passed",
                    "exit_code": 1 if failures else 0,
                    "tests": {
                        "collected": 6,
                        "passed": 6 - len(failures),
                        "failed": len(failures),
                        "errors": 0,
                        "skipped": 0,
                    },
                    "nodeids": nodeids,
                    "failure_details": [
                        {
                            "nodeid": nodeid,
                            "outcome": "failed",
                            "when": "call",
                            "message": f"{nodeid} remains a public counterexample.",
                        }
                        for nodeid in failures
                    ],
                }

            report = {
                "case_fingerprint": load_case(manifest_path).fingerprint,
                "check_group": kwargs["check_group"],
                "status": "candidate_not_accepted",
                "stages": {
                    "old_original": stage(set()),
                    "new_original": stage(set(nodeids)),
                    "new_candidate": stage(failed),
                },
                "candidate": {"supplied_sha256": candidate_sha},
                "report_path": str(output / "report.json"),
            }
            Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
            return report

        return run

    task = _submit_candidate(task, "partial-three")
    partial = dict(task["candidate"])
    task = advance_task(
        Path(task["task_path"]),
        reviewed_revision=partial["revision"],
        reviewed_sha256=partial["sha256"],
        reviewer="offline-reviewer",
        review_note="Reviewed partial schema 5 candidate.",
        review_only=True,
        comparator=comparator({0, 1, 2}),
    )
    partial_observation = "observation:" + task["latest_observation"]
    task = _submit_candidate(task, "regressed-zero")
    regressed = dict(task["candidate"])
    task = advance_task(
        Path(task["task_path"]),
        reviewed_revision=regressed["revision"],
        reviewed_sha256=regressed["sha256"],
        reviewer="offline-reviewer",
        review_note="Reviewed regressed schema 5 candidate.",
        review_only=True,
        comparator=comparator(set(range(6))),
    )
    regressed_observation = "observation:" + task["latest_observation"]
    case = load_case(manifest)
    current = load_candidate(case, task["current_candidate"])
    context = context_from_reference(freeze_context(task), case, current)

    assert "restore_candidates" not in context
    assert [item["passed_count"] for item in context["candidate_options"]] == [3, 0]
    assert context["recommended_candidate"]["revision"] == partial["revision"]
    assert context["recommended_candidate"]["public_observation_ref"] == partial_observation
    assert context["candidate_options"][1]["current_edit_base"] is True
    assert all("candidate_reference" not in item for item in context["candidate_options"])

    action = {
        "type": "restore_candidate",
        "revision": partial["revision"],
        "reason": "Return to the reviewed three-of-six base before the next bounded edit.",
        "evidence_refs": [regressed_observation],
    }
    with pytest.raises(ValueError, match="complete public observation"):
        accept_action(task, {"action": action, "report_path": "offline"}, case)

    observations_before = json.loads(json.dumps(task["observations"]))
    history_before = json.loads(json.dumps(task["candidate_history"]))
    action["evidence_refs"] = [partial_observation]
    assert accept_action(task, {"action": action, "report_path": "offline"}, case) is True

    assert task["current_candidate"]["revision"] == partial["revision"]
    assert task["candidate"]["revision"] == partial["revision"]
    assert task["observations"] == observations_before
    assert task["candidate_history"][:-1] == history_before
    assert task["candidate_history"][-1]["restoration"]["evidence_ref"] == partial_observation


def test_schema_5_terminal_task_cannot_be_revived_by_candidate_restore(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _review_candidate(
        _submit_candidate(_create_protocol_5_task(manifest, tmp_path), "terminal"),
        passed=True,
    )
    task["schema_version"] = 5
    task["status"] = "unresolved"
    before = json.loads(json.dumps(task))
    case = load_case(manifest)
    current = load_candidate(case, task["current_candidate"])

    with pytest.raises(ValueError, match="Terminal task history is read-only"):
        from upgrade_workbench.diagnostics import restore_candidate

        restore_candidate(
            task,
            case,
            {
                "type": "restore_candidate",
                "revision": current.revision,
                "reason": "Do not revive a terminal task.",
                "evidence_refs": ["observation:" + task["latest_observation"]],
            },
            current,
        )

    assert task == before


def test_protocol_5_reserves_last_call_for_finish(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, max_calls=2)
    task = _review_candidate(_submit_candidate(task, "last-call"), passed=True)
    observation = "observation:" + task["latest_observation"]

    def finish_transport(request, **_kwargs):
        payload = json.loads(request.data)
        public_context = json.loads(payload["messages"][1]["content"])
        feedback = public_context["task_context"]["edit_feedback"]
        assert "FINAL_CALL_FINISH_ONLY" in feedback
        assert public_context["task_context"]["remaining_calls"] == 1
        return _provider_response({
            "summary": "Use the reserved final call.",
            "action": {
                "type": "finish",
                "reason": "candidate_ready",
                "explanation": "The current reviewed candidate is ready for independent acceptance.",
                "evidence_refs": [observation],
                "contract_coverage": [{
                    "requirement_id": "release.registered-behavior",
                    "state": "supported",
                    "evidence_refs": [observation],
                    "tool_limitation": None,
                }],
            },
        })

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=finish_transport)

    assert result["status"] == "submitted"
    assert len(result["attempts"]) == 2
    assert result["finish"]["reason"] == "candidate_ready"


def test_protocol_5_response_can_cite_bound_version_evidence(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path)

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        reference = "version:" + context["version_evidence"][0]["evidence_key"]
        block = next(
            item for item in context["source_files"]
            if item["path"] in context["allowed_changes"]
        )
        action = {
            "type": "submit_candidate",
            "base_revision": context["candidate"]["revision"],
            "edits": [{
                "path": block["path"],
                "old": block["text"],
                "new": "# Protocol 5 offline candidate\n" + block["text"],
            }],
            "issues": [{
                "hypothesis": "The migration evidence identifies a bounded compatibility risk.",
                "evidence_refs": [reference],
                "unknown": "Runtime behavior remains unobserved.",
                "next_observation": "Run the registered public check.",
            }],
        }
        return _provider_response({"summary": "Protocol 5 candidate", "action": action})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert result["status"] == "pending_review"
    assert len(result["attempts"]) == 1
    assert result["candidate"]["revision"] == result["current_candidate"]["revision"]


def test_contract_auditor_response_is_read_only(tmp_path):
    prepared, _ = _prepared_protocol_5_request(tmp_path)
    prepared_path = Path(prepared["report_path"])
    prepared["output_format"] = "contract_audit"
    request = json.loads(Path(prepared["request_path"]).read_bytes())
    from upgrade_workbench.generation.request import _instructions

    request["messages"][0]["content"] = _instructions("contract_audit")
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode()
    Path(prepared["request_path"]).write_bytes(body)
    prepared["request_sha256"] = _digest(body)
    prepared_path.write_text(json.dumps(prepared, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    response = {
        "summary": "One requirement needs a narrower observation.",
        "audit": {
            "verdict": "specific_questions",
            "questions": [
                {
                    "requirement_id": "release.registered-behavior",
                    "evidence_refs": ["business_contract"],
                    "specific_conflict": "The current public observation does not exercise the registered branch.",
                    "suggested_observation": "Run one reviewed public check for the registered branch.",
                }
            ],
        },
    }

    result = complete_request(
        prepared,
        api_key="offline-test-key",
        transport=lambda *_args, **_kwargs: _provider_response(response),
    )

    assert result["status"] == "audit_ready"
    assert result["target_code_executed"] is False
    assert "candidate_patch" not in result
    assert not prepared_path.with_name("candidate.patch").exists()


def test_contract_auditor_instruction_checks_failure_paths_for_stateful_recovery():
    from upgrade_workbench.generation.contract_auditor import instructions

    prompt = instructions()
    assert "error-recovery requirements against failure paths" in prompt
    assert "long-lived connection commits but has no visible rollback" in prompt
    assert "happy-path writes commit" in prompt


def test_contract_auditor_rejects_unregistered_reference_syntax(tmp_path):
    prepared, _ = _prepared_protocol_5_request(tmp_path)
    prepared_path = Path(prepared["report_path"])
    prepared["output_format"] = "contract_audit"
    request = json.loads(Path(prepared["request_path"]).read_bytes())
    from upgrade_workbench.generation.request import _instructions

    request["messages"][0]["content"] = _instructions("contract_audit")
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode()
    Path(prepared["request_path"]).write_bytes(body)
    prepared["request_sha256"] = _digest(body)
    prepared_path.write_text(json.dumps(prepared, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    response = {
        "summary": "Review the current dependency behavior.",
        "audit": {
            "verdict": "specific_questions",
            "questions": [
                {
                    "requirement_id": "release.registered-behavior",
                    "evidence_refs": ["evidence:migration.md#pydantic-v2"],
                    "specific_conflict": "The current candidate may not preserve the registered behavior.",
                    "suggested_observation": "Run the registered public check against the current revision.",
                }
            ],
        },
    }

    result = complete_request(
        prepared,
        api_key="offline-test-key",
        transport=lambda *_args, **_kwargs: _provider_response(response),
    )

    assert result["status"] == "provider_response_rejected"
    assert result["reason"] == "contract_audit_invalid_reference"
    assert "candidate_patch" not in result


def test_contract_auditor_reports_bounded_text_rejection_precisely(tmp_path):
    prepared, _ = _prepared_protocol_5_request(tmp_path)
    prepared_path = Path(prepared["report_path"])
    prepared["output_format"] = "contract_audit"
    request = json.loads(Path(prepared["request_path"]).read_bytes())
    from upgrade_workbench.generation.request import _instructions

    request["messages"][0]["content"] = _instructions("contract_audit")
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode()
    Path(prepared["request_path"]).write_bytes(body)
    prepared["request_sha256"] = _digest(body)
    prepared_path.write_text(json.dumps(prepared, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    response = {
        "summary": "The current revision has one bounded question.",
        "audit": {
            "verdict": "specific_questions",
            "questions": [
                {
                    "requirement_id": "release.registered-behavior",
                    "evidence_refs": ["business_contract"],
                    "specific_conflict": "x" * 701,
                    "suggested_observation": "Run one reviewed public check.",
                }
            ],
        },
    }

    result = complete_request(
        prepared,
        api_key="offline-test-key",
        transport=lambda *_args, **_kwargs: _provider_response(response),
    )

    assert result["status"] == "provider_response_rejected"
    assert result["reason"] == "contract_audit_text_too_long"
    assert "candidate_patch" not in result


def test_contract_auditor_rejects_embedded_edits(tmp_path):
    prepared, _ = _prepared_protocol_5_request(tmp_path)
    prepared_path = Path(prepared["report_path"])
    prepared["output_format"] = "contract_audit"
    request = json.loads(Path(prepared["request_path"]).read_bytes())
    from upgrade_workbench.generation.request import _instructions

    request["messages"][0]["content"] = _instructions("contract_audit")
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode()
    Path(prepared["request_path"]).write_bytes(body)
    prepared["request_sha256"] = _digest(body)
    prepared_path.write_text(json.dumps(prepared, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    response = {
        "summary": "Attempted scope expansion.",
        "audit": {
            "verdict": "no_specific_conflict",
            "questions": [],
            "edits": [{"path": "demo.py", "old": "x", "new": "y"}],
        },
    }

    result = complete_request(
        prepared,
        api_key="offline-test-key",
        transport=lambda *_args, **_kwargs: _provider_response(response),
    )

    assert result["status"] == "provider_response_rejected"
    assert "candidate_patch" not in result


def test_protocol_5_task_runs_at_most_two_audits_in_same_attempt_budget(tmp_path):
    from upgrade_workbench.tasks import advance_task

    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path)
    seen_roles: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        if "read-only Contract Auditor" in system:
            seen_roles.append("auditor")
            if seen_roles.count("auditor") == 1:
                audit = {
                    "verdict": "specific_questions",
                    "questions": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "evidence_refs": ["business_contract"],
                            "specific_conflict": "No current observation exercises the registered behavior.",
                            "suggested_observation": "Run a reviewed public check for this requirement.",
                        }
                    ],
                }
            else:
                audit = {"verdict": "no_specific_conflict", "questions": []}
            return _provider_response({"summary": "Read-only audit", "audit": audit})
        seen_roles.append("solver")
        context = json.loads(payload["messages"][1]["content"])
        version_ref = "version:" + context["version_evidence"][0]["evidence_key"]
        if seen_roles.count("solver") == 2:
            feedback = context["task_context"]["edit_feedback"]
            assert "source-level risk" in feedback
            questions = context["diagnostic_state"]["contract_audits"][0]["audit"]["questions"]
            assert questions[0]["specific_conflict"] == (
                "No current observation exercises the registered behavior."
            )
        if seen_roles.count("solver") == 1:
            coverage = [
                {
                    "requirement_id": "release.registered-behavior",
                    "state": "supported",
                    "evidence_refs": [version_ref],
                    "tool_limitation": None,
                }
            ]
            reason = "candidate_ready"
        else:
            coverage = [
                {
                    "requirement_id": "release.registered-behavior",
                    "state": "unobserved",
                    "evidence_refs": [],
                    "tool_limitation": "No reviewed execution route remains in this bounded offline fixture.",
                }
            ]
            reason = "unresolved"
        action = {
            "type": "finish",
            "reason": reason,
            "explanation": "Bounded offline finish",
            "evidence_refs": [version_ref],
            "contract_coverage": coverage,
        }
        return _provider_response({"summary": "Solver finish", "action": action})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert seen_roles == ["solver", "auditor", "solver", "auditor"], result
    assert len(result["attempts"]) == 4
    assert [item.get("role", "solver") for item in result["attempts"]] == [
        "solver",
        "contract_auditor",
        "solver",
        "contract_auditor",
    ]
    assert len(result["contract_audits"]) == 2
    assert result["status"] == "unresolved"
    assert result["candidate"] is None


def test_rejected_finish_audit_retries_once_and_blocks_silent_submission(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _review_candidate(
        _submit_candidate(_create_protocol_5_task(manifest, tmp_path, max_calls=4), "audit-retry"),
        passed=True,
    )
    observation = "observation:" + task["latest_observation"]
    seen_roles: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        context = json.loads(payload["messages"][1]["content"])
        if "read-only Contract Auditor" in system:
            seen_roles.append("auditor")
            if seen_roles.count("auditor") == 2:
                feedback = context["task_context"]["protocol_feedback"]
                assert feedback == {
                    "code": "contract_audit_invalid_reference",
                    "attempt_index": 3,
                }
            return _provider_response(
                {
                    "summary": "Audit with an invalid reference form.",
                    "audit": {
                        "verdict": "specific_questions",
                        "questions": [
                            {
                                "requirement_id": "release.registered-behavior",
                                "evidence_refs": ["evidence:migration.md#pydantic-v2"],
                                "specific_conflict": "The current source may retain one incompatible behavior.",
                                "suggested_observation": "Run one bounded reviewed observation.",
                            }
                        ],
                    },
                }
            )
        seen_roles.append("solver")
        return _provider_response(
            {
                "summary": "Solver finish",
                "action": {
                    "type": "finish",
                    "reason": "candidate_ready",
                    "explanation": "The reviewed candidate passes its development check.",
                    "evidence_refs": [observation],
                    "contract_coverage": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "state": "supported",
                            "evidence_refs": [observation],
                            "tool_limitation": None,
                        }
                    ],
                },
            }
        )

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert seen_roles == ["solver", "auditor", "auditor"]
    assert result["status"] == "unresolved"
    assert result["stop_reason"] == "required_contract_audit_not_accepted"
    assert result["contract_audits"] == []
    assert result["contract_audit_failure"] == {
        "status": "rejected",
        "reason": "contract_audit_invalid_reference",
        "solver_finish_receipt": result["attempts"][-3]["receipt"],
    }
    assert result.get("finish") is None


def test_rejected_finish_audit_retry_can_recover_with_grounded_question(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    task = _review_candidate(
        _submit_candidate(_create_protocol_5_task(manifest, tmp_path, max_calls=6), "audit-recovery"),
        passed=True,
    )
    observation = "observation:" + task["latest_observation"]
    roles: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        if "read-only Contract Auditor" in system:
            roles.append("auditor")
            auditor_count = roles.count("auditor")
            if auditor_count == 1:
                audit = {
                    "verdict": "specific_questions",
                    "questions": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "evidence_refs": ["evidence:invalid"],
                            "specific_conflict": "Invalid reference fixture.",
                            "suggested_observation": "Retry the audit response format.",
                        }
                    ],
                }
            elif auditor_count == 2:
                audit = {
                    "verdict": "specific_questions",
                    "questions": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "evidence_refs": ["business_contract"],
                            "specific_conflict": "The visible source leaves one bounded behavior at risk.",
                            "suggested_observation": "Run one reviewed public observation.",
                        }
                    ],
                }
            else:
                audit = {"verdict": "no_specific_conflict", "questions": []}
            return _provider_response({"summary": "Read-only audit", "audit": audit})
        roles.append("solver")
        context = json.loads(payload["messages"][1]["content"])
        if roles.count("solver") == 2:
            assert context["diagnostic_state"]["contract_audits"][0]["audit"]["questions"]
        return _provider_response(
            {
                "summary": "Solver finish",
                "action": {
                    "type": "finish",
                    "reason": "candidate_ready",
                    "explanation": "The reviewed candidate is ready for independent acceptance.",
                    "evidence_refs": [observation],
                    "contract_coverage": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "state": "supported",
                            "evidence_refs": [observation],
                            "tool_limitation": None,
                        }
                    ],
                },
            }
        )

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert roles == ["solver", "auditor", "auditor", "solver", "auditor"]
    assert result["status"] == "submitted"
    assert len(result["contract_audits"]) == 2
    assert result["finish"]["reason"] == "candidate_ready"


def test_bounded_auditor_runs_before_solver_after_three_failed_revisions(tmp_path, monkeypatch):
    from upgrade_workbench import tasks

    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, max_calls=8)
    for index in range(3):
        task = _submit_candidate(task, f"failed-revision-{index}")
        task = _review_candidate(task, passed=False)

    seen_roles: list[str] = []
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        if "read-only Contract Auditor" in system:
            seen_roles.append("auditor")
            context = json.loads(payload["messages"][1]["content"])
            version_ref = "version:" + context["version_evidence"][0]["evidence_key"]
            audit = {
                "verdict": "specific_questions",
                "questions": [
                    {
                        "requirement_id": "release.registered-behavior",
                        "evidence_refs": ["business_contract", version_ref],
                        "specific_conflict": "The same public behavior failed on three reviewed revisions.",
                        "suggested_observation": "Compare the recurring failure evidence before another edit.",
                    }
                ],
            }
            return _provider_response({"summary": "Stagnation audit", "audit": audit})
        seen_roles.append("solver")
        context = json.loads(payload["messages"][1]["content"])
        assert "Contract Auditor raised" in context["task_context"]["edit_feedback"]
        action = {
            "type": "submit_candidate",
            "base_revision": snapshot.revision,
            "edits": [{"path": name, "old": old, "new": "# after-audit\n" + old}],
        }
        return _provider_response({"summary": "Candidate after audit", "action": action})

    monkeypatch.setattr(tasks, "_require_current_protocol", lambda _protocol: None)
    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert seen_roles == ["auditor", "solver"]
    assert len(result["contract_audits"]) == 1
    assert result["status"] == "pending_review"


def test_disabled_contract_audit_policy_never_calls_auditor(tmp_path):
    from upgrade_workbench.tasks import advance_task

    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, max_calls=2, contract_audit_policy="disabled")
    roles: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        assert "read-only Contract Auditor" not in system
        roles.append("solver")
        context = json.loads(payload["messages"][1]["content"])
        version_ref = "version:" + context["version_evidence"][0]["evidence_key"]
        action = {
            "type": "finish",
            "reason": "unresolved",
            "explanation": "No candidate was produced in this bounded fixture.",
            "evidence_refs": [version_ref],
            "contract_coverage": [
                {
                    "requirement_id": "release.registered-behavior",
                    "state": "unobserved",
                    "evidence_refs": [],
                    "tool_limitation": "No candidate behavior was available to observe.",
                }
            ],
        }
        return _provider_response({"summary": "Solver finish", "action": action})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert roles == ["solver"]
    assert len(result["attempts"]) == 1
    assert result["contract_audits"] == []
    assert result["status"] == "unresolved"


def test_final_only_audit_gap_does_not_force_unresolved(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    _replace_catalog(
        manifest,
        lambda catalog: catalog["requirements"].append(
            {
                "id": "release.final-only",
                "statement": "Independent final acceptance checks one additional behavior.",
                "public_check_nodeids": [],
            }
        ),
    )
    task = _review_candidate(
        _submit_candidate(_create_protocol_5_task(manifest, tmp_path), "final-only-advisory"),
        passed=True,
    )
    observation = "observation:" + task["latest_observation"]
    seen_roles: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        system = payload["messages"][0]["content"]
        if "read-only Contract Auditor" in system:
            seen_roles.append("auditor")
            audit = (
                {
                    "verdict": "specific_questions",
                    "questions": [
                        {
                            "requirement_id": "release.final-only",
                            "evidence_refs": ["business_contract"],
                            "specific_conflict": "No current observation covers the final-only behavior.",
                            "suggested_observation": "Leave this for independent final acceptance.",
                        }
                    ],
                }
                if seen_roles.count("auditor") == 1
                else {"verdict": "no_specific_conflict", "questions": []}
            )
            return _provider_response({"summary": "Read-only audit", "audit": audit})
        seen_roles.append("solver")
        context = json.loads(payload["messages"][1]["content"])
        if seen_roles.count("solver") == 2:
            feedback = context["task_context"]["edit_feedback"]
            assert "does not by itself block candidate_ready" in feedback
            assert "do not finish unresolved solely" in feedback
        action = {
            "type": "finish",
            "reason": "candidate_ready",
            "explanation": "Development checks pass; final-only behavior remains for independent acceptance.",
            "evidence_refs": [observation],
            "contract_coverage": [
                {
                    "requirement_id": "release.registered-behavior",
                    "state": "supported",
                    "evidence_refs": [observation],
                    "tool_limitation": None,
                },
                {
                    "requirement_id": "release.final-only",
                    "state": "unobserved",
                    "evidence_refs": [],
                    "tool_limitation": None,
                },
            ],
        }
        return _provider_response({"summary": "Solver finish", "action": action})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert seen_roles == ["solver", "auditor", "solver", "auditor"]
    assert len(result["contract_audits"]) == 2
    assert result["status"] == "submitted"
    assert result["finish"]["reason"] == "candidate_ready"
    assert result["finish"]["diagnostic_scope"]["contract_coverage_gate"][
        "unobserved_final_acceptance_requirement_ids"
    ] == ["release.final-only"]


def test_completed_contract_audit_receipt_recovers_without_replay(tmp_path, monkeypatch):
    from upgrade_workbench import tasks
    from upgrade_workbench.service.recovery import recover_task

    class Crash(BaseException):
        pass

    owner = "a" * 32
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, service_owner=owner)
    complete = tasks.complete_request
    calls: list[str] = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        if "read-only Contract Auditor" in payload["messages"][0]["content"]:
            calls.append("auditor")
            return _provider_response(
                {
                    "summary": "Recovered read-only audit",
                    "audit": {"verdict": "no_specific_conflict", "questions": []},
                }
            )
        calls.append("solver")
        return _provider_response(
            {
                "summary": "Solver finish",
                "action": {
                    "type": "finish",
                    "reason": "candidate_ready",
                    "explanation": "Bounded offline finish",
                    "evidence_refs": ["business_contract"],
                    "contract_coverage": [
                        {
                            "requirement_id": "release.registered-behavior",
                            "state": "supported",
                            "evidence_refs": ["business_contract"],
                            "tool_limitation": None,
                        }
                    ],
                },
            }
        )

    def crash_after_audit(prepared, **kwargs):
        result = complete(prepared, **kwargs)
        if prepared["output_format"] == "contract_audit":
            raise Crash()
        return result

    monkeypatch.setattr(tasks, "complete_request", crash_after_audit)
    with pytest.raises(Crash):
        tasks.advance_task(
            Path(task["task_path"]),
            api_key="unused",
            transport=transport,
            execution_owner=owner,
        )

    recovered = recover_task(Path(task["task_path"]), Path(task["work_root"]), owner)

    assert calls == ["solver", "auditor"]
    assert recovered["status"] == "ready"
    assert len(recovered["contract_audits"]) == 1
    assert [item.get("role", "solver") for item in recovered["attempts"]] == [
        "solver",
        "contract_auditor",
    ]
    assert recovered["budget"]["calls"] == 2
    assert recovered["budget"]["groups"][0]["state"] == "settled"
