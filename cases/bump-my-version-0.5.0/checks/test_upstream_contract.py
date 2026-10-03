"""执行上游已有断言；包装只明确参数与隔离目录，不更改原测试。"""

from pathlib import Path

import pytest

from tests import test_config as upstream_config
from tests import test_files as upstream_files
from tests import test_version_part as upstream_version


@pytest.fixture(autouse=True)
def isolated_project_directory(tmp_path, monkeypatch):
    """SCM 探测只看到容器临时目录，不能关联快照或宿主机仓库。"""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def upstream_fixtures():
    return Path(upstream_config.__file__).parent / "fixtures"


def test_ini_configuration_matches_upstream_fixture(upstream_fixtures):
    upstream_config.test_read_ini_file("basic_cfg.cfg", "basic_cfg_expected.json", upstream_fixtures)


def test_toml_configuration_matches_upstream_fixture(upstream_fixtures):
    upstream_config.test_read_toml_file("basic_cfg.toml", "basic_cfg_expected.json", upstream_fixtures)


def test_false_independent_flag_remains_false(tmp_path):
    upstream_config.test_independent_falsy_value_in_config_does_not_bump_independently(tmp_path)


def test_configuration_file_precedence(tmp_path):
    upstream_config.test_multiple_config_files(tmp_path)


def test_configuration_interpolation(tmp_path, upstream_fixtures):
    upstream_config.test_correct_interpolation_for_setup_cfg_files(tmp_path, upstream_fixtures)


@pytest.mark.parametrize("config_filename", [".bumpversion.cfg", "setup.cfg", "pyproject.toml"])
def test_update_only_current_version_in_config(tmp_path, config_filename, upstream_fixtures):
    expected = (
        upstream_config.TOML_EXPECTED_DIFF
        if config_filename.endswith(".toml")
        else upstream_config.CFG_EXPECTED_DIFF
    )
    upstream_config.test_update_config_file(tmp_path, config_filename, expected, upstream_fixtures)


def test_independent_build_number_survives_major_reset():
    upstream_version.test_build_number_configuration()


@pytest.mark.parametrize("part, expected", [("patch", "0.9.1"), ("minor", "0.10"), ("major", "1")])
def test_optional_version_parts_serialize_as_upstream(part, expected):
    upstream_version.test_serialize_three_part(part, expected)


@pytest.mark.parametrize("initial, part, expected", [("1.5.dev", "release", "1.5"), ("1.5", "minor", "1.6.dev")])
def test_non_numeric_release_sequence(initial, part, expected):
    upstream_version.test_bump_non_numeric_parts(initial, part, expected)


def test_nonzero_first_value_is_used_on_reset():
    upstream_version.test_part_first_value("0.9.4", "major", "1.1.0")


def test_unknown_version_part_is_rejected():
    upstream_version.test_bump_version_missing_part()


def test_invalid_parse_regex_is_rejected(tmp_path):
    upstream_version.test_version_part_invalid_regex_exit(tmp_path)


def test_same_file_can_use_two_replacement_formats(tmp_path):
    upstream_files.test_single_file_processed_twice(tmp_path)


def test_multi_file_release_and_following_patch(tmp_path):
    upstream_files.test_multi_file_configuration(tmp_path)


def test_missing_search_does_not_modify_file(tmp_path):
    upstream_files.test_non_matching_search_does_not_modify_file(tmp_path)


def test_utf8_content_survives_version_replacement(tmp_path):
    upstream_files.test_simple_replacement_in_utf8_file(tmp_path)
