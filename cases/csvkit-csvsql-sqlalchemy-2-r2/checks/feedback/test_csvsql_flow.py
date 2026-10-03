"""公开反馈：执行实际 csvsql 查询，并从独立连接核查持久结果。"""

import csv
from io import StringIO
import sqlite3

from csvkit.utilities.csvsql import CSVSQL


def run_csvsql(args):
    output = StringIO()
    CSVSQL(args, output).run()
    return list(csv.reader(StringIO(output.getvalue())))


def test_query_filters_csv_rows(tmp_path):
    source = tmp_path / "sales.csv"
    source.write_text("name,region\nAda,east\nLin,west\nBo,east\n")

    rows = run_csvsql(["--query", "SELECT name FROM sales WHERE region = 'east' ORDER BY name",
                       str(source)])
    assert rows == [["name"], ["Ada"], ["Bo"]]


def test_file_database_query_and_commit(tmp_path):
    source = tmp_path / "inventory.csv"
    database = tmp_path / "inventory.sqlite"
    source.write_text("sku,status\nA,ready\nB,hold\n")

    rows = run_csvsql(["--db", f"sqlite:///{database}", "--insert", "--tables", "stock",
                       "--query", "SELECT sku FROM stock WHERE status = 'ready'", str(source)])
    assert rows == [["sku"], ["A"]]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT sku, status FROM stock ORDER BY sku").fetchall() == [
            ("A", "ready"), ("B", "hold")]
