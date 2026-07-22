"""qdp.db 单元测试：建表、幂等性、主键防重复。"""

from __future__ import annotations

import duckdb
import pytest

from qdp.db import connect, init_schema

EXPECTED_TABLES = ["adj_factor", "daily_bar", "stock_basic", "trade_calendar"]


@pytest.fixture
def conn():
    """每个测试用独立内存库，互不污染、不落盘。"""
    c = connect(":memory:")
    yield c
    c.close()


def test_init_schema_creates_all_tables(conn):
    tables = init_schema(conn)
    assert tables == EXPECTED_TABLES


def test_init_schema_is_idempotent(conn):
    """重复建表不报错、不丢已有数据。"""
    init_schema(conn)
    conn.execute(
        "INSERT INTO daily_bar (symbol, trade_date, close) VALUES ('000001', DATE '2026-07-21', 10.50)"
    )
    tables = init_schema(conn)  # 第二次执行
    assert tables == EXPECTED_TABLES
    count = conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    assert count == 1  # 数据还在


def test_daily_bar_pk_rejects_duplicate(conn):
    """同一 (symbol, trade_date) 插两次必须被主键拦下——幂等写入的地基。"""
    init_schema(conn)
    row = "('000001', DATE '2026-07-21', 10.50)"
    conn.execute(f"INSERT INTO daily_bar (symbol, trade_date, close) VALUES {row}")
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(f"INSERT INTO daily_bar (symbol, trade_date, close) VALUES {row}")


def test_symbol_keeps_leading_zeros(conn):
    """代码按字符串存，000001 不能变成 1。"""
    init_schema(conn)
    conn.execute(
        "INSERT INTO daily_bar (symbol, trade_date, close) VALUES ('000001', DATE '2026-07-21', 10.50)"
    )
    symbol = conn.execute("SELECT symbol FROM daily_bar").fetchone()[0]
    assert symbol == "000001"


def test_connect_creates_parent_dir(tmp_path):
    """数据库文件的父目录不存在时自动创建。"""
    db_file = tmp_path / "sub" / "dir" / "test.duckdb"
    c = connect(db_file)
    c.close()
    assert db_file.exists()
