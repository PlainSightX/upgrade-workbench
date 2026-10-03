"""冻结的 final-only 检查；不得进入 Solver 或 Auditor 上下文。"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3

import databases.backends.sqlite as sqlite_backend
import pytest
import sqlalchemy
from databases import Database


def run(coroutine):
    return asyncio.run(coroutine)


def schema(tmp_path):
    path = tmp_path / "final.db"
    metadata = sqlalchemy.MetaData()
    parents = sqlalchemy.Table(
        "parents",
        metadata,
        sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column("name", sqlalchemy.String(100), nullable=False),
    )
    children = sqlalchemy.Table(
        "children",
        metadata,
        sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column("parent_id", sqlalchemy.ForeignKey("parents.id"), nullable=False),
        sqlalchemy.Column("enabled", sqlalchemy.Boolean, nullable=False),
    )
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    engine.dispose()
    return f"sqlite+aiosqlite:///{path}", parents, children


async def seed(database, parents, children):
    await database.execute(parents.insert(), {"id": 1, "name": "root"})
    await database.execute_many(
        children.insert(),
        [
            {"id": 10, "parent_id": 1, "enabled": True},
            {"id": 11, "parent_id": 1, "enabled": False},
        ],
    )


def test_column_objects_address_joined_results(tmp_path):
    url, parents, children = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await seed(database, parents, children)
            query = (
                sqlalchemy.select(parents.c.name, children.c.id, children.c.enabled)
                .select_from(parents.join(children))
                .order_by(children.c.id)
            )
            rows = await database.fetch_all(query)
            assert rows[0][parents.c.name] == "root"
            assert rows[0][children.c.id] == 10
            assert rows[1][children.c.enabled] is False

    run(scenario())


def test_labels_and_textual_columns_remain_named(tmp_path):
    url, parents, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute(parents.insert(), {"id": 1, "name": "root"})
            labeled = await database.fetch_one(
                sqlalchemy.select(parents.c.name.label("display_name"))
            )
            textual = await database.fetch_one(
                "SELECT name AS public_name FROM parents WHERE id = :id", {"id": 1}
            )
            assert labeled["display_name"] == "root"
            assert textual._mapping["public_name"] == "root"

    run(scenario())


def test_mapping_shape_matches_sequence(tmp_path):
    url, parents, children = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await seed(database, parents, children)
            row = await database.fetch_one(
                sqlalchemy.select(children.c.id, children.c.enabled).where(
                    children.c.id == 10
                )
            )
            assert list(row._mapping.keys()) == ["id", "enabled"]
            assert list(row._mapping.values()) == [10, True]
            assert list(row) == [10, True]
            assert len(row) == 2

    run(scenario())


def test_execute_many_preserves_boolean_values_and_order(tmp_path):
    url, parents, children = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await seed(database, parents, children)
            rows = await database.fetch_all(children.select().order_by(children.c.id.desc()))
            assert [(row["id"], row["enabled"]) for row in rows] == [
                (11, False),
                (10, True),
            ]

    run(scenario())


def test_nested_transaction_rolls_back_inner_only(tmp_path):
    url, parents, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            async with database.transaction():
                await database.execute(parents.insert(), {"id": 1, "name": "outer"})
                with pytest.raises(RuntimeError):
                    async with database.transaction():
                        await database.execute(
                            parents.insert(), {"id": 2, "name": "inner"}
                        )
                        raise RuntimeError("rollback inner savepoint")
                query = sqlalchemy.select(sqlalchemy.func.count()).select_from(parents)
                assert await database.fetch_val(query) == 1
            rows = await database.fetch_all(parents.select())
            assert [(row["id"], row["name"]) for row in rows] == [(1, "outer")]

    run(scenario())


def test_failed_statement_leaves_connection_reusable(tmp_path):
    url, parents, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            async with database.connection() as connection:
                with pytest.raises(sqlite3.OperationalError):
                    await connection.execute("INSERT INTO missing_table(value) VALUES (1)")
                await connection.execute(
                    parents.insert(), {"id": 1, "name": "after-error"}
                )
                row = await connection.fetch_one(parents.select())
                assert row["name"] == "after-error"

    run(scenario())


def test_missing_key_is_rejected_without_corrupting_sequence(tmp_path):
    url, parents, _ = schema(tmp_path)

    async def scenario():
        async with Database(url) as database:
            await database.execute(parents.insert(), {"id": 1, "name": "root"})
            row = await database.fetch_one(parents.select())
            assert tuple(row) == (1, "root")
            with pytest.raises((KeyError, IndexError, TypeError)):
                _ = row["missing"]

    run(scenario())


def test_candidate_stays_native_and_bounded():
    source = inspect.getsource(sqlite_backend)
    assert "sqlalchemy.future" not in source
    assert "monkeypatch" not in source
    assert "pytest" not in source
    if int(sqlalchemy.__version__.split(".", 1)[0]) >= 2:
        assert "Row._default_key_style" not in source
