"""准备新服务的显式配置；不连接数据库、不启动容器、不调用模型。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from ..budget import BudgetLedger
from ..cases.manifest import assert_no_links
from .config import Settings


def prepare_config(
    config: Path, *, work_root: Path, budget: Path, case: Path, profile: Path,
    database_url: str,
) -> dict[str, str]:
    """只写入新身份；同参复用，冲突时在任何文件写入前拒绝。"""
    config, work_root, budget, case, profile = (
        path.absolute() for path in (config, work_root, budget, case, profile)
    )
    for path in (config, work_root, budget, case, profile):
        assert_no_links(path)
    config, work_root, budget, case, profile = (
        path.resolve() for path in (config, work_root, budget, case, profile)
    )
    if work_root == Path(work_root.anchor):
        raise ValueError("service_work_root_must_not_be_drive_root")
    if not budget.is_file():
        raise ValueError("reviewed_budget_required")
    # 验证账本只读，不能借准备配置顺带升级或重建用户账本。
    with sqlite3.connect(budget.as_uri() + "?mode=ro", uri=True) as ledger:
        row = ledger.execute("SELECT body FROM configuration WHERE id=1").fetchone()
    if row is None:
        raise ValueError("reviewed_budget_configuration_required")
    specification = json.loads(row[0])
    BudgetLedger._validate(specification)
    selected = json.loads(profile.read_text(encoding="utf-8"))
    if (not isinstance(selected, dict) or selected.get("protocol_revision") not in {3, 4, 5, 6}
            or not isinstance(selected.get("generation"), dict)
            or selected["generation"].get("model") != specification["model"]
            or type(selected.get("max_calls")) is not int or selected["max_calls"] < 1):
        raise ValueError("invalid_service_profile")
    from ..tasks import validate_operation_profile

    operation = dict(selected)
    if operation.pop("provider_transport_route", "system") not in {"system", "process_direct"}:
        raise ValueError("invalid_service_profile")
    if set(operation) & {"manifest_path", "work_root", "budget_path", "service_owner"}:
        raise ValueError("invalid_service_profile")
    try:
        validate_operation_profile(case, operation, specification)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("invalid_service_profile") from error
    from ..cases import load_case

    loaded = load_case(case)
    if any(path.is_relative_to(loaded.root) for path in (config, work_root, budget)):
        raise ValueError("service_output_must_be_outside_frozen_case")
    case_id = loaded.manifest.case_id
    settings = Settings(
        work_root=work_root, budget=budget, database_url=database_url,
        token=secrets.token_urlsafe(36), cases={case_id: case}, profiles={"main": selected},
    )
    if config.exists():
        previous = Settings.load(config)
        if (previous.work_root != settings.work_root or previous.budget != settings.budget
                or previous.database_url != settings.database_url
                or previous.cases != settings.cases or previous.profiles != settings.profiles):
            raise ValueError("existing_service_config_identity_conflict")
        if len(previous.token) < 32:
            raise ValueError("invalid_existing_service_token")
        return {"status": "existing_config_reused", "config": str(config)}
    # validate() 会创建工作根；先检查最坏嵌套布局，失败时不留下身份文件。
    settings.preflight_layout("f" * 32)
    settings.validate()
    data = {
        "work_root": str(work_root), "budget": str(budget), "database_url": database_url,
        "token": settings.token, "cases": {case_id: str(case)}, "profiles": settings.profiles,
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建，第二个准备进程不能覆盖先创建的身份。
    with config.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"status": "config_prepared", "config": str(config)}
