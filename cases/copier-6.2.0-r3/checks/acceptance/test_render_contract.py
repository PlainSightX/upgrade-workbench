"""独立最终检查：只由评价器读取，不向生成或修复过程暴露。"""

import yaml
from copier import run_copy


def _template(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "copier.yml").write_text(
        "project: orchard\ncount: 4\nsecret_code:\n  type: str\n  default: hidden\n  secret: true\n",
        encoding="utf-8",
    )
    (source / "{{ project }}.txt.jinja").write_text("{{ project }}:{{ count }}\n", encoding="utf-8")
    (source / ".copier-answers.yml.jinja").write_text("{{ _copier_answers|to_nice_yaml }}", encoding="utf-8")
    (source / "unchanged.txt").write_bytes(b"literal\x00bytes\n")
    return source


def test_filename_content_and_answer_record_agree(tmp_path):
    source = _template(tmp_path)
    destination = tmp_path / "result"
    run_copy(str(source), destination, data={"project": "grove"}, defaults=True, quiet=True)
    assert (destination / "grove.txt").read_text(encoding="utf-8") == "grove:4\n"
    assert not (destination / "orchard.txt").exists()
    assert (destination / "unchanged.txt").read_bytes() == b"literal\x00bytes\n"
    answers = yaml.safe_load((destination / ".copier-answers.yml").read_text(encoding="utf-8"))
    assert answers["project"] == "grove"
    assert answers["count"] == 4
    assert "secret_code" not in answers


def test_pretend_makes_no_output_directory(tmp_path):
    source = _template(tmp_path)
    destination = tmp_path / "result"
    run_copy(str(source), destination, defaults=True, quiet=True, pretend=True)
    assert not destination.exists()


def test_skip_if_exists_preserves_user_file(tmp_path):
    source = _template(tmp_path)
    destination = tmp_path / "result"
    destination.mkdir()
    (destination / "orchard.txt").write_text("user edit\n", encoding="utf-8")
    run_copy(str(source), destination, defaults=True, quiet=True, overwrite=True, skip_if_exists=("orchard.txt",))
    assert (destination / "orchard.txt").read_text(encoding="utf-8") == "user edit\n"
    assert (destination / "unchanged.txt").read_bytes() == b"literal\x00bytes\n"


def test_exclusion_leaves_other_rendered_files(tmp_path):
    source = _template(tmp_path)
    destination = tmp_path / "result"
    run_copy(str(source), destination, defaults=True, quiet=True, exclude=("unchanged.txt",))
    assert not (destination / "unchanged.txt").exists()
    assert (destination / "orchard.txt").read_text(encoding="utf-8") == "orchard:4\n"
