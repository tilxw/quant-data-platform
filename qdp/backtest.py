"""自研向量化回测引擎：多标的、周频调仓、含交易成本、防未来函数。

## 时间线约定（防未来函数的全部要害）

    T 日收盘 ──> 算出 T 日因子值 ──> 排序选股，定出目标权重
                                        │
    T+1 日 ────────────────────────────┘ 这一天开始持有新权重

所以引擎内部对权重做了一次 `shift(1)`：**T 日的因子只能决定 T+1 起的持仓**。
单元测试 `test_no_lookahead_*` 直接篡改未来价格来验证这条线没被跨过。

## 权重漂移模型

调仓日把权重重置到目标值，两次调仓之间**任持仓随价格自然漂移**（不做日内再平衡）——
这才是真实持股的样子，也和 vectorbt 的 `targetpercent` 语义一致，对拍才有意义。
实现上不逐日循环：用每只股票的累计净值 C 把"份数"表示成分段常量矩阵 U，
组合收益一次矩阵运算算完（见 run_backtest 内注释）。

## 成本模型（A 股口径）

- 佣金：双边收取；
- 印花税：**仅卖出单边**征收（很多回测会漏掉"单边"这一点）；
- 滑点：双边，按成交额比例近似。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
CASH = "__cash__"   # 现金在内部矩阵里占一列，名字加下划线避开真实股票代码


@dataclass(frozen=True)
class CostModel:
    """交易成本参数（A 股口径）。"""

    commission: float = 0.00025  # 佣金 万2.5，买卖双边
    stamp_tax: float = 0.001     # 印花税 千1，仅卖出
    slippage: float = 0.0005     # 滑点 万5，双边

    @property
    def two_way_rate(self) -> float:
        """双边费率（佣金+滑点），卖出还要另加印花税。"""
        return self.commission + self.slippage


@dataclass
class BacktestResult:
    nav: pd.Series                 # 净值曲线（起点 1.0）
    returns: pd.Series             # 每日净收益率（已扣成本）
    gross_returns: pd.Series       # 每日毛收益率（未扣成本）
    costs: pd.Series               # 每日成本（占组合净值比例）
    turnover: pd.Series            # 每日双边换手率
    weights: pd.DataFrame          # 每日实际持仓权重（已 shift，可直接喂给 vectorbt）
    stats: dict = field(default_factory=dict)


# ---------------- 面板准备 ----------------


def load_price_panel(conn) -> pd.DataFrame:
    """宽表价格面板：行=交易日，列=股票，值=后复权价。"""
    df = conn.execute("""
        SELECT b.trade_date, b.symbol,
               CAST(b.close AS DOUBLE) * f.hfq_factor AS hfq_close
        FROM daily_bar b JOIN adj_factor f USING (symbol, trade_date)
        ORDER BY b.trade_date, b.symbol
    """).df()
    return df.pivot(index="trade_date", columns="symbol", values="hfq_close")


def load_factor_panel(conn, factor_name: str) -> pd.DataFrame:
    """宽表因子面板：行=交易日，列=股票，值=因子值。"""
    df = conn.execute("""
        SELECT trade_date, symbol, value FROM factor_value
        WHERE factor_name = ? ORDER BY trade_date, symbol
    """, [factor_name]).df()
    if df.empty:
        raise ValueError(f"因子 {factor_name!r} 在库中没有数据，请先跑 compute_factors.py")
    return df.pivot(index="trade_date", columns="symbol", values="value")


def weekly_rebalance_dates(dates: pd.Index) -> pd.DatetimeIndex:
    """每个自然周的最后一个交易日作为调仓日（周频调仓）。

    用 resample 按周分组取每组最后一天——正是"日频转周频"那条惯用法的真实用例。
    """
    s = pd.Series(pd.to_datetime(dates), index=pd.to_datetime(dates))
    return pd.DatetimeIndex(s.resample("W").last().dropna().values)


# ---------------- 组合构建 ----------------


def quantile_weights(
    factor: pd.DataFrame,
    prices: pd.DataFrame,
    rebalance_dates: pd.Index,
    *,
    n_groups: int = 5,
    group: int = 1,
) -> pd.DataFrame:
    """按因子值分层，取第 group 组（1 = 因子值最高组）等权持有。

    只在调仓日给出目标权重，其余日期为 NaN（表示"不动"）。
    可选股票必须同时满足：当日有因子值、当日有价格（停牌股不能买）。
    """
    target = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    for day in rebalance_dates:
        if day not in factor.index or day not in prices.index:
            continue
        scores = factor.loc[day].dropna()
        scores = scores[prices.loc[day].reindex(scores.index).notna()]
        if len(scores) < n_groups:
            continue
        # 从高到低排名后切成 n_groups 组；group=1 拿最高的那批
        ranks = scores.rank(ascending=False, method="first")
        size = len(scores) / n_groups
        picked = scores.index[(ranks > (group - 1) * size) & (ranks <= group * size)]
        if len(picked) == 0:
            continue
        row = pd.Series(0.0, index=prices.columns)
        row[picked] = 1.0 / len(picked)   # 等权
        target.loc[day] = row
    return target


def equal_weight_benchmark(
    prices: pd.DataFrame, rebalance_dates: pd.Index
) -> pd.DataFrame:
    """基准：全池等权，同样的调仓节奏（可比性的前提是节奏一致）。"""
    target = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    for day in rebalance_dates:
        if day not in prices.index:
            continue
        available = prices.loc[day].dropna().index
        if len(available) == 0:
            continue
        row = pd.Series(0.0, index=prices.columns)
        row[available] = 1.0 / len(available)
        target.loc[day] = row
    return target


# ---------------- 引擎 ----------------


def run_backtest(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    cost: CostModel | None = None,
) -> BacktestResult:
    """向量化回测：给定价格面板与调仓日目标权重，算出净值、成本、换手。

    数学上怎么做到"不逐日循环"：
    设 C_i[t] 为股票 i 的累计净值（(1+r).cumprod()）。持仓期内份数不变，
    令 u_i = w_i / C_i[t0]（t0 为建仓日），则组合价值 ∝ Σ u_i·C_i[t]。
    u 只在调仓日变化 → 前向填充成矩阵 U 后，组合日收益就是两次加权求和之比。
    """
    cost = cost or CostModel()
    symbols = list(prices.columns)
    # 停牌日用前值填充：当日收益为 0，复牌当天体现全部涨跌
    px = prices.ffill()
    ret = px.pct_change(fill_method=None).fillna(0.0)
    C = (1.0 + ret).cumprod()

    # 现金当成"永不涨跌的第 N+1 只资产"，权重 = 1 − 股票权重之和。
    # 少了这一列，半仓（另一半是现金）会被当成满仓、收益被放大——
    # 这是 2026-07-26 与 vectorbt 对拍时抓出来的真实 bug。
    C[CASH] = 1.0
    target = target_weights.reindex(columns=symbols).copy()
    target[CASH] = 1.0 - target_weights.sum(axis=1, min_count=1)

    # u = 目标权重 / 累计净值；ffill 让持仓期内份数保持不变；
    # shift(1) 是防未来函数的那一步：T 日定的权重从 T+1 才生效
    u = (target / C).ffill().shift(1)
    held = u.notna().any(axis=1)

    weighted_now = (u * C).sum(axis=1)
    weighted_prev = (u * C.shift(1)).sum(axis=1)
    gross = (weighted_now / weighted_prev - 1.0).where(held, 0.0).fillna(0.0)

    # 当日收盘漂移后的实际权重（占全组合比例，含现金那部分被摊掉）；
    # 报告与对拍只关心股票权重，现金列在此丢弃
    actual_w = (u * C).div(weighted_now, axis=0).fillna(0.0)[symbols]

    # 成本只在调仓日发生：把目标权重和"漂移后的现有权重"相减得到换手
    turnover = pd.Series(0.0, index=prices.index)
    costs = pd.Series(0.0, index=prices.index)
    prev_w = pd.Series(0.0, index=symbols)
    for day in target_weights.dropna(how="all").index:
        drifted = actual_w.loc[day] if held.get(day, False) else prev_w
        trade = target_weights.loc[day].fillna(0.0) - drifted
        sold = (-trade).clip(lower=0).sum()
        turnover.loc[day] = trade.abs().sum()
        costs.loc[day] = turnover.loc[day] * cost.two_way_rate + sold * cost.stamp_tax
        prev_w = target_weights.loc[day].fillna(0.0)

    net = gross - costs
    nav = (1.0 + net).cumprod()
    result = BacktestResult(
        nav=nav, returns=net, gross_returns=gross, costs=costs,
        turnover=turnover, weights=actual_w,
    )
    result.stats = performance_stats(result)
    return result


def performance_stats(result: BacktestResult) -> dict:
    """绩效指标。无风险利率取 0，夏普口径在报告里注明。"""
    nav, ret = result.nav, result.returns
    n = len(nav)
    if n == 0 or nav.iloc[-1] <= 0:
        return {}
    years = n / TRADING_DAYS_PER_YEAR
    ann_ret = nav.iloc[-1] ** (1 / years) - 1
    ann_vol = ret.std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown = 1 - nav / nav.cummax()      # 手写清单那两行的真实用途
    return {
        "累计收益": nav.iloc[-1] - 1,
        "年化收益": ann_ret,
        "年化波动": ann_vol,
        "夏普": ann_ret / ann_vol if ann_vol > 0 else float("nan"),
        "最大回撤": drawdown.max(),
        "年化换手": result.turnover.sum() / years,
        "累计成本": result.costs.sum(),
        "交易日数": n,
    }
