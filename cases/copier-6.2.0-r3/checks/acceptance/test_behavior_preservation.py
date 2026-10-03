"""最终行为组合；不将本文件或结果提供给求解器。"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from copier import Worker, run_copy
from copier.user_data import AnswersMap, Question
from jinja2.sandbox import SandboxedEnvironment


def question(default, answers, **kwargs):
    return Question(var_name="value", default=default, answers=answers,
                    jinja_env=SandboxedEnvironment(), **kwargs)


def test_last_answer_does_not_redefine_boolean_type():
    item = question(False, AnswersMap(last={"value": "yes"}))
    assert item.get_type_name() == "bool"
    assert item.get_default() is True
    assert item.filter_answer("no") is False


def test_user_default_does_not_redefine_empty_string_type():
    item = question("", AnswersMap(user_defaults={"value": 0}))
    assert item.get_type_name() == "str"
    assert item.get_default() == "0"
    assert item.filter_answer("007") == "007"


def test_answer_precedence_and_explicit_type_are_preserved():
    answers = AnswersMap(init={"value": "9"}, last={"value": "8"},
                         user_defaults={"value": "7"})
    assert question("6", answers, type="int").get_default() == 9
    assert question(6, AnswersMap(last={"value": "8"}, user_defaults={"value": "7"})).get_default() == 8
    assert question(6, AnswersMap(user_defaults={"value": "7"})).get_default() == 7
    assert answers.init == {"value": "9"}
    assert answers.last == {"value": "8"}
    assert answers.user_defaults == {"value": "7"}


def test_omitted_default_preserves_yaml_conversion():
    item = Question(var_name="value", answers=AnswersMap(), jinja_env=SandboxedEnvironment())
    assert item.get_type_name() == "yaml"
    assert item.filter_answer("{enabled: true}") == {"enabled": True}


def test_to_json_keeps_nested_dataclass_path_and_options(tmp_path):
    @dataclass
    class Item:
        name: str
        path: Path

    source = tmp_path / "template"
    source.mkdir()
    # 只用已有过滤器与原生数据对象，不执行模板任务或自定义扩展。
    worker = Worker(src_path=str(source), dst_path=tmp_path / "output", defaults=True, quiet=True)
    encoded = worker.jinja_env.filters["to_json"](
        {"item": Item("中文", Path("relative.txt"))}, ensure_ascii=False, sort_keys=True)
    assert "中文" in encoded
    assert json.loads(encoded) == {"item": {"name": "中文", "path": "relative.txt"}}


def test_relative_answer_file_string_is_accepted(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "result.txt.jinja").write_text("ok", encoding="utf-8")
    destination = tmp_path / "output"
    worker = run_copy(str(source), destination, answers_file="answers.yml", defaults=True, quiet=True)
    assert worker.answers_relpath == Path("answers.yml")
    assert (destination / "result.txt").read_text(encoding="utf-8") == "ok"


def test_absolute_answer_file_string_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        Worker(answers_file=str(tmp_path / "answers.yml"))
