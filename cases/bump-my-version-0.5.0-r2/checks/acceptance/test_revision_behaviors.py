"""回顾性补足原应用行为；只在已审阅快照的隔离容器中执行。"""

from copy import deepcopy
from pathlib import Path

import pytest
from bumpversion.bump import do_bump
from bumpversion.config import DEFAULTS, Config, FileConfig, VersionPartConfig, get_configuration
from bumpversion.exceptions import VersionNotFoundError
from bumpversion.files import resolve_file_config
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """SCM 只探测本次临时目录，所有发布目标也限定在这里。"""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configured_defaults():
    # 正常应用入口完成模型类型准备；不依据依赖版本切换测试预期。
    return get_configuration(current_version="1.2.3")


@pytest.mark.parametrize("state", ["omitted", "null", "valid"])
def test_file_optional_values(state):
    values = {
        "filename": "VERSION", "glob": "releases/*.txt", "parse": r"(?P<major>\d+)",
        "serialize": ["{major}"], "search": "v{current_version}",
        "replace": "v{new_version}",
    }
    supplied = {} if state == "omitted" else (
        dict.fromkeys(values) if state == "null" else values
    )
    result = FileConfig(**supplied)
    expected = values if state == "valid" else dict.fromkeys(values)
    assert {name: getattr(result, name) for name in values} == expected


@pytest.mark.parametrize("state", ["omitted", "null", "valid"])
def test_version_part_optional_values(state):
    values = {"values": ["dev", "stable"], "optional_value": "stable", "first_value": "dev"}
    supplied = {} if state == "omitted" else (
        dict.fromkeys(values) if state == "null" else values
    )
    result = VersionPartConfig(**supplied)
    expected = values if state == "valid" else dict.fromkeys(values)
    assert {name: getattr(result, name) for name in values} == expected
    assert result.independent is False


@pytest.mark.parametrize("state", ["omitted", "null", "valid"])
def test_configuration_optional_values(state, configured_defaults):
    values = {
        "current_version": "1.2.3", "tag_message": "Release {new_version}",
        "commit_args": "--no-verify", "scm_info": configured_defaults.scm_info,
    }
    supplied = deepcopy(DEFAULTS)
    for name in values:
        supplied.pop(name)
    if state != "omitted":
        supplied.update(dict.fromkeys(values) if state == "null" else values)
    result = Config(**supplied)
    expected = values if state == "valid" else dict.fromkeys(values)
    assert {name: getattr(result, name) for name in values} == expected


@pytest.mark.parametrize("state", ["omitted", "null", "empty"])
def test_serialize_rejects_missing_null_and_empty(state, configured_defaults):
    supplied = deepcopy(DEFAULTS)
    if state == "omitted":
        supplied.pop("serialize")
    else:
        supplied["serialize"] = None if state == "null" else []
    with pytest.raises(ValidationError) as raised:
        Config(**supplied)
    assert any(error["loc"] == ("serialize",) for error in raised.value.errors())


@pytest.mark.parametrize("formats", [["{major}"], ["{major}.{minor}", "{major}"]],
                         ids=["single", "multiple"])
def test_serialize_preserves_nonempty_format_order(formats, configured_defaults):
    supplied = deepcopy(DEFAULTS)
    supplied["serialize"] = formats
    result = Config(**supplied)
    assert result.serialize == formats
    assert result.parse == DEFAULTS["parse"]
    assert result.search == DEFAULTS["search"]
    assert result.replace == DEFAULTS["replace"]


def _glob_project(root: Path):
    releases = root / "releases"
    (releases / "nested").mkdir(parents=True)
    initial = {
        "releases/first.txt": b"app=1.2.3\nother=1.2.3\n",
        "releases/nested/second.txt": b"title=Release\napp=1.2.3\n",
        "releases/keep.md": b"app=1.2.3\nkeep\n",
    }
    for name, contents in initial.items():
        (root / name).write_bytes(contents)
    config_path = root / "pyproject.toml"
    config_path.write_text(
        '[tool.bumpversion]\ncurrent_version = "1.2.3"\ncommit = false\ntag = false\n'
        'serialize = ["{major}.{minor}.{patch}"]\n'
        '\n[[tool.bumpversion.files]]\nglob = "releases/**/*.txt"\n'
        'search = "app={current_version}"\nreplace = "app={new_version}"\n',
        encoding="utf-8",
    )
    return config_path, initial


def test_glob_resolution_preserves_caller_configuration(tmp_path):
    config_path, initial = _glob_project(tmp_path)
    config = get_configuration(config_path)
    rule = config.files[0]
    names = ("filename", "glob", "parse", "serialize", "search", "replace")
    before = {name: deepcopy(getattr(rule, name)) for name in names}
    resolved = resolve_file_config(config.files, config.version_config)
    assert {Path(item.path).as_posix() for item in resolved} == {
        "releases/first.txt", "releases/nested/second.txt",
    }
    assert len(resolved) == 2
    assert all(item.search == "app={current_version}" for item in resolved)
    assert all(item.replace == "app={new_version}" for item in resolved)
    assert {name: getattr(rule, name) for name in names} == before
    assert {name: (tmp_path / name).read_bytes() for name in initial} == initial


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_glob_release_updates_only_matching_targets(tmp_path, dry_run):
    config_path, initial = _glob_project(tmp_path)
    before_config = config_path.read_bytes()
    config = get_configuration(config_path)
    do_bump("minor", None, config, config_file=config_path, dry_run=dry_run)
    for name, contents in initial.items():
        expected = contents
        if not dry_run and name.endswith(".txt"):
            expected = contents.replace(b"app=1.2.3", b"app=1.3.0")
        assert (tmp_path / name).read_bytes() == expected
    expected_config = before_config if dry_run else before_config.replace(
        b'current_version = "1.2.3"', b'current_version = "1.3.0"'
    )
    assert config_path.read_bytes() == expected_config


def test_glob_without_matches_preserves_rule_and_files(tmp_path):
    config_path, initial = _glob_project(tmp_path)
    config = get_configuration(config_path)
    rule = FileConfig(glob="missing/**/*.txt", filename="unchanged-fallback.txt")
    assert resolve_file_config([rule], config.version_config) == []
    assert rule.filename == "unchanged-fallback.txt"
    assert rule.glob == "missing/**/*.txt"
    assert {name: (tmp_path / name).read_bytes() for name in initial} == initial


def test_glob_preflight_rejects_before_any_write(tmp_path):
    config_path, initial = _glob_project(tmp_path)
    # 保证失败文件连原始版本字符串也没有，避免触发旧实现允许的回退匹配。
    initial["releases/nested/second.txt"] = b"app=0.9.0\nother=0.8.0\n"
    (tmp_path / "releases/nested/second.txt").write_bytes(initial["releases/nested/second.txt"])
    before_config = config_path.read_bytes()
    config = get_configuration(config_path)
    with pytest.raises(VersionNotFoundError):
        do_bump("minor", None, config, config_file=config_path)
    assert {name: (tmp_path / name).read_bytes() for name in initial} == initial
    assert config_path.read_bytes() == before_config
