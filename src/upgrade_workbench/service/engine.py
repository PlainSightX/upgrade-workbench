"""图只编排持久命令；任务文件仍拥有候选、审阅和模型结果。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from filelock import FileLock, Timeout
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from psycopg import OperationalError

from ..cases import load_case
from ..generation.provider import provider_transport_route
from ..tasks import TERMINAL, advance_task, create_operation, finalize_task
from .config import ServiceLayoutPreflightError, Settings, identity
from .recovery import clear_stale_core_lock, owned_task, recover_task, version
from .store import Conflict, Store


class Flow(TypedDict, total=False):
    job_id: str
    command_id: str


class WorkerDatabaseError(RuntimeError):
    """保留故障位置，不把数据库错误文本或DSN交给普通输出。"""

    def __init__(self, error: OperationalError, *, phase: str, job_id: str | None = None):
        super().__init__("worker_database_unavailable")
        self.phase = phase
        self.job_id = job_id
        # 无SQLSTATE为连接/传输失败；已知连接或停机状态仅在纯轮询时有限重试。
        state = error.sqlstate
        unavailable = state is None or state.startswith("08") or state in {"57P01", "57P02", "57P03", "53300"}
        self.retryable = phase == "poll" and unavailable

    def public(self) -> dict:
        return {"status": "database_unavailable", "phase": self.phase,
                "job_id": self.job_id, "retryable": self.retryable,
                "next_action": "retry_poll" if self.retryable else "restore_database_then_reconcile_existing_command"}


class Engine:
    def __init__(self, settings: Settings, *, transport=None, comparator=None, hook=None):
        self.settings = settings
        self.store = Store(settings.database_url)
        self.transport = transport
        self.comparator = comparator
        self.hook = hook or (lambda *_: None)

    def setup(self) -> None:
        self.settings.validate()
        self.store.setup(str(self.settings.work_root))
        with PostgresSaver.from_conn_string(self.settings.database_url) as saver:
            saver.setup()

    @contextmanager
    def exclusive(self, job_id: str):
        root = self.settings.job_root(job_id)
        root.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(root / "worker.lock", timeout=0), self.store.job_lock(job_id):
                yield root
        except Timeout as error:
            raise Conflict("job_busy") from error

    def task_path(self, job_id: str) -> Path:
        path = self.store.job(job_id)["task_path"]
        if path is None:
            raise Conflict("job_initializing")
        return Path(path)

    def task(self, job_id: str) -> dict:
        path = self.task_path(job_id)
        task = owned_task(path, self.settings.job_root(job_id), job_id)
        if Path(task["budget_path"]).resolve() != self.settings.budget.resolve():
            raise ValueError("service_budget_binding_changed")
        return task

    def initialize(self, state: Flow) -> dict:
        job_id = state["job_id"]
        job = self.store.job(job_id)
        root = self.settings.job_root(job_id)
        if load_case(Path(job["request"]["manifest_path"])).fingerprint != job["request"]["case_fingerprint"]:
            raise ValueError("submitted_case_changed")
        if not job["task_path"]:
            # 先完成纯本地布局检查，再创建任务并绑定PG；失败不得触发模型或候选物化。
            self.settings.preflight_layout(job_id)
        if job["task_path"]:
            self.task(job_id)
            return {}
        # create后、PG绑定前终止时，从唯一属于本作业的原任务继续，不创建第二个任务。
        paths = list((root / "tasks").glob("*/task.json"))
        if len(paths) > 1:
            raise ValueError("ambiguous_job_task")
        if paths:
            task = owned_task(paths[0], root, job_id)
        else:
            if (root / "tasks").exists() and any((root / "tasks").iterdir()):
                raise ValueError("partial_task_creation_requires_reconciliation")
            request = job["request"]
            profile = dict(request["profile_settings"])
            route = profile.pop("provider_transport_route", "system")
            if route not in {"system", "process_direct"}:
                raise ValueError("invalid_provider_transport_route")
            task = create_operation(Path(request["manifest_path"]), root,
                                    budget_path=self.settings.budget, service_owner=job_id,
                                    **profile)
        self.hook("task_created", job_id)
        self.store.bind_task(job_id, task["task_path"])
        self.hook("task_bound", job_id)
        return {}

    def wait_command(self, state: Flow) -> dict:
        command_id = interrupt({"job_id": state["job_id"], "waiting_for": "explicit_command"})
        return {"command_id": identity(command_id)}

    def _result(self, job_id: str, *, recovered: bool = False) -> dict:
        task = self.task(job_id)
        return {"task_status": task["status"], "version": version(Path(task["task_path"])),
                "candidate_revision": (task.get("candidate") or {}).get("revision"),
                "final_evaluation": task["final_evaluation"], "recovered": recovered}

    def _apply(self, task: dict, request: dict, job_id: str) -> None:
        path = Path(task["task_path"])
        action = request["action"]
        options = {"execution_owner": job_id}
        if self.comparator is not None:
            options["comparator"] = self.comparator
        if action == "finalize":
            candidate = task.get("candidate")
            no_change = task.get("schema_version") in {3, 4, 5} and task["status"] == "no_change_claimed"
            if task["status"] not in TERMINAL or (not no_change and (not candidate or not candidate["reviewed"])):
                raise Conflict("finalization_requires_terminal_reviewed_candidate")
            finalize_task(path, **options)
        elif action == "diagnostic_retry":
            advance_task(path, diagnostic_request_id=request["request_id"], diagnostic_retry=True, **options)
        elif action == "diagnostic_review":
            pending = task.get("pending_diagnostic")
            if task["status"] != "pending_diagnostic_review" or not pending or pending["request"]["id"] != request["request_id"]:
                raise Conflict("stale_diagnostic_review")
            advance_task(path, diagnostic_request_id=request["request_id"],
                         diagnostic_decision=request["decision"], reviewer=request["reviewer"],
                         review_note=request["note"], review_only=True, **options)
        elif action == "review":
            if task["status"] != "pending_review":
                raise Conflict("review_not_pending")
            candidate = task["candidate"]
            if candidate["revision"] != request["revision"] or candidate["sha256"] != request["sha256"]:
                raise Conflict("stale_candidate_review")
            advance_task(path, review_only=True, reviewed_revision=request["revision"],
                         reviewed_sha256=request["sha256"], reviewer=request["reviewer"],
                         review_note=request["note"],
                         reject_reason="candidate_contract_rejected" if request["decision"] == "reject" else None,
                         **options)
        elif action == "advance":
            if task["status"] not in {"ready", "pending_dependency_query"}:
                raise Conflict("task_not_ready_for_advance")
            if task["status"] == "pending_dependency_query":
                # 查询已绑定锁环境和受限动作；此命令只执行查询，不请求模型。
                advance_task(path, **options)
                return
            needs_seed = task["seed_strategy"] == "official" and task["seed"] is None
            if needs_seed and not request["reviewed_tool"]:
                raise Conflict("static_tool_review_required")
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not needs_seed and not key.strip() and self.transport is None:
                raise Conflict("provider_key_not_configured")
            profile = self.store.job(job_id)["request"]["profile_settings"]
            route = profile.get("provider_transport_route", "system")
            endpoint = task["protocol"]["generation"]["endpoint"]
            with provider_transport_route(endpoint, route):
                advance_task(path, api_key=key or "offline-injected-transport", transport=self.transport,
                             reviewed_tool=request["reviewed_tool"], **options)
        else:
            raise Conflict("unsupported_command")

    def execute(self, state: Flow) -> dict:
        job_id, command_id = state["job_id"], state["command_id"]
        command = self.store.command(command_id, job_id)
        if command["status"] in {"completed", "blocked", "rejected"}:
            return {}
        try:
            task = self.task(job_id)
            path = Path(task["task_path"])
            root = self.settings.job_root(job_id)
            clear_stale_core_lock(path, job_id, root)
            if command["status"] == "running":
                # 图节点重进时只核对已有副作用；绝不重新调用_apply。
                adopt_dependency_report = False
                if version(path) == command["before_version"]:
                    # 查询报告先于task落盘；仅允许原advance命令走无执行能力的收据恢复。
                    pending_query = (
                        command["request"]["action"] == "advance"
                        and task.get("schema_version") == 5
                        and task["status"] == "pending_dependency_query"
                    )
                    if not pending_query:
                        raise ValueError("interrupted_command_without_proven_effect")
                    adopt_dependency_report = True
                recover_task(path, root, job_id, adopt_dependency_report=adopt_dependency_report)
                result = self._result(job_id, recovered=True)
            else:
                if command["request"]["expected_version"] != version(path):
                    raise Conflict("stale_task_version")
                self.store.start(command_id, version(path))
                self.hook("before_core", job_id)
                self._apply(task, command["request"], job_id)
                self.hook("after_core", job_id)
                result = self._result(job_id)
            self.store.finish(command_id, "completed", result)
            self.hook("after_command", job_id)
        except Conflict as error:
            self.store.finish(command_id, "rejected", {"reason": str(error)})
        except (ValueError, OSError, KeyError):
            # 异常文本可能含DSN/路径或不可信数据；服务只暴露固定类别。
            self.store.finish(command_id, "blocked", {"reason": "artifact_or_execution_requires_reconciliation"})
        return {}

    @contextmanager
    def graph(self):
        with PostgresSaver.from_conn_string(self.settings.database_url) as saver:
            saver.serde = JsonPlusSerializer(pickle_fallback=False)
            builder = StateGraph(Flow)
            builder.add_node("initialize", self.initialize)
            builder.add_node("wait_command", self.wait_command)
            builder.add_node("execute", self.execute)
            builder.add_edge(START, "initialize")
            builder.add_edge("initialize", "wait_command")
            builder.add_edge("wait_command", "execute")
            builder.add_edge("execute", "wait_command")
            yield builder.compile(checkpointer=saver)

    def step(self, job_id: str) -> str:
        """每次处理一个已登记命令；pause不杀死已经运行的请求。"""
        with self.exclusive(job_id), self.graph() as graph:
            job = self.store.job(job_id)
            if job["paused"] or job["blocked"]:
                return "paused_or_blocked"
            config = {"configurable": {"thread_id": job_id}}
            state = graph.get_state(config)
            if not state.values:
                graph.invoke({"job_id": job_id}, config, durability="sync")
                self.store.acknowledge_graph(job_id)
                return "initialized"
            interrupted = any(item.interrupts for item in state.tasks)
            if not interrupted:
                graph.invoke(None, config, durability="sync")
                self.store.acknowledge_graph(job_id)
                return "recovered_graph"
            pending = [c for c in self.store.commands(job_id) if c["status"] in {"queued", "running"}]
            if not pending:
                self.store.acknowledge_graph(job_id)
                return "waiting"
            graph.invoke(Command(resume=pending[0]["id"]), config, durability="sync")
            self.store.acknowledge_graph(job_id)
            return "command_processed"

    def run_once(self) -> list[dict]:
        results = []
        try:
            jobs = self.store.runnable()
        except OperationalError as error:
            raise WorkerDatabaseError(error, phase="poll") from error
        for job_id in jobs:
            try:
                try:
                    status = self.step(job_id)
                except Conflict:
                    status = "busy"
                except ServiceLayoutPreflightError as error:
                    self.store.block(job_id, error.code)
                    status = "blocked"
                except (ValueError, OSError, KeyError):
                    self.store.block(job_id, "initialization_or_graph_requires_reconciliation")
                    status = "blocked"
            except OperationalError as error:
                # PG可能已提交start/finish或保存图节点；此处不重发、不强写blocked。
                # 数据库恢复后，原running命令由execute核对收据/版本，不能直接重跑_apply。
                raise WorkerDatabaseError(error, phase="job", job_id=job_id) from error
            results.append({"job_id": job_id, "status": status})
        return results

    def public_job(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        result = {key: job[key] for key in ("id", "paused", "blocked", "created_at")}
        result.update(case_id=job["request"]["case_id"], profile=job["request"]["profile"], status="initializing")
        if not job["task_path"]:
            return result
        before = version(Path(job["task_path"]))
        task = self.task(job_id)
        if version(Path(job["task_path"])) != before:
            raise Conflict("task_changed_during_read_retry")
        candidate = task.get("candidate")
        result.update(status=task["status"], task_id=task["task_id"], version=before,
                      calls=len(task["attempts"]), final_evaluation=task["final_evaluation"],
                      stop_reason=task.get("stop_reason"), terminal=task["status"] in TERMINAL,
                      candidate={key: candidate[key] for key in ("revision", "sha256", "reviewed", "origin")} if candidate else None)
        if task.get("schema_version") in {3, 4, 5}:
            from ..diagnostic_state import diagnostic_execution_summary

            # 只读状态接口不写新的context产物；公共摘要不含宿主路径或探针代码。
            pending = task.get("pending_diagnostic")
            result.update(pending_diagnostic_id=pending["request"]["id"] if pending else None,
                          diagnostic_runs=len(task["diagnostic_runs"]), observations=len(task["observations"]),
                          diagnostics=diagnostic_execution_summary(task),
                          finish=task.get("finish"), final_result_status=(task.get("final_result") or {}).get("status"),
                          verification_subject={k: v for k, v in task.get("verification_subject", {}).items()
                                                if k != "source_reference"})
        if task.get("schema_version") == 5:
            pending_query = task.get("pending_dependency_query")
            result.update(
                workflow_profile=task["protocol"].get("workflow_profile", "workbench"),
                pending_dependency_query_id=(
                    pending_query["request"]["id"] if pending_query else None
                ),
                dependency_queries=len(task.get("dependency_queries", [])),
                dependency_facts=len(task.get("dependency_facts", [])),
                investigator_handoffs=len(task.get("investigator_handoffs", [])),
            )
        return result
