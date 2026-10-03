"""JWT r2 只验证案例身份和公开合同映射，不执行目标代码。"""

from pathlib import Path

from upgrade_workbench.cases import load_case
from upgrade_workbench.cases.requirements import requirements_for_case
from upgrade_workbench.evaluation import load_evaluation

ROOT = Path(__file__).resolve().parents[3]
PARENT = ROOT / "cases/fastapi-jwt-auth-pydantic-2/manifest.json"
REVISION = ROOT / "cases/fastapi-jwt-auth-pydantic-2-r2/manifest.json"


def test_jwt_r2_is_seen_protocol_5_development_case() -> None:
    parent = load_case(PARENT)
    revision = load_case(REVISION)
    evaluation = load_evaluation(revision)
    requirements = requirements_for_case(revision).contract.requirements

    assert revision.manifest.case_id == "fastapi-jwt-auth-pydantic-2-r2"
    assert revision.manifest.source == parent.manifest.source
    assert {
        path: digest
        for path, digest in revision.manifest.file_hashes.items()
        if path.startswith(("source/", "checks/", "requirements/", "evidence/"))
    } == {
        path: digest
        for path, digest in parent.manifest.file_hashes.items()
        if path.startswith(("source/", "checks/", "requirements/", "evidence/"))
    }
    assert evaluation["split"] == "development"
    assert len(requirements) == 22


def test_jwt_r2_public_checks_do_not_overclaim_hidden_boundaries() -> None:
    requirements = {
        item.id: item.public_check_nodeids
        for item in requirements_for_case(load_case(REVISION)).contract.requirements
    }

    assert requirements["location.list-valid"] == [
        "feedback/test_public.py::test_location_validation"
    ]
    assert requirements["location.invalid-rejected"] == [
        "feedback/test_public.py::test_location_validation"
    ]
    for requirement_id in (
        "location.set-tuple-valid",
        "sequence.all-items-validated",
        "config.strict-scalars",
        "config.strings-trimmed-nonempty",
        "config.optional-none",
        "expiry.false-no-exp",
        "expiry.seconds-timedelta",
        "auth.bad-signature-rejected",
        "auth.denylist-enforced",
        "auth.custom-header",
        "auth.cookie-csrf",
        "callback.model-valid",
        "migration.native-pydantic-v2",
    ):
        assert requirements[requirement_id] == []
