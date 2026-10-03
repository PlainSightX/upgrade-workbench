"""等待依赖查询是正常工作状态，命令行必须给出可执行的接续提示。"""

import json

import pytest

from upgrade_workbench import cli


@pytest.mark.parametrize("command", ["task-status", "continue-task"])
def test_pending_query_is_non_error_and_explains_next_action(monkeypatch, capsys, command):
    state = {"schema_version": 5, "task_id": "test", "task_path": "unused.json",
             "case_id": "fixture", "arm": "direct_repair", "phase": "operation",
             "status": "pending_dependency_query", "attempts": [], "candidate": None}
    monkeypatch.setattr(cli, "inspect_task", lambda *_a, **_kw: state)
    monkeypatch.setattr(cli, "advance_task", lambda *_a, **_kw: state)
    assert cli.main([command, "unused.json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pending_dependency_query"
    assert "continue-task" in result["next_action"]
    assert "不调用模型" in result["next_action"]
