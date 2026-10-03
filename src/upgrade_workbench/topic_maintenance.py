"""逐主题维护解释：原知识不变，当前解释保留明确的替代链与修订身份。"""

from __future__ import annotations

import re

from .generation.source_context import digest


def _origin(value):
    if (not isinstance(value, dict) or set(value) != {"namespace", "sha256", "topic_id"}
            or value["namespace"] not in {"local", "imported"}
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or not isinstance(value["topic_id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", value["topic_id"])):
        raise ValueError("Invalid topic origin")
    return value["namespace"], value["sha256"], value["topic_id"]


def validate_revision(case, snapshot, action):
    from .generation.project_context import bind_pages

    _origin(action["origin"])
    previous = action["previous_topic_sha256"]
    if not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{64}", previous):
        raise ValueError("Topic revision requires previous explanation identity")
    page = bind_pages(case, snapshot, [action["page"]], require_complete=False)["pages"][0]
    if page["id"] != action["origin"]["topic_id"]:
        raise ValueError("Topic revision cannot rename its origin")
    return page


def view(revision, *, knowledge=None, knowledge_id=None, imported=None, observations=()):
    """每个主题独立失效；局部更新绝不能把未复核的其他解释洗成当前知识。"""
    entries = {}
    if knowledge is not None:
        for page in knowledge["pages"]:
            origin = {"namespace": "local", "sha256": knowledge_id, "topic_id": page["id"]}
            entries[_origin(origin)] = {
                "origin": origin, "topic": page, "topic_sha256": digest(page),
                "interpretation_revision": knowledge["revision"], "update_observation_id": None,
                "status": ("model_interpretation_unverified" if knowledge["revision"] == revision else "needs_review"),
            }
    if imported is not None:
        for page in imported["topics"]:
            original = {key: value for key, value in page.items() if key not in {"applicability", "import_origin", "lineage"}}
            origin = page.get("import_origin") or {"namespace": "imported", "sha256": imported["bundle_sha256"], "topic_id": page["id"]}
            entries[_origin(origin)] = {
                "origin": origin, "topic": original, "topic_sha256": digest(original),
                "interpretation_revision": (page["lineage"]["latest"] if "lineage" in page else imported["origin"])["revision"], "update_observation_id": None,
                "status": page["applicability"]["status"],
            }
    for observation in observations:
        if observation["action"]["type"] != "revise_project_topic":
            continue
        update = observation["result"]
        key = _origin(update["origin"])
        if key not in entries:
            # 新建整套本地知识后，旧套的主题更新仍留在历史，但不覆盖新套的同名主题。
            continue
        entry = entries[key]
        if entry["topic_sha256"] != update["previous_topic_sha256"]:
            raise ValueError("Topic update chain changed or forked")
        entries[key] = {
            "origin": update["origin"], "topic": update["topic"], "topic_sha256": update["topic_sha256"],
            "interpretation_revision": update["revision"], "update_observation_id": observation["id"],
            "status": "model_interpretation_unverified" if update["revision"] == revision else "needs_review",
        }
    return {"current_revision": revision, "topics": list(entries.values()),
            "meaning": "Per-origin current explanations, not execution evidence. Untouched origins retain their historical status; every candidate revision requires rechecking prior interpretations."}


def revision_result(case, snapshot, action, current_view):
    page = validate_revision(case, snapshot, action)
    key = _origin(action["origin"])
    entry = next((item for item in current_view["topics"] if _origin(item["origin"]) == key), None)
    if entry is None or entry["topic_sha256"] != action["previous_topic_sha256"]:
        raise ValueError("Topic revision does not match a supplied prior explanation")
    if page["kind"] != entry["topic"]["kind"]:
        raise ValueError("Topic revision cannot change its responsibility kind")
    return {"origin": action["origin"], "previous_topic_sha256": action["previous_topic_sha256"],
            "revision": snapshot.revision, "topic": page, "topic_sha256": digest(page),
            "meaning": "Source citations bound; revised explanation is not behavioral or acceptance evidence."}
