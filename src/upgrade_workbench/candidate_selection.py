"""基于完整公开观察选择候选；本模块只计算，不修改任务状态。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

_DIGEST = re.compile(r"[0-9a-f]{64}")


class CandidateSelectionError(ValueError):
    """候选元数据或公开观察不足以支持可比较选择。"""


def _digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PublicSuite:
    """同一案例的一组完整公开检查；nodeid 顺序也是身份的一部分。"""

    case_fingerprint: str
    nodeids: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class CandidateCoverage:
    """一个已审候选在一组完整公开检查上的精确通过集合。"""

    revision: str
    patch_sha256: str
    origin: str
    candidate_reference: Mapping[str, str]
    observation_ref: str
    suite: PublicSuite
    passed_nodeids: tuple[str, ...]
    failed_nodeids: tuple[str, ...]
    observation_index: int

    @property
    def passed_count(self) -> int:
        return len(self.passed_nodeids)

    @property
    def total_count(self) -> int:
        return len(self.suite.nodeids)

    def public(self) -> dict:
        """返回可持久化的选择结果，不暴露或改变任务内部状态。"""
        return {
            "revision": self.revision,
            "patch_sha256": self.patch_sha256,
            "origin": self.origin,
            "candidate_reference": dict(self.candidate_reference),
            "observation_ref": self.observation_ref,
            "suite_sha256": self.suite.sha256,
            "passed_nodeids": list(self.passed_nodeids),
            "failed_nodeids": list(self.failed_nodeids),
            "passed_count": self.passed_count,
            "total_count": self.total_count,
        }


def _nodeids(stage: Mapping[str, object], field: str) -> tuple[str, ...]:
    values = stage.get(field)
    if (
        not isinstance(values, list)
        or not values
        or stage.get(field + "_truncated") is True
        or any(not isinstance(value, str) or not value or "\x00" in value for value in values)
        or len(values) != len(set(values))
    ):
        raise CandidateSelectionError(f"{field} is absent, truncated, duplicated, or invalid")
    return tuple(values)


def _public_suite(
    case_fingerprint: str,
    observation: Mapping[str, object],
) -> tuple[PublicSuite, tuple[str, ...], tuple[str, ...]]:
    if observation.get("kind") != "public_checks":
        raise CandidateSelectionError("Only public-check observations are comparable")
    result = observation.get("result")
    if not isinstance(result, Mapping) or result.get("scope") != "public":
        raise CandidateSelectionError("Observation is not a bounded public result")
    if result.get("status") not in {"passed", "failed"}:
        raise CandidateSelectionError("Incomplete public execution is not comparable")
    stages = result.get("stages")
    if not isinstance(stages, Mapping):
        raise CandidateSelectionError("Public result has no stage identity")
    old = stages.get("old_original")
    target = stages.get("new_candidate")
    if isinstance(target, Mapping) and target.get("status") == "not_supplied":
        target = stages.get("new_original")
    if not isinstance(old, Mapping) or not isinstance(target, Mapping):
        raise CandidateSelectionError("Public result lacks old and candidate stages")
    if old.get("status") != "passed" or target.get("status") not in {"passed", "failed"}:
        raise CandidateSelectionError("Public stages did not complete on a valid baseline")
    old_nodes = _nodeids(old, "nodeids")
    target_nodes = _nodeids(target, "nodeids")
    if old_nodes != target_nodes:
        raise CandidateSelectionError("Public suite changed between baseline and candidate")
    raw_failed = target.get("failed_nodeids", [])
    if target.get("failed_nodeids_truncated") is True:
        raise CandidateSelectionError("Failed-node identity was truncated")
    if not isinstance(raw_failed, list) or any(
        not isinstance(value, str) or not value or "\x00" in value for value in raw_failed
    ):
        raise CandidateSelectionError("Failed-node identity is invalid")
    if len(raw_failed) != len(set(raw_failed)) or not set(raw_failed) <= set(target_nodes):
        raise CandidateSelectionError("Failed nodes are duplicated or outside the public suite")
    failed_set = set(raw_failed)
    failed = tuple(node for node in target_nodes if node in failed_set)
    if target.get("status") == "passed":
        if failed or result.get("status") != "passed":
            raise CandidateSelectionError("Passing status disagrees with failed-node evidence")
    elif not failed or result.get("status") != "failed":
        raise CandidateSelectionError("Failed execution lacks a complete failed-node set")
    passed = tuple(node for node in target_nodes if node not in failed_set)
    identity = {
        "case_fingerprint": case_fingerprint,
        "scope": "public",
        "nodeids": list(target_nodes),
    }
    return PublicSuite(case_fingerprint, target_nodes, _digest(identity)), passed, failed


def _reviewed_candidates(task: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    reviews = task.get("review_history", [])
    history = task.get("candidate_history", [])
    if not isinstance(reviews, list) or not isinstance(history, list):
        raise CandidateSelectionError("Candidate and review history must be lists")
    reviewed: dict[str, Mapping[str, object]] = {}
    for candidate in history:
        if not isinstance(candidate, Mapping) or candidate.get("reviewed") is not True:
            continue
        revision = candidate.get("revision")
        patch_sha256 = candidate.get("sha256")
        patch_path = candidate.get("patch_path")
        if (
            not isinstance(revision, str)
            or not _DIGEST.fullmatch(revision)
            or not isinstance(patch_sha256, str)
            or not _DIGEST.fullmatch(patch_sha256)
            or not isinstance(patch_path, str)
            or not patch_path
            or candidate.get("origin") not in {"official_tool", "agent_candidate"}
        ):
            continue
        accepted = any(
            isinstance(review, Mapping)
            and review.get("revision") == revision
            and review.get("sha256") == patch_sha256
            and isinstance(review.get("reviewer"), str)
            and review["reviewer"].strip()
            and isinstance(review.get("note"), str)
            and review["note"].strip()
            and review.get("decision") != "rejected"
            for review in reviews
        )
        if accepted:
            reviewed[revision] = candidate
    return reviewed


def collect_candidate_coverages(
    task: Mapping[str, object],
    observations: Iterable[Mapping[str, object]],
) -> tuple[CandidateCoverage, ...]:
    """提取已审候选的完整公开覆盖；同 revision/suite 仅保留最后一次观察。"""
    case_fingerprint = task.get("case_fingerprint")
    if not isinstance(case_fingerprint, str) or not _DIGEST.fullmatch(case_fingerprint):
        raise CandidateSelectionError("Task requires an exact case fingerprint")
    candidates = _reviewed_candidates(task)
    selected: dict[tuple[str, str], CandidateCoverage] = {}
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            continue
        revision = observation.get("revision")
        candidate = candidates.get(revision) if isinstance(revision, str) else None
        if candidate is None:
            continue
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not _DIGEST.fullmatch(observation_id):
            continue
        try:
            suite, passed, failed = _public_suite(case_fingerprint, observation)
        except CandidateSelectionError:
            continue
        patch_path = Path(str(candidate["patch_path"]))
        coverage = CandidateCoverage(
            revision=revision,
            patch_sha256=str(candidate["sha256"]),
            origin=str(candidate["origin"]),
            candidate_reference={
                "path": str(patch_path.with_name("revision.json")),
                "revision": revision,
            },
            observation_ref="observation:" + observation_id,
            suite=suite,
            passed_nodeids=passed,
            failed_nodeids=failed,
            observation_index=index,
        )
        selected[(revision, suite.sha256)] = coverage
    return tuple(sorted(selected.values(), key=lambda item: item.observation_index))


def comparable_candidate_groups(
    task: Mapping[str, object],
    observations: Iterable[Mapping[str, object]],
) -> tuple[tuple[CandidateCoverage, ...], ...]:
    """按 suite 分组并按公开通过数排序；同分不同通过集合不会被合并。"""
    grouped: dict[str, list[CandidateCoverage]] = defaultdict(list)
    for coverage in collect_candidate_coverages(task, observations):
        grouped[coverage.suite.sha256].append(coverage)
    groups = []
    for values in grouped.values():
        groups.append(tuple(sorted(
            values,
            key=lambda item: (-item.passed_count, -item.observation_index, item.revision),
        )))
    return tuple(sorted(
        groups,
        key=lambda group: (-max(item.observation_index for item in group), group[0].suite.sha256),
    ))


def select_edit_base(
    task: Mapping[str, object],
    observations: Iterable[Mapping[str, object]],
    revision: str,
    *,
    suite_sha256: str | None = None,
) -> CandidateCoverage:
    """选择一个可比较的编辑基底；调用方决定是否切换指针，本函数绝不改任务。"""
    matches = [
        item
        for item in collect_candidate_coverages(task, observations)
        if item.revision == revision
        and (suite_sha256 is None or item.suite.sha256 == suite_sha256)
    ]
    if not matches:
        raise CandidateSelectionError("Requested revision lacks reviewed, complete public evidence")
    if len(matches) > 1:
        raise CandidateSelectionError("Requested revision is ambiguous across public suites")
    return matches[0]
