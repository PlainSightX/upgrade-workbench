"""PG拥有服务命令和幂等身份，不复制候选或费用事实。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class Conflict(ValueError):
    """同键异内容、陈旧状态或归属冲突。"""


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as db:
            yield db

    def setup(self, work_root: str) -> None:
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(782193401)")
            db.execute("CREATE TABLE IF NOT EXISTS uw_service_meta (id int PRIMARY KEY, version int NOT NULL, root text NOT NULL)")
            db.execute("INSERT INTO uw_service_meta VALUES (1,1,%s) ON CONFLICT DO NOTHING", (work_root,))
            if db.execute("SELECT version,root FROM uw_service_meta WHERE id=1").fetchone() != {"version": 1, "root": work_root}:
                raise Conflict("service_storage_binding_changed")
            db.execute("""CREATE TABLE IF NOT EXISTS uw_jobs (
                id text PRIMARY KEY, idempotency_key text UNIQUE NOT NULL, request_hash text NOT NULL,
                request jsonb NOT NULL, task_path text, paused boolean NOT NULL DEFAULT false,
                blocked text, created_at timestamptz NOT NULL DEFAULT now())""")
            db.execute("""CREATE TABLE IF NOT EXISTS uw_commands (
                id text PRIMARY KEY, job_id text NOT NULL REFERENCES uw_jobs(id),
                idempotency_key text NOT NULL, request_hash text NOT NULL, request jsonb NOT NULL,
                status text NOT NULL DEFAULT 'queued', result jsonb, before_version text,
                created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
                finished_at timestamptz, UNIQUE(job_id,idempotency_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS uw_commands_ready ON uw_commands(job_id,status,created_at)")
            db.execute("ALTER TABLE uw_commands ADD COLUMN IF NOT EXISTS graph_ack boolean NOT NULL DEFAULT false")
            db.execute("ALTER TABLE uw_jobs ADD COLUMN IF NOT EXISTS graph_ready boolean NOT NULL DEFAULT false")

    def submit(self, key: str, request: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT INTO uw_jobs(id,idempotency_key,request_hash,request) VALUES (%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING",
                       (uuid4().hex, key, digest(request), Jsonb(request)))
            row = db.execute("SELECT * FROM uw_jobs WHERE idempotency_key=%s", (key,)).fetchone()
            if row["request_hash"] != digest(request):
                raise Conflict("idempotency_key_conflict")
            return row

    def job(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uw_jobs WHERE id=%s", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job_not_found")
            return row

    def jobs(self) -> list[str]:
        with self.connect() as db:
            return [row["id"] for row in db.execute("SELECT id FROM uw_jobs ORDER BY created_at DESC LIMIT 100").fetchall()]

    def bind_task(self, job_id: str, path: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT task_path FROM uw_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if row is None or row["task_path"] not in (None, path):
                raise Conflict("task_binding_conflict")
            db.execute("UPDATE uw_jobs SET task_path=%s WHERE id=%s", (path, job_id))

    def enqueue(self, job_id: str, key: str, request: dict) -> dict:
        with self.connect() as db:
            job = db.execute("SELECT * FROM uw_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if job is None:
                raise KeyError("job_not_found")
            existing = db.execute("SELECT * FROM uw_commands WHERE job_id=%s AND idempotency_key=%s", (job_id, key)).fetchone()
            if existing:
                if existing["request_hash"] != digest(request):
                    raise Conflict("idempotency_key_conflict")
                return existing
            if job["blocked"]:
                raise Conflict("job_requires_reconciliation")
            row = db.execute("INSERT INTO uw_commands(id,job_id,idempotency_key,request_hash,request) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                             (uuid4().hex, job_id, key, digest(request), Jsonb(request))).fetchone()
            return row

    def command(self, command_id: str, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uw_commands WHERE id=%s AND job_id=%s", (command_id, job_id)).fetchone()
            if row is None:
                raise KeyError("command_not_found")
            return row

    def commands(self, job_id: str) -> list[dict]:
        with self.connect() as db:
            return db.execute("SELECT * FROM uw_commands WHERE job_id=%s ORDER BY created_at,id", (job_id,)).fetchall()

    def start(self, command_id: str, version: str) -> None:
        with self.connect() as db:
            row = db.execute("UPDATE uw_commands SET status='running',before_version=%s,started_at=now() WHERE id=%s AND status='queued' RETURNING id",
                             (version, command_id)).fetchone()
            if row is None:
                raise Conflict("command_already_started")

    def finish(self, command_id: str, status: str, result: dict) -> None:
        if status not in {"completed", "blocked", "rejected"}:
            raise ValueError("invalid_command_status")
        with self.connect() as db:
            row = db.execute("UPDATE uw_commands SET status=%s,result=%s,finished_at=now() WHERE id=%s AND status IN ('queued','running') RETURNING job_id",
                             (status, Jsonb(result), command_id)).fetchone()
            if row is None:
                raise Conflict("command_result_already_frozen")
            if status == "blocked":
                db.execute("UPDATE uw_jobs SET blocked=%s WHERE id=%s", (result["reason"], row["job_id"]))

    def pause(self, job_id: str, paused: bool) -> None:
        with self.connect() as db:
            if db.execute("UPDATE uw_jobs SET paused=%s WHERE id=%s RETURNING id", (paused, job_id)).fetchone() is None:
                raise KeyError("job_not_found")

    def block(self, job_id: str, reason: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE uw_jobs SET blocked=%s WHERE id=%s", (reason, job_id))

    def runnable(self) -> list[str]:
        with self.connect() as db:
            return [row["id"] for row in db.execute("""SELECT j.id FROM uw_jobs j WHERE NOT j.paused AND j.blocked IS NULL
                AND (j.task_path IS NULL OR NOT j.graph_ready OR EXISTS (SELECT 1 FROM uw_commands c WHERE c.job_id=j.id
                AND (c.status IN ('queued','running') OR NOT c.graph_ack))) ORDER BY j.created_at LIMIT 100""").fetchall()]

    def acknowledge_graph(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE uw_jobs SET graph_ready=true WHERE id=%s", (job_id,))
            db.execute("UPDATE uw_commands SET graph_ack=true WHERE job_id=%s AND status IN ('completed','rejected','blocked')", (job_id,))

    @contextmanager
    def job_lock(self, job_id: str):
        # 单主机文件锁另外保护PG会话断线后仍在运行的进程，不以此锁宣称多机隔离。
        with self.connect() as db:
            acquired = db.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (job_id,)).fetchone()["acquired"]
            if not acquired:
                raise Conflict("job_busy")
            try:
                yield
            finally:
                db.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (job_id,))
