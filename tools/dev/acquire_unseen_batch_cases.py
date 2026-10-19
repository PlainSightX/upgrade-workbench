"""固定三个未见仓库的上游源码，准入 Pydantic 1->2 与 SQLAlchemy 1.4->2.0 案例。

沿用 sqlalchemy-mixins 案例的顺序：acquire 只解包固定归档；evidence 复用已核验哈希的
官方迁移原文；freeze 在任何模型请求前记录合同、条目与检查哈希；admit 在 Docker 中运行
旧依赖与直接升级两路；seal 只在旧环境全过、直接升级失败且输入与冻结记录逐字节一致后
生成清单。宿主不导入、不执行目标源码。

案例封存后重跑 acquire / evidence 只会逐字节确认，seal 遇到已有清单会拒绝。
归档需先从各案例的 archive_url 下载到工作目录 downloads/ 下，脚本只核对 SHA-256，不联网。

用法：acquire_unseen_batch_cases.py <command> --case <case_id> [--label r1]
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
# 归档、冻结记录与准入报告放在 Git 忽略的 .local/ 下；UW_UB_WORK 可改到别处。
WORK = Path(os.environ.get("UW_UB_WORK", ROOT / ".local/unseen-batch"))
# Docker 每阶段的工作目录；Windows 路径过长时可用 UW_UB_DOCKER_ROOT 指向更短目录。
DOCKER_ROOT = Path(os.environ.get("UW_UB_DOCKER_ROOT", WORK / "docker"))
BASE = "python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b"

# 迁移原文章节：只按标题登记该家族通用的官方章节，不按已知失败挑选，也不写修法。
PYDANTIC_HEADINGS = {
    "pydantic-v2-base-model": "### Changes to `pydantic.BaseModel`",
    "pydantic-v2-field": "### Changes to `pydantic.Field`",
    "pydantic-v2-config": "### Changes to config",
    "pydantic-v2-validators": "### Changes to validators",
    "pydantic-v2-optional-fields": "#### Required, optional, and nullable fields",
    "pydantic-v2-moved": "## Moved in Pydantic V2",
    "pydantic-v2-removed": "## Removed in Pydantic V2",
}
SQLALCHEMY_HEADINGS = {
    "sqlalchemy-v2-loader-string-paths":
        "ORM Query  - Joining / loading on relationships uses attributes, not strings",
    "sqlalchemy-v2-query-get": "ORM Query - get() method moves to Session",
    "sqlalchemy-v2-query-unified-select": "ORM Query Unified with Core Select",
    "sqlalchemy-v2-dynamic-relationship":
        "\"Dynamic\" relationship loaders superseded by \"Write Only\"",
    "sqlalchemy-v2-declarative-first-class": "Declarative becomes a first class API",
}

CASES: dict[str, dict] = {
    "pydantic-cli-pydantic-2": {
        "repository": "https://github.com/mpkocher/pydantic-cli",
        "revision": "50d1edaa239a9b1d16e4f089f5151bd0930d1329",
        "tag": "v4.3.0",
        "archive": "pcli-50d1eda.tar.gz",
        "archive_sha256": "99d9ca068bd0820399abe99caf50da5bb7cb65d193ff73fe8b5e8d875dda1ce9",
        "license": "MIT",
        "package": "pydantic_cli",
        "tests_prefix": "pydantic_cli/tests/",
        "keep": {"LICENSE", "README.md"},
        "provenance": {"setup.py", "REQUIREMENTS.txt", "REQUIREMENTS-TEST.txt", "CHANGELOG.md"},
        "family": "pydantic",
        "evidence_parent": "fastapi-required-list-pydantic-2-r2",
        "evidence_license": "evidence/pydantic-LICENSE",
        "old_version": "1.10.15", "new_version": "2.6.4",
        "title": "pydantic-cli 4.3.0 CLI model migration from Pydantic 1.10.15 to 2.6.4",
        "description": "Pinned complete v4.3.0 library package; upstream tests and examples kept as provenance only; later upstream Pydantic 2 releases are withheld.",
    },
    "flask-pydantic-pydantic-2": {
        "repository": "https://github.com/pallets-eco/flask-pydantic",
        "revision": "ebb08fed208bcc478e286cbd195328cc6993bd28",
        "tag": "v0.11.0",
        "archive": "fpyd-ebb08fe.tar.gz",
        "archive_sha256": "a8407463c6e9aec163baf35a14a528234cc57c9983bb9f2f332394bf726d67fe",
        "license": "MIT",
        "package": "flask_pydantic",
        "tests_prefix": "tests/",
        "keep": {"LICENSE", "README.md"},
        "provenance": {"setup.py", "requirements/base.pip", "requirements/test.pip", "HISTORY.md"},
        "family": "pydantic",
        "evidence_parent": "fastapi-required-list-pydantic-2-r2",
        "evidence_license": "evidence/pydantic-LICENSE",
        "old_version": "1.10.15", "new_version": "2.6.4",
        "title": "Flask-Pydantic 0.11.0 request validation migration from Pydantic 1.10.15 to 2.6.4",
        "description": "Pinned complete v0.11.0 library package; upstream tests kept as provenance only; the upstream 0.12.0 Pydantic 2 release is withheld.",
    },
    "sqlalchemy-filters-sqlalchemy-2": {
        "repository": "https://github.com/juliotrigo/sqlalchemy-filters",
        "revision": "e182378a7a53f05b8c11f9889787bd5b12579a5c",
        "tag": "v0.13.0",
        "archive": "sqlf-e182378.tar.gz",
        "archive_sha256": "45844a5500b1e0aafc4e2b7a40a555cc15caa88a532dc05b3af8b9af311f01d3",
        "license": "Apache-2.0",
        "package": "sqlalchemy_filters",
        "tests_prefix": "test/",
        "keep": {"LICENSE", "README.rst"},
        "provenance": {"setup.py", "tox.ini", "CHANGELOG.rst", ".github/workflows/tests.yml"},
        "family": "sqlalchemy",
        "evidence_parent": "sqlalchemy-mixins-sqlalchemy-2",
        "evidence_license": "evidence/LICENSE",
        "old_version": "1.4.54", "new_version": "2.0.38",
        "title": "sqlalchemy-filters 0.13.0 query helper migration from SQLAlchemy 1.4.54 to 2.0.38",
        "description": "Pinned complete v0.13.0 library package; upstream tests kept as provenance only; no upstream SQLAlchemy 2.0 release exists for this tag.",
    },
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


def case_dir(case_id: str) -> Path:
    return ROOT / "cases" / case_id


def acquire(case_id: str) -> None:
    """库源码进入 source/，上游测试与打包元数据只作来源留存放入 upstream/，不进入检查。"""
    spec, case = CASES[case_id], case_dir(case_id)
    archive_path = WORK / "downloads" / spec["archive"]
    url = f"{spec['repository'].replace('github.com', 'codeload.github.com')}/tar.gz/{spec['revision']}"
    if not archive_path.is_file():
        raise FileNotFoundError(f"Download {url} to {archive_path} first")
    if digest(archive_path.read_bytes()) != spec["archive_sha256"]:
        raise ValueError("Pinned upstream archive changed")
    prefix = spec["repository"].rsplit("/", 1)[1] + "-" + spec["revision"]
    package = spec["package"] + "/"
    selected: dict[str, bytes] = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] != prefix
                    or member.issym() or member.islnk()):
                raise ValueError(f"Unsafe archive member: {member.name}")
            if member.isdir() or (len(path.parts) == 1):
                continue
            if not member.isfile():
                raise ValueError(f"Unexpected archive member type: {member.name}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative.startswith(spec["tests_prefix"]) or relative.startswith(package + "examples/"):
                target = "upstream/" + relative
            elif relative.startswith(package):
                target = "source/" + relative
            elif relative in spec["keep"]:
                target = "source/" + relative
            elif relative in spec["provenance"]:
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
        save(case / name, contents)
    dump(case / "SOURCE.json", {
        "repository": spec["repository"],
        "revision": spec["revision"],
        "tag": spec["tag"],
        "archive_url": url,
        "archive_sha256": spec["archive_sha256"],
        "license": spec["license"],
        "files": {name: {"upstream_path": name.split("/", 1)[1], "sha256": digest(contents)}
                  for name, contents in sorted(selected.items())},
        "target_code_executed_on_host": False,
        "classification": "unseen_public_repository_never_used_by_this_workbench",
        "upstream_answer_patch_in_solver_input": False,
    })
    print(json.dumps({"case": case_id, "files": len(selected)}))


def _sections(lines: list[str], family: str) -> list[int]:
    """返回章节标题行号：Markdown 以 # 开头；RST 标题后紧跟同长下划线。"""
    if family == "pydantic":
        fence, starts = False, []
        for index, line in enumerate(lines):
            if line.startswith("```"):
                fence = not fence
            elif not fence and re.match(r"^#{1,6} ", line):
                starts.append(index)
        return starts
    return [index for index in range(len(lines) - 1)
            if lines[index].strip() and re.fullmatch(r"([=~^\-])\1{3,}", lines[index + 1].strip())
            and len(lines[index + 1].strip()) >= len(lines[index].strip())]


def _level(line: str, family: str) -> int:
    return len(line) - len(line.lstrip("#")) if family == "pydantic" else 0


def evidence(case_id: str) -> None:
    """复用此前案例已核验的同一份官方迁移原文字节，按标题重新定位行范围。"""
    from upgrade_workbench.cases import load_case
    from upgrade_workbench.cases.manifest import read_verified_file

    spec, case = CASES[case_id], case_dir(case_id)
    family = spec["family"]
    parent = load_case(ROOT / "cases" / spec["evidence_parent"] / "manifest.json")
    parent_bundle = json.loads(read_verified_file(
        parent.root, "evidence/bundle.json", parent.manifest.file_hashes["evidence/bundle.json"]))
    doc_name = "evidence/migration.md" if family == "pydantic" else "evidence/migration.rst"
    raw = read_verified_file(parent.root, doc_name, parent.manifest.file_hashes[doc_name])
    save(case / doc_name, raw)
    save(case / spec["evidence_license"], read_verified_file(
        parent.root, spec["evidence_license"], parent.manifest.file_hashes[spec["evidence_license"]]))
    lines = raw.decode("utf-8").splitlines()
    starts = _sections(lines, family)
    headings = PYDANTIC_HEADINGS if family == "pydantic" else SQLALCHEMY_HEADINGS
    entries = []
    for key, heading in headings.items():
        matches = [index for index in starts if lines[index].strip() == heading]
        if len(matches) != 1:
            raise ValueError(f"Heading not unique: {heading}")
        start = matches[0]
        level = _level(lines[start], family)
        # Markdown 章节延续到同级或更高级标题前；RST 延续到下一个标题前。
        following = [index for index in starts if index > start
                     and (family != "pydantic" or _level(lines[index], family) <= level)]
        end = (following[0] - 1) if following else len(lines)
        while end > start and not lines[end - 1].strip():
            end -= 1
        entries.append({"evidence_key": key, "path": doc_name, "sha256": digest(raw),
                        "heading": heading, "start_line": start + 1, "end_line": end})
    dump(case / "evidence/bundle.json", {
        "schema_version": 1, "package": family,
        "old_version": spec["old_version"], "new_version": spec["new_version"],
        "repository": parent_bundle["repository"], "revision": parent_bundle["revision"],
        "upstream_path": parent_bundle["upstream_path"], "license_path": spec["evidence_license"],
        "entries": entries,
    })
    print(json.dumps({"entries": [(e["evidence_key"], e["start_line"], e["end_line"]) for e in entries]}))


def inputs(case_id: str) -> dict[str, str]:
    case = case_dir(case_id)
    files = {
        path.relative_to(case).as_posix(): digest(path.read_bytes())
        for parent in ("source", "checks", "requirements", "evidence", "upstream")
        for path in sorted((case / parent).rglob("*")) if path.is_file()
    }
    for name in ("SOURCE.json", "business-contract.md", "contract-requirements.json",
                 "environment.json", "oracle-sources.md"):
        files[name] = digest((case / name).read_bytes())
    return files


FROZEN = ("business-contract.md", "contract-requirements.json")


def freeze(case_id: str) -> None:
    """模型请求前登记合同、条目与两组检查的哈希和 UTC 时间；已存在则拒绝覆盖。"""
    case = case_dir(case_id)
    record_path = WORK / case_id / "freeze.json"
    if record_path.exists():
        raise ValueError("Freeze record exists; contract and checks are already frozen")
    names = sorted(path.relative_to(case).as_posix() for path in (case / "checks").rglob("*")
                   if path.is_file()) + list(FROZEN)
    dump(record_path, {"case_id": case_id, "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "model_requests_before": 0,
                       "sha256": {name: digest((case / name).read_bytes()) for name in names}})
    print(record_path.read_text(encoding="utf-8"))


def admit(case_id: str, label: str, purpose: str = "pre_solver_admission") -> None:
    """模型前准入：旧依赖必须全过，直接升级必须失败；报告按标签保留，不覆盖。

    purpose=authoring_dry_run 时是冻结前的编写期试运行，报告单独命名，不能用于 seal。
    """
    from upgrade_workbench.execution import DockerExecutor

    case = case_dir(case_id)
    prefix = "admission" if purpose == "pre_solver_admission" else "draft"
    report_path = WORK / case_id / f"{prefix}-{label}.json"
    if report_path.exists():
        raise ValueError("Admission exists; inspect it instead of rerunning")
    if purpose == "pre_solver_admission" and not (WORK / case_id / "freeze.json").exists():
        raise ValueError("Freeze contract and checks before formal admission")
    environment = json.loads((case / "environment.json").read_bytes())
    report = {"purpose": purpose, "case_id": case_id, "label": label,
              "revision": CASES[case_id]["revision"], "started_at": datetime.now(UTC).isoformat(),
              "model_requests_before": 0, "input_sha256": inputs(case_id), "base_image": BASE,
              "environments": {}, "stages": {}}
    try:
        for stage, lock in (("old", "old.txt"), ("direct", "new.txt")):
            executor = DockerExecutor(DOCKER_ROOT / case_id / label / stage)
            prepared = executor.prepare_environment(
                case / "requirements" / lock, base_image=BASE,
                python_version=environment["python_version"], timeout_seconds=600)
            report["environments"][stage] = prepared
            result = executor.verify(prepared["image_id"], case / "source",
                                     case / "checks", timeout_seconds=240, diagnostic=True)
            report["stages"][stage] = result
            print(json.dumps({"stage": stage, "status": result["status"],
                              "tests": result.get("tests")}), flush=True)
            if stage == "old" and result["status"] != "passed":
                report["decision"] = "old_baseline_invalid"
                break
    finally:
        dump(report_path, report)
        print(json.dumps({"admission": str(report_path.relative_to(ROOT))}), flush=True)


def seal(case_id: str, label: str) -> None:
    """只接受与冻结记录和准入报告完全同字节的输入，生成 v2 清单与评价分组。"""
    from upgrade_workbench.cases import load_case
    from upgrade_workbench.cases.manifest import inventory_files
    from upgrade_workbench.cases.requirements import requirements_for_case
    from upgrade_workbench.evaluation import load_evaluation
    from upgrade_workbench.evidence import load_version_evidence

    spec, case = CASES[case_id], case_dir(case_id)
    if (case / "manifest.json").exists():
        raise ValueError("Case is already sealed")
    frozen = json.loads((WORK / case_id / "freeze.json").read_bytes())
    for name, expected in frozen["sha256"].items():
        if digest((case / name).read_bytes()) != expected:
            raise ValueError(f"Frozen input changed: {name}")
    admission = json.loads((WORK / case_id / f"admission-{label}.json").read_bytes())
    if admission["input_sha256"] != inputs(case_id) or admission["base_image"] != BASE:
        raise ValueError("Admission inputs changed after Docker run")
    groups = {}
    for group in ("feedback", "acceptance"):
        paths = sorted((case / "checks" / group).glob("test_*.py"))
        nodes = [f"{path.relative_to(case / 'checks').as_posix()}::{node.name}"
                 for path in paths for node in ast.parse(path.read_bytes()).body
                 if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        groups[group] = {"paths": [path.relative_to(case / "checks").as_posix() for path in paths],
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
    original = json.loads((case / "SOURCE.json").read_bytes())
    for name, record in original["files"].items():
        if digest((case / name).read_bytes()) != record["sha256"]:
            raise ValueError(f"Upstream source changed: {name}")
    dump(case / "evaluation.json", {
        "schema_version": 1, "case_id": case.name, "split": "holdout", "groups": groups,
        "note": "Frozen before any model request; written from upstream README/docstrings/tests and admitted in Docker before task creation.",
    })
    requirements = json.loads((case / "contract-requirements.json").read_bytes())
    if requirements["contract_sha256"] != digest((case / "business-contract.md").read_bytes()):
        raise ValueError("Business contract hash changed")
    package = "source/" + spec["package"] + "/"
    allowed = sorted(name.removeprefix("source/") for name in original["files"]
                     if name.startswith(package) and name.endswith(".py"))
    dump(case / "manifest.json", {
        "schema_version": 2, "case_id": case.name, "title": spec["title"],
        "source": {"kind": "upstream_snapshot", "repository": spec["repository"],
                   "revision": spec["revision"], "license": spec["license"],
                   "description": spec["description"]},
        "review": {"status": "reviewed",
                   "note": "2026-10-19 reviewed pinned source, license, contract and checks; target code executed only in isolated Docker with network none."},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "requirements_path": "contract-requirements.json",
        "allowed_changes": allowed,
        "file_hashes": {name: digest((case / name).read_bytes())
                        for name in sorted(inventory_files(case)) if name != "manifest.json"},
        "expected_new_original": "failed",
    })
    loaded = load_case(case / "manifest.json")
    load_evaluation(loaded)
    requirements_for_case(loaded)
    load_version_evidence(loaded)
    print(json.dumps({"fingerprint": loaded.fingerprint, "files": len(loaded.manifest.file_hashes),
                      "groups": {k: v["expected_count"] for k, v in groups.items()}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["acquire", "evidence", "draft", "freeze", "admit", "seal"])
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--label", default="r1")
    args = parser.parse_args()
    if args.command == "draft":
        admit(args.case, args.label, purpose="authoring_dry_run")
    elif args.command in {"admit", "seal"}:
        {"admit": admit, "seal": seal}[args.command](args.case, args.label)
    else:
        {"acquire": acquire, "evidence": evidence, "freeze": freeze}[args.command](args.case)
