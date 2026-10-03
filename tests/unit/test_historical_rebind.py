"""Protocol 6 historical decision inputs remain immutable and separately namespaced."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import load_candidate, publish_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    freeze_context,
    observe,
    public_historical_input,
    resolve_refs,
)
from upgrade_workbench.generation import complete_request
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.tasks import (
    _next_model_role,
    _save,
    bind_historical_input,
    build_historical_input_spec,
    create_operation,
    inspect_task,
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CASE = ROOT / "cases/bump-my-version-0.5.0-r2"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _case(tmp_path: Path, *, case_id: str = "historical-rebind-fixture") -> Path:
    root = tmp_path / "case"
    shutil.copytree(LEGACY_CASE, root)
    check_name = "checks/feedback/test_historical_rebind.py"
    check = root / check_name
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text("def test_registered_behavior():\n    assert True\n", encoding="utf-8")
    contract = (root / "business-contract.md").read_bytes()
    catalog = {
        "schema_version": 1,
        "contract_path": "business-contract.md",
        "contract_sha256": _digest(contract),
        "requirements": [{
            "id": "release.registered-behavior",
            "statement": "The registered public behavior remains valid.",
            "public_check_nodeids": ["feedback/test_historical_rebind.py::test_registered_behavior"],
        }],
    }
    catalog_bytes = json.dumps(catalog, sort_keys=True).encode()
    (root / "contract-requirements.json").write_bytes(catalog_bytes)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        schema_version=2,
        case_id=case_id,
        requirements_path="contract-requirements.json",
    )
    manifest["file_hashes"].update({
        check_name: _digest(check.read_bytes()),
        "contract-requirements.json": _digest(catalog_bytes),
    })
    # 新夹具身份与新增公开检查也必须进入评价合同，不能继承父案例的case_id。
    evaluation_path = root / "evaluation.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["case_id"] = case_id
    feedback = evaluation["groups"]["feedback"]
    feedback["paths"].append("feedback/test_historical_rebind.py")
    feedback["nodeids"].append("feedback/test_historical_rebind.py::test_registered_behavior")
    feedback["expected_count"] = len(feedback["nodeids"])
    evaluation_bytes = json.dumps(evaluation).encode()
    evaluation_path.write_bytes(evaluation_bytes)
    manifest["file_hashes"]["evaluation.json"] = _digest(evaluation_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _ledger(path: Path) -> None:
    BudgetLedger(path, {
        "mode": "user_managed",
        "limit_usd": None,
        "model": "owned-model",
        "input_per_million": "1",
        "output_per_million": "1",
        "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-21",
    })


def _generation() -> dict:
    return {
        "model": "owned-model",
        "endpoint": "https://provider.example/chat/completions",
        "thinking_mode": "disabled",
        "max_output_tokens": 1024,
        "timeout_seconds": 10,
    }


def _provider_response(action: dict) -> bytes:
    return json.dumps({
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"summary": "Frozen decision point.", "action": action})},
        }],
    }).encode()


def _source_task(tmp_path: Path, protocol_revision: int = 5) -> tuple[Path, dict, dict]:
    manifest = _case(tmp_path)
    ledger = tmp_path / "source-ledger.sqlite"
    _ledger(ledger)
    task = create_operation(
        manifest,
        tmp_path / "source-work",
        budget_path=ledger,
        seed_strategy="none",
        protocol_revision=protocol_revision,
        generation=_generation(),
        max_calls=2,
        contract_audit_policy="disabled",
        **({"investigator_policy": "disabled"} if protocol_revision == 6 else {}),
    )
    case = load_case(manifest)
    original = load_candidate(case)
    name = case.manifest.allowed_changes[0]
    candidate = publish_candidate(
        case,
        original,
        {name: b"# historical base\n" + original.files[name]},
        Path(task["task_path"]).parent / "source-candidate",
        origin="agent_candidate",
    )
    task["current_candidate"] = candidate.reference
    before = observe(
        task,
        case,
        {"type": "fixture_observation"},
        {"status": "passed", "marker": "before-cutoff"},
        kind="fixture",
        snapshot=candidate,
    )
    protocol = task["protocol"]
    prepared = prepare_case_proposal(
        manifest,
        Path(task["work_root"]),
        public_source_ack=True,
        experiment_arm="full",
        output_format="contract_actions" if protocol_revision == 5 else "protocol_v6_actions",
        candidate_reference=candidate.reference,
        task_context={"task_id": task["task_id"], "attempt_index": 1, "remaining_calls": 2},
        source_policy=protocol["source_policy"],
        diagnostic_context_reference=freeze_context(task),
        **protocol["generation"],
    )
    response = complete_request(
        prepared,
        api_key="unused",
        transport=lambda *_args, **_kwargs: _provider_response({
            "type": "finish",
            "reason": "unresolved",
            "explanation": "This fixture freezes one completed request.",
            "evidence_refs": [],
            "contract_coverage": [{
                "requirement_id": "release.registered-behavior",
                "state": "unobserved",
                "evidence_refs": [],
                "tool_limitation": "The fixture does not execute target checks.",
            }],
        }),
    )
    assert response["status"] == "agent_finished"
    task["attempts"] = [{
        "receipt": response["report_path"],
        "request_id": Path(response["report_path"]).parent.name,
        "status": response["status"],
    }]
    observe(
        task,
        case,
        {"type": "fixture_observation"},
        {"status": "passed", "marker": "after-cutoff"},
        kind="fixture",
        snapshot=candidate,
    )
    task.update(status="unresolved", stop_reason="fixture_terminal")
    _save(task, Path(task["task_path"]))
    source = inspect_task(Path(task["task_path"]))
    assert before["id"] != source["observations"][-1]["id"]
    return manifest, source, build_historical_input_spec(Path(task["task_path"]), 1)


def _target(manifest: Path, tmp_path: Path, spec: dict, name: str = "target") -> dict:
    ledger = tmp_path / f"{name}-ledger.sqlite"
    _ledger(ledger)
    return create_operation(
        manifest,
        tmp_path / f"{name}-work",
        budget_path=ledger,
        seed_strategy="none",
        protocol_revision=6,
        generation=_generation(),
        max_calls=5,
        contract_audit_policy="disabled",
        investigator_policy="required_once",
        historical_input=spec,
    )


@pytest.mark.parametrize("source_protocol", [5, 6])
def test_rebind_accepts_schema_4_and_5_sources_without_post_cutoff_leakage(tmp_path, source_protocol):
    manifest, source, spec = _source_task(tmp_path / "source", source_protocol)
    target = _target(manifest, tmp_path, spec)
    binding = target["historical_binding"]
    public = public_historical_input(target, target["historical_inputs"][0])

    assert target["attempts"] == []
    assert target["observations"] == []
    assert target["candidate"] is None
    assert public["stale"] is False
    assert public["primary_input_sha256"] == binding["primary_input_sha256"]
    visible = json.dumps(public["visible"], sort_keys=True)
    assert "before-cutoff" in visible
    assert "after-cutoff" not in visible

    source_candidate = Path(spec["source_task_path"]).parent / "source-candidate"
    rebound_candidate = Path(binding["candidate_reference"]["path"]).parent
    for name in ("revision.json", "candidate.patch", "increment.patch"):
        assert (rebound_candidate / name).read_bytes() == (source_candidate / name).read_bytes()

    historical_ref = public["historical_observation_refs"][0]
    resolve_refs(target, [historical_ref])
    raw_id = historical_ref.rsplit(":", 1)[-1]
    with pytest.raises(ValueError, match="Unknown public evidence references"):
        resolve_refs(target, ["observation:" + raw_id])
    assert source["observations"][-1]["id"] not in visible


def test_two_rebound_arms_share_the_same_primary_input_identity(tmp_path):
    manifest, _source, spec = _source_task(tmp_path / "source")
    first = _target(manifest, tmp_path, spec, "first")
    second = _target(manifest, tmp_path, spec, "second")

    assert first["historical_binding"]["primary_input_sha256"] == second["historical_binding"]["primary_input_sha256"]
    assert first["task_id"] != second["task_id"]
    assert first["historical_binding"]["candidate_reference"] != second["historical_binding"]["candidate_reference"]


def test_historical_input_remains_readable_after_current_candidate_advances(tmp_path):
    manifest, _source, spec = _source_task(tmp_path / "source")
    target = _target(manifest, tmp_path, spec)
    case = load_case(manifest)
    base = load_candidate(case, target["current_candidate"])
    name = case.manifest.allowed_changes[0]
    advanced = publish_candidate(
        case,
        base,
        {name: b"# current edit\n" + base.files[name]},
        Path(target["task_path"]).parent / "current-candidate",
        origin="agent_candidate",
    )
    target["current_candidate"] = advanced.reference
    _save(target, Path(target["task_path"]))

    loaded = inspect_task(Path(target["task_path"]))
    assert public_historical_input(loaded, loaded["historical_inputs"][0])["stale"] is True


@pytest.mark.parametrize("artifact", ["proposal", "request", "context", "candidate"])
def test_rebind_rejects_tampered_historical_artifacts(tmp_path, artifact):
    manifest, source, spec = _source_task(tmp_path / "source")
    proposal_path = Path(source["attempts"][0]["receipt"])
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    paths = {
        "proposal": proposal_path,
        "request": Path(proposal["request_path"]),
        "context": Path(proposal["diagnostic_context_reference"]["path"]),
        "candidate": Path(proposal["candidate_reference"]["path"]).with_name("candidate.patch"),
    }
    paths[artifact].write_bytes(paths[artifact].read_bytes() + b"\n")

    with pytest.raises(ValueError):
        _target(manifest, tmp_path, spec)


def test_rebind_rejects_duplicate_cross_case_incomplete_and_nonempty_targets(tmp_path):
    manifest, source, spec = _source_task(tmp_path / "source")
    target = _target(manifest, tmp_path, spec)
    with pytest.raises(ValueError, match="new empty Protocol 6 task"):
        bind_historical_input(Path(target["task_path"]), spec)

    other_manifest = _case(tmp_path / "other", case_id="other-case")
    with pytest.raises(ValueError, match="different cases"):
        _target(other_manifest, tmp_path, spec, "other")

    source["status"] = "ready"
    _save(source, Path(source["task_path"]))
    with pytest.raises(ValueError, match="terminal source task"):
        build_historical_input_spec(Path(source["task_path"]), 1)

    source["status"] = "unresolved"
    source["attempts"][0]["status"] = "reserved"
    _save(source, Path(source["task_path"]))
    with pytest.raises(ValueError, match="incomplete or has an unknown outcome"):
        build_historical_input_spec(Path(source["task_path"]), 1)
    with pytest.raises(ValueError, match="unknown"):
        build_historical_input_spec(Path(source["task_path"]), 2)


def test_rebind_rejects_a_target_with_existing_current_state(tmp_path):
    manifest, _source, spec = _source_task(tmp_path / "source")
    ledger = tmp_path / "nonempty-ledger.sqlite"
    _ledger(ledger)
    target = create_operation(
        manifest,
        tmp_path / "nonempty-work",
        budget_path=ledger,
        seed_strategy="none",
        protocol_revision=6,
        generation=_generation(),
        max_calls=5,
        contract_audit_policy="disabled",
        investigator_policy="required_once",
    )
    target["issues"] = [{"hypothesis": "Existing state must not be overwritten."}]
    _save(target, Path(target["task_path"]))

    with pytest.raises(ValueError, match="new empty Protocol 6 task"):
        bind_historical_input(Path(target["task_path"]), spec)


def test_required_investigator_stops_after_three_calls_without_consuming_solver_reserve(tmp_path):
    manifest, _source, spec = _source_task(tmp_path / "source")
    task = _target(manifest, tmp_path, spec)

    assert _next_model_role(task) == "investigator"
    session = task["investigator_sessions"][0]
    for attempt_index in (1, 2, 3):
        task["attempts"].append({"status": "provider_response_rejected"})
        session["attempt_indices"].append(attempt_index)
        if attempt_index < 3:
            assert _next_model_role(task) == "investigator"

    assert _next_model_role(task) is None
    assert len(task["attempts"]) == 3
    assert task["protocol"]["max_calls"] - len(task["attempts"]) == 2
    assert task["status"] == "unresolved"
    assert task["stop_reason"] == "required_investigator_handoff_missing"
    assert session["status"] == "budget_exhausted"
    assert "investigator_budget_feedback" not in task
