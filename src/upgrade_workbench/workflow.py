"""编排三路比较；不生成补丁，也不把参考答案冒充 Agent 输出。"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .cases import (
    CaseValidationError,
    PatchInfrastructureError,
    PatchValidationError,
    load_case,
    stage_source,
)
from .cases.manifest import assert_no_links, inventory_files, read_verified_file
from .evaluation import select_checks
from .execution import DockerExecutor, DockerUnavailable
from .execution.postgres import load_postgres_spec

CandidateOrigin = Literal["calibration_reference", "user_candidate", "agent_candidate", "official_tool"]
STAGES = ("old_original", "new_original", "new_candidate")


def _snapshot_hashes(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in sorted(inventory_files(directory))
    }


def _check_snapshots(directory: Path, expected: dict[str, dict[str, str]]) -> None:
    """检查实际待运行的副本，而不只检查留在案例目录里的原件。"""
    for name, hashes in expected.items():
        target = directory / name
        if inventory_files(target) != set(hashes):
            raise CaseValidationError(f"Frozen snapshot inventory changed: {name}")
        for relative, digest in hashes.items():
            read_verified_file(target, relative, digest)


def _save(report: dict[str, Any], directory: Path) -> None:
    """每个边界写入进度；临时文件替换防止中止时留下半份 JSON。"""
    report["updated_at"] = datetime.now(UTC).isoformat()
    temporary = directory / "report.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "report.json")
    lines = [
        "# 升级行为比较",
        "",
        f"- 状态：{report['status']}",
        f"- 案例：{report.get('case_id', '尚未完成输入核验')}",
        f"- 候选来源：{report.get('candidate_origin') or '未提供'}",
        f"- 结论：{report.get('reason', '正在执行')}",
        "",
        "| 环境与源码 | 状态 | 收集 | 通过 | 失败 | 错误 | 跳过 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in STAGES:
        value = report["stages"][name]
        counts = value.get("tests", {})
        lines.append(
            f"| {name} | {value['status']} | "
            + " | ".join(
                str(counts.get(key, "-"))
                for key in ("collected", "passed", "failed", "errors", "skipped")
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "完整输入指纹、镜像身份、日志位置和失败原因见同目录 report.json。",
            "本记录只证明所选行为检查的结果，不证明完整应用迁移、安全沙箱或个人独立贡献。",
            "参考补丁校准不计为自主 Agent 解决任务。",
            "",
        ]
    )
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _fully_passed(result: dict[str, Any]) -> bool:
    """exit 0 不够：所有收集到的用例都必须真正执行并通过。"""
    tests = result.get("tests", {})
    identities = result.get("nodeids", [])
    collected = tests.get("collected", 0)
    return (
        result.get("status") == "passed"
        and result.get("exit_code") == 0
        and collected > 0
        and tests.get("passed") == collected
        and all(tests.get(key) == 0 for key in ("failed", "errors", "skipped"))
        and len(identities) == len(set(identities)) == collected
    )


def _all_skipped(result: dict[str, Any]) -> bool:
    """执行器把全跳过标为 error，但它不是引擎或依赖环境故障。"""
    tests = result.get("tests", {})
    count = tests.get("collected", 0)
    return (
        result.get("exit_code") == 0
        and count > 0
        and tests.get("skipped") == count
        and all(tests.get(key) == 0 for key in ("passed", "failed", "errors"))
        and len(result.get("nodeids", [])) == count
        and all(item.get("ok") for item in result.get("cleanup", {}).values())
    )


def _conclusion(report: dict[str, Any]) -> tuple[str, str]:
    old, direct, candidate = (report["stages"][name] for name in STAGES)
    if old["status"] not in {"passed", "failed"} and not _all_skipped(old):
        return "execution_incomplete", "旧环境未完成有效执行，不能断言原代码失败。"
    if not _fully_passed(old):
        return "baseline_invalid", "旧环境未完整通过，不能归因于本次依赖升级。"
    if _all_skipped(direct):
        return "comparison_incomplete", "新环境原代码的检查全部跳过，不能断言没有回归。"
    if direct["status"] not in {"passed", "failed"}:
        return "execution_incomplete", "新环境原代码的执行未得到有效结果。"
    if direct["status"] == "passed" and not _fully_passed(direct):
        return "comparison_incomplete", "新环境原代码没有完整执行全部检查，不能断言没有回归。"
    # 导入失败时尚未收集完整用例是升级证据；正常收集必须与旧环境一致。
    if direct["nodeids"] != old["nodeids"] and not (
        direct["tests"]["errors"] and set(direct["nodeids"]).issubset(old["nodeids"])
    ):
        return "test_set_changed", "新环境原代码收集的测试身份发生变化，需审查。"
    if candidate["status"] == "not_supplied":
        return "comparison_only", "已完成升级前后比较，尚未提供候选补丁。"
    if candidate["status"] not in {"passed", "failed"} and not _all_skipped(candidate):
        return "execution_incomplete", "候选未完成有效执行，不能断言修复正确或错误。"
    if not _fully_passed(candidate):
        return "candidate_not_accepted", "候选未完整通过冻结检查，不能接受。"
    if candidate["nodeids"] != old["nodeids"]:
        return "test_set_changed", "候选与旧环境的测试身份不同，不能接受。"
    if direct["status"] == "passed":
        return "no_regression_observed", "直接升级已通过所选检查，不能把候选记为修复成功。"
    if report["candidate_origin"] == "calibration_reference":
        return "calibration_passed", "参考补丁恢复了所选行为；这是验证链路校准，不是 Agent 成绩。"
    return "candidate_verified", "候选通过相同冻结检查；仍需人工审查未覆盖的行为。"


def run_comparison(
    manifest_path: Path,
    work_root: Path,
    *,
    candidate_patch: Path | None = None,
    candidate_origin: CandidateOrigin | None = None,
    prepare_timeout: int = 600,
    test_timeout: int = 60,
    base_image: str | None = None,
    executor: Any = None,
    check_group: str = "all",
    expected_candidate_sha256: str | None = None,
    diagnostic: bool = False,
) -> dict[str, Any]:
    """在独立目录冻结输入并执行比较，失败也返回可定位的结果包。"""
    if type(diagnostic) is not bool or (diagnostic and check_group != "feedback"):
        raise ValueError("Diagnostic output is only available for public feedback checks")
    if (candidate_patch is None) != (candidate_origin is None):
        raise ValueError("Provide both a candidate patch and its origin, or neither")
    if expected_candidate_sha256 is not None and candidate_patch is None:
        raise ValueError("Reviewed hash requires a candidate patch")
    if candidate_origin not in {None, "calibration_reference", "user_candidate", "agent_candidate", "official_tool"}:
        raise ValueError("Unknown candidate origin")
    for timeout in (prepare_timeout, test_timeout):
        if type(timeout) is not int or timeout < 1:
            raise ValueError("Timeouts must be positive integers")
    root = Path(work_root).absolute()
    assert_no_links(root)
    directory = root / "comparisons" / uuid4().hex
    assert_no_links(directory)
    directory.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": directory.name,
        "status": "running",
        "created_at": datetime.now(UTC).isoformat(),
        "report_path": str(directory / "report.json"),
        "manifest_path": str(Path(manifest_path).absolute()),
        "candidate_origin": candidate_origin,
        "current_action": "validate_input",
        "environments": {},
        "stages": {name: {"status": "not_run"} for name in STAGES},
        "model_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0},
        "automatic_patch_generation": False,
        "requested_base_image": base_image,
        "check_group": check_group,
        "host_python": sys.version.split()[0],
        "implementation_hashes": {
            path.relative_to(Path(__file__).parent).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(Path(__file__).parent.rglob("*.py"))
        },
    }
    if candidate_patch is None:
        report["stages"]["new_candidate"] = {"status": "not_supplied"}
    if diagnostic:
        report["diagnostic_output"] = True
    started = time.monotonic()
    _save(report, directory)
    try:
        case = load_case(manifest_path)
        postgres = load_postgres_spec(case)
        verification_options = {"postgres": postgres} if postgres is not None else {}
        if diagnostic:
            verification_options["diagnostic"] = True
        if postgres is not None:
            report["database_contract"] = postgres
        report.update(
            case_id=case.manifest.case_id,
            case_fingerprint=case.fingerprint,
            source=case.manifest.source.model_dump(),
            expected_new_original=case.manifest.expected_new_original,
        )
        if case.environment is not None:
            if base_image is not None and base_image != case.environment.base_image:
                raise CaseValidationError("Requested base image conflicts with the case environment")
            report["case_environment"] = case.environment.as_dict()
        frozen = directory / "inputs"
        frozen.mkdir()
        (frozen / "manifest.json").write_text(
            case.manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        # 锁、检查与来源证据使用同一次哈希核验得到的字节，不再盲目复制原路径。
        for name, digest in case.manifest.file_hashes.items():
            if name.startswith("source/"):
                continue
            contents = read_verified_file(case.root, name, digest)
            target = frozen / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        report["original"] = stage_source(case, directory / "original")
        selected_checks = directory / "selected_checks"
        expected_nodeids = select_checks(case, check_group, selected_checks)
        checks_path = selected_checks if expected_nodeids is not None else frozen / "checks"
        report["expected_nodeids"] = expected_nodeids
        if candidate_patch is not None:
            assert_no_links(Path(candidate_patch).absolute())
            data = Path(candidate_patch).read_bytes()
            if expected_candidate_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_candidate_sha256:
                raise PatchValidationError("Candidate no longer matches the reviewed SHA-256")
            report["reviewed_candidate_sha256"] = expected_candidate_sha256
            patch = frozen / "candidate.patch"
            patch.write_bytes(data)
            report["candidate"] = stage_source(case, directory / "candidate", patch)
            report["candidate"]["origin"] = candidate_origin
            report["candidate"]["supplied_sha256"] = hashlib.sha256(data).hexdigest()
        snapshot_hashes = {
            "inputs": _snapshot_hashes(frozen),
            "original": _snapshot_hashes(directory / "original"),
        }
        if candidate_patch is not None:
            snapshot_hashes["candidate"] = _snapshot_hashes(directory / "candidate")
        if expected_nodeids is not None:
            snapshot_hashes["selected_checks"] = _snapshot_hashes(selected_checks)
        report["snapshot_hashes"] = snapshot_hashes
        runner = executor if executor is not None else DockerExecutor(root / "executor")
        environment_options: dict[str, Any] = {}
        if case.environment is not None:
            environment_options = {
                "python_version": case.environment.python_version,
                "local_wheels": [frozen / name for name in case.environment.local_wheels],
            }
            base_image = case.environment.base_image
        for environment in ("old", "new"):
            _check_snapshots(directory, snapshot_hashes)
            report["current_action"] = f"prepare_{environment}_environment"
            _save(report, directory)
            # 新环境沿用旧环境已解析的基础镜像，tag 中途更新不能改变实验条件。
            selected_base = (
                report["environments"].get("old", {}).get("base_image_digest", base_image)
            )
            receipt = runner.prepare_environment(
                frozen / "requirements" / f"{environment}.txt",
                timeout_seconds=prepare_timeout,
                **environment_options,
                **({"base_image": selected_base} if selected_base is not None else {}),
            )
            report["environments"][environment] = receipt
            old_base = report["environments"]["old"].get("base_image_id")
            if old_base is not None and receipt.get("base_image_id") != old_base:
                raise ValueError("Old and new environments use different base images")
            _check_snapshots(directory, snapshot_hashes)
            stage = f"{environment}_original"
            report["current_action"] = stage
            report["stages"][stage] = {"status": "running"}
            _save(report, directory)
            report["stages"][stage] = runner.verify(
                receipt["image_id"],
                directory / "original",
                checks_path,
                timeout_seconds=test_timeout,
                **verification_options,
            )
            _check_snapshots(directory, snapshot_hashes)
            _save(report, directory)
            if environment == "old" and expected_nodeids is not None and (
                report["stages"][stage].get("nodeids") != expected_nodeids
            ):
                report.update(status="test_set_changed", reason="Old baseline differs from frozen evaluation nodeids")
                return report
            if environment == "old" and not _fully_passed(report["stages"][stage]):
                status, reason = _conclusion(report)
                report.update(status=status, reason=reason)
                return report
        if candidate_patch is not None:
            _check_snapshots(directory, snapshot_hashes)
            report["current_action"] = "new_candidate"
            report["stages"]["new_candidate"] = {"status": "running"}
            _save(report, directory)
            report["stages"]["new_candidate"] = runner.verify(
                report["environments"]["new"]["image_id"],
                directory / "candidate",
                checks_path,
                timeout_seconds=test_timeout,
                **verification_options,
            )
            _check_snapshots(directory, snapshot_hashes)
        if load_case(manifest_path).fingerprint != case.fingerprint:
            raise CaseValidationError("Case changed during comparison; results cannot be accepted")
        direct = report["stages"]["new_original"]
        report["expected_behavior_observed"] = direct[
            "status"
        ] == case.manifest.expected_new_original and (
            direct["status"] == "failed" or _fully_passed(direct)
        )
        status, reason = _conclusion(report)
        report.update(status=status, reason=reason)
    except DockerUnavailable as exc:
        report.update(status="blocked_environment", reason=str(exc))
    except PatchInfrastructureError as exc:
        report.update(status="execution_error", failure_category="infrastructure", reason=str(exc))
    except (CaseValidationError, PatchValidationError) as exc:
        report.update(status="rejected_input", reason=str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        report.update(status="execution_error", reason=str(exc))
    except KeyboardInterrupt:
        report.update(
            status="interrupted", reason="User or process interruption; no acceptance is claimed."
        )
    except Exception as exc:
        report.update(
            status="execution_error",
            reason=f"Unexpected {type(exc).__name__}; inspect the traceback.",
        )
        raise
    finally:
        for stage in report["stages"].values():
            if stage["status"] == "running":
                stage["status"] = "incomplete"
        report["last_action"] = report.pop("current_action", "unknown")
        report["duration_seconds"] = round(time.monotonic() - started, 6)
        _save(report, directory)
    return report
