"""本机运行布局只管理公共路径，不拥有服务身份、任务状态或验收结论。"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[2]
CONFIG = REPOSITORY / ".local/runtime-layout.json"


def assert_no_links(path: Path) -> None:
    # manifest 本身需要 execution.environment，延迟载入以避免包入口循环。
    from .cases.manifest import assert_no_links as check
    check(path)


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path

    @property
    def tests(self) -> Path:
        return self.root / "t"

    @property
    def environments(self) -> Path:
        return self.root / "env"

    @property
    def default_service_root(self) -> Path:
        # 仅用于新实例默认值；现有 Settings.work_root 永远优先。
        return self.root / "active"


def load_layout(config: Path = CONFIG) -> RuntimeLayout:
    assert_no_links(config.absolute())
    data = json.loads(config.read_text(encoding="utf-8"))
    if set(data) != {"schema_version", "runtime_root"} or data["schema_version"] != 1:
        raise ValueError("invalid_runtime_layout")
    root = Path(data["runtime_root"])
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError("runtime_root_must_be_absolute_non_drive_root")
    assert_no_links(root)
    return RuntimeLayout(root)


def configured_cache(work_root: Path) -> Path | None:
    if not CONFIG.is_file():
        return None
    layout = load_layout()
    # 自有项目与登记短根才自动接入；pytest 等其他临时根不污染本机缓存。
    if work_root.is_relative_to(REPOSITORY / ".local") or work_root.is_relative_to(layout.root):
        return layout.environments
    return None


@contextmanager
def short_test_root(layout: RuntimeLayout):
    assert_no_links(layout.tests)
    layout.tests.mkdir(parents=True, exist_ok=True)
    owned = layout.tests / uuid4().hex[:8]
    owned.mkdir()
    try:
        yield owned
    finally:
        assert_no_links(owned)
        if owned.resolve().parent != layout.tests.resolve():
            raise RuntimeError("refusing_to_remove_unowned_test_root")
        shutil.rmtree(owned)


def remove_empty_legacy_roots(layout: RuntimeLayout) -> list[str]:
    removed = []
    for name in ("uwit", "uw-service-it"):
        path = Path(layout.root.anchor) / name
        assert_no_links(path)
        if not path.exists():
            continue
        if path.resolve() != path.absolute() or not path.is_dir():
            raise ValueError("legacy_root_identity_conflict")
        # rmdir 为非递归，目录非空或竞争写入均失败，不扩大删除范围。
        path.rmdir()
        removed.append(str(path))
    return removed
