"""可选的版本语义自查；改变调查提示，不授予验收权限或增加退出门禁。"""

from __future__ import annotations

POLICY = "semantic-risk-v1"


def validate_policy(value: str | None) -> str | None:
    if value is not None and value != POLICY:
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


def instructions(value: str | None, output_format: str) -> str:
    validate_policy(value)
    return INSTRUCTIONS if value is not None and output_format in {
        "diagnostic_actions", "contract_actions", "protocol_v6_actions"
    } else ""
