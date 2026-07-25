"""龙虎榜采集脚本（自写爬虫，不走 akshare）：按交易日增量抓取并幂等入库。

用法（项目根目录，激活 venv）：
    python scripts/update_billboard.py --days 5      # 冒烟：只补最近 5 个交易日
    python scripts/update_billboard.py               # 增量补齐到 T-1
    python scripts/update_billboard.py --start 2022-01-01   # 指定回溯起点

依赖 trade_calendar 表（由 scripts/update_data.py 采集）来决定哪天该抓。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp.config import load_settings
from qdp.crawler import update_billboard
from qdp.db import connect, init_schema
from qdp.ingest import IngestError
from qdp.log import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="采集龙虎榜（自写爬虫）")
    parser.add_argument("--start", help="强制从该日重采（YYYY-MM-DD），用于回溯历史")
    parser.add_argument("--end", help="采到该日为止（YYYY-MM-DD，默认 T-1）；"
                                     "与 --start 同日即只补采一天")
    parser.add_argument("--days", type=int, help="只采最近 N 个自然日内的交易日，冒烟用")
    parser.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒（默认 1.0）")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    # --start / --days 都是"强制起点"：绕过增量进度，指哪打哪（重采靠幂等去重）
    forced_start = (
        datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    )
    if args.days:
        forced_start = date.today() - timedelta(days=args.days)
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        report = update_billboard(
            conn,
            default_start=settings.start_date,
            start=forced_start,
            end=end,
            sleep_s=args.sleep,
            max_fail_ratio=settings.max_fail_ratio,
        )
    except IngestError as exc:
        logger.error("龙虎榜更新中止：%s", exc)
        return 1
    finally:
        conn.close()

    logger.info("龙虎榜更新完成 ✅ 新增 %d 行；失败 %d/%d 天",
                report.new_rows, len(report.failed), report.total_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
