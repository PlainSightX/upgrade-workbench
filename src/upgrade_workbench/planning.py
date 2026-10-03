"""连接只读影响定位、固定版本依据和一次待审阅的生成请求。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .analysis import analyze_case
from .cases import CaseValidationError, load_case
from .cases.manifest import assert_no_links
from .evidence import load_version_evidence
from .generation import prepare_request


def create_analysis(manifest_path: Path, work_root: Path, *, candidate_reference: dict | None = None) -> dict:
    """已有案例没有版本资料时照实报告，不能把静态规则当成已绑定依据。"""
    case = load_case(manifest_path)
    report = analyze_case(case, candidate_reference=candidate_reference)
    if "evidence/bundle.json" in case.manifest.file_hashes:
        from .candidates import load_candidate
        from .generation.request import MAX_EVIDENCE_BYTES
        from .retrieval import evidence_for_sources

        report["version_evidence"] = load_version_evidence(case)
        report["retrieval"] = evidence_for_sources(
            case, load_candidate(case, candidate_reference).blocks(), max_bytes=MAX_EVIDENCE_BYTES,
        )
    else:
        report["version_evidence"] = None
    root = Path(work_root).absolute()
    assert_no_links(root)
    root = root.resolve()
    if root.is_relative_to(case.root):
        raise CaseValidationError("Analysis outputs must be outside the immutable case")
    directory = root / "analysis" / uuid4().hex
    assert_no_links(directory)
    directory.mkdir(parents=True, exist_ok=False)
    report.update(
        status="analysis_completed",
        target_code_executed=False,
        report_path=str(directory / "analysis.json"),
    )
    Path(report["report_path"]).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def prepare_case_proposal(manifest_path: Path, work_root: Path, **options) -> dict:
    """后台始终核验版本绑定，实验臂只控制生成者实际可见的信息。"""
    analysis = create_analysis(manifest_path, work_root, candidate_reference=options.get("candidate_reference"))
    if analysis["version_evidence"] is None:
        raise CaseValidationError("Proposal requires a registered evidence/bundle.json")
    case = load_case(manifest_path)
    contract_path = options.pop("contract_path", case.root / "business-contract.md")
    return prepare_request(
        case,
        analysis,
        analysis["version_evidence"]["entries"],
        work_root=work_root,
        contract_path=contract_path,
        **options,
    )
