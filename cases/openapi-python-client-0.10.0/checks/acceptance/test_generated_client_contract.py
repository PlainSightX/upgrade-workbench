"""最终验收真实生成物：不调用远端API，不提前向修复循环提供结果。"""

import importlib
import json

import httpx
from openapi_python_client import MetaType, create_new_client
from openapi_python_client.config import Config


def _generate(tmp_path, monkeypatch, package_name):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    document = {
        "openapi": "3.0.2",
        "info": {"title": "People API", "version": "2.1.0"},
        "paths": {
            "/people/{person_id}": {"get": {
                "operationId": "get_person", "tags": ["people"],
                "parameters": [{"name": "person_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "One person", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Person"}}}}},
            }},
        },
        "components": {"schemas": {"Person": {
            "type": "object", "required": ["id", "display-name"],
            "properties": {"id": {"type": "integer"}, "display-name": {"type": "string"}, "nickname": {"type": "string", "nullable": True}},
        }}},
    }
    source = tmp_path / "api.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    errors = create_new_client(url=None, path=source, meta=MetaType.NONE, config=Config(package_name_override=package_name))
    assert errors == []
    package = tmp_path / package_name
    for path in package.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    return package


def test_generated_model_preserves_alias_optional_and_extra_values(tmp_path, monkeypatch):
    _generate(tmp_path, monkeypatch, "roundtrip_client")
    model = importlib.import_module("roundtrip_client.models.person").Person
    payload = {"id": 7, "display-name": "Lee", "nickname": None, "external": {"k": [1, 2]}}
    assert model.from_dict(payload).to_dict() == payload
    omitted = {"id": 8, "display-name": "Lin"}
    assert model.from_dict(omitted).to_dict() == omitted


def test_generated_endpoint_builds_path_and_parses_model_response(tmp_path, monkeypatch):
    _generate(tmp_path, monkeypatch, "endpoint_client")
    endpoint = importlib.import_module("endpoint_client.api.people.get_person")
    client = importlib.import_module("endpoint_client.client").Client(base_url="https://example.invalid")
    kwargs = endpoint._get_kwargs(person_id=42, client=client)
    assert kwargs["url"] == "https://example.invalid/people/42"
    payload = {"id": 42, "display-name": "Han"}
    response = endpoint._parse_response(response=httpx.Response(200, json=payload))
    assert response.to_dict() == payload
    assert endpoint._parse_response(response=httpx.Response(404, json={})) is None


def test_project_metadata_uses_requested_name_and_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "api.json"
    source.write_text(json.dumps({"openapi": "3.0.2", "info": {"title": "Actual Title", "version": "1.0.0"}, "paths": {}}), encoding="utf-8")
    config = Config(project_name_override="custom-api", package_name_override="custom_api", package_version_override="2.5.0")
    assert create_new_client(url=None, path=source, meta=MetaType.POETRY, config=config) == []
    project = tmp_path / "custom-api"
    metadata = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "custom-api"' in metadata
    assert 'version = "2.5.0"' in metadata
    assert (project / "custom_api" / "__init__.py").exists()
    assert "Actual Title" in (project / "README.md").read_text(encoding="utf-8")


def test_unsupported_major_version_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "api.json"
    source.write_text(json.dumps({"openapi": "2.0.0", "info": {"title": "Old API", "version": "1.0.0"}, "paths": {}}), encoding="utf-8")
    errors = create_new_client(url=None, path=source, meta=MetaType.NONE, config=Config())
    assert len(errors) == 1
    assert errors[0].header == "openapi-python-client only supports OpenAPI 3.x"
    assert list(tmp_path.iterdir()) == [source]
