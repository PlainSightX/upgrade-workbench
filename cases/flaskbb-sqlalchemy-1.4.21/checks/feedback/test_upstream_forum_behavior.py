"""从上游既有测试中选出的公开行为回归。"""

import pytest
from flaskbb import create_app
from flaskbb.configs.testing import TestingConfig as Config
from flaskbb.forum.models import Post, Topic

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


def test_forum_update_last_post(topic, user):
    post = Post(content="Test Content 2")
    post.save(topic=topic, user=user)

    assert topic.forum.last_post == post

    post.delete()
    topic.forum.update_last_post()

    assert topic.forum.last_post == topic.first_post


def test_topic_save(forum, user):
    post = Post(content="Test Content")
    topic = Topic(title="Test Title")

    assert forum.last_post_id is None
    assert forum.post_count == 0
    assert forum.topic_count == 0

    topic.save(forum=forum, post=post, user=user)

    assert topic.title == "Test Title"
    assert topic.first_post_id == post.id
    assert topic.last_post_id == post.id
    assert forum.last_post_id == post.id
    assert forum.post_count == 1
    assert forum.topic_count == 1


def test_post_save(topic, user):
    post = Post(content="Test Content")
    post.save(topic=topic, user=user)

    post.content = "Test Edit Content"
    post.save()

    assert post.content == "Test Edit Content"
    assert topic.user.post_count == 2
    assert topic.post_count == 2
    assert topic.last_post == post
    assert topic.forum.post_count == 2


def test_hiding_post_updates_counts(forum, topic, user):
    new_post = Post(content="spam")
    new_post.save(user=user, topic=topic)
    new_post.hide(user)

    assert user.post_count == 1
    assert topic.post_count == 1
    assert forum.post_count == 1
    assert topic.last_post != new_post
    assert forum.last_post != new_post

    new_post.unhide()
    assert topic.post_count == 2
    assert user.post_count == 2
    assert forum.post_count == 2
    assert topic.last_post == new_post
    assert forum.last_post == new_post


def test_hiding_topic_updates_counts(forum, topic, user):
    assert forum.post_count == 1

    topic.hide(user)
    assert forum.post_count == 0
    assert topic.hidden_by == user
    assert forum.last_post is None

    topic.unhide()
    assert forum.post_count == 1
    assert topic.hidden_by is None
    assert forum.last_post == topic.last_post
