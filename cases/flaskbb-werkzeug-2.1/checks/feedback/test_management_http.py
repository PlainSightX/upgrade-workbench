"""公开 HTTP 行为：真实路由、登录身份及提交后的数据库状态。"""

import pytest
from flask import url_for
from flaskbb import create_app
from flaskbb.configs.testing import TestingConfig
from flaskbb.extensions import db
from flaskbb.user.models import Group, User
from flaskbb.forum.models import Report

pytest_plugins = ("tests.conftest",)


@pytest.fixture
def application(tmp_path):
    class RuntimeConfig(TestingConfig):
        WHOOSHEE_DIR = str(tmp_path / "whoosh")
        WTF_CSRF_ENABLED = False
        SESSION_PROTECTION = None

    app = create_app(RuntimeConfig, instance_path=str(tmp_path / "instance"))
    with app.app_context():
        yield app
        db.session.remove()


@pytest.fixture
def client(application, default_settings, admin_user):
    client = application.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(admin_user.id)
        session["_fresh"] = True
    return client


def send(client, endpoint, **values):
    return client.post(url_for("management." + endpoint, **values),
                       base_url="http://localhost:5000", follow_redirects=False)


def test_delete_user(client, user):
    identity = user.id
    response = send(client, "delete_user", user_id=identity)
    assert response.status_code == 302
    db.session.expire_all()
    assert User.query.get(identity) is None


def test_ban_user(client, user):
    identity = user.id
    response = send(client, "ban_user", user_id=identity)
    assert response.status_code == 302
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is True


def test_unban_user(client, user):
    user.ban()
    identity = user.id
    response = send(client, "unban_user", user_id=identity)
    assert response.status_code == 302
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is False


def test_delete_group(client):
    group = Group(name="temporary", description="disposable custom group")
    group.save()
    identity = group.id
    response = client.post(url_for("management.delete_group", group_id=identity),
                           data={"confirm": "yes"}, base_url="http://localhost:5000")
    assert response.status_code == 302
    db.session.expire_all()
    assert Group.query.get(identity) is None


def test_mark_report_read(client, admin_user):
    report = Report(reason="moderation request")
    report.save()
    identity = report.id
    response = client.post(url_for("management.report_markread", report_id=identity),
                           data={"confirm": "yes"}, base_url="http://localhost:5000")
    assert response.status_code == 302
    db.session.expire_all()
    saved = Report.query.get(identity)
    assert saved.zapped is not None and saved.zapped_by == admin_user.id


def test_delete_report(client):
    report = Report(reason="resolved report")
    report.save()
    identity = report.id
    response = client.post(url_for("management.delete_report", report_id=identity),
                           data={"confirm": "yes"}, base_url="http://localhost:5000")
    assert response.status_code == 302
    db.session.expire_all()
    assert Report.query.get(identity) is None
