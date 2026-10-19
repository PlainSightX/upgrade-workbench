"""公开反馈检查：修复过程中可见；覆盖 README 例 1-5 的查询、路径、请求体、表单与响应序列化。

每个测试新建 Flask 应用并用 test_client 发请求；响应体按 JSON 解析后比较，不比较原始空白。
错误项只核对结构与 `loc`，不固定 Pydantic 版本相关的 `msg`/`type` 文本。
不使用 `from __future__ import annotations`：库按视图函数的 `__annotations__` 取模型类。
"""

from typing import Optional

import pytest
from flask import Flask, request
from pydantic import BaseModel

from flask_pydantic import validate


class QueryModel(BaseModel):
    age: int


class BodyModel(BaseModel):
    name: str
    nickname: Optional[str] = None


class FormModel(BaseModel):
    name: str
    nickname: Optional[str] = None


class ResponseModel(BaseModel):
    id: int
    age: int
    name: str
    nickname: Optional[str] = None


@pytest.fixture()
def client():
    app = Flask("public_checks")

    @app.route("/query", methods=["GET"])
    @validate()
    def by_query(query: QueryModel):
        return ResponseModel(id=0, age=query.age, name="abc", nickname="123")

    @app.route("/character/<character_id>/", methods=["GET"])
    @validate()
    def by_path(character_id: int):
        return ResponseModel(id=character_id, age=character_id * 2, name="c")

    @app.route("/body", methods=["POST"])
    @validate()
    def by_body(body: BodyModel):
        return ResponseModel(id=1, age=30, name=body.name, nickname=body.nickname)

    @app.route("/form", methods=["POST"])
    @validate()
    def by_form(form: FormModel):
        return ResponseModel(id=2, age=40, name=form.name, nickname=form.nickname)

    @app.route("/both", methods=["POST"])
    @validate(body=BodyModel, query=QueryModel, on_success_status=201)
    def both():
        return ResponseModel(id=3, age=request.query_params.age,
                             name=request.body_params.name, nickname=request.body_params.nickname)

    return app.test_client()


def test_query_model_parses_and_serializes_response(client):
    response = client.get("/query?age=20")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == {"id": 0, "age": 20, "name": "abc", "nickname": "123"}


def test_query_validation_errors_return_400(client):
    missing = client.get("/query")
    assert missing.status_code == 400
    errors = missing.get_json()["validation_error"]["query_params"]
    assert len(errors) == 1 and errors[0]["loc"] == ["age"]
    assert isinstance(errors[0]["msg"], str) and isinstance(errors[0]["type"], str)
    wrong = client.get("/query?age=abc")
    assert wrong.status_code == 400
    assert wrong.get_json()["validation_error"]["query_params"][0]["loc"] == ["age"]


def test_path_parameter_is_converted(client):
    response = client.get("/character/4/")
    assert response.status_code == 200
    assert response.get_json() == {"id": 4, "age": 8, "name": "c", "nickname": None}


def test_json_body_model(client):
    response = client.post("/body", json={"name": "Geralt", "nickname": "White Wolf"})
    assert response.status_code == 200
    assert response.get_json() == {"id": 1, "age": 30, "name": "Geralt", "nickname": "White Wolf"}
    invalid = client.post("/body", json={"nickname": "x"})
    assert invalid.status_code == 400
    assert invalid.get_json()["validation_error"]["body_params"][0]["loc"] == ["name"]


def test_form_model(client):
    response = client.post("/form", data={"name": "Triss"})
    assert response.status_code == 200
    assert response.get_json() == {"id": 2, "age": 40, "name": "Triss", "nickname": None}


def test_decorator_models_and_success_status(client):
    response = client.post("/both?age=7", json={"name": "Yen"})
    assert response.status_code == 201
    assert response.get_json() == {"id": 3, "age": 7, "name": "Yen", "nickname": None}
