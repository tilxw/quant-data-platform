"""通用重试工具（akshare 数据源与自写爬虫共用）。"""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


def call_with_retry(
    fn: Callable,
    *,
    what: str,
    attempts: int = 3,
    base_delay_s: float = 2.0,  # 实测东财限频封禁以分钟计，退避宁慢勿快
):
    """网络抖动档：失败重试 attempts 次、指数退避；全失败则抛最后一次异常。"""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if i == attempts:
                raise
            delay = base_delay_s * 2 ** (i - 1)
            logger.warning(
                "%s 第 %d/%d 次失败（%s: %s），%.1f 秒后重试",
                what, i, attempts, type(exc).__name__, exc, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
