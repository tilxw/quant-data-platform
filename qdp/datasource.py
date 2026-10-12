"""akshare 数据源封装：抓取、清洗、重试。

所有函数返回与库表结构对齐的"标准化 DataFrame"，列名和单位在本文件统一收口——
akshare 上游改接口时只需要改这一个文件。

akshare 在函数内部延迟导入：它首次导入慢，且单元测试只测清洗逻辑（离线），
不应依赖它。
"""

from __future__ import annotations

import logging
import os
from datetime import date

import pandas as pd

from qdp.retry import call_with_retry  # 与自写爬虫共用，此处再导出保持调用点不变

logger = logging.getLogger(__name__)

# 数据源全是境内站点（东财/新浪/中证指数），经本地代理访问时常见
# 断连抖动；加进 NO_PROXY 让 requests 对这些域名直连，代理留给需要它的流量。
_DOMESTIC_DOMAINS = "eastmoney.com,sina.com.cn,csindex.com.cn"
for _var in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_var, "")
    if _DOMESTIC_DOMAINS not in _existing:
        os.environ[_var] = ",".join(filter(None, [_existing, _DOMESTIC_DOMAINS]))


class DataSourceError(Exception):
    """数据源返回结构不符合预期（接口改版、被限频返回错误页等）。"""


# ---------------- 清洗（纯函数，离线可单测） ----------------

BAR_COLUMNS = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]

_BAR_COLUMN_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",  # 东财单位：手（1手 = 100股）
    "成交额": "amount",  # 单位：元
}


def normalize_bars(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """东财日线原始表 → daily_bar 表结构（重命名列、补前导零、手→股）。"""
    missing = set(_BAR_COLUMN_MAP) - set(raw.columns)
    if missing:
        raise DataSourceError(f"{symbol} 日线缺少列 {sorted(missing)}，akshare 可能改了接口")
    df = raw[list(_BAR_COLUMN_MAP)].rename(columns=_BAR_COLUMN_MAP).copy()
    df["symbol"] = str(symbol).zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["volume"] = (df["volume"].astype("float64") * 100).round().astype("int64")
    return df[BAR_COLUMNS]


def compute_hfq_factors(bars: pd.DataFrame, hfq_close: pd.DataFrame) -> pd.DataFrame:
    """后复权因子 = 同源后复权收盘价 ÷ 不复权收盘价（按日对齐）。

    设计决策：因子从同一数据源推导（当前主源新浪，2026-10-02 前为东财）而非另接独立因子接口，
    保证价与因子自洽；另一个独立数据源留给跨源对拍质检使用，独立性正是对拍的价值所在。
    """
    merged = bars[["symbol", "trade_date", "close"]].merge(hfq_close, on="trade_date", how="inner")
    merged["hfq_factor"] = merged["hfq_close"] / merged["close"]
    return merged[["symbol", "trade_date", "hfq_factor"]]


# ---------------- 抓取（联网） ----------------


def fetch_daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
    """抓一只股票 [start, end] 的不复权日线；区间内无数据（停牌/未上市）返回空表。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="",
        ),
        what=f"{symbol} 不复权日线",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    return normalize_bars(raw, symbol)


def fetch_hfq_close(symbol: str, start: date, end: date) -> pd.DataFrame:
    """抓同一接口的后复权收盘价（仅用于推导复权因子）。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="hfq",
        ),
        what=f"{symbol} 后复权日线",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["trade_date", "hfq_close"])
    if "日期" not in raw.columns or "收盘" not in raw.columns:
        raise DataSourceError(f"{symbol} 后复权日线缺少 日期/收盘 列，akshare 可能改了接口")
    df = raw[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "hfq_close"}).copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


# ---------------- 新浪行情源（2026-10-02 起为主源；此前是东财被限频时的备用源） ----------------

_SINA_BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _sina_symbol(symbol: str) -> str:
    """新浪代码格式：sh600000 / sz000001。"""
    symbol = str(symbol).zfill(6)
    return ("sh" if symbol.startswith("6") else "sz") + symbol


def normalize_sina_bars(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """新浪日线原始表 → daily_bar 表结构。注意：新浪 volume 单位已是股，不乘 100。"""
    missing = set(_SINA_BAR_COLUMNS) - set(raw.columns)
    if missing:
        raise DataSourceError(f"{symbol} 新浪日线缺少列 {sorted(missing)}，akshare 可能改了接口")
    df = raw[_SINA_BAR_COLUMNS].rename(columns={"date": "trade_date"}).copy()
    df["symbol"] = str(symbol).zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["volume"] = df["volume"].astype("float64").round().astype("int64")
    return df[BAR_COLUMNS]


def fetch_daily_bars_sina(symbol: str, start: date, end: date) -> pd.DataFrame:
    """新浪源：不复权日线。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_daily(
            symbol=_sina_symbol(symbol),
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="",
        ),
        what=f"{symbol} 不复权日线(新浪)",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    return normalize_sina_bars(raw, symbol)


def fetch_hfq_close_sina(symbol: str, start: date, end: date) -> pd.DataFrame:
    """新浪源：后复权收盘价（推导复权因子用）。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_daily(
            symbol=_sina_symbol(symbol),
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="hfq",
        ),
        what=f"{symbol} 后复权日线(新浪)",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["trade_date", "hfq_close"])
    if "date" not in raw.columns or "close" not in raw.columns:
        raise DataSourceError(f"{symbol} 新浪后复权日线缺少 date/close 列")
    df = raw[["date", "close"]].rename(columns={"date": "trade_date", "close": "hfq_close"}).copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


# ---------------- 备用行情源：腾讯（第三独立源，对拍裁决用） ----------------

_TX_BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


def normalize_tx_bars(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """腾讯日线原始表 → daily_bar 表结构。注意：腾讯 volume 单位是手，×100 转股。"""
    missing = set(_TX_BAR_COLUMNS) - set(raw.columns)
    if missing:
        raise DataSourceError(f"{symbol} 腾讯日线缺少列 {sorted(missing)}，akshare 可能改了接口")
    df = raw[_TX_BAR_COLUMNS].rename(columns={"date": "trade_date"}).copy()
    df["symbol"] = str(symbol).zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["volume"] = (df["volume"].astype("float64") * 100).round().astype("int64")
    return df[BAR_COLUMNS]


def fetch_daily_bars_tx(symbol: str, start: date, end: date) -> pd.DataFrame:
    """腾讯源：不复权日线。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_hist_tx(
            symbol=_sina_symbol(symbol),  # 腾讯与新浪同用 sh/sz 前缀格式
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="",
        ),
        what=f"{symbol} 不复权日线(腾讯)",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    return normalize_tx_bars(raw, symbol)


def fetch_hfq_close_tx(symbol: str, start: date, end: date) -> pd.DataFrame:
    """腾讯源：后复权收盘价。"""
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.stock_zh_a_hist_tx(
            symbol=_sina_symbol(symbol),
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            adjust="hfq",
        ),
        what=f"{symbol} 后复权日线(腾讯)",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["trade_date", "hfq_close"])
    if "date" not in raw.columns or "close" not in raw.columns:
        raise DataSourceError(f"{symbol} 腾讯后复权日线缺少 date/close 列")
    df = raw[["date", "close"]].rename(columns={"date": "trade_date", "close": "hfq_close"}).copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


# 可选行情源注册表：值为 (日线抓取函数, 后复权收盘抓取函数)
# 腾讯源不注册：2026-07-23 对拍实测其后复权是"加法复权"流派（累计分红直接
# 加回价格，日收益率被系统性压缩 ~0.8 倍），与东财/新浪的"乘法复权"不可比，
# 因子推导会得到错误结果。fetch_*_tx 保留，未来可作"不复权价"第三裁判。
BAR_SOURCES = {
    "eastmoney": (fetch_daily_bars, fetch_hfq_close),
    "sina": (fetch_daily_bars_sina, fetch_hfq_close_sina),
}


def fetch_trade_calendar() -> pd.DataFrame:
    """全部 A 股交易日（新浪工具接口，1990 年至今年年底）。"""
    import akshare as ak

    raw = call_with_retry(lambda: ak.tool_trade_date_hist_sina(), what="交易日历")
    if raw is None or raw.empty or "trade_date" not in raw.columns:
        raise DataSourceError("交易日历返回异常，akshare 可能改了接口")
    return pd.DataFrame({
        "cal_date": pd.to_datetime(raw["trade_date"]).dt.date,
        "is_open": True,
    })


def fetch_index_universe(index_code: str = "000300") -> pd.DataFrame:
    """指数当前成分股名单（中证指数官网接口）。

    index_code：000300 沪深300、000905 中证500……由配置 universe.index_code 传入，
    不在这里写死——配置必须接线才算生效。
    """
    import akshare as ak

    raw = call_with_retry(
        lambda: ak.index_stock_cons_csindex(symbol=index_code),
        what=f"指数 {index_code} 成分股",
    )
    if raw is None or raw.empty or "成分券代码" not in raw.columns:
        raise DataSourceError(f"指数 {index_code} 成分股返回异常，akshare 可能改了接口")
    symbols = raw["成分券代码"].astype(str).str.zfill(6)
    return pd.DataFrame({
        "symbol": symbols,
        "name": raw["成分券名称"].astype(str),
        # 6 开头为沪市、0/3 开头为深市；沪深300 无北交所股票
        "exchange": symbols.str[0].map(lambda c: "SH" if c == "6" else "SZ"),
    })
