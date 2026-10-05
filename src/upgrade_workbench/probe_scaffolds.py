"""为已审查案例提供不含业务答案的探针启动脚手架。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from .cases import LoadedCase


@dataclass(frozen=True)
class ProbeScaffold:
    """精确绑定案例身份的 pytest conftest；模型不能修改这段初始化代码。"""

    scaffold_id: str
    case_id: str
    case_fingerprint: str
    code: str
    fixtures: tuple[str, ...]
    usage: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8")).hexdigest()

    def public(self) -> dict:
        return {
            "id": self.scaffold_id,
            "sha256": self.sha256,
            "fixtures": list(self.fixtures),
            "usage": self.usage,
            "scope": "Reviewed public application initialization only; no business assertion or acceptance answer.",
        }


_FLASKBB_SCAFFOLD = ProbeScaffold(
    scaffold_id="flaskbb-public-fixtures-v2",
    case_id="flaskbb-sqlalchemy-1-4-21-r2",
    case_fingerprint="b1e69a715e9076be2d1d875efb8b1aba79214cbc08348ac10aca49e8db5a2f5f",
    fixtures=(
        "application", "database", "request_context", "post_request_context",
        "default_groups", "default_settings", "user", "category", "forum", "topic", "post",
    ),
    usage=(
        "Write only the probe test and request the listed fixtures as function arguments. "
        "The host creates FlaskBB with TestingConfig and a writable temporary instance path. "
        "Do not create another app, replace db.session, or import hidden/final checks."
    ),
    code='''"""Host-owned public FlaskBB probe fixtures; no business expectation is encoded here."""

import pytest

from flaskbb.app import create_app
from flaskbb.configs.testing import TestingConfig
from flaskbb.extensions import db
from flaskbb.utils.populate import create_default_groups, create_default_settings


@pytest.fixture(autouse=True)
def application(tmp_path):
    """Use the public factory with an explicitly writable isolated instance directory."""
    instance_path = tmp_path / "flaskbb-instance"
    whooshee_path = tmp_path / "whoosh-index"

    class RuntimeConfig(TestingConfig):
        # 旧版 Flask-Whooshee 即使使用内存存储，也会先创建配置中的索引目录。
        WHOOSHEE_DIR = str(whooshee_path)

    app = create_app(RuntimeConfig, instance_path=str(instance_path))
    context = app.app_context()
    context.push()
    try:
        yield app
    finally:
        db.session.remove()
        context.pop()


@pytest.fixture()
def database():
    """Create and remove the public model schema inside the current probe environment."""
    db.create_all()
    try:
        yield db
    finally:
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def request_context(application):
    with application.test_request_context():
        yield


@pytest.fixture()
def post_request_context(application):
    with application.test_request_context(method="POST"):
        yield


@pytest.fixture()
def default_groups(database):
    return create_default_groups()


@pytest.fixture()
def default_settings(database):
    return create_default_settings()


from tests.fixtures.forum import *  # noqa: E402,F403
from tests.fixtures.user import *  # noqa: E402,F403
''',
)


_FLASKBB_BROWSING_SCAFFOLD = ProbeScaffold(
    scaffold_id="flaskbb-browsing-public-fixtures-v1",
    case_id="flaskbb-sqlalchemy-2-0-http",
    case_fingerprint="8e1b988b4ad4d2191284b3d2018ae89a5529115401fdefdcdd6c6a86101f8622",
    fixtures=("application", "request_context", "http_client", "http_get"),
    usage=(
        "The scaffold creates and tears down the real schema. HTTP probes use http_client(user_id=None) "
        "and http_get(client, path) without requesting request_context. Seed native models inside an "
        "explicit with application.test_request_context(base_url='http://localhost:5000') block, "
        "using create_default_groups/create_default_settings as needed; return scalar IDs/URLs. "
        "Leave that block before HTTP requests. ORM-only probes may request request_context. "
        "Native tests.conftest fixtures are not implicitly loaded. No post fixture exists. "
        "Only session protection is disabled for fixture authentication; CSRF and permissions remain real. "
        "No business assertion or scenario is supplied by this scaffold."
    ),
    code='''"""真实应用初始化；不向模型提供场景答案或保留跨请求的登录上下文。"""
import pytest
from flaskbb import create_app
from flaskbb.configs.testing import TestingConfig
from flaskbb.extensions import db


@pytest.fixture(autouse=True)
def application(tmp_path):
    class RuntimeConfig(TestingConfig):
        WHOOSHEE_DIR = str(tmp_path / "whoosh-index")
        SESSION_PROTECTION = None

    app = create_app(RuntimeConfig, instance_path=str(tmp_path / "instance"))
    with app.app_context():
        db.create_all()
    try:
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()


@pytest.fixture
def request_context(application):
    with application.test_request_context(base_url="http://localhost:5000"):
        yield


@pytest.fixture
def http_client(application):
    def make(user_id=None):
        client = application.test_client()
        if user_id is not None:
            with application.app_context():
                with client.session_transaction(base_url="http://localhost:5000") as session:
                    session["_user_id"] = str(user_id)
                    session["_fresh"] = True
        return client
    return make


@pytest.fixture
def http_get(application):
    def get(client, path):
        with application.app_context():
            return client.get(path, base_url="http://localhost:5000", follow_redirects=False)
    return get
''',
)


def scaffold_for(case: LoadedCase) -> ProbeScaffold | None:
    """身份变化时默认拒绝复用，避免旧启动假设静默漂移。"""
    # 公开测试输入剔除未消费的前端资产；只认可逐项审阅的完整指纹，不按名称猜测。
    public_aliases = {
        # 发布来源说明改变完整指纹；只认可已逐项核对的新公开夹具身份。
        ("flaskbb-sqlalchemy-1-4-21-r2", "3d4ba2509b46bf5d283726922b9f0d609160ed507e9b8fe4d9fbb2eeb611a6f7"):
            "b1e69a715e9076be2d1d875efb8b1aba79214cbc08348ac10aca49e8db5a2f5f",
        ("flaskbb-sqlalchemy-1-4-21-r2", "54167b665adb7a2071195fbda6dde5eb04b014ef94cec3a6410caaaa287ffaaa"):
            "8bbe842cd9faf54d26f9efed32bc5497ec57de1aee2d785f0e9009523ea831dc",
        ("flaskbb-sqlalchemy-1-4-21-r2", "5a1629585a0679c279718cb80b65291b6eaaf2da3906b3297120775b116c0b21"):
            "a9a9891ff212e2fc0391f4d3eab7ccf50625a07fe99d433ada76d736f3fecab4",
        ("flaskbb-werkzeug-2-1", "45df1510d27a934c829623469d0f5bc282873d4f263e19aa56657c7471235d2d"):
            "be4cd0df52e971e9ed56fcc8aa1f069a25664353edcd07735fde82891c3d476a",
        ("flaskbb-sqlalchemy-1-4-21-r2", "03eb1110c4bc4ef7ebdfc29250712cbf5faa987536d0e3a28528a62e7ec8f3f7"):
            "b1e69a715e9076be2d1d875efb8b1aba79214cbc08348ac10aca49e8db5a2f5f",
        ("flaskbb-sqlalchemy-1-4-21-r2", "9395969ead691dab3b040a5e0534b146f5333d558c0b2d254e79ef54f6c7b072"):
            "8bbe842cd9faf54d26f9efed32bc5497ec57de1aee2d785f0e9009523ea831dc",
        ("flaskbb-sqlalchemy-1-4-21-r2", "4015b287c80dc4b305f91ca279a79451c02cf8f445f5e2c91922f3540517e5e4"):
            "a9a9891ff212e2fc0391f4d3eab7ccf50625a07fe99d433ada76d736f3fecab4",
        ("flaskbb-werkzeug-2-1", "dc43d17c017bc34e46467db10e56807e910b8677de4ec016eae462cf164dfaa4"):
            "be4cd0df52e971e9ed56fcc8aa1f069a25664353edcd07735fde82891c3d476a",
    }
    original = public_aliases.get((case.manifest.case_id, case.fingerprint))
    if original is not None:
        scaffold = scaffold_for(replace(case, fingerprint=original))
        return replace(scaffold, case_fingerprint=case.fingerprint) if scaffold else None
    if (case.manifest.case_id, case.fingerprint) in {
        ("flaskbb-sqlalchemy-2-0-write", "0125121a0d7449288ceb5a395f6585b5b624569a8758e2a474a41379f652bd37"),
        ("flaskbb-sqlalchemy-2-0-permissions", "665fac5e7c057a4478384fa766991051a0f89b6c39d18e5cff96c08813b0b28f"),
    }:
        from .cases.manifest import read_verified_file

        name = "public-cli-scaffold.py"
        return ProbeScaffold(
            scaffold_id="flaskbb-native-history-write-fixtures-v1",
            case_id=case.manifest.case_id, case_fingerprint=case.fingerprint,
            code=read_verified_file(case.root, name, case.manifest.file_hashes[name]).decode("utf-8"),
            fixtures=("migration_database", "migration_cli"),
            usage="migration_database copies the old-native nonempty SQLite history at881dd22cab94, "
                  "including ordinary member reply/edit permissions, upstream default settings and lastseen. "
                  "migration_cli(path,args) uses a fresh ScriptInfo/app and temporary instance/Whooshee paths. "
                  "It does not create_all or stamp. Inspect real CLI errors and independent connections. "
                  "For HTTP writes create a fresh app on that same path after native upgrade; use real forms, "
                  "CSRF, user loader and permissions. The public write check documents setup. No implicit "
                  "current-schema fixtures, search indexing or password-login proof. No business assertions "
                  "are supplied; a fresh app is not a fresh process.",
        )
    if (case.manifest.case_id == "flaskbb-sqlalchemy-2-0-cli"
            and case.fingerprint == "414985fadbd3e2cfb9d2b9af386cab88b2b2b0e77a10e51bf7392791d3de1edc"):
        from .cases.manifest import read_verified_file

        name = "public-cli-scaffold.py"
        return ProbeScaffold(
            scaffold_id="flaskbb-native-cli-public-fixtures-v1",
            case_id=case.manifest.case_id, case_fingerprint=case.fingerprint,
            code=read_verified_file(case.root, name, case.manifest.file_hashes[name]).decode("utf-8"),
            fixtures=("migration_database", "migration_cli"),
            usage="migration_database is a fresh copy of the old-native nonempty SQLite seed at881dd22cab94. "
                  "migration_cli(path,args) invokes the real CLI with fresh ScriptInfo/app, copied ALEMBIC config "
                  "and temporary instance/Whooshee paths; returns the CliRunner result. No outer app context, "
                  "create_all or stamp. Inspect result.exit_code/exception and fresh database connections; "
                  "Config.print_stdout is not reliably in result.output. No business assertions are supplied. "
                  "Fresh app is not fresh process. The retained ORM/HTTP fixtures belong to their readable "
                  "checks and are not implicitly installed in this CLI scaffold.",
        )
    scaffold = _FLASKBB_SCAFFOLD
    if (case.manifest.case_id == _FLASKBB_BROWSING_SCAFFOLD.case_id
            and case.fingerprint == _FLASKBB_BROWSING_SCAFFOLD.case_fingerprint):
        return _FLASKBB_BROWSING_SCAFFOLD
    if (case.manifest.case_id == "flaskbb-werkzeug-2-1"
            and case.fingerprint == "be4cd0df52e971e9ed56fcc8aa1f069a25664353edcd07735fde82891c3d476a"):
        return replace(
            scaffold, scaffold_id="flaskbb-http-public-fixtures-v1",
            case_id=case.manifest.case_id, case_fingerprint=case.fingerprint,
            fixtures=tuple(name for name in scaffold.fixtures if name != "post")
                + ("admin_user", "super_moderator_user", "moderator_user", "Fred"),
            code=scaffold.code.replace(
                "WHOOSHEE_DIR = str(whooshee_path)",
                "WHOOSHEE_DIR = str(whooshee_path)\n        WTF_CSRF_ENABLED = False\n        SESSION_PROTECTION = None",
            ),
            usage=scaffold.usage + " HTTP tests may set test-client session _user_id and _fresh. "
                "CSRF and session protection are disabled only in the fixture. Use localhost:5000; "
                "real authorization decorators remain active. Do not follow rendered redirect pages.",
        )
    if (
        case.manifest.case_id == scaffold.case_id
        and case.fingerprint == scaffold.case_fingerprint
    ):
        return scaffold
    # 新合同目录只增加条款身份；原源码、检查、锁、环境和夹具保持逐字节相同。
    if (
        case.manifest.case_id == scaffold.case_id
        and case.fingerprint == "8bbe842cd9faf54d26f9efed32bc5497ec57de1aee2d785f0e9009523ea831dc"
    ):
        return replace(scaffold, case_fingerprint=case.fingerprint)
    # 修正公开声明使用新身份；冻结旧任务仍返回当时的原始说明。
    if (
        case.manifest.case_id == scaffold.case_id
        and case.fingerprint == "a9a9891ff212e2fc0391f4d3eab7ccf50625a07fe99d433ada76d736f3fecab4"
    ):
        return replace(scaffold, scaffold_id="flaskbb-public-fixtures-v3",
                       case_fingerprint=case.fingerprint,
                       fixtures=tuple(name for name in scaffold.fixtures if name != "post"))
    return None
