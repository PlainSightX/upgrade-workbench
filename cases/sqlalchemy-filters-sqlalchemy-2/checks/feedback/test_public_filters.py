"""公开反馈检查：修复过程中可见；覆盖 README 的单模型过滤、显式连接后的多模型过滤、排序与分页。

每个测试新建内存 SQLite 引擎和会话，按固定数据集比较返回的主键序列。
模型使用 SQLAlchemy 1.4 与 2.0 都接受的 `sqlalchemy.orm.declarative_base` 写法。
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.hybrid import hybrid_method, hybrid_property
from sqlalchemy.orm import Session, declarative_base, relationship

from sqlalchemy_filters import apply_filters, apply_pagination, apply_sort


class _Common:
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(50), nullable=False)
    count = sa.Column(sa.Integer, nullable=True)

    @hybrid_property
    def count_square(self):
        return self.count * self.count

    @hybrid_method
    def three_times_count(self):
        return self.count * 3


Base = declarative_base(cls=_Common)


class Bar(Base):
    __tablename__ = "bar"
    foos = relationship("Foo", back_populates="bar")


class Foo(Base):
    __tablename__ = "foo"
    bar_id = sa.Column(sa.Integer, sa.ForeignKey("bar.id"), nullable=True)
    bar = relationship("Bar", back_populates="foos")


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([Bar(id=1, name="bar_1", count=5), Bar(id=2, name="bar_2", count=10),
                Bar(id=3, name="bar_3", count=None)])
    db.add_all([Foo(id=1, name="foo_1", count=2, bar_id=1), Foo(id=2, name="foo_2", count=3, bar_id=2),
                Foo(id=3, name="foo_1", count=4, bar_id=3), Foo(id=4, name="foo_4", count=None, bar_id=None)])
    db.commit()
    yield db
    db.close()
    engine.dispose()


def ids(query):
    return [row.id for row in query.all()]


def test_single_model_filter_with_operators(session):
    query = session.query(Bar)
    assert ids(apply_filters(query, [{"field": "name", "op": "==", "value": "bar_1"}])) == [1]
    assert ids(apply_filters(query, {"field": "count", "op": "is_null"})) == [3]
    filtered = apply_filters(query, [{"field": "count", "op": ">=", "value": 5}])
    assert ids(apply_filters(filtered, [{"field": "name", "op": "!=", "value": "bar_2"}])) == [1]


def test_hybrid_property_and_method_filters(session):
    query = session.query(Bar)
    assert ids(apply_filters(query, [{"field": "count_square", "op": ">=", "value": 26}])) == [2]
    assert ids(apply_filters(query, [{"field": "three_times_count", "op": "==", "value": 15}])) == [1]


def test_filters_on_explicit_join(session):
    query = session.query(Foo).join(Bar)
    spec = [{"model": "Foo", "field": "name", "op": "==", "value": "foo_1"},
            {"model": "Bar", "field": "count", "op": ">=", "value": 5}]
    assert ids(apply_filters(query, spec).order_by(Foo.id)) == [1]


def test_sort_by_field_and_direction(session):
    query = session.query(Foo)
    assert ids(apply_sort(query, [{"field": "name", "direction": "desc"},
                                  {"field": "id", "direction": "asc"}])) == [4, 2, 1, 3]


def test_sort_on_explicit_join(session):
    query = session.query(Foo).join(Bar)
    sorted_query = apply_sort(query, [{"model": "Bar", "field": "count", "direction": "desc"}])
    # SQLite 降序时 NULL 排在最后。
    assert ids(sorted_query) == [2, 1, 3]
    assert ids(apply_sort(query, [{"model": "Bar", "field": "name", "direction": "asc"}])) == [1, 2, 3]


def test_pagination_metadata(session):
    query = session.query(Foo).order_by(Foo.id)
    page, pagination = apply_pagination(query, page_number=2, page_size=3)
    assert ids(page) == [4]
    assert tuple(pagination) == (2, 3, 2, 4)
