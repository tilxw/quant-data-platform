"""因子计算：从行情/龙虎榜面板算出因子值，向量化实现。

三条贯穿全模块的约定：

1. **时点语义**：factor_value 里某行的 trade_date，指"用到该日收盘为止的数据
   算出的值"。它在当日收盘后才可得，所以回测层必须 shift(1) 才能用来决定次日
   仓位——因子定义保持纯粹，滞后多少天是回测的参数。
2. **停牌处理**：滚动窗口按"库里已有的交易行"滚，不按日历补齐。rolling(20)
   = 最近 20 个有交易的日子；长期停牌股的因子会跨越更长的日历时间（已知局限）。
3. **价格口径**：价格类因子一律用后复权价（真实价 × 复权因子），除权除息不会
   被误当成暴跌；成交额用不复权的真实成交额（它本来就是真金白银）。
"""

from __future__ import annotations

import logging

import pandas as pd

from qdp.ingest import upsert

logger = logging.getLogger(__name__)

# 因子定义表：名字 → 中文说明（README 和体检报告都从这里取，避免两处描述打架）
FACTOR_SPECS = {
    "mom20": "20日动量：后复权价过去 20 个交易日的累计涨幅",
    "vol20": "20日波动率：日收益率的 20 日滚动标准差",
    "rev5": "5日反转：过去 5 日涨幅取负（短期涨多了倾向回落）",
    "illiq20": "Amihud 非流动性：|日收益率| ÷ 成交额(亿元) 的 20 日均值，越大越难交易",
    "lhb_cnt60": "龙虎榜活跃度：过去 60 个交易日中的上榜天数",
}

MOM_WINDOW = 20
VOL_WINDOW = 20
REV_WINDOW = 5
ILLIQ_WINDOW = 20
LHB_WINDOW = 60

FACTOR_COLUMNS = ["symbol", "trade_date", "factor_name", "value"]


def load_panel(conn) -> pd.DataFrame:
    """取出计算因子所需的面板：后复权价、成交额、当日是否上龙虎榜。

    龙虎榜按 (symbol, trade_date) 去重后打 0/1 标记——同股同日可能多次上榜，
    但"当天上没上榜"只算一次。
    """
    return conn.execute("""
        SELECT b.symbol, b.trade_date,
               CAST(b.close AS DOUBLE) * f.hfq_factor AS hfq_close,
               CAST(b.amount AS DOUBLE)               AS amount,
               CASE WHEN l.symbol IS NULL THEN 0.0 ELSE 1.0 END AS on_billboard
        FROM daily_bar b
        JOIN adj_factor f USING (symbol, trade_date)
        LEFT JOIN (SELECT DISTINCT symbol, trade_date FROM dragon_tiger) l
               ON l.symbol = b.symbol AND l.trade_date = b.trade_date
        ORDER BY b.symbol, b.trade_date
    """).df()


def compute_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """面板 → 因子长表（纯函数，离线可单测）。

    全程按 symbol 分组做整列运算，没有逐行 Python 循环——
    每个因子都只在"该股自己的时间序列"内滚动，绝不会串到别的股票。
    """
    if panel.empty:
        return pd.DataFrame(columns=FACTOR_COLUMNS)

    df = panel.sort_values(["symbol", "trade_date"]).copy()
    g = df.groupby("symbol", sort=False)

    # 日收益率：今收/昨收 - 1。fill_method=None 表示遇到空值不前向填充，
    # 免得用"补出来的价格"算出假收益
    df["ret"] = g["hfq_close"].pct_change(fill_method=None)

    df["mom20"] = g["hfq_close"].pct_change(MOM_WINDOW, fill_method=None)
    df["vol20"] = g["ret"].transform(lambda s: s.rolling(VOL_WINDOW).std())
    df["rev5"] = -g["hfq_close"].pct_change(REV_WINDOW, fill_method=None)

    # Amihud：单位成交额（亿元）推动的价格变动幅度，越大说明越"不抗砸"
    illiq_daily = df["ret"].abs() / (df["amount"] / 1e8)
    illiq_daily = illiq_daily.replace([float("inf"), float("-inf")], pd.NA)  # 防御：成交额为 0 会除出 inf（实测库中没有这样的行，数据源停牌日不出 K 线）
    df["illiq_daily"] = pd.to_numeric(illiq_daily, errors="coerce")
    df["illiq20"] = df.groupby("symbol", sort=False)["illiq_daily"].transform(
        lambda s: s.rolling(ILLIQ_WINDOW).mean()
    )

    df["lhb_cnt60"] = g["on_billboard"].transform(lambda s: s.rolling(LHB_WINDOW).sum())

    # 宽转长：一个因子值一行。算不出的（窗口不足、停牌无收益）直接丢弃不入库
    long = df.melt(
        id_vars=["symbol", "trade_date"],
        value_vars=list(FACTOR_SPECS),
        var_name="factor_name",
        value_name="value",
    ).dropna(subset=["value"])
    return long[FACTOR_COLUMNS].reset_index(drop=True)


def update_factors(conn) -> dict[str, int]:
    """全量重算因子并写库，返回各因子的行数。

    这里用 upsert（INSERT OR REPLACE）而不是采集用的 INSERT OR IGNORE：
    因子是**派生数据**，公式改了就该以新值为准；而行情是**事实数据**，
    入库后不许改写。两种写入语义对应两种数据性质。
    """
    panel = load_panel(conn)
    if panel.empty:
        logger.warning("行情面板为空，无因子可算")
        return {}
    logger.info("面板 %d 行（%d 只股票），开始计算 %d 个因子",
                len(panel), panel["symbol"].nunique(), len(FACTOR_SPECS))
    factors = compute_factors(panel)
    upsert(conn, "factor_value", factors)
    counts = factors["factor_name"].value_counts().to_dict()
    for name in FACTOR_SPECS:
        logger.info("  %-10s %8d 行  — %s", name, counts.get(name, 0), FACTOR_SPECS[name])
    return counts
