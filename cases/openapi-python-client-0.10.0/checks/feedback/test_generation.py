"""公开反馈：真实文档解析、模板生成和错误返回，禁用网络输入。"""

import json

from openapi_python_client import MetaType, create_new_client
from openapi_python_client.config import Config


def test_empty_configuration_keeps_default_options():
    config = Config()
    assert config.field_prefix == "field_"
    assert config.class_overrides == {}
    assert config.project_name_override is None
    assert config.package_name_override is None


def test_local_document_generates_model_and_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = {
        "openapi": "3.0.2",
        "info": {"title": "Orchard", "version": "1.0.0"},
        "paths": {"/status": {"get": {"operationId": "get_status", "responses": {"200": {"description": "OK"}}}}},
        "components": {"schemas": {"Item": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}},
    }
    source = tmp_path / "api.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    errors = create_new_client(url=None, path=source, meta=MetaType.NONE, config=Config())
    assert errors == []
    package = tmp_path / "orchard_client"
    assert (package / "models" / "item.py").is_file()
    assert (package / "api" / "default" / "get_status.py").is_file()
    assert (package / "client.py").is_file()
    for path in package.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_invalid_document_returns_error_without_generated_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "invalid.json"
    source.write_text('{"openapi": "not-a-version", "paths": {}}', encoding="utf-8")
    errors = create_new_client(url=None, path=source, meta=MetaType.NONE, config=Config())
    assert len(errors) == 1
    assert errors[0].header == "Failed to parse OpenAPI document"
    assert list(tmp_path.iterdir()) == [source]
