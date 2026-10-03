"""内容寻址依赖环境；调用者持有按键锁，索引不是镜像真实性的替代品。"""

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from ..runtime import assert_no_links

RECIPE = "hash-locked-pip-wheels-v3-sorted"
IDENTITY_FIELDS = ("base_image_id", "base_image_digest", "platform", "expected_python",
                   "lock_sha256", "local_wheel_sha256", "recipe")


def content_key(identity: dict) -> str:
    body = {key: identity[key] for key in IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    assert_no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class EnvironmentCache:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        assert_no_links(self.root)
        (self.root / "index").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(exist_ok=True)

    def lock(self, key: str):
        self.validate_key(key)
        path = self.root / "locks" / (key + ".lock")
        assert_no_links(path)
        return FileLock(path)

    def read(self, key: str) -> dict | None:
        self.validate_key(key)
        path = self.root / "index" / (key + ".json")
        assert_no_links(path)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("cache_key") != key or content_key(value) != key:
            raise ValueError("environment_cache_identity_conflict")
        return value

    def publish(self, key: str, report: dict) -> None:
        self.validate_key(key)
        if content_key(report) != key:
            raise ValueError("environment_cache_identity_conflict")
        # 本地 operation、日志路径和构建耗时属于收据，不属于共享内容。
        fields = (*IDENTITY_FIELDS, "image_id", "image_tag", "cache_key")
        atomic_json(self.root / "index" / (key + ".json"), {k: report[k] for k in fields})

    @staticmethod
    def validate_key(key: str) -> None:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid_environment_cache_key")
