"""每日流水线入口：行情（新浪）→ 质检（闸门）→ 龙虎榜 → 因子 → 回测。

用法（项目根目录，激活 venv）：
    python scripts/run_pipeline.py

由 cron 定时触发。任一步失败即中止其后所有步骤，本脚本退出码非 0。
每次运行的日志在 logs/pipeline_YYYYmmdd_HHMMSS/ 下，一步一个文件。

步骤顺序的依据：
- 质检紧跟行情：闸门贴着它检查的数据，行情有问题立刻停（fail fast）。
  质检是控制依赖——因子不读 qc_result，但必须等质检放行
- 龙虎榜要读 trade_calendar，而交易日历由行情脚本写入，所以排在行情之后
- init_db 不进流水线：每个脚本启动时都会自己建表（幂等）
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp.config import PROJECT_ROOT
from qdp.log import setup_logging
from qdp.pipeline import Step, run_pipeline

SCRIPTS = PROJECT_ROOT / "scripts"

STEPS = [
    Step("行情采集", SCRIPTS / "update_data.py"),
    # 暂时跳过跨源对拍：主源已换成新浪，可唯一的独立源东财既会断连、7 月下旬起返回的数据也有误。
    # 拿不可靠的源当闸门参照，等于让一个和今天数据无关的检查有权拦下整条流水线（7/24 的两条
    # ERROR 就是这么来的）。有了可靠的独立源再恢复；需要时可手动 run_qc.py --cross-source 指定源
    Step("数据质检", SCRIPTS / "run_qc.py", ("--skip-cross",)),
    Step("龙虎榜采集", SCRIPTS / "update_billboard.py"),
    Step("因子计算", SCRIPTS / "compute_factors.py"),
    # 对拍验证的是回测引擎的实现，不是数据：引擎代码不变就不必天天对，改引擎时手动跑
    Step("回测报告", SCRIPTS / "run_backtest.py", ("--skip-crosscheck",)),
]


def main() -> int:
    setup_logging()
    run_dir = PROJECT_ROOT / "logs" / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}"
    return run_pipeline(STEPS, run_dir)


if __name__ == "__main__":
    sys.exit(main())
