"""冻结业务边界，在两个求解任务结束前不向模型公开。"""

from datetime import timedelta

import jwt
import pytest
from pydantic import BaseModel, ValidationError
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



def test_strict_bool_and_integer_inputs():
    for values in ({'authjwt_denylist_enabled': 1}, {'authjwt_cookie_max_age': '60'}):
        with pytest.raises(ValidationError):
            LoadConfig(**values)


def test_string_trim_and_minimum():
    assert LoadConfig(authjwt_header_name=' X-Token ').authjwt_header_name == 'X-Token'
    for value in ('', '   '):
        with pytest.raises(ValidationError):
            LoadConfig(authjwt_secret_key=value)


def test_all_sequence_items_validated():
    for values in (
        {'authjwt_token_location': ['headers', 'query']},
        {'authjwt_denylist_token_checks': ['access', 'unknown']},
        {'authjwt_csrf_methods': ['POST', 'TRACE']},
        {'authjwt_token_location': [1]},
    ):
        with pytest.raises(ValidationError):
            LoadConfig(**values)


def test_set_and_tuple_input():
    for value in ({'headers', 'cookies'}, ('headers', 'cookies')):
        assert set(LoadConfig(authjwt_token_location=value).authjwt_token_location) == {'headers', 'cookies'}


def test_optional_none_remains_accepted():
    value = LoadConfig(authjwt_header_type=None, authjwt_decode_audience=None, authjwt_secret_key=None)
    assert value.authjwt_header_type is None and value.authjwt_secret_key is None


def test_expiry_false_keeps_no_exp(configure):
    auth = configure(authjwt_access_token_expires=False, authjwt_refresh_token_expires=False)
    for token in (auth.create_access_token(subject='alice'), auth.create_refresh_token(subject='alice')):
        assert 'exp' not in jwt.decode(token, 'public-contract-test-key', algorithms=['HS256'])


def test_seconds_and_timedelta(configure):
    for duration in (120, timedelta(seconds=120)):
        auth = configure(authjwt_access_token_expires=duration)
        claims = jwt.decode(auth.create_access_token(subject='alice'), 'public-contract-test-key', algorithms=['HS256'])
        assert claims['exp'] - claims['iat'] == 120
    with pytest.raises(ValidationError):
        LoadConfig(authjwt_refresh_token_expires=True)


def test_bad_signature_rejected(configure, client_factory):
    token = configure().create_access_token(subject='alice')
    configure(authjwt_secret_key='different-public-test-key')
    assert client_factory().get('/protected', headers={'Authorization': f'Bearer {token}'}).status_code == 422


def test_cookie_csrf(configure, client_factory):
    auth = configure(authjwt_token_location=['cookies'], authjwt_csrf_methods=['post'])
    token = auth.create_access_token(subject='alice')
    client = client_factory()
    client.cookies.set('access_token_cookie', token)
    assert client.post('/protected').status_code == 401
    assert client.post('/protected', headers={'X-CSRF-Token': 'wrong'}).status_code == 401
    csrf = jwt.decode(token, 'public-contract-test-key', algorithms=['HS256'])['csrf']
    response = client.post('/protected', headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200 and response.json()['subject'] == 'alice'


def test_denylist_reject(configure, client_factory):
    auth = configure(authjwt_denylist_enabled=True, authjwt_denylist_token_checks=['access'])
    AuthJWT.token_in_denylist_loader(lambda decrypted: decrypted['sub'] == 'revoked')
    client = client_factory()
    token = auth.create_access_token(subject='revoked')
    assert client.get('/protected', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    token = auth.create_access_token(subject='allowed')
    assert client.get('/protected', headers={'Authorization': f'Bearer {token}'}).status_code == 200


def test_custom_header(configure, client_factory):
    token = configure(authjwt_header_name=' X-Token ', authjwt_header_type=None).create_access_token(subject='alice')
    client = client_factory()
    assert client.get('/protected', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert client.get('/protected', headers={'X-Token': token}).status_code == 200


def test_model_callback():
    class Settings(BaseModel):
        authjwt_secret_key: str = 'model-callback-test-key'
        authjwt_cookie_secure: bool = True

    AuthJWT.load_config(lambda: Settings())
    assert AuthJWT._secret_key == 'model-callback-test-key' and AuthJWT._cookie_secure is True
