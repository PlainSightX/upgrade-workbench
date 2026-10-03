"""独立检查用户 SQL 的字符串值及钩子效果。"""

import csv
import sqlite3
from io import StringIO

from csvkit.utilities.csvsql import CSVSQL


def run_csvsql(args):
    output = StringIO()
    CSVSQL(args, output).run()
    return list(csv.reader(StringIO(output.getvalue())))


def test_query_preserves_colon_literal(tmp_path):
    source = tmp_path / "items.csv"
    source.write_text("item\nA\n")

    rows = run_csvsql(["--query", "SELECT ':pending' AS note FROM items", str(source)])
    assert rows == [["note"], [":pending"]]


def test_after_insert_preserves_colon_literal(tmp_path):
    source = tmp_path / "orders.csv"
    database = tmp_path / "orders.sqlite"
    source.write_text("ref\nO-1\n")

    run_csvsql([
        "--db", f"sqlite:///{database}", "--insert", "--tables", "orders",
        "--before-insert", "CREATE TABLE audit (note TEXT)",
        "--after-insert", "INSERT INTO audit VALUES (':posted')", str(source),
    ])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT note FROM audit").fetchall() == [(":posted",)]
