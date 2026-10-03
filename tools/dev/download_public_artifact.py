"""有界 Range 下载公开大文件；只有长度和已知 SHA-256 均匹配才发布到目标路径。"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


def download(url: str, output: Path, *, size: int, sha256: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Public HTTPS URL and known SHA-256 required")
    if output.is_file():
        with output.open("rb") as handle:
            if output.stat().st_size == size and hashlib.file_digest(handle, "sha256").hexdigest() == sha256:
                print(f"Already verified: {output.name}", flush=True)
                return
    chunks = output.parent / (output.name + ".chunks")
    chunks.mkdir(parents=True, exist_ok=True)
    chunk_size = 16 * 1024 * 1024
    spans = [(start, min(start + chunk_size, size) - 1) for start in range(0, size, chunk_size)]

    def fetch(span):
        start, end = span
        target = chunks / f"{start:012d}.part"
        if target.is_file() and target.stat().st_size == end - start + 1:
            return target
        partial = target.with_suffix(".incomplete")
        result = subprocess.run([
            "curl.exe", "--silent", "--show-error", "--fail", "--location", "--noproxy", "*",
            "--proto", "=https", "--proto-redir", "=https", "--range", f"{start}-{end}",
            "--connect-timeout", "15", "--max-time", "90", "--retry", "2", "--retry-all-errors",
            "--output", str(partial), "--write-out", "%{http_code};%header{content-range}", url,
        ], capture_output=True, timeout=300)
        if result.returncode or result.stdout.decode().strip() != f"206;bytes {start}-{end}/{size}":
            raise RuntimeError(f"Artifact range rejected at offset {start}; no partial publication")
        if partial.stat().st_size != end - start + 1:
            raise ValueError("Artifact range length mismatch")
        partial.replace(target)
        return target

    completed = []
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        for index, path in enumerate(pool.map(fetch, spans), 1):
            completed.append(path)
            if index % 16 == 0 or index == len(spans):
                print(f"{output.name}: {index}/{len(spans)} verified ranges received", flush=True)
    finally:
        # 首个失败不再触发剩余几百个下载；已开始的有界请求结束后保留现有片段。
        pool.shutdown(wait=True, cancel_futures=True)
    partial = output.with_name(output.name + ".assembling")
    with partial.open("wb") as handle:
        for path in completed:
            with path.open("rb") as source:
                shutil.copyfileobj(source, handle)
    with partial.open("rb") as handle:
        if partial.stat().st_size != size or hashlib.file_digest(handle, "sha256").hexdigest() != sha256:
            raise ValueError("Whole artifact hash mismatch; chunks retained for diagnosis")
    partial.replace(output)
    print(f"SHA-256 verified: {output.name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    download(args.url, args.output, size=args.size, sha256=args.sha256)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
