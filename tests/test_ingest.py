"""qdp.ingest 测试：增量起点、幂等写入、异常分档（离线，注入假抓取函数）。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qdp.db import connect, init_schema
from qdp.ingest import (
    IngestError,
    idempotent_insert,
    next_start_date,
    run_update,
    update_symbol,
)

START = date(2026, 7, 1)
END = date(2026, 7, 21)
DAYS = [date(2026, 7, 20), date(2026, 7, 21)]


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def make_bars(symbol: str, days: list[date], close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": symbol, "trade_date": days,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1000, "amount": 1e6,
    })


def make_factors(symbol: str, days: list[date]) -> pd.DataFrame:
    return pd.DataFrame({"symbol": symbol, "trade_date": days, "hfq_factor": 2.0})


def make_hfq(days: list[date]) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": days, "hfq_close": 20.0})


def test_idempotent_insert_counts_only_new_rows(conn):
    df = make_bars("000001", DAYS)
    assert idempotent_insert(conn, "daily_bar", df) == 2
    assert idempotent_insert(conn, "daily_bar", df) == 0  # 重跑：全部被主键拦下跳过
    assert conn.execute("SELECT count(*) FROM daily_bar").fetchone()[0] == 2


def test_next_start_date(conn):
    assert next_start_date(conn, "daily_bar", "000001", START) == START
    idempotent_insert(conn, "daily_bar", make_bars("000001", DAYS))
    assert next_start_date(conn, "daily_bar", "000001", START) == date(2026, 7, 22)


def test_update_symbol_requests_incremental_range(conn):
    """库里已有 7/15 的数据，抓取应从 7/16 开始，而不是从头抓。"""
    idempotent_insert(conn, "daily_bar", make_bars("000001", [date(2026, 7, 15)]))
    idempotent_insert(conn, "adj_factor", make_factors("000001", [date(2026, 7, 15)]))
    seen = {}

    def fake_bars(symbol, start, end):
        seen["start"] = start
        return make_bars(symbol, DAYS)

    n_bars, n_factors = update_symbol(
        conn, "000001", default_start=START, end=END,
        fetch_bars=fake_bars, fetch_hfq=lambda s, a, b: make_hfq(DAYS),
    )
    assert seen["start"] == date(2026, 7, 16)
    assert (n_bars, n_factors) == (2, 2)


def test_update_symbol_skips_when_up_to_date(conn):
    """数据已到 end 当天时，一个请求都不该发。"""
    idempotent_insert(conn, "daily_bar", make_bars("000001", [END]))
    idempotent_insert(conn, "adj_factor", make_factors("000001", [END]))

    def boom(*args):
        raise AssertionError("已最新时不应发请求")

    assert update_symbol(
        conn, "000001", default_start=START, end=END, fetch_bars=boom, fetch_hfq=boom,
    ) == (0, 0)


def fake_calendar() -> pd.DataFrame:
    return pd.DataFrame({"cal_date": DAYS, "is_open": True})


def fake_universe() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["000001", "000002", "600000"],
        "name": ["平安银行", "万科A", "浦发银行"],
        "exchange": ["SZ", "SZ", "SH"],
    })


def test_run_update_degrades_on_single_symbol_failure(conn):
    """一只股票挂了：跳过它继续跑，记入失败清单（辅助缺→降级）。"""

    def fake_bars(symbol, start, end):
        if symbol == "000002":
            raise ConnectionError("模拟单只失败")
        return make_bars(symbol, DAYS)

    report = run_update(
        conn, default_start=START, sleep_s=0, max_fail_ratio=0.5, end=END,
        fetch_calendar=fake_calendar, fetch_universe=fake_universe,
        fetch_bars=fake_bars, fetch_hfq=lambda s, a, b: make_hfq(DAYS),
    )
    assert report.failed == ["000002"]
    assert report.new_bars == 4  # 其余两只各 2 行
    assert conn.execute("SELECT count(*) FROM stock_basic").fetchone()[0] == 3


def test_run_update_stops_when_calendar_fails(conn):
    """交易日历拿不到（核心错→停）。"""

    def bad_calendar():
        raise ConnectionError("日历接口挂了")

    with pytest.raises(IngestError):
        run_update(
            conn, default_start=START, sleep_s=0, max_fail_ratio=0.5, end=END,
            fetch_calendar=bad_calendar, fetch_universe=fake_universe,
        )


def test_run_update_stops_on_too_many_failures(conn):
    """失败比例超限：整体不可信，报错停（不静默给残缺数据）。"""

    def bad_bars(*args):
        raise ConnectionError("全挂")

    with pytest.raises(IngestError):
        run_update(
            conn, default_start=START, sleep_s=0, max_fail_ratio=0.1, end=END,
            fetch_calendar=fake_calendar, fetch_universe=fake_universe,
            fetch_bars=bad_bars, fetch_hfq=bad_bars,
        )
