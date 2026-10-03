"""公开行为示例：类型来源、空默认值以及配置的JSON渲染。"""

import json

from copier import run_copy
from copier.user_data import AnswersMap, Question
from jinja2.sandbox import SandboxedEnvironment


def test_declared_default_type_survives_answer_override():
    answers = AnswersMap(init={"count": "7"})
    question = Question(var_name="count", answers=answers,
                        jinja_env=SandboxedEnvironment(), default=2)
    # 答案优先级只改变取值，不改变由声明默认值确定的类型。
    assert question.get_type_name() == "int"
    assert question.get_default() == 7
    assert type(question.get_default()) is int
    assert answers.init == {"count": "7"}


def test_null_default_keeps_yaml_question_type():
    question = Question(var_name="value", answers=AnswersMap(),
                        jinja_env=SandboxedEnvironment(), default=None)
    assert question.get_type_name() == "yaml"
    assert question.get_default() is None
    assert question.filter_answer("[1, 2]") == [1, 2]


def test_to_json_supports_worker_config(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "conf.json.jinja").write_text("{{ _copier_conf|to_json }}", encoding="utf-8")
    destination = tmp_path / "result"
    run_copy(str(source), destination, defaults=True, quiet=True)
    result = json.loads((destination / "conf.json").read_text(encoding="utf-8"))
    assert result["dst_path"] == str(destination)
    assert result["defaults"] is True
