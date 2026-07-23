"""数据质检脚本：离线体检 + 跨源对拍，生成 Markdown 体检单并写入 qc_result 表。

用法（项目根目录，激活 venv）：
    python scripts/run_qc.py                      # 完整体检（对拍默认东财，抽样+哨兵）
    python scripts/run_qc.py --skip-cross         # 只做离线检查，零网络请求
    python scripts/run_qc.py --symbols 000001     # 只对拍指定股票
    python scripts/run_qc.py --cross-source sina  # 切换对拍源

退出码：存在 ERROR → 1（调度/CI 据此告警），否则 0。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdp import quality as qc
from qdp.config import PROJECT_ROOT, load_settings
from qdp.datasource import BAR_SOURCES
from qdp.db import connect, init_schema
from qdp.log import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="数据质检")
    parser.add_argument("--skip-cross", action="store_true", help="跳过跨源对拍（零网络请求）")
    parser.add_argument("--sample", type=int, help="对拍随机抽样只数（默认取配置）")
    parser.add_argument("--symbols", help="只对拍这些代码（逗号分隔），调试用")
    parser.add_argument("--cross-source", choices=sorted(BAR_SOURCES), default="eastmoney",
                        help="对拍用的外部源（应不同于库内数据的来源才有对拍价值）")
    parser.add_argument("--sleep", type=float, default=1.0, help="对拍请求间隔秒")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        init_schema(conn)  # 幂等，保证 qc_result 等表存在
        items = qc.check_freshness(conn)
        items += qc.check_completeness(conn, warn_ratio=settings.gap_warn_ratio)
        items += qc.check_factor_and_returns(conn)

        if args.skip_cross:
            cross_desc = "本次跳过（--skip-cross）"
        else:
            if args.symbols:
                sample = args.symbols.split(",")
            else:
                all_syms = [r[0] for r in conn.execute(
                    "SELECT DISTINCT symbol FROM daily_bar").fetchall()]
                sample = qc.pick_sample(all_syms, settings.sentinels,
                                        args.sample or settings.qc_sample_size)
            fetch_bars, fetch_hfq = BAR_SOURCES[args.cross_source]
            logger.info("跨源对拍开始：%s 源，共 %d 只", args.cross_source, len(sample))
            items += qc.check_cross_source(conn, sample,
                                           fetch_bars=fetch_bars, fetch_hfq=fetch_hfq,
                                           sleep_s=args.sleep,
                                           return_tol=settings.return_diff_tol)
            cross_desc = f"{args.cross_source} 源 × {len(sample)} 只（哨兵 + 随机轮换）"

        run_ts = datetime.now()
        qc.persist_results(conn, run_ts, items)
        report = qc.render_markdown(conn, items, run_ts, cross_desc)
        out = PROJECT_ROOT / "reports" / f"qc_{run_ts:%Y%m%d_%H%M%S}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(report, encoding="utf-8")

        n_err = sum(1 for i in items if i.level == qc.ERROR)
        n_warn = sum(1 for i in items if i.level == qc.WARN)
        logger.info("质检完成：ERROR %d / WARN %d，体检单已生成 %s", n_err, n_warn, out)
        return 1 if n_err else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
