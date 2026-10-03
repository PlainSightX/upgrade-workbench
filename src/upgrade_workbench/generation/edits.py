"""把模型的精确文本替换编译为标准 diff，不让模型承担 hunk 行数计算。"""

from __future__ import annotations

import difflib

from ..cases import LoadedCase, PatchValidationError, load_case
from ..cases.manifest import read_verified_file, relative_path
from .request import MAX_PATCH_BYTES, _protected_source

MAX_EDITS = 32


class EditValidationError(PatchValidationError):
    """精确编辑不满足模型动作合同，并携带稳定拒绝码。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _text_bytes(value: str) -> bytes:
    try:
        contents = value.encode("utf-8")
    except UnicodeError as error:
        raise EditValidationError(
            "Exact edit text must be valid UTF-8",
            code="edit_invalid_fields",
        ) from error
    if b"\x00" in contents:
        raise EditValidationError("Binary exact edits are forbidden", code="edit_invalid_fields")
    return contents


def edited_sources(
    case: LoadedCase, edits: object, source_overrides: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """所有定位基于同一份已核验基底；只返回实际触及的文件，不丢弃其他修改。"""
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise EditValidationError(
            f"Provide between 1 and {MAX_EDITS} exact edits",
            code="edit_invalid_fields",
        )
    current = load_case(case.manifest_path)
    if current.fingerprint != case.fingerprint:
        raise PatchValidationError("Case changed before exact edit conversion")
    originals: dict[str, bytes] = {}
    replacements: dict[str, list[tuple[int, int, bytes, int]]] = {}
    total_bytes = 0
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or set(edit) != {"path", "old", "new"}:
            raise EditValidationError(
                "Each exact edit requires only path, old and new",
                code="edit_invalid_fields",
            )
        if any(not isinstance(edit[key], str) for key in edit):
            raise EditValidationError(
                "Exact edit fields must be strings",
                code="edit_invalid_fields",
            )
        name, old, new = edit["path"], edit["old"], edit["new"]
        try:
            relative_path(name)
        except ValueError as error:
            raise EditValidationError(
                "Exact edit path is not canonical",
                code="edit_path_outside_allowlist",
            ) from error
        if name not in current.manifest.allowed_changes or _protected_source(name):
            raise EditValidationError(
                "Exact edit path is outside the editable source allowlist",
                code="edit_path_outside_allowlist",
            )
        if not old or old == new:
            raise EditValidationError(
                f"Exact edit #{index} in {name}: old text is empty or old equals new. "
                "Use nonempty current source and a real change; omit unchanged edits. No edits applied.",
                code="edit_invalid_fields",
            )
        old_bytes, new_bytes = _text_bytes(old), _text_bytes(new)
        total_bytes += len(name.encode("utf-8")) + len(old_bytes) + len(new_bytes)
        if total_bytes > MAX_PATCH_BYTES:
            raise EditValidationError(
                "Exact edits exceed the total output byte budget",
                code="edit_invalid_fields",
            )
        if name not in originals:
            registered = f"source/{name}"
            originals[name] = read_verified_file(
                current.root, registered, current.manifest.file_hashes[registered]
            )
            if source_overrides is not None:
                originals[name] = source_overrides[name]
        original = originals[name]
        start = original.find(old_bytes)
        # 从下一个字节查找，也能发现 aaa 在 aaaa 中的重叠歧义。
        if start < 0:
            raise EditValidationError(
                f"Exact edit #{index} in {name}: old text was not found in the current source. "
                "Read the current revision and copy its exact text; edits in one submission all use "
                "that same base, not earlier edits' output. No edits applied.",
                code="edit_old_text_not_found",
            )
        if original.find(old_bytes, start + 1) >= 0:
            raise EditValidationError(
                f"Exact edit #{index} in {name}: old text matches multiple locations. "
                "Add surrounding context from the current source to identify one location. No edits applied.",
                code="edit_invalid_fields",
            )
        replacements.setdefault(name, []).append((start, start + len(old_bytes), new_bytes, index))

    updated_files = {}
    for name, changes in sorted(replacements.items()):
        changes.sort(key=lambda change: change[0])
        for before, after in zip(changes, changes[1:]):
            if before[1] > after[0]:
                raise EditValidationError(
                    f"Exact edits #{before[3]} and #{after[3]} in {name} overlap in the same base source. "
                    "Merge them into one replacement or choose disjoint ranges; do not chain replacements "
                    "within one submission. No edits applied.",
                    code="edit_invalid_fields",
                )
        original = originals[name]
        updated = original
        for start, end, new_bytes, _index in reversed(changes):
            updated = updated[:start] + new_bytes + updated[end:]
        if updated == original:
            raise EditValidationError(
                "Exact edits cancel out without changing the source",
                code="edit_invalid_fields",
            )
        updated_files[name] = updated
    if load_case(case.manifest_path).fingerprint != case.fingerprint:
        raise PatchValidationError("Case changed during source editing")
    return updated_files


def diff_files(originals: dict[str, bytes], updated_files: dict[str, bytes]) -> bytes:
    """diff只负责传输；当前文件内容与原始文件内容分别保存，不靠模型累计hunk。"""
    chunks: list[str] = []
    for name, updated in sorted(updated_files.items()):
        original = originals[name]
        try:
            before_lines = original.decode("utf-8").splitlines(keepends=True)
            after_lines = updated.decode("utf-8").splitlines(keepends=True)
        except UnicodeError as error:
            raise PatchValidationError("Exact edit source must be valid UTF-8") from error
        for line in difflib.unified_diff(
            before_lines, after_lines, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="\n"
        ):
            chunks.append(line)
            if not line.endswith("\n"):
                chunks.append("\n\\ No newline at end of file\n")
    patch = "".join(chunks).encode("utf-8")
    if len(patch) > MAX_PATCH_BYTES:
        raise PatchValidationError("Generated diff exceeds the output byte budget")
    return patch


def edits_to_patch(case: LoadedCase, edits: object) -> bytes:
    """旧协议仍是原始相对的完整编辑集；不改变冻结任务的编辑含义。"""
    updated = edited_sources(case, edits)
    originals = {
        name: read_verified_file(case.root, f"source/{name}", case.manifest.file_hashes[f"source/{name}"])
        for name in updated
    }
    patch = diff_files(originals, updated)
    if not patch:
        raise PatchValidationError("Generated diff is empty")
    return patch
