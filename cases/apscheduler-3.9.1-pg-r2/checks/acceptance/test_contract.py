"""冻结后的额外验收不送回生成者；合法 legacy 控制与业务检查分开命名。"""

import operator
import os
from contextlib import closing
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg2
import pytest
import pytz
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import Session, declarative_base

from apscheduler.executors.debug import DebugExecutor
from apscheduler.job import Job
from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

WHEN = datetime(2999, 1, 1, tzinfo=pytz.utc)


def job(job_id, when=WHEN):
    return Job(BackgroundScheduler(timezone=pytz.utc), id=job_id, func=operator.add, args=(7, 9), kwargs={},
               trigger=DateTrigger(run_date=WHEN, timezone=pytz.utc), executor="default",
               name="unchanged", misfire_grace_time=60, coalesce=True,
               max_instances=3, next_run_time=when)


def sql(statement, parameters=None, *, fetch=True):
    with closing(psycopg2.connect(os.environ["UPGRADE_WORKBENCH_PG_DSN"])) as db:
        with db:
            with db.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall() if fetch else None


@pytest.fixture
def store():
    engine = create_engine(os.environ["UPGRADE_WORKBENCH_DATABASE_URL"],
                           pool_size=1, max_overflow=0, pool_timeout=0.5)
    value = SQLAlchemyJobStore(engine=engine, tablename="jobs_" + uuid4().hex)
    value.start(None, "default")
    try:
        yield value
    finally:
        value.shutdown()


def test_reopen_recovers_committed_job(store):
    store.add_job(job("restart"))
    second = SQLAlchemyJobStore(url=os.environ["UPGRADE_WORKBENCH_DATABASE_URL"],
                               tablename=store.jobs_t.name)
    try:
        second.start(None, "reopened")
        recovered = second.lookup_job("restart")
        assert recovered.id == "restart" and recovered.args == (7, 9)
        assert recovered.func(*recovered.args) == 16
    finally:
        second.shutdown()


def test_paused_scheduler_restart_modify_remove(store):
    table = store.jobs_t.name

    def scheduler():
        backend = SQLAlchemyJobStore(url=os.environ["UPGRADE_WORKBENCH_DATABASE_URL"], tablename=table)
        return BackgroundScheduler(timezone=pytz.utc, jobstores={"default": backend},
                                   executors={"default": DebugExecutor()})

    first = scheduler()
    first.start(paused=True)
    try:
        first.add_job(operator.add, DateTrigger(run_date=WHEN), id="api_job", args=(1, 2))
    finally:
        first.shutdown()
    second = scheduler()
    second.start(paused=True)
    try:
        recovered = second.get_job("api_job")
        assert recovered.args == (1, 2)
        second.modify_job("api_job", args=(4, 5))
        assert second.get_job("api_job").args == (4, 5)
        second.remove_job("api_job")
        assert second.get_job("api_job") is None
    finally:
        second.shutdown()
    assert sql('SELECT count(*) FROM "' + table + '"') == [(0,)]


def test_corrupt_row_cleanup_is_durable_and_preserves_valid_job(store):
    store.add_job(job("valid"))
    sql('INSERT INTO "' + store.jobs_t.name + '" VALUES (%s, %s, %s)',
        ("corrupt", WHEN.timestamp(), psycopg2.Binary(b"not-a-pickle")), fetch=False)
    assert [value.id for value in store.get_all_jobs()] == ["valid"]
    assert sql('SELECT id FROM "' + store.jobs_t.name + '"') == [("valid",)]


def test_reads_and_writes_return_pool_connections(store):
    store.add_job(job("one"))
    for index in range(15):
        assert store.lookup_job("one").id == "one"
        assert len(store.get_all_jobs()) == 1
        assert len(store.get_due_jobs(WHEN)) == 1
        assert store.get_next_run_time() == WHEN
        store.update_job(job("one"))
        assert store.engine.pool.checkedout() == 0


def test_error_paths_return_pool_connections(store):
    store.add_job(job("one"))
    for index in range(8):
        with pytest.raises(ConflictingIdError):
            store.add_job(job("one"))
        with pytest.raises(JobLookupError):
            store.update_job(job("missing"))
        with pytest.raises(JobLookupError):
            store.remove_job("missing")
        assert store.engine.pool.checkedout() == 0
        assert store.lookup_job("one").id == "one"


def test_database_constraint_failure_does_not_poison_next_operation(store):
    table = store.jobs_t.name
    sql('ALTER TABLE "' + table + '" ADD CONSTRAINT allowed_job CHECK (id <> \'blocked\')', fetch=False)
    with pytest.raises(ConflictingIdError):
        store.add_job(job("blocked"))
    assert store.engine.pool.checkedout() == 0
    store.add_job(job("allowed"))
    assert sql('SELECT id FROM "' + table + '"') == [("allowed",)]


def test_subsecond_due_boundary_and_parameterized_identity(store):
    # SQL 外观的 id 是普通数据；不得拼接进查询或破坏其他记录。
    quoted = "q' OR 1=1 --"
    origin = datetime(2030, 1, 1, tzinfo=pytz.utc)
    store.add_job(job(quoted, origin + timedelta(microseconds=400)))
    store.add_job(job("later", origin + timedelta(microseconds=402)))
    assert [value.id for value in store.get_due_jobs(origin + timedelta(microseconds=401))] == [quoted]
    store.remove_job(quoted)
    assert [value.id for value in store.get_all_jobs()] == ["later"]


def test_pause_update_and_remove_all_survive_reopen(store):
    store.add_job(job("paused"))
    store.update_job(job("paused", None))
    assert sql('SELECT next_run_time FROM "' + store.jobs_t.name + '"') == [(None,)]
    assert store.get_next_run_time() is None
    store.remove_all_jobs()
    assert sql('SELECT count(*) FROM "' + store.jobs_t.name + '"') == [(0,)]


def test_unrelated_memory_store_contract_remains_valid():
    memory = MemoryJobStore()
    memory.start(None, "memory")
    memory.add_job(job("one"))
    assert memory.lookup_job("one").id == "one"
    memory.remove_job("one")
    assert memory.get_all_jobs() == []


def test_compatibility_control_legacy_query_is_still_supported():
    # 这是 API 合法性负对照，不计作 APScheduler ORM 迁移成果。
    base = declarative_base()

    class Item(base):
        __tablename__ = "legacy_" + uuid4().hex
        id = Column(Integer, primary_key=True)

    engine = create_engine(os.environ["UPGRADE_WORKBENCH_DATABASE_URL"])
    try:
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Item(id=7))
            session.commit()
            assert session.query(Item).filter(Item.id == 7).one().id == 7
    finally:
        engine.dispose()
