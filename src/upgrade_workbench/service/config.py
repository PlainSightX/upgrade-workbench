"""服务只接收登记名称；路径、端点和密钥留在本机配置边界内。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..cases import load_case
from ..cases.manifest import assert_no_links
from ..cases.patches import PatchInfrastructureError, PatchValidationError, _git
from ..evaluation import load_evaluation
from ..evidence import load_version_evidence


def identity(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("invalid_identity")
    return value


class ServiceLayoutPreflightError(ValueError):
    """新服务作业的最坏嵌套布局不能安全创建或执行。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__("service_layout_preflight_failed")


def _nearest_existing(path: Path) -> Path:
    current = path.absolute()
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    return current


def _probe_writable(parent: Path) -> None:
    """在最近的真实父目录中做一次可创建/删除检查，不留下布局目录。"""
    parent = _nearest_existing(parent)
    assert_no_links(parent)
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise ServiceLayoutPreflightError("work_root_unwritable")
    try:
        with tempfile.TemporaryDirectory(prefix=".uw-preflight-", dir=parent):
            pass
    except (OSError, PermissionError) as error:
        raise ServiceLayoutPreflightError("work_root_unwritable") from error


def _probe_git_layout(parent: Path, cwd_length: int) -> None:
    """在实际父目录下的等长嵌套路径实测Git；自有文本不加载目标源码或运行代码。"""
    parent = _nearest_existing(parent)
    assert_no_links(parent)
    try:
        with tempfile.TemporaryDirectory(prefix=".uw-git-", dir=parent) as name:
            stage = Path(name)
            extra = cwd_length - len(str(stage / "source"))
            if extra >= 2:
                parts = max(1, (extra + 120) // 121)
                width, remainder = divmod(extra - parts, parts)
                for index in range(parts):
                    stage /= "p" * (width + (index < remainder))
            worktree = stage / "source"
            worktree.mkdir(parents=True)
            sample = worktree / "preflight.txt"
            sample.write_bytes(b"before\n")
            patch = b"--- a/preflight.txt\n+++ b/preflight.txt\n@@ -1 +1 @@\n-before\n+after\n"
            _git(["-c", "init.templateDir=", "init", "--bare", "--quiet", "git"], stage)
            command = ["--git-dir=../git", "--work-tree=.", "apply", "--whitespace=nowarn"]
            _git([*command, "--check", "-"], worktree, patch)
            _git([*command, "-"], worktree, patch)
            if sample.read_bytes() != b"after\n":
                raise ServiceLayoutPreflightError("git_unusable")
            worktree.rename(stage / "ready")
    except (PatchInfrastructureError, PatchValidationError) as error:
        raise ServiceLayoutPreflightError("git_unusable") from error
    except OSError as error:
        raise ServiceLayoutPreflightError("work_root_unwritable") from error


def preflight_layout(
    work_root: Path,
    job_id: str,
    *,
    windows_limit: int = 260,
    enforce_windows_limit: bool | None = None,
) -> dict[str, object]:
    """按当前服务布局检查最坏路径，且不创建任务、候选或诊断目录。"""
    job_id = identity(job_id)
    root = Path(work_root).absolute()
    assert_no_links(root)
    if shutil.which("git") is None:
        raise ServiceLayoutPreflightError("git_unavailable")
    job_root = root / "jobs" / job_id
    # 请求/候选物化和任务/诊断执行是两条并行路径，均使用最大合法身份长度。
    proposal_id = "f" * 32
    diagnostic_request_id = "f" * 64
    task_id = "f" * 32
    stage = ".upgrade-stage-" + ("f" * 8)
    paths = {
        "proposal_git_cwd": job_root / "proposals" / proposal_id / "revision" / stage / "source",
        "task_diagnostic_git_cwd": (
            job_root / "tasks" / task_id / "executions" / ("999-" + diagnostic_request_id)
            / "probe" / stage / "source"
        ),
        "task_comparison_git_cwd": (
            job_root / "tasks" / task_id / "executions" / ("999-" + diagnostic_request_id)
            / "comparisons" / proposal_id / stage / "source"
        ),
        "task_candidate_git_cwd": (
            job_root / "tasks" / task_id / "candidates" / proposal_id / stage / "source"
        ),
        "task_seed_input_git_cwd": (
            job_root / "tasks" / task_id / "seed-inputs" / stage / "source"
        ),
        "job_root": job_root,
    }
    for path in paths.values():
        assert_no_links(path)
        _probe_writable(path.parent)
    longest = max(len(str(path)) for path in paths.values())
    enforce_windows_limit = os.name == "nt" if enforce_windows_limit is None else enforce_windows_limit
    if enforce_windows_limit and longest >= windows_limit:
        raise ServiceLayoutPreflightError("windows_path_too_long")
    # 同一真实祖先只需按其最深路径验证一次；已有任务目录不会被探测物化污染。
    probes = {}
    for path in paths.values():
        parent = _nearest_existing(path.parent)
        probes[parent] = max(probes.get(parent, 0), len(str(path)))
    for parent, length in probes.items():
        _probe_git_layout(parent, length)
    return {
        "job_root": str(job_root),
        "max_path_length": longest,
        "windows_limit": windows_limit,
        "checked_paths": {name: str(path) for name, path in paths.items()},
        "git_materialization_verified": True,
    }


@dataclass(frozen=True)
class Settings:
    work_root: Path
    budget: Path
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    cases: dict[str, Path] = field(default_factory=dict)
    profiles: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Settings:
        assert_no_links(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(work_root=Path(data["work_root"]).absolute(), budget=Path(data["budget"]).absolute(),
                   database_url=data["database_url"], token=data["token"],
                   cases={key: Path(value).absolute() for key, value in data["cases"].items()},
                   profiles=data["profiles"])

    def validate(self) -> None:
        if len(self.token) < 32 or not self.database_url.startswith("postgresql://"):
            raise ValueError("invalid_service_credentials")
        assert_no_links(self.work_root)
        assert_no_links(self.budget)
        if not self.budget.is_file() or not self.cases or not self.profiles:
            raise ValueError("service_registry_incomplete")
        for name, manifest in self.cases.items():
            case = load_case(manifest)
            if case.manifest.case_id != name:
                raise ValueError("case_registry_identity_mismatch")
            if "evaluation.json" in case.manifest.file_hashes:
                load_evaluation(case)
            # 服务登记必须证明案例能进入请求构建，不能只验证文件哈希。
            if "evidence/bundle.json" in case.manifest.file_hashes:
                load_version_evidence(case)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def preflight_layout(self, job_id: str) -> dict[str, object]:
        return preflight_layout(self.work_root, job_id)

    def job_root(self, job_id: str) -> Path:
        path = self.work_root / "jobs" / identity(job_id)
        assert_no_links(path)
        return path
