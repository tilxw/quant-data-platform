"""qdp.quality 测试：全部离线，用内存库+合成数据。"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from qdp import quality as qc
from qdp.db import connect, init_schema
from qdp.ingest import idempotent_insert

# 2026-07-06（周一）起的 10 个工作日
DAYS10 = [d for d in (date(2026, 7, 6) + timedelta(days=i) for i in range(14))
          if d.weekday() < 5][:10]


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    idempotent_insert(c, "trade_calendar",
                      pd.DataFrame({"cal_date": DAYS10, "is_open": True}))
    yield c
    c.close()


def seed(conn, symbol, days, closes=None, factors=None):
    closes = closes if closes is not None else [10.0] * len(days)
    factors = factors if factors is not None else [2.0] * len(days)
    idempotent_insert(conn, "daily_bar", pd.DataFrame({
        "symbol": symbol, "trade_date": days,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1000, "amount": 1e6,
    }))
    idempotent_insert(conn, "adj_factor", pd.DataFrame({
        "symbol": symbol, "trade_date": days, "hfq_factor": factors,
    }))


def by_symbol(items, check):
    return {i.symbol: i for i in items if i.check_name == check and i.symbol != "ALL"}


def test_completeness_grades_gaps_and_excludes_prelisting(conn):
    seed(conn, "AAAA", DAYS10)                       # 完整
    seed(conn, "BBBB", DAYS10[4:])                   # 上市晚但自此完整：不算缺
    seed(conn, "CCCC", DAYS10[:5] + DAYS10[6:])      # 缺 1 天（10%）→ INFO
    seed(conn, "DDDD", DAYS10[:7])                   # 缺尾部 3 天（30%）→ WARN
    items = qc.check_completeness(conn, warn_ratio=0.10)
    got = by_symbol(items, "completeness")
    assert "AAAA" not in got and "BBBB" not in got
    assert got["CCCC"].level == qc.INFO and got["CCCC"].metric == 1
    assert got["DDDD"].level == qc.WARN and got["DDDD"].metric == 3


def test_freshness_levels(conn):
    seed(conn, "AAAA", DAYS10[:7])  # 落后 3 个交易日
    today = DAYS10[-1] + timedelta(days=1)
    item = qc.check_freshness(conn, today=today)[0]
    assert item.level == qc.WARN and item.metric == 3

    seed(conn, "BBBB", DAYS10)      # 补齐到最新后：INFO
    item = qc.check_freshness(conn, today=today)[0]
    assert item.level == qc.INFO and item.metric == 0


def test_factor_drop_is_error_but_rounding_noise_ignored(conn):
    # EEEE：第 8 天因子从 2.0 掉到 1.5（相邻日回撤 25%）→ ERROR（因子不会下降，回撤只能是数据错），只算 1 天
    seed(conn, "EEEE", DAYS10, factors=[2.0] * 7 + [1.5] + [2.0, 2.0])
    # FFFF：因子有 ±0.2% 抖动（收盘价舍入噪声量级）→ 不报
    seed(conn, "FFFF", DAYS10, factors=[2.0, 2.004] * 5)
    items = qc.check_factor_and_returns(conn)
    got = by_symbol(items, "factor_sanity")
    assert "EEEE" in got and got["EEEE"].level == qc.ERROR and got["EEEE"].metric == 1
    assert "FFFF" not in got


def test_extreme_return_flagged_but_new_stock_days_skipped(conn):
    # GGGG：第 8 天暴涨 40% → 报；HHHH：第 3 天暴涨（上市初期）→ 跳过不报
    seed(conn, "GGGG", DAYS10, closes=[10.0] * 7 + [14.0] + [14.0, 14.0])
    seed(conn, "HHHH", DAYS10, closes=[10.0, 10.0, 14.0] + [14.0] * 7)
    items = qc.check_factor_and_returns(conn)
    got = by_symbol(items, "return_sanity")
    assert "GGGG" in got and got["GGGG"].level == qc.ERROR and got["GGGG"].metric >= 1
    assert "HHHH" not in got


def test_compare_is_anchor_independent():
    """两源后复权锚点差一个倍数时，收益率对拍应完全通过——规则的灵魂。"""
    closes = [10.0, 10.1, 10.2, 10.3, 10.4]
    db = pd.DataFrame({"trade_date": DAYS10[:5], "close": closes,
                       "hfq_close": [c * 2.0 for c in closes]})
    src_bars = pd.DataFrame({"trade_date": DAYS10[:5], "close": closes})
    src_hfq = pd.DataFrame({"trade_date": DAYS10[:5],
                            "hfq_close": [c * 2.0 * 3.7 for c in closes]})  # 锚点差 3.7 倍
    r = qc.compare_with_source(db, src_bars, src_hfq, return_tol=1e-4)
    assert r["overlap"] == 5 and r["raw_bad"] == 0 and r["ret_bad"] == 0


def test_compare_catches_real_divergence():
    closes = [10.0, 10.1, 10.2, 10.3, 10.4]
    db = pd.DataFrame({"trade_date": DAYS10[:5], "close": closes,
                       "hfq_close": [c * 2.0 for c in closes]})
    src_bars = pd.DataFrame({"trade_date": DAYS10[:5],
                             "close": [10.0, 10.1, 10.25, 10.3, 10.4]})  # 一天价不一致
    hfq = [c * 2.0 for c in closes]
    hfq[3] *= 1.01                                                       # 一天复权价被污染
    src_hfq = pd.DataFrame({"trade_date": DAYS10[:5], "hfq_close": hfq})
    r = qc.compare_with_source(db, src_bars, src_hfq, return_tol=1e-4)
    assert r["raw_bad"] == 1
    assert r["ret_bad"] == 2  # 污染一天影响前后两个收益率
    assert r["ret_max"] > 1e-3


def test_compare_tolerates_mixed_date_dtypes():
    """回归：库里读出 datetime64、抓取清洗后是 date 对象，合并不得报错。"""
    closes = [10.0, 10.1, 10.2]
    db = pd.DataFrame({"trade_date": pd.to_datetime(DAYS10[:3]),  # datetime64
                       "close": closes, "hfq_close": [c * 2 for c in closes]})
    src_bars = pd.DataFrame({"trade_date": DAYS10[:3], "close": closes})  # date 对象
    src_hfq = pd.DataFrame({"trade_date": DAYS10[:3], "hfq_close": [c * 2 for c in closes]})
    r = qc.compare_with_source(db, src_bars, src_hfq, return_tol=1e-4)
    assert r["overlap"] == 3 and r["raw_bad"] == 0 and r["ret_bad"] == 0


def test_pick_sample_includes_sentinels_and_rotates():
    pool = [str(i).zfill(6) for i in range(100)]
    sentinels = ["600519", "000001"]
    got = qc.pick_sample(pool + sentinels, sentinels, 10, rng=random.Random(42))
    assert set(sentinels) <= set(got)
    assert len(got) == 12


def test_render_surfaces_error_above_truncation(conn):
    """回归：ERROR 不得被大量 WARN 挤进 15 条截断区之下。"""
    seed(conn, "AAAA", DAYS10)
    items = [qc.QcItem("cross_source", str(i).zfill(6), qc.WARN, None, "对拍失败")
             for i in range(20)]
    items.append(qc.QcItem("cross_source", "600221", qc.ERROR, 873.0, "严重分歧"))
    md = qc.render_markdown(conn, items, datetime(2026, 7, 24, 12, 0), "东财 × 21 只")
    assert "600221" in md and "严重分歧" in md


def test_persist_and_render(conn):
    seed(conn, "AAAA", DAYS10)
    items = [qc.QcItem("freshness", "ALL", qc.ERROR, 5.0, "落后 5 个交易日"),
             qc.QcItem("cross_source", "AAAA", qc.INFO, 0.0, "全对上")]
    run_ts = datetime(2026, 7, 24, 10, 0, 0)
    qc.persist_results(conn, run_ts, items)
    assert conn.execute("SELECT count(*) FROM qc_result").fetchone()[0] == 2
    md = qc.render_markdown(conn, items, run_ts, "东财 × 1 只")
    assert "数据质检报告" in md and "ERROR 1" in md and "落后 5 个交易日" in md
