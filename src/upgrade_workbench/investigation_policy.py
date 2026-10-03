"""任务创建时冻结调查预算；用真实输入判断重复，不替模型宣布业务进展。"""

from __future__ import annotations

VERSION = "investigation-budget-v1"
DEFAULT_DIAGNOSTICS = {
    "max_runs": 8, "max_probes": 3, "max_revisions_per_probe": 2, "max_observation_revisits": 4,
}
DEFAULT_FINISH = {"explanation_max_characters": 2000, "evidence_refs_max_items": 8}


def policy(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != VERSION or set(value) - {
        "version", "diagnostic_limits", "finish_limits", "finish_notice_calls"
    }:
        raise ValueError("Unknown investigation policy")
    result = {"version": VERSION}
    for key, defaults in (("diagnostic_limits", DEFAULT_DIAGNOSTICS), ("finish_limits", DEFAULT_FINISH)):
        supplied = value.get(key, {})
        if not isinstance(supplied, dict) or set(supplied) - set(defaults):
            raise ValueError(f"Unknown {key}")
        result[key] = defaults | supplied
        for name, limit in result[key].items():
            if type(limit) is not int or limit < 1:
                raise ValueError(f"{name} must be a frozen positive integer")
    if result["diagnostic_limits"]["max_observation_revisits"] < 2:
        raise ValueError("Repeated observation limit must leave room for a warning")
    notice = value.get("finish_notice_calls", 2)
    if type(notice) is not int or notice < 1:
        raise ValueError("finish_notice_calls must be a positive integer")
    result["finish_notice_calls"] = notice
    return result


def observation_visible(context, observation, cursor=None):
    """只检查本次真实请求，不用动作执行后的最新观察反推模型看过什么。"""
    runtime = context.get("diagnostic_state", {})
    if cursor is not None:
        from .diagnostic_output import output_cursor

        page = runtime.get("observation_output", {})
        fields = ("stage", "stream", "sha256", "offset")
        return (page.get("observation_id") == observation["id"] and all(key in page for key in fields)
                and output_cursor(*(page[key] for key in fields)) == cursor)
    for row in runtime.get("observations", []) + runtime.get("navigation_memory", {}).get("observations", []):
        if (row.get("id") == observation["id"] and row.get("revision") == observation["revision"]
                and row.get("result") == observation["result"]):
            return True
    result = observation["result"]
    if observation["kind"] == "source_excerpts":
        return bool(result["reads"]) and all(observation_visible(context, {
            "id": observation["id"], "revision": row["revision"], "kind": "source",
            "action": {"type": "read_source"}, "result": row}) for row in result["reads"])
    if observation["kind"] != "source" or observation["action"]["type"] != "read_source":
        return False
    # 同一字节可在常驻原文中完整提供；路径/行号命中本身不能证明内容一致。
    for block in context.get("source_files", []):
        if (block.get("path") != result.get("path") or block.get("revision") != result.get("revision")
                or block.get("start_line", 0) > result["start_line"]
                or block.get("end_line", 0) < result["end_line"]):
            continue
        start = result["start_line"] - block["start_line"]
        end = result["end_line"] - block["start_line"] + 1
        if block.get("text", "").splitlines(keepends=True)[start:end] == result["text"].splitlines(keepends=True):
            return True
    return False


def note_progress(task, revision, *, observation=None, cursor=None, context=None, changed=False):
    state = task.setdefault("diagnostic_progress", {})
    if state.get("revision") != revision:
        state.clear()
        state.update(revision=revision, seen_observations=[], seen_output_pages=[], consecutive_revisits=0)
    visible = False
    repeated = False
    if observation is not None:
        seen = state.setdefault("seen_output_pages", []) if cursor else state["seen_observations"]
        identity = [observation["id"], cursor] if cursor else observation["id"]
        visible = observation_visible(context or {}, observation, cursor)
        repeated = identity in seen and visible and not changed
        if identity not in seen:
            seen.append(identity)
    if observation is not None or changed:
        state["consecutive_revisits"] = state["consecutive_revisits"] + 1 if repeated else 0
    # 未产生可核验信息的动作不能替重复调查清零；总预算另行约束所有调用。
    state["last_retrieval"] = {
        "observation_id": observation["id"] if observation else None,
        "cursor": cursor, "already_visible_in_request": visible, "counted_as_repeat": repeated,
        "meaning": "Information retrieval only; not proof of repair or verified behavior.",
    }
    count = state["consecutive_revisits"]
    limit = task["protocol"]["diagnostic_policy"]["max_observation_revisits"]
    state["warning"] = (
        "Repeated retrieval of unchanged evidence already present in your request. Refer to the visible "
        "observation/source or obtain a new discriminating observation. This is not fresh verification. "
        f"Consecutive repeats: {count}/{limit}." if count >= min(2, limit - 1) else None
    )
    if count >= limit:
        task.update(status="unresolved", stop_reason="repeated_visible_evidence_no_progress")
