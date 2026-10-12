"""龙虎榜爬虫：自写 requests 采集（刻意不走 akshare）。

接口由 F12 在东方财富数据中心页面亲手定位：页面用 JSONP（<script> 加载，
所以不出现在 Network 的 XHR 筛选里），去掉 callback 参数即返回纯 JSON。

工程要点：
- 请求头带 UA + Referer：该站校验来源（防盗链），不带 Referer 可能被拒；
- 超时用 (连接, 读取) 二元组：卡在连接和卡在读数据是两回事，分开设更精确；
- 重试走共用的 call_with_retry（网络抖 → 指数退避）；
- code=9201「返回数据为空」是业务空数据（非交易日/当天无人上榜），不是错误，
  必须与真失败分开处理，否则周末跑一次就误报故障。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd
import requests

from qdp.ingest import IngestError, idempotent_insert
from qdp.retry import call_with_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_DAILYBILLBOARD_DETAILSNEW"
REFERER = "https://data.eastmoney.com/stock/tradedetail.html"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Referer": REFERER, "Accept": "*/*"}

EMPTY_CODE = 9201          # 上游约定的「无数据」业务码
STOCK_TYPE_CODE = "058001001"  # A股；榜上还混有可转债(060)等，需过滤
PAGE_SIZE = 200            # 单日通常不足 100 条，一页装得下，仍保留翻页逻辑兜底
TIMEOUT = (5, 15)          # (连接超时, 读取超时) 秒

# 只取需要的列：一是省流量，二是上游给 columns=ALL 时会塞进一堆内部编码字段
COLUMNS = [
    "SECURITY_CODE", "SECURITY_NAME_ABBR", "TRADE_DATE", "EXPLANATION", "EXPLAIN",
    "CLOSE_PRICE", "CHANGE_RATE", "BILLBOARD_NET_AMT", "BILLBOARD_BUY_AMT",
    "BILLBOARD_SELL_AMT", "BILLBOARD_DEAL_AMT", "ACCUM_AMOUNT", "TURNOVERRATE",
    "FREE_MARKET_CAP", "MARKET", "SECURITY_TYPE_CODE",
]

BILLBOARD_COLUMNS = [
    "symbol", "trade_date", "reason", "name", "close_price", "change_rate",
    "net_amt", "buy_amt", "sell_amt", "deal_amt", "accum_amount",
    "turnover_rate", "free_market_cap", "interpret", "market",
]

_FIELD_MAP = {
    "SECURITY_CODE": "symbol",
    "TRADE_DATE": "trade_date",
    "EXPLANATION": "reason",        # 上榜原因（如"日涨幅偏离值达7%的证券"）
    "SECURITY_NAME_ABBR": "name",
    "CLOSE_PRICE": "close_price",
    "CHANGE_RATE": "change_rate",
    "BILLBOARD_NET_AMT": "net_amt",
    "BILLBOARD_BUY_AMT": "buy_amt",
    "BILLBOARD_SELL_AMT": "sell_amt",
    "BILLBOARD_DEAL_AMT": "deal_amt",
    "ACCUM_AMOUNT": "accum_amount",
    "TURNOVERRATE": "turnover_rate",
    "FREE_MARKET_CAP": "free_market_cap",
    "EXPLAIN": "interpret",         # 解读（如"2家机构买入，成功率35.38%"）
    "MARKET": "market",
}


class CrawlError(Exception):
    """上游返回结构异常或请求彻底失败（接口改版、被封、网络不通）。"""


# 复用连接：同一台主机连发几百个请求，省掉每次的 TCP/TLS 握手
_SESSION = requests.Session()


def request_json(params: dict, *, session: requests.Session | None = None) -> dict:
    """带 headers / 超时 / 重试的请求骨架，返回解析后的 JSON。"""
    sess = session or _SESSION

    def _once() -> dict:
        resp = sess.get(BASE_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()   # 非 2xx 立刻抛错，别把错误页当数据往下传
        return resp.json()

    return call_with_retry(_once, what=f"龙虎榜 {params.get('filter', '')}")


def _day_filter(trade_date: date) -> str:
    """上游的日期过滤语法：闭区间两个条件并排写。"""
    d = trade_date.strftime("%Y-%m-%d")
    return f"(TRADE_DATE<='{d}')(TRADE_DATE>='{d}')"


def fetch_billboard_raw(trade_date: date, *, request: Callable = request_json) -> list[dict]:
    """抓某一交易日的龙虎榜原始记录（自动翻页）。

    非交易日或当天无人上榜时返回空列表——这是正常情形，不抛异常。
    """
    rows: list[dict] = []
    page = 1
    while True:
        payload = request({
            "reportName": REPORT_NAME,
            "columns": ",".join(COLUMNS),
            "filter": _day_filter(trade_date),
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "sortColumns": "SECURITY_CODE",
            "sortTypes": 1,
            "source": "WEB",
            "client": "WEB",
        })
        if payload.get("code") == EMPTY_CODE:
            break                       # 业务空数据，正常
        if not payload.get("success"):  # 其他 success=false 才是真异常
            raise CrawlError(f"{trade_date} 上游返回失败：{payload.get('message')!r}")
        result = payload.get("result") or {}
        rows.extend(result.get("data") or [])
        if page >= (result.get("pages") or 1):
            break
        page += 1
    return rows


def normalize_billboard(rows: list[dict]) -> pd.DataFrame:
    """原始 JSON 记录 → dragon_tiger 表结构（纯函数，离线可单测）。"""
    if not rows:
        return pd.DataFrame(columns=BILLBOARD_COLUMNS)
    df = pd.DataFrame(rows)
    missing = set(_FIELD_MAP) - set(df.columns)
    if missing:
        raise CrawlError(f"龙虎榜返回缺少字段 {sorted(missing)}，接口可能改版")
    # 榜单含可转债等非股票品种，本项目股票池只要 A 股
    df = df[df["SECURITY_TYPE_CODE"].astype(str) == STOCK_TYPE_CODE]
    df = df[list(_FIELD_MAP)].rename(columns=_FIELD_MAP)
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["reason"] = df["reason"].fillna("未标注")   # 主键列不允许为空
    # 同股同日可因多个原因上榜，(symbol, trade_date, reason) 才是唯一键；
    # 上游偶发重复行在此去重，避免撞主键
    return df.drop_duplicates(subset=["symbol", "trade_date", "reason"])[BILLBOARD_COLUMNS]


def fetch_billboard(trade_date: date, *, request: Callable = request_json) -> pd.DataFrame:
    """抓取 + 清洗某一交易日的龙虎榜。"""
    return normalize_billboard(fetch_billboard_raw(trade_date, request=request))


# ---------------- 入库编排：按交易日增量 + 幂等写入 ----------------


@dataclass
class BillboardReport:
    total_days: int = 0
    new_rows: int = 0
    failed: list[str] = field(default_factory=list)


def next_billboard_date(conn, default: date) -> date:
    """增量起点：库内最后一个已采日的次日；空表则用默认起点。"""
    last = conn.execute("SELECT max(trade_date) FROM dragon_tiger").fetchone()[0]
    return last + timedelta(days=1) if last is not None else default


def trading_days(conn, start: date, end: date) -> list[date]:
    """待采日期只取日历里的交易日——周末假日根本不发请求。"""
    return [r[0] for r in conn.execute(
        "SELECT cal_date FROM trade_calendar WHERE is_open AND cal_date BETWEEN ? AND ? "
        "ORDER BY cal_date", [start, end],
    ).fetchall()]


def update_billboard(
    conn,
    *,
    default_start: date,
    start: date | None = None,
    end: date | None = None,
    sleep_s: float = 1.0,
    max_fail_ratio: float = 0.1,
    fetch=fetch_billboard,
) -> BillboardReport:
    """按交易日增量抓取龙虎榜入库。异常分档同行情增量入库：单日失败降级，超限熔断。

    start 显式指定时强制从该日重采（回溯用）：增量起点只会向前推进，
    库里已有数据后就永远不会自己回头补更早的历史，回溯必须显式声明。
    重采已有日期靠幂等写入自动跳过，不会产生重复行。
    """
    end = end or (date.today() - timedelta(days=1))  # 龙虎榜盘后才发布，只采到 T-1
    start = start or next_billboard_date(conn, default_start)
    days = trading_days(conn, start, end)
    report = BillboardReport(total_days=len(days))
    if not days:
        logger.info("龙虎榜已是最新（起点 %s > 截止 %s），无需请求", start, end)
        return report

    logger.info("龙虎榜待采 %d 个交易日：%s → %s", len(days), days[0], days[-1])
    for i, day in enumerate(days, 1):
        try:
            df = fetch(day)
            # 交易日理论上必有上榜股；返回空只记日志不报错，下次运行会重采该日
            report.new_rows += idempotent_insert(conn, "dragon_tiger", df)
        except Exception as exc:
            logger.warning("%s 龙虎榜抓取失败，跳过：%s", day, exc)
            report.failed.append(str(day))
            # 熔断要趁早：失败天数一旦超过上限，结局已定，立刻停（同 ingest.run_update）
            if len(report.failed) > max_fail_ratio * len(days):
                logger.error("失败日期（%d 天）：%s", len(report.failed), ", ".join(report.failed))
                raise IngestError(
                    f"已失败 {len(report.failed)} 天（共 {len(days)} 天），超过上限 "
                    f"{max_fail_ratio:.0%}，提前熔断，剩余 {len(days) - i} 天未尝试"
                ) from exc
        if i % 20 == 0 or i == len(days):
            logger.info("进度 %d/%d：累计新增 %d 行，失败 %d 天",
                        i, len(days), report.new_rows, len(report.failed))
        time.sleep(sleep_s)

    if report.failed:  # 走到这里说明失败天数没超上限：降级完成，但失败清单必须留痕
        logger.error("失败日期（%d 天）：%s", len(report.failed), ", ".join(report.failed))
    return report
