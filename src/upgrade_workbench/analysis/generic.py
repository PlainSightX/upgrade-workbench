"""提供真实源码绑定和通用依赖导航，不虚构某个库的静态影响规则。"""

from ..candidates import load_candidate
from ..evidence import load_version_evidence
from .dependencies import build_dependencies


def analyze_case(case, *, candidate_reference=None):
    binding = load_version_evidence(case)["binding"]
    snapshot = load_candidate(case, candidate_reference)
    blocks = snapshot.blocks()
    return {
        "schema_version": 1,
        "case_id": case.manifest.case_id,
        "case_fingerprint": case.fingerprint,
        "analyzer": {"name": "version-bound-generic-source-navigation", "version": "0.1.0"},
        "source_binding": {
            "revision": snapshot.revision,
            "files": {block["path"]: block["sha256"] for block in blocks},
        },
        "findings": [],
        "unknowns": [],
        "dependencies": build_dependencies(blocks, []),
        "scope_limitations": [
            f"No dedicated {binding['package']} static impact rules are implemented.",
            "Empty findings do not establish compatibility. Use source navigation, bound official documentation and isolated behavioral checks.",
            "Import and symbol relationships are navigation hints, not complete request routing or runtime type inference.",
        ],
    }
