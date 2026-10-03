"""精确替换按原始字节定位，diff 行数与最终换行交给本地编译处理。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.cases import PatchValidationError, load_case, stage_source
from upgrade_workbench.generation import complete_request, prepare_request
from upgrade_workbench.generation import edits as edits_module
from upgrade_workbench.generation.edits import EditValidationError, edits_to_patch


def replace_original(case, contents):
    (case.source_dir / "model.py").write_bytes(contents)
    manifest = json.loads(case.manifest_path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["source/model.py"] = hashlib.sha256(contents).hexdigest()
    case.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_case(case.manifest_path)


def stage_edits(case, edits, tmp_path):
    patch = edits_to_patch(case, edits)
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_bytes(patch)
    stage_source(case, tmp_path / "candidate", patch_path)
    return patch, (tmp_path / "candidate/model.py").read_bytes()


def test_multiple_edits_use_original_positions_not_prior_replacements(owned_case, tmp_path):
    changes = [
        {"path": "model.py", "old": "Profile", "new": "LongerProfileName"},
        {"path": "model.py", "old": "label:", "new": "title:"},
        {"path": "model.py", "old": " str | None", "new": " int | None = None"},
    ]
    _, actual = stage_edits(owned_case, list(reversed(changes)), tmp_path)
    assert actual == (
        b"from pydantic import BaseModel\nclass LongerProfileName(BaseModel):\n"
        b"    title: int | None = None\n"
    )


@pytest.mark.parametrize("original,new,expected", [
    (b"VALUE = 1", "2", b"VALUE = 2"),
    (b"VALUE = 1\n", "2", b"VALUE = 2\n"),
    (b"VALUE = 1\r\n", "2", b"VALUE = 2\r\n"),
    (b"VALUE = 1", "2\n", b"VALUE = 2\n"),
])
def test_generated_diff_preserves_exact_final_newline_semantics(
    owned_case, tmp_path, original, new, expected
):
    case = replace_original(owned_case, original)
    patch, actual = stage_edits(case, [{"path": "model.py", "old": "1", "new": new}], tmp_path)
    assert actual == expected
    if not original.endswith(b"\n"):
        assert b"\\ No newline at end of file" in patch


def test_unicode_replacement_keeps_utf8_bytes(owned_case, tmp_path):
    case = replace_original(owned_case, '# 旧说明\nVALUE = "旧"\n'.encode())
    _, actual = stage_edits(case, [{"path": "model.py", "old": '"旧"', "new": '"新"'}], tmp_path)
    assert actual == '# 旧说明\nVALUE = "新"\n'.encode()


@pytest.mark.parametrize("changes", [
    [], [{}], [{"path": "model.py", "old": "label", "new": "title", "extra": "x"}],
    [{"path": "model.py", "old": "", "new": "insert"}],
    [{"path": "model.py", "old": "label", "new": "label"}],
    [{"path": "model.py", "old": 3, "new": "x"}],
    [{"path": "model.py", "old": "not present", "new": "x"}],
    [{"path": "model.py", "old": "label", "new": "\x00"}],
    [{"path": "../checks/test_contract.py", "old": "label", "new": "x"}],
    [{"path": "helper.py", "old": "VALUE", "new": "x"}],
    [{"path": "model.py", "old": "label: str", "new": "x"},
     {"path": "model.py", "old": "str | None", "new": "y"}],
    [{"path": "model.py", "old": "label", "new": "title"},
     {"path": "model.py", "old": "title", "new": "other"}],
    [{"path": "model.py", "old": "label", "new": "x"}] * 33,
])
def test_ambiguous_outside_or_invalid_edits_are_rejected(owned_case, changes):
    with pytest.raises(PatchValidationError):
        edits_to_patch(owned_case, changes)


def test_overlapping_occurrences_are_ambiguous_even_when_bytes_count_is_one(owned_case):
    case = replace_original(owned_case, b"aaaa\n")
    with pytest.raises(PatchValidationError, match="matches multiple locations"):
        edits_to_patch(case, [{"path": "model.py", "old": "aaa", "new": "z"}])


@pytest.mark.parametrize("old,new,expected", [("absent", "replacement", "not found"),
    ("label", "label", "old equals new"), ("", "insert", "old text is empty")])
def test_edit_feedback_identifies_file_and_one_based_index(owned_case, old, new, expected):
    edits = [{"path": "model.py", "old": "Profile", "new": "AnotherProfile"},
             {"path": "model.py", "old": old, "new": new}]
    before = (owned_case.source_dir / "model.py").read_bytes()
    with pytest.raises(PatchValidationError) as failure:
        edits_to_patch(owned_case, edits)
    assert "#2 in model.py" in str(failure.value) and expected in str(failure.value)
    assert "No edits applied" in str(failure.value)
    assert (owned_case.source_dir / "model.py").read_bytes() == before


@pytest.mark.parametrize(("edits", "code"), [
    ([{"path": "model.py", "old": "not present", "new": "x"}], "edit_old_text_not_found"),
    ([{"path": "helper.py", "old": "VALUE", "new": "OTHER"}], "edit_path_outside_allowlist"),
    ([{"path": "../model.py", "old": "label", "new": "title"}], "edit_path_outside_allowlist"),
])
def test_model_edit_rejections_carry_stable_codes(owned_case, edits, code):
    with pytest.raises(EditValidationError) as failure:
        edits_to_patch(owned_case, edits)
    assert isinstance(failure.value, PatchValidationError)
    assert failure.value.code == code


def test_overlap_feedback_names_original_indices_even_when_input_is_reversed(owned_case):
    edits = [{"path": "model.py", "old": "str | None", "new": "int | None"},
             {"path": "model.py", "old": "label: str", "new": "value: str"}]
    with pytest.raises(PatchValidationError) as failure:
        edits_to_patch(owned_case, edits)
    assert "#2 and #1 in model.py overlap" in str(failure.value)
    assert "Merge them into one replacement" in str(failure.value)


def test_bad_second_file_does_not_persist_first_file_and_corrected_batch_is_atomic(owned_case, tmp_path):
    from upgrade_workbench.candidates import apply_increment, load_candidate

    manifest = json.loads(owned_case.manifest_path.read_bytes())
    manifest["allowed_changes"].append("helper.py")
    owned_case.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    case = load_case(owned_case.manifest_path)
    base = load_candidate(case)
    edits = [{"path": "model.py", "old": "Profile", "new": "AnotherProfile"},
             {"path": "helper.py", "old": "missing", "new": "4"}]
    action = {"type": "submit_candidate", "base_revision": base.revision, "edits": edits}
    with pytest.raises(PatchValidationError, match="#2 in helper.py"):
        apply_increment(case, base, action, tmp_path / "rejected")
    assert load_candidate(case).files == base.files
    assert not list((tmp_path / "rejected").rglob("*.py"))
    edits[1]["old"] = "3"
    changed = apply_increment(case, base, action, tmp_path / "accepted")
    assert changed.parent == base.revision
    assert b"AnotherProfile" in changed.files["model.py"] and changed.files["helper.py"] == b"VALUE = 4\n"
    assert load_candidate(case).files == base.files


def test_total_edit_bytes_and_generated_diff_bytes_are_bounded(owned_case, monkeypatch):
    change = [{"path": "model.py", "old": "label", "new": "x" * 100}]
    monkeypatch.setattr(edits_module, "MAX_PATCH_BYTES", 50)
    with pytest.raises(PatchValidationError, match="total output"):
        edits_to_patch(owned_case, change)
    with pytest.raises(PatchValidationError, match="Generated diff"):
        edits_to_patch(owned_case, [{"path": "model.py", "old": "label", "new": "x"}])


def test_exact_edits_provider_path_retains_raw_response_and_uses_stage_source(
    owned_case, analysis, evidence, arguments
):
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="exact_edits")
    payload = json.loads(Path(prepared["request_path"]).read_text(encoding="utf-8"))
    assert "Do not compute unified diff hunk headers" in payload["messages"][0]["content"]
    proposal = {"summary": "Keep omitted field behavior.", "edits": [
        {"path": "model.py", "old": "label: str | None", "new": "label: str | None = None"},
    ]}
    raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(proposal),
    }}]}).encode()
    report = complete_request(prepared, api_key="owned-secret", transport=lambda *a, **kw: raw)
    assert report["status"] == "pending_review"
    assert report["output_format"] == "exact_edits"
    assert report["patch_compilation"] == "difflib_from_exact_original_edits"
    assert report["edit_count"] == 1
    assert Path(report["response_path"]).read_bytes() == raw
    assert report["target_code_executed"] is False
    assert b" = None\n" in (Path(report["candidate_source_dir"]) / "model.py").read_bytes()


def test_invalid_exact_edit_is_a_candidate_rejection_not_an_executed_test_failure(
    owned_case, analysis, evidence, arguments
):
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="exact_edits")
    raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "summary": "Bad edit", "edits": [{"path": "model.py", "old": "absent", "new": "x"}],
    })}}]}).encode()
    report = complete_request(prepared, api_key="owned-secret", transport=lambda *a, **kw: raw)
    assert report["status"] == "candidate_rejected"
    assert report["verification_status"] == "not_run"
    assert not Path(report["report_path"]).with_name("candidate").exists()
