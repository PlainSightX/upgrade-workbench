"""问题输入过滤器单独覆盖；不以显式 data 路径冒充问题转换。"""

from copier.user_data import AnswersMap, Question
from jinja2.sandbox import SandboxedEnvironment


def test_question_filter_casts_declared_integer_and_boolean():
    integer = Question(
        var_name="count", answers=AnswersMap(), jinja_env=SandboxedEnvironment(),
        type="int", default=2,
    )
    boolean = Question(
        var_name="enabled", answers=AnswersMap(), jinja_env=SandboxedEnvironment(),
        type="bool", default=False,
    )
    answer = integer.filter_answer("7")
    assert type(answer) is int
    assert answer == 7
    assert integer.filter_answer("2") == 2
    assert boolean.filter_answer("yes") is True
    assert boolean.filter_answer("no") is False
