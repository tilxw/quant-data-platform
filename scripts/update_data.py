"""数据更新脚本：交易日历 + 沪深300成分股 + 日线/复权因子，增量幂等入库。

用法（项目根目录，激活 venv）：
    python scripts/update_data.py                     # 全量增量更新（可随时重跑）
    python scripts/update_data.py --limit 5           # 冒烟测试：只跑前 5 只
    python scripts/update_data.py --symbols 000001,600519
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp.config import load_settings
from qdp.db import connect, init_schema
from qdp.ingest import IngestError, run_update
from qdp.log import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="增量更新行情数据")
    parser.add_argument("--symbols", help="只更新这些代码（逗号分隔），调试用")
    parser.add_argument("--limit", type=int, help="只更新成分股前 N 只，冒烟测试用")
    parser.add_argument(
        "--sleep", type=float,
        help="覆盖配置的请求间隔秒数（首次全量灌库建议 1.0，宁慢勿封）",
    )
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)  # 幂等，确保表存在
        report = run_update(
            conn,
            default_start=settings.start_date,
            sleep_s=args.sleep if args.sleep is not None else settings.request_sleep_s,
            max_fail_ratio=settings.max_fail_ratio,
            symbols=args.symbols.split(",") if args.symbols else None,
            limit=args.limit,
        )
    except IngestError as exc:
        logger.error("更新中止：%s", exc)
        return 1
    finally:
        conn.close()

    logger.info(
        "更新完成 ✅ 新增行情 %d 行、因子 %d 行；失败 %d/%d",
        report.new_bars, report.new_factors, len(report.failed), report.total_symbols,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
