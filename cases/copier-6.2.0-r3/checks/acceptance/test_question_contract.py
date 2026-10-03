"""问题默认值和拒绝边界；这些断言仅供最终验收。"""

import pytest
from copier import run_copy
from copier.user_data import AnswersMap, Question
from jinja2.sandbox import SandboxedEnvironment


def test_defaults_cast_text_before_rendering(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "copier.yml").write_text(
        'count:\n  type: int\n  default: "7"\nenabled:\n  type: bool\n  default: "yes"\n',
        encoding="utf-8",
    )
    (source / "result.txt.jinja").write_text(
        "{{ count + 1 }}:{{ enabled }}:{{ count is number }}:{{ enabled is boolean }}\n",
        encoding="utf-8",
    )
    destination = tmp_path / "result"
    run_copy(str(source), destination, defaults=True, quiet=True)
    assert (destination / "result.txt").read_text(encoding="utf-8") == "8:True:True:True\n"


def test_question_type_is_inferred_from_default():
    integer = Question(
        var_name="count", answers=AnswersMap(), jinja_env=SandboxedEnvironment(), default=2,
    )
    boolean = Question(
        var_name="enabled", answers=AnswersMap(), jinja_env=SandboxedEnvironment(), default=False,
    )
    assert integer.get_type_name() == "int"
    assert integer.get_default() == 2
    assert type(integer.get_default()) is int
    assert integer.filter_answer("8") == 8
    assert boolean.get_type_name() == "bool"
    assert boolean.get_default() is False
    assert boolean.filter_answer("yes") is True


def test_question_rejects_invalid_numeric_answer():
    question = Question(
        var_name="count", answers=AnswersMap(), jinja_env=SandboxedEnvironment(),
        type="int", default=2,
    )
    # validate_answer 拒绝输入与 filter_answer 抛错是两个已有接口合同。
    assert question.validate_answer("7") is True
    assert question.validate_answer("not-a-number") is False
    with pytest.raises(ValueError):
        question.filter_answer("not-a-number")
