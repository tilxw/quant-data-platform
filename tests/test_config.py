"""qdp.config 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from qdp.config import load_settings


def test_load_default_settings():
    s = load_settings()
    assert s.index_code == "000300"
    assert s.db_path.is_absolute()  # 相对路径应已解析为绝对路径
    assert s.db_path.name == "quant.duckdb"


def test_missing_config_raises():
    """配置文件缺失属于核心错误：直接报错停下，不静默用默认值。"""
    with pytest.raises(FileNotFoundError):
        load_settings(Path("/不存在的路径/settings.toml"))
