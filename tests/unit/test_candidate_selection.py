import copy
import hashlib

import pytest

from upgrade_workbench.candidate_selection import (
    CandidateSelectionError,
    collect_candidate_coverages,
    comparable_candidate_groups,
    select_edit_base,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


NODES = tuple(f"checks/test_public.py::test_{index}" for index in range(6))


def candidate(label: str) -> dict:
    return {
        "revision": digest("revision-" + label),
        "sha256": digest("patch-" + label),
        "patch_path": f"C:/bounded/{label}/candidate.patch",
        "origin": "agent_candidate",
        "reviewed": True,
    }


def task(*candidates: dict, status: str = "ready") -> dict:
    return {
        "case_fingerprint": digest("case"),
        "status": status,
        "candidate_history": list(candidates),
        "review_history": [
            {
                "revision": item["revision"],
                "sha256": item["sha256"],
                "reviewer": "codex",
                "note": "Bounded candidate review.",
            }
            for item in candidates
        ],
    }


def observation(item: dict, failed: tuple[str, ...], *, nodes=NODES, label="observation") -> dict:
    status = "failed" if failed else "passed"
    stage = {"status": status, "nodeids": list(nodes), "failed_nodeids": list(failed)}
    return {
        "id": digest(label + item["revision"]),
        "kind": "public_checks",
        "revision": item["revision"],
        "result": {
            "scope": "public",
            "status": status,
            "stages": {
                "old_original": {"status": "passed", "nodeids": list(nodes)},
                "new_candidate": stage,
            },
        },
    }


def test_partial_candidate_is_ranked_above_current_zero_of_six():
    partial = candidate("partial")
    zero = candidate("zero")
    observations = [
        observation(partial, NODES[:3], label="partial"),
        observation(zero, NODES, label="zero"),
    ]

    groups = comparable_candidate_groups(task(partial, zero), observations)

    assert len(groups) == 1
    assert [item.passed_count for item in groups[0]] == [3, 0]
    assert groups[0][0].passed_nodeids == NODES[3:]
    assert groups[0][0].failed_nodeids == NODES[:3]
    assert groups[0][0].total_count == 6


def test_same_score_different_pass_sets_remain_distinct():
    left = candidate("left")
    right = candidate("right")
    observations = [
        observation(left, NODES[:3], label="left"),
        observation(right, NODES[3:], label="right"),
    ]

    group = comparable_candidate_groups(task(left, right), observations)[0]

    assert len(group) == 2
    assert group[0].passed_count == group[1].passed_count == 3
    assert {item.passed_nodeids for item in group} == {NODES[:3], NODES[3:]}


@pytest.mark.parametrize("mutation", ["incomplete", "truncated", "changed_suite", "unreviewed"])
def test_incomparable_or_unreviewed_observations_are_not_selectable(mutation):
    item = candidate(mutation)
    row = observation(item, NODES[:3])
    current_task = task(item)
    if mutation == "incomplete":
        row["result"]["status"] = "execution_incomplete"
    elif mutation == "truncated":
        row["result"]["stages"]["new_candidate"]["nodeids_truncated"] = True
    elif mutation == "changed_suite":
        row["result"]["stages"]["new_candidate"]["nodeids"] = list(NODES[:-1])
        row["result"]["stages"]["new_candidate"]["failed_nodeids"] = list(NODES[:2])
    else:
        item["reviewed"] = False

    assert collect_candidate_coverages(current_task, [row]) == ()
    with pytest.raises(CandidateSelectionError, match="lacks reviewed"):
        select_edit_base(current_task, [row], item["revision"])


def test_selecting_edit_base_does_not_change_terminal_task():
    item = candidate("terminal")
    current_task = task(item, status="unresolved")
    before = copy.deepcopy(current_task)
    row = observation(item, NODES[:3])

    selected = select_edit_base(current_task, [row], item["revision"])

    assert selected.revision == item["revision"]
    assert selected.candidate_reference["path"].endswith("revision.json")
    assert current_task == before
    assert current_task["status"] == "unresolved"
