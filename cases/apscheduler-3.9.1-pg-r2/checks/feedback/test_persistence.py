"""公开的真实 PG 行为反馈；不以实现内部的 SQLAlchemy 调用形式打分。"""

import operator
import os
from contextlib import closing
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg2
import pytest
import pytz
from sqlalchemy import create_engine

from apscheduler.job import Job
from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

WHEN = datetime(2030, 1, 1, tzinfo=pytz.utc)


def make_job(job_id, when=WHEN):
    return Job(BackgroundScheduler(timezone=pytz.utc), id=job_id, func=operator.add, args=(2, 3), kwargs={},
               trigger=DateTrigger(run_date=WHEN, timezone=pytz.utc), executor="default",
               name="persisted", misfire_grace_time=30, coalesce=True,
               max_instances=2, next_run_time=when)


def rows(store):
    # 独立 DBAPI 连接只能看到已提交结果，不能被当前 Session 的缓存蒙蔽。
    with closing(psycopg2.connect(os.environ["UPGRADE_WORKBENCH_PG_DSN"])) as db:
        with db.cursor() as cursor:
            cursor.execute('SELECT id, next_run_time FROM "' + store.jobs_t.name + '" ORDER BY id')
            return cursor.fetchall()


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


def test_add_is_committed_and_restores_callable(store):
    store.add_job(make_job("one"))
    assert rows(store) == [("one", WHEN.timestamp())]
    job = store.lookup_job("one")
    assert job.id == "one" and job.func(*job.args, **job.kwargs) == 5
    assert job.next_run_time == WHEN and job.max_instances == 2


def test_missing_lookup_is_none(store):
    assert store.lookup_job("absent") is None


def test_update_is_visible_to_another_connection(store):
    store.add_job(make_job("one"))
    replacement = make_job("one", WHEN + timedelta(days=1))
    replacement.max_instances = 6
    store.update_job(replacement)
    assert rows(store) == [("one", replacement.next_run_time.timestamp())]
    assert store.lookup_job("one").max_instances == 6


def test_due_order_and_paused_last(store):
    for name, when in (("late", WHEN + timedelta(days=1)), ("paused", None),
                       ("early", WHEN - timedelta(days=1))):
        store.add_job(make_job(name, when))
    assert [job.id for job in store.get_due_jobs(WHEN)] == ["early"]
    assert [job.id for job in store.get_all_jobs()] == ["early", "late", "paused"]
    assert store.get_next_run_time() == WHEN - timedelta(days=1)


def test_remove_is_committed_without_removing_other_jobs(store):
    store.add_job(make_job("one"))
    store.add_job(make_job("two"))
    store.remove_job("one")
    assert rows(store) == [("two", WHEN.timestamp())]
    assert store.lookup_job("one") is None


def test_duplicate_is_rolled_back_and_next_write_works(store):
    store.add_job(make_job("one"))
    with pytest.raises(ConflictingIdError):
        store.add_job(make_job("one", WHEN + timedelta(days=2)))
    store.add_job(make_job("two"))
    assert rows(store) == [("one", WHEN.timestamp()), ("two", WHEN.timestamp())]


def test_missing_delete_preserves_error_contract(store):
    with pytest.raises(JobLookupError):
        store.remove_job("absent")
    store.add_job(make_job("after_error"))
    assert len(rows(store)) == 1


def test_remove_all_is_durable(store):
    store.add_job(make_job("one"))
    store.add_job(make_job("two"))
    store.remove_all_jobs()
    assert rows(store) == []
    assert store.get_next_run_time() is None
