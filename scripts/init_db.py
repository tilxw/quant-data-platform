"""建库脚本：创建 DuckDB 数据库文件并按 sql/schema.sql 建表。

用法（在项目根目录，激活 venv 后）：
    python scripts/init_db.py

可重复执行（幂等）：已建的表不会被动，已有数据不会丢。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 脚本直接运行时，把项目根目录加进导入路径，才能 import qdp
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp.config import load_settings
from qdp.db import connect, init_schema
from qdp.log import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    settings = load_settings()
    logger.info("数据库文件: %s", settings.db_path)
    conn = connect(settings.db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()
    logger.info("建库完成 ✅")


if __name__ == "__main__":
    main()
