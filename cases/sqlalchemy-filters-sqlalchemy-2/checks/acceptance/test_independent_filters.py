"""独立验收检查：冻结后不向修复过程公开；只在任务终态后运行。

覆盖 README 列明的自动连接（过滤、排序、限制加载）、关闭自动连接、嵌套布尔函数、字段/聚合/
select_from 查询、操作符、空值排序、限制加载、分页边界与错误类型。
每个测试新建内存 SQLite 引擎；限制加载通过 SQL 语句文本和实例 `__dict__` 判断哪些列被加载。
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import event, func
from sqlalchemy.ext.hybrid import hybrid_method, hybrid_property
from sqlalchemy.orm import Session, declarative_base, relationship

from sqlalchemy_filters import apply_filters, apply_loads, apply_pagination, apply_sort
from sqlalchemy_filters.exceptions import (
    BadFilterFormat,
    BadQuery,
    BadSortFormat,
    BadSpec,
    FieldNotFound,
    InvalidPage,
)


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


class Qux(Base):
    __tablename__ = "qux"


@pytest.fixture()
def db():
    """返回 (session, statements)；statements 记录实际发往数据库的 SQL。"""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all([Bar(id=1, name="bar_1", count=5), Bar(id=2, name="bar_2", count=10),
                     Bar(id=3, name="bar_3", count=None)])
    session.add_all([Foo(id=1, name="foo_1", count=2, bar_id=1), Foo(id=2, name="foo_2", count=3, bar_id=2),
                     Foo(id=3, name="foo_1", count=4, bar_id=3), Foo(id=4, name="foo_4", count=None, bar_id=None)])
    session.add(Qux(id=1, name="qux_1", count=1))
    session.commit()
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    yield session, statements
    session.close()
    engine.dispose()


def ids(query):
    return [row.id for row in query.all()]


def test_auto_join_for_model_specific_filter(db):
    session, _ = db
    query = session.query(Foo)
    spec = [{"field": "name", "op": "==", "value": "foo_1"},
            {"model": "Bar", "field": "count", "op": ">=", "value": 5}]
    assert ids(apply_filters(query, spec).order_by(Foo.id)) == [1]
    explicit = [{"model": "Foo", "field": "name", "op": "==", "value": "foo_1"}, spec[1]]
    assert ids(apply_filters(session.query(Foo).join(Bar), explicit).order_by(Foo.id)) == [1]


def test_auto_join_can_be_disabled(db):
    session, _ = db
    spec = [{"model": "Bar", "field": "count", "op": "==", "value": 5}]
    with pytest.raises(BadSpec):
        apply_filters(session.query(Foo), spec, do_auto_join=False)


def test_auto_join_for_sort(db):
    session, _ = db
    query = apply_sort(session.query(Foo), [{"model": "Bar", "field": "name", "direction": "desc"}])
    assert ids(query) == [3, 2, 1]


def test_nested_boolean_functions(db):
    session, _ = db
    spec = [{"or": [
        {"and": [{"field": "name", "op": "==", "value": "foo_1"},
                 {"field": "count", "op": ">", "value": 2}]},
        {"not": [{"field": "count", "op": "is_not_null"}]},
    ]}]
    assert ids(apply_filters(session.query(Foo), spec).order_by(Foo.id)) == [3, 4]
    with pytest.raises(BadFilterFormat):
        apply_filters(session.query(Foo), {"not": [{"field": "id"}, {"field": "id"}]})
    with pytest.raises(BadFilterFormat):
        apply_filters(session.query(Foo), {"or": []})


def test_list_and_text_operators(db):
    session, _ = db
    query = session.query(Foo)
    assert ids(apply_filters(query, {"field": "id", "op": "in", "value": [2, 4]}).order_by(Foo.id)) == [2, 4]
    assert ids(apply_filters(query, {"field": "id", "op": "not_in", "value": [2, 4]}).order_by(Foo.id)) == [1, 3]
    assert ids(apply_filters(query, {"field": "name", "op": "like", "value": "foo_%"})
               .order_by(Foo.id)) == [1, 2, 3, 4]
    assert ids(apply_filters(query, {"field": "name", "op": "ilike", "value": "FOO_1"}).order_by(Foo.id)) == [1, 3]
    assert ids(apply_filters(query, {"field": "name", "value": "foo_2"})) == [2]


def test_field_aggregate_and_select_from_queries(db):
    session, _ = db
    by_fields = apply_filters(session.query(Foo.id, Foo.name), {"field": "name", "op": "==", "value": "foo_1"})
    assert sorted(row.id for row in by_fields) == [1, 3]
    counted = apply_filters(session.query(func.count(Foo.id)), {"field": "count", "op": "is_null"})
    assert counted.scalar() == 1
    select_from = apply_filters(session.query().select_from(Bar).add_columns(Bar.id),
                                {"field": "count", "op": "==", "value": 10})
    assert [row.id for row in select_from] == [2]


def test_multiple_models_require_explicit_model(db):
    session, _ = db
    query = session.query(Foo, Bar).join(Bar)
    with pytest.raises(BadSpec):
        apply_filters(query, {"field": "name", "op": "==", "value": "x"})
    with pytest.raises(BadSpec):
        apply_filters(query, {"model": "Qux", "field": "name", "op": "==", "value": "x"}, do_auto_join=False)
    with pytest.raises(BadQuery):
        apply_filters(session.query(), {"field": "name", "op": "==", "value": "x"}, do_auto_join=False)


def test_invalid_field_operator_and_sort_spec(db):
    session, _ = db
    query = session.query(Foo)
    with pytest.raises(FieldNotFound):
        apply_filters(query, {"field": "missing", "op": "==", "value": 1})
    with pytest.raises(BadFilterFormat):
        apply_filters(query, {"field": "id", "op": "between", "value": 1})
    with pytest.raises(BadFilterFormat):
        apply_filters(query, {"field": "id", "op": "=="})
    with pytest.raises(BadSortFormat):
        apply_sort(query, {"field": "id", "direction": "sideways"})


def test_hybrid_sort_and_nulls_ordering(db):
    session, _ = db
    query = session.query(Bar)
    assert ids(apply_sort(query, {"field": "count_square", "direction": "desc", "nullslast": True})) == [2, 1, 3]
    assert ids(apply_sort(query, {"field": "three_times_count", "direction": "asc", "nullsfirst": True})) == [3, 1, 2]


def test_apply_loads_restricts_loaded_columns(db):
    session, statements = db
    rows = apply_loads(session.query(Foo).order_by(Foo.id), ["name"]).all()
    select = statements[-1]
    assert "foo.name" in select and "foo.count" not in select and "foo.bar_id" not in select
    assert rows[0].name == "foo_1"
    assert "count" not in rows[0].__dict__


def test_apply_loads_auto_joins_and_restricts_each_model(db):
    session, statements = db
    query = session.query(Foo, Bar).join(Bar)
    rows = apply_loads(query, [{"model": "Foo", "fields": ["name"]},
                               {"model": "Bar", "fields": ["count"]}]).order_by(Foo.id).all()
    select = statements[-1]
    assert "foo.name" in select and "bar.count" in select
    assert "foo.count" not in select and "bar.name" not in select
    assert [(foo.name, bar.count) for foo, bar in rows] == [("foo_1", 5), ("foo_2", 10), ("foo_1", None)]


def test_pagination_defaults_and_errors(db):
    session, _ = db
    query = session.query(Foo).order_by(Foo.id)
    page, pagination = apply_pagination(query)
    assert ids(page) == [1, 2, 3, 4] and tuple(pagination) == (1, 4, 1, 4)
    page, pagination = apply_pagination(query, page_number=1, page_size=10)
    assert ids(page) == [1, 2, 3, 4] and pagination.page_size == 4
    page, pagination = apply_pagination(query, page_number=3, page_size=2)
    assert ids(page) == [] and pagination.num_pages == 2
    with pytest.raises(InvalidPage):
        apply_pagination(query, page_number=0, page_size=2)
    with pytest.raises(InvalidPage):
        apply_pagination(query, page_number=1, page_size=-1)
