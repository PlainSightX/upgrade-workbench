"""公开反馈覆盖配置入口及基础鉴权，不替代独立边界验收。"""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from fastapi_jwt_auth import AuthJWT
from fastapi_jwt_auth.config import LoadConfig

import copy

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from fastapi_jwt_auth.exceptions import AuthJWTException


@pytest.fixture(autouse=True)
def isolated_configuration():
    """隔离类级配置，避免检查顺序改变鉴权状态。"""
    original = dict(AuthJWT.__dict__)
    yield
    for name in set(AuthJWT.__dict__) - set(original):
        delattr(AuthJWT, name)
    for name, value in original.items():
        if name.startswith('_') and not name.startswith('__'):
            setattr(AuthJWT, name, value)


@pytest.fixture
def configure():
    def load(**options):
        values = {'authjwt_secret_key': 'public-contract-test-key', **options}
        AuthJWT.load_config(lambda: list(copy.deepcopy(values).items()))
        return AuthJWT()
    return load


@pytest.fixture
def client_factory():
    clients = []

    def build():
        app = FastAPI()

        @app.exception_handler(AuthJWTException)
        def auth_error(request, error):
            return JSONResponse(status_code=error.status_code, content={'detail': error.message})

        @app.get('/protected')
        @app.post('/protected')
        def protected(authorize: AuthJWT = Depends()):
            authorize.jwt_required()
            return {'subject': authorize.get_jwt_subject()}

        client = TestClient(app)
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()



def test_defaults():
    value = LoadConfig()
    assert set(value.authjwt_token_location) == {'headers'}
    assert value.authjwt_access_token_expires == timedelta(minutes=15)
    assert value.authjwt_cookie_csrf_protect is True


def test_location_validation():
    assert list(LoadConfig(authjwt_token_location=['cookies']).authjwt_token_location) == ['cookies']
    with pytest.raises(ValidationError):
        LoadConfig(authjwt_token_location=['query'])


def test_expiry_true_rejected():
    with pytest.raises(ValidationError):
        LoadConfig(authjwt_access_token_expires=True)


def test_csrf_method_normalized():
    assert list(LoadConfig(authjwt_csrf_methods=['post', 'get']).authjwt_csrf_methods) == ['POST', 'GET']


def test_valid_access(configure, client_factory):
    auth = configure()
    token = auth.create_access_token(subject='alice')
    response = client_factory().get('/protected', headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 200
    assert response.json() == {'subject': 'alice'}


def test_missing_header(configure, client_factory):
    configure()
    assert client_factory().get('/protected').status_code == 401


def test_refresh_not_access(configure, client_factory):
    token = configure().create_refresh_token(subject='alice')
    assert client_factory().get('/protected', headers={'Authorization': f'Bearer {token}'}).status_code == 422


def test_callback_validation():
    with pytest.raises(ValidationError):
        AuthJWT.load_config(lambda: [('authjwt_cookie_secure', 'true')])
    with pytest.raises(TypeError):
        AuthJWT.load_config(lambda: 42)
