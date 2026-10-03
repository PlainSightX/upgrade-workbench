"""模拟排名只验证合同；真实 BGE 排序另由开发探针运行，不能相互代替。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.cases import load_case
from upgrade_workbench.retrieval import EvidenceIndex
from upgrade_workbench.semantic import (
    MODELS,
    LocalBGE,
    _model_assets,
    rrf,
    semantic_search,
    validate_config,
)

CASE = Path(__file__).parents[2] / "cases/copier-6.2.0-r2/manifest.json"


class FakeBackend:
    identity = {"injected_test_double": True}

    def dense_scores(self, query, documents):
        return [float("Changes to dataclasses" in text) for text in documents]

    def rerank_scores(self, query, documents):
        return [float("Changes to dataclasses" in text) for text in documents]


def test_rrf_fuses_ranks_without_duplicate_vote():
    assert rrf([["a", "a", "b"], ["b"]]) == rrf([["a", "b"], ["b"]])
    assert rrf([["a", "b"], ["b"]])[0]["key"] == "b"
    assert rrf([["z"], ["a"]])[0]["key"] == "a"


@pytest.mark.parametrize("mode", ["dense", "hybrid", "rerank"])
def test_exact_source_parent_dedup_and_explicit_relevance_limit(mode):
    index = EvidenceIndex(load_case(CASE))
    report = semantic_search(index, "dataclass", backend=FakeBackend(), mode=mode)
    assert report["no_match_detection"] == "not_calibrated"
    assert report["binding"]["new_version"] == "2.11.7"
    assert len({hit["parent"]["evidence_key"] for hit in report["hits"]}) == len(report["hits"])
    assert all(hit["parent"] in index.parents for hit in report["hits"])
    assert report == semantic_search(index, "dataclass", backend=FakeBackend(), mode=mode)


def test_no_terms_does_not_run_inference():
    backend = FakeBackend()
    backend.dense_scores = lambda *_: pytest.fail("Empty query must not run a model")
    report = semantic_search(EvidenceIndex(load_case(CASE)), "***", backend=backend)
    assert report["hits"] == []


@pytest.mark.parametrize("values", [[float("nan")] * 56, [1.0], [float("inf")] * 56])
def test_bad_model_scores_fail_without_lexical_fallback(values):
    backend = FakeBackend()
    backend.dense_scores = lambda *_: values
    with pytest.raises(ValueError, match="scores"):
        semantic_search(EvidenceIndex(load_case(CASE)), "dataclass", backend=backend)


def test_reranker_is_explicit_and_receives_only_candidate_pool():
    backend = FakeBackend()
    sizes = []
    backend.rerank_scores = lambda query, docs: sizes.append(len(docs)) or [0.0] * len(docs)
    index = EvidenceIndex(load_case(CASE))
    semantic_search(index, "dataclass", backend=backend, mode="hybrid", candidate_k=5)
    assert not sizes
    semantic_search(index, "dataclass", backend=backend, mode="rerank", candidate_k=5)
    assert sizes == [5]


def test_configuration_requires_explicit_local_models_and_device():
    with pytest.raises(ValueError):
        validate_config({"mode": "hybrid"})
    with pytest.raises(ValueError):
        validate_config({"models_root": "x", "mode": "hybrid", "device": "auto"})


def assets(tmp_path, *, module_type="sentence_transformers.models.Transformer", module_path=""):
    root = tmp_path / "dense"
    names = ["config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors",
             "modules.json", "1_Pooling/config.json", "sentence_bert_config.json"]
    records = {}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (json.dumps([{"type": module_type, "path": module_path}]) if name == "modules.json" else "{}")
        path.write_text(text, encoding="utf-8")
        records[name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    model, revision = MODELS["dense"]
    (root / "assets.json").write_text(json.dumps({"model": model, "revision": revision,
        "remote_code": False, "files": records}), encoding="utf-8")
    return root


def test_modified_local_model_is_rejected_before_inference(tmp_path):
    root = assets(tmp_path)
    assert _model_assets(tmp_path, "dense")[0] == root
    (root / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="asset changed"):
        _model_assets(tmp_path, "dense")


@pytest.mark.parametrize("kind,path", [("remote.CustomModel", ""),
                                      ("sentence_transformers.models.Transformer", "../other")])
def test_remote_or_escaping_sentence_model_module_is_rejected(tmp_path, kind, path):
    assets(tmp_path, module_type=kind, module_path=path)
    with pytest.raises(ValueError, match="Custom model modules"):
        _model_assets(tmp_path, "dense")


def test_neural_length_overflow_cannot_silently_truncate():
    backend = object.__new__(LocalBGE)
    backend.encoder = type("Encoder", (), {"tokenizer": staticmethod(lambda *a, **kw: {"length": [8193]})})()
    with pytest.raises(ValueError, match="no silent text truncation"):
        backend._check_lengths(["oversized block"])


def test_task_cli_freezes_explicit_retrieval_without_editing_config(tmp_path, monkeypatch, capsys):
    from upgrade_workbench import cli

    original = {"generation": {"model": "test"}, "execution": {}, "max_calls": 2}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    captured = {}

    def create(*_args, **kwargs):
        captured.update(kwargs)
        return {"task_id": "test", "task_path": "test", "case_id": "test", "status": "ready", "attempts": []}

    monkeypatch.setattr(cli, "create_operation", create)
    assert cli.main(["task-create", str(CASE), "--config", str(path), "--budget", "unused",
                     "--retrieval-mode", "hybrid", "--models-root", str(tmp_path), "--device", "cpu"]) == 0
    assert captured["generation"]["semantic_config"] == {
        "models_root": str(tmp_path.absolute()), "mode": "hybrid", "device": "cpu"}
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
