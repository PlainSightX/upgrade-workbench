"""真实Optuna公开行为；独立SQL观察提交，不能由候选改变断言。"""

import os
from contextlib import closing
from uuid import uuid4

import optuna
import psycopg2
import pytest
from psycopg2 import sql

from optuna.distributions import FloatDistribution
from optuna.storages import RDBStorage
from optuna.storages._rdb import models
from optuna.storages._rdb.storage import _create_scoped_session
from optuna.study import StudyDirection
from optuna.trial import TrialState


def db(schema, query, params=(), *, fetch=True):
    with closing(psycopg2.connect(os.environ["UPGRADE_WORKBENCH_PG_DSN"],
                                options=f"-c search_path={schema} -c statement_timeout=3000")) as connection:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall() if fetch else None


@pytest.fixture
def opened():
    schema = "case_" + uuid4().hex
    db("public", sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)), fetch=False)
    stores = []

    def open_store():
        store = RDBStorage(os.environ["UPGRADE_WORKBENCH_DATABASE_URL"], engine_kwargs={
            "pool_size": 4, "max_overflow": 0, "pool_timeout": 2,
            "connect_args": {"options": f"-c search_path={schema} -c lock_timeout=1000 -c statement_timeout=3000"},
        })
        stores.append(store)
        return store

    try:
        yield open_store, schema
    finally:
        for store in stores:
            store.remove_session()
            store.engine.dispose()


def study(store, name="study", directions=None):
    return store.create_new_study(directions or [StudyDirection.MINIMIZE], study_name=name)


def populated_trial(store, study_id, value=0.25):
    trial_id = store.create_new_trial(study_id)
    store.set_trial_param(trial_id, "learning_rate", 0.5, FloatDistribution(0.0, 1.0))
    store.set_trial_user_attr(trial_id, "context", {"fold": 2, "names": ["甲", "乙"]})
    store.set_trial_system_attr(trial_id, "worker", "one")
    store.set_trial_intermediate_value(trial_id, 3, 0.75)
    assert store.set_trial_state_values(trial_id, TrialState.COMPLETE, [value])
    return trial_id


def test_schema_revision_is_committed_and_reopenable(opened):
    factory, schema = opened
    store = factory()
    head = store.get_head_version()
    assert db(schema, "SELECT version_num FROM alembic_version") == [(head,)]
    assert factory().get_current_version() == head


def test_study_directions_and_attributes_are_durable(opened):
    factory, schema = opened
    store = factory()
    sid = study(store)
    store.set_study_user_attr(sid, "owner", {"team": "science"})
    store.set_study_system_attr(sid, "tag", [1, 2])
    assert db(schema, "SELECT study_name FROM studies WHERE study_id=%s", (sid,)) == [("study",)]
    other = factory()
    assert other.get_study_directions(sid) == [StudyDirection.MINIMIZE]
    assert other.get_study_user_attrs(sid) == {"owner": {"team": "science"}}
    assert other.get_study_system_attrs(sid) == {"tag": [1, 2]}


def test_duplicate_study_rolls_back_and_next_write_works(opened):
    factory, schema = opened
    store = factory()
    study(store, "duplicate")
    with pytest.raises(optuna.exceptions.DuplicatedStudyError):
        study(store, "duplicate")
    study(store, "next")
    assert db(schema, "SELECT study_name FROM studies ORDER BY study_name") == [("duplicate",), ("next",)]


def test_trial_relationships_are_loaded_and_persisted(opened):
    factory, schema = opened
    store = factory()
    sid = study(store)
    tid = populated_trial(store, sid)
    trial = store.get_all_trials(sid)[0]
    assert trial._trial_id == tid and trial.number == 0
    assert trial.params == {"learning_rate": 0.5} and trial.values == [0.25]
    assert trial.user_attrs["context"]["fold"] == 2
    assert trial.system_attrs == {"worker": "one"}
    assert trial.intermediate_values == {3: 0.75}
    assert db(schema, "SELECT COUNT(*) FROM trial_params WHERE trial_id=%s", (tid,)) == [(1,)]


def test_completion_and_values_visible_to_independent_connection(opened):
    factory, schema = opened
    store = factory()
    tid = populated_trial(store, study(store), 3.25)
    assert db(schema, "SELECT state FROM trials WHERE trial_id=%s", (tid,)) == [("COMPLETE",)]
    assert db(schema, "SELECT value FROM trial_values WHERE trial_id=%s", (tid,)) == [(3.25,)]


def test_best_trial_uses_join_and_excludes_unfinished_trials(opened):
    factory, _ = opened
    store = factory()
    sid = study(store)
    populated_trial(store, sid, 5.0)
    best = populated_trial(store, sid, 1.0)
    pending = store.create_new_trial(sid)
    assert store.get_best_trial(sid)._trial_id == best
    assert store.get_trial(pending).state == TrialState.RUNNING


def test_study_delete_cascades_but_preserves_other_study(opened):
    factory, schema = opened
    store = factory()
    removed = study(store, "removed")
    tid = populated_trial(store, removed)
    kept = study(store, "kept")
    store.delete_study(removed)
    assert store.get_study_name_from_id(kept) == "kept"
    assert db(schema, "SELECT COUNT(*) FROM trials WHERE trial_id=%s", (tid,)) == [(0,)]
    assert db(schema, "SELECT COUNT(*) FROM trial_params WHERE trial_id=%s", (tid,)) == [(0,)]
    assert db(schema, "SELECT COUNT(*) FROM trial_values WHERE trial_id=%s", (tid,)) == [(0,)]


def test_session_exception_rolls_back_whole_unit_and_can_continue(opened):
    factory, schema = opened
    store = factory()
    study(store, "before")
    with pytest.raises(RuntimeError, match="abort-owned-unit"):
        with _create_scoped_session(store.scoped_session) as session:
            session.add(models.StudyModel(study_name="uncommitted"))
            session.flush()
            raise RuntimeError("abort-owned-unit")
    study(store, "after")
    assert db(schema, "SELECT study_name FROM studies ORDER BY study_name") == [("after",), ("before",)]


def test_trial_numbers_are_sequential_and_scoped_to_study(opened):
    factory, _ = opened
    store = factory()
    first, second = study(store, "first"), study(store, "second")
    for _ in range(3):
        store.create_new_trial(first)
    store.create_new_trial(second)
    assert [trial.number for trial in store.get_all_trials(first)] == [0, 1, 2]
    assert [trial.number for trial in store.get_all_trials(second)] == [0]
