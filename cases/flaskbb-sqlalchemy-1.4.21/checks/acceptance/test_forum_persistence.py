"""独立检查迁移后的论坛持久化、查询和 autoflush 边界。"""

import pytest
from flaskbb import create_app
from flaskbb.configs.testing import TestingConfig as Config
from flaskbb.extensions import db
from flaskbb.forum.models import Forum, Post, Topic
from flaskbb.user.models import Group

pytest_plugins = ("tests.conftest",)


@pytest.fixture
def application(tmp_path):
    """运行时状态写入容器临时目录，源码快照继续保持只读。"""
    class RuntimeConfig(Config):
        # 旧版 Flask-Whooshee 即使使用内存索引，也会先创建这个目录。
        WHOOSHEE_DIR = str(tmp_path / "whoosh_index")

    app = create_app(RuntimeConfig, instance_path=str(tmp_path / "instance"))
    context = app.app_context()
    context.push()
    yield app
    context.pop()


def test_topic_and_first_post_survive_session_reload(forum, user):
    topic = Topic(title="Reloadable topic")
    post = Post(content="first body")
    topic.save(forum=forum, user=user, post=post)
    topic_id, post_id, forum_id = topic.id, post.id, forum.id

    db.session.expire_all()
    stored = Topic.query.filter_by(id=topic_id).one()
    stored_forum = Forum.query.filter_by(id=forum_id).one()

    assert stored.username == user.username
    assert stored.first_post_id == post_id
    assert stored.last_post_id == post_id
    assert stored.post_count == 1
    assert stored_forum.topic_count == 1
    assert stored_forum.post_count == 1
    assert stored_forum.last_post_id == post_id


def test_second_post_updates_persisted_last_post_and_counts(topic, user):
    first_post_id = topic.first_post_id
    second = Post(content="second body")
    second.save(user=user, topic=topic)
    topic_id, forum_id, second_id = topic.id, topic.forum_id, second.id

    db.session.expire_all()
    stored = Topic.query.filter_by(id=topic_id).one()
    stored_forum = Forum.query.filter_by(id=forum_id).one()

    assert stored.first_post_id == first_post_id
    assert stored.last_post_id == second_id
    assert stored.post_count == 2
    assert stored_forum.last_post_id == second_id
    assert stored_forum.post_count == 2


def test_hidden_post_is_excluded_unless_query_explicitly_includes_it(topic, user):
    hidden = Post(content="hidden body")
    hidden.save(user=user, topic=topic)
    hidden.hide(user)
    hidden_id = hidden.id

    db.session.expire_all()
    assert Post.query.filter(Post.id == hidden_id).first() is None
    assert (
        Post.query.with_hidden().filter(Post.id == hidden_id).one().id
        == hidden_id
    )


def test_hidden_topic_is_excluded_unless_query_explicitly_includes_it(topic, user):
    topic.hide(user)
    topic_id = topic.id

    db.session.expire_all()
    assert Topic.query.filter(Topic.id == topic_id).first() is None
    assert (
        Topic.query.with_hidden().filter(Topic.id == topic_id).one().id
        == topic_id
    )


def test_relationship_setup_does_not_flush_incomplete_forum(database, category):
    forum = Forum(title="Members only", category=category)

    # 读取 Group 会触发 ORM 查询；未完成对象不能在关系设置中被提前写入。
    forum.groups = Group.query.filter(Group.guest == False).all()  # noqa: E712
    forum.save()

    db.session.expire_all()
    stored = Forum.query.filter_by(id=forum.id).one()
    assert stored.category_id == category.id
    assert stored.title == "Members only"
