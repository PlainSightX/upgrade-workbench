"""独立验收检查：模型不可见，只在任务终态后运行。

覆盖 README 与上游测试描述、但公开反馈未直接覆盖的行为：嵌套预加载 schema、
自动预加载、动态关系上的 smart_query、嵌套逻辑过滤、别名过滤、序列化、检视属性、
时间戳和事务边界。查询次数以引擎事件计数。
"""

from __future__ import annotations

import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.hybrid import hybrid_method, hybrid_property
from sqlalchemy.orm import Session, aliased, declarative_base, relationship

from sqlalchemy_mixins import (
    JOINED,
    SUBQUERY,
    AllFeaturesMixin,
    TimestampsMixin,
    smart_query,
)

Base = declarative_base()


class BaseModel(Base, AllFeaturesMixin):
    __abstract__ = True


class User(BaseModel):
    __tablename__ = "user"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    posts = relationship("Post", back_populates="user")
    # README 示例：lazy="dynamic" 关系返回查询对象，可再交给 smart_query。
    comments_ = relationship("Comment", lazy="dynamic")


class Post(BaseModel):
    __tablename__ = "post"
    id = sa.Column(sa.Integer, primary_key=True)
    body = sa.Column(sa.String)
    user_id = sa.Column(sa.Integer, sa.ForeignKey("user.id"))
    archived = sa.Column(sa.Boolean, default=False)
    user = relationship("User", back_populates="posts")
    author_view = relationship("User", viewonly=True)
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

    @hybrid_method
    def is_commented_by_user(cls, user, mapper=None):
        mapper = mapper or cls
        return mapper.comments.any(Comment.user_id == user.id)


class Comment(BaseModel):
    __tablename__ = "comment"
    id = sa.Column(sa.Integer, primary_key=True)
    body = sa.Column(sa.String)
    user_id = sa.Column(sa.Integer, sa.ForeignKey("user.id"))
    post_id = sa.Column(sa.Integer, sa.ForeignKey("post.id"))
    rating = sa.Column(sa.Integer)
    user = relationship("User")
    post = relationship("Post", back_populates="comments")


StampBase = declarative_base()


class Stamped(StampBase, AllFeaturesMixin, TimestampsMixin):
    __tablename__ = "stamped"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)


def _engine(url="sqlite://"):
    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)
    StampBase.metadata.create_all(engine)
    counter = [0]

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter[0] += 1

    return engine, counter


@pytest.fixture()
def db():
    """内存库会话；counter[0] 为已执行 SQL 语句数。"""
    engine, counter = _engine()
    session = Session(engine)
    BaseModel.set_session(session)
    Stamped.set_session(session)
    yield session, counter
    BaseModel.set_session(None)
    Stamped.set_session(None)
    session.close()
    engine.dispose()


def seed(session):
    """u1 Bill、u2 Alex、u3 Bishop；p11 已归档；cm99 没有用户和帖子。"""
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
    session.close()


def _check_nested_schema(db, schema):
    """帖子与作者一条联结查询，评论及其作者一条子查询，访问时不再发 SQL。"""
    session, counter = db
    seed(session)
    counter[0] = 0
    posts = Post.with_(schema).filter(Post.id.in_([11, 21])).order_by(Post.id).all()
    assert counter[0] == 2
    observed = [(p.user.name, sorted((c.body, c.user.name) for c in p.comments)) for p in posts]
    assert observed == [("Bill", [("cm11", "Bill"), ("cm12", "Alex")]),
                        ("Alex", [("cm21", "Bill")])]
    assert counter[0] == 2


def test_with_nested_schema_string_keys(db):
    _check_nested_schema(db, {"user": JOINED, "comments": (SUBQUERY, {"user": JOINED})})


def test_with_nested_schema_attribute_keys(db):
    _check_nested_schema(db, {Post.user: JOINED,
                              Post.comments: (SUBQUERY, {Comment.user: JOINED})})


def test_with_subquery_dotted_string_path(db):
    session, counter = db
    seed(session)
    counter[0] = 0
    users = User.with_subquery("posts", "posts.comments").order_by(User.id).all()
    assert counter[0] == 3
    bodies = {u.name: sorted(c.body for p in u.posts for c in p.comments) for u in users}
    assert bodies == {"Bill": ["cm11", "cm12"], "Alex": ["cm21", "cm22"], "Bishop": []}
    assert counter[0] == 3


def test_where_auto_eager_loads_joined_relations(db):
    session, counter = db
    seed(session)
    counter[0] = 0
    comments = Comment.where(post___public=True, post___user___name__like="Al%").all()
    assert counter[0] == 1
    assert sorted((c.id, c.post.body, c.post.user.name) for c in comments) == [
        (21, "p21", "Alex"), (22, "p22", "Alex")]
    assert counter[0] == 1


def test_smart_query_function_with_schema_and_sort(db):
    session, counter = db
    seed(session)
    counter[0] = 0
    result = smart_query(Comment.query, filters={"user__isnull": False},
                         sort_attrs=["-rating", "id"],
                         schema={"post": {"user": JOINED}}).all()
    assert [c.id for c in result] == [22, 12, 11, 21]
    assert [c.post.user.name for c in result] == ["Alex", "Bill", "Bill", "Alex"]
    assert counter[0] == 1


def test_smart_query_on_dynamic_relationship(db):
    session, _ = db
    seed(session)
    bill = session.get(User, 1)
    public = smart_query(bill.comments_, filters={"post___public": True},
                         sort_attrs=["-rating"]).all()
    assert [c.id for c in public] == [21]
    everything = smart_query(bill.comments_, sort_attrs=["-id"]).all()
    assert [c.id for c in everything] == [21, 11]


def test_nested_logical_filters_and_hybrid_method(db):
    session, _ = db
    seed(session)
    either = Comment.smart_query(filters={sa.or_: {"rating__gt": 2, "user___name": "Alex"}}).all()
    assert sorted(c.id for c in either) == [12, 22]
    both = Comment.smart_query(filters={sa.and_: [
        {sa.or_: {"rating": 1, "rating__ge": 3}},
        {"post___public": True},
    ]}).all()
    assert sorted(c.id for c in both) == [21, 22]
    alex = session.get(User, 2)
    assert sorted(p.id for p in Post.where(is_commented_by_user=alex).all()) == [11]


def test_filter_and_order_expressions_on_alias(db):
    session, _ = db
    seed(session)
    alias = aliased(Post)
    rows = session.query(alias).filter(*alias.filter_expr(user_id=2, body__like="p2%")) \
        .order_by(*alias.order_expr("-id")).all()
    assert [p.id for p in rows] == [22, 21]
    with pytest.raises(KeyError):
        Post.filter_expr(missing__gt=1)
    with pytest.raises(KeyError):
        Post.order_expr("missing")


def test_active_record_collection_helpers(db):
    session, _ = db
    seed(session)
    assert sorted(u.name for u in User.all()) == ["Alex", "Bill", "Bishop"]
    assert User.first() is not None
    Post.destroy(11, 12)
    assert sorted(p.id for p in Post.all()) == [21, 22]
    with pytest.raises(KeyError):
        User.create(not_a_column="x")
    with pytest.raises(KeyError):
        User.find(1).update(not_a_column="x")


def test_inspection_attributes(db):
    assert set(Post.columns) == {"id", "body", "user_id", "archived"}
    assert set(Post.relations) == {"user", "author_view", "comments"}
    assert set(Post.settable_relations) == {"user", "comments"}
    assert Post.hybrid_properties == ["public"]
    assert Post.hybrid_methods == ["is_commented_by_user"]
    assert Post.primary_keys == ["id"]
    assert set(User.settable_attributes) == {"id", "name", "posts", "comments_"}


def test_to_dict_nested_hybrid_and_exclude(db):
    session, _ = db
    seed(session)
    comment = session.get(Comment, 11)
    assert comment.to_dict(nested=True, exclude=["body"]) == {
        "id": 11, "user_id": 1, "post_id": 11, "rating": 1,
        "user": {"id": 1, "name": "Bill"},
        "post": {"id": 11, "body": "p11", "user_id": 1, "archived": True},
    }
    post = session.get(Post, 21)
    flat = post.to_dict(hybrid_attributes=True)
    assert flat == {"id": 21, "body": "p21", "user_id": 2, "archived": False, "public": True}


def test_timestamps_are_populated(db):
    stamped = Stamped.create(name="first")
    assert isinstance(stamped.created_at, datetime.datetime)
    assert isinstance(stamped.updated_at, datetime.datetime)


def test_save_flushes_but_caller_owns_commit(tmp_path):
    """save/create 只 flush；提交与回滚仍由调用方的会话决定，另一连接可验证。"""
    engine, _ = _engine(f"sqlite:///{tmp_path / 'tx.db'}")
    session, other = Session(engine), Session(engine)
    BaseModel.set_session(session)
    try:
        User.create(name="kept")
        assert other.query(User).count() == 0
        session.commit()
        other.rollback()
        assert [u.name for u in other.query(User).all()] == ["kept"]
        User.create(name="discarded")
        session.rollback()
        other.rollback()
        assert [u.name for u in other.query(User).all()] == ["kept"]
        assert [u.name for u in User.all()] == ["kept"]
    finally:
        BaseModel.set_session(None)
        session.close()
        other.close()
        engine.dispose()
