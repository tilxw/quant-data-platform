"""因子计算脚本：从行情/龙虎榜算出全部因子并写入 factor_value 表。

用法（项目根目录，激活 venv）：
    python scripts/compute_factors.py

全量重算（几秒钟），不做增量：因子是纯函数派生数据，重算结果确定，
用 INSERT OR REPLACE 覆盖即可——比维护一套增量逻辑更简单也更不容易错。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp.config import load_settings
from qdp.db import connect, init_schema
from qdp.factors import update_factors
from qdp.log import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
        counts = update_factors(conn)
    finally:
        conn.close()
    if not counts:
        logger.error("没有算出任何因子，请先跑 scripts/update_data.py 采集行情")
        return 1
    logger.info("因子计算完成 ✅ 共 %d 行", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
