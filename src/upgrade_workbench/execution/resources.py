"""项目资源盘点与精确回收；不按标签或名字前缀直接删除。"""

import hashlib
import json
from pathlib import Path

from ..cases.manifest import assert_no_links
from ..runtime import REPOSITORY, load_layout
from .cache import EnvironmentCache
from .docker import DockerExecutor, _require_success


def inventory(executor: DockerExecutor, roots: tuple[Path, ...], cache: EnvironmentCache) -> dict:
    references, failures = {}, []
    for root in roots:
        assert_no_links(root)
        if not root.is_dir():
            failures.append(str(root))
            continue
        for path in root.rglob("prepared-images/*.json"):
            try:
                assert_no_links(path)
                record = json.loads(path.read_text(encoding="utf-8"))
                image_id = record["image_id"]
                if path.stem != image_id.removeprefix("sha256:"):
                    raise ValueError("receipt_identity_conflict")
                references.setdefault(image_id, []).append(str(path))
            except (ValueError, OSError, KeyError):
                failures.append(str(path))
    result = executor._run(["image", "ls", "--no-trunc", "--quiet", "--filter",
                            "label=org.upgrade-workbench.kind=environment"], 30)
    _require_success(result, "resource_inventory_failed")
    containers = executor._run(["ps", "-a", "--quiet"], 30)
    _require_success(containers, "container_inventory_failed")
    used, owned_containers = set(), []
    for container in containers.stdout.split():
        # 只投影归属/状态字段，不读取或输出容器环境中的服务凭据。
        format_ = '{"image":{{json .Image}},"name":{{json .Name}},"labels":{{json .Config.Labels}},"running":{{json .State.Running}}}'
        inspected = executor._run(["inspect", "--format", format_, container], 30)
        _require_success(inspected, "container_inventory_changed")
        value = json.loads(inspected.stdout)
        used.add(value["image"])
        labels = value.get("labels") or {}
        kind = labels.get("org.upgrade-workbench.kind")
        if labels.get("project") == "upgrade-workbench" or kind in {"verification", "materialization", "ephemeral-postgres"}:
            owned_containers.append({"container_id": container, "name": value["name"].lstrip("/"),
                "image_id": value["image"], "kind": kind,
                "operation": labels.get("org.upgrade-workbench.operation"),
                "running": value["running"], "state": "protected_not_automatically_deleted"})
    rows = []
    for image_id in sorted(set(result.stdout.split())):
        image = executor._inspect_image(image_id, 30)
        labels = image.get("Config", {}).get("Labels") or {}
        key = labels.get("org.upgrade-workbench.content")
        try:
            record = cache.read(key) if isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key) else None
        except (ValueError, OSError, KeyError):
            record = None
        known = bool(record and record.get("image_id") == image_id and labels.get("project") == "upgrade-workbench")
        state = ("in_use" if image_id in used else "retained_evidence" if image_id in references else
                 "unknown_protected" if failures or not known else "unreferenced_cache")
        rows.append({"image_id": image_id, "state": state, "cache_key": key,
                     "retained_receipts": references.get(image_id, []), "tags": image.get("RepoTags", [])})
    snapshots = executor._run(["image", "ls", "--no-trunc", "--quiet", "--filter",
                               "label=org.upgrade-workbench.kind=snapshot"], 30)
    _require_success(snapshots, "snapshot_inventory_failed")
    snapshot_rows = []
    for image_id in sorted(set(snapshots.stdout.split())):
        image = executor._inspect_image(image_id, 30)
        labels = image.get("Config", {}).get("Labels") or {}
        snapshot_rows.append({"image_id": image_id, "project": labels.get("project"),
            "operation": labels.get("org.upgrade-workbench.operation"),
            "state": "protected_reconcile_exact_execution_receipt"})
    return {"schema_version": 1, "reference_roots": [str(p) for p in roots],
            "scan_complete": not failures, "scan_failures": failures, "images": rows,
            "containers": owned_containers, "snapshot_images": snapshot_rows,
            "deletable_ids": [r["image_id"] for r in rows if r["state"] == "unreferenced_cache"],
            "volumes_and_build_cache": "protected_not_managed"}


def default_inventory():
    layout = load_layout()
    executor = DockerExecutor(REPOSITORY / ".local/resource-status")
    cache = EnvironmentCache(layout.environments)
    roots = (REPOSITORY / ".local", layout.root / "active", layout.tests)
    return executor, roots, cache


def apply_preview(path: Path, expected_sha256: str) -> dict:
    assert_no_links(path)
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError("cleanup_preview_changed")
    preview = json.loads(body)
    executor, roots, cache = default_inventory()
    removed = []
    for image_id in preview["deletable_ids"]:
        row = next(r for r in preview["images"] if r["image_id"] == image_id)
        with cache.lock(row["cache_key"]).acquire(timeout=0):
            current = inventory(executor, roots, cache)
            if not current["scan_complete"] or image_id not in current["deletable_ids"]:
                raise ValueError("cleanup_reference_or_usage_changed")
            actual = next(r for r in current["images"] if r["image_id"] == image_id)
            if actual != row:
                raise ValueError("cleanup_object_changed")
            # 不使用 force；Docker 也必须确认对象不被容器依赖。
            result = executor._run(["image", "rm", image_id], 30)
            _require_success(result, "exact_image_cleanup_failed")
            (cache.root / "index" / (row["cache_key"] + ".json")).unlink()
            removed.append(image_id)
    return {"removed": removed, "volumes_deleted": 0, "global_prune": False}
