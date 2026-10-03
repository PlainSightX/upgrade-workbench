"""使用自有案例、分发和假执行器验证查询边界；不启动 Docker 或第三方应用。"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from upgrade_workbench.cases import load_case
from upgrade_workbench.execution import _dependency_query_runner as script
from upgrade_workbench.execution import run_case_dependency_query
from upgrade_workbench.execution.cache import RECIPE, content_key

IMAGE = "sha256:" + "a" * 64
BASE_ID = "sha256:" + "b" * 64
BASE_DIGEST = "python@sha256:" + "c" * 64


def action(operation="inspect_symbol", environment="new", **fields):
    defaults = {
        "inspect_symbol": {"module": "owned_dependency.api", "qualname": "Thing"},
        "list_files": {"prefix": ""},
        "read_file": {"path": "owned_dependency/api.py", "start_line": 1, "end_line": 4},
        "search_text": {"query": "Thing", "paths": ["owned_dependency/api.py"], "max_results": 5},
    }
    return {
        "type": "query_dependency", "environment": environment,
        "distribution": "owned-dependency", "operation": operation,
        **defaults[operation], **fields,
    }


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "case"
    files = {
        "source/application.py": b"raise RuntimeError('must not execute application')\n",
        "checks/test_acceptance.py": b"raise RuntimeError('must not copy acceptance checks')\n",
        "SOURCE.json": b'{"kind":"synthetic_calibration"}\n',
        "wheels/owned_dependency-2.0-py3-none-any.whl": b"owned wheel fixture bytes",
        "environment.json": json.dumps({
            "schema_version": 1, "base_image": BASE_DIGEST, "python_version": "3.9",
            "local_wheels": ["wheels/owned_dependency-2.0-py3-none-any.whl"],
        }).encode(),
    }
    for role, version in (("old", "1.0"), ("new", "2.0")):
        files[f"requirements/{role}.txt"] = (
            f"owned_dependency=={version} \\\n    --hash=sha256:{'d' * 64}\n"
            f"pytest==7.4.4 --hash=sha256:{'e' * 64}\n"
        ).encode()
    for name, contents in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    manifest = {
        "schema_version": 1, "case_id": "owned-dependency-query", "title": "Owned query fixture",
        "source": {
            "kind": "synthetic_calibration", "repository": "owned", "revision": "v1",
            "license": "MIT", "description": "Self-owned query fixture",
        },
        "review": {"status": "reviewed", "note": "Self-owned input"},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "allowed_changes": ["application.py"], "expected_new_original": "failed",
        "file_hashes": {name: hashlib.sha256(contents).hexdigest() for name, contents in files.items()},
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_case(path)


class FakeExecutor:
    """校验实际冻结输入并输出自有收据，不执行所复制脚本或目标依赖。"""

    def __init__(self, fault=None):
        self.fault = fault
        self.prepared = []
        self.verified = []

    def prepare_environment(self, lock_path, **options):
        self.prepared.append((lock_path.read_bytes(), options))
        if self.fault == "prepare_error":
            raise RuntimeError("owned preparation failure")
        identity = {
            "base_image_id": BASE_ID, "base_image_digest": BASE_DIGEST,
            "platform": "linux/amd64", "expected_python": options["python_version"],
            "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            "local_wheel_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in options["local_wheels"]},
            "recipe": RECIPE,
        }
        if self.fault == "wrong_lock":
            identity["lock_sha256"] = "f" * 64
        key = content_key(identity)
        return {**identity, "cache_key": key, "image_id": IMAGE, "image_tag": "upgrade-workbench-env:" + key, "cache_hit": True}

    def verify(self, image_id, source_dir, checks_dir, **options):
        assert not list(source_dir.iterdir())
        assert sorted(path.name for path in checks_dir.iterdir()) == ["query.json", "test_dependency_query.py"]
        assert (checks_dir / "test_dependency_query.py").read_bytes() == Path(script.__file__).read_bytes()
        assert options == {"timeout_seconds": 25, "diagnostic": True}
        config = json.loads((checks_dir / "query.json").read_bytes())
        self.verified.append((image_id, config))
        public = {
            "status": "observed", "operation": config["action"]["operation"],
            "distribution": config["action"]["distribution"], "installed_version": config["expected_versions"][0],
            "python_version": "3.9.21", "result": {"signature": "(value)"}, "limitations": [],
        }
        if self.fault == "unavailable":
            public.update(status="unavailable", code="symbol_not_found", result=None, limitations=["Owned missing symbol"])
        record = {"nonce": config["nonce"], "query_id": config["query_id"], "public": public}
        if self.fault == "wrong_nonce":
            record["nonce"] = "wrong"
        if self.fault == "oversized":
            public["result"] = {"text": "x" * 25_000}
        line = script.PREFIX + json.dumps(record) + "\n"
        output = checks_dir.parent / "stdout.log"
        output.write_text(line * (2 if self.fault == "duplicate_record" else 1), encoding="utf-8")
        if self.fault == "input_changed":
            (checks_dir / "query.json").write_text("{}", encoding="utf-8")
        return {
            "status": "passed", "exit_code": 0, "image_id": "sha256:" + "f" * 64 if self.fault == "wrong_image" else image_id,
            "python_version": "3.9.21", "stdout": str(output),
            "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "cleanup": {"container": {"ok": self.fault != "cleanup_failed"}, "snapshot_image": {"ok": True}},
            "safety": {"network": "none", "host_mounts": []},
        }


@pytest.mark.parametrize("role,version", [("old", "1.0"), ("new", "2.0")])
def test_selected_case_lock_wheels_and_fixed_query_use_one_executor(case, tmp_path, role, version):
    runner = FakeExecutor()
    report = run_case_dependency_query(case, action(environment=role), tmp_path / "query", runner, 90, 25)
    assert report["status"] == "observed"
    assert report["public"]["installed_version"] == version
    assert report["public"]["environment"]["role"] == role
    assert runner.prepared[0][0] == (case.root / f"requirements/{role}.txt").read_bytes()
    assert runner.prepared[0][1]["base_image"] == BASE_DIGEST
    assert runner.prepared[0][1]["timeout_seconds"] == 90
    assert runner.prepared[0][1]["local_wheels"][0].read_bytes() == b"owned wheel fixture bytes"
    assert runner.verified[0][0] == report["preparation"]["image_id"]
    assert report["execution"]["cleanup"] == report["cleanup"]
    assert report["public"]["scope_limit"].startswith("Installed dependency facts only")
    assert len(json.dumps(report["public"]).encode()) <= 24_000
    assert json.loads(Path(report["report_path"]).read_bytes()) == report
    assert {Path(ref["path"]).name for ref in report["artifacts"]} >= {
        "preparation.json", "execution.json", "stdout.log", "requirements.txt", "test_dependency_query.py", "query.json",
    }
    with pytest.raises(FileExistsError):
        run_case_dependency_query(case, action(environment=role), tmp_path / "query", runner, 90, 25)
    assert len(runner.prepared) == 1


@pytest.mark.parametrize("fault", [
    "prepare_error", "wrong_lock", "wrong_nonce", "oversized", "duplicate_record",
    "input_changed", "wrong_image", "cleanup_failed",
])
def test_incomplete_or_misbound_query_retains_receipt_without_publishing_fact(case, tmp_path, fault):
    report = run_case_dependency_query(case, action(), tmp_path / "query", FakeExecutor(fault), 90, 25)
    assert report["status"] == "execution_incomplete"
    assert report["public"] is None
    assert report["failure"]["message"]
    assert json.loads(Path(report["report_path"]).read_bytes()) == report
    if fault not in {"prepare_error", "wrong_lock"}:
        assert report["execution"]
        assert report["cleanup"]


def test_unavailable_information_is_not_execution_failure_or_business_pass(case, tmp_path):
    report = run_case_dependency_query(case, action(), tmp_path / "query", FakeExecutor("unavailable"), 90, 25)
    assert report["status"] == "unavailable"
    assert report["public"]["code"] == "symbol_not_found"
    assert report["execution"]["status"] == "passed"


@pytest.mark.parametrize("bad_action", [
    action(distribution="unregistered"), action(module="os", qualname="system()"),
    action("read_file", path="../secret"), action("read_file", path="C:/secret"),
    action("list_files", prefix="/usr"), action("search_text", paths=["x/../../secret"]),
    action(command="echo invalid"), action(qualname="x" * 513),
])
def test_unregistered_distribution_or_path_never_reaches_executor(case, tmp_path, bad_action):
    runner = FakeExecutor()
    with pytest.raises(ValueError):
        run_case_dependency_query(case, bad_action, tmp_path / "query", runner)
    assert not runner.prepared
    assert not (tmp_path / "query").exists()


def test_changed_case_and_case_output_are_rejected_before_preparation(case, tmp_path):
    runner = FakeExecutor()
    with pytest.raises(ValueError, match="overlaps"):
        run_case_dependency_query(case, action(), case.root / "query", runner)
    (case.root / "requirements/new.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        run_case_dependency_query(case, action(), tmp_path / "query", runner)
    assert not runner.prepared


@pytest.fixture
def owned_distribution(tmp_path, monkeypatch):
    """运行脚本逻辑时仅提供测试自写的分发，避免宿主导入目标包。"""
    root = tmp_path / "site"
    package = root / "owned_dependency"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "class Thing:\n    label = 'owned'\n    def __init__(self, value, enabled=True):\n        self.value = value\n",
        encoding="utf-8",
    )
    (package / "static.py").write_text("raise RuntimeError('static reads must not import')\n", encoding="utf-8")
    info = root / "owned_dependency-2.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: owned-dependency\nVersion: 2.0\n", encoding="utf-8")
    (info / "RECORD").write_text(
        "owned_dependency/__init__.py,,\nowned_dependency/api.py,,\nowned_dependency/static.py,,\n../outside.py,,\n",
        encoding="utf-8",
    )
    (tmp_path / "outside.py").write_text("OUTSIDE = True\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    yield package, info
    for name in ("owned_dependency.api", "owned_dependency.static", "owned_dependency"):
        sys.modules.pop(name, None)


def query_config(query_action):
    return {"action": query_action, "expected_versions": ["2.0"], "query_id": "a" * 64, "nonce": "owned-nonce"}


def test_container_script_observes_actual_runtime_signature_and_owned_source(owned_distribution):
    result = script.query(query_config(action()))
    assert result["status"] == "observed"
    assert result["installed_version"] == "2.0"
    assert result["result"]["signature"] == "(value, enabled=True)"
    assert result["result"]["mro"] == ["owned_dependency.api.Thing", "builtins.object"]
    assert result["result"]["source"]["path"] == "owned_dependency/api.py"
    assert len(result["result"]["source"]["sha256"]) == 64


def test_static_reads_do_not_import_and_cannot_access_record_traversal(owned_distribution):
    result = script.query(query_config(action("read_file", path="owned_dependency/static.py")))
    assert result["status"] == "observed"
    assert "must not import" in result["result"]["text"]
    assert "owned_dependency.static" not in sys.modules
    listed = script.query(query_config(action("list_files")))
    assert "../outside.py" not in {row["path"] for row in listed["result"]["files"]}
    searched = script.query(query_config(action("search_text")))
    assert searched["result"]["matches"][0]["line"] == 1


@pytest.mark.parametrize("query_action,code", [
    (action(module="os", qualname="system"), "module_not_owned"),
    (action(qualname="Missing"), "symbol_not_found"),
    (action("read_file", path="../outside.py"), "file_not_owned"),
    (action("read_file", start_line=100, end_line=101), "read_range_out_of_bounds"),
])
def test_runtime_queries_report_explicit_limitations(owned_distribution, query_action, code):
    result = script.query(query_config(query_action))
    assert result["status"] == "unavailable"
    assert result["code"] == code


def test_installed_version_is_checked_before_module_import(owned_distribution):
    config = query_config(action())
    config["expected_versions"] = ["1.0"]
    result = script.query(config)
    assert result["code"] == "installed_version_mismatch"
    assert "owned_dependency.api" not in sys.modules


def test_large_source_result_is_bounded_including_json_escaping(owned_distribution):
    package, _ = owned_distribution
    (package / "api.py").write_text("VALUE = '" + "\u4e2d" * 10_000 + "'\n", encoding="utf-8")
    config = query_config(action("read_file"))
    record = script.output_record(config, script.query(config))
    assert record["public"]["code"] == "result_too_large"
    assert len(json.dumps(record).encode()) <= 24_000


def test_unrecorded_and_missing_distribution_files_do_not_expand_queries(owned_distribution):
    package, info = owned_distribution
    (package / "unrecorded.py").write_text("PRIVATE = True\n", encoding="utf-8")
    with (info / "RECORD").open("a", encoding="utf-8") as handle:
        handle.write("owned_dependency/missing.py,,\n")
    listing = script.query(query_config(action("list_files")))
    assert listing["status"] == "observed"
    names = {item["path"] for item in listing["result"]["files"]}
    assert "owned_dependency/missing.py" not in names
    assert "owned_dependency/unrecorded.py" not in names
    unavailable = script.query(query_config(action("read_file", path="owned_dependency/unrecorded.py")))
    assert unavailable["code"] == "file_not_owned"


def test_import_origin_mismatch_is_not_published_as_selected_distribution_fact(owned_distribution, monkeypatch):
    package, _ = owned_distribution
    real_import = script.importlib.import_module

    def wrong_origin(name):
        module = real_import(name)
        module.__file__ = str(package.parent / "outside.py")
        return module

    monkeypatch.setattr(script.importlib, "import_module", wrong_origin)
    result = script.query(query_config(action()))
    assert result["code"] == "module_origin_mismatch"


def test_fixed_script_emits_one_nonce_bound_record_after_partial_library_output(
    owned_distribution, monkeypatch, capsys, tmp_path,
):
    config = query_config(action())
    (tmp_path / "query.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(script, "__file__", str(tmp_path / "test_dependency_query.py"))
    print("partial dependency log", end="")
    script.test_dependency_query()
    lines = capsys.readouterr().out.splitlines()
    records = [json.loads(line[len(script.PREFIX):]) for line in lines if line.startswith(script.PREFIX)]
    assert len(records) == 1
    assert records[0]["nonce"] == config["nonce"]
    assert records[0]["public"]["status"] == "observed"
