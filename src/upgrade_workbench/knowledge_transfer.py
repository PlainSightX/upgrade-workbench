"""同仓库知识准入：保存历史解释，重算当前来源，绝不导入旧行为结论。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .candidates import load_candidate
from .cases import load_case
from .cases.manifest import assert_no_links, read_verified_file, relative_path
from .generation.source_context import digest, encoded

VERSION = "knowledge-import-v1"
MAINTAINED_VERSION = "knowledge-import-v2"
MAX_BYTES = 1_000_000


def inputs(case, snapshot, execution):
    """记录全部公开源码及声明输入；宿主运行时密钥/变量从不成为知识输入。"""
    hashes = case.manifest.file_hashes
    names = [case.manifest.old_lock, case.manifest.new_lock, "business-contract.md",
             "contract-requirements.json", "environment.json"]
    names += list(case.environment.local_wheels) if case.environment else []
    return {
        "source": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(snapshot.files.items())},
        "declared": {name: hashes.get(name) for name in names} | {
            "case.allowed_changes": digest(sorted(case.manifest.allowed_changes)),
            "case.review": digest(case.manifest.review.model_dump()),
        },
        "base_image": execution.get("base_image"),
    }


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_bundle(bundle):
    """摘要用于完整性；本地导出来源不是第三方可信签名。"""
    if isinstance(bundle, dict) and bundle.get("version") == MAINTAINED_VERSION:
        try:
            return _validate_maintained(bundle)
        except (TypeError, AttributeError, KeyError) as error:
            # 嵌套历史版本也是外部输入；格式损坏统一走普通拒绝路径。
            raise ValueError("Invalid nested maintained knowledge bundle") from error
    return _validate_original(bundle)


def _validate_original(bundle, *, complete=True):
    fields = {"version", "repository", "origin", "inputs", "knowledge", "excerpts"}
    if not isinstance(bundle, dict) or set(bundle) != fields or bundle["version"] != VERSION:
        raise ValueError("Invalid knowledge bundle")
    if len(encoded(bundle)) > MAX_BYTES:
        raise ValueError("Knowledge bundle exceeds capacity")
    if not isinstance(bundle["repository"], str) or not bundle["repository"].strip():
        raise ValueError("Knowledge bundle requires repository identity")
    origin = bundle["origin"]
    if set(origin) != {"task_id", "case_fingerprint", "revision", "knowledge_id", "receipt_sha256", "response_sha256"}:
        raise ValueError("Invalid knowledge origin")
    if not re.fullmatch(r"[0-9a-f]{32}", origin["task_id"]) or any(not _sha(v) for k, v in origin.items() if k != "task_id"):
        raise ValueError("Invalid knowledge origin identity")
    identity = bundle["inputs"]
    if set(identity) != {"source", "declared", "base_image"}:
        raise ValueError("Invalid knowledge input identity")
    for group in ("source", "declared"):
        if not isinstance(identity[group], dict):
            raise ValueError("Invalid knowledge input inventory")
        for name, value in identity[group].items():
            relative_path(name)
            if not _sha(value) and not (group == "declared" and value is None):
                raise ValueError("Invalid knowledge input digest")
    if identity["base_image"] is not None and not isinstance(identity["base_image"], str):
        raise ValueError("Invalid knowledge environment identity")
    if not {"case.allowed_changes", "case.review", "business-contract.md", "contract-requirements.json",
            "environment.json", "requirements/old.txt", "requirements/new.txt"} <= set(identity["declared"]):
        raise ValueError("Knowledge declared input inventory is incomplete")
    knowledge = bundle["knowledge"]
    if (set(knowledge) != {"schema_version", "case_fingerprint", "revision", "source_sha256", "pages", "epistemic_status"}
            or knowledge["schema_version"] != 1 or not _sha(knowledge["source_sha256"])
            or knowledge["case_fingerprint"] != origin["case_fingerprint"]
            or knowledge["revision"] != origin["revision"]
            or knowledge["epistemic_status"] != "model_interpretation_not_behavioral_evidence"):
        raise ValueError("Knowledge bundle origin differs from explanations")
    pages = knowledge["pages"]
    if not isinstance(pages, list) or not (3 if complete else 1) <= len(pages) <= 16:
        raise ValueError("Invalid bounded knowledge pages")
    seen, excerpts = set(), {}
    for page in pages:
        if len(encoded(page)) > 20_000:
            raise ValueError("Knowledge page exceeds source tool capacity")
        if set(page) != {"id", "kind", "title", "explanation", "sources", "unknowns"}:
            raise ValueError("Unexpected fields in knowledge page")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", page["id"]) or page["id"] in seen:
            raise ValueError("Invalid knowledge topic identity")
        seen.add(page["id"])
        if page["kind"] not in {"module", "flow", "constraint"}:
            raise ValueError("Invalid knowledge topic kind")
        from .generation.project_context import _text

        _text(page["title"], 160)
        _text(page["explanation"], 5000)
        if not isinstance(page["unknowns"], list) or len(page["unknowns"]) > 8:
            raise ValueError("Invalid knowledge unknowns")
        for unknown in page["unknowns"]:
            _text(unknown, 1000)
        if not isinstance(page["sources"], list) or not 1 <= len(page["sources"]) <= 12:
            raise ValueError("Invalid knowledge citations")
        paths = set()
        for ref in page["sources"]:
            if set(ref) != {"path", "start_line", "end_line", "file_sha256", "excerpt_sha256"}:
                raise ValueError("Invalid knowledge citation fields")
            name = relative_path(ref["path"])
            paths.add(name)
            expected = (identity["declared"].get(name) if name == "business-contract.md"
                        else identity["source"].get(name))
            if expected is None or expected != ref["file_sha256"] or not _sha(ref["excerpt_sha256"]):
                raise ValueError("Citation is outside exported public inputs")
            start, end = ref["start_line"], ref["end_line"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise ValueError("Invalid citation range")
            text = bundle["excerpts"].get(ref["excerpt_sha256"])
            if (not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != ref["excerpt_sha256"]
                    or len(text.splitlines(keepends=True)) != end - start + 1):
                raise ValueError("Knowledge excerpt identity differs")
            excerpts[ref["excerpt_sha256"]] = text
        if page["kind"] == "constraint" and "business-contract.md" not in paths:
            raise ValueError("Constraint lacks public contract")
        if page["kind"] == "flow" and len(paths - {"business-contract.md"}) < 2:
            raise ValueError("Flow lacks cross-file citations")
    if (complete and {page["kind"] for page in pages} != {"module", "flow", "constraint"}) or bundle["excerpts"] != excerpts:
        raise ValueError("Knowledge bundle has missing topics or extra source text")
    unbound = [page | {"sources": [{k: v for k, v in ref.items() if k not in {"file_sha256", "excerpt_sha256"}}
                                  for ref in page["sources"]]} for page in pages]
    if len(encoded(unbound)) > 40_000:
        raise ValueError("Knowledge exceeds original action capacity")
    return bundle


def _local_bundle(task):
    from .diagnostics import public_knowledge, read

    reference = task.get("project_knowledge_reference")
    if reference is None:
        raise ValueError("Task has no locally authored knowledge to export")
    knowledge = public_knowledge(task, reference)
    record = read(reference, Path(task["task_path"]).parent)
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, record["source_reference"])
    receipt = json.loads(Path(record["receipt_path"]).read_bytes())
    request_path = Path(receipt["request_path"])
    assert_no_links(request_path)
    if request_path.parent != Path(record["receipt_path"]).parent:
        raise ValueError("Knowledge request outside source receipt")
    request = request_path.read_bytes()
    context = json.loads(json.loads(request)["messages"][1]["content"])
    if (hashlib.sha256(request).hexdigest() != receipt["request_sha256"]
            or context["task_context"]["task_id"] != task["task_id"]
            or context["candidate"]["revision"] != snapshot.revision):
        raise ValueError("Knowledge source request identity differs")
    excerpts = {}
    for page in knowledge["pages"]:
        for ref in page["sources"]:
            name = ref["path"]
            data = (read_verified_file(case.root, name, case.manifest.file_hashes[name])
                    if name == "business-contract.md" else snapshot.files[name])
            excerpts[ref["excerpt_sha256"]] = "".join(data.decode().splitlines(keepends=True)[ref["start_line"]-1:ref["end_line"]])
    bundle = validate_bundle({"version": VERSION, "repository": case.manifest.source.repository,
        "origin": {"task_id": task["task_id"], "case_fingerprint": case.fingerprint,
                   "revision": snapshot.revision, "knowledge_id": reference["id"],
                   "receipt_sha256": record["receipt_sha256"], "response_sha256": receipt["response_sha256"]},
        "inputs": inputs(case, snapshot, task["protocol"].get("execution", {})),
        "knowledge": knowledge, "excerpts": excerpts})
    return bundle


def export_bundle(task_path, output):
    """核验原模型来源后导出；混合版本逐页保留，不把历史知识洗成当前。"""
    from .diagnostics import public_observation
    from .tasks import inspect_task

    task = inspect_task(Path(task_path))
    case = load_case(Path(task["manifest_path"]))
    updated = any(public_observation(task, ref)["action"]["type"] == "revise_project_topic"
                  for ref in task.get("observations", []))
    if updated or task.get("knowledge_import_reference"):
        bundle = _export_maintained(task, case)
    else:
        bundle = _local_bundle(task)
    output = Path(output).absolute()
    assert_no_links(output)
    if output.resolve().is_relative_to(case.root):
        raise ValueError("Knowledge export must stay outside immutable case")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = encoded(bundle)
    with output.open("xb") as stream:
        stream.write(data)
    return {"path": str(output), "sha256": hashlib.sha256(data).hexdigest(),
            "topics": len(bundle["knowledge"]["pages"] if bundle["version"] == VERSION else bundle["topics"]),
            "source_task_id": task["task_id"]}


def load_bundle(spec, case):
    if not isinstance(spec, dict) or set(spec) != {"path", "sha256"} or not _sha(spec["sha256"]):
        raise ValueError("Knowledge import requires path and expected sha256")
    path = Path(spec["path"]).absolute()
    assert_no_links(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Knowledge bundle exceeds capacity")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise ValueError("Knowledge bundle digest differs")
    bundle = validate_bundle(json.loads(data))
    if encoded(bundle) != data:
        raise ValueError("Knowledge bundle must retain exported canonical bytes")
    if bundle["repository"] != case.manifest.source.repository:
        raise ValueError("Knowledge import requires the same repository")
    return bundle


def binding(bundle):
    return {"version": bundle["version"], "bundle_sha256": digest(bundle)}


def public_import(task, case, snapshot):
    """请求从任务内不可变副本重算，不依赖原任务或外部知识包仍在线。"""
    from .diagnostics import read

    protocol = task.get("protocol", task)
    contract = protocol.get("knowledge_import")
    reference = task.get("knowledge_import_reference")
    if contract is None and reference is None:
        return None
    if not contract or not reference or reference["id"] != contract["bundle_sha256"]:
        raise ValueError("Knowledge import binding differs")
    bundle = validate_bundle(read(reference, Path(task["task_path"]).parent))
    if binding(bundle) != contract or bundle["repository"] != case.manifest.source.repository:
        raise ValueError("Knowledge import repository or protocol differs")
    current = inputs(case, snapshot, protocol.get("execution", {}))
    if bundle["version"] == MAINTAINED_VERSION:
        return _public_maintained(bundle, contract, case, snapshot, current)
    return _public_original(bundle, contract, case, snapshot, current)


def _public_original(bundle, contract, case, snapshot, current):
    old = bundle["inputs"]
    changed_source = sorted(name for name in old["source"].keys() | current["source"].keys()
                            if old["source"].get(name) != current["source"].get(name))
    changed_inputs = sorted(name for name in old["declared"].keys() | current["declared"].keys()
                            if old["declared"].get(name) != current["declared"].get(name))
    if old["base_image"] != current["base_image"]:
        changed_inputs.append("execution.base_image")
    topics = []
    for page in bundle["knowledge"]["pages"]:
        citations = []
        for ref in page["sources"]:
            name = ref["path"]
            data = (read_verified_file(case.root, name, case.manifest.file_hashes[name])
                    if name == "business-contract.md" else snapshot.files.get(name))
            location, status = None, "changed_or_missing"
            if data is not None:
                lines = data.decode().splitlines(keepends=True)
                excerpt = bundle["excerpts"][ref["excerpt_sha256"]]
                if hashlib.sha256(data).hexdigest() == ref["file_sha256"]:
                    if "".join(lines[ref["start_line"]-1:ref["end_line"]]) != excerpt:
                        raise ValueError("Knowledge citation differs from current identical file")
                    location = {"start_line": ref["start_line"], "end_line": ref["end_line"]}
                    status = "unchanged_file"
                else:
                    count = ref["end_line"] - ref["start_line"] + 1
                    found = [i for i in range(len(lines)-count+1) if "".join(lines[i:i+count]) == excerpt]
                    if len(found) == 1:
                        location = {"start_line": found[0]+1, "end_line": found[0]+count}
                        status = "unique_excerpt_relocated"
                    elif len(found) > 1:
                        status = "ambiguous_excerpt"
            citations.append({"historical": ref, "status": status, "current_location": location})
        topics.append(page | {"applicability": {
            "sources": citations,
            "status": "needs_review" if changed_source or changed_inputs else "reference_only",
            "interpretation": "historical_model_interpretation_unverified"}})
    return {"bundle_sha256": contract["bundle_sha256"], "origin": bundle["origin"],
            "repository": bundle["repository"], "current_revision": snapshot.revision,
            "changed_source_paths": changed_source, "changed_declared_inputs": changed_inputs,
            "topics": topics,
            "meaning": "Historical interpretations, never current behavior or acceptance. Recheck changed, missing or uncertain dependencies, callers and configuration. Runtime configuration outside declared inputs is unknown. No old observations, patches or acceptance answers are imported."}


def _root_page(bundle, identity):
    return next(page for page in bundle["knowledge"]["pages"] if page["id"] == identity)


def _validate_maintained(bundle):
    """逐版验证引用与前序身份；摘要不构成第三方签名或行为证明。"""
    if (set(bundle) != {"version", "repository", "origin", "roots", "topics"}
            or len(encoded(bundle)) > MAX_BYTES
            or not isinstance(bundle["roots"], dict) or not bundle["roots"]
            or not isinstance(bundle["topics"], list) or not bundle["topics"]
            or not isinstance(bundle["origin"], dict) or set(bundle["origin"]) != {"task_id"}
            or not isinstance(bundle["origin"]["task_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", bundle["origin"]["task_id"])):
        raise ValueError("Invalid maintained knowledge bundle")
    for identity, original in bundle["roots"].items():
        _validate_original(original)
        if digest(original) != identity or original["repository"] != bundle["repository"]:
            raise ValueError("Maintained root identity differs")
    seen, used = set(), set()
    for topic in bundle["topics"]:
        if (not isinstance(topic, dict) or set(topic) != {"root_sha256", "topic_id", "updates"}
                or not _sha(topic["root_sha256"]) or not isinstance(topic["topic_id"], str)
                or not isinstance(topic["updates"], list)):
            raise ValueError("Invalid maintained topic chain")
        key = (topic["root_sha256"], topic["topic_id"])
        if key in seen or key[0] not in bundle["roots"]:
            raise ValueError("Duplicated or unknown maintained topic origin")
        seen.add(key)
        used.add(key[0])
        try:
            page = _root_page(bundle["roots"][key[0]], key[1])
        except StopIteration as error:
            raise ValueError("Unknown root topic") from error
        update_ids = set()
        for update in topic["updates"]:
            if (not isinstance(update, dict) or set(update) != {"previous_topic_sha256", "request_sha256", "bundle"}
                    or update["previous_topic_sha256"] != digest(page) or not _sha(update["request_sha256"])):
                raise ValueError("Maintained explanation chain changed or forked")
            version = _validate_original(update["bundle"], complete=False)
            if (version["repository"] != bundle["repository"] or len(version["knowledge"]["pages"]) != 1
                    or version["knowledge"]["pages"][0]["id"] != key[1]
                    or version["knowledge"]["pages"][0]["kind"] != page["kind"]):
                raise ValueError("Maintained update changes topic responsibility")
            identity = (version["origin"]["task_id"], version["origin"]["knowledge_id"])
            if identity in update_ids:
                raise ValueError("Duplicated maintained update")
            update_ids.add(identity)
            page = version["knowledge"]["pages"][0]
    if used != set(bundle["roots"]):
        raise ValueError("Maintained bundle includes inactive roots")
    return bundle


def _export_maintained(task, case):
    import copy

    from .diagnostics import public_observation, read
    from .generation.project_context import bind_pages

    roots, topics, handles = {}, [], {}
    imported_ref = task.get("knowledge_import_reference")
    if imported_ref:
        imported = validate_bundle(read(imported_ref, Path(task["task_path"]).parent))
        if binding(imported) != task["protocol"]["knowledge_import"]:
            raise ValueError("Maintained import identity changed")
        if imported["version"] == VERSION:
            identity = digest(imported)
            roots[identity] = imported
            topics = [{"root_sha256": identity, "topic_id": page["id"], "updates": []}
                      for page in imported["knowledge"]["pages"]]
        else:
            roots = copy.deepcopy(imported["roots"])
            topics = copy.deepcopy(imported["topics"])
        for topic in topics:
            handles[("imported", topic["root_sha256"], topic["topic_id"])] = topic
    if task.get("project_knowledge_reference"):
        original = _local_bundle(task)
        identity = digest(original)
        roots[identity] = original
        for page in original["knowledge"]["pages"]:
            topic = {"root_sha256": identity, "topic_id": page["id"], "updates": []}
            topics.append(topic)
            handles[("local", task["project_knowledge_reference"]["id"], page["id"])] = topic
    if not topics:
        raise ValueError("Task has no knowledge to export")
    for reference in task.get("observations", []):
        public = public_observation(task, reference)
        if public["action"]["type"] != "revise_project_topic":
            continue
        update = public["result"]
        origin = update["origin"]
        topic = handles.get((origin["namespace"], origin["sha256"], origin["topic_id"]))
        if topic is None:
            # 被新整套本地知识替换的旧主题只留在任务历史。
            continue
        record = read(reference, Path(task["task_path"]).parent)
        snapshot = load_candidate(case, record["source_reference"])
        receipt_ref = record["generation_receipt"]
        receipt = json.loads(Path(receipt_ref["path"]).read_bytes())
        knowledge = bind_pages(case, snapshot, [record["action"]["page"]], require_complete=False)
        excerpts = {}
        for ref in knowledge["pages"][0]["sources"]:
            name = ref["path"]
            data = (read_verified_file(case.root, name, case.manifest.file_hashes[name])
                    if name == "business-contract.md" else snapshot.files[name])
            excerpts[ref["excerpt_sha256"]] = "".join(data.decode().splitlines(keepends=True)[ref["start_line"]-1:ref["end_line"]])
        version = {"version": VERSION, "repository": case.manifest.source.repository,
                   "origin": {"task_id": task["task_id"], "case_fingerprint": case.fingerprint,
                              "revision": snapshot.revision, "knowledge_id": reference["id"],
                              "receipt_sha256": receipt_ref["sha256"], "response_sha256": receipt["response_sha256"]},
                   "inputs": inputs(case, snapshot, task["protocol"].get("execution", {})),
                   "knowledge": knowledge, "excerpts": excerpts}
        topic["updates"].append({"previous_topic_sha256": update["previous_topic_sha256"],
                                 "request_sha256": receipt["request_sha256"], "bundle": version})
    return _validate_maintained({"version": MAINTAINED_VERSION, "repository": case.manifest.source.repository,
                                "origin": {"task_id": task["task_id"]}, "roots": roots, "topics": topics})


def _public_maintained(bundle, contract, case, snapshot, current):
    topics, changed_sources, changed_inputs = [], set(), set()
    for chain in bundle["topics"]:
        root = bundle["roots"][chain["root_sha256"]]
        latest = chain["updates"][-1]["bundle"] if chain["updates"] else root
        view = _public_original(latest, contract, case, snapshot, current)
        topic = next(page for page in view["topics"] if page["id"] == chain["topic_id"])
        topic["import_origin"] = {"namespace": "imported", "sha256": chain["root_sha256"], "topic_id": chain["topic_id"]}
        topic["lineage"] = {"root": root["origin"], "latest": latest["origin"], "updates": len(chain["updates"])}
        topics.append(topic)
        changed_sources.update(view["changed_source_paths"])
        changed_inputs.update(view["changed_declared_inputs"])
    return {"version": MAINTAINED_VERSION, "bundle_sha256": contract["bundle_sha256"], "origin": bundle["origin"],
            "repository": bundle["repository"], "current_revision": snapshot.revision,
            "changed_source_paths": sorted(changed_sources), "changed_declared_inputs": sorted(changed_inputs),
            "topics": topics, "meaning": "Each topic retains its own source and explanation revision. Imported interpretations are never current behavioral evidence; unchanged topics are not renewed by exporting others."}
