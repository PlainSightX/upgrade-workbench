"""下载维护方固定版本的模型资产并核验；不加载远端 Python 或使用账户令牌。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import getproxies

MODELS = {
    "dense": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"),
    "reranker": ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"),
}
FILES = {"README.md", "config.json", "config_sentence_transformers.json", "modules.json",
         "sentence_bert_config.json", "sentencepiece.bpe.model", "special_tokens_map.json",
         "tokenizer.json", "tokenizer_config.json", "1_Pooling/config.json",
         "pytorch_model.bin", "model.safetensors"}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def curl(url: str, *, destination: Path | None = None, direct: bool = False) -> bytes:
    # Windows curl 使用系统 TLS；显式传本进程代理，避免不同客户端忽略注册表代理。
    command = ["curl.exe" if sys.platform == "win32" else "curl", "--fail", "--location",
               "--silent", "--show-error", "--proto", "=https", "--proto-redir", "=https",
               "--connect-timeout", "20", "--max-time", "1200", "--retry", "2", "--retry-all-errors"]
    proxy = getproxies().get("https")
    if direct:
        command.extend(["--noproxy", "*"])
    elif proxy:
        command.extend(["--proxy", proxy])
    if destination:
        command.extend(["--output", str(destination)])
    result = subprocess.run([*command, url], capture_output=True, timeout=3700)
    if result.returncode:
        # 不写 curl 错误文本：重定向或代理地址可能含敏感参数。
        raise RuntimeError(f"Public model download failed: curl exit {result.returncode}")
    return result.stdout


def fetch(root: Path, kind: str, *, mirror: bool = False) -> dict:
    model, revision = MODELS[kind]
    metadata_url = f"https://huggingface.co/api/models/{model}/revision/{revision}?blobs=true"
    data = json.loads(curl(metadata_url))
    if data.get("sha") != revision or data.get("id") != model:
        raise ValueError("Model metadata identity mismatch")
    target = root / kind
    target.mkdir(parents=True, exist_ok=True)
    records = {}
    for item in data["siblings"]:
        name = item["rfilename"]
        if name not in FILES:
            continue
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = item.get("lfs", {}).get("sha256")
        if not (path.is_file() and expected and digest(path) == expected):
            print(f"Downloading {kind}/{name} ({item['size']} bytes)", flush=True)
            partial = path.with_name(path.name + ".partial")
            url = (f"https://modelscope.cn/models/{model}/resolve/master/{name}" if mirror
                   else f"https://huggingface.co/{model}/resolve/{revision}/{name}")
            if mirror and item["size"] > 100_000_000 and expected:
                from download_public_artifact import download

                download(url, partial, size=item["size"], sha256=expected)
            else:
                curl(url, destination=partial, direct=mirror)
            if partial.stat().st_size != item["size"]:
                raise ValueError("Model asset size mismatch")
            if expected:
                valid = digest(partial) == expected
            else:
                raw = partial.read_bytes()
                valid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == item["blobId"]
            if not valid:
                raise ValueError("Model asset hash mismatch")
            partial.replace(path)
        records[name] = {"sha256": digest(path), "bytes": path.stat().st_size,
                         "upstream_lfs_sha256": expected, "upstream_git_blob": item["blobId"]}
    # 此固定版 bge-m3 尚无 safetensors；只用 weights_only 读已核验官方权重后本地转换。
    # 运行阶段始终加载 safetensors，不允许 pickle 的任意对象反序列化回退。
    if kind == "dense":
        import torch
        from safetensors.torch import save_file

        weights = torch.load(target / "pytorch_model.bin", map_location="cpu", weights_only=True)
        if not isinstance(weights, dict) or not all(isinstance(value, torch.Tensor) for value in weights.values()):
            raise ValueError("Model state is not a tensor mapping")
        converted = target / "model.safetensors.converting"
        save_file({key: value.contiguous() for key, value in weights.items()},
                  str(converted), metadata={"format": "pt"})
        converted.replace(target / "model.safetensors")
    safe = target / "model.safetensors"
    records["model.safetensors"] = {"sha256": digest(safe), "bytes": safe.stat().st_size,
                                    "origin": "upstream" if kind == "reranker" else "local_weights_only_conversion"}
    manifest = {"schema_version": 1, "model": model, "revision": revision,
                "metadata_origin": metadata_url, "files": records, "remote_code": False,
                "asset_transport": "modelscope_direct_verified_against_upstream" if mirror else "huggingface"}
    (target / "assets.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"kind": kind, "model": model, "revision": revision, "files": len(records)}), flush=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=tuple(MODELS))
    parser.add_argument("--modelscope-direct", action="store_true",
                        help="只对公开资产采用直连镜像；仍逐文件验证维护方固定提交的哈希")
    args = parser.parse_args()
    for kind in (args.kind,) if args.kind else MODELS:
        fetch(args.output, kind, mirror=args.modelscope_direct)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
