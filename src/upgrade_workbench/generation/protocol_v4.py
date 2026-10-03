"""诊断动作协议：模型表达请求，宿主核验身份并决定何时允许执行。"""

from __future__ import annotations

import ast
import re

from ..diagnostic_oracles import validate_oracle

FORMAT = "diagnostic_actions"
DIAGNOSTIC_ACTIONS = {"run_public_checks", "propose_probe", "revise_probe", "run_probe"}
DIAGNOSTIC_POLICY = {"max_runs": 4, "max_probes": 2, "max_revisions_per_probe": 2,
                     "max_observation_revisits": 4}
DIGEST = re.compile(r"[0-9a-f]{64}")


def _has_pytest_entrypoint(tree: ast.Module) -> bool:
    """探针必须由 pytest 调用，不能把收集阶段的顶层副作用冒充完成执行。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("test_")
                for item in node.body
            ):
                return True
    return False


def _text(value, maximum=1000, *, field="text"):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        length = len(value) if isinstance(value, str) else None
        raise ValueError(f"Invalid {field}; action not executed. Expected nonempty text without NUL, "
                         f"maximum {maximum} characters; actual_type={type(value).__name__}, length={length}. "
                         "Shorten or correct this field; do not resend the unchanged value.")


def evidence_refs(value, *, allow_facts=False, maximum=8):
    if not isinstance(value, list) or len(value) > maximum:
        count = len(value) if isinstance(value, list) else None
        limit = "eight" if maximum == 8 else str(maximum)
        raise ValueError(f"Invalid evidence_refs; action not executed. At most {limit} public evidence references; actual_count={count}")
    for index, ref in enumerate(value):
        _text(ref, 256, field=f"evidence_refs[{index}]")
        pattern = r"(?:source|version|observation):[^\s]+|business_contract"
        if allow_facts:
            pattern += r"|fact:[0-9a-f]{64}"
        if not re.fullmatch(pattern, ref):
            fact_hint = " or fact:<existing dependency fact ID>" if allow_facts else ""
            raise ValueError("Use business_contract, source:path, version:key or observation:id" + fact_hint)


def _contract_coverage(value, *, allow_facts=False):
    """要求模型显式区分合同内缺口与合同外限制，避免从自由文本猜退出语义。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        count = len(value) if isinstance(value, list) else None
        raise ValueError(f"Invalid contract_coverage; expected one to eight declarations; actual_count={count}")
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "requirement", "scope", "status", "evidence_refs", "tool_limitation"
        }:
            raise ValueError(f"Invalid contract_coverage[{index}] fields")
        _text(item["requirement"], 500, field=f"contract_coverage[{index}].requirement")
        identity = item["requirement"].strip().casefold()
        if identity in seen:
            raise ValueError("Duplicate contract_coverage requirement")
        seen.add(identity)
        if item["scope"] not in {"in_contract", "outside_contract"}:
            raise ValueError("contract_coverage.scope must be in_contract or outside_contract")
        if item["status"] not in {"verified", "unverified"}:
            raise ValueError("contract_coverage.status must be verified or unverified")
        evidence_refs(item["evidence_refs"], allow_facts=allow_facts)
        limitation = item["tool_limitation"]
        if limitation is not None:
            _text(limitation, 500, field=f"contract_coverage[{index}].tool_limitation")
        if item["status"] == "verified" and (not item["evidence_refs"] or limitation is not None):
            raise ValueError("Verified contract coverage needs evidence_refs and cannot declare a tool limitation")
        if item["scope"] == "outside_contract" and (item["status"] != "unverified" or limitation is not None):
            raise ValueError("Outside-contract coverage must be unverified without a tool limitation")
        if limitation is not None and not (item["scope"] == "in_contract" and item["status"] == "unverified"):
            raise ValueError("tool_limitation only applies to an unverified in-contract requirement")


def validate(case, value, snapshot, *, allow_facts=False, project_context_policy=None,
             role="solver", preparation_policy=None, preparation_phase=None):
    from .actions import AgentActionError, action_field_error_code, validate_action
    from .edits import EditValidationError
    from .roles import RoleContractError, require_preparation_action

    try:
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ValueError("One typed action is required")
        require_preparation_action(preparation_policy, role=role, phase=preparation_phase,
                                   action_type=value["type"])
        action = dict(value)
        issues = action.pop("issues", None)
        if issues is not None:
            if not isinstance(issues, list) or len(issues) > 4:
                raise ValueError("At most four diagnostic issues")
            for issue in issues:
                if not isinstance(issue, dict) or set(issue) != {"hypothesis", "evidence_refs", "unknown", "next_observation"}:
                    raise ValueError("Invalid diagnostic issue")
                for key in ("hypothesis", "unknown", "next_observation"):
                    _text(issue[key], 1000, field=f"issues.{key}")
                evidence_refs(issue["evidence_refs"], allow_facts=allow_facts)
        kind = action["type"]
        from .project_context import ACTIONS
        from .project_context import validate_action as validate_knowledge

        if kind in ACTIONS:
            validate_knowledge(case, snapshot, action, project_context_policy, role=role,
                               preparation_policy=preparation_policy, preparation_phase=preparation_phase)
            return value
        required = {
            "submit_candidate": {"type", "base_revision", "edits"},
            "list_sources": {"type", "revision"},
            "outline_source": {"type", "revision", "path"},
            "search_source": {"type", "revision", "query"},
            "read_source": {"type", "revision", "path", "start_line", "end_line"},
            "get_observation": {"type", "observation_id"},
            "run_public_checks": {"type", "revision"},
            "propose_probe": {"type", "revision", "code", "purpose", "expected_observation", "evidence_refs", "oracle"},
            "revise_probe": {"type", "revision", "parent_probe_id", "revision_reason", "code", "purpose",
                             "expected_observation", "evidence_refs", "oracle"},
            "run_probe": {"type", "revision", "probe_id"},
            "finish": {"type", "reason", "explanation", "evidence_refs", "contract_coverage"},
        }
        optional = {
            "submit_candidate": set(),
            "list_sources": {"view", "prefix", "cursor"},
            "outline_source": {"view", "cursor"},
            "search_source": {"view", "paths", "scope", "cursor", "max_results"},
            "read_source": {"view"}, "get_observation": {"cursor"},
            "run_public_checks": {"view"}, "propose_probe": set(), "revise_probe": set(),
            "run_probe": set(), "finish": set(),
        }
        if kind not in required:
            raise AgentActionError(
                f"Unknown action type {kind[:80]!r}; action not executed. "
                f"Supported types: {', '.join(sorted(required))}"
            )
        missing = sorted(required[kind] - set(action))
        extra = sorted(str(key)[:80] for key in set(action) - required[kind] - optional[kind])
        if missing or extra:
            oracle_hint = (
                " Probe oracle is required: use a numeric oracle object or explicit null for observation only."
                if kind in {"propose_probe", "revise_probe"} and "oracle" in missing
                else ""
            )
            raise AgentActionError(
                f"Invalid {kind} fields; action not executed. missing={missing}; "
                f"extra={extra[:5]}; required={sorted(required[kind])}; "
                f"optional={sorted(optional[kind] | {'issues'})}.{oracle_hint}",
                code=action_field_error_code(extra),
            )
        if kind == "submit_candidate":
            validate_action(case, action, candidate=snapshot)
            return value
        if "revision" in action and not (isinstance(action["revision"], str) and DIGEST.fullmatch(action["revision"])):
            raise ValueError("Source revision must be an exact SHA256")
        if action.get("view", "current") not in {"original", "current"}:
            raise ValueError("View must be original or current")
        if "cursor" in action and action["cursor"] is not None:
            _text(action["cursor"], 150, field=f"{kind}.cursor")
        if kind == "list_sources":
            prefix = action.get("prefix", "")
            if not isinstance(prefix, str) or len(prefix) > 256 or ".." in prefix or "\\" in prefix:
                raise ValueError("Invalid source prefix")
        elif kind == "read_source":
            # 沿用含首尾计数的明确纠错信息。
            validate_action(case, {k: v for k, v in action.items() if k != "revision"}, candidate=snapshot)
        elif kind == "outline_source":
            if action["path"] not in snapshot.files:
                raise AgentActionError("Path is outside registered source")
        elif kind == "search_source":
            _text(action["query"], 200, field="search_source.query")
            paths = action.get("paths")
            if (paths is None) == (action.get("scope") != "repository"):
                raise ValueError("Select explicit paths OR scope=repository")
            if paths is not None:
                if not isinstance(paths, list) or not paths or len(paths) > 128:
                    raise ValueError("Invalid public search paths")
                if any(not isinstance(path, str) for path in paths):
                    raise ValueError("Invalid public search paths")
                if any(path not in snapshot.files for path in paths):
                    raise AgentActionError("Search path is outside registered source")
                if len(paths) != len(set(paths)):
                    raise ValueError("Invalid public search paths")
            maximum = action.get("max_results", 50)
            if type(maximum) is not int or not 1 <= maximum <= 50:
                raise ValueError("Search returns at most 50 results per page")
        elif kind in {"propose_probe", "revise_probe"}:
            if action["oracle"] is not None:
                validate_oracle(action["oracle"])
            _text(action["code"], 12_000, field=f"{kind}.code")
            if len(action["code"].encode()) > 12_000:
                raise ValueError("Probe exceeds 12000 bytes")
            tree = ast.parse(action["code"])
            if not _has_pytest_entrypoint(tree):
                raise ValueError(
                    "Probe must define at least one pytest-collected test_* function or Test* method; "
                    "top-level collection side effects are not a completed diagnostic execution"
                )
            _text(action["purpose"], field=f"{kind}.purpose")
            _text(action["expected_observation"], field=f"{kind}.expected_observation")
            evidence_refs(action["evidence_refs"], allow_facts=allow_facts)
            if not action["evidence_refs"]:
                raise ValueError("Probe needs public evidence references")
            if kind == "revise_probe":
                if not isinstance(action["parent_probe_id"], str) or not DIGEST.fullmatch(action["parent_probe_id"]):
                    raise ValueError("Use an exact parent probe ID")
                _text(action["revision_reason"], field="revise_probe.revision_reason")
        elif kind in {"run_probe", "get_observation"}:
            key = "probe_id" if kind == "run_probe" else "observation_id"
            if not isinstance(action[key], str) or not DIGEST.fullmatch(action[key]):
                raise ValueError("Use an exact public artifact ID")
            if kind == "get_observation" and action.get("cursor") is not None:
                _text(action["cursor"], 150, field="get_observation.cursor")
        elif kind == "finish":
            if action["reason"] not in {"candidate_ready", "no_change_claimed", "unresolved"}:
                raise ValueError("Invalid finish reason")
            _text(action["explanation"], 2000, field="finish.explanation")
            evidence_refs(action["evidence_refs"], allow_facts=allow_facts)
            _contract_coverage(action["contract_coverage"], allow_facts=allow_facts)
        return value
    except AgentActionError:
        raise
    except RoleContractError as error:
        raise AgentActionError(str(error), code="role_write_forbidden") from error
    except EditValidationError as error:
        raise AgentActionError(str(error)[:1000], code=error.code) from error
    except (ValueError, KeyError, TypeError, SyntaxError) as error:
        raise AgentActionError(str(error)[:1000], code="action_invalid_fields") from error


def json_instructions() -> str:
    return (
        'Use strict JSON, not Python literals or Markdown. Escape quotes, backslashes and newlines '
        'inside string values. Line numbers are integers, not quoted text. '
        r'Valid escaped text: {"summary":"Read the \"answer order\" flow","action":{"type":"list_sources","revision":"<supplied SHA256>"}}. '
        'For example, a citation is {"path":"package/file.py","start_line":1,"end_line":20}. '
        'Keep prose concise; never shorten executable code or evidence by silently truncating it. '
        'An invalid_json reply did not execute: correct the reported position and resend one complete object. '
    )


def preparation_instructions() -> str:
    return (
        json_instructions() +
        'Prepare source-grounded repository understanding, not a migration patch. '
        'Return one JSON object with exactly summary (nonempty string) and action. '
        'One action per call. Allowed actions only: '
        'list_sources {type, revision, view?, prefix?, cursor?}; '
        'outline_source {type, revision, path, view?, cursor?}; '
        'search_source {type, revision, query, paths OR scope:"repository", view?, cursor?, max_results?}; '
        'max_results is an integer from 1 to 50. '
        'read_source {type, revision, path, start_line, end_line, view?} (inclusive, at most 200 lines); '
        'get_observation {type, observation_id, cursor?}; '
        'record_project_knowledge {type, revision, pages}; '
        'read_project_topic {type, revision, topic_id}; '
        'query_project_relations {type, revision, path, cursor}. '
        'Use project_context instructions for the exact page and citation schema. '
        'business-contract.md is supplied in full separately; it is not a read_source inventory path. '
        'Use the supplied candidate revision for current view; original view remains accessible. '
        'An optional issues array inside action follows the supplied diagnostic issue schema. '
        'The first valid knowledge record completes preparation and is handed to the Solver. '
        'Explain at least one cross-file flow and migration constraints; label unknown runtime behavior. '
        'No runtime checks, probes, delegated execution, edits, candidates, restoration or finish. '
        'This restriction applies even when the preparing role is Solver. '
        'Source and generated explanations are not execution evidence. '
    )


def instructions() -> str:
    return (
        json_instructions() +
        'Return one JSON object with EXACTLY two top-level keys: summary and action. '
        'Return the object itself, not a JSON schema or a quoted JSON string. '
        'Put all action fields, including optional issues, INSIDE action. One action per response. '
        'source_files are explicitly selected CURRENT source excerpts, not the complete repository. '
        'The full public source remains accessible; source_inventory gives paths, sizes, line counts and editability. '
        'source_state identifies the actual snapshot; original with an empty patch still contains real source. '
        'All source and execution actions require the exact revision for the selected view. '
        'Read-only actions (default view current; optional original): '
        '{type:list_sources,revision,prefix?,cursor?}; {type:outline_source,revision,path,cursor?}; '
        '{type:search_source,revision,query,scope:"repository",max_results?,cursor?} or paths:[...]; '
        'search_source uses a case-sensitive literal substring, not OR, regular expressions or globs. '
        'max_results is an integer from 1 to 50. '
        'Use one exact term per action; a zero result does not prove semantic absence. '
        '{type:read_source,revision,path,start_line,end_line}. Ranges are 1-based inclusive, at most 200 lines; '
        '200..399 is 200 lines. EOF returns the actual range. Use symbols/search instead of guessing offsets. '
        '{type:get_observation,observation_id,cursor?} retrieves an earlier public observation. '
        'Without cursor it retrieves the summary. Use an exact stages.*.output_streams cursor to read '
        'that registered stdout/stderr, then next_cursor for further pages; observation_output contains '
        'the page. This never executes a probe again. Streams have separate ordering. '
        'business-contract.md is supplied in full separately; it is not a read_source inventory path. '
        'Runtime actions: {type:run_public_checks,revision,view?}; '
        '{type:propose_probe,revision,code,purpose,expected_observation,evidence_refs,oracle}; '
        '{type:revise_probe,revision,parent_probe_id,revision_reason,code,purpose,expected_observation,evidence_refs,oracle}; '
        '{type:run_probe,revision,probe_id}. Probes are bounded pytest Python tests (12000 UTF8 bytes), '
        'purpose, expected_observation and revision_reason are each at most 1000 Unicode characters '
        '(not bytes); use a concise path, observation and limitation, not an exhaustive essay. '
        'not shell commands; only existing dependencies. Define at least one pytest-collected test_* function '
        'or Test* method; top-level collection side effects do not count as completed execution. '
        'A reviewed probe runs in isolated old/new environments. '
        'They are diagnostic hypotheses, never independent acceptance tests. No network, host files, hidden checks, '
        'test-framework monkeypatching or dependency installation. Use print/assert to observe behavior. '
        'If execution_environment.probe_scaffold is present, use its listed fixtures and follow its usage text; '
        'write the business observation body only. Do not recreate the application, replace its session, or guess '
        'fixture/bootstrap behavior already supplied by the reviewed scaffold. '
        'The oracle key is required. Use explicit oracle:null for an observation-only probe: purpose and '
        'expected_observation must describe the actual local path/object and what remains unknown. '
        'Expected output is a hypothesis, not an assertion of business correctness. Such probes may print '
        'values, types or exceptions without a numeric marker; even passing pytest cannot establish a '
        'business predicate. Do not invent an absolute threshold just to obtain an observation. '
        'For a measured business predicate, oracle must contain requirement (public business constraint), subject (what is measured), exercise '
        '(exact path and observation point), basis (absolute/delta/version_delta), operator (eq/le/ge), '
        'and expected (fixed finite number justified by public evidence). absolute compares after to expected; '
        'delta compares after-before; version_delta compares new after minus old after. '
        'Use absolute for an absolute requirement; a baseline may already be wrong. Do not weaken the requirement '
        'to get a passing probe. A comparison across versions is not a correctness oracle for either version. '
        'For a numeric oracle, each environment must print exactly one standalone line, starting with a newline to avoid pytest progress text: '
        'print("\\nUPGRADE_WORKBENCH_MEASUREMENT:" + json.dumps({"before": before, "after": after, "path_completed": True})). '
        'before/after must be finite numbers read from the tested behavior, not constants guessed from expectations. '
        'Emit only after exercising the declared path and before cleanup that would mask the observation; '
        'do not put path_completed=True in finally or assert the predicate before emitting the values. '
        'For a complex condition, measure the count of violations. Setup/collection failures are reported as '
        'setup_failed and missing measurements are inconclusive; neither is evidence that the business hypothesis '
        'is false. Correct an eligible probe rather than treating framework initialization as a business result. '
        'The host computes predicate_satisfied independently of pytest status. no_counterexample_observed means only '
        'this path/object/threshold; it does NOT disprove a general hypothesis. Read all stage measurements and scope_limit. '
        'For PostgreSQL cases, UPGRADE_WORKBENCH_PG_DSN is a libpq keyword/value DSN, NOT a SQLAlchemy URL. '
        'Use it with psycopg2.connect; for SQLAlchemy use psycopg2.extensions.parse_dsn and '
        'sqlalchemy.engine.URL.create, mapping dbname to database and other options appropriately. '
        'The DSN belongs only to the disposable database for this execution. '
        'Evidence refs use business_contract, source:<exact path from source_inventory>, '
        'version:<exact evidence_key from version_evidence>, observation:<exact ID>. '
        'Do not prepend registered/ or source/ to a source_inventory path. Do not invent evidence keys. '
        'If a runtime observation is needed and no execution blocker is known, request that observation using '
        'run_public_checks or propose_probe before concluding that information is unavailable. '
        'Example: {"summary":"Check registered public behavior","action":{"type":"run_public_checks",'
        '"revision":"replace with source_state.revision"}}. '
        'Submit incremental edits using {type:submit_candidate,base_revision,edits:[{path,old,new}]}; '
        'each old string must occur once in CURRENT source. Only allowed_changes, at most 32 edits. '
        'A candidate is a proposed repair for review and testing, not a claim that all requirements are proven. '
        'Concrete source/version evidence may justify a bounded edit even before a discriminating probe succeeds; '
        'state its rationale and remaining uncertainty, and do not edit merely to force progress. '
        'remaining_diagnostic_runs limits explicit public-check/probe requests, NOT submit_candidate or the '
        'automatic public feedback performed after candidate review. With calls remaining, exhausted probe '
        'budgets alone do not close that repair path. Candidate feedback still runs in the reviewed executor. '
        'Passing partial checks does not negate a concrete source-level contract violation. '
        'Known measured counterexamples still require their existing repair-and-rerun contract; '
        'these permissions do not clear them or replace independent final acceptance. '
        'Any action may include issues:[{hypothesis,evidence_refs,unknown,next_observation}] (at most 4); '
        'keep these short, externally checkable statements, not internal reasoning. '
        'Example with an issue: {"summary":"Check public behavior","action":{"type":"run_public_checks",'
        '"revision":"replace with source_state.revision","issues":[{"hypothesis":"a behavior may differ",'
        '"evidence_refs":["business_contract"],"unknown":"runtime outcome",'
        '"next_observation":"old/new public checks"}]}}. '
        'protocol_feedback code diagnostic_issues_outside_action means the response did not execute: '
        'move issues from the top level into action. Do not drop the action or resend the old object. '
        'Preserve the business contract, including error paths. Public checks can all pass while defects remain; '
        'use source/version evidence and an independently reasoned probe when existing checks cannot distinguish a hypothesis. '
        'Each rejected action did not execute; follow edit_feedback, not an earlier successful result. '
        'Finish with {type:finish,reason,explanation,evidence_refs,contract_coverage}; explanation is at most 2000 characters, '
        'evidence_refs at most eight entries. reason is candidate_ready (real reviewed edits '
        'with current public checks passed), no_change_claimed (unchanged source with public checks passed), '
        'or unresolved (remaining uncertainty or exhausted progress). Finish is never acceptance. '
        'contract_coverage contains one to eight declarations with EXACT fields '
        '{requirement,scope,status,evidence_refs,tool_limitation}. scope is in_contract or outside_contract; '
        'status is verified or unverified. This legacy status is a model support declaration, NOT a host '
        'certificate of execution or acceptance. A verified item needs public evidence_refs. In explanation, '
        'explicitly distinguish source inference from actual current-revision execution; unchanged source '
        'does not prove unchanged behavior. Cite only relevant observations in each item; an observation '
        'ID may denote a source read, stale run or observation-only probe, not a passed business predicate. '
        'The host separately records evidence kinds, execution scopes and freshness without inferring '
        'semantic coverage from prose. Independent final acceptance remains separate. Use tool_limitation only '
        'for a concrete unverified in-contract requirement that the available reviewed tools cannot observe; '
        'do not invent a limitation merely to exit. An unverified in-contract item blocks candidate_ready and '
        'no_change_claimed. It also blocks unresolved while remaining_diagnostic_runs and at least one new, '
        'revisable or reusable reviewed probe route remain. Outside-contract limitations do not force more work. '
        'No-change claims require separate final verification; never force pointless edits. '
        'A current counterexample blocks candidate_ready/no_change_claimed even when referenced or pytest passed. '
        'Investigate its source, submit justified edits and rerun the relevant probe on the new revision, or finish unresolved. '
        'remaining_probes counts NEW root hypotheses (including rejected proposals), NOT runs or bounded revisions. '
        'revise_probe corrects a rejected or inconclusive latest probe while preserving its history. '
        'For numeric-to-numeric revision, copy oracle requirement, subject, basis, operator and expected exactly; '
        'only exercise may change. An eligible rejected/inconclusive numeric probe may instead narrow to '
        'oracle:null with corrected code and an honest local scope. unestablished_oracles preserves its original '
        'unproven requirement; observation-only success cannot prove that requirement. Rejected/failed '
        'observation-only probes may be revised with oracle:null; a new numeric predicate needs a new root. '
        'Explain the correction in revision_reason and provide the complete corrected code. Each root allows at most '
        'two revisions. Inspect can_revise, remaining_revisions and diagnostic_options before proposing. '
        'A measured counterexample cannot be revised away; repair source and rerun that probe or finish unresolved. '
        'Even at remaining_probes=0, run_probe can reuse an existing probe ID on the current revision when diagnostic '
        'runs remain. probes includes full code and last_review_decision; inspect these before reuse. '
        'A prior rejection is not permission to run rejected code unchanged; each execution remains subject to review. '
        'review_history preserves exact public review reasons across navigation. recent_actions records action outcomes; '
        'model_summary is only a model claim, not execution evidence. runtime_findings keeps earlier runtime results '
        'visible even when observations contains newer source reads. A stale/inconclusive result is not a pass. '
        'Before a successful finish, reference and explain current observation-only probe results too; '
        'state their limits rather than treating them as business acceptance. The host retains those limits '
        'and unestablished_oracles in the finish record. Availability of more calls does not oblige more calls, '
        'but do not claim a budget is exhausted when its remaining counter is positive. '
        'last_diagnostic_reused=true means a stored result was returned, not a new execution. '
        'Repeatedly retrieving already seen observations does not verify again; progress.warning flags this pattern. '
        'After four consecutive revisits with no new observation, source revision or diagnostic request, the host '
        'stops unresolved. Request genuinely new evidence or state what remains unknown, never force a success claim. '
        'Old observations are explicitly stale on new revisions. Respect remaining call/diagnostic limits. '
    )
