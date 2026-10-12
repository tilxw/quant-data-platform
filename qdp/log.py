"""日志初始化。

选型：用标准库 logging 而非 loguru——零依赖，且是 Python 生态的通用标准。
各模块用法：logger = logging.getLogger(__name__)
"""

from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局日志格式。程序入口（scripts/*.py）调用一次即可。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
