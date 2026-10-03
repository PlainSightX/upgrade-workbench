"""终态后才运行的ORM行为：不回传给模型，不要求重写合法Query。"""

import os
from contextlib import closing
from uuid import uuid4

import optuna
import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy.exc import IntegrityError

from optuna.distributions import CategoricalDistribution, FloatDistribution
from optuna.storages import RDBStorage
from optuna.storages._rdb import models
from optuna.storages._rdb.storage import _create_scoped_session
from optuna.study import StudyDirection
from optuna.trial import TrialState, create_trial


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


def test_frozen_graph_survives_closed_session_and_reopen(opened):
    factory, _ = opened
    store = factory()
    sid = study(store)
    template = create_trial(value=1.25, params={"x": 0.5}, distributions={"x": FloatDistribution(0, 1)},
                            user_attrs={"data": [1, 2]}, system_attrs={"worker": "x"}, intermediate_values={1: 2.5})
    store.create_new_trial(sid, template)
    store.remove_session()
    store.engine.dispose()
    restored = factory().get_all_trials(sid)[0]
    assert restored.params == {"x": 0.5} and restored.values == [1.25]
    assert restored.user_attrs == {"data": [1, 2]} and restored.system_attrs == {"worker": "x"}
    assert restored.intermediate_values == {1: 2.5}
    restored.user_attrs["data"].append(3)
    assert factory().get_trial(restored._trial_id).user_attrs == {"data": [1, 2]}


def test_attribute_update_replaces_value_without_duplicate_rows(opened):
    factory, schema = opened
    store = factory()
    sid = study(store)
    tid = store.create_new_trial(sid)
    for value in ({"old": 1}, {"new": 2}):
        store.set_study_user_attr(sid, "owner", value)
        store.set_trial_user_attr(tid, "owner", value)
    assert factory().get_trial_user_attrs(tid) == {"owner": {"new": 2}}
    assert db(schema, "SELECT COUNT(*) FROM trial_user_attributes WHERE trial_id=%s", (tid,)) == [(1,)]
    assert factory().get_study_user_attrs(sid) == {"owner": {"new": 2}}


def test_incompatible_distribution_rolls_back_without_corruption(opened):
    factory, _ = opened
    store = factory()
    sid = study(store)
    first, second = store.create_new_trial(sid), store.create_new_trial(sid)
    store.set_trial_param(first, "choice", 0, CategoricalDistribution(["a", "b"]))
    with pytest.raises(ValueError):
        store.set_trial_param(second, "choice", 0, CategoricalDistribution(["c", "d"]))
    assert store.get_trial(second).params == {}
    assert store.get_trial(first).params == {"choice": "a"}
    store.set_trial_param(second, "choice", 1, CategoricalDistribution(["a", "b"]))
    assert store.get_trial(second).params == {"choice": "b"}


def test_completed_trial_is_immutable(opened):
    factory, _ = opened
    store = factory()
    tid = store.create_new_trial(study(store), create_trial(value=2.0))
    with pytest.raises(RuntimeError):
        store.set_trial_user_attr(tid, "forbidden", True)
    assert store.get_trial(tid).user_attrs == {} and store.get_trial(tid).values == [2.0]


def test_multiobjective_direction_and_value_order_survive_reopen(opened):
    factory, _ = opened
    store = factory()
    sid = study(store, directions=[StudyDirection.MINIMIZE, StudyDirection.MAXIMIZE])
    tid = store.create_new_trial(sid)
    store.set_trial_state_values(tid, TrialState.COMPLETE, [3.0, 8.0])
    reopened = factory()
    assert reopened.get_study_directions(sid) == [StudyDirection.MINIMIZE, StudyDirection.MAXIMIZE]
    assert reopened.get_trial(tid).values == [3.0, 8.0]
    with pytest.raises(RuntimeError):
        reopened.get_best_trial(sid)


def test_state_filter_and_study_isolation(opened):
    factory, _ = opened
    store = factory()
    sid, unrelated = study(store, "main"), study(store, "other")
    complete = store.create_new_trial(sid, create_trial(value=1.0))
    store.create_new_trial(sid)
    store.create_new_trial(unrelated, create_trial(value=-100.0))
    assert [trial._trial_id for trial in store.get_all_trials(sid, states=(TrialState.COMPLETE,))] == [complete]
    assert store.get_best_trial(sid)._trial_id == complete


def test_constraint_failure_rolls_back_and_returns_connection(opened):
    factory, schema = opened
    store = factory()
    study(store, "kept")
    with pytest.raises(IntegrityError):
        with _create_scoped_session(store.scoped_session) as session:
            session.add(models.StudyModel(study_name="pending"))
            session.flush()
            session.add(models.StudyModel(study_name="kept"))
            session.flush()
    assert db(schema, "SELECT study_name FROM studies") == [("kept",)]
    study(store, "usable")
    assert store.engine.pool.checkedout() == 0


def test_repeated_queries_do_not_exhaust_pool(opened):
    factory, _ = opened
    store = factory()
    sid = study(store)
    store.create_new_trial(sid, create_trial(value=1.0))
    for _ in range(12):
        assert store.get_current_version() == store.get_head_version()
        assert len(store.get_all_trials(sid)) == 1
        assert store.engine.pool.checkedout() == 0


def test_future_schema_revision_is_rejected_not_silently_stamped(opened):
    factory, schema = opened
    store = factory()
    study(store, "preserved")
    db(schema, "UPDATE alembic_version SET version_num=%s", ("unknown_future_revision",), fetch=False)
    with pytest.raises(RuntimeError, match="compatible"):
        factory()
    assert db(schema, "SELECT version_num FROM alembic_version") == [("unknown_future_revision",)]
    assert db(schema, "SELECT study_name FROM studies") == [("preserved",)]


def test_high_level_ask_tell_and_resume_use_real_storage(opened):
    factory, _ = opened
    store = factory()
    experiment = optuna.create_study(study_name="ask-tell", storage=store, sampler=optuna.samplers.RandomSampler(seed=7))
    trial = experiment.ask()
    value = trial.suggest_float("x", 0.0, 1.0)
    experiment.tell(trial, value * value)
    resumed = optuna.load_study(study_name="ask-tell", storage=factory())
    assert resumed.best_trial.params == {"x": value}
    assert resumed.best_value == value * value


def test_heartbeat_relationship_keeps_trial_identity(opened):
    factory, schema = opened
    store = factory()
    tid = store.create_new_trial(study(store))
    store.record_heartbeat(tid)
    store.record_heartbeat(tid)
    assert db(schema, "SELECT trial_id, COUNT(*) FROM trial_heartbeats GROUP BY trial_id") == [(tid, 1)]
    assert store.get_trial(tid).state == TrialState.RUNNING


def test_valid_legacy_query_and_relationship_not_removed(opened):
    factory, _ = opened
    store = factory()
    sid = study(store)
    store.create_new_trial(sid, create_trial(value=2.0))
    with _create_scoped_session(store.scoped_session) as session:
        found = session.query(models.StudyModel).filter(models.StudyModel.study_id == sid).one()
        assert len(found.trials) == 1 and found.trials[0].values[0].value == 2.0
