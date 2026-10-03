"""把核验后的源文件和受限统一 diff 放入独立工作目录。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from .manifest import (
    CaseValidationError,
    LoadedCase,
    assert_no_links,
    inventory_files,
    load_case,
    read_verified_file,
    relative_path,
)


class PatchValidationError(ValueError):
    """候选补丁不满足修改边界或不能完整应用。"""


class PatchInfrastructureError(RuntimeError):
    """Git/宿主进程基础设施失败，不代表候选补丁语义错误。"""


def _validated_patch(data: bytes, allowed: set[str]) -> set[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise PatchValidationError("Candidate patch must be UTF-8 text") from error
    if "\x00" in text:
        raise PatchValidationError("Binary patches are forbidden")
    forbidden = (
        "new file mode ", "deleted file mode ", "old mode ", "new mode ",
        "rename from ", "rename to ", "copy from ", "copy to ",
        "similarity index ", "dissimilarity index ", "GIT binary patch", "Binary files ",
    )
    if any(line.startswith(forbidden) for line in text.splitlines()):
        raise PatchValidationError("Add/delete/rename/mode/binary changes are forbidden")
    try:
        parsed = PatchSet(text)
    except (UnidiffParseError, ValueError, IndexError) as error:
        raise PatchValidationError(f"Invalid unified diff: {error}") from error
    if not parsed:
        raise PatchValidationError("Candidate patch contains no file changes")
    names: set[str] = set()
    for item in parsed:
        if item.is_added_file or item.is_removed_file or item.is_binary_file or not item:
            raise PatchValidationError("Only existing text files with hunks may be changed")
        before, after = item.source_file, item.target_file
        if not before.startswith("a/") or not after.startswith("b/"):
            raise PatchValidationError("Diff paths must use canonical a/ and b/ prefixes")
        try:
            old_name = relative_path(before[2:])
            new_name = relative_path(after[2:])
        except ValueError as error:
            raise PatchValidationError(str(error)) from error
        if old_name != new_name:
            raise PatchValidationError("Renames are forbidden")
        if old_name not in allowed or not old_name.endswith(".py"):
            raise PatchValidationError(f"Patch path is not allowed: {old_name}")
        if old_name in names:
            raise PatchValidationError(f"Duplicate file patch: {old_name}")
        names.add(old_name)
        for line in str(item.patch_info or "").splitlines():
            if line.startswith("diff --git ") and line != f"diff --git a/{old_name} b/{old_name}":
                raise PatchValidationError("Git header paths disagree with unified diff paths")
            if line.startswith("index ") and not re.fullmatch(
                r"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?", line
            ):
                raise PatchValidationError("Unsupported Git index mode or metadata")
    return names


def _git(arguments: list[str], cwd: Path, data: bytes | None = None) -> None:
    # 显式隔离 Git 配置和仓库定位，避免继承宿主仓库或全局辅助程序。
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    options: list[str] = []
    if os.name == "nt":
        # 长文件名支持只影响本次Git，不修改用户配置。进程cwd自身另有Windows限制。
        if len(str(cwd.absolute())) >= 260:
            raise PatchInfrastructureError(
                "Windows Git process working directory exceeds the supported length; "
                "use a shorter work_root for a new task, never relocate a registered task"
            )
        options = ["-c", "core.longpaths=true"]
    try:
        result = subprocess.run(
            ["git", *options, *arguments], cwd=cwd, env=env, input=data,
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PatchInfrastructureError("Git process could not be started or completed") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if arguments[:5] == ["-c", "init.templateDir=", "init", "--bare", "--quiet"]:
            raise PatchInfrastructureError("Git temporary repository initialization failed")
        raise PatchValidationError(f"Git patch validation failed: {detail}")


def _remove_owned_directory(path: Path, parent: Path) -> None:
    """仅清理本次创建且仍位于已核验父目录的目录。"""
    assert_no_links(parent)
    assert_no_links(path)
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or resolved == parent.resolve():
        raise PatchValidationError(f"Refusing cleanup outside the owned parent: {path}")
    shutil.rmtree(resolved)


def stage_source(
    case: LoadedCase, destination: Path, candidate_patch: Path | None = None
) -> dict[str, object]:
    """重新核验案例，复制源文件，然后原子地接受受限补丁结果。"""
    current = load_case(case.manifest_path)
    if current.fingerprint != case.fingerprint:
        raise CaseValidationError("Case manifest changed after loading")
    destination = Path(destination).absolute()
    assert_no_links(destination)
    if destination.exists():
        raise PatchValidationError(f"Destination already exists: {destination}")
    if destination.resolve().is_relative_to(current.root):
        raise PatchValidationError("Destination must be outside the immutable case root")
    data: bytes | None = None
    expected_changes: set[str] = set()
    if candidate_patch is not None:
        assert_no_links(Path(candidate_patch))
        try:
            data = Path(candidate_patch).read_bytes()
        except OSError as error:
            raise PatchValidationError(f"Cannot read candidate patch: {error}") from error
        expected_changes = _validated_patch(data, set(current.manifest.allowed_changes))

    destination.parent.mkdir(parents=True, exist_ok=True)
    assert_no_links(destination.parent)
    temporary = Path(tempfile.mkdtemp(prefix=".upgrade-stage-", dir=destination.parent))
    worktree = temporary / "source"
    worktree.mkdir()
    try:
        original: dict[str, str] = {}
        for name, digest in sorted(current.manifest.file_hashes.items()):
            if not name.startswith("source/"):
                continue
            relative = name[len("source/"):]
            contents = read_verified_file(current.root, name, digest)
            target = worktree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
            original[relative] = digest
        if data is not None:
            # Git内部使用相对于固定cwd的短路径，避免Windows深目录触发GIT_DIR长度限制。
            # 仓库仍在本次独占临时目录中；不依赖宿主仓库发现或全局配置。
            _git(["-c", "init.templateDir=", "init", "--bare", "--quiet", "git"], temporary)
            command = ["--git-dir=../git", "--work-tree=.", "apply", "--whitespace=nowarn"]
            _git([*command, "--check", "-"], worktree, data)
            _git([*command, "-"], worktree, data)
        actual = inventory_files(worktree)
        if actual != set(original):
            raise PatchValidationError("Staged file inventory changed after patch application")
        changed = {
            name for name in actual
            if hashlib.sha256((worktree / name).read_bytes()).hexdigest() != original[name]
        }
        if changed != expected_changes:
            raise PatchValidationError("Actual changed files differ from validated patch paths")
        if load_case(case.manifest_path).fingerprint != case.fingerprint:
            raise CaseValidationError("Case changed during source staging")
        assert_no_links(destination.parent)
        if destination.exists():
            raise PatchValidationError(f"Destination appeared during staging: {destination}")
        worktree.rename(destination)
        return {
            "source_dir": str(destination),
            "candidate_sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
            "changed_files": sorted(changed),
        }
    finally:
        _remove_owned_directory(temporary, destination.parent)
