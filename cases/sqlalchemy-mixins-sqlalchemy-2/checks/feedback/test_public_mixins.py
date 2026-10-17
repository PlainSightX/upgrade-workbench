"""公开反馈检查：修复过程中可见；覆盖 README 列明的基本 ActiveRecord、预加载与查询行为。

每个测试新建内存 SQLite 引擎和会话；查询次数通过引擎事件计数，只统计真正发往数据库的语句。
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Session, declarative_base, relationship

from sqlalchemy_mixins import AllFeaturesMixin, ModelNotFoundError

Base = declarative_base()


class BaseModel(Base, AllFeaturesMixin):
    __abstract__ = True


class User(BaseModel):
    __tablename__ = "user"
    __repr_attrs__ = ["name"]
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    posts = relationship("Post", back_populates="user")


class Post(BaseModel):
    __tablename__ = "post"
    __repr_attrs__ = ["body", "user"]
    # 嵌套 User 的 repr 为 16 个字符，放宽上限以检查配置被读取而非截断规则。
    __repr_max_length__ = 25
    id = sa.Column(sa.Integer, primary_key=True)
    body = sa.Column(sa.String)
    user_id = sa.Column(sa.Integer, sa.ForeignKey("user.id"))
    archived = sa.Column(sa.Boolean, default=False)
    user = relationship("User", back_populates="posts")
    comments = relationship("Comment", back_populates="post")

    @hybrid_property
    def public(self):
        return not self.archived

    @public.setter
    def public(self, value):
        self.archived = not value

    @public.expression
    def public(cls):
        return ~cls.archived


class Comment(BaseModel):
    __tablename__ = "comment"
    __repr_attrs__ = ["body"]
    id = sa.Column(sa.Integer, primary_key=True)
    body = sa.Column(sa.String)
    user_id = sa.Column(sa.Integer, sa.ForeignKey("user.id"))
    post_id = sa.Column(sa.Integer, sa.ForeignKey("post.id"))
    rating = sa.Column(sa.Integer)
    user = relationship("User")
    post = relationship("Post", back_populates="comments")


@pytest.fixture()
def db():
    """返回 (session, counter)；counter[0] 为当前已执行 SQL 语句数。"""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    counter = [0]

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter[0] += 1

    session = Session(engine)
    BaseModel.set_session(session)
    yield session, counter
    BaseModel.set_session(None)
    session.close()
    engine.dispose()


def seed(session):
    """固定数据：u1 Bill、u2 Alex、u3 Bishop；p11 已归档；cm99 没有用户和帖子。"""
    u1, u2, u3 = User(id=1, name="Bill"), User(id=2, name="Alex"), User(id=3, name="Bishop")
    p11 = Post(id=11, body="p11", user=u1, archived=True)
    p12 = Post(id=12, body="p12", user=u1)
    p21 = Post(id=21, body="p21", user=u2)
    p22 = Post(id=22, body="p22", user=u2)
    comments = [
        Comment(id=11, body="cm11", user=u1, post=p11, rating=1),
        Comment(id=12, body="cm12", user=u2, post=p11, rating=2),
        Comment(id=21, body="cm21", user=u1, post=p21, rating=1),
        Comment(id=22, body="cm22", user=u3, post=p22, rating=3),
        Comment(id=99, body="cm99"),
    ]
    session.add_all([u1, u2, u3, p11, p12, p21, p22, *comments])
    session.commit()
    # 关闭后重新加载，避免身份映射中已加载的属性掩盖查询次数。
    session.close()


def test_create_update_delete_persist(db):
    session, _ = db
    user = User.create(name="Zed")
    assert user.id is not None
    assert session.query(User).filter_by(name="Zed").one() is user

    post = Post.create(body="draft", user=user, public=False)
    assert post.archived is True
    post.update(body="final", public=True)
    session.expire_all()
    stored = session.query(Post).filter_by(id=post.id).one()
    assert (stored.body, stored.archived, stored.user.name) == ("final", False, "Zed")

    stored.delete()
    assert session.query(Post).count() == 0


def test_find_and_find_or_fail(db):
    session, _ = db
    seed(session)
    assert User.find(2).name == "Alex"
    assert User.find(123456) is None
    assert User.find_or_fail(1).name == "Bill"
    with pytest.raises(ModelNotFoundError):
        User.find_or_fail(123456)


def test_with_joined_string_path_loads_relation(db):
    session, counter = db
    seed(session)
    counter[0] = 0
    comments = Comment.with_joined("user").order_by(Comment.id).all()
    assert counter[0] == 1
    assert [c.user.name if c.user else None for c in comments] == [
        "Bill", "Alex", "Bill", "Bishop", None]
    # 关系已经随主查询加载，访问时不应再发 SQL。
    assert counter[0] == 1


def test_where_filters_through_relation(db):
    session, _ = db
    seed(session)
    by_author = Comment.where(post___user___name="Alex").all()
    assert sorted(c.id for c in by_author) == [21, 22]
    rated = Comment.where(rating__gt=1, user___name__like="B%").all()
    assert [c.id for c in rated] == [22]


def test_sort_by_relation_and_descending(db):
    session, _ = db
    seed(session)
    ordered = Comment.sort("user___name", "-rating", "id").all()
    # SQLite 升序时 NULL 在前；外连接保留没有用户的评论。
    assert [c.id for c in ordered] == [99, 12, 11, 21, 22]


def test_repr_and_flat_to_dict(db):
    session, _ = db
    seed(session)
    user = session.get(User, 1)
    assert repr(user) == "<User #1 'Bill'>"
    assert user.to_dict() == {"id": 1, "name": "Bill"}
    post = session.get(Post, 11)
    assert repr(post) == "<Post #11 body:'p11' user:<User #1 'Bill'>>"
