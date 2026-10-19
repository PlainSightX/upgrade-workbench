"""独立验收检查：冻结后不向修复过程公开；只在任务终态后运行。

覆盖 README 与 `validate` docstring 列明的多值查询参数、多模型请求体与响应、exclude_none、
别名序列化、元组返回（状态码/响应头）、错误状态码与抛错配置、自定义校验器错误、路径参数错误、
`get_json_params` 透传，以及非模型返回值透传。模型只使用 Pydantic 1 与 2 语义一致的写法。
"""

from typing import List, Optional

import pytest
from flask import Flask, request
from pydantic import BaseModel, Field, validator

from flask_pydantic import ValidationError, validate
from flask_pydantic.exceptions import InvalidIterableOfModelsException


class TagQuery(BaseModel):
    tags: List[str]
    limit: int = 10


class Item(BaseModel):
    name: str
    price: float = Field(..., gt=0)


class Profile(BaseModel):
    user_id: int = Field(..., alias="userId")
    nickname: Optional[str] = None


class Named(BaseModel):
    name: str

    @validator("name")
    def no_spaces(cls, value):
        if " " in value:
            raise ValueError("name must not contain spaces")
        return value


def make_app(**config):
    app = Flask("acceptance_checks")
    app.config.update(config)

    @app.route("/tags")
    @validate()
    def tags(query: TagQuery):
        return {"tags": query.tags, "limit": query.limit}

    @app.route("/items", methods=["POST"])
    @validate(body=Item, request_body_many=True, response_many=True)
    def many_items():
        return [Item(name=item.name.upper(), price=item.price * 2) for item in request.body_params]

    @app.route("/profile")
    @validate(exclude_none=True, response_by_alias=True)
    def profile():
        return Profile(userId=5)

    @app.route("/profile-plain")
    @validate()
    def profile_plain():
        return Profile(userId=6, nickname="n")

    @app.route("/created", methods=["POST"])
    @validate()
    def created(body: Item):
        return body, 201, {"X-Item": body.name}

    @app.route("/headers-only", methods=["POST"])
    @validate()
    def headers_only(body: Item):
        return body, {"X-Only": "1"}

    @app.route("/named", methods=["POST"])
    @validate()
    def named(body: Named):
        return body

    @app.route("/user/<user_id>")
    @validate()
    def user(user_id: int):
        return {"user_id": user_id, "type": type(user_id).__name__}

    @app.route("/raw")
    @validate()
    def raw():
        return "plain text", 202

    @app.route("/bad-many")
    @validate(response_many=True)
    def bad_many():
        return [1, 2]

    return app


@pytest.fixture()
def client():
    return make_app().test_client()


def test_repeated_query_params_become_list(client):
    response = client.get("/tags?tags=a&tags=b&limit=3")
    assert response.status_code == 200
    assert response.get_json() == {"tags": ["a", "b"], "limit": 3}
    single = client.get("/tags?tags=only")
    assert single.get_json() == {"tags": ["only"], "limit": 10}


def test_request_body_many_and_response_many(client):
    response = client.post("/items", json=[{"name": "a", "price": 1.5}, {"name": "b", "price": 2}])
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == [{"name": "A", "price": 3.0}, {"name": "B", "price": 4.0}]


def test_request_body_many_rejects_non_array_and_invalid_item(client):
    not_array = client.post("/items", json=5)
    assert not_array.status_code == 400
    errors = not_array.get_json()["validation_error"]["body_params"]
    assert errors[0]["loc"] == ["root"]
    invalid = client.post("/items", json=[{"name": "a", "price": -1}])
    assert invalid.status_code == 400
    error = invalid.get_json()["validation_error"]["body_params"][0]
    assert error["loc"] == ["price"]


def test_exclude_none_and_alias_serialization(client):
    assert client.get("/profile").get_json() == {"userId": 5}
    assert client.get("/profile-plain").get_json() == {"user_id": 6, "nickname": "n"}


def test_tuple_returns_set_status_and_headers(client):
    response = client.post("/created", json={"name": "pen", "price": 2})
    assert response.status_code == 201
    assert response.headers["X-Item"] == "pen"
    assert response.get_json() == {"name": "pen", "price": 2.0}
    headers_only = client.post("/headers-only", json={"name": "cup", "price": 1})
    assert headers_only.status_code == 200 and headers_only.headers["X-Only"] == "1"


def test_custom_validator_error_is_reported_as_json(client):
    response = client.post("/named", json={"name": "two words"})
    assert response.status_code == 400
    error = response.get_json()["validation_error"]["body_params"][0]
    assert error["loc"] == ["name"]
    assert "name must not contain spaces" in error["msg"]


def test_path_param_validation_error(client):
    ok = client.get("/user/12")
    assert ok.get_json() == {"user_id": 12, "type": "int"}
    bad = client.get("/user/abc")
    assert bad.status_code == 400
    assert bad.get_json()["validation_error"]["path_params"][0]["loc"] == ["user_id"]


def test_configured_error_status_code():
    response = make_app(FLASK_PYDANTIC_VALIDATION_ERROR_STATUS_CODE=422).test_client().get("/tags")
    assert response.status_code == 422
    assert response.get_json()["validation_error"]["query_params"][0]["loc"] == ["tags"]


def test_configured_raise_exposes_error_groups():
    app = make_app(FLASK_PYDANTIC_VALIDATION_ERROR_RAISE=True)
    captured = []

    @app.errorhandler(ValidationError)
    def handle(error):
        captured.append(error)
        return {"handled": True}, 418

    response = app.test_client().post("/named", json={"name": "a b"})
    assert response.status_code == 418
    error = captured[0]
    assert error.body_params[0]["loc"] == ("name",) or error.body_params[0]["loc"] == ["name"]
    assert error.query_params is None and error.path_params is None


def test_silent_get_json_params_treat_missing_body_as_empty():
    app = Flask("silent_checks")

    @app.route("/silent", methods=["POST"])
    @validate(body=Named, get_json_params={"silent": True})
    def silent():
        return request.body_params

    response = app.test_client().post("/silent", data="not json", content_type="text/plain")
    assert response.status_code == 400
    assert response.get_json()["validation_error"]["body_params"][0]["loc"] == ["name"]


def test_non_model_return_passes_through_and_bad_many_raises(client):
    response = client.get("/raw")
    assert response.status_code == 202 and response.get_data(as_text=True) == "plain text"
    app = make_app()
    app.config["TESTING"] = True
    with pytest.raises(InvalidIterableOfModelsException):
        app.test_client().get("/bad-many")
