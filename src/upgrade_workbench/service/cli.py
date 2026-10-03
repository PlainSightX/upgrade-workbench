"""本地部署入口：API和worker独立进程，共用显式配置和持久状态。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .config import Settings
from .engine import Engine, WorkerDatabaseError


def _run_worker(engine: Engine, *, once: bool) -> int:
    failures = 0
    try:
        while True:
            try:
                results = engine.run_once()
            except WorkerDatabaseError as error:
                failures += 1
                retry = error.retryable and not once and failures < 3
                report = error.public() | {"attempt": failures, "will_retry": retry}
                if not retry and error.phase == "poll":
                    report["next_action"] = "restore_database_then_restart_worker"
                print(json.dumps(report), flush=True)
                if not retry:
                    return 2
                time.sleep(2 ** (failures - 1))
                continue
            failures = 0
            if results or once:
                print(json.dumps(results), flush=True)
            if once:
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local recoverable upgrade service")
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="生成新实例配置，不连接PG或调用模型")
    prepare.add_argument("--work-root", type=Path, required=True)
    prepare.add_argument("--budget", type=Path, required=True)
    prepare.add_argument("--case", type=Path, required=True)
    prepare.add_argument("--profile", type=Path, required=True)
    prepare.add_argument("--database-url-env", default="UPGRADE_DATABASE_URL")
    sub.add_parser("setup")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8767)
    request = sub.add_parser("request")
    request.add_argument("method", choices=["GET", "POST"])
    request.add_argument("path")
    request.add_argument("--json", default="{}")
    request.add_argument("--body-file", type=Path)
    request.add_argument("--idempotency-key")
    request.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        from .bootstrap import prepare_config

        database_url = os.environ.get(args.database_url_env)
        if not database_url:
            parser.error("database URL environment variable is missing")
        result = prepare_config(args.config, work_root=args.work_root, budget=args.budget,
                                case=args.case, profile=args.profile, database_url=database_url)
        print(json.dumps(result))
        return 0
    settings = Settings.load(args.config)
    settings.validate()
    engine = Engine(settings)
    if args.command == "request":
        if not args.path.startswith("/") or any(c in args.path for c in "?#\\\r\n"):
            parser.error("request requires a local API path")
        headers = {"Authorization": "Bearer " + settings.token, "Content-Type": "application/json"}
        if args.idempotency_key:
            headers["Idempotency-Key"] = args.idempotency_key
        body = json.loads(args.body_file.read_text(encoding="utf-8") if args.body_file else args.json)
        request = Request(f"http://127.0.0.1:{args.port}" + args.path, method=args.method,
                          headers=headers, data=json.dumps(body).encode() if args.method == "POST" else None)
        # 本机API连接不借用公网代理，也不改变任何全局代理设置。
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
                print(response.read().decode("utf-8"))
        except HTTPError as error:
            print(error.read().decode("utf-8"))
            return 1
    elif args.command == "setup":
        engine.setup()
        print(json.dumps({"status": "initialized"}))
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False)
    else:
        return _run_worker(engine, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
