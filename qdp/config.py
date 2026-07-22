"""配置加载：把 config/settings.toml 读成带类型的 Settings 对象。

集中在这一处读配置，其他模块只依赖 Settings，不各自读文件。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# 项目根目录 = 本文件所在目录（qdp/）的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.toml"


@dataclass(frozen=True)
class Settings:
    """全局配置。frozen=True：配置只读，防止运行中被意外改掉。"""

    db_path: Path
    index_code: str


def load_settings(config_path: Path | None = None) -> Settings:
    """读取 TOML 配置文件并返回 Settings。

    Args:
        config_path: 配置文件路径，默认用 config/settings.toml。

    Raises:
        FileNotFoundError: 配置文件不存在（核心配置缺失属于致命错误，直接报错停下）。
    """
    path = config_path or DEFAULT_CONFIG_PATH
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    db_path = Path(raw["storage"]["db_path"])
    if not db_path.is_absolute():
        # 相对路径一律相对项目根目录解析，保证从任何工作目录运行结果一致
        db_path = PROJECT_ROOT / db_path

    return Settings(
        db_path=db_path,
        index_code=raw["universe"]["index_code"],
    )
