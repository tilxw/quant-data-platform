"""qdp.backtest 测试：引擎数值正确性、防未来函数、成本模型（全部离线）。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qdp.backtest import (
    CostModel,
    equal_weight_benchmark,
    performance_stats,
    quantile_weights,
    run_backtest,
    weekly_rebalance_dates,
)

DAYS = pd.DatetimeIndex([d for d in (date(2026, 1, 5) + timedelta(days=i) for i in range(40))
                         if d.weekday() < 5])
NO_COST = CostModel(commission=0.0, stamp_tax=0.0, slippage=0.0)


def make_prices(paths: dict[str, list[float]], days=None) -> pd.DataFrame:
    days = days if days is not None else DAYS[:len(next(iter(paths.values())))]
    return pd.DataFrame(paths, index=days)


def single_target(prices: pd.DataFrame, day, weights: dict) -> pd.DataFrame:
    target = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    row = pd.Series(0.0, index=prices.columns)
    for sym, w in weights.items():
        row[sym] = w
    target.loc[day] = row
    return target


def test_single_asset_matches_buy_and_hold():
    """满仓单只股票、零成本：净值必须等于该股自身涨幅。"""
    px = make_prices({"A": [10.0 * 1.01 ** i for i in range(20)]})
    target = single_target(px, px.index[0], {"A": 1.0})
    r = run_backtest(px, target, NO_COST)
    # 第 0 天定权重，第 1 天开始持有 → 净值 = 从第 1 天到最后的累计涨幅
    assert r.nav.iloc[-1] == pytest.approx(px["A"].iloc[-1] / px["A"].iloc[0])


def test_equal_weight_two_assets():
    """两只股票各半仓，一涨一跌，组合收益应是两者平均。"""
    px = make_prices({
        "A": [10.0 * 1.02 ** i for i in range(11)],
        "B": [20.0 * 0.99 ** i for i in range(11)],
    })
    target = single_target(px, px.index[0], {"A": 0.5, "B": 0.5})
    r = run_backtest(px, target, NO_COST)
    # 第一持有日（index 1）的组合收益 = (2% + (-1%)) / 2
    assert r.returns.iloc[1] == pytest.approx((0.02 - 0.01) / 2)


def test_weights_drift_between_rebalances():
    """两次调仓之间不做再平衡：涨的那只权重会自然变大。"""
    px = make_prices({
        "A": [10.0 * 1.10 ** i for i in range(6)],   # 猛涨
        "B": [10.0] * 6,                             # 不动
    })
    target = single_target(px, px.index[0], {"A": 0.5, "B": 0.5})
    r = run_backtest(px, target, NO_COST)
    assert r.weights.loc[px.index[1], "A"] > 0.5     # A 涨完权重超过一半
    assert r.weights.loc[px.index[1]].sum() == pytest.approx(1.0)


def test_no_lookahead_signal_applies_next_day():
    """T 日定的权重，T 日当天不能产生收益——只能从 T+1 起。"""
    px = make_prices({"A": [10.0, 20.0, 22.0, 24.2]})  # 第 1 天暴涨 100%
    target = single_target(px, px.index[1], {"A": 1.0})  # 在暴涨当天收盘才建仓
    r = run_backtest(px, target, NO_COST)
    assert r.returns.iloc[1] == pytest.approx(0.0)     # 没吃到当天那 100%
    assert r.returns.iloc[2] == pytest.approx(0.1)     # 从次日开始吃 10%


def test_no_lookahead_future_prices_cannot_change_past_nav():
    """篡改未来价格，过去的净值必须完全不变。"""
    base_px = make_prices({"A": [10.0 * 1.01 ** i for i in range(20)]})
    target = single_target(base_px, base_px.index[0], {"A": 1.0})
    base = run_backtest(base_px, target, NO_COST)

    tampered_px = base_px.copy()
    tampered_px.iloc[10:] *= 5      # 第 10 天起价格翻五倍
    tampered = run_backtest(tampered_px, target, NO_COST)

    pd.testing.assert_series_equal(base.nav.iloc[:10], tampered.nav.iloc[:10])


def test_cost_charged_on_rebalance_only():
    """成本只在调仓日发生；建仓换手率为 1（空仓→满仓）。"""
    px = make_prices({"A": [10.0] * 6})   # 价格不动，净值变化只来自成本
    target = single_target(px, px.index[0], {"A": 1.0})
    cost = CostModel(commission=0.001, stamp_tax=0.0, slippage=0.0)
    r = run_backtest(px, target, cost)
    assert r.turnover.iloc[0] == pytest.approx(1.0)
    assert r.costs.iloc[0] == pytest.approx(0.001)
    assert r.costs.iloc[1:].sum() == pytest.approx(0.0)   # 之后没有调仓
    assert r.nav.iloc[-1] == pytest.approx(1 - 0.001)


def test_stamp_tax_charged_on_sell_side_only():
    """印花税只对卖出部分征收——A 股特有细节。"""
    px = make_prices({"A": [10.0] * 8, "B": [10.0] * 8})
    target = pd.DataFrame(np.nan, index=px.index, columns=px.columns)
    target.loc[px.index[0]] = [1.0, 0.0]     # 全仓 A
    target.loc[px.index[3]] = [0.0, 1.0]     # 换成全仓 B：卖出 1.0、买入 1.0
    only_stamp = CostModel(commission=0.0, slippage=0.0, stamp_tax=0.002)
    r = run_backtest(px, target, only_stamp)
    assert r.costs.iloc[0] == pytest.approx(0.0)        # 建仓只买不卖，免税
    assert r.turnover.iloc[3] == pytest.approx(2.0)     # 双边换手 = 卖1 + 买1
    assert r.costs.iloc[3] == pytest.approx(1.0 * 0.002)  # 只对卖出的 1.0 收税


def test_partial_investment_is_diluted_by_cash():
    """半仓（另一半现金）时收益必须减半——回归：现金曾被从分母里除掉，半仓被当满仓。"""
    px = make_prices({"A": [10.0, 11.0, 12.1]})   # 每天涨 10%
    half = single_target(px, px.index[0], {"A": 0.5})
    r = run_backtest(px, half, NO_COST)
    assert r.returns.iloc[1] == pytest.approx(0.05)    # 10% 的一半
    full = single_target(px, px.index[0], {"A": 1.0})
    assert run_backtest(px, full, NO_COST).returns.iloc[1] == pytest.approx(0.10)


def test_cash_weight_tracks_stock_weight_drift():
    """半仓状态下股票涨了，股票权重应超过 50%（现金没跟着涨）。"""
    px = make_prices({"A": [10.0, 20.0, 20.0]})
    r = run_backtest(px, single_target(px, px.index[0], {"A": 0.5}), NO_COST)
    assert r.weights.loc[px.index[1], "A"] == pytest.approx(1 / 1.5)  # 1.0/(1.0+0.5)


def test_quantile_weights_picks_top_group_equally():
    px = make_prices({s: [10.0] * 10 for s in list("ABCDEFGHIJ")})
    factor = pd.DataFrame(
        {s: [float(i)] * 10 for i, s in enumerate(list("ABCDEFGHIJ"))}, index=px.index
    )  # J 最高、A 最低
    target = quantile_weights(factor, px, [px.index[0]], n_groups=5, group=1)
    row = target.loc[px.index[0]]
    assert set(row[row > 0].index) == {"I", "J"}          # 最高组是前 20%
    assert row.sum() == pytest.approx(1.0)
    assert row["J"] == pytest.approx(0.5)                  # 等权

    bottom = quantile_weights(factor, px, [px.index[0]], n_groups=5, group=5)
    brow = bottom.loc[px.index[0]]
    assert set(brow[brow > 0].index) == {"A", "B"}         # 最低组


def test_quantile_weights_excludes_suspended_stocks():
    """停牌（当日无价格）的股票不能被选进组合。"""
    px = make_prices({s: [10.0] * 10 for s in list("ABCDEFGHIJ")})
    px.loc[px.index[0], "J"] = np.nan                      # J 当天停牌
    factor = pd.DataFrame(
        {s: [float(i)] * 10 for i, s in enumerate(list("ABCDEFGHIJ"))}, index=px.index
    )
    row = quantile_weights(factor, px, [px.index[0]], n_groups=5, group=1).loc[px.index[0]]
    assert "J" not in row[row > 0].index


def test_benchmark_is_equal_weight_over_available():
    px = make_prices({"A": [10.0] * 5, "B": [10.0] * 5, "C": [10.0] * 5})
    px.loc[px.index[0], "C"] = np.nan
    row = equal_weight_benchmark(px, [px.index[0]]).loc[px.index[0]]
    assert row["A"] == pytest.approx(0.5) and row["C"] == 0.0


def test_weekly_rebalance_dates_are_week_ends():
    days = pd.DatetimeIndex([d for d in (date(2026, 1, 5) + timedelta(days=i)
                                         for i in range(21)) if d.weekday() < 5])
    reb = weekly_rebalance_dates(days)
    assert list(reb) == [pd.Timestamp("2026-01-09"), pd.Timestamp("2026-01-16"),
                         pd.Timestamp("2026-01-23")]   # 每周五
    assert all(d in days for d in reb)


def test_performance_stats_drawdown_and_sharpe():
    px = make_prices({"A": [10, 11, 12, 9, 10, 11, 12, 13, 14, 15]})
    target = single_target(px, px.index[0], {"A": 1.0})
    r = run_backtest(px, target, NO_COST)
    nav = r.nav
    assert r.stats["最大回撤"] == pytest.approx((1 - nav / nav.cummax()).max())
    assert r.stats["累计收益"] == pytest.approx(nav.iloc[-1] - 1)
    assert r.stats["交易日数"] == len(px)


def test_performance_stats_empty():
    empty = run_backtest(make_prices({"A": [10.0, 10.0]}),
                         pd.DataFrame(np.nan, index=DAYS[:2], columns=["A"]), NO_COST)
    assert empty.nav.iloc[-1] == pytest.approx(1.0)   # 从未建仓：净值恒为 1
    assert performance_stats(empty)["累计收益"] == pytest.approx(0.0)
