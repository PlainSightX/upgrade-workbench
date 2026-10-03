"""公开反馈检查：本地模板读取、答案验证和真实目录输出。"""

from pathlib import Path

import pytest
from copier import Worker, run_copy


def test_local_template_renders_default_answers(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "copier.yml").write_text("project: orchard\ncount: 3\n", encoding="utf-8")
    (source / "README.md.jinja").write_text("{{ project }}:{{ count }}\n", encoding="utf-8")
    destination = tmp_path / "result"
    run_copy(str(source), destination, defaults=True, quiet=True)
    assert (destination / "README.md").read_text(encoding="utf-8") == "orchard:3\n"
    assert not (destination / "copier.yml").exists()


def test_explicit_answers_drive_typed_rendering(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "copier.yml").write_text(
        "count:\n  type: int\n  default: 2\nenabled:\n  type: bool\n  default: false\n",
        encoding="utf-8",
    )
    (source / "result.txt.jinja").write_text("{{ count + 1 }}:{{ enabled }}\n", encoding="utf-8")
    destination = tmp_path / "result"
    run_copy(str(source), destination, data={"count": "7", "enabled": "yes"}, defaults=True, quiet=True)
    assert (destination / "result.txt").read_text(encoding="utf-8") == "8:True\n"


def test_missing_template_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_copy(str(tmp_path / "missing"), tmp_path / "result", defaults=True, quiet=True)


def test_answer_file_must_be_relative(tmp_path):
    with pytest.raises(ValueError):
        Worker(src_path=str(tmp_path), dst_path=tmp_path / "result", answers_file=Path("/outside.yml"))
