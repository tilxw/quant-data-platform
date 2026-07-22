"""日志初始化。

家具级决策：用标准库 logging 而非 loguru——零依赖，且 logging 是
生产环境的通用语言，面试提到日志默认指它。
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
