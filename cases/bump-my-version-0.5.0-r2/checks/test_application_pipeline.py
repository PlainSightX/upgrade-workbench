"""项目补充的流程合同：真实文件更新、dry-run 与写入前检查。"""

from pathlib import Path

import pytest
from bumpversion.bump import do_bump
from bumpversion.config import get_configuration
from bumpversion.exceptions import VersionNotFoundError


@pytest.fixture
def release_project(tmp_path, monkeypatch):
    """所有可写内容位于容器 tmp_path，禁用提交和打标签。"""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        '[tool.bumpversion]\n'
        'current_version = "1.2.3"\n'
        'commit = false\n'
        'tag = false\n'
        '\n[[tool.bumpversion.files]]\n'
        'filename = "VERSION"\n'
        '\n[[tool.bumpversion.files]]\n'
        'filename = "requirements.txt"\n'
        'search = "MyProject=={current_version}"\n'
        'replace = "MyProject=={new_version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "OtherPackage==1.2.3\nMyProject==1.2.3\n", encoding="utf-8"
    )
    return config_path


def _project_bytes(config_path: Path) -> dict[str, bytes]:
    return {
        name: (config_path.parent / name).read_bytes()
        for name in ("pyproject.toml", "VERSION", "requirements.txt")
    }


def test_release_updates_both_files_and_config_without_touching_unrelated_package(release_project):
    before = _project_bytes(release_project)
    config = get_configuration(release_project)
    do_bump("minor", None, config, config_file=release_project)
    after = _project_bytes(release_project)
    assert after["VERSION"] == b"1.3.0\n"
    assert after["requirements.txt"] == b"OtherPackage==1.2.3\nMyProject==1.3.0\n"
    assert after["pyproject.toml"] == before["pyproject.toml"].replace(
        b'current_version = "1.2.3"', b'current_version = "1.3.0"'
    )


def test_dry_run_preserves_all_project_bytes(release_project):
    before = _project_bytes(release_project)
    config = get_configuration(release_project)
    do_bump("minor", None, config, config_file=release_project, dry_run=True)
    assert _project_bytes(release_project) == before


def test_failed_preflight_does_not_partially_update_earlier_file_or_config(release_project):
    (release_project.parent / "requirements.txt").write_text(
        "OtherPackage==0.8.0\nMyProject==0.9.0\n", encoding="utf-8"
    )
    before = _project_bytes(release_project)
    config = get_configuration(release_project)
    with pytest.raises(VersionNotFoundError):
        do_bump("minor", None, config, config_file=release_project)
    assert _project_bytes(release_project) == before
