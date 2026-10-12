"""数据质检：新鲜度、完整性、因子/收益体检、跨源对拍，输出体检单并存库。

质检规则：
- 对拍口径：比"后复权日收益率"而非价格——两源后复权锚点不同，价格天然差一个
  乘法常数，而常数在收益率的除法里自动消掉；不复权收盘价（交易所官方数据）
  两源必须完全一致。
- 对拍范围：固定哨兵股必查 + 随机抽样轮换，多次运行渐进覆盖全池。
- 缺口：上市前排除；其余标"停牌或缺失"仅记录（两者在库里无法区分，需停复牌
  数据源，列为已知局限）；单只缺口占比超阈值才升级 WARN。
- 输出：Markdown 体检单（人看）+ qc_result 表（机器查、历史可追溯）。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

import pandas as pd

from qdp.ingest import idempotent_insert

logger = logging.getLogger(__name__)

INFO, WARN, ERROR = "INFO", "WARN", "ERROR"

# 阈值放代码常量，随时可调（调整历史见 git）
# 因子回撤阈值定 0.5%：收盘价四舍五入到分会给"因子=后复权价÷收盘价"带来
# ~0.1% 量级的日间抖动（低价股更明显，2026-07-23 实测 55 只误报），
# 而真实的因子错误通常是 1%+ 的跳变——0.5% 落在两者之间。
# 2026-10-02 起回撤直接判 ERROR（原为 WARN）：复权因子只会在除权日上升、绝不会下降，舍入噪声
# 至多约 0.25%，超过 0.5% 的回撤只能是数据错误。验证：新浪全量库 33.9 万行 0 次触发；
# 已知有错的东财存档库触发 6 只，6 只均被巨潮分红公告证实有错（漏报为 0，误报为 0）。
# 起因：东财补进的数据让 000063 因子一夜跌 22%，原规则只报 WARN，闸门没拦
FACTOR_DROP_TOL = 5e-3
RETURN_LIMIT = 0.21     # 日收益率上限：主板±10%/创业板科创板±20%，留余量；超出即 ERROR（因子对时不可能发生）
SKIP_FIRST_DAYS = 5     # 新股上市初期无涨跌幅限制，前 5 个交易日不做涨跌停粗检


@dataclass
class QcItem:
    """一条质检结论；symbol 为 'ALL' 表示全局项。"""

    check_name: str
    symbol: str
    level: str
    metric: float | None
    detail: str


# ---------------- 离线检查（只查库，零网络请求） ----------------


def check_freshness(conn, today: date | None = None) -> list[QcItem]:
    """新鲜度：库里最新日期应追平最近一个已收盘的交易日（T-1）。"""
    today = today or date.today()
    expected = conn.execute(
        "SELECT max(cal_date) FROM trade_calendar WHERE is_open AND cal_date <= ?",
        [today - timedelta(days=1)],
    ).fetchone()[0]
    actual = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    if expected is None or actual is None:
        return [QcItem("freshness", "ALL", ERROR, None, "日历或行情表为空，无法判断新鲜度")]
    behind = conn.execute(
        "SELECT count(*) FROM trade_calendar WHERE is_open AND cal_date > ? AND cal_date <= ?",
        [actual, expected],
    ).fetchone()[0]
    level = INFO if behind == 0 else (WARN if behind <= 3 else ERROR)
    return [QcItem("freshness", "ALL", level, float(behind),
                   f"库内最新 {actual}，应到 {expected}，落后 {behind} 个交易日")]


def check_completeness(conn, *, warn_ratio: float) -> list[QcItem]:
    """完整性：每只股票自其首条行情起，日历上的交易日都应有行情。

    上市前的日子用"首条行情日"排除；缺口 = 停牌或缺失（无法区分，见模块注释）。
    """
    last_day = conn.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    if last_day is None:
        return [QcItem("completeness", "ALL", ERROR, None, "行情表为空")]
    stats = conn.execute("""
        WITH per AS (
            SELECT symbol, min(trade_date) AS first_day, count(*) AS actual
            FROM daily_bar GROUP BY symbol
        )
        SELECT p.symbol, p.first_day, p.actual, count(c.cal_date) AS expected
        FROM per p JOIN trade_calendar c
          ON c.is_open AND c.cal_date BETWEEN p.first_day AND ?
        GROUP BY p.symbol, p.first_day, p.actual
    """, [last_day]).df()

    items: list[QcItem] = []
    for row in stats.itertuples():
        gaps = int(row.expected - row.actual)
        if gaps <= 0:
            continue
        ratio = gaps / row.expected
        level = WARN if ratio > warn_ratio else INFO
        items.append(QcItem("completeness", row.symbol, level, float(gaps),
                            f"{row.first_day} 起应有 {row.expected} 日，实有 {row.actual}，"
                            f"缺 {gaps}（{ratio:.1%}，停牌或缺失）"))
    items.sort(key=lambda i: -(i.metric or 0.0))
    items.insert(0, QcItem("completeness", "ALL", INFO, float(len(items)),
                           f"{len(stats)} 只中 {len(items)} 只存在缺口"))
    return items


def check_factor_and_returns(conn) -> list[QcItem]:
    """因子/收益体检（离线）：
    - 后复权因子只应随分红上行，出现明显回撤 → 疑似因子错误；
    - 后复权日收益率绝对值超过涨跌停粗检上限 → 需人工核查。
    """
    df = conn.execute("""
        SELECT b.symbol, b.trade_date,
               CAST(b.close AS DOUBLE) * f.hfq_factor AS hfq_close, f.hfq_factor
        FROM daily_bar b JOIN adj_factor f USING (symbol, trade_date)
        ORDER BY b.symbol, b.trade_date
    """).df()
    items: list[QcItem] = []
    for symbol, g in df.groupby("symbol", sort=False):
        # 相邻日回撤而非对历史最高水位：cummax 参照会让一次噪声尖峰
        # 把之后所有日子都标脏（2026-07-23 实测教训）
        drop = -g["hfq_factor"].pct_change()
        n_drop = int((drop > FACTOR_DROP_TOL).sum())
        if n_drop:
            items.append(QcItem("factor_sanity", symbol, ERROR, float(n_drop),
                                f"后复权因子出现 {n_drop} 天相邻日回撤超 {FACTOR_DROP_TOL:.1%}"
                                "——因子只会随分红上升，回撤只能是数据错误"))
        ret = g["hfq_close"].pct_change().iloc[SKIP_FIRST_DAYS:]
        n_ext = int((ret.abs() > RETURN_LIMIT).sum())
        if n_ext:
            items.append(QcItem("return_sanity", symbol, ERROR, float(n_ext),
                                f"{n_ext} 天后复权日收益率超 ±{RETURN_LIMIT:.0%}"
                                "——超出涨跌停上限，疑似因子漏记除权或数据错误"))
    n_anom = len(items)
    items.insert(0, QcItem("factor_sanity", "ALL", INFO, float(n_anom),
                           f"{df['symbol'].nunique()} 只完成因子/收益体检，异常 {n_anom} 项"))
    return items


# ---------------- 跨源对拍（联网，抽样） ----------------


def pick_sample(all_symbols: Sequence[str], sentinels: Sequence[str], n: int,
                rng: random.Random | None = None) -> list[str]:
    """哨兵股必查 + 随机抽 n 只（每次轮换，多跑几次渐进覆盖全池）。"""
    rng = rng or random.Random()
    pool = [s for s in all_symbols if s not in set(sentinels)]
    picked = rng.sample(pool, min(n, len(pool)))
    return sorted(set(sentinels) | set(picked))


def _norm_dates(df: pd.DataFrame) -> pd.DataFrame:
    """merge 键类型统一：库里读出是 datetime64、抓取清洗后是 date 对象，
    pandas 拒绝合并异类型键，统一转 datetime64。"""
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def compare_with_source(db: pd.DataFrame, src_bars: pd.DataFrame, src_hfq: pd.DataFrame,
                        *, return_tol: float) -> dict:
    """纯函数：库内序列 vs 外部源序列（可离线单测）。

    - 不复权收盘价：同为交易所官方数据，必须一致（容差半分钱防浮点）；
    - 后复权价：两源锚点不同不可直接比，比"日收益率"——锚点差异是乘法常数，
      在收益率里自动消掉。
    """
    db, src_bars, src_hfq = _norm_dates(db), _norm_dates(src_bars), _norm_dates(src_hfq)
    m = db.merge(src_bars[["trade_date", "close"]].rename(columns={"close": "src_close"}),
                 on="trade_date")
    raw_bad = int((m["close"] - m["src_close"]).abs().gt(0.005).sum())

    s = db.merge(src_hfq.rename(columns={"hfq_close": "src_hfq"}), on="trade_date")
    s = s.sort_values("trade_date")
    diff = (s["hfq_close"].pct_change() - s["src_hfq"].pct_change()).abs().dropna()
    return {
        "overlap": int(len(s)),
        "raw_bad": raw_bad,
        "ret_bad": int(diff.gt(return_tol).sum()),
        "ret_max": float(diff.max()) if len(diff) else 0.0,
    }


def _load_db_series(conn, symbol: str) -> pd.DataFrame:
    return conn.execute("""
        SELECT b.trade_date, CAST(b.close AS DOUBLE) AS close,
               CAST(b.close AS DOUBLE) * f.hfq_factor AS hfq_close
        FROM daily_bar b JOIN adj_factor f USING (symbol, trade_date)
        WHERE b.symbol = ? ORDER BY b.trade_date
    """, [symbol]).df()


def check_cross_source(conn, symbols: Sequence[str], *, fetch_bars, fetch_hfq,
                       sleep_s: float, return_tol: float) -> list[QcItem]:
    """跨源对拍。单只失败按"辅助缺→降级"处理：质检自身不应把流程搞挂。"""
    items: list[QcItem] = []
    for sym in symbols:
        try:
            db = _load_db_series(conn, sym)
            if db.empty:
                items.append(QcItem("cross_source", sym, WARN, None, "库内无数据，无法对拍"))
                continue
            start, end = db["trade_date"].iloc[0], db["trade_date"].iloc[-1]
            src_bars = fetch_bars(sym, start, end)
            time.sleep(sleep_s)
            src_hfq = fetch_hfq(sym, start, end)
            r = compare_with_source(db, src_bars, src_hfq, return_tol=return_tol)
            if r["overlap"] == 0:
                items.append(QcItem("cross_source", sym, WARN, None, "对拍源无重叠数据"))
            elif r["raw_bad"] > 0:
                items.append(QcItem("cross_source", sym, ERROR, float(r["raw_bad"]),
                                    f"不复权收盘价 {r['raw_bad']} 天不一致——交易所官方数据不该有分歧，必查"))
            elif r["ret_bad"] > 5:
                items.append(QcItem("cross_source", sym, ERROR, float(r["ret_bad"]),
                                    f"复权收益率 {r['ret_bad']} 天差异超容差（最大 {r['ret_max']:.2e}），疑似因子翻车"))
            elif r["ret_bad"] > 0:
                items.append(QcItem("cross_source", sym, WARN, float(r["ret_bad"]),
                                    f"复权收益率 {r['ret_bad']} 天差异超容差（最大 {r['ret_max']:.2e}）"))
            else:
                items.append(QcItem("cross_source", sym, INFO, 0.0,
                                    f"{r['overlap']} 天全对上（收益率最大差 {r['ret_max']:.1e}）"))
        except Exception as exc:
            logger.warning("%s 对拍失败，跳过：%s", sym, exc)
            items.append(QcItem("cross_source", sym, WARN, None, f"对拍失败（网络/接口）：{exc}"))
        time.sleep(sleep_s)
    return items


# ---------------- 结果落地：存库 + 体检单 ----------------


def persist_results(conn, run_ts: datetime, items: Sequence[QcItem]) -> None:
    df = pd.DataFrame([{
        "run_ts": run_ts, "check_name": i.check_name, "symbol": i.symbol,
        "level": i.level, "metric": i.metric, "detail": i.detail,
    } for i in items])
    idempotent_insert(conn, "qc_result", df)


def render_markdown(conn, items: Sequence[QcItem], run_ts: datetime, cross_desc: str) -> str:
    n = {lv: sum(1 for i in items if i.level == lv) for lv in (ERROR, WARN, INFO)}
    total, syms, lo, hi = conn.execute(
        "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date) FROM daily_bar"
    ).fetchone()
    lines = [
        f"# 数据质检报告 · {run_ts:%Y-%m-%d %H:%M}",
        "",
        f"- 库概览：{syms} 只 / {total:,} 行 / {lo} → {hi}",
        f"- 跨源对拍：{cross_desc}",
        f"- 结论：**ERROR {n[ERROR]} · WARN {n[WARN]} · INFO {n[INFO]}**",
        "",
    ]
    sections = [("freshness", "新鲜度"), ("completeness", "完整性（缺口 = 停牌或缺失）"),
                ("factor_sanity", "因子体检"), ("return_sanity", "收益率粗检"),
                ("cross_source", "跨源对拍")]
    for check, title in sections:
        sub = [i for i in items if i.check_name == check]
        if not sub:
            continue
        lines.append(f"## {title}")
        for i in sub:
            if i.symbol == "ALL":
                lines.append(f"- 汇总：[{i.level}] {i.detail}")
        bad = [i for i in sub if i.level != INFO and i.symbol != "ALL"]
        # ERROR 排最前、再按指标降序，然后才截断——2026-07-24 体检单曾把唯一的
        # ERROR 埋进 32 个 WARN 的折叠区，教训：呈现层截断也能掩盖重点
        bad.sort(key=lambda i: (i.level != ERROR, -(i.metric or 0.0)))
        for i in bad[:15]:
            lines.append(f"- [{i.level}] {i.symbol}：{i.detail}")
        if len(bad) > 15:
            lines.append(f"- …另有 {len(bad) - 15} 项异常，详见 qc_result 表")
        passed = [i for i in sub if i.level == INFO and i.symbol != "ALL"]
        if passed:
            lines.append(f"- 通过：{len(passed)} 只无异常")
        lines.append("")
    lines += [
        "> 已知局限：缺口无法区分停牌与真缺数（需停复牌数据源，列入后续改进）；",
        "> 对拍为抽样制，未覆盖部分由每次随机轮换逐步补齐。",
    ]
    return "\n".join(lines)
