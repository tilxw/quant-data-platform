"""qdp.crosscheck 测试：与 vectorbt 的对拍必须在零成本下逼到机器精度。

这些测试会真的调用 vectorbt（比其他测试慢几秒），它们是"自研引擎正确性"的
最终防线：任何执行层改动一旦算错，零成本对拍立刻从 1e-16 崩到可见量级。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qdp.backtest import CostModel, run_backtest
from qdp.crosscheck import compare_navs, crosscheck, reference_nav

pytest.importorskip("vectorbt")

DAYS = pd.date_range("2026-01-05", periods=30, freq="B")


def synthetic(n_assets: int, seed: int = 0, vol: float = 0.02) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cols = [f"{i:06d}" for i in range(n_assets)]
    return pd.DataFrame(
        {c: 10 * np.cumprod(1 + rng.normal(0.001, vol, len(DAYS))) for c in cols},
        index=DAYS,
    )


def rotating_targets(prices: pd.DataFrame, rebalance_idx=(0, 10, 20)) -> pd.DataFrame:
    """每次调仓换一批持仓，制造真实的买卖混合。"""
    target = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    n = len(prices.columns)
    for k, i in enumerate(rebalance_idx):
        w = np.zeros(n)
        for j in range(min(3, n)):
            w[(k + j) % n] = 1.0 / min(3, n)
        target.loc[prices.index[i]] = w
    return target


@pytest.mark.parametrize("n_assets", [1, 3, 8])
def test_zero_cost_matches_vectorbt_to_machine_precision(n_assets):
    px = synthetic(n_assets)
    report, _ = crosscheck(px, rotating_targets(px), fee_rate=0.0)
    assert report["最大相对偏差"] < 1e-10, report


def test_zero_cost_matches_with_partial_cash_position():
    """含现金仓位时也必须对上——曾经这里差 1e-2（现金没进分母）。"""
    px = synthetic(1)
    target = pd.DataFrame(np.nan, index=px.index, columns=px.columns)
    col = px.columns[0]
    target.loc[px.index[0], col] = 1.0
    target.loc[px.index[10], col] = 0.5      # 半仓，另一半现金
    target.loc[px.index[20], col] = 1.0
    report, _ = crosscheck(px, target, fee_rate=0.0)
    assert report["最大相对偏差"] < 1e-10, report


def test_fee_residual_is_first_order_in_fee_rate():
    """含成本残差应随费率线性缩放——证明它是口径差异而非 bug。"""
    px = synthetic(3, vol=0.03)
    target = rotating_targets(px)
    d1 = crosscheck(px, target, fee_rate=1e-3)[0]["最大相对偏差"]
    d2 = crosscheck(px, target, fee_rate=1e-4)[0]["最大相对偏差"]
    assert d1 > 1e-6                       # 确实有可观测的差异
    assert 5 < d1 / d2 < 20                # 一阶：费率降 10 倍，残差降约 10 倍


@pytest.mark.parametrize("n_assets", [1, 3, 8])
def test_engine_matches_naive_reference(n_assets):
    """自研引擎 vs 逐日循环参考实现：必须到机器精度（第三方裁判，不依赖 vectorbt）。"""
    px = synthetic(n_assets, seed=3)
    target = rotating_targets(px)
    mine = run_backtest(px, target, CostModel(0, 0, 0)).nav
    ref = reference_nav(px, target)
    assert float(((mine - ref).abs() / ref.abs()).max()) < 1e-12


def test_reference_charges_fees_like_engine():
    px = synthetic(3, seed=4)
    target = rotating_targets(px)
    fee = 0.001
    mine = run_backtest(px, target, CostModel(commission=fee, stamp_tax=0, slippage=0)).nav
    ref = reference_nav(px, target, fee_rate=fee)
    # 成本口径一致：两者应远比"自研 vs vectorbt"的一阶差异更接近
    assert float(((mine - ref).abs() / ref.abs()).max()) < 1e-6


def test_compare_navs_alignment():
    a = pd.Series([1.0, 1.1, 1.2], index=DAYS[:3])
    b = pd.Series([1.0, 1.1, 1.3], index=DAYS[:3])
    r = compare_navs(a, b)
    assert r["对比天数"] == 3
    assert r["最大绝对偏差"] == pytest.approx(0.1)
