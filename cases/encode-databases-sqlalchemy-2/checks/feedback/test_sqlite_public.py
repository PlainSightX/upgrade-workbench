"""公开反馈：验证 SQLite 查询、结果接口和基础事务行为。"""

from __future__ import annotations

import asyncio
import datetime

import sqlalchemy
from databases import Database


class DayOffset(sqlalchemy.types.TypeDecorator):
    """把日期存成相对天数，用于观察绑定与结果处理器。"""

    impl = sqlalchemy.Integer
    cache_ok = True
    epoch = datetime.date(2020, 1, 1)

    def process_bind_param(self, value, dialect):
        return (value - self.epoch).days

    def process_result_value(self, value, dialect):
        return self.epoch + datetime.timedelta(days=value)


def run(coroutine):
    return asyncio.run(coroutine)


def schema(tmp_path):
    path = tmp_path / "public.db"
    metadata = sqlalchemy.MetaData()
    notes = sqlalchemy.Table(
        "notes",
        metadata,
        sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column("text", sqlalchemy.String(100), nullable=False),
        sqlalchemy.Column("completed", sqlalchemy.Boolean, nullable=False),
    )
    events = sqlalchemy.Table(
        "events",
        metadata,
        sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column("day", DayOffset(), nullable=False),
    )
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    engine.dispose()
    return f"sqlite+aiosqlite:///{path}", notes, events


def test_core_crud_preserves_named_and_positional_access(tmp_path):
    url, notes, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute(notes.insert(), {"text": "first", "completed": True})
            await database.execute_many(
                notes.insert(),
                [
                    {"text": "second", "completed": False},
                    {"text": "third", "completed": True},
                ],
            )
            rows = await database.fetch_all(notes.select().order_by(notes.c.id))
            assert [row["text"] for row in rows] == ["first", "second", "third"]
            assert rows[0][1] == "first"
            assert rows[0]._mapping["completed"] is True

    run(scenario())


def test_raw_query_exposes_mapping(tmp_path):
    url, _, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute(
                "INSERT INTO notes(text, completed) VALUES (:text, :completed)",
                {"text": "raw", "completed": True},
            )
            row = await database.fetch_one("SELECT id, text, completed FROM notes")
            assert row.text == "raw"
            assert row._mapping["id"] == 1
            # 原始 SQL 没有列类型元数据，保留 SQLite 返回的整数语义。
            assert row._mapping["completed"] == 1

    run(scenario())


def test_fetch_val_accepts_position_and_name(tmp_path):
    url, notes, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute(notes.insert(), {"text": "value", "completed": True})
            query = sqlalchemy.select(notes.c.id, notes.c.text, notes.c.completed)
            assert await database.fetch_val(query, column=1) == "value"
            assert await database.fetch_val(query, column="completed") is True

    run(scenario())


def test_custom_type_processor_is_applied(tmp_path):
    url, _, events = schema(tmp_path)
    expected = datetime.date(2026, 9, 21)

    async def scenario():
        async with Database(url) as database:
            await database.execute(events.insert(), {"day": expected})
            row = await database.fetch_one(sqlalchemy.select(events.c.day))
            assert row._mapping["day"] == expected

    run(scenario())


def test_iterate_preserves_record_interface(tmp_path):
    url, notes, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute_many(
                notes.insert(),
                [
                    {"text": "a", "completed": False},
                    {"text": "b", "completed": True},
                ],
            )
            observed = []
            async for row in database.iterate(notes.select().order_by(notes.c.id)):
                observed.append((row["text"], row[2], row._mapping["id"]))
            return observed

    assert run(scenario()) == [("a", False, 1), ("b", True, 2)]


def test_force_rollback_does_not_persist(tmp_path):
    url, notes, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            async with database.transaction(force_rollback=True):
                await database.execute(
                    notes.insert(), {"text": "temporary", "completed": True}
                )
                query = sqlalchemy.select(sqlalchemy.func.count()).select_from(notes)
                assert await database.fetch_val(query) == 1
            query = sqlalchemy.select(sqlalchemy.func.count()).select_from(notes)
            assert await database.fetch_val(query) == 0

    run(scenario())
