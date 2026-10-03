"""只验证通用状态策略，不执行第三方源码或推定模型修复能力。"""

from upgrade_workbench.diagnostic_state import diagnostic_execution_summary, note_progress


def state():
    return {"protocol": {"diagnostic_policy": {"max_observation_revisits": 4}}, "status": "ready"}


def test_new_evidence_resets_revisits_and_reloading_preserves_count():
    import json

    task = state()
    note_progress(task, "r1", observation_id="a")
    note_progress(task, "r1", observation_id="b")
    note_progress(task, "r1", observation_id="a")
    note_progress(task, "r1", observation_id="b")
    assert task["diagnostic_progress"]["warning"]
    task = json.loads(json.dumps(task))
    note_progress(task, "r1", observation_id="new-source")
    assert task["diagnostic_progress"]["consecutive_revisits"] == 0
    assert not task["diagnostic_progress"]["warning"]
    for _ in range(2):
        note_progress(task, "r1", observation_id="a")
    note_progress(task, "r2", observation_id="a")
    assert task["diagnostic_progress"]["consecutive_revisits"] == 0
    assert task["status"] == "ready"


def test_new_reviewed_execution_resets_counter_even_for_existing_observation():
    task = state()
    note_progress(task, "r1", observation_id="a")
    for _ in range(3):
        note_progress(task, "r1", observation_id="a")
    note_progress(task, "r1", observation_id="a", changed=True)
    assert task["diagnostic_progress"]["consecutive_revisits"] == 0
    assert task["status"] == "ready"


def test_execution_projection_keeps_historical_and_target_completion_separate():
    import copy

    task = {"diagnostic_runs": [
        {"status": "completed"},
        {"status": "completed", "phase": "completed", "last_phase": "target_completed",
         "outcome": "semantic_failed"},
        {"status": "failed", "phase": "failed", "last_phase": "started",
         "last_action": "materialize"},
        {"status": "unknown", "phase": "unknown", "last_phase": "target_started"},
        *[{"status": "running", "phase": "started"} for _ in range(14)],
    ]}
    original = copy.deepcopy(task)
    summary = diagnostic_execution_summary(task)

    assert summary["counts"] == {
        "registered": 18, "running": 14, "completed": 1, "failed": 1,
        "unknown": 1, "historical": 1, "target_completed": 1,
    }
    assert len(summary["runs"]) == 16 and summary["runs_truncated"]
    assert summary["latest"]["phase"] == "started"
    assert task == original
