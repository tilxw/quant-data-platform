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
