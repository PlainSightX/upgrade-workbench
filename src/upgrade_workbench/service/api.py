"""本机认证API：受理命令与执行命令分离，HTTP断开不会取消或重发模型请求。"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from ..cases import load_case
from .config import Settings, identity
from .engine import Engine
from .recovery import read_bound, version
from .store import Conflict

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Key = Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Submission(Input):
    case_id: str = Field(min_length=1, max_length=128)
    profile: str = Field(min_length=1, max_length=128)


class Advance(Input):
    expected_version: Digest
    reviewed_tool: bool = False


class Review(Input):
    expected_version: Digest
    revision: Digest
    sha256: Digest
    decision: Literal["accept", "reject"]
    reviewer: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=2000)


class Finalize(Input):
    expected_version: Digest


class DiagnosticReview(Input):
    expected_version: Digest
    request_id: Digest
    decision: Literal["accept", "reject"]
    reviewer: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=2000)


class DiagnosticRetry(Input):
    expected_version: Digest
    request_id: Digest


class Pause(Input):
    paused: bool


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
    engine = engine or Engine(settings)
    security = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials, settings.token):
            raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="Upgrade Workbench", version="0.1", dependencies=[Depends(authorize)],
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, error: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _error: KeyError):
        return JSONResponse(status_code=404, content={"detail": "not_found"})

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, _error: ValueError):
        return JSONResponse(status_code=409, content={"detail": "artifact_integrity_or_identity_conflict"})

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # 浏览器网页不能借localhost替用户排队；Bearer认证不能由cookie替代。
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"} or request.headers.get("origin"):
            return JSONResponse(status_code=403, content={"detail": "local_api_only"})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16_384:
                return JSONResponse(status_code=413, content={"detail": "request_too_large"})
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/health")
    def health():
        with engine.store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ready", "execution": "separate_worker"}

    @app.get("/registry")
    def registry():
        return {"cases": sorted(settings.cases), "profiles": sorted(settings.profiles)}

    @app.post("/jobs", status_code=202)
    def submit(body: Submission, idempotency_key: Key):
        if body.case_id not in settings.cases or body.profile not in settings.profiles:
            raise HTTPException(422, "unregistered_case_or_profile")
        request = body.model_dump() | {"manifest_path": str(settings.cases[body.case_id]),
                                       "case_fingerprint": load_case(settings.cases[body.case_id]).fingerprint,
                                       "profile_settings": settings.profiles[body.profile]}
        job = engine.store.submit(idempotency_key, request)
        return engine.public_job(job["id"])

    @app.get("/jobs/{job_id}")
    def job(job_id: str):
        return engine.public_job(identity(job_id))

    @app.get("/jobs")
    def jobs():
        return [engine.public_job(job_id) for job_id in engine.store.jobs()]

    def enqueue(job_id: str, key: str, request: dict):
        identity(job_id)
        # 重复键的返回优先于陈旧版本检查：重试同一HTTP请求应拿回已有命令。
        for existing in engine.store.commands(job_id):
            if existing["idempotency_key"] == key:
                if existing["request"] != request:
                    raise Conflict("idempotency_key_conflict")
                return command(job_id, existing["id"])
        task = engine.task(job_id)
        if request["expected_version"] != version(Path(task["task_path"])):
            raise Conflict("stale_task_version")
        item = engine.store.enqueue(job_id, key, request)
        return command(job_id, item["id"])

    @app.post("/jobs/{job_id}/advance", status_code=202)
    def advance(job_id: str, body: Advance, idempotency_key: Key):
        return enqueue(job_id, idempotency_key, body.model_dump() | {"action": "advance"})

    @app.post("/jobs/{job_id}/review", status_code=202)
    def review(job_id: str, body: Review, idempotency_key: Key):
        if not body.reviewer.strip() or not body.note.strip():
            raise HTTPException(422, "review_identity_and_note_required")
        return enqueue(job_id, idempotency_key, body.model_dump() | {"action": "review"})

    @app.post("/jobs/{job_id}/finalize", status_code=202)
    def finalize(job_id: str, body: Finalize, idempotency_key: Key):
        return enqueue(job_id, idempotency_key, body.model_dump() | {"action": "finalize"})

    @app.post("/jobs/{job_id}/diagnostic-review", status_code=202)
    def diagnostic_review(job_id: str, body: DiagnosticReview, idempotency_key: Key):
        if not body.reviewer.strip() or not body.note.strip():
            raise HTTPException(422, "review_identity_and_note_required")
        return enqueue(job_id, idempotency_key, body.model_dump() | {"action": "diagnostic_review"})

    @app.post("/jobs/{job_id}/diagnostic-retry", status_code=202)
    def diagnostic_retry(job_id: str, body: DiagnosticRetry, idempotency_key: Key):
        return enqueue(job_id, idempotency_key, body.model_dump() | {"action": "diagnostic_retry"})

    @app.post("/jobs/{job_id}/pause")
    def pause(job_id: str, body: Pause):
        engine.store.pause(identity(job_id), body.paused)
        return engine.public_job(job_id)

    @app.get("/jobs/{job_id}/commands")
    def commands(job_id: str):
        engine.store.job(identity(job_id))
        return [command(job_id, item["id"]) for item in engine.store.commands(job_id)]

    @app.get("/jobs/{job_id}/commands/{command_id}")
    def command(job_id: str, command_id: str):
        item = engine.store.command(identity(command_id), identity(job_id))
        return {key: item[key] for key in ("id", "job_id", "status", "request", "result", "created_at", "finished_at")}

    @app.get("/jobs/{job_id}/diff", response_class=PlainTextResponse)
    def diff(job_id: str):
        task = engine.task(identity(job_id))
        if not task.get("candidate"):
            return ""
        return read_bound(Path(task["candidate"]["patch_path"]), settings.job_root(job_id)).decode("utf-8")

    @app.get("/jobs/{job_id}/diagnostic")
    def diagnostic(job_id: str):
        from ..diagnostic_state import diagnostic_execution_summary
        from ..diagnostics import read

        task = engine.task(identity(job_id))
        pending = task.get("pending_diagnostic")
        if not pending:
            if task.get("diagnostic_runs"):
                return {"request_id": None, "diagnostics": diagnostic_execution_summary(task)}
            raise Conflict("diagnostic_not_pending")
        root = Path(task["task_path"]).parent
        request = read(pending["request"], root)
        probe = next((p for p in task["probes"] if p["id"] == request["probe_id"]), None)
        return {"request_id": pending["request"]["id"],
                "subject": {k: request[k] for k in ("revision", "patch_sha256", "kind", "probe_id")},
                "probe": read(probe, root) if probe else None,
                "diagnostics": diagnostic_execution_summary(task)}

    @app.get("/jobs/{job_id}/report")
    def report(job_id: str):
        task = engine.task(identity(job_id))
        final = task.get("final_result")
        if not final:
            raise Conflict("final_result_not_available")
        raw = read_bound(Path(final["path"]), settings.job_root(job_id))
        if hashlib.sha256(raw).hexdigest() != final["sha256"]:
            raise ValueError("final_result_changed")
        return json.loads(raw)

    @app.get("/openapi.json")
    def schema():
        return app.openapi()

    return app
