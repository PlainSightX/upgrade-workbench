import pytest

from upgrade_workbench.generation.roles import (
    RoleContractError,
    recoverable_attempt_contract,
    role_spec,
    validate_investigation,
)


def handoff() -> dict:
    return {
        "observed_facts": [
            {
                "statement": "The inspected symbol accepts one positional dialect argument.",
                "scope": "Observed in the registered SQLAlchemy 2.0.7 dependency environment only.",
                "evidence_refs": ["historical:row-signature"],
            }
        ],
        "scope_limits": ["The observation does not establish complete application behavior."],
        "remaining_hypotheses": [
            {
                "hypothesis": "A result processor still changes one custom value.",
                "evidence_refs": ["source:databases/backends/sqlite.py"],
                "next_check": "Request one bounded public observation for the custom processor.",
            }
        ],
        "conflicts": [
            {
                "claim": "The original interface implemented mapping access.",
                "conflict": "The registered original source raises NotImplementedError.",
                "evidence_refs": ["source:databases/interfaces.py"],
            }
        ],
        "next_discriminating_action": {
            "kind": "request_observation",
            "description": "Exercise the custom type processor on the current revision.",
            "evidence_refs": ["business_contract"],
        },
    }


def test_role_registry_keeps_one_candidate_writer():
    solver = role_spec("solver")
    investigator = role_spec("investigator")
    auditor = role_spec("contract_auditor")

    assert solver.candidate_write_allowed and solver.terminal_write_allowed
    assert not investigator.candidate_write_allowed and not investigator.terminal_write_allowed
    assert not auditor.candidate_write_allowed and not auditor.terminal_write_allowed
    assert investigator.observation_request_allowed
    assert not auditor.observation_request_allowed
    solver.require_candidate_write()
    with pytest.raises(RoleContractError, match="cannot write"):
        investigator.require_candidate_write()


def test_protocol_6_role_formats_are_explicit_and_distinct():
    assert role_spec("solver").output_format == "protocol_v6_actions"
    assert role_spec("investigator").output_format == "investigator_actions"
    assert role_spec("contract_auditor").output_format == "contract_audit"


@pytest.mark.parametrize(
    "role,output_format",
    [
        ("solver", "protocol_v6_actions"),
        ("investigator", "investigator_actions"),
        ("contract_auditor", "contract_audit"),
    ],
)
def test_schema_5_recovery_requires_exact_role_format(role, output_format):
    spec, expected = recoverable_attempt_contract(
        {"schema_version": 5},
        {"role": role, "output_format": output_format},
    )

    assert spec.role == role
    assert expected == output_format


@pytest.mark.parametrize(
    "attempt",
    [
        {},
        {"role": "solver"},
        {"output_format": "protocol_v6_actions"},
        {"role": "solver", "output_format": "investigator_actions"},
        {"role": "investigator", "output_format": "protocol_v6_actions"},
    ],
)
def test_schema_5_recovery_rejects_implicit_or_cross_role_format(attempt):
    with pytest.raises(RoleContractError):
        recoverable_attempt_contract({"schema_version": 5}, attempt)


@pytest.mark.parametrize(
    "schema,attempt,role,output_format",
    [
        (3, {}, "solver", "diagnostic_actions"),
        (4, {}, "solver", "contract_actions"),
        (4, {"role": "contract_auditor"}, "contract_auditor", "contract_audit"),
    ],
)
def test_protocol_4_and_5_recovery_keep_legacy_role_inference(
    schema, attempt, role, output_format
):
    spec, expected = recoverable_attempt_contract({"schema_version": schema}, attempt)

    assert spec.role == role
    assert expected == output_format


def test_investigator_accepts_structured_read_only_handoff():
    value = handoff()

    assert validate_investigation(value) is value
    assert role_spec("investigator").validate_output(value) is value


@pytest.mark.parametrize("forbidden", ["edits", "patch", "finish", "candidate_reference"])
def test_investigator_rejects_candidate_or_terminal_fields(forbidden):
    value = handoff()
    value[forbidden] = []

    with pytest.raises(RoleContractError, match="exact read-only schema"):
        validate_investigation(value)


def test_investigator_rejects_unbound_fact_and_write_action():
    value = handoff()
    value["observed_facts"][0]["evidence_refs"] = []
    with pytest.raises(RoleContractError, match="require evidence"):
        validate_investigation(value)

    value = handoff()
    value["next_discriminating_action"]["kind"] = "submit_candidate"
    with pytest.raises(RoleContractError, match="read-only"):
        validate_investigation(value)


def test_solver_requires_protocol_specific_validator():
    with pytest.raises(RoleContractError, match="protocol validator"):
        role_spec("solver").validate_output({"type": "finish"})

    assert role_spec("solver").validate_output(
        {"type": "finish"}, protocol_validator=lambda value: value
    ) == {"type": "finish"}


def test_unknown_role_is_rejected():
    with pytest.raises(RoleContractError, match="Unknown model role"):
        role_spec("writer_two")


def test_probe_recommendation_rejection_names_exact_field_and_correction():
    value = handoff()
    value["next_discriminating_action"]["kind"] = "run_probe"
    with pytest.raises(RoleContractError) as failure:
        validate_investigation(value)
    assert "next_discriminating_action.kind" in str(failure.value)
    assert "request_observation" in str(failure.value)
    assert value["next_discriminating_action"]["kind"] == "run_probe"
    value["next_discriminating_action"]["kind"] = "request_observation"
    assert validate_investigation(value) is value
