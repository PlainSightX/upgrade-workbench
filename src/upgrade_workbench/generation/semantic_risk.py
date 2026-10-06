"""可选的版本语义自查；改变调查提示，不授予验收权限或增加退出门禁。"""

from __future__ import annotations

POLICY = "semantic-risk-v1"
COMPARISON_POLICY = "semantic-risk-v2"


def validate_policy(value: str | None) -> str | None:
    if value is not None and value not in {POLICY, COMPARISON_POLICY}:
        raise ValueError("Unknown semantic risk policy")
    return value


INSTRUCTIONS = (
    " Semantic-risk-v1: before choosing an API replacement, distinguish the exact receiver, "
    "call path and input interpretation in the OLD and NEW versions using supplied version "
    "evidence or current source. Similar method names on different objects need not share "
    "semantics. Cite the relevant public source/evidence reference in your summary; do not "
    "substitute general library knowledge for a conflicting supplied passage. "
    "Before declaring behavior verified, compare what the current measurements actually "
    "observe with the semantic changes introduced by your patch: input interpretation, "
    "returned values, side effects and lifecycle where applicable. State which input classes "
    "were exercised and which plausible differences remain unmeasured. A successful ordinary "
    "path does not prove preservation of all legal inputs. If two plausible implementations "
    "fit the observed success but differ under the contract, use existing read/probe tools "
    "to construct one discriminating measurement derived from those semantics. Prefer "
    "assertions on actual values or externally observable effects, not mere absence of an "
    "exception. A differential probe may use OLD behavior as a reference only where the "
    "business contract requires preservation; intentional changes require explicit resolution. "
    "Do not invent tests just to fill a table. Keep conclusions limited to the measured inputs "
    "and report unresolved uncertainty when the existing budget or tools cannot resolve it. "
    "Use the existing action schema and summary, with no new role or mandatory extra call. "
    "This self-review is advisory; public checks and your explanation never replace independent acceptance."
)


COMPARISON_INSTRUCTIONS = (
    " Semantic-risk-v2 adds opt-in measured probe comparisons. An observation-only probe may print "
    "exactly one standalone line beginning with a newline and UPGRADE_WORKBENCH_PROBE_SAMPLE: "
    'followed by JSON {"input": <actual exercised input>, "output": <observed value or caught error>, '
    '"path_completed": true}. The record is bounded to 2048 UTF-8 bytes; use finite JSON values. '
    "Print from a collected test after the exercised operation; never invent a measurement. "
    "Catch an expected business exception only when it is the output under investigation; initialization "
    "or fixture failures are incomplete paths. Use the existing reviewed probe route and action schema. "
    "diagnostic_state.probe_comparisons binds the same probe/input to identified sources in the same "
    "new environment. candidate_vs_candidate compares two actually observed candidate revisions; "
    "direct_upgrade_vs_candidate only compares the direct upgrade with a patch and must not be "
    "described as comparing two alternative patches. same means this input did not distinguish these "
    "sources; different means it did, not which source is correct; incomplete establishes neither. "
    "Read the exact sources, outputs and right_is_current_source before citing a comparison. If useful, "
    "reuse the same reviewed probe ID after an independently justified candidate change so its input "
    "and code remain comparable. Do not edit a sound patch merely to produce a difference or manufacture "
    "an alternative known-bad implementation. A comparison is optional, not a finish gate. In your "
    "summary distinguish measured differences from source-based reasoning and untested input classes."
    " A numeric UPGRADE_WORKBENCH_MEASUREMENT record may include input and output alongside "
    "before, after and path_completed; this single record serves both the numeric oracle and "
    "sample comparison. Do not also emit a second sample record for the same operation. "
    "old_original_vs_candidate compares the same concrete input in the reviewed old and new "
    "dependency environments. Use it as a preservation reference only where the business "
    "contract requires preservation; a difference is not automatically a defect. "
    "For input-shaped requirements, derive typed partitions from the source and business contract: "
    "omitted keys, explicit null, representative legal values, and relevant invalid or collection "
    "boundaries. Do not equate nullable with optional or count accepted inputs without recording "
    "their resulting values. A small finite domain may be enumerated, but compare it with reasonable "
    "targeted boundary examples before using more execution resources. Record the same actual "
    "inputs and normalized business outputs in both environments; incidental error wording is not "
    "business behavior. Never catch import/setup failures as ordinary input rejection."
)


def instructions(value: str | None, output_format: str) -> str:
    validate_policy(value)
    if value is None or output_format not in {
        "diagnostic_actions", "contract_actions", "protocol_v6_actions"
    }:
        return ""
    return INSTRUCTIONS + (COMPARISON_INSTRUCTIONS if value == COMPARISON_POLICY else "")
