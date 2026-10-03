"""每条行为验证独立的 PostgreSQL；仅共享禁外网的 loopback 命名空间。"""

from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..cases.manifest import read_verified_file


class PostgresSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    kind: Literal["postgresql"]
    image: str = Field(pattern=r"^postgres@sha256:[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


def load_postgres_spec(case) -> dict | None:
    name = "execution.json"
    if name not in case.manifest.file_hashes:
        if (case.root / name).exists():
            raise ValueError("Unregistered execution.json must not be silently ignored")
        return None
    spec = PostgresSpec.model_validate_json(read_verified_file(case.root, name, case.manifest.file_hashes[name]))
    return spec.model_dump()


class EphemeralPostgres:
    """不发布端口，不挂载宿主文件，不复用任务服务数据库或持久卷。"""

    def __init__(self, executor, operation: str, spec: dict):
        if not re.fullmatch(r"[0-9a-f]{32}", operation):
            raise ValueError("Invalid database operation identity")
        self.executor = executor
        self.spec = PostgresSpec.model_validate(spec)
        self.operation = operation
        self.name = "upgrade-workbench-pg-" + operation
        self.report = {"kind": "postgresql", "container": self.name, "image": self.spec.image,
                       "ownership": {"project": "upgrade-workbench", "operation": operation},
                       "network": "none", "target_network": "container:" + self.name,
                       "published_ports": [], "persistent_volumes": [],
                       "data": "disposable_tmpfs", "credentials": "isolated_trust_no_host_secrets"}

    def start(self, deadline: float) -> None:
        from .docker import _PROXIES, _remaining, _require_success

        inspected = self.executor._run(["image", "inspect", self.spec.image], _remaining(deadline))
        _require_success(inspected, "Pinned PostgreSQL image missing; pull reviewed digest before validation")
        image = json.loads(inspected.stdout)[0]
        if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", ""))
            or image.get("Os") != "linux" or image.get("Config", {}).get("OnBuild")
            or set(image.get("Config", {}).get("Volumes", {})) - {"/var/lib/postgresql/data"}):
            raise ValueError("PostgreSQL image has unsupported metadata or volumes")
        self.report["image_id"] = image["Id"]
        command = [
            "run", "--detach", "--pull=never", "--name", self.name,
            "--label=org.upgrade-workbench.kind=ephemeral-postgres",
            "--label=project=upgrade-workbench",
            f"--label=org.upgrade-workbench.operation={self.operation}",
            "--network=none", "--user=postgres", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--pids-limit=128", "--memory=384m",
            "--memory-swap=384m", "--cpus=1", "--ipc=private", "--log-driver=none",
            "--tmpfs=/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs=/var/run/postgresql:rw,noexec,nosuid,nodev,size=8m,mode=1777",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--env=POSTGRES_HOST_AUTH_METHOD=trust", "--env=POSTGRES_DB=workbench",
            "--env=POSTGRES_USER=workbench", "--env=PGDATA=/var/lib/postgresql/data/db",
            *[f"--env={key}=" for key in _PROXIES], self.spec.image,
            "postgres", "-c", "listen_addresses=127.0.0.1", "-c", "max_connections=20",
            "-c", "shared_buffers=32MB", "-c", "statement_timeout=10000",
            "-c", "idle_in_transaction_session_timeout=10000",
        ]
        result = self.executor._run(command, _remaining(deadline))
        _require_success(result, "Temporary PostgreSQL start failed")
        while True:
            ready = self.executor._run(["exec", self.name, "psql", "-h", "127.0.0.1",
                                       "-U", "workbench", "-d", "workbench", "-Atqc",
                                       "SELECT current_setting('server_version')"], _remaining(deadline))
            if ready.returncode == 0:
                self.report["server_version"] = ready.stdout.strip()
                break
            time.sleep(min(0.25, _remaining(deadline)))
        inspection = self.executor._run(["inspect", self.name], _remaining(deadline))
        _require_success(inspection, "Cannot inspect database isolation")
        container = json.loads(inspection.stdout)[0]
        host = container["HostConfig"]
        if (host["NetworkMode"] != "none" or host.get("PortBindings")
            or host.get("Binds") or any(mount["Type"] != "tmpfs" for mount in container.get("Mounts", []))):
            raise ValueError("Database isolation contract does not match live container")
        self.report["isolation_verified"] = True

    def cleanup(self) -> dict:
        return self.executor._cleanup(["rm", "--force", "--volumes", self.name])
