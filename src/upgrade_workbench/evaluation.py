"""核验冻结评价分组；公开反馈容器永远不带最终验收文件。"""

from __future__ import annotations

import json
from pathlib import Path

from .cases import CaseValidationError, LoadedCase
from .cases.manifest import read_verified_file, relative_path


def load_evaluation(case: LoadedCase) -> dict:
    """分组配置也必须被案例指纹覆盖，不能在试跑后临时换评分文件。"""
    name = "evaluation.json"
    digest = case.manifest.file_hashes.get(name)
    if digest is None:
        raise CaseValidationError("A registered evaluation.json is required")
    try:
        data = json.loads(read_verified_file(case.root, name, digest))
        if (
            type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("case_id") != case.manifest.case_id
            or data.get("split") not in {"development", "holdout"}
            or set(data.get("groups", {})) != {"feedback", "acceptance"}
        ):
            raise ValueError("Evaluation identity or groups do not match the case")
        all_paths: set[str] = set()
        all_nodes: set[str] = set()
        for group in data["groups"].values():
            paths, nodes = group["paths"], group["nodeids"]
            if (
                not isinstance(paths, list) or not paths or len(set(paths)) != len(paths)
                or not isinstance(nodes, list) or not nodes or len(set(nodes)) != len(nodes)
                or type(group["expected_count"]) is not int
                or group["expected_count"] != len(nodes)
            ):
                raise ValueError("Evaluation groups require distinct paths and nonempty nodeids")
            for path in paths:
                relative_path(path)
                if not path.endswith(".py") or f"checks/{path}" not in case.manifest.file_hashes:
                    raise ValueError("Evaluation can only select registered Python check files")
            if all_paths.intersection(paths) or all_nodes.intersection(nodes):
                raise ValueError("Feedback and acceptance checks must not overlap")
            for node in nodes:
                if not isinstance(node, str) or "::" not in node or node.split("::")[0] not in paths:
                    raise ValueError("A nodeid must belong to the selected group")
            all_paths.update(paths)
            all_nodes.update(nodes)
        registered = {
            name.removeprefix("checks/") for name in case.manifest.file_hashes
            if name.startswith("checks/")
        }
        if all_paths != registered:
            raise ValueError("Every check file must have exactly one evaluation owner")
    except (ValueError, TypeError, KeyError) as error:
        raise CaseValidationError(f"Invalid evaluation contract: {error}") from error
    return data


def select_checks(case: LoadedCase, group: str, destination: Path) -> list[str] | None:
    """只物化所选文件；保持相对路径，pytest 身份不会因分组而改名。"""
    if group not in {"all", "feedback", "acceptance"}:
        raise CaseValidationError("Unknown check group")
    if "evaluation.json" not in case.manifest.file_hashes:
        if group != "all":
            raise CaseValidationError("Legacy cases only support all checks")
        return None
    evaluation = load_evaluation(case)
    selected = (
        list(evaluation["groups"].values()) if group == "all"
        else [evaluation["groups"][group]]
    )
    destination.mkdir(parents=True, exist_ok=False)
    for item in selected:
        for name in item["paths"]:
            source_name = f"checks/{name}"
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_verified_file(
                case.root, source_name, case.manifest.file_hashes[source_name],
            ))
    return sorted(node for item in selected for node in item["nodeids"])
