import math
from json import dumps

import pytest

from upgrade_workbench.spectrum import (
    SPECTRUM_PREFIX,
    SpectrumRecord,
    decide_gate,
    ochiai,
    parse_spectrum_log,
    rank_spectrum,
)


def spectrum(nodeid, outcome, *locations):
    return SpectrumRecord(nodeid=nodeid, outcome=outcome, locations=frozenset(locations))


def test_ochiai_uses_failing_and_passing_counts():
    assert ochiai(failed_covered=2, passed_covered=0, total_failed=2) == 1.0
    assert ochiai(failed_covered=1, passed_covered=3, total_failed=2) == pytest.approx(
        1 / math.sqrt(8)
    )


@pytest.mark.parametrize(
    "values",
    [
        {"failed_covered": -1, "passed_covered": 0, "total_failed": 1},
        {"failed_covered": 2, "passed_covered": 0, "total_failed": 1},
        {"failed_covered": True, "passed_covered": 0, "total_failed": 1},
    ],
)
def test_ochiai_rejects_invalid_counts(values):
    with pytest.raises(ValueError):
        ochiai(**values)


def test_ranking_counts_each_test_once_and_is_deterministic():
    records = [
        spectrum("f1", "failed", ("pkg/a.py", 8), ("pkg/a.py", 9)),
        spectrum("f2", "failed", ("pkg/a.py", 8)),
        spectrum("p1", "passed", ("pkg/a.py", 9), ("pkg/b.py", 2)),
    ]
    ranked = rank_spectrum(records, allowed_files={"pkg/a.py", "pkg/b.py"})
    assert [(item.path, item.line) for item in ranked] == [
        ("pkg/a.py", 8),
        ("pkg/a.py", 9),
        ("pkg/b.py", 2),
    ]
    assert ranked[0].failed_covered == 2
    assert ranked[0].passed_covered == 0


def test_zero_failures_produce_zero_scores():
    ranked = rank_spectrum(
        [spectrum("p1", "passed", ("pkg/a.py", 3))], allowed_files={"pkg/a.py"}
    )
    assert ranked[0].score == 0.0
    assert ranked[0].total_failed == 0


def test_errors_skips_and_unlisted_files_do_not_enter_score():
    ranked = rank_spectrum(
        [
            spectrum("f1", "failed", ("pkg/a.py", 4), ("private.py", 1)),
            spectrum("e1", "error", ("pkg/a.py", 5)),
            spectrum("s1", "skipped", ("pkg/a.py", 6)),
        ],
        allowed_files={"pkg/a.py"},
    )
    assert [(item.path, item.line) for item in ranked] == [("pkg/a.py", 4)]


def test_spectrum_parser_accepts_pytest_progress_before_marker(tmp_path):
    payload = {"schema_version": 1, "tests": []}
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        "EFEEEF" + SPECTRUM_PREFIX + dumps(payload) + "\n", encoding="utf-8"
    )

    assert parse_spectrum_log(stdout) == payload


def test_gate_does_not_claim_vacuous_localization_improvement():
    result = {
        "execution": {"status": "failed", "tests": {"errors": 0}},
        "spectrum": {
            "truncated": False,
            "outcomes": ["passed", "failed"],
            "top_tie_count": 1,
        },
        "labels": [{"sbfl_rank": 1, "current_workset_rank": None}],
        "current_localization": {"requests": [], "traceback_locations": []},
    }

    decision = decide_gate([result, result, result])

    assert decision["current_workset_comparison_available"] is False
    assert decision["improves_current_workset"] is False
    assert decision["traceback_comparison_available"] is False
    assert decision["traceback_locations_retained_top_20"] is False
    assert decision["admitted"] is False
