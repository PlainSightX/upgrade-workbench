"""通用资源所有权正反例：不借用案例修复答案，也不运行目标代码。"""

import textwrap

import pytest

from upgrade_workbench.analysis.lifecycle import RULE
from upgrade_workbench.analysis.sqlalchemy import scan_source


def scan(body, signature="engine: sa.engine.Engine"):
    source = "import sqlalchemy as sa\n\ndef work(" + signature + "):\n" + textwrap.indent(textwrap.dedent(body), "    ")
    found, unknown = scan_source("source/work.py", source.encode())
    return [x for x in found if x["rule"] == RULE], unknown


@pytest.mark.parametrize("body,cleanup", [
    ("c = engine.connect()\nc.execute(statement)", "not_established"),
    ("c = engine.connect()\nc.execute(statement)\nc.close()", "normal_path_only"),
    ("c = engine.connect()\nwith c.begin():\n    c.execute(statement)", "transaction_scope_only"),
    ("c = engine.connect()\nc.commit()", "not_established"),
    ("c = engine.connect()\nc.rollback()", "not_established"),
    ("c = engine.connect()\nengine.dispose()", "not_established"),
    ("with engine.connect().begin():\n    operation()", "not_established"),
    ("c = engine.connect()\ntry:\n    operation()\nexcept Exception:\n    c.close()", "normal_path_only"),
    ("c = engine.connect()\noperation()\ntry:\n    query(c)\nfinally:\n    c.close()", "normal_path_only"),
    ("c = engine.connect()\ntry:\n    query(c)\nfinally:\n    might_raise()\n    c.close()", "normal_path_only"),
    ("c = engine.connect()\ntry:\n    query(c)\nfinally:\n    if enabled:\n        c.close()", "normal_path_only"),
    ("try:\n    for _ in range(2):\n        c = engine.connect()\nfinally:\n    c.close()", "normal_path_only"),
    ("c = engine.connect()\ntry:\n    might_raise()\n    alias = c\nfinally:\n    alias.close()", "not_established"),
    ("c = engine.connect()\nwith other_context(), c:\n    query(c)", "not_established"),
])
def test_unguarded_acquisition_distinguishes_cleanup_patterns(body, cleanup):
    found, _ = scan(body)
    assert len(found) == 1
    assert found[0]["lifecycle"]["cleanup"] == cleanup
    assert found[0]["lifecycle"]["engine_basis"] == "declared_engine"
    assert found[0]["status"] == "potential_impact"


@pytest.mark.parametrize("body", [
    "with engine.connect() as c:\n    c.execute(statement)",
    "with engine.begin() as c:\n    c.execute(statement)",
    "c = engine.connect()\ntry:\n    c.execute(statement)\nfinally:\n    c.close()",
    "try:\n    c = engine.connect()\n    c.execute(statement)\nfinally:\n    c.close()",
    "c = engine.connect()\nwith c:\n    query(c)",
    "c = engine.connect()\nc.close()",
    "for item in items:\n    c = engine.connect()\n    try:\n        query(c)\n    finally:\n        c.close()",
])
def test_recognized_cleanup_does_not_raise_lifetime_finding(body):
    found, _ = scan(body)
    assert found == []


@pytest.mark.parametrize("expression", ["consume(engine.connect())", "consume(connection=engine.connect())"])
def test_inline_handoff_keeps_callee_ownership_uncertain(expression):
    found, _ = scan(expression)
    assert len(found) == 1 and found[0]["lifecycle"]["ownership"] == "callee_unknown"


@pytest.mark.parametrize("body", ["return engine.connect()", "c = engine.connect()\nreturn c",
    "self.connection = engine.connect()", "yield engine.connect()",
    "c = engine.connect()\nc = other()\nc.close()"])
def test_explicit_escape_or_rebinding_is_unknown_not_safe_or_proven_leak(body):
    found, unknown = scan(body)
    assert not found and any(x["rule"] == "connection_ownership_escaped" for x in unknown)


@pytest.mark.parametrize("signature,body", [
    ("engine", "engine.connect()"),
    ("engine: Client", "engine.connect()"),
    ("engine: sa.engine.Engine", "engine = unrelated\nengine.connect()"),
    ("", "engine = other_factory()\nengine.connect()"),
    ("", "engine = sa.create_engine(url)\nengine = unrelated\nengine.connect()"),
])
def test_untyped_injected_or_rebound_receivers_never_gain_engine_identity(signature, body):
    found, unknown = scan(body, signature)
    assert not found and any(x["rule"] == "connection_receiver_unresolved" for x in unknown)


@pytest.mark.parametrize("factory", ["sa.create_engine", "sa.engine.create_engine", "sa.engine.create.create_engine"])
def test_direct_factory_has_static_binding(factory):
    found, _ = scan(f"e = {factory}(url)\nc = e.connect()", "")
    assert len(found) == 1 and found[0]["lifecycle"]["engine_basis"] == "direct_factory"


def test_typed_member_traces_constructor_and_stays_class_scoped():
    source = '''
import sqlalchemy as sa
class Store:
    def __init__(self, database: "sa.engine.Engine"):
        self.database = database
    def read(self):
        return consume(self.database.connect())
class Unrelated:
    def __init__(self, database):
        self.database = database
    def read(self):
        return consume(self.database.connect())
'''
    found, unknown = scan_source("source/application.py", source.encode())
    assert len(found) == 1 and found[0]["line"] == 7
    assert found[0]["lifecycle"]["engine_basis"] == "member_annotation"
    assert found[0]["lifecycle"]["engine_line"] == 5
    assert any(x["line"] == 12 and x["rule"] == "connection_receiver_unresolved" for x in unknown)


def test_member_factory_and_injected_alternative_are_different():
    template = '''
import sqlalchemy as sa
class Store:
    def __init__(self, custom=None):
        self.engine = sa.create_engine(url)
        {extra}
    def read(self):
        c = self.engine.connect()
'''
    found, _ = scan_source("source/app.py", template.format(extra="pass").encode())
    assert len(found) == 1 and found[0]["lifecycle"]["engine_basis"] == "member_factory"
    found, unknown = scan_source("source/app.py", template.format(extra="if custom:\n            self.engine = custom").encode())
    assert not found and unknown


@pytest.mark.parametrize("import_line,wrapper", [("from contextlib import closing as finish", "finish"),
                                                ("import contextlib as ctx", "ctx.closing")])
def test_standard_closing_is_recognized_but_not_shadowed_wrapper(import_line, wrapper):
    source = f'''import sqlalchemy as sa
{import_line}
def work(engine: sa.engine.Engine):
    with {wrapper}(engine.connect()) as c:
        operation(c)
'''
    found, _ = scan_source("source/app.py", source.encode())
    assert not found
    source = source.replace("def work(engine:", f"def work({wrapper.split('.')[0]}, engine:")
    found, _ = scan_source("source/app.py", source.encode())
    assert len(found) == 1


def test_nested_cleanup_function_cannot_close_outer_connection_by_merely_existing():
    found, _ = scan("c = engine.connect()\ndef cleanup():\n    c.close()\noperation(c)")
    assert len(found) == 1 and found[0]["lifecycle"]["cleanup"] == "not_established"


def test_factory_alias_and_engine_alias_are_bounded_and_resolvable():
    source = '''from sqlalchemy import create_engine as make
engine = make(url)
def work():
    e = engine
    consume(e.connect())
'''
    found, _ = scan_source("source/app.py", source.encode())
    assert len(found) == 1


def test_shadowed_sqlalchemy_import_does_not_fake_annotation_identity():
    source = '''import sqlalchemy as sa
def work(sa, engine: sa.engine.Engine):
    consume(engine.connect())
'''
    found, _ = scan_source("source/app.py", source.encode())
    assert not found


@pytest.mark.parametrize("guard_import,guard", [("from typing import TYPE_CHECKING", "TYPE_CHECKING"),
                                              ("import typing as t", "t.TYPE_CHECKING")])
def test_type_checking_declaration_survives_runtime_loader_without_trusting_loader(guard_import, guard):
    source = f'''{guard_import}
if {guard}:
    import sqlalchemy as db
else:
    db = custom_loader("sqlalchemy")
def typed(engine: "db.engine.Engine"):
    consume(engine.connect())
def untyped():
    engine = db.create_engine(url)
    consume(engine.connect())
'''
    found, unknown = scan_source("source/app.py", source.encode())
    assert len(found) == 1 and found[0]["line"] == 7
    assert found[0]["lifecycle"]["engine_basis"] == "declared_engine"
    assert any(row["line"] == 10 and row["rule"] == "connection_receiver_unresolved" for row in unknown)


def test_shadowed_type_checking_flag_is_not_a_static_import_contract():
    source = '''from typing import TYPE_CHECKING
TYPE_CHECKING = custom_flag
if TYPE_CHECKING:
    import sqlalchemy as db
else:
    db = other
def work(engine: "db.engine.Engine"):
    consume(engine.connect())
'''
    assert scan_source("source/app.py", source.encode())[0] == []


def test_reassigned_self_cannot_borrow_original_member_type():
    source = '''import sqlalchemy as sa
class Store:
    def __init__(self, engine: sa.engine.Engine):
        self.engine = engine
    def work(self):
        self = replacement
        self.engine.connect()
'''
    found, unknown = scan_source("source/app.py", source.encode())
    assert not found and any(row["rule"] == "connection_receiver_unresolved" for row in unknown)
