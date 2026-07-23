"""qdp.datasource 清洗与重试逻辑测试（全部离线，不碰网络）。"""

from __future__ import annotations

import pandas as pd
import pytest

from qdp.datasource import (
    BAR_COLUMNS,
    DataSourceError,
    call_with_retry,
    compute_hfq_factors,
    normalize_bars,
)


def make_raw_bars() -> pd.DataFrame:
    """仿照东财接口返回结构造的假原始数据。"""
    return pd.DataFrame({
        "日期": ["2026-07-20", "2026-07-21"],
        "股票代码": ["000001", "000001"],
        "开盘": [10.0, 10.5],
        "收盘": [10.5, 11.0],
        "最高": [10.6, 11.2],
        "最低": [9.9, 10.4],
        "成交量": [1000, 2000],  # 单位：手
        "成交额": [1.05e6, 2.2e6],
        "振幅": [1.0, 1.0],  # 多余列应被丢弃
    })


def test_normalize_bars_maps_columns_and_units():
    df = normalize_bars(make_raw_bars(), "1")  # 故意传 "1"，验证补前导零
    assert list(df.columns) == BAR_COLUMNS
    assert df["symbol"].tolist() == ["000001", "000001"]
    assert df["volume"].tolist() == [100_000, 200_000]  # 手 → 股
    assert str(df["trade_date"].iloc[0]) == "2026-07-20"


def test_normalize_bars_missing_column_raises():
    raw = make_raw_bars().drop(columns=["收盘"])
    with pytest.raises(DataSourceError):
        normalize_bars(raw, "000001")


def test_compute_hfq_factors():
    bars = normalize_bars(make_raw_bars(), "000001")
    hfq = pd.DataFrame({"trade_date": bars["trade_date"], "hfq_close": [105.0, 165.0]})
    out = compute_hfq_factors(bars, hfq)
    assert out["hfq_factor"].tolist() == [10.0, 15.0]  # 105/10.5, 165/11.0


def test_normalize_sina_bars_keeps_volume_unit():
    """新浪 volume 单位已是股，不能再乘 100。"""
    from qdp.datasource import _sina_symbol, normalize_sina_bars

    raw = pd.DataFrame({
        "date": ["2026-07-22"],
        "open": [3.11], "high": [3.22], "low": [3.09], "close": [3.19],
        "volume": [163572262.0], "amount": [517044810.0],
        "outstanding_share": [9.7e9], "turnover": [0.0168],  # 多余列应被丢弃
    })
    df = normalize_sina_bars(raw, "2")
    assert list(df.columns) == BAR_COLUMNS
    assert df["symbol"].iloc[0] == "000002"
    assert df["volume"].iloc[0] == 163572262
    assert _sina_symbol("600519") == "sh600519"
    assert _sina_symbol("2") == "sz000002"


def test_normalize_tx_bars_converts_volume_to_shares():
    """腾讯 volume 单位是手，必须 ×100 转股（与新浪相反，别搞混）。"""
    from qdp.datasource import normalize_tx_bars

    raw = pd.DataFrame({
        "date": ["2026-07-22"],
        "open": [10.81], "close": [10.98], "high": [10.98], "low": [10.77],
        "volume": [1029484.0], "turnover": [0.0053], "amount": [1.12e9],
    })
    df = normalize_tx_bars(raw, "1")
    assert list(df.columns) == BAR_COLUMNS
    assert df["symbol"].iloc[0] == "000001"
    assert df["volume"].iloc[0] == 102948400  # 手 → 股


def test_call_with_retry_recovers_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("网络抖动")
        return "ok"

    assert call_with_retry(flaky, what="测试", attempts=3, base_delay_s=0) == "ok"
    assert calls["n"] == 3


def test_call_with_retry_exhausts_and_raises():
    def always_fail():
        raise ConnectionError("一直失败")

    with pytest.raises(ConnectionError):
        call_with_retry(always_fail, what="测试", attempts=2, base_delay_s=0)
