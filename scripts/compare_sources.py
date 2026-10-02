"""数据源迁移对账：旧库（东财）vs 影子库（新浪），逐只对比，输出差异清单。

用法（项目根目录，激活 venv）：
    python scripts/compare_sources.py
    python scripts/compare_sources.py --old data/quant.duckdb --new data/quant_sina.duckdb

两个库都以只读方式打开，不改任何数据。结果写 reports/source_compare_YYYYmmdd_HHMMSS.md。
判定口径见 qdp/source_diff.py。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from qdp import source_diff as sd
from qdp.config import PROJECT_ROOT
from qdp.log import setup_logging

logger = logging.getLogger(__name__)

STATUS_ORDER = ["🔴 收盘价不一致", "🔴 口径不一致", "🟡 个别日不同", "无重叠", "仅旧库有", "仅新库有", "✅ 一致"]


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def render(df, old_path: Path, new_path: Path, run_ts: datetime) -> str:
    counts = df["status"].value_counts()
    lines = [
        f"# 数据源迁移对账 · {run_ts:%Y-%m-%d %H:%M}",
        "",
        f"- 旧库（基准）：`{old_path.name}`　新库：`{new_path.name}`",
        f"- 共 {len(df)} 只股票",
        "",
        "## 汇总",
        "",
        "| 结果 | 只数 |",
        "|---|---|",
    ]
    lines += [f"| {s} | {int(counts.get(s, 0))} |" for s in STATUS_ORDER if counts.get(s, 0)]
    lines += [
        "",
        "> 判定口径：不复权收盘价必须逐日一致；复权因子的基准可以不同，只要比值恒定，收益率就一致；"
        f"后复权日收益率差异超过 max({sd.RET_TOL:.1%}, {sd.NOISE_MULT}×半分钱舍入噪声) 的天记为分歧。",
        f"> 因子跳变 = 相邻日变化超过 {sd.JUMP_TOL:.1%}；复权因子只应在除权日跳变，跳变次数明显偏多的一方更可疑。",
        "",
        "## 需要排查的股票",
        "",
        "| 股票 | 结果 | 重叠天数 | 收盘价不一致 | 收益率分歧天数 | 最大分歧 | 分歧日期（前 5 个） | 因子跳变 旧/新 | 疑点 | 新/旧因子比值 | 覆盖差异 仅旧/仅新 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    todo = df[df["status"] != "✅ 一致"].copy()
    todo["_order"] = todo["status"].map({s: i for i, s in enumerate(STATUS_ORDER)})
    for r in todo.sort_values(["_order", "symbol"]).itertuples():
        if r.status in ("仅旧库有", "仅新库有", "无重叠"):
            lines.append(f"| {r.symbol} | {r.status} | | | | | | | | | |")
            continue
        lines.append(
            f"| {r.symbol} | {r.status} | {r.overlap} | {r.raw_bad} | {r.ret_bad} | {r.ret_max:.2%} "
            f"| {', '.join(r.bad_dates)} | {r.jumps_old}/{r.jumps_new} | {r.suspect} "
            f"| {r.ratio_min:.4f}~{r.ratio_max:.4f} | {r.only_old}/{r.only_new} |"
        )
    if todo.empty:
        lines.append("（无：全部一致）")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="数据源迁移对账（只读）")
    parser.add_argument("--old", default="data/quant.duckdb", help="基准库（默认东财原库）")
    parser.add_argument("--new", default="data/quant_sina.duckdb", help="影子库（默认新浪重灌库）")
    args = parser.parse_args()

    setup_logging()
    old_path, new_path = resolve(args.old), resolve(args.new)
    old = sd.load_series(duckdb.connect(str(old_path), read_only=True))
    new = sd.load_series(duckdb.connect(str(new_path), read_only=True))
    logger.info("旧库 %d 行 / %d 只，新库 %d 行 / %d 只",
                len(old), old["symbol"].nunique(), len(new), new["symbol"].nunique())

    df = sd.compare_databases(old, new)
    run_ts = datetime.now()
    out = PROJECT_ROOT / "reports" / f"source_compare_{run_ts:%Y%m%d_%H%M%S}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(df, old_path, new_path, run_ts), encoding="utf-8")
    for status, n in df["status"].value_counts().items():
        logger.info("  %s：%d 只", status, n)
    logger.info("对账报告已生成 %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
