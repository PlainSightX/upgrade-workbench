"""回收不能误伤历史引用、未知资源或其他项目；CLI预览不执行删除。"""

import json
from types import SimpleNamespace

import pytest

from upgrade_workbench import cli
from upgrade_workbench.execution import resources
from upgrade_workbench.execution.cache import RECIPE, EnvironmentCache, atomic_json, content_key


def test_references_and_unknown_are_protected(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    cache = EnvironmentCache(tmp_path / "cache")
    identity = {"base_image_id": "base", "base_image_digest": "digest", "platform": "linux/amd64",
                "expected_python": "3.9", "lock_sha256": "lock", "local_wheel_sha256": {}, "recipe": RECIPE}
    key = content_key(identity)
    known, old, alien = ["sha256:" + letter * 64 for letter in "abc"]
    cache.publish(key, identity | {"cache_key": key, "image_id": known, "image_tag": "upgrade-workbench-env:" + key})
    atomic_json(root / "prepared-images" / ("b" * 64 + ".json"), {"image_id": old})

    class Executor:
        def _run(self, args, timeout):
            found = args[-1] == "label=org.upgrade-workbench.kind=environment"
            return SimpleNamespace(returncode=0, stdout="\n".join([known, old, alien]) if found else "", stderr="")

        def _inspect_image(self, image_id, timeout):
            return {"Config": {"Labels": {"project": "upgrade-workbench" if image_id != alien else "other",
                                          "org.upgrade-workbench.content": key}}, "RepoTags": []}

    report = resources.inventory(Executor(), (root,), cache)
    assert report["deletable_ids"] == [known]
    assert [r["state"] for r in report["images"]] == ["unreferenced_cache", "retained_evidence", "unknown_protected"]
    # 任何扫描缺口将所有无引用对象降为未知保护。
    assert resources.inventory(Executor(), (root, tmp_path / "missing"), cache)["deletable_ids"] == []


def test_resources_cli_has_exit_code_and_one_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(resources, "default_inventory", lambda: (None, (), None))
    monkeypatch.setattr(resources, "inventory", lambda *args: {"images": [], "deletable_ids": []})
    output = tmp_path / "preview.json"
    assert cli.main(["resources", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_text())


def test_status_projects_safe_fields_without_exposing_other_services(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    cache = EnvironmentCache(tmp_path / "cache")

    class Executor:
        def _run(self, args, timeout):
            if args[0] == "ps":
                value = "ours\nother"
            elif args[0] == "inspect":
                assert ".Config.Env" not in args[2]
                value = json.dumps({"image": "image-" + args[-1], "name": "/" + args[-1],
                    "running": True, "labels": {"project": "upgrade-workbench" if args[-1] == "ours" else "wind-workbench"}})
            else:
                value = ""
            return SimpleNamespace(returncode=0, stdout=value, stderr="")

    report = resources.inventory(Executor(), (root,), cache)
    assert [r["name"] for r in report["containers"]] == ["ours"]
    assert report["containers"][0]["state"] == "protected_not_automatically_deleted"
    assert report["deletable_ids"] == []


@pytest.mark.parametrize("conflict", ["preview_hash", "retained_reference", "changed_identity"])
def test_cleanup_rechecks_exact_preview_before_any_delete(tmp_path, monkeypatch, conflict):
    import hashlib

    key = "a" * 64
    row = {"image_id": "sha256:" + "b" * 64, "cache_key": key, "tags": ["owned"], "state": "unreferenced_cache"}
    preview = {"images": [row], "deletable_ids": [row["image_id"]], "scan_complete": True}
    path = tmp_path / "preview.json"
    atomic_json(path, preview)
    current = dict(preview)
    if conflict == "retained_reference":
        current["deletable_ids"] = []
    elif conflict == "changed_identity":
        current["images"] = [row | {"tags": ["changed"]}]
    cache = EnvironmentCache(tmp_path / "cache")
    executor = SimpleNamespace(_run=lambda *args: pytest.fail("unsafe delete was attempted"))
    monkeypatch.setattr(resources, "default_inventory", lambda: (executor, (), cache))
    monkeypatch.setattr(resources, "inventory", lambda *args: current)
    expected = "wrong" if conflict == "preview_hash" else hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="cleanup_"):
        resources.apply_preview(path, expected)
