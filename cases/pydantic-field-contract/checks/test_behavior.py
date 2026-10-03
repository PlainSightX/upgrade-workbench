"""冻结的行为要求；候选补丁不能修改此文件。"""

import pytest
from pydantic import ValidationError

from profile_model import Profile


def test_omitted_nickname_keeps_default() -> None:
    value = Profile(required_note=None, priority=7)
    assert value.nickname is None


def test_required_nullable_field_stays_required() -> None:
    with pytest.raises(ValidationError):
        Profile(nickname="A", priority=7)


def test_explicit_null_is_accepted() -> None:
    value = Profile(nickname=None, required_note=None, priority=7)
    assert value.required_note is None


def test_numeric_string_keeps_integer_behavior() -> None:
    value = Profile(nickname="A", required_note="n", priority="10")
    assert type(value.priority) is int
    assert value.priority == 10


def test_text_priority_is_not_forced_to_number() -> None:
    value = Profile(nickname="A", required_note="n", priority="urgent")
    assert value.priority == "urgent"
