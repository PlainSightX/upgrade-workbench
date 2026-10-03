"""Protocol 6 local A/B runs must share a verifiable objective and input identity."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from upgrade_workbench import diagnostics
from upgrade_workbench import tasks as task_module
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.diagnostics import store
from upgrade_workbench.generation import ProposalInputError, prepare_request
from upgrade_workbench.generation.protocol_v6 import (
    DECISION_OBJECTIVE,
    DEPENDENCY_QUERY_POLICY,
    DIAGNOSTIC_POLICY,
    decision_objective_sha256,
    validate_decision_objective,
)
from upgrade_workbench.generation.provider import _load_prepared
from upgrade_workbench.generation.source_context import POLICY


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _catalog(owned_case) -> SimpleNamespace:
    digest = owned_case.manifest.file_hashes["business-contract.md"]
    requirement = SimpleNamespace(id="label.default")
    public = {
        "schema_version": 1,
        "contract_path": "business-contract.md",
        "contract_sha256": digest,
        "requirements": [{
            "id": requirement.id,
            "statement": "Omitted label remains None.",
            "public_check_nodeids": [],
        }],
        "development_gate_requirement_ids": [],
        "final_acceptance_requirement_ids": [requirement.id],
    }
    return SimpleNamespace(
        path="business-contract.md",
        sha256=digest,
        contract=SimpleNamespace(requirements=[requirement]),
        public=lambda: copy.deepcopy(public),
    )


def _install_protocol6_dependencies(monkeypatch, owned_case) -> SimpleNamespace:
    """Add only the structured catalog/runtime missing from the small owned fixture."""
    from upgrade_workbench.cases import requirements

    catalog = _catalog(owned_case)
    monkeypatch.setattr(requirements, "requirements_for_case", lambda _case: catalog)
    monkeypatch.setattr(
        diagnostics,
        "context_from_reference",
        lambda *_args, **_kwargs: {"observations": [], "navigation_assistance": "baseline"},
    )
    return catalog


def _protocol(catalog, *, point: str, investigator_policy: str) -> dict:
    return {
        "protocol_revision": 6,
        "status": "operation",
        "max_calls": 5,
        "implementation": task_module.implementation_identity(6),
        "generation": {
            "model": "owned-model",
            "endpoint": "https://provider.example/chat/completions",
            "thinking_mode": "disabled",
            "max_output_tokens": 1024,
            "timeout_seconds": 10,
        },
        "execution": {},
        "context_assistance": "full",
        "navigation_assistance": "failure_guided",
        "source_policy": copy.deepcopy(POLICY),
        "diagnostic_policy": copy.deepcopy(DIAGNOSTIC_POLICY),
        "contract_requirements": {
            "path": catalog.path,
            "sha256": catalog.sha256,
            "requirement_ids": [item.id for item in catalog.contract.requirements],
        },
        "contract_audit_policy": "disabled",
        "investigator_policy": investigator_policy,
        "dependency_query_policy": copy.deepcopy(DEPENDENCY_QUERY_POLICY),
        "role_budget": {"investigator_max_calls": 3, "solver_reserved_calls": 2},
        "decision_objective": copy.deepcopy(DECISION_OBJECTIVE),
        "decision_point_id": point,
    }


def _comparison_task(owned_case, catalog, *, point: str, arm: str) -> dict:
    policy = "disabled" if arm == "a" else "required_once"
    primary = "1" * 64 if point == "d1" else "2" * 64
    return {
        "case_fingerprint": owned_case.fingerprint,
        "protocol": _protocol(catalog, point=point, investigator_policy=policy),
        "historical_binding": {
            "primary_input_sha256": primary,
            "candidate_revision": "3" * 64,
            "candidate_sha256": "4" * 64,
        },
    }


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
            "pricing_checked_at": "2026-09-21",
        },
    )


def _task(owned_case, catalog, tmp_path: Path) -> dict:
    # 请求构建夹具只有私有oracle；真实任务还要求显式的公开/独立检查分组。
    evaluation = {
        "schema_version": 1, "case_id": owned_case.manifest.case_id, "split": "development",
        "groups": {
            "feedback": {"paths": ["test_feedback.py"],
                         "nodeids": ["test_feedback.py::test_feedback"], "expected_count": 1},
            "acceptance": {"paths": ["test_contract.py"],
                           "nodeids": ["test_contract.py::test_oracle"], "expected_count": 1},
        },
    }
    contents = {
        "checks/test_feedback.py": b"def test_feedback():\n    assert True\n",
        "evaluation.json": json.dumps(evaluation).encode(),
    }
    manifest = json.loads(owned_case.manifest_path.read_text(encoding="utf-8"))
    for name, data in contents.items():
        (owned_case.root / name).write_bytes(data)
        manifest["file_hashes"][name] = _digest(data)
    owned_case.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ledger = tmp_path / "budget.sqlite"
    _ledger(ledger)
    return task_module.create_task(
        owned_case.manifest_path,
        tmp_path / "work",
        _protocol(catalog, point="d1", investigator_policy="disabled"),
        arm="direct_repair",
        phase="operation",
        budget_path=ledger,
        kind="operation",
    )


def _request(
    owned_case,
    analysis,
    evidence,
    arguments,
    binding: dict,
    *,
    output_format: str,
    attempt_index: int,
) -> dict:
    from upgrade_workbench.candidates import load_candidate

    snapshot = load_candidate(owned_case)
    binding.update(
        case_fingerprint=owned_case.fingerprint,
        candidate_revision=snapshot.revision,
        candidate_sha256=snapshot.sha256,
    )
    binding["pair_input_sha256"] = task_module._digest(
        {key: value for key, value in binding.items() if key != "pair_input_sha256"}
    )
    return prepare_request(
        owned_case,
        analysis,
        evidence,
        **arguments,
        output_format=output_format,
        candidate_reference=None,
        source_policy=POLICY,
        diagnostic_context_reference={"id": "0" * 64, "path": "offline-unit-fixture"},
        task_context={
            "task_id": "comparison-fixture",
            "attempt_index": attempt_index,
            "remaining_calls": 6 - attempt_index,
        },
        decision_objective=copy.deepcopy(DECISION_OBJECTIVE),
        comparison_binding=copy.deepcopy(binding),
    )


def test_decision_objective_accepts_only_the_frozen_enum_contract():
    frozen = validate_decision_objective(copy.deepcopy(DECISION_OBJECTIVE))
    assert frozen == DECISION_OBJECTIVE
    assert frozen is not DECISION_OBJECTIVE

    with pytest.raises(ValueError, match="frozen bounded contract"):
        validate_decision_objective(DECISION_OBJECTIVE | {"root_cause": "manual answer"})
    with pytest.raises(ValueError, match="frozen bounded contract"):
        validate_decision_objective("repair the transaction by adding rollback")
    with pytest.raises(ValueError, match="frozen bounded contract"):
        validate_decision_objective(DECISION_OBJECTIVE | {"mode": "free_form_repair"})
    with pytest.raises(ValueError, match="frozen bounded contract"):
        validate_decision_objective(DECISION_OBJECTIVE | {"schema_version": True})


@pytest.mark.parametrize("policy", ["disabled", "required_once"])
def test_local_role_budget_does_not_limit_full_tasks(owned_case, monkeypatch, policy):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    protocol = _protocol(catalog, point="d1", investigator_policy=policy)
    task_module._require_current_protocol(protocol)
    protocol["max_calls"] = 30
    with pytest.raises(ValueError, match="exactly 5"):
        task_module._require_current_protocol(protocol)
    del protocol["decision_objective"]
    del protocol["decision_point_id"]
    task_module._require_current_protocol(protocol)
    if policy == "required_once":
        protocol["max_calls"] = 4
        with pytest.raises(ValueError, match="reserved Solver"):
            task_module._require_current_protocol(protocol)


def test_pair_identity_matches_within_decision_point_and_differs_across_points(
    owned_case, monkeypatch,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    d1_a = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d1", arm="a")
    )
    d1_b = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d1", arm="b")
    )
    d2_a = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d2", arm="a")
    )
    d2_b = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d2", arm="b")
    )

    assert d1_a["pair_input_sha256"] == d1_b["pair_input_sha256"]
    assert d2_a["pair_input_sha256"] == d2_b["pair_input_sha256"]
    assert d1_a["pair_input_sha256"] != d2_a["pair_input_sha256"]
    assert d1_a["decision_objective_sha256"] == decision_objective_sha256(
        DECISION_OBJECTIVE
    )


def test_solver_and_investigator_requests_keep_the_same_objective_and_pair_identity(
    owned_case, analysis, evidence, arguments, monkeypatch,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    binding = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d1", arm="b")
    )
    requests = [
        _request(
            owned_case,
            analysis,
            evidence,
            arguments,
            binding,
            output_format=output_format,
            attempt_index=index,
        )
        for index, output_format in enumerate(
            ("protocol_v6_actions", "investigator_actions", "protocol_v6_actions"),
            start=1,
        )
    ]

    for prepared in requests:
        payload = json.loads(Path(prepared["request_path"]).read_text(encoding="utf-8"))
        context = json.loads(payload["messages"][1]["content"])
        assert context["decision_objective"] == DECISION_OBJECTIVE
        assert context["comparison_binding"] == {
            "schema_version": 1,
            "decision_point_id": "d1",
            "pair_input_sha256": binding["pair_input_sha256"],
        }
        assert prepared["decision_objective_sha256"] == binding["decision_objective_sha256"]
        assert prepared["pair_input_sha256"] == binding["pair_input_sha256"]


def test_comparison_identity_remains_bound_to_starting_candidate_across_iterations(
    owned_case, analysis, evidence, arguments, monkeypatch,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    binding = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d1", arm="a")
    )
    first = _request(
        owned_case,
        analysis,
        evidence,
        arguments,
        binding,
        output_format="protocol_v6_actions",
        attempt_index=1,
    )
    starting_revision = binding["candidate_revision"]
    starting_pair = binding["pair_input_sha256"]

    from upgrade_workbench.candidates import load_candidate, publish_candidate

    base = load_candidate(owned_case)
    changed = publish_candidate(
        owned_case,
        base,
        {
            "model.py": base.files["model.py"].replace(
                b"label: str | None", b"label: str | None = None"
            )
        },
        Path(arguments["work_root"]) / "changed-candidate",
        origin="agent_candidate",
    )
    second = prepare_request(
        owned_case,
        analysis,
        evidence,
        **arguments,
        output_format="protocol_v6_actions",
        candidate_reference=changed.reference,
        source_policy=POLICY,
        diagnostic_context_reference={"id": "0" * 64, "path": "offline-unit-fixture"},
        task_context={
            "task_id": "comparison-fixture",
            "attempt_index": 2,
            "remaining_calls": 4,
            "previous_candidate": {"sha256": changed.sha256, "revision": changed.revision},
        },
        decision_objective=copy.deepcopy(DECISION_OBJECTIVE),
        comparison_binding=copy.deepcopy(binding),
    )
    assert first["pair_input_sha256"] == second["pair_input_sha256"] == starting_pair
    assert binding["candidate_revision"] == starting_revision
    payload = json.loads(Path(second["request_path"]).read_text(encoding="utf-8"))
    context = json.loads(payload["messages"][1]["content"])
    assert context["candidate"]["revision"] == changed.revision
    assert context["comparison_binding"]["pair_input_sha256"] == starting_pair


def test_task_rejects_tampered_decision_objective_hash(
    owned_case, tmp_path, monkeypatch,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    task = _task(owned_case, catalog, tmp_path)
    task["decision_objective_sha256"] = "0" * 64
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValueError, match="decision objective identity changed"):
        task_module.inspect_task(Path(task["task_path"]))


def test_task_rejects_tampered_attempt_pair_hash(
    owned_case, tmp_path, monkeypatch,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    task = _task(owned_case, catalog, tmp_path)
    task_path = Path(task["task_path"])
    primary = "1" * 64
    candidate_directory = task_path.parent / "historical_candidates" / primary
    candidate_directory.mkdir(parents=True)
    candidate_patch = b"historical candidate patch\n"
    increment_patch = b"historical increment patch\n"
    (candidate_directory / "candidate.patch").write_bytes(candidate_patch)
    (candidate_directory / "increment.patch").write_bytes(increment_patch)
    (candidate_directory / "revision.json").write_text("{}", encoding="utf-8")
    candidate_reference = {"path": str(candidate_directory / "revision.json")}
    candidate_revision = "5" * 64
    candidate_sha256 = _digest(candidate_patch)
    historical = store(
        task_path.parent / "historical_inputs",
        {"candidate": {"increment_patch_sha256": _digest(increment_patch)}},
    )
    task["historical_inputs"] = [historical]
    task["historical_binding"] = {
        "input_id": historical["id"],
        "primary_input_sha256": primary,
        "source_task_id": "source-task",
        "attempt_index": 1,
        "candidate_reference": candidate_reference,
        "candidate_revision": candidate_revision,
        "candidate_sha256": candidate_sha256,
    }
    task["comparison_binding"] = task_module._comparison_binding(task)
    task["attempts"] = [{
        "decision_objective_sha256": task["decision_objective_sha256"],
        "pair_input_sha256": "0" * 64,
    }]
    real_load_candidate = task_module.load_candidate

    def load_candidate(case, reference=None):
        if reference == candidate_reference:
            return SimpleNamespace(revision=candidate_revision, sha256=candidate_sha256)
        return real_load_candidate(case, reference)

    monkeypatch.setattr(task_module, "load_candidate", load_candidate)
    monkeypatch.setattr(diagnostics, "public_historical_input", lambda *_args, **_kwargs: {})
    task_module._save(task, task_path)

    with pytest.raises(ValueError, match="attempt comparison identity changed"):
        task_module.inspect_task(task_path)


@pytest.mark.parametrize("field", ["decision_objective_sha256", "pair_input_sha256"])
def test_prepared_proposal_rejects_tampered_comparison_hash(
    owned_case,
    analysis,
    evidence,
    arguments,
    monkeypatch,
    field,
):
    catalog = _install_protocol6_dependencies(monkeypatch, owned_case)
    binding = task_module._comparison_binding(
        _comparison_task(owned_case, catalog, point="d1", arm="a")
    )
    prepared = _request(
        owned_case,
        analysis,
        evidence,
        arguments,
        binding,
        output_format="protocol_v6_actions",
        attempt_index=1,
    )
    _load_prepared(prepared)

    tampered = copy.deepcopy(prepared)
    tampered[field] = "0" * 64
    Path(tampered["report_path"]).write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ProposalInputError):
        _load_prepared(tampered)
