"""独立保留批处理、权限与错误输入语义，不向 Solver 提供本文件。"""

import pytest
from flask import url_for
from flaskbb import create_app
from flaskbb.configs.testing import TestingConfig
from flaskbb.extensions import db
from flaskbb.user.models import Group, User
from flaskbb.forum.models import Report

pytest_plugins = ("tests.conftest",)
OPERATIONS = ("delete_user", "ban_user", "unban_user", "delete_group",
              "report_markread", "delete_report")


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
def login(application, default_settings):
    def as_user(actor):
        client = application.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(actor.id)
            session["_fresh"] = True
        return client
    return as_user


def send(client, operation, values=None, **payload):
    return client.post(url_for("management." + operation, **(values or {})),
                       base_url="http://localhost:5000", **payload)


def batch(client, operation, selected):
    response = send(client, operation, json={"ids": [str(selected)]})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == 200
    assert [row["id"] for row in body["data"]] == [selected]
    db.session.expire_all()


def reports():
    values = [Report(reason=reason) for reason in ("selected", "untouched")]
    for value in values:
        value.save()
    return [value.id for value in values]


def test_batch_delete_user(login, admin_user, user, Fred):
    selected, untouched = user.id, Fred.id
    batch(login(admin_user), "delete_user", selected)
    assert User.query.get(selected) is None
    assert User.query.get(untouched) is not None


def test_batch_ban_user(login, admin_user, user, Fred):
    selected, untouched = user.id, Fred.id
    batch(login(admin_user), "ban_user", selected)
    assert User.query.get(selected).primary_group.banned is True
    assert User.query.get(untouched).primary_group.banned is False


def test_batch_unban_user(login, admin_user, user, Fred):
    user.ban()
    Fred.ban()
    selected, untouched = user.id, Fred.id
    batch(login(admin_user), "unban_user", selected)
    assert User.query.get(selected).primary_group.banned is False
    assert User.query.get(untouched).primary_group.banned is True


def test_batch_delete_group(login, admin_user):
    first = Group(name="selected", description="custom")
    second = Group(name="untouched", description="custom")
    first.save()
    second.save()
    selected, untouched = first.id, second.id
    batch(login(admin_user), "delete_group", selected)
    assert Group.query.get(selected) is None
    assert Group.query.get(untouched) is not None


def test_batch_mark_report(login, admin_user):
    selected, untouched = reports()
    actor = admin_user.id
    batch(login(admin_user), "report_markread", selected)
    assert Report.query.get(selected).zapped_by == actor
    assert Report.query.get(selected).zapped is not None
    assert Report.query.get(untouched).zapped is None


def test_batch_delete_report(login, admin_user):
    selected, untouched = reports()
    batch(login(admin_user), "delete_report", selected)
    assert Report.query.get(selected) is None
    assert Report.query.get(untouched) is not None


def test_self_batch_protection(login, admin_user):
    client = login(admin_user)
    identity = admin_user.id
    for operation in ("delete_user", "ban_user"):
        response = send(client, operation, json={"ids": [str(identity)]})
        assert response.status_code == 200 and response.get_json()["data"] == []
        db.session.expire_all()
        assert User.query.get(identity).primary_group.banned is False


def test_single_self_ban_protection(login, admin_user):
    identity = admin_user.id
    response = send(login(admin_user), "ban_user", {"user_id": identity})
    assert response.status_code == 302
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is False


def test_standard_group_batch_protection(login, admin_user, default_groups):
    identity = default_groups[3].id
    response = send(login(admin_user), "delete_group", json={"ids": [str(identity)]})
    assert response.status_code == 200 and response.get_json()["status"] == 404
    db.session.expire_all()
    assert Group.query.get(identity) is not None


def test_normal_user_cannot_manage(login, user, Fred):
    client = login(user)
    identity = Fred.id
    group = Group(name="protected", description="custom")
    group.save()
    group_id = group.id
    report_id, other_report = reports()
    for operation in OPERATIONS:
        selected = group_id if operation == "delete_group" else report_id if "report" in operation else identity
        response = send(client, operation, json={"ids": [str(selected)]})
        assert response.status_code == 302
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is False
    assert Group.query.get(group_id) is not None
    assert Report.query.get(report_id).zapped is None
    assert Report.query.get(other_report).zapped is None


def test_malformed_json_stays_bad_request(login, admin_user, user):
    client = login(admin_user)
    identity = user.id
    for operation in OPERATIONS:
        response = send(client, operation, data='{"ids":', content_type="application/json")
        assert response.status_code == 400
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is False


def test_empty_json_selection_changes_nothing(login, admin_user, user):
    client = login(admin_user)
    identity = user.id
    report_id, _ = reports()
    for operation in OPERATIONS:
        response = send(client, operation, json={"ids": []})
        assert response.status_code == 200 and response.get_json()["status"] == 404
    db.session.expire_all()
    assert User.query.get(identity).primary_group.banned is False
    assert Report.query.get(report_id).zapped is None


def test_unknown_single_objects_stay_not_found(login, admin_user):
    client = login(admin_user)
    for operation in OPERATIONS:
        key = "group_id" if operation == "delete_group" else "report_id" if "report" in operation else "user_id"
        response = send(client, operation, {key: 999999})
        assert response.status_code == 404


def test_explicit_mark_all_route_preserved(login, admin_user):
    identities = reports()
    actor = admin_user.id
    response = send(login(admin_user), "report_markread")
    assert response.status_code == 302
    db.session.expire_all()
    assert all(Report.query.get(identity).zapped_by == actor for identity in identities)
    assert all(Report.query.get(identity).zapped is not None for identity in identities)
