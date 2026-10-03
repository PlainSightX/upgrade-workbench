"""失败身份是定位线索，不等于根因相同，也不改变候选接受规则。"""

import json

from upgrade_workbench.diagnostic_state import failed_public_nodes, recurring_public_failures

NODE = "feedback/test_behavior.py::test_defaults"


def observation(identity, revision, status="failed", *, prefix="", kind="public_checks"):
    stage = {"status": status, "nodeids": [NODE], "output_excerpt": f"FAILED {prefix}{NODE} - AssertionError"}
    return {"id": identity, "revision": revision, "kind": kind,
            "result": {"scope": "public", "status": status,
                       "stages": {"old_original": {"status": "passed"}, "new_candidate": stage}}}


def semantic_observation(identity, revision, fingerprint):
    row = observation(identity, revision)
    row["result"]["stages"]["new_candidate"]["failure_clusters"] = [{
        "fingerprint": fingerprint,
        "occurrences": 1,
        "nodeids": [NODE],
        "phases": ["call"],
    }]
    return row


def test_changed_revisions_and_reload_keep_failure_identity_without_modifying_observations():
    rows = [observation("a", "r1", prefix="checks/"), observation("b", "r2")]
    original = json.dumps(rows)
    result = recurring_public_failures(json.loads(original), "r3")
    assert result["items"][0] == {"nodeid": NODE, "distinct_failed_revisions": 2,
        "first_observation_id": "a", "latest_observation_id": "b", "latest_failed_revision": "r2",
        "stale": True, "probe_observations_since_first_failure": []}
    assert result["next_observation"] and "does not block edits" in result["next_observation"]
    assert json.dumps(rows) == original


def test_same_revision_retries_do_not_inflate_count_and_complete_pass_clears_warning():
    rows = [observation("a", "r1"), observation("b", "r1")]
    assert recurring_public_failures(rows, "r1")["items"] == []
    rows.append(observation("c", "r2"))
    assert recurring_public_failures(rows, "r2")["items"][0]["stale"] is False
    rows.append(observation("d", "r3", "passed"))
    assert recurring_public_failures(rows, "r3")["items"] == []
    rows.append(observation("e", "r4"))
    assert recurring_public_failures(rows, "r4")["items"] == []


def test_probe_is_linked_without_treating_it_as_resolution_or_another_public_failure():
    rows = [observation("a", "r1"), observation("p", "r1", "passed", kind="probe"),
            observation("b", "r2")]
    item = recurring_public_failures(rows, "r2")["items"][0]
    assert item["distinct_failed_revisions"] == 2
    assert item["probe_observations_since_first_failure"] == ["p"]


def test_uncollected_or_non_public_log_lines_and_incomplete_execution_are_not_evidence():
    rows = [observation("a", "r1"), observation("b", "r2")]
    rows[1]["result"]["scope"] = "acceptance"
    assert not recurring_public_failures(rows, "r2")["items"]
    rows[1]["result"]["scope"] = "public"
    rows[1]["result"]["status"] = "execution_incomplete"
    assert not recurring_public_failures(rows, "r2")["items"]
    stage = {"nodeids": [NODE]}
    assert failed_public_nodes(stage, f"FAILED acceptance/private.py::test_hidden\nFAILED {NODE} - error") == [NODE]
    assert failed_public_nodes(stage, f"some printed FAILED {NODE}") == []


def test_failure_identity_is_preserved_when_exception_tail_is_not_available():
    rows = [observation("a", "r1"), observation("b", "r2")]
    for row in rows:
        row["result"]["stages"]["new_candidate"].update(failed_nodeids=[NODE], output_excerpt="truncated output")
    assert recurring_public_failures(rows, "r2")["items"][0]["distinct_failed_revisions"] == 2


def test_semantic_signature_recurrence_is_visible_but_not_claimed_as_root_cause():
    fingerprint = "a" * 64
    result = recurring_public_failures([
        semantic_observation("a", "r1", fingerprint),
        semantic_observation("b", "r2", fingerprint),
    ], "r2")
    assert result["semantic_clusters"] == [{
        "fingerprint": fingerprint,
        "distinct_failed_revisions": 2,
        "first_observation_id": "a",
        "latest_observation_id": "b",
        "latest_failed_revision": "r2",
        "latest_nodeids": [NODE],
        "stale": False,
    }]
    assert "not that its root cause has been proven" in result["scope"]
