"""入库编排：增量更新 + 幂等写入 + 异常分档。

异常分档原则（核心错→停；辅助缺→降级；网络抖→重试）：
- 交易日历/成分股名单抓不到 → IngestError 直接停（后面全白跑）；
- 单只股票重试后仍失败 → 记录并跳过，收尾汇总；失败比例超限 → 停；
- 单次请求失败 → datasource.call_with_retry 已做指数退避重试。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

import pandas as pd

from qdp import datasource as ds

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 25


class IngestError(Exception):
    """核心数据环节失败，本次更新整体不可信，直接停。"""


@dataclass
class UpdateReport:
    """一次更新的结果汇总，日志和调用方都用它。"""

    total_symbols: int = 0
    new_bars: int = 0
    new_factors: int = 0
    failed: list[str] = field(default_factory=list)


def idempotent_insert(conn, table: str, df: pd.DataFrame) -> int:
    """幂等写入：INSERT OR IGNORE，主键已存在的行自动跳过。返回真正新增的行数。"""
    if df.empty:
        return 0
    cols = ", ".join(df.columns)
    before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    conn.register("_incoming", df)
    try:
        conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM _incoming")
    finally:
        conn.unregister("_incoming")
    after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return after - before


def upsert(conn, table: str, df: pd.DataFrame) -> None:
    """INSERT OR REPLACE：主键撞上时以新数据为准（如股票改名、戴帽 ST）。"""
    if df.empty:
        return
    cols = ", ".join(df.columns)
    conn.register("_incoming", df)
    try:
        conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _incoming")
    finally:
        conn.unregister("_incoming")


def next_start_date(conn, table: str, symbol: str, default: date) -> date:
    """增量起点：库里该股最后一天的次日；一条都没有则用配置的起始日。"""
    last = conn.execute(
        f"SELECT max(trade_date) FROM {table} WHERE symbol = ?", [symbol]
    ).fetchone()[0]
    return last + timedelta(days=1) if last is not None else default


def update_symbol(
    conn,
    symbol: str,
    *,
    default_start: date,
    end: date,
    fetch_bars=ds.fetch_daily_bars,
    fetch_hfq=ds.fetch_hfq_close,
    sleep_s: float = 0.0,
) -> tuple[int, int]:
    """增量更新一只股票的日线与复权因子。返回（新增行情行数, 新增因子行数）。"""
    # 行情/因子两张表各算增量起点，取更早者一次抓齐；
    # 重叠部分靠幂等写入自动跳过，不会重复入库
    start = min(
        next_start_date(conn, "daily_bar", symbol, default_start),
        next_start_date(conn, "adj_factor", symbol, default_start),
    )
    if start > end:
        return 0, 0  # 已是最新，不发请求
    bars = fetch_bars(symbol, start, end)
    if bars.empty:
        return 0, 0  # 区间内停牌/未上市，属正常情况
    time.sleep(sleep_s)  # 同一只股票的两次请求之间也让一让
    hfq = fetch_hfq(symbol, start, end)
    factors = ds.compute_hfq_factors(bars, hfq)
    return (
        idempotent_insert(conn, "daily_bar", bars),
        idempotent_insert(conn, "adj_factor", factors),
    )


def run_update(
    conn,
    *,
    default_start: date,
    sleep_s: float,
    max_fail_ratio: float,
    end: date | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    fetch_calendar=ds.fetch_trade_calendar,
    fetch_universe=ds.fetch_hs300_universe,
    fetch_bars=ds.fetch_daily_bars,
    fetch_hfq=ds.fetch_hfq_close,
) -> UpdateReport:
    """全量更新入口：交易日历 → 成分股名单 → 逐只增量拉行情+因子。"""
    # 日线只采到 T-1：盘中当天数据不完整，而幂等写入不会覆盖已入库的行，
    # 半根K线一旦进库就永远不会被修正，所以宁可晚一天
    end = end or (date.today() - timedelta(days=1))

    try:
        n_cal = idempotent_insert(conn, "trade_calendar", fetch_calendar())
        logger.info("交易日历新增 %d 天", n_cal)
    except Exception as exc:
        raise IngestError(f"交易日历更新失败：{exc}") from exc

    try:
        universe = fetch_universe()
        upsert(conn, "stock_basic", universe)
        logger.info("沪深300成分股名单 %d 只已更新", len(universe))
    except Exception as exc:
        raise IngestError(f"沪深300成分股获取失败：{exc}") from exc

    todo = list(symbols) if symbols else universe["symbol"].tolist()
    if limit:
        todo = todo[:limit]

    report = UpdateReport(total_symbols=len(todo))
    for i, sym in enumerate(todo, 1):
        try:
            n_bars, n_factors = update_symbol(
                conn, sym,
                default_start=default_start, end=end,
                fetch_bars=fetch_bars, fetch_hfq=fetch_hfq, sleep_s=sleep_s,
            )
            report.new_bars += n_bars
            report.new_factors += n_factors
        except Exception as exc:
            # 单只失败降级：跳过继续跑，收尾统一汇报，绝不静默
            logger.warning("%s 更新失败（重试后仍失败），跳过：%s", sym, exc)
            report.failed.append(sym)
            # 失败常伴随限频，多歇一会给数据源降温（测试里 sleep_s=0 时不生效）
            time.sleep(10 * sleep_s)
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            logger.info(
                "进度 %d/%d：累计新增行情 %d 行、因子 %d 行，失败 %d 只",
                i, len(todo), report.new_bars, report.new_factors, len(report.failed),
            )
        time.sleep(sleep_s)

    if report.failed:
        logger.error("失败清单（%d 只）：%s", len(report.failed), ", ".join(report.failed))
        if len(report.failed) > max_fail_ratio * len(todo):
            raise IngestError(
                f"失败 {len(report.failed)}/{len(todo)} 只，超过上限 {max_fail_ratio:.0%}，"
                "本次更新不可信，请检查网络或接口"
            )
    return report
