"""独立验收：换数据与组合，检查查询和语句钩子的实际结果。"""

import csv
from io import StringIO
import sqlite3

from csvkit.utilities.csvsql import CSVSQL


def run_csvsql(args):
    output = StringIO()
    CSVSQL(args, output).run()
    return list(csv.reader(StringIO(output.getvalue())))


def test_query_joins_distinct_csv_sources(tmp_path):
    people = tmp_path / "people.csv"
    teams = tmp_path / "teams.csv"
    people.write_text("person,team\nMia,red\nKai,blue\n")
    teams.write_text("team,city\nred,Leeds\nblue,Bristol\n")

    rows = run_csvsql(["--query", "SELECT person, city FROM people JOIN teams "
                       "ON people.team = teams.team ORDER BY person", str(people), str(teams)])
    assert rows == [["person", "city"], ["Kai", "Bristol"], ["Mia", "Leeds"]]


def test_before_after_insert_effects_are_committed(tmp_path):
    source = tmp_path / "orders.csv"
    database = tmp_path / "orders.sqlite"
    source.write_text("ref,state\nO-1,new\nO-2,new\n")

    run_csvsql(["--db", f"sqlite:///{database}", "--insert", "--tables", "orders",
                "--before-insert", "CREATE TABLE audit (note TEXT)",
                "--after-insert", "INSERT INTO audit VALUES ('loaded')", str(source)])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT ref, state FROM orders ORDER BY ref").fetchall() == [
            ("O-1", "new"), ("O-2", "new")]
        assert connection.execute("SELECT note FROM audit").fetchall() == [("loaded",)]
