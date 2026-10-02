"""用巨潮资讯（证监会指定的信息披露平台）的分红公告，核对两个行情源的复权因子跳变。

原理：交易所除权参考价 = (前收盘价 − 每股派息) ÷ (1 + 送转比例)，
因子在除权日的理论跳变倍数 = 前收盘价 ÷ 除权参考价（未计配股）。
哪个源的跳变日期和幅度都对得上公告，哪个源就是对的；公告之外的跳变则无法解释。

用法（项目根目录，激活 venv）：
    python scripts/verify_exdates.py                          # 默认核对迁移对账发现的 6 只
    python scripts/verify_exdates.py --symbols 000063,600519

两个库只读；联网只请求巨潮，每只股票 1 个请求。结果写 reports/exdate_verify_YYYYmmdd_HHMMSS.md。
排查性质的工具，未纳入单元测试。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import pandas as pd

import qdp.datasource  # noqa: F401  导入即设置国内站点直连
from qdp.config import PROJECT_ROOT
from qdp.log import setup_logging
from qdp.retry import call_with_retry

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = "000001,000002,000063,000100,000157,000858"
MATCH_TOL = 2e-3   # 跳变倍数与理论值的相对误差容差：参考价按分取整，留出余量
JUMP_TOL = 5e-3    # 同质检：因子相邻日变化超 0.5% 记一次跳变


def official_events(symbol: str, start, end) -> pd.DataFrame:
    """巨潮分红记录 → 每个除权日的送转比例 S（每股）与每股派息 D（元）。"""
    import akshare as ak
    df = call_with_retry(lambda: ak.stock_dividend_cninfo(symbol=symbol), what=f"{symbol} 巨潮分红")
    df = df.dropna(subset=["除权日"]).copy()
    df["除权日"] = pd.to_datetime(df["除权日"])
    df = df[df["除权日"].between(start, end)]
    for col in ["送股比例", "转增比例", "派息比例"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    g = df.groupby("除权日")[["送股比例", "转增比例", "派息比例"]].sum()
    return pd.DataFrame({"S": (g["送股比例"] + g["转增比例"]) / 10, "D": g["派息比例"] / 10})


def load(conn, symbol: str) -> pd.DataFrame:
    df = conn.execute("""
        SELECT b.trade_date, CAST(b.close AS DOUBLE) AS close, f.hfq_factor
        FROM daily_bar b JOIN adj_factor f USING (symbol, trade_date)
        WHERE b.symbol = ? ORDER BY 1
    """, [symbol]).df()
    return df.set_index(pd.to_datetime(df["trade_date"]))


def jump_ratio(df: pd.DataFrame, day, prev):
    if day in df.index and prev in df.index:
        return df.loc[day, "hfq_factor"] / df.loc[prev, "hfq_factor"]
    return float("nan")


def jump_days(df: pd.DataFrame) -> set:
    return set(df.index[df["hfq_factor"].pct_change().abs() > JUMP_TOL])


def verify(symbol: str, sina_conn, em_conn) -> tuple[dict, pd.DataFrame]:
    s, e = load(sina_conn, symbol), load(em_conn, symbol)
    events = official_events(symbol, s.index[1], s.index[-1])
    rows = []
    for day, ev in events.iterrows():
        if day not in s.index:
            rows.append({"除权日": day.date(), "说明": "除权日不在行情里（停牌？）"})
            continue
        prev = s.index[s.index.get_loc(day) - 1]
        p = s.loc[prev, "close"]
        theory = (1 + ev.S) * p / (p - ev.D)
        js, je = jump_ratio(s, day, prev), jump_ratio(e, day, prev)
        rows.append({
            "除权日": day.date(), "每股送转": ev.S, "每股派息": ev.D, "前收盘": p,
            "理论倍数": theory, "新浪倍数": js, "东财倍数": je,
            "新浪吻合": abs(js / theory - 1) < MATCH_TOL,
            "东财吻合": bool(abs(je / theory - 1) < MATCH_TOL) if pd.notna(je) else None,
        })
    detail = pd.DataFrame(rows)
    official = set(events.index)
    summary = {
        "股票": symbol,
        "公告除权次数": len(events),
        "新浪吻合": int(detail["新浪吻合"].sum()) if not detail.empty else 0,
        "东财吻合": int(detail["东财吻合"].fillna(False).astype(bool).sum()) if not detail.empty else 0,
        "新浪无法解释的跳变": len(jump_days(s) - official),
        "东财无法解释的跳变": len(jump_days(e) - official),
    }
    return summary, detail


def render(summaries: list[dict], details: dict, run_ts: datetime) -> str:
    lines = [
        f"# 复权因子 vs 巨潮分红公告 · {run_ts:%Y-%m-%d %H:%M}", "",
        "新浪 = 影子库 `quant_sina.duckdb`；东财 = 原库 `quant.duckdb`（含 10/01—10/02 用东财补进的数据）。",
        f"理论跳变倍数 = (1+每股送转) × 前收盘 ÷ (前收盘 − 每股派息)；与理论值相对误差 < {MATCH_TOL:.1%} 记为吻合。",
        f"无法解释的跳变 = 因子相邻日变化超 {JUMP_TOL:.1%}、但当天没有任何分红送转公告。", "",
        "## 汇总", "",
        "| 股票 | 公告除权次数 | 新浪吻合 | 东财吻合 | 新浪无法解释的跳变 | 东财无法解释的跳变 |",
        "|---|---|---|---|---|---|",
    ]
    for r in summaries:
        lines.append(f"| {r['股票']} | {r['公告除权次数']} | {r['新浪吻合']} | {r['东财吻合']} "
                     f"| {r['新浪无法解释的跳变']} | {r['东财无法解释的跳变']} |")
    for sym, d in details.items():
        lines += ["", f"## {sym} 逐次核对", "",
                  "| 除权日 | 每股送转 | 每股派息 | 前收盘 | 理论倍数 | 新浪倍数 | 东财倍数 | 新浪 | 东财 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in d.to_dict("records"):
            if "说明" in r and isinstance(r.get("说明"), str):
                lines.append(f"| {r['除权日']} | {r['说明']} | | | | | | | |")
                continue
            mark = lambda ok: "—" if ok is None else ("✅" if ok else "❌")
            lines.append(f"| {r['除权日']} | {r['每股送转']:.2f} | {r['每股派息']:.3f} | {r['前收盘']:.2f} "
                         f"| {r['理论倍数']:.4f} | {r['新浪倍数']:.4f} | {r['东财倍数']:.4f} "
                         f"| {mark(r['新浪吻合'])} | {mark(r['东财吻合'])} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="复权因子跳变 vs 巨潮分红公告（只读）")
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="逗号分隔")
    parser.add_argument("--sina", default="data/quant_sina.duckdb")
    parser.add_argument("--em", default="data/quant.duckdb")
    args = parser.parse_args()

    setup_logging()
    sina_conn = duckdb.connect(str(PROJECT_ROOT / args.sina), read_only=True)
    em_conn = duckdb.connect(str(PROJECT_ROOT / args.em), read_only=True)
    summaries, details = [], {}
    for sym in args.symbols.split(","):
        summary, detail = verify(sym, sina_conn, em_conn)
        summaries.append(summary)
        details[sym] = detail
        logger.info("%s：公告 %d 次，新浪吻合 %d、东财吻合 %d；无法解释的跳变 新浪 %d / 东财 %d",
                    sym, summary["公告除权次数"], summary["新浪吻合"], summary["东财吻合"],
                    summary["新浪无法解释的跳变"], summary["东财无法解释的跳变"])
        time.sleep(1.5)

    run_ts = datetime.now()
    out = PROJECT_ROOT / "reports" / f"exdate_verify_{run_ts:%Y%m%d_%H%M%S}.md"
    out.write_text(render(summaries, details, run_ts), encoding="utf-8")
    logger.info("核对报告已生成 %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
