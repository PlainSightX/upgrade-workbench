"""真实解析器的边界测试，不执行目标代码或要求目标依赖安装。"""

from importlib import metadata
from types import SimpleNamespace

import pytest

from upgrade_workbench.analysis import static


def snapshot(before, after=None):
    return SimpleNamespace(original=before, files=before if after is None else after, revision="b" * 64)


def test_baseline_new_disappeared_and_line_shifts():
    before = {"a.py": b"print(old)\nprint(gone)\n"}
    after = {"a.py": b"# moved\nprint(old)\nprint(new)\n"}
    report = static.diagnose(snapshot(before, after), "a" * 64)
    assert report["comparison_available"]
    assert [r["comparison"] for r in report["findings"]] == ["pre_existing", "newly_observed"]
    assert report["disappeared"][0]["message"] == "Undefined name `gone`"
    assert report["current"]["revision"] == "b" * 64
    assert report["baseline"]["source_hashes"] != report["current"]["source_hashes"]


def test_repeated_names_classified_by_counts_not_set():
    report = static.diagnose(snapshot({"a.py": b"print(old)\n"},
                                      {"a.py": b"print(old)\nprint(old)\n"}), "original")
    assert [r["comparison"] for r in report["findings"]] == ["pre_existing", "newly_observed"]


def test_noqa_config_cache_and_target_modules_cannot_interfere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ruff.toml").write_text("[lint]\nignore = ['F821']\n")
    (tmp_path / "ruff.py").write_text("raise RuntimeError('must not import target')")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("RUFF_OUTPUT_FILE", str(tmp_path / "unexpected-output.json"))
    code = b"raise RuntimeError('never execute')\nprint(missing)  # noqa: F821\n"
    files = {"target.py": code, "template.jinja": b"{{ missing }}"}
    result = static._scan(files)
    assert result["status"] == "complete"
    assert len(result["findings"]) == 1
    assert files["target.py"] == code
    assert list(result["source_hashes"]) == ["target.py"]
    assert not (tmp_path / ".ruff_cache").exists()
    assert not (tmp_path / "unexpected-output.json").exists()


def test_clean_snapshot_does_not_imply_behavior_success():
    report = static.visible(static.diagnose(snapshot({"a.py": b"x = 1\nprint(x)\n"}), "original"))
    assert report["current"]["status"] == "complete"
    assert report["current"]["observed_count"] == 0
    assert report["scope"] == "static_warning_not_runtime_failure_or_behavior_acceptance"


def test_parse_errors_are_partial_not_zero_pass():
    report = static.diagnose(snapshot({"bad.py": b"def broken(:\n", "a.py": b"print(x)\n"}), "original")
    assert report["current"]["status"] == "partial"
    assert report["current"]["errors"][0]["code"] == "parse_error"
    assert not report["comparison_available"]
    assert report["findings"][0]["comparison"] == "unclassified"


@pytest.mark.parametrize("failure,code", [("missing", "tool_missing"), ("wrong", "tool_version_mismatch")])
def test_unavailable_tool_explicit(monkeypatch, failure, code):
    def version(_):
        if failure == "missing":
            raise metadata.PackageNotFoundError("ruff")
        return "0.0.1"
    monkeypatch.setattr(static.metadata, "version", version)
    report = static.diagnose(snapshot({"a.py": b"pass\n"}), "original")
    assert report["current"]["status"] == "unavailable"
    assert report["current"]["errors"] == [{"code": code}]
    assert not report["comparison_available"]


@pytest.mark.parametrize("failure,code", [("timeout", "tool_timeout"),
    ("error", "tool_execution_failed"), ("invalid", "tool_output_invalid")])
def test_tool_failure_not_clean(monkeypatch, failure, code):
    def run(command, **kwargs):
        if failure == "timeout":
            raise static.subprocess.TimeoutExpired(command, 20)
        if failure == "invalid":
            kwargs["stdout"].write(b"not JSON")
        return SimpleNamespace(returncode=2 if failure == "error" else 0)
    monkeypatch.setattr(static.subprocess, "run", run)
    result = static._scan({"a.py": b"pass\n"})
    assert result["status"] == "unavailable"
    assert result["errors"] == [{"code": code}]


def test_visible_feedback_reports_truncation():
    report = static.diagnose(snapshot({"a.py": b"print(x)\n" * 140}), "original")
    public = static.visible(report)
    assert len(public["findings"]) == 128 and public["omitted_findings"] == 12
    assert len(report["findings"]) == public["current"]["observed_count"] == 140


def test_source_escape_rejected():
    with pytest.raises(ValueError, match="escapes"):
        static._scan({"../escape.py": b"pass\n"})


def test_option_like_filename_is_source_not_tool_configuration():
    result = static._scan({"--config.py": b"print(missing)\n"})
    assert result["status"] == "complete"
    assert result["findings"][0]["path"] == "--config.py"
