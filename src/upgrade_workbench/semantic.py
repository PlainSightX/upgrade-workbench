"""固定本地 BGE 权重的可选混合检索；不下载模型、不改变版本证据权威。"""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from .cases.manifest import assert_no_links
from .retrieval import EvidenceIndex, _digest, _terms

MODELS = {
    "dense": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"),
    "reranker": ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"),
}


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or set(config) != {"models_root", "device", "mode"}:
        raise ValueError("Semantic retrieval requires models_root, device and mode")
    if config["mode"] not in {"dense", "hybrid", "rerank"} or config["device"] not in {"cpu", "cuda"}:
        raise ValueError("Unknown semantic retrieval mode or explicit device")
    if not isinstance(config["models_root"], str) or not config["models_root"]:
        raise ValueError("A local model asset directory is required")
    return {**config, "models_root": str(Path(config["models_root"]).absolute())}


def rrf(rankings: list[list[str]], *, constant: int = 60) -> list[dict]:
    """RRF 融合排名而非直接相加异尺度分数；同一路重复命中不额外投票。"""
    if type(constant) is not int or constant < 1:
        raise ValueError("RRF constant must be a positive integer")
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (constant + rank)
    return [{"key": key, "score": score}
            for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _model_assets(root: Path, kind: str) -> tuple[Path, str]:
    path = root / kind
    assert_no_links(path)
    manifest = path / "assets.json"
    assert_no_links(manifest)
    if not manifest.is_file():
        raise ValueError("Verified model assets missing; run tools/dev/fetch_retrieval_models.py")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if (data.get("model"), data.get("revision")) != MODELS[kind] or data.get("remote_code") is not False:
        raise ValueError("Local model identity differs from the registered revision")
    required = {"config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"}
    if kind == "dense":
        required |= {"modules.json", "1_Pooling/config.json", "sentence_bert_config.json"}
    if not required <= data.get("files", {}).keys():
        raise ValueError("Required model asset is unregistered")
    for name, record in data["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Model asset escapes its registered directory")
        file = path / relative
        assert_no_links(file)
        with file.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != record["sha256"] or file.stat().st_size != record["bytes"]:
            raise ValueError("Local model asset changed")
    if kind == "dense":
        allowed = {"sentence_transformers.models.Transformer", "sentence_transformers.models.Pooling",
                   "sentence_transformers.models.Normalize"}
        modules = json.loads((path / "modules.json").read_text(encoding="utf-8"))
        if any(item["type"] not in allowed or item.get("path") not in {"", "1_Pooling", "2_Normalize"}
               for item in modules):
            raise ValueError("Custom model modules are not allowed")
    return path, _digest(data)


class LocalBGE:
    """只通过成熟推理库运行已核验模型，源码与私有信息不离开本机。"""

    def __init__(self, root: Path, *, device: str, reranker: bool) -> None:
        try:
            import numpy as np
            import torch
            from sentence_transformers import CrossEncoder, SentenceTransformer
        except ImportError as error:
            raise RuntimeError("Semantic retrieval requires uv sync --locked --extra retrieval") from error
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but unavailable; choose CPU explicitly")
        self.np = np
        if device == "cpu":
            # 小批量编码避免宿主高线程数的同步开销；只作用于当前推理进程。
            torch.set_num_threads(8)
        dense_path, dense_hash = _model_assets(root, "dense")
        dtype = torch.float16 if device == "cuda" else torch.float32
        self.encoder = SentenceTransformer(str(dense_path), device=device, local_files_only=True,
            trust_remote_code=False, model_kwargs={"use_safetensors": True, "torch_dtype": dtype})
        self.encoder.max_seq_length = 8192
        self.reranker = None
        reranker_hash = None
        if reranker:
            path, reranker_hash = _model_assets(root, "reranker")
            self.reranker = CrossEncoder(str(path), device=device, max_length=8192,
                local_files_only=True, trust_remote_code=False,
                model_kwargs={"use_safetensors": True, "torch_dtype": dtype})
        self.identity = {"dense": dense_hash, "reranker": reranker_hash, "device": device,
                         "dtype": str(dtype), "max_tokens": 8192, "batch_size": 2,
                         "cpu_threads": torch.get_num_threads() if device == "cpu" else None,
                         "sentence_transformers": version("sentence-transformers"),
                         "torch": version("torch"), "transformers": version("transformers")}
        self.documents: dict[str, object] = {}
        self.queries: dict[str, object] = {}
        self.reranked: dict[str, list[float]] = {}

    def _check_lengths(self, texts: list[str]) -> None:
        lengths = self.encoder.tokenizer(texts, truncation=False, padding=False, return_length=True)["length"]
        if any(length > 8192 for length in lengths):
            raise ValueError("Dense model token capacity exceeded; no silent text truncation")

    def dense_scores(self, query: str, documents: list[str]) -> list[float]:
        self._check_lengths([query, *documents])
        key = _digest(documents)
        if key not in self.documents:
            if len(self.documents) >= 4:
                self.documents.clear()
            self.documents[key] = self.encoder.encode(documents, normalize_embeddings=True,
                convert_to_numpy=True, batch_size=2, show_progress_bar=False).astype(self.np.float32)
        if query not in self.queries:
            if len(self.queries) >= 256:
                self.queries.clear()
            self.queries[query] = self.encoder.encode([query], normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False).astype(self.np.float32)[0]
        vector = self.queries[query]
        return (self.documents[key] @ vector).tolist()

    def rerank_scores(self, query: str, documents: list[str]) -> list[float]:
        if self.reranker is None:
            raise ValueError("Reranker was not loaded; no silent fallback")
        key = _digest({"query": query, "documents": documents})
        if key in self.reranked:
            return list(self.reranked[key])
        lengths = self.reranker.tokenizer([query] * len(documents), documents,
            truncation=False, padding=False, return_length=True)["length"]
        if any(length > 8192 for length in lengths):
            raise ValueError("Reranker token capacity exceeded; no silent text truncation")
        values = self.reranker.predict([(query, text) for text in documents],
                                      batch_size=2, show_progress_bar=False)
        if len(self.reranked) >= 64:
            self.reranked.clear()
        self.reranked[key] = values.reshape(-1).tolist()
        return list(self.reranked[key])


@lru_cache(maxsize=2)
def _backend(root: str, device: str, reranker: bool) -> LocalBGE:
    return LocalBGE(Path(root), device=device, reranker=reranker)


def _scores(values: list[float], count: int) -> list[float]:
    if len(values) != count or any(not math.isfinite(value) for value in values):
        raise ValueError("Model returned invalid or nonfinite scores")
    return values


def semantic_search(index: EvidenceIndex, query: str, *, backend, mode: str = "hybrid",
                    top_k: int = 5, candidate_k: int = 20) -> dict:
    if mode not in {"dense", "hybrid", "rerank"}:
        raise ValueError("Unknown semantic retrieval mode")
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")
    if type(candidate_k) is not int or not top_k <= candidate_k <= 100:
        raise ValueError("Candidate capacity must cover top_k and be at most 100")
    terms = _terms(query)
    identity = _digest({"corpus": index.identity, "model": backend.identity, "mode": mode,
                        "rrf_constant": 60, "candidate_k": candidate_k})
    result = {"schema_version": 1, "method": f"bge_m3_{mode}_parent_child_v1",
              "corpus_sha256": index.identity, "retrieval_sha256": identity,
              "models": backend.identity, "case_fingerprint": index.bundle["case_fingerprint"],
              "binding": index.bundle["binding"], "query": query, "terms": terms,
              "counts": {"parents": len(index.parents), "children": len(index.children)},
              "scope": "ranked_candidates_not_relevance_threshold_or_migration_acceptance"}
    if not terms:
        return {**result, "status": "no_match", "hits": [], "ranking": []}
    children = {child["evidence_key"]: child for child in index.children}
    texts = {key: " / ".join(child["heading_path"]) + "\n" + child["excerpt"] for key, child in children.items()}
    scores = _scores(backend.dense_scores(query, list(texts.values())), len(texts))
    dense = sorted(zip(texts, scores, strict=True), key=lambda item: (-item[1], item[0]))[:candidate_k]
    lexical = index.rank_children(query)[:candidate_k]
    ranks = {"dense": {key: rank for rank, (key, _) in enumerate(dense, 1)},
             "bm25": {item["key"]: rank for rank, item in enumerate(lexical, 1)}}
    dense_scores = dict(dense)
    fused = rrf([[item["key"] for item in lexical], [key for key, _ in dense]])[:candidate_k]
    ranking = ([{"key": key, "score": value} for key, value in dense] if mode == "dense" else fused)
    rerank_scores = {}
    if mode == "rerank" and ranking:
        values = _scores(backend.rerank_scores(query, [texts[item["key"]] for item in ranking]), len(ranking))
        rerank_scores = {item["key"]: value for item, value in zip(ranking, values, strict=True)}
        ranking = sorted(ranking, key=lambda item: (-rerank_scores[item["key"]], -item["score"], item["key"]))
    hits, seen = [], set()
    for row in ranking:
        key = row["key"]
        child = children[key]
        parent = child["parent"]
        if parent["evidence_key"] in seen:
            continue
        seen.add(parent["evidence_key"])
        hits.append({"rank": len(hits) + 1,
                     "child": {key: value for key, value in child.items() if key != "parent"},
                     "parent": parent, "dense_score": dense_scores.get(key),
                     "bm25_rank": ranks["bm25"].get(key), "dense_rank": ranks["dense"].get(key),
                     "rrf_score": row["score"] if mode != "dense" else None,
                     "reranker_score": rerank_scores.get(key)})
        if len(hits) == top_k:
            break
    return {**result, "status": "ranked_candidates", "hits": hits,
            "ranking": ranking, "candidate_k": candidate_k,
            "relevance_threshold": None, "no_match_detection": "not_calibrated"}


def configured_search(index: EvidenceIndex, query: str, *, top_k: int, config: dict) -> dict:
    config = validate_config(config)
    backend = _backend(config["models_root"], config["device"], config["mode"] == "rerank")
    return semantic_search(index, query, backend=backend, mode=config["mode"], top_k=top_k)
