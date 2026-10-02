"""qdp.source_diff 测试：数据源迁移对账的判定口径（全部离线，用合成序列）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qdp.source_diff import compare_databases, compare_symbol

DAYS = pd.bdate_range("2026-01-05", periods=30)


def walk(start: float, seed: int = 0) -> np.ndarray:
    """日涨跌约 ±2% 的价格序列，保留到分（和真实收盘价一样）。"""
    r = np.random.default_rng(seed).normal(0, 0.02, len(DAYS))
    return np.round(start * np.cumprod(1 + r), 2)


def series(closes, factors) -> pd.DataFrame:
    return pd.DataFrame({"trade_date": DAYS[:len(closes)], "close": closes, "hfq_factor": factors})


def test_identical_sources_are_consistent():
    px = walk(10)
    r = compare_symbol(series(px, 1.2), series(px, 1.2))
    assert r["status"] == "✅ 一致" and r["raw_bad"] == 0 and r["ret_bad"] == 0


def test_constant_factor_ratio_is_still_consistent():
    """复权基准不同、因子差一个固定倍数：收益率完全一样，不算分歧。"""
    px = walk(10)
    r = compare_symbol(series(px, 1.0), series(px, 1.07))
    assert r["status"] == "✅ 一致"
    assert abs(r["ratio_min"] - 1.07) < 1e-12 and abs(r["ratio_max"] - 1.07) < 1e-12


def test_single_exdate_difference_is_reported_with_its_date():
    """新源在第 10 天多做了一次 2% 的除权调整，旧源没有：只有那一天收益率分歧。"""
    px = walk(10)
    new_f = np.where(np.arange(len(px)) >= 10, 1.02, 1.0)
    r = compare_symbol(series(px, 1.0), series(px, new_f))
    assert r["status"] == "🟡 个别日不同"
    assert r["bad_dates"] == [str(DAYS[10].date())]
    assert (r["jumps_old"], r["jumps_new"]) == (0, 1)


def test_daily_drifting_factor_is_inconsistent_and_suspected():
    """旧源的"因子"随价格天天变（像东财的 000001），新源只是常数：口径不一致，疑点指向旧源。"""
    px = walk(10)
    r = compare_symbol(series(px, (px + 5) / px), series(px, 1.05))
    assert r["status"] == "🔴 口径不一致"
    assert r["suspect"] == "旧源因子跳变过多"


def test_raw_close_mismatch_is_flagged_first():
    """不复权收盘价是交易所数据，两源差一分钱也要报。"""
    px = walk(10)
    px_new = px.copy()
    px_new[7] += 0.01
    r = compare_symbol(series(px, 1.0), series(px_new, 1.0))
    assert r["status"] == "🔴 收盘价不一致" and r["raw_bad"] == 1


def test_low_price_rounding_noise_is_not_a_false_alarm():
    """低价股：新源的后复权价只保留到分，因子带舍入抖动；不能误判成分歧（"误报 55 只"的教训）。"""
    px = walk(2.0, seed=1)
    true_f = 1.3711
    noisy_f = np.round(px * true_f, 2) / px
    r = compare_symbol(series(px, true_f), series(px, noisy_f))
    assert r["status"] == "✅ 一致"


def test_compare_databases_covers_missing_symbols_and_days():
    px = walk(10)
    old = pd.concat([series(px, 1.0).assign(symbol="A"), series(px, 1.0).assign(symbol="B")])
    new_a = series(px, 1.0).drop(index=5).assign(symbol="A")       # 新库 A 缺一天
    new = pd.concat([new_a, series(px, 1.0).assign(symbol="C")])
    df = compare_databases(old, new).set_index("symbol")
    assert df.loc["A", "status"] == "✅ 一致" and df.loc["A", "only_old"] == 1
    assert df.loc["B", "status"] == "仅旧库有"
    assert df.loc["C", "status"] == "仅新库有"
