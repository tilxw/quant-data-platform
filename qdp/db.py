"""DuckDB 连接与建表。

所有模块通过这里拿数据库连接，不各自 duckdb.connect——
连接入口唯一，之后要加只读模式、重试等策略只改这一处。
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from qdp.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """连接 DuckDB 数据库文件；文件或父目录不存在则自动创建。

    传入 ":memory:" 时返回内存库（测试用，不落盘）。
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def init_schema(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """执行 sql/schema.sql 建表，返回库中现有表名。

    DDL 全部为 CREATE TABLE IF NOT EXISTS，重复执行不报错、
    不动已有数据（幂等），因此可以放心随时重跑。
    """
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.execute(ddl)
    tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
    logger.info("建表完成，当前库中共 %d 张表: %s", len(tables), ", ".join(sorted(tables)))
    return sorted(tables)
