"""验证静态定位的边界，尤其避免把别名遮蔽或显式必填误报为迁移变化。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.analysis import analyze_case
from upgrade_workbench.cases.manifest import CaseValidationError, LoadedCase, load_case

CALIBRATION = Path(__file__).parents[3] / "cases" / "pydantic-field-contract"


def case_with_source(tmp_path: Path, source: str) -> LoadedCase:
    case = tmp_path / "case"
    shutil.copytree(CALIBRATION, case)
    target = case / "source" / "profile_model.py"
    target.write_text(source, encoding="utf-8")
    manifest_path = case / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["source/profile_model.py"] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_case(manifest_path)


def test_aliases_nullable_and_explicit_required_are_distinct(tmp_path: Path) -> None:
    case = case_with_source(tmp_path, '''
import pydantic as pd
from pydantic import Field as F
from typing import Any as A, Optional as O, Union as U, Annotated, ClassVar
class Customer(pd.BaseModel):
    missing: O[str]
    union: U[int, None]
    modern: str | None
    anything: A
    described: O[str] = F(description="x")
    explicit: O[str] = ...
    field_required: O[str] = F(...)
    keyword_required: O[str] = F(default=...)
    factory: O[str] = F(default_factory=lambda: None)
    nullable: O[str] = None
    annotated_factory: Annotated[O[str], F(default_factory=lambda: None)]
    not_a_field: ClassVar[O[str]]
''')
    report = analyze_case(case)
    assert {finding["symbol"] for finding in report["findings"]} == {
        "Customer.missing", "Customer.union", "Customer.modern", "Customer.anything",
        "Customer.described",
    }
    assert all(finding["status"] == "potential_impact" for finding in report["findings"])
    assert all(finding["evidence_key"] for finding in report["findings"])
    assert report["case_fingerprint"] == case.fingerprint
    json.dumps(report)


def test_config_decorators_and_exact_import_moves(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel as Model, validator as validate, root_validator as root
from pydantic import BaseSettings
from pydantic.parse import Protocol
from other_package import validator
class Request(Model):
    class Config:
        orm_mode = True
    @validate("name")
    def check_name(cls, value):
        return value
    @root()
    def check_all(cls, values):
        return values
    @validator("name")
    def foreign(cls, value):
        return value
'''))
    rules = [finding["rule"] for finding in report["findings"]]
    assert rules.count("legacy_validator") == 2
    assert set(rules) == {"basesettings_moved", "import_moved_or_removed", "class_config",
                          "legacy_validator"}
    assert next(f for f in report["findings"] if f["symbol"] == "Request.Config")["line"] == 7


def test_module_alias_settings_and_nested_classes_keep_scope_explicit(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
import pydantic as pd
class Settings(pd.BaseSettings):
    value: str | None
    class Nested(pd.BaseModel):
        inner: str | None
'''))
    assert {item["rule"] for item in report["findings"]} == {
        "basesettings_moved", "nullable_without_default"
    }
    assert [item["symbol"] for item in report["unknowns"]] == ["Settings.Nested"]


@pytest.mark.parametrize("shadow", [
    "Model = object", "from unrelated import BaseModel as Model", "def Model(): pass",
    "if condition:\n    Model = object", "del Model",
])
def test_module_shadowing_does_not_invent_pydantic_fields(tmp_path: Path, shadow: str) -> None:
    report = analyze_case(case_with_source(tmp_path, f'''
from pydantic import BaseModel as Model
from typing import Optional
{shadow}
class Request(Model):
    value: Optional[str]
'''))
    assert report["findings"] == []
    assert any(item["rule"] == "unresolved_inheritance" for item in report["unknowns"])


def test_local_shadowing_and_deferred_class_are_unknown(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel
from typing import Optional
class Request(BaseModel):
    Optional = list
    value: Optional[str]
def factory(BaseModel):
    class Local(BaseModel):
        value: str | None
    return Local
'''))
    assert report["findings"] == []
    assert {item["rule"] for item in report["unknowns"]} == {
        "unresolved_annotation", "function_local_class"
    }


def test_method_local_names_do_not_shadow_class_annotations(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel
from typing import Optional
class Request(BaseModel):
    def method(self):
        Optional = list
        return Optional()
    value: Optional[str]
'''))
    assert [item["symbol"] for item in report["findings"]] == ["Request.value"]


def test_shadowed_field_metadata_cannot_create_missing_default_finding(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel, Field as F
from typing import Optional, Annotated
F = make_field_factory()
class Request(BaseModel):
    a: Optional[str] = F(...)
    b: Annotated[Optional[str], F(default_factory=lambda: None)]
'''))
    assert report["findings"] == []
    assert [item["rule"] for item in report["unknowns"]] == [
        "dynamic_field_default", "dynamic_field_default"
    ]


def test_conditional_wildcard_import_invalidates_previous_bindings(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel
if condition:
    from other import *
class Request(BaseModel):
    value: str | None
'''))
    assert report["findings"] == []
    assert any(item["rule"] == "unresolved_inheritance" for item in report["unknowns"])


def test_inheritance_and_dynamic_defaults_are_not_claimed_resolved(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic import BaseModel, Field
class Parent(BaseModel):
    value: str | None = Field(**options)
class Child(Parent):
    other: str | None
class Dynamic(make_base()):
    other: str | None
'''))
    assert report["findings"] == []
    assert [item["rule"] for item in report["unknowns"]] == [
        "dynamic_field_default", "unresolved_inheritance", "unresolved_inheritance"
    ]


def test_forward_annotations_and_namespace_mutation(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, '''
import pydantic as pd
from typing import Optional as O
class First(pd.BaseModel):
    value: "O[str]"
pd.BaseModel = object
class Second(pd.BaseModel):
    value: "O[str]"
'''))
    assert [item["symbol"] for item in report["findings"]] == ["First.value"]
    assert [item["symbol"] for item in report["unknowns"]] == ["Second"]


def test_syntax_error_is_an_unknown_not_clean_compatibility(tmp_path: Path) -> None:
    report = analyze_case(case_with_source(tmp_path, "class Broken(:\n"))
    assert report["findings"] == []
    assert report["unknowns"][0]["rule"] == "unparseable_source"
    assert report["unknowns"][0]["line"] == 1


def test_changed_source_is_rejected_after_loading(tmp_path: Path) -> None:
    case = case_with_source(tmp_path, "from pydantic import BaseModel\n")
    (case.source_dir / "profile_model.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(CaseValidationError, match="SHA-256 mismatch"):
        analyze_case(case)


def test_target_is_never_imported_or_executed(tmp_path: Path) -> None:
    marker = tmp_path / "target-executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('target')\n"
    analyze_case(case_with_source(tmp_path, source))
    assert not marker.exists()


def test_pydantic_dataclass_alias_and_validator_not_model_field_semantics(tmp_path):
    report = analyze_case(case_with_source(tmp_path, '''
from pydantic.dataclasses import dataclass as dc
from pydantic import validator as check
@dc(config={})
class Input:
    value: str | None
    @check("value")
    def validate(cls, value):
        return value
'''))
    assert [(item["rule"], item["line"]) for item in report["findings"]] == [
        ("pydantic_dataclass", 4), ("legacy_validator", 7),
    ]
    assert "nullable_without_default" not in {item["rule"] for item in report["findings"]}


@pytest.mark.parametrize("binding", [
    "from dataclasses import dataclass", "from unrelated import dataclass",
    "from pydantic.dataclasses import dataclass\ndataclass = other",
    "from pydantic.dataclasses import dataclass\nif condition:\n    dataclass = other",
])
def test_dataclass_name_alone_is_not_pydantic(tmp_path, binding):
    report = analyze_case(case_with_source(tmp_path, binding + "\n@dataclass\nclass Input:\n    value: str | None\n"))
    assert report["findings"] == []


def test_conditional_custom_hooks_remain_potential_not_reachability(tmp_path):
    report = analyze_case(case_with_source(tmp_path, '''
if condition:
    class Custom:
        @classmethod
        def __get_validators__(cls):
            yield check
        def __modify_schema__(cls, schema):
            pass
'''))
    assert [(item["symbol"], item["line"]) for item in report["findings"]] == [
        ("Custom.__get_validators__", 5), ("Custom.__modify_schema__", 7),
    ]
    assert all(item["status"] == "potential_impact" for item in report["findings"])
    assert report["unknowns"][0]["rule"] == "conditional_or_contextual_bindings"


def test_candidate_shift_repair_and_original_source_binding(tmp_path):
    from upgrade_workbench.candidates import apply_increment, load_candidate

    case = case_with_source(tmp_path, 'from pydantic import BaseModel\nclass Model(BaseModel):\n    value: "str | None"\n')
    base = load_candidate(case)
    shifted = apply_increment(case, base, {"type": "submit_candidate", "base_revision": base.revision,
        "edits": [{"path": "profile_model.py", "old": "from pydantic", "new": "# shift\nfrom pydantic"}]}, tmp_path / "shift")
    report = analyze_case(case, candidate_reference=shifted.reference)
    assert report["findings"][0]["line"] == 4
    assert report["source_binding"]["revision"] == shifted.revision
    assert report["source_binding"]["files"]["profile_model.py"] == hashlib.sha256(shifted.files["profile_model.py"]).hexdigest()
    repaired = apply_increment(case, shifted, {"type": "submit_candidate", "base_revision": shifted.revision,
        "edits": [{"path": "profile_model.py", "old": 'value: "str | None"', "new": 'value: "str | None" = None'}]}, tmp_path / "repair")
    assert analyze_case(case, candidate_reference=repaired.reference)["findings"] == []
    assert analyze_case(case)["findings"][0]["line"] == 3
