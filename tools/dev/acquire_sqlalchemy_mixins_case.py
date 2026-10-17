"""固定 sqlalchemy-mixins 1.5.3 上游源码，准入 SQLAlchemy 1.4 -> 2.0 未见仓库案例。

步骤顺序是证据的一部分：acquire 只解包固定归档；admit 在任何模型请求前用 Docker
运行旧依赖与直接升级两路；seal 只在旧环境全过、直接升级失败后生成清单。
宿主不导入、不执行目标源码。

案例已封存：重跑 acquire / evidence 只会逐字节确认已提交的文件，seal 遇到已有清单会拒绝。
归档需先从 ARCHIVE_URL 下载到 ARCHIVE，脚本只核对 SHA-256，不联网。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "cases/sqlalchemy-mixins-sqlalchemy-2"
# 归档与准入报告放在 Git 忽略的 .local/ 下；UW_SAM_WORK 可改到别处。
WORK = Path(os.environ.get("UW_SAM_WORK", ROOT / ".local/unseen-sam"))
# Docker 每阶段的工作目录；路径过长时可用 UW_SAM_DOCKER_ROOT 指向更短的目录。
TASK_ROOT = Path(os.environ.get("UW_SAM_DOCKER_ROOT", WORK / "docker"))
REPOSITORY = "https://github.com/absent1706/sqlalchemy-mixins"
REVISION = "ce4badbc51f8049783fa3909615ccfc7c1198d98"  # tag v1.5.3
ARCHIVE_URL = f"https://codeload.github.com/absent1706/sqlalchemy-mixins/tar.gz/{REVISION}"
ARCHIVE = WORK / "downloads/sam-ce4badb.tar.gz"
ARCHIVE_SHA256 = "6410a63ab5312e8aa78dad7997e9cb4da3c73d3d224b51718db70e85f47a3d7b"
BASE = "python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b"
EVIDENCE_PARENT = ROOT / "cases/csvkit-csvsql-sqlalchemy-2-a3"
# 迁移原文标题：只登记与 ORM 选项、Query 和声明式相关的官方章节，不写修法。
EVIDENCE_HEADINGS = {
    "sqlalchemy-v2-loader-string-paths":
        "ORM Query  - Joining / loading on relationships uses attributes, not strings",
    "sqlalchemy-v2-query-get": "ORM Query - get() method moves to Session",
    "sqlalchemy-v2-query-unified-select": "ORM Query Unified with Core Select",
    "sqlalchemy-v2-dynamic-relationship":
        "\"Dynamic\" relationship loaders superseded by \"Write Only\"",
    "sqlalchemy-v2-declarative-first-class": "Declarative becomes a first class API",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, data: bytes) -> None:
    """只新建或确认同字节，避免静默覆盖已冻结输入。"""
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Existing evidence differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def dump(path: Path, value: object) -> None:
    save(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def acquire() -> None:
    """库源码进入 source/，上游测试只作来源留存放入 upstream/，不进入检查。"""
    if not ARCHIVE.is_file():
        raise FileNotFoundError(f"Download {ARCHIVE_URL} to {ARCHIVE} first")
    if digest(ARCHIVE.read_bytes()) != ARCHIVE_SHA256:
        raise ValueError("Pinned upstream archive changed")
    prefix = "sqlalchemy-mixins-" + REVISION
    selected: dict[str, bytes] = {}
    with tarfile.open(ARCHIVE, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] != prefix
                    or member.issym() or member.islnk()):
                raise ValueError(f"Unsafe archive member: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Unexpected archive member type: {member.name}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative.startswith("sqlalchemy_mixins/tests/"):
                target = "upstream/" + relative
            elif relative.startswith("sqlalchemy_mixins/"):
                target = "source/" + relative
            elif relative in {"LICENSE.txt", "README.md"}:
                target = "source/" + relative
            elif relative in {"setup.py", "requirements.txt", ".github/workflows/test-pr.yml"}:
                target = "upstream/" + relative
            else:
                continue
            if member.size > 500_000 or target in selected:
                raise ValueError("Source size or duplicate path bound exceeded")
            contents = archive.extractfile(member).read()
            if relative.endswith(".py"):
                ast.parse(contents)
            selected[target] = contents
    for name, contents in sorted(selected.items()):
        save(CASE / name, contents)
    dump(CASE / "SOURCE.json", {
        "repository": REPOSITORY,
        "revision": REVISION,
        "tag": "v1.5.3",
        "archive_url": ARCHIVE_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "license": "MIT",
        "files": {name: {"upstream_path": name.split("/", 1)[1], "sha256": digest(contents)}
                  for name, contents in sorted(selected.items())},
        "target_code_executed_on_host": False,
        "classification": "unseen_public_repository_never_used_by_this_workbench",
        "upstream_answer_patch_in_solver_input": False,
    })
    print(json.dumps({"files": len(selected)}))


def evidence() -> None:
    """复用已核验的同一份 SQLAlchemy 迁移原文字节，按标题重新定位行范围。"""
    from upgrade_workbench.cases import load_case
    from upgrade_workbench.cases.manifest import read_verified_file

    parent = load_case(EVIDENCE_PARENT / "manifest.json")
    parent_bundle = json.loads(read_verified_file(
        parent.root, "evidence/bundle.json", parent.manifest.file_hashes["evidence/bundle.json"]))
    raw = read_verified_file(parent.root, "evidence/migration.rst",
                             parent.manifest.file_hashes["evidence/migration.rst"])
    save(CASE / "evidence/migration.rst", raw)
    save(CASE / "evidence/LICENSE", read_verified_file(
        parent.root, "evidence/LICENSE", parent.manifest.file_hashes["evidence/LICENSE"]))
    lines = raw.decode("utf-8").splitlines()
    # RST 小节标题后紧跟同长下划线；以它们为章节边界。
    starts = [index for index in range(len(lines) - 1)
              if lines[index].strip() and re.fullmatch(r"([=~^\-])\1{3,}", lines[index + 1].strip())
              and len(lines[index + 1].strip()) >= len(lines[index].strip())]
    entries = []
    for key, heading in EVIDENCE_HEADINGS.items():
        matches = [index for index in starts if lines[index].strip() == heading]
        if len(matches) != 1:
            raise ValueError(f"Heading not unique: {heading}")
        start = matches[0]
        following = [index for index in starts if index > start]
        end = (following[0] - 1) if following else len(lines)
        while end > start and not lines[end - 1].strip():
            end -= 1
        entries.append({"evidence_key": key, "path": "evidence/migration.rst",
                        "sha256": digest(raw), "heading": heading,
                        "start_line": start + 1, "end_line": end})
    dump(CASE / "evidence/bundle.json", {
        "schema_version": 1, "package": "sqlalchemy",
        "old_version": "1.4.54", "new_version": "2.0.38",
        "repository": parent_bundle["repository"], "revision": parent_bundle["revision"],
        "upstream_path": parent_bundle["upstream_path"], "license_path": "evidence/LICENSE",
        "entries": entries,
    })
    print(json.dumps({"entries": [(e["evidence_key"], e["start_line"], e["end_line"]) for e in entries]}))


def inputs() -> dict[str, str]:
    files = {
        path.relative_to(CASE).as_posix(): digest(path.read_bytes())
        for parent in ("source", "checks", "requirements", "evidence", "upstream")
        for path in sorted((CASE / parent).rglob("*")) if path.is_file()
    }
    for name in ("SOURCE.json", "business-contract.md", "contract-requirements.json",
                 "environment.json", "oracle-sources.md"):
        files[name] = digest((CASE / name).read_bytes())
    return files


def admit(label: str) -> None:
    """模型前准入：旧依赖必须全过，直接升级必须失败；报告按标签保留，不覆盖。"""
    from upgrade_workbench.execution import DockerExecutor

    report_path = WORK / f"admission-{label}.json"
    if report_path.exists():
        raise ValueError("Admission exists; inspect it instead of rerunning")
    report = {"purpose": "pre_solver_admission", "label": label, "revision": REVISION,
              "started_at": datetime.now(UTC).isoformat(), "model_requests_before": 0,
              "input_sha256": inputs(), "base_image": BASE, "environments": {}, "stages": {}}
    try:
        for stage, lock in (("old", "old.txt"), ("direct", "new.txt")):
            executor = DockerExecutor(TASK_ROOT / label / stage)
            prepared = executor.prepare_environment(
                CASE / "requirements" / lock, base_image=BASE,
                python_version="3.9", timeout_seconds=600)
            report["environments"][stage] = prepared
            result = executor.verify(prepared["image_id"], CASE / "source",
                                     CASE / "checks", timeout_seconds=240, diagnostic=True)
            report["stages"][stage] = result
            print(json.dumps({"stage": stage, "status": result["status"],
                              "tests": result.get("tests")}), flush=True)
            if stage == "old" and result["status"] != "passed":
                report["decision"] = "old_baseline_invalid"
                break
    finally:
        dump(report_path, report)
        print(json.dumps({"admission": str(report_path)}), flush=True)


def seal(label: str) -> None:
    """只接受与准入报告完全同字节的输入，生成 v2 清单与评价分组。"""
    from upgrade_workbench.cases import load_case
    from upgrade_workbench.cases.manifest import inventory_files
    from upgrade_workbench.cases.requirements import requirements_for_case
    from upgrade_workbench.evaluation import load_evaluation
    from upgrade_workbench.evidence import load_version_evidence

    if (CASE / "manifest.json").exists():
        raise ValueError("Case is already sealed")
    admission = json.loads((WORK / f"admission-{label}.json").read_bytes())
    if admission["input_sha256"] != inputs() or admission["base_image"] != BASE:
        raise ValueError("Admission inputs changed after Docker run")
    groups = {}
    for group in ("feedback", "acceptance"):
        paths = sorted((CASE / "checks" / group).glob("*.py"))
        nodes = [f"{path.relative_to(CASE / 'checks').as_posix()}::{node.name}"
                 for path in paths for node in ast.parse(path.read_bytes()).body
                 if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        groups[group] = {"paths": [path.relative_to(CASE / "checks").as_posix() for path in paths],
                         "nodeids": sorted(nodes), "expected_count": len(nodes)}
    expected = {node for item in groups.values() for node in item["nodeids"]}
    for stage in ("old", "direct"):
        result = admission["stages"].get(stage)
        counts = (result or {}).get("tests", {})
        if not result or result.get("status") != ("passed" if stage == "old" else "failed"):
            raise ValueError(f"Missing old-pass/direct-fail contrast: {stage}")
        if (stage == "old" and (counts.get("passed") != len(expected) or counts.get("errors")
                                or set(result.get("nodeids", [])) != expected)):
            raise ValueError("Old baseline does not pass every registered check")
        if not all(item.get("ok") for item in result.get("cleanup", {}).values()):
            raise ValueError(f"Cleanup incomplete: {stage}")
    original = json.loads((CASE / "SOURCE.json").read_bytes())
    for name, record in original["files"].items():
        if digest((CASE / name).read_bytes()) != record["sha256"]:
            raise ValueError(f"Upstream source changed: {name}")
    dump(CASE / "evaluation.json", {
        "schema_version": 1, "case_id": CASE.name, "split": "holdout", "groups": groups,
        "note": "Frozen before any model request; written from upstream README/tests and admitted in Docker before task creation.",
    })
    requirements = json.loads((CASE / "contract-requirements.json").read_bytes())
    if requirements["contract_sha256"] != digest((CASE / "business-contract.md").read_bytes()):
        raise ValueError("Business contract hash changed")
    allowed = sorted(name.removeprefix("source/") for name in original["files"]
                     if name.startswith("source/sqlalchemy_mixins/") and name.endswith(".py"))
    dump(CASE / "manifest.json", {
        "schema_version": 2, "case_id": CASE.name,
        "title": "sqlalchemy-mixins 1.5.3 ORM helper migration from SQLAlchemy 1.4.54 to 2.0.38",
        "source": {"kind": "upstream_snapshot", "repository": REPOSITORY, "revision": REVISION,
                   "license": "MIT",
                   "description": "Pinned complete v1.5.3 library package; upstream tests kept as provenance only; the upstream 2.0 support release is withheld."},
        "review": {"status": "reviewed",
                   "note": "2026-10-17 reviewed pinned source, MIT license, contract and checks; target code executed only in isolated Docker with network none."},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "requirements_path": "contract-requirements.json",
        "allowed_changes": allowed,
        "file_hashes": {name: digest((CASE / name).read_bytes())
                        for name in sorted(inventory_files(CASE)) if name != "manifest.json"},
        "expected_new_original": "failed",
    })
    case = load_case(CASE / "manifest.json")
    load_evaluation(case)
    requirements_for_case(case)
    load_version_evidence(case)
    print(json.dumps({"fingerprint": case.fingerprint, "files": len(case.manifest.file_hashes),
                      "groups": {k: v["expected_count"] for k, v in groups.items()}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["acquire", "evidence", "admit", "seal"])
    parser.add_argument("--label", default="r1")
    args = parser.parse_args()
    if args.command in {"admit", "seal"}:
        {"admit": admit, "seal": seal}[args.command](args.label)
    else:
        {"acquire": acquire, "evidence": evidence}[args.command]()
