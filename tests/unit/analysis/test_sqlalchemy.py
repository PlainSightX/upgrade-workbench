"""迁移定位只报告有限证据，不把所有老 API 或同名方法判成必须重写。"""

from upgrade_workbench.analysis.sqlalchemy import scan_source


def scan(text):
    return scan_source("source/application.py", text.encode())


def test_direct_import_aliases_and_removed_constructor_arguments():
    found, _ = scan('''
import sqlalchemy as sa
from sqlalchemy import select as choose
from sqlalchemy.orm import Session
a = choose([column])
b = sa.MetaData(bind=engine)
s = Session(engine, autocommit=True)
''')
    assert {x["rule"] for x in found} == {"sa_select_list", "sa_bound_metadata", "sa_session_autocommit"}


def test_guarded_imports_and_injected_engine_remain_potential():
    found, unknown = scan('''
try:
    from sqlalchemy import create_engine, select
except ImportError:
    raise
class Store:
    def __init__(self, engine=None):
        if engine:
            self.engine = engine
        else:
            self.engine = create_engine("postgresql://")
    def read(self):
        return self.engine.execute(select([column]))
''')
    assert {x["rule"] for x in found} == {"sa_select_list", "sa_engine_execute"}
    assert {x["rule"] for x in unknown} == {"receiver_alternative_assignment"}


def test_valid_query_connection_and_unrelated_receiver_are_not_removed():
    found, unknown = scan('''
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, select
engine = create_engine("postgresql://")
with engine.connect() as connection:
    connection.execute(select(column))
with Session(engine) as session:
    session.query(Item).filter(Item.id == 1).all()
def other(engine):
    engine.execute("unrelated object")
''')
    assert found == []
    assert any(x["rule"] == "execute_receiver_unresolved" for x in unknown)


def test_receiver_in_one_function_does_not_leak_to_another():
    found, _ = scan('''
from sqlalchemy import create_engine
def known():
    client = create_engine("postgresql://")
    client.execute(statement)
def unknown(client):
    client.execute(statement)
''')
    assert len(found) == 1 and found[0]["line"] == 5


def test_shadowed_import_and_reassigned_receiver_are_unknown():
    found, _ = scan('''
from sqlalchemy import select, create_engine
select = local_function
select([column])
engine = create_engine("postgresql://")
engine = other_object
engine.execute(statement)
''')
    assert found == []


def test_syntax_failure_is_retained_as_unknown():
    found, unknown = scan("def bad(:")
    assert not found and unknown[0]["rule"] == "unparseable_source"


def test_unrelated_reimport_does_not_inherit_sqlalchemy_alias():
    found, _ = scan('''
from sqlalchemy import select
from another_library import select
select([value])
''')
    assert found == []


def test_long_lived_commit_without_rollback_is_a_bounded_recovery_risk():
    found, _ = scan('''
from sqlalchemy import create_engine
class Store:
    def __init__(self):
        self._db = create_engine("postgresql://")
        self._conn = self._db.connect()
    def write(self, statement):
        self._conn.execute(statement)
        self._conn.commit()
''')
    risks = [item for item in found if item["rule"] == "sa_connection_error_recovery"]
    assert len(risks) == 1
    assert risks[0]["symbol"] == "self._conn"
    assert risks[0]["evidence_key"] == "sqlalchemy-v2-autocommit"


def test_visible_rollback_suppresses_long_lived_recovery_risk():
    found, _ = scan('''
from sqlalchemy import create_engine
class Store:
    def __init__(self):
        self._db = create_engine("postgresql://")
        self._conn = self._db.connect()
    def write(self, statement):
        try:
            self._conn.execute(statement)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
''')
    assert not any(item["rule"] == "sa_connection_error_recovery" for item in found)
