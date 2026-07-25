"""qdp.crawler 测试：清洗、翻页、空数据分辨、增量入库（全部离线，注入假请求）。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from qdp.crawler import (
    CrawlError,
    fetch_billboard,
    fetch_billboard_raw,
    normalize_billboard,
    next_billboard_date,
    update_billboard,
)
from qdp.db import connect, init_schema
from qdp.ingest import IngestError, idempotent_insert

DAYS = [d for d in (date(2026, 7, 6) + timedelta(days=i) for i in range(14))
        if d.weekday() < 5][:10]


def raw_row(code="000011", reason="日涨幅偏离值达7%的证券", day="2026-07-24 00:00:00",
            type_code="058001001"):
    """仿照接口真实返回构造的一条原始记录。"""
    return {
        "SECURITY_CODE": code, "SECURITY_NAME_ABBR": "深物业A", "TRADE_DATE": day,
        "EXPLANATION": reason, "EXPLAIN": "2家机构买入", "CLOSE_PRICE": 8.26,
        "CHANGE_RATE": 9.9867, "BILLBOARD_NET_AMT": 27996021.3,
        "BILLBOARD_BUY_AMT": 74805567.96, "BILLBOARD_SELL_AMT": 46809546.66,
        "BILLBOARD_DEAL_AMT": 121615114.62, "ACCUM_AMOUNT": 299493417,
        "TURNOVERRATE": 4.756, "FREE_MARKET_CAP": 4519053197.54, "MARKET": "SZ",
        "SECURITY_TYPE_CODE": type_code, "TRADE_ID": 1,  # 多余字段应被丢弃
    }


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    idempotent_insert(c, "trade_calendar", pd.DataFrame({"cal_date": DAYS, "is_open": True}))
    yield c
    c.close()


def test_normalize_maps_fields_and_pads_code():
    df = normalize_billboard([raw_row(code="11")])
    assert df["symbol"].iloc[0] == "000011"        # 前导零补回
    assert df["trade_date"].iloc[0] == date(2026, 7, 24)
    assert df["reason"].iloc[0] == "日涨幅偏离值达7%的证券"
    assert df["interpret"].iloc[0] == "2家机构买入"
    assert "TRADE_ID" not in df.columns


def test_normalize_filters_non_stock():
    """榜上混着可转债（类型码 060），只保留 A 股。"""
    df = normalize_billboard([raw_row(), raw_row(code="123275", type_code="060")])
    assert df["symbol"].tolist() == ["000011"]


def test_normalize_keeps_same_stock_with_different_reasons():
    """同股同日多原因上榜必须各留一行——主键含 reason 的理由。"""
    df = normalize_billboard([raw_row(reason="原因A"), raw_row(reason="原因B")])
    assert len(df) == 2
    df2 = normalize_billboard([raw_row(reason="原因A"), raw_row(reason="原因A")])
    assert len(df2) == 1  # 上游偶发重复行去重


def test_normalize_empty_returns_empty_frame():
    assert normalize_billboard([]).empty


def test_fetch_raw_handles_empty_business_code():
    """code=9201「返回数据为空」是非交易日的正常应答，不能当异常。"""
    def fake(params):
        return {"result": None, "success": False, "message": "返回数据为空", "code": 9201}

    assert fetch_billboard_raw(date(2026, 7, 25), request=fake) == []


def test_fetch_raw_raises_on_real_failure():
    def fake(params):
        return {"result": None, "success": False, "message": "系统繁忙", "code": 500}

    with pytest.raises(CrawlError):
        fetch_billboard_raw(date(2026, 7, 24), request=fake)


def test_fetch_raw_follows_pagination():
    seen = []

    def fake(params):
        seen.append(params["pageNumber"])
        page = params["pageNumber"]
        return {"success": True, "code": 0,
                "result": {"pages": 3, "count": 3,
                           "data": [raw_row(code=f"00000{page}")]}}

    rows = fetch_billboard_raw(date(2026, 7, 24), request=fake)
    assert seen == [1, 2, 3] and len(rows) == 3


def test_fetch_raw_missing_field_raises():
    def fake(params):
        bad = raw_row()
        del bad["BILLBOARD_NET_AMT"]
        return {"success": True, "code": 0, "result": {"pages": 1, "data": [bad]}}

    with pytest.raises(CrawlError):
        fetch_billboard(date(2026, 7, 24), request=fake)


def day_rows(day: date, n: int = 2) -> pd.DataFrame:
    return normalize_billboard([
        raw_row(code=str(i).zfill(6), day=day.strftime("%Y-%m-%d %H:%M:%S"))
        for i in range(n)
    ])


def test_update_billboard_is_incremental_and_idempotent(conn):
    fetched: list[date] = []

    def fake_fetch(day):
        fetched.append(day)
        return day_rows(day)

    r1 = update_billboard(conn, default_start=DAYS[0], end=DAYS[4], sleep_s=0,
                          fetch=fake_fetch)
    assert r1.total_days == 5 and r1.new_rows == 10

    fetched.clear()
    r2 = update_billboard(conn, default_start=DAYS[0], end=DAYS[6], sleep_s=0,
                          fetch=fake_fetch)
    assert fetched == DAYS[5:7]      # 只补没采过的两天
    assert r2.new_rows == 4

    fetched.clear()
    r3 = update_billboard(conn, default_start=DAYS[0], end=DAYS[6], sleep_s=0,
                          fetch=fake_fetch)
    assert fetched == [] and r3.new_rows == 0   # 已最新：一个请求都不发


def test_update_billboard_skips_non_trading_days(conn):
    """日历里没有的日子（周末）根本不该发请求。"""
    fetched = []

    def fake_fetch(day):
        fetched.append(day)
        return day_rows(day)

    update_billboard(conn, default_start=DAYS[0], end=DAYS[-1] + timedelta(days=2),
                     sleep_s=0, fetch=fake_fetch)
    assert fetched == DAYS
    assert all(d.weekday() < 5 for d in fetched)


def test_update_billboard_degrades_then_circuit_breaks(conn):
    """单日失败降级继续；失败比例超限则熔断。"""
    def flaky(day):
        if day == DAYS[1]:
            raise ConnectionError("模拟单日失败")
        return day_rows(day)

    r = update_billboard(conn, default_start=DAYS[0], end=DAYS[4], sleep_s=0,
                         max_fail_ratio=0.5, fetch=flaky)
    assert r.failed == [str(DAYS[1])] and r.new_rows == 8

    with pytest.raises(IngestError):
        update_billboard(conn, default_start=DAYS[0], end=DAYS[-1], sleep_s=0,
                         max_fail_ratio=0.1,
                         fetch=lambda d: (_ for _ in ()).throw(ConnectionError("全挂")))


def test_forced_start_enables_backfill(conn):
    """回归：库里已有近期数据后，增量起点只会向前；回溯更早历史必须靠显式 start。"""
    idempotent_insert(conn, "dragon_tiger", day_rows(DAYS[8]))
    fetched = []

    def fake_fetch(day):
        fetched.append(day)
        return day_rows(day)

    # 不给 start：认为已最新，一个请求都不发
    r = update_billboard(conn, default_start=DAYS[0], end=DAYS[8], sleep_s=0, fetch=fake_fetch)
    assert fetched == [] and r.total_days == 0

    # 显式 start：从头重采，已有日期靠幂等去重（第 9 天不产生新行）
    r = update_billboard(conn, default_start=DAYS[0], start=DAYS[0], end=DAYS[8],
                         sleep_s=0, fetch=fake_fetch)
    assert fetched == DAYS[:9]
    assert r.new_rows == 16  # 9 天 × 2 行，其中第 9 天的 2 行已存在


def test_next_billboard_date(conn):
    assert next_billboard_date(conn, DAYS[0]) == DAYS[0]
    idempotent_insert(conn, "dragon_tiger", day_rows(DAYS[3]))
    assert next_billboard_date(conn, DAYS[0]) == DAYS[3] + timedelta(days=1)
