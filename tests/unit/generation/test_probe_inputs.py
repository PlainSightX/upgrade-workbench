"""验证真实输入生成和有限反例选择，不把已知错误变体当未见任务。"""

import builtins

import pytest

from upgrade_workbench.generation.probe_inputs import (
    smallest_recorded_counterexample,
    text_inputs,
)


def test_hypothesis_inputs_are_replayable_and_stay_in_declared_domain():
    pytest.importorskip("hypothesis")
    arguments = {"alphabet": "abcXYZ09:_ -'", "max_length": 12, "max_examples": 24}
    first = text_inputs(**arguments)
    assert first == text_inputs(**arguments)
    assert 1 <= len(first) <= 24 and len(first) == len(set(first))
    assert all(len(value) <= 12 and set(value) <= set(arguments["alphabet"]) for value in first)


def test_missing_optional_dependency_has_an_actionable_error(monkeypatch):
    original = builtins.__import__

    def without_hypothesis(name, *args, **kwargs):
        if name == "hypothesis":
            raise ImportError("Optional dependency is absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_hypothesis)
    with pytest.raises(RuntimeError, match="locked probe-generation extra"):
        text_inputs(alphabet="abc")


@pytest.mark.parametrize("arguments", [
    {"alphabet": ""}, {"alphabet": "a\x00b"}, {"alphabet": None},
    {"alphabet": "a", "max_length": True}, {"alphabet": "a", "max_length": 129},
    {"alphabet": "a", "max_examples": 0}, {"alphabet": "a", "max_examples": 129},
])
def test_invalid_domains_are_rejected_before_generating(arguments):
    with pytest.raises(ValueError, match="bounded"):
        text_inputs(**arguments)


def test_counterexample_reduction_uses_only_completed_recorded_differences():
    incomplete = {"input": "", "status": "incomplete"}
    same = {"input": "a", "status": "same"}
    short = {"input": ":a", "status": "different"}
    long = {"input": "value-:parameter", "status": "different"}
    assert smallest_recorded_counterexample([long, incomplete, same, short]) is short
    assert smallest_recorded_counterexample([incomplete, same]) is None
