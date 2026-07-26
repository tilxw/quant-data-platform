"""qdp.factors 测试：因子数值正确性、防未来函数、停牌处理（全部离线）。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from qdp.db import connect, init_schema
from qdp.factors import FACTOR_SPECS, compute_factors, load_panel, update_factors
from qdp.ingest import idempotent_insert

# 60 个连续工作日，够 lhb_cnt60 的窗口
DAYS = [d for d in (date(2026, 1, 1) + timedelta(days=i) for i in range(120))
        if d.weekday() < 5][:80]


def make_panel(symbol="000001", closes=None, amounts=None, billboard=None, days=None):
    days = days or DAYS[:len(closes or DAYS)]
    n = len(days)
    return pd.DataFrame({
        "symbol": symbol,
        "trade_date": days,
        "hfq_close": closes if closes is not None else [10.0] * n,
        "amount": amounts if amounts is not None else [1e8] * n,
        "on_billboard": billboard if billboard is not None else [0.0] * n,
    })


def pick(long: pd.DataFrame, name: str, day: date) -> float:
    row = long[(long["factor_name"] == name) & (long["trade_date"] == day)]
    return float(row["value"].iloc[0])


def test_mom20_is_20_bar_return():
    # 每天涨 1%：20 个交易日的累计涨幅 = 1.01^20 - 1
    closes = [10.0 * 1.01 ** i for i in range(40)]
    long = compute_factors(make_panel(closes=closes))
    assert pick(long, "mom20", DAYS[20]) == pytest.approx(1.01 ** 20 - 1)


def test_rev5_is_negated_return():
    closes = [10.0 * 1.02 ** i for i in range(40)]
    long = compute_factors(make_panel(closes=closes))
    assert pick(long, "rev5", DAYS[10]) == pytest.approx(-(1.02 ** 5 - 1))


def test_vol20_matches_manual_std():
    closes = [10.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(1, 41)]
    panel = make_panel(closes=closes)
    long = compute_factors(panel)
    ret = pd.Series(closes).pct_change()
    assert pick(long, "vol20", DAYS[25]) == pytest.approx(ret.iloc[6:26].std())


def test_illiq20_scales_by_amount():
    """成交额翻倍，Amihud 非流动性减半——数值含义要站得住。"""
    closes = [10.0 * 1.01 ** i for i in range(40)]
    a = compute_factors(make_panel(closes=closes, amounts=[1e8] * 40))
    b = compute_factors(make_panel(closes=closes, amounts=[2e8] * 40))
    assert pick(a, "illiq20", DAYS[30]) == pytest.approx(2 * pick(b, "illiq20", DAYS[30]))


def test_lhb_cnt60_counts_trailing_window():
    billboard = [0.0] * 80
    for i in (5, 10, 40):
        billboard[i] = 1.0
    long = compute_factors(make_panel(closes=[10.0] * 80, billboard=billboard))
    assert pick(long, "lhb_cnt60", DAYS[59]) == 3.0   # 三次都在窗口内
    assert pick(long, "lhb_cnt60", DAYS[70]) == 1.0   # 只剩第 40 天那次


def test_no_lookahead_future_data_cannot_change_past_factor():
    """核心测试：改掉未来的价格，过去的因子值必须纹丝不动。"""
    closes = [10.0 * 1.01 ** i for i in range(40)]
    base = compute_factors(make_panel(closes=closes))
    tampered_closes = closes[:30] + [c * 5 for c in closes[30:]]  # 第 30 天起暴涨
    tampered = compute_factors(make_panel(closes=tampered_closes))
    for name in ("mom20", "vol20", "rev5", "illiq20"):
        for day in DAYS[20:29]:
            assert pick(base, name, day) == pytest.approx(pick(tampered, name, day)), \
                f"{name} 在 {day} 被未来数据影响了——存在未来函数"


def test_short_window_produces_no_row():
    """窗口不足的日子不出因子值（不存 NaN，也不用半截窗口凑）。"""
    long = compute_factors(make_panel(closes=[10.0] * 10))
    assert long[long["factor_name"] == "mom20"].empty      # 10 行算不出 20 日动量
    assert not long[long["factor_name"] == "rev5"].empty   # 5 日反转够


def test_factors_never_leak_across_symbols():
    """两只股票拼在一起算，不能互相污染。"""
    a = make_panel("000001", closes=[10.0 * 1.01 ** i for i in range(40)])
    b = make_panel("600000", closes=[50.0 * 0.99 ** i for i in range(40)])
    joint = compute_factors(pd.concat([a, b], ignore_index=True))
    alone_a = compute_factors(a)
    got = joint[(joint["symbol"] == "000001") & (joint["factor_name"] == "mom20")]
    exp = alone_a[alone_a["factor_name"] == "mom20"]
    assert got["value"].tolist() == pytest.approx(exp["value"].tolist())


def test_empty_panel():
    assert compute_factors(pd.DataFrame(columns=[
        "symbol", "trade_date", "hfq_close", "amount", "on_billboard"])).empty


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def seed_db(conn, symbol="000001", n=40, billboard_days=()):
    days = DAYS[:n]
    closes = [10.0 * 1.01 ** i for i in range(n)]
    idempotent_insert(conn, "daily_bar", pd.DataFrame({
        "symbol": symbol, "trade_date": days, "open": closes, "high": closes,
        "low": closes, "close": closes, "volume": 1000, "amount": 1e8,
    }))
    idempotent_insert(conn, "adj_factor", pd.DataFrame({
        "symbol": symbol, "trade_date": days, "hfq_factor": 2.0,
    }))
    if billboard_days:
        idempotent_insert(conn, "dragon_tiger", pd.DataFrame({
            "symbol": symbol, "trade_date": [days[i] for i in billboard_days],
            "reason": "日涨幅偏离值达7%的证券",
        }))


def test_load_panel_marks_billboard_days_once(conn):
    seed_db(conn, billboard_days=(3, 7))
    # 同股同日多次上榜只应算一天
    idempotent_insert(conn, "dragon_tiger", pd.DataFrame({
        "symbol": "000001", "trade_date": [DAYS[3]], "reason": "另一个上榜原因",
    }))
    panel = load_panel(conn)
    assert len(panel) == 40                       # 没因为重复上榜而多出行
    assert panel["on_billboard"].sum() == 2.0


def test_load_panel_uses_hfq_price(conn):
    seed_db(conn)
    panel = load_panel(conn)
    assert panel["hfq_close"].iloc[0] == pytest.approx(10.0 * 2.0)  # 复权因子 2.0


def test_update_factors_is_recomputable(conn):
    """派生数据用 REPLACE 语义：重算覆盖旧值，不像行情那样只增不改。"""
    seed_db(conn)
    counts = update_factors(conn)
    assert set(counts) <= set(FACTOR_SPECS)
    n1 = conn.execute("SELECT count(*) FROM factor_value").fetchone()[0]
    assert n1 > 0

    conn.execute("UPDATE factor_value SET value = -999 WHERE factor_name = 'mom20'")
    update_factors(conn)
    n2 = conn.execute("SELECT count(*) FROM factor_value").fetchone()[0]
    bad = conn.execute("SELECT count(*) FROM factor_value WHERE value = -999").fetchone()[0]
    assert n2 == n1 and bad == 0   # 行数不变、被篡改的值已被重算覆盖
