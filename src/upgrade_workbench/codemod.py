"""隔离运行官方静态迁移工具；仅产出待审补丁，不执行目标应用。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from upgrade_workbench.cases import load_case
from upgrade_workbench.cases.manifest import (
    LoadedCase,
    assert_no_links,
    inventory_files,
    read_verified_file,
)

TOOL_VERSION = "0.8.0"
TOOL_REVISION = "e97380c321090b4fc8641f761df742bdb76060d6"
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/0e/3a/0092e7b523307b71a203c6c6a6608dbee2a92c2616c264fd7fdf561279b5/"
    "bump_pydantic-0.8.0-py3-none-any.whl"
)
WHEEL_SHA256 = "6cbb4deb5869a69baa5a477f28f3e2d8fb09b687e114c018bd54470590ae7bf7"
TOOL_REQUIREMENTS = (
    "bump-pydantic==0.8.0\n"
    "typer==0.9.0\n"
    "click==8.1.7\n"
    "libcst==1.1.0\n"
    "rich==13.7.1\n"
    "typing-extensions==4.14.0\n"
)

_NON_APPLICATION_PARTS = {"test", "tests", "checks", "fixtures", "__pycache__"}


def application_inputs(case: LoadedCase) -> tuple[list[str], list[str]]:
    """选择登记应用包的完整Python上下文，不把可编辑文件误当完整输入。"""
    prefix = case.manifest.snapshot_dir + "/"
    allowed = [PurePosixPath(name) for name in case.manifest.allowed_changes]
    if any(set(path.parts[:-1]) & _NON_APPLICATION_PARTS for path in allowed):
        raise ValueError("Application edit allowlist must not include test or check directories")
    roots = {path.parts[0] for path in allowed if len(path.parts) > 1}
    includes_flat_application = any(len(path.parts) == 1 for path in allowed)
    inputs = []
    outside = []
    for registered in sorted(case.manifest.file_hashes):
        if not registered.startswith(prefix) or not registered.endswith(".py"):
            continue
        name = registered.removeprefix(prefix)
        path = PurePosixPath(name)
        if set(path.parts[:-1]) & _NON_APPLICATION_PARTS:
            continue
        if includes_flat_application or len(path.parts) == 1 or path.parts[0] in roots:
            inputs.append(name)
        else:
            outside.append(name)
    if outside:
        # 无法确认归属时明示失败，不能悄悄丢掉可能被应用导入的另一源码包。
        raise ValueError(f"Unclassified registered Python source roots: {outside}")
    if not inputs or set(case.manifest.allowed_changes) - set(inputs):
        raise ValueError("Application inventory is empty or omits an editable module")
    input_roots = sorted(roots | ({"."} if includes_flat_application else set()))
    return inputs, input_roots


def collect_output_patch(
    source: Path, original: dict[str, bytes], allowed_changes: list[str], *, allow_empty: bool = False,
) -> tuple[str, list[str], dict[str, str]]:
    """保留原始工具文件，仅把同输入清单、同修改范围的输出转为候选。"""
    actual_files = inventory_files(source)
    if set(actual_files) != set(original):
        raise ValueError("Official tool added or removed a target file")
    changed = []
    patch = []
    hashes = {}
    for name, contents in sorted(original.items()):
        output = (source / name).read_bytes()
        hashes[name] = hashlib.sha256(output).hexdigest()
        before = contents.decode("utf-8").replace("\r\n", "\n")
        after = output.decode("utf-8").replace("\r\n", "\n")
        if before == after:
            continue
        changed.append(name)
        patch.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
    if set(changed) - set(allowed_changes):
        raise ValueError("Official tool modified files outside the candidate allowlist")
    if not patch and not allow_empty:
        raise ValueError("Official tool produced no candidate diff")
    return "".join(patch), changed, hashes


def run_official_tool(manifest: Path, output_root: Path, *, reviewed_tool: bool) -> dict:
    """复用已审查的静态转换入口；每次独立记录，不覆盖原始官方产物。"""
    if reviewed_tool is not True:
        raise ValueError("Read the pinned tool source before approving its static execution")
    case = load_case(manifest)
    from .evidence import migration_package

    if migration_package(case) != "pydantic":
        raise ValueError("No reviewed official seed for this family; explicitly select seed='none'")
    input_files, input_roots = application_inputs(case)
    output_root = output_root.absolute()
    assert_no_links(output_root)
    output_root = output_root.resolve()
    if output_root.is_relative_to(case.root):
        raise ValueError("Official tool output must be outside the immutable case")
    directory = output_root / f"bump-pydantic-{TOOL_VERSION}-{uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "source"
    record: dict = {
        "baseline_kind": "official_tool",
        "tool": "bump-pydantic",
        "tool_version": TOOL_VERSION,
        "tool_revision": TOOL_REVISION,
        "tool_repository": "https://github.com/pydantic/bump-pydantic",
        "tool_review": "CLI reads and rewrites LibCST nodes; does not import or execute target code; next_file returns None",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_fingerprint": case.fingerprint,
        "case_manifest": str(case.manifest_path),
        "allowed_files": case.manifest.allowed_changes,
        "input_files": input_files,
        "input_roots": input_roots,
        "input_python_modules": len(input_files),
        "input_hashes": {
            name: case.manifest.file_hashes[f"{case.manifest.snapshot_dir}/{name}"]
            for name in input_files
        },
        "input_policy": "complete application context; only allowlisted output changes accepted",
        "commands": [],
        "target_code_executed": False,
        "model_calls": 0,
        "manual_semantic_edits": False,
        "candidate_origin": "official_tool",
        "diff_newlines": "LF unified diff; raw official-tool output files are also retained",
        "status": "preparing",
        "baseline_path": str(directory / "baseline.json"),
    }

    def save() -> None:
        (directory / "baseline.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def command(
        name: str, arguments: list[str], *, timeout: int = 600
    ) -> subprocess.CompletedProcess:
        # 静态工具也不需要密钥。只保留操作系统、临时目录、工具查找和联网代理基础项。
        allowed_env = {
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PATH",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PROGRAMDATA",
            "PROGRAMFILES",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            arguments,
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout_path = directory / f"{name}.stdout.log"
        stderr_path = directory / f"{name}.stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        record["commands"].append(
            {
                "name": name,
                "argv": arguments,
                "cwd": str(directory),
                "exit_code": result.returncode,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
        )
        save()
        if result.returncode:
            raise RuntimeError(f"{name} failed with exit code {result.returncode}; see {directory}")
        return result

    save()
    try:
        # 固定 wheel 作为工具来源证据；解读文件不触发其入口或任何目标代码。
        request = urllib.request.Request(
            WHEEL_URL, headers={"User-Agent": "Upgrade-Workbench-baseline"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            wheel = response.read(1_000_001)
        if hashlib.sha256(wheel).hexdigest() != WHEEL_SHA256:
            raise ValueError("Official tool wheel hash mismatch")
        wheel_path = directory / "bump_pydantic-0.8.0-py3-none-any.whl"
        wheel_path.write_bytes(wheel)
        record["tool_wheel"] = {"url": WHEEL_URL, "sha256": WHEEL_SHA256}
        with zipfile.ZipFile(wheel_path) as archive:
            for name in archive.namelist():
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ValueError("Unexpected wheel path")
                if name.startswith("bump_pydantic/") and name.endswith(".py"):
                    target = directory / "tool-source" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))

        original: dict[str, bytes] = {}
        for name in input_files:
            registered = f"{case.manifest.snapshot_dir}/{name}"
            contents = read_verified_file(
                case.root, registered, case.manifest.file_hashes[registered]
            )
            original[name] = contents
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
        (directory / "tool-requirements.in").write_text(TOOL_REQUIREMENTS, encoding="utf-8")
        environment = directory / ".venv"
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        command("create-environment", ["uv", "venv", "--python", "3.12", str(environment)])
        command(
            "lock-tool",
            [
                "uv",
                "pip",
                "compile",
                "tool-requirements.in",
                "--output-file",
                "tool-requirements.txt",
                "--python-version",
                "3.12",
                "--only-binary",
                ":all:",
                "--generate-hashes",
                "--no-emit-index-url",
            ],
        )
        record["tool_lock_sha256"] = hashlib.sha256(
            (directory / "tool-requirements.txt").read_bytes()
        ).hexdigest()
        command(
            "install-tool",
            [
                "uv",
                "pip",
                "sync",
                "--python",
                str(python),
                "--require-hashes",
                "--only-binary",
                ":all:",
                "tool-requirements.txt",
            ],
        )
        installed_modules = (
            environment / "Lib/site-packages"
            if sys.platform == "win32"
            else environment / "lib/python3.12/site-packages"
        )
        reviewed_modules = directory / "tool-source"
        for reviewed in reviewed_modules.rglob("*.py"):
            installed = installed_modules / reviewed.relative_to(reviewed_modules)
            if installed.read_bytes() != reviewed.read_bytes():
                raise ValueError("Installed official-tool code differs from the pinned wheel")
        record["installed_tool_matches_pinned_wheel"] = True
        command("tool-version", [str(python), "-I", "-m", "bump_pydantic", "--version"])
        command(
            "transform",
            [
                str(python),
                "-I",
                "-m",
                "bump_pydantic",
                "--log-file",
                "tool-errors.log",
                "source",
            ],
            timeout=180,
        )
        patch, changed, output_hashes = collect_output_patch(
            source, original, case.manifest.allowed_changes, allow_empty=True,
        )
        candidate = directory / "candidate.patch"
        candidate.write_text(patch, encoding="utf-8", newline="\n")
        record["candidate_path"] = str(candidate)
        record["candidate_sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        record["changed_files"] = changed
        error_log = directory / "tool-errors.log"
        record["tool_reported_errors"] = error_log.read_text(encoding="utf-8") if error_log.exists() else ""
        record["copied_output_hashes"] = output_hashes
        if load_case(manifest).fingerprint != case.fingerprint:
            raise ValueError("Case changed during official tool run")
        record["status"] = "pending_candidate_safety_review" if patch else "no_change"
        save()
        return record
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        # 失败时也记录原样输出身份；拒绝非法文件不意味着删除失败证据。
        if source.is_dir():
            try:
                record["raw_output_hashes"] = {
                    name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                    for name in inventory_files(source)
                }
            except (OSError, ValueError) as inventory_error:
                record["raw_output_inventory_error"] = str(inventory_error)
        save()
        return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path(".local/baselines"))
    parser.add_argument("--reviewed-tool", action="store_true")
    args = parser.parse_args()
    manifest = args.case / "manifest.json" if args.case.is_dir() else args.case
    result = run_official_tool(manifest, args.output_root, reviewed_tool=args.reviewed_tool)
    print(json.dumps({"baseline_path": result["baseline_path"],
                      "candidate": result.get("candidate_path"), "status": result["status"]}))
    if result["status"] == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
