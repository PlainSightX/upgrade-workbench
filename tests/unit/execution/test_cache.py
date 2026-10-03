"""只验证缓存身份与并发发布；真正镜像复用另做现场验证。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_docker import FakeDocker

from upgrade_workbench.execution.cache import RECIPE, EnvironmentCache, content_key
from upgrade_workbench.execution.docker import DockerExecutor


def inputs(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n")
    fake = FakeDocker()
    executors = [DockerExecutor(tmp_path / str(i), cache_root=tmp_path / "cache") for i in range(2)]
    for executor in executors:
        executor._run = fake
    return lock, fake, executors


def test_cross_root_concurrent_only_one_build(tmp_path):
    lock, fake, executors = inputs(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda e: e.prepare_environment(lock), executors))
    assert sum(c[0] == "build" for c in fake.commands) == 1
    assert result[0]["image_id"] == result[1]["image_id"]
    assert sum(bool(r["cache_hit"]) for r in result) == 1
    assert all(list(e.work_root.glob("prepared-images/*.json")) for e in executors)


def test_corrupt_index_and_image_labels_refuse_without_cleanup(tmp_path):
    lock, fake, executors = inputs(tmp_path)
    report = executors[0].prepare_environment(lock)
    fake.commands.clear()
    fake.labels["org.upgrade-workbench.content"] = "wrong"
    with pytest.raises(ValueError, match="image_identity_conflict"):
        executors[1].prepare_environment(lock)
    assert not any(c[:2] == ["image", "rm"] for c in fake.commands)
    path = tmp_path / "cache/index" / (report["cache_key"] + ".json")
    data = json.loads(path.read_text())
    data["recipe"] = "other"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cache_identity_conflict"):
        executors[1].prepare_environment(lock)


def test_interrupted_build_no_ready_index(tmp_path):
    lock, fake, executors = inputs(tmp_path)
    fake.fail_command = "build"
    with pytest.raises(RuntimeError):
        executors[0].prepare_environment(lock)
    assert not list((tmp_path / "cache/index").glob("*.json"))
    fake.fail_command = None
    assert not executors[1].prepare_environment(lock)["cache_hit"]


@pytest.mark.parametrize("field", ["base_image_id", "base_image_digest", "platform", "expected_python", "lock_sha256", "recipe", "local_wheel_sha256"])
def test_key_includes_every_content_input(field):
    identity = {"base_image_id": "id", "base_image_digest": "digest", "platform": "linux/amd64",
                "expected_python": "3.9", "lock_sha256": "lock", "recipe": RECIPE, "local_wheel_sha256": {}}
    assert content_key(identity) != content_key(identity | {field: "changed"})


def test_atomic_index_excludes_usage_paths(tmp_path):
    lock, _, executors = inputs(tmp_path)
    report = executors[0].prepare_environment(lock)
    cached = EnvironmentCache(tmp_path / "cache").read(report["cache_key"])
    assert "build_log" not in cached and "duration_seconds" not in cached


def test_wheel_order_matches_canonical_recipe(tmp_path):
    lock, _, executors = inputs(tmp_path)
    wheels = [tmp_path / f"{name}-1.0-py3-none-any.whl" for name in ("zeta", "alpha")]
    for wheel in wheels:
        wheel.write_bytes(b"owned-wheel-bytes")
    first = executors[0].prepare_environment(lock, local_wheels=wheels)
    second = executors[1].prepare_environment(lock, local_wheels=list(reversed(wheels)))
    assert first["cache_key"] == second["cache_key"] and second["cache_hit"]
    recipe = (Path(first["build_log"]).parent / "context/Dockerfile").read_text()
    assert recipe.index("wheels/alpha-") < recipe.index("wheels/zeta-")
