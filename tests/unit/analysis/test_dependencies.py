"""跨文件关联只能陈述已证实的静态边，不能把未知分支补成确定类型。"""

import hashlib

from upgrade_workbench.analysis.dependencies import build_dependencies


def analyze(files, symbol="Base.value", file="pkg/base.py"):
    blocks = [{"path": path, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
              for path, text in files.items()]
    findings = [{"file": "source/" + file, "line": 3, "symbol": symbol,
                 "rule": "nullable_without_default"}]
    return build_dependencies(blocks, findings)


def test_relative_import_alias_reexport_and_inheritance_chain():
    report = analyze({
        "pkg/base.py": "class Base:\n    value: str | None\n",
        "pkg/__init__.py": "from .base import Base as Exported\n",
        "pkg/child.py": "from . import Exported as Parent\nclass Child(Parent):\n    pass\n",
        "pkg/api.py": "from .child import Child\n",
    })
    places = {(row["location"]["path"], row["location"]["relation"]) for row in report["associations"]}
    assert ("pkg/child.py", "inherits_symbol") in places
    assert ("pkg/api.py", "imports_symbol") in places
    assert next(row for row in report["associations"] if row["location"]["path"] == "pkg/api.py")["chain"][-1]["target"] == "pkg.child.Child"
    assert all(row["status"] == "potential_static_dependency" for row in report["associations"])


def test_module_import_alias_and_src_layout():
    report = analyze({"src/pkg/base.py": "class Base: pass\n",
                      "src/pkg/child.py": "import pkg.base as b\nclass Child(b.Base): pass\n"},
                     file="src/pkg/base.py")
    assert len(report["associations"]) == 1
    assert report["associations"][0]["location"]["relation"] == "inherits_symbol"


def test_unrelated_import_does_not_inherit_whole_module_risk():
    report = analyze({"pkg/base.py": "class Base: pass\nclass Unrelated: pass\n",
                      "pkg/use.py": "from .base import Unrelated\n"})
    assert report["associations"] == []


def test_shadowed_or_conditional_imports_stay_unknown():
    report = analyze({"pkg/base.py": "class Base: pass\n",
                      "pkg/use.py": "from .base import Base\nBase = object\nclass Child(Base): pass\n",
                      "pkg/maybe.py": "if flag:\n    from .base import Base\nclass Child(Base): pass\n"})
    assert report["associations"] == []
    assert report["unknowns"]


def test_cyclic_reexports_and_ambiguous_modules_do_not_guess():
    report = analyze({"pkg/base.py": "from .other import Base\n",
                      "pkg/other.py": "from .base import Base\n",
                      "pkg/use.py": "from .base import Base\n"})
    assert report["associations"] == []
    assert report["unknowns"]
    report = analyze({"pkg/base.py": "class Base: pass\n",
                      "pkg/base/__init__.py": "class Base: pass\n",
                      "pkg/use.py": "from .base import Base\n"})
    assert report["associations"] == []
    assert any(item["reason"] == "ambiguous_module_path" for item in report["unknowns"])


def test_function_local_import_is_not_a_module_export_and_no_execution():
    report = analyze({"pkg/base.py": "class Base: pass\nraise RuntimeError('never execute')\n",
                      "pkg/use.py": "def f():\n    from .base import Base\n"})
    assert report["associations"] == []


def test_current_source_reanalysis_removes_deleted_edge():
    files = {"pkg/base.py": "class Base: pass\n", "pkg/use.py": "from .base import Base\n"}
    first = analyze(files)
    files["pkg/use.py"] = "x = 1\n"
    second = analyze(files)
    assert first["associations"] and not second["associations"]
    assert first["source_hashes"] != second["source_hashes"]


def test_namespace_mutation_and_conditional_wildcard_invalidate_aliases():
    for source in ["import pkg.base as b\nb.Base = object\nclass Child(b.Base): pass\n",
                   "from .base import Base\nif flag:\n    from other import *\nclass Child(Base): pass\n"]:
        report = analyze({"pkg/base.py": "class Base: pass\n", "pkg/use.py": source})
        assert not report["associations"]


def test_duplicate_class_exports_do_not_attribute_old_risk_to_new_class():
    report = analyze({"pkg/base.py": "class Base: pass\nclass Base: pass\n",
                      "pkg/use.py": "from .base import Base\n"})
    assert not report["associations"]
    assert any(row["reason"] == "duplicate_class_binding" for row in report["unknowns"])
