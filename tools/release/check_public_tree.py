"""检查实际公开候选的身份、输入闭包与明显私有内容；不执行第三方源码。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from upgrade_workbench.cases import load_case

FORBIDDEN_PARTS = {".git", ".idea", ".venv", ".local", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
# 由片段拼接已知机器模式，避免检查器把自己的规则正文误判成泄露。
PRIVATE_TEXT = re.compile("fu" + "xia" + r"|(?:D:)[/\\]+(?:Codex" + "Workspace|uwsvc)"
                          + "|/home/" + "peter/Development" + r"|sk-[A-Za-z0-9]{24,}")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check(root: Path) -> dict:
    publication = json.loads((root / "PUBLICATION.json").read_bytes())
    expected = publication["files"]
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() and not set(path.relative_to(root).parts) & FORBIDDEN_PARTS}
    if actual != set(expected) | {"PUBLICATION.json"}:
        raise ValueError("Public file inventory differs from reviewed publication manifest")
    for name, digest in expected.items():
        path = root / name
        if path.is_symlink() or path.suffix.lower() in {".log", ".key", ".pem", ".p12", ".sqlite", ".jsonl"}:
            raise ValueError(f"Forbidden public asset: {name}")
        raw = path.read_bytes()
        if sha(raw) != digest:
            raise ValueError(f"Publication bytes changed: {name}")
        members = [(name, raw)]
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                members += [(name + ":" + item, archive.read(item)) for item in archive.namelist() if not item.endswith("/")]
        for member, content in members:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if PRIVATE_TEXT.search(text):
                raise ValueError(f"Private path or key pattern in public asset: {member}")
    manifests = 0
    for name, identities in publication["fixtures"].items():
        for variant, identity in identities.items():
            case = load_case(root / "cases" / name / variant)
            if case.fingerprint != identity["public_fingerprint"]:
                raise ValueError("Public case identity changed")
            manifests += 1
    # 仅检查自有入口文档；第三方保留 README 的上游站内链接不是本仓库承诺。
    for name in ["README.md", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md",
                 "docs/public-install.md", "docs/public-tests.md", "docs/joint-r2-agent.md", "docs/examples/csvsql/README.md",
                 "docs/examples/comparison/README.md", "docs/examples/semantic-risk/README.md"]:
        document = root / name
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "#")):
                continue
            if not (document.parent / target.split("#", 1)[0]).is_file():
                raise ValueError(f"Broken product documentation link in {name}: {target}")
    return {"files": len(actual), "case_manifests": manifests,
            "boundary": "Tree/hash/manifest/known pattern/link checks; not proof of every possible secret or license"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(check(args.root.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
