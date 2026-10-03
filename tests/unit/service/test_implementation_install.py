"""安装包身份不能误用当前目录的其他项目依赖锁。"""

import hashlib
from pathlib import Path

import pytest

from upgrade_workbench import tasks


def test_installed_identity_uses_bundled_lock_not_cwd(tmp_path, monkeypatch):
    package = tmp_path / "site-packages/upgrade_workbench"
    package.mkdir(parents=True)
    module = package / "tasks.py"
    module.write_text("# installed source\n", encoding="utf-8")
    resources = package / "_resources"
    resources.mkdir()
    (resources / "uv.lock").write_bytes(b"bundled lock\n")
    (tmp_path / "uv.lock").write_bytes(b"unrelated project\n")
    monkeypatch.setattr(tasks, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    result = tasks.implementation_identity()
    assert result["uv_lock_sha256"] == hashlib.sha256(b"bundled lock\n").hexdigest()


def test_installed_identity_rejects_missing_bundle_even_if_cwd_has_lock(tmp_path, monkeypatch):
    package = tmp_path / "site-packages/upgrade_workbench"
    package.mkdir(parents=True)
    module = package / "tasks.py"
    module.touch()
    (tmp_path / "uv.lock").write_bytes(b"unrelated project\n")
    monkeypatch.setattr(tasks, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        tasks.implementation_identity()


def test_source_identity_still_uses_repository_lock():
    package = Path(tasks.__file__).resolve().parent
    if package.parent.name == "src":
        assert tasks.implementation_identity()["uv_lock_sha256"] == hashlib.sha256(
            (package.parents[1] / "uv.lock").read_bytes()
        ).hexdigest()
