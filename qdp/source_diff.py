"""数据源迁移对账：同一股票池、不同行情源的两个库，逐只对比行情与复权口径。

背景（2026-10-02）：东财对连续请求断连，决定把行情源整体迁到新浪。迁移用影子库：
新浪全量灌进新库，拿旧库（东财）当基准全量对账，口径不一致的股票逐只排查后再切换。

判定口径（与质检的跨源对拍一致：比收益率，不比复权价）：
- 不复权收盘价是交易所官方数据，两源必须逐日一致；
- 复权因子的绝对水平（基准）可以不同，只要两源因子之比恒定，后复权收益率就完全相同；
- 真正要紧的是后复权日收益率：差异超过舍入噪声的那些天，才是口径分歧。
  噪声按价位定：后复权价只保留到分，半分钱的误差对低价股影响更大（"误报 55 只"的教训）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HALF_CENT = 0.005     # 价格保留到分，四舍五入的最大误差
RET_TOL = 1e-3        # 收益率差异的下限：低于 0.1% 一律视为一致
NOISE_MULT = 4        # 容差 = 舍入噪声的 4 倍（两源各自都有舍入误差，再留余量）
JUMP_TOL = 5e-3       # 因子相邻日变化超过 0.5% 记一次跳变（同质检的因子体检阈值）
FEW_DAYS = 10         # 收益率分歧天数不超过它，记为"个别日不同"（多半是某次除权处理不同）
SHOW_DATES = 5        # 报告里每只股票最多列出的分歧日期


def load_series(conn) -> pd.DataFrame:
    """从一个库读出每只股票每天的不复权收盘价与后复权因子。"""
    return conn.execute("""
        SELECT b.symbol, b.trade_date, CAST(b.close AS DOUBLE) AS close, f.hfq_factor
        FROM daily_bar b JOIN adj_factor f USING (symbol, trade_date)
        ORDER BY b.symbol, b.trade_date
    """).df()


def compare_symbol(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    """对比同一只股票在两个库里的序列（列：trade_date、close、hfq_factor）。"""
    old = old.sort_values("trade_date")
    new = new.sort_values("trade_date")
    lo = max(old["trade_date"].min(), new["trade_date"].min())
    hi = min(old["trade_date"].max(), new["trade_date"].max())
    old_days = set(old.loc[old["trade_date"].between(lo, hi), "trade_date"])
    new_days = set(new.loc[new["trade_date"].between(lo, hi), "trade_date"])

    m = old.merge(new, on="trade_date", suffixes=("_old", "_new")).reset_index(drop=True)
    if m.empty:
        return {"status": "无重叠", "overlap": 0}

    raw_bad = int(((m["close_old"] - m["close_new"]).abs() > HALF_CENT + 1e-9).sum())
    hfq_old = m["close_old"] * m["hfq_factor_old"]
    hfq_new = m["close_new"] * m["hfq_factor_new"]
    ret_diff = (hfq_new.pct_change() - hfq_old.pct_change()).abs()
    tol = np.maximum(RET_TOL, NOISE_MULT * HALF_CENT / np.minimum(hfq_old, hfq_new).shift())
    bad = ret_diff > tol                       # 首行 NaN 比较结果为 False
    ratio = m["hfq_factor_new"] / m["hfq_factor_old"]
    jumps_old = int((m["hfq_factor_old"].pct_change().abs() > JUMP_TOL).sum())
    jumps_new = int((m["hfq_factor_new"].pct_change().abs() > JUMP_TOL).sum())

    n_bad = int(bad.sum())
    if raw_bad:
        status = "🔴 收盘价不一致"
    elif n_bad == 0:
        status = "✅ 一致"
    elif n_bad <= FEW_DAYS:
        status = "🟡 个别日不同"
    else:
        status = "🔴 口径不一致"

    # 谁更自洽：复权因子只该在除权日跳变，跳变次数明显多出来的一方更可疑
    suspect = ""
    if jumps_old > 2 * jumps_new + 2:
        suspect = "旧源因子跳变过多"
    elif jumps_new > 2 * jumps_old + 2:
        suspect = "新源因子跳变过多"

    return {
        "status": status,
        "overlap": len(m),
        "raw_bad": raw_bad,
        "ret_bad": n_bad,
        "ret_max": float(ret_diff.max()) if n_bad else 0.0,
        "bad_dates": [str(pd.Timestamp(d).date()) for d in m.loc[bad, "trade_date"].head(SHOW_DATES)],
        "jumps_old": jumps_old,
        "jumps_new": jumps_new,
        "suspect": suspect,
        "ratio_min": float(ratio.min()),
        "ratio_max": float(ratio.max()),
        "only_old": len(old_days - new_days),
        "only_new": len(new_days - old_days),
    }


def compare_databases(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """两个库的全量序列逐只对账，每只股票一行。"""
    old_by = dict(tuple(old.groupby("symbol")))
    new_by = dict(tuple(new.groupby("symbol")))
    rows = []
    for sym in sorted(set(old_by) | set(new_by)):
        if sym not in new_by:
            rows.append({"symbol": sym, "status": "仅旧库有"})
        elif sym not in old_by:
            rows.append({"symbol": sym, "status": "仅新库有"})
        else:
            rows.append({"symbol": sym, **compare_symbol(old_by[sym], new_by[sym])})
    return pd.DataFrame(rows)
