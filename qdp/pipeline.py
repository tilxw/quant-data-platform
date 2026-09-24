"""流水线编排：按顺序跑一组脚本，任一步非 0 即中止并告警。

设计取舍（第 10 站拍板）：
- 用子进程跑脚本，不 import 进来调 main()：退出码是现成的接口；脚本崩溃拖不垮
  编排器；各脚本的 argparse 读的是自己的命令行，不会误读编排器的参数。
- 只认 0 / 非 0：查出数据问题和脚本自身崩溃都要停，区分了也不改变动作；
  要分辨是哪一种，看告警里带的日志尾巴。
- 不在这一层重试：采集脚本内层已做请求级重试 + 熔断，外层再试只会放大请求量；
  离线步骤的失败是确定性的，重跑结果一样。
- 每一步都幂等，所以失败修好后直接整条重跑即可，不需要"从失败处续跑"。
- 每步有超时：任务卡死时不会拖住整条流水线。

与 Airflow 概念一一对应：Step ↔ Task（BashOperator）、步骤列表 ↔ DAG、
非 0 后下游不跑 ↔ upstream_failed、alert() ↔ on_failure_callback。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from qdp.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124   # 沿用 Linux timeout 命令的约定：超时的退出码是 124
TAIL_LINES = 20           # 告警里带上失败步骤日志的最后几行


@dataclass
class Step:
    """流水线里的一步 = 跑一个脚本。"""

    name: str                       # 日志与告警里显示的名字
    script: Path                    # 脚本的绝对路径
    args: tuple[str, ...] = ()      # 传给脚本的命令行参数
    timeout_s: float = 3600         # 超过这么多秒没跑完，视为卡死并强制终止


def run_step(step: Step, log_path: Path) -> int:
    """用子进程跑一个脚本，输出全部写进 log_path，返回退出码。"""
    # sys.executable = 正在运行编排器的这个 Python。cron 的环境很干净，
    # 直接写 "python" 可能调到没装依赖的系统 Python
    cmd = [sys.executable, str(step.script), *step.args]
    with open(log_path, "w", encoding="utf-8") as f:
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=f,
                                  stderr=subprocess.STDOUT, timeout=step.timeout_s)
        except subprocess.TimeoutExpired:
            logger.error("%s 超过 %.0f 秒没跑完，已强制终止", step.name, step.timeout_s)
            return TIMEOUT_EXIT_CODE
    return proc.returncode


def tail_lines(path: Path, n: int = TAIL_LINES) -> str:
    """读出日志文件的最后 n 行。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def alert(step: Step, code: int, log_path: Path) -> None:
    """告警出口。目前只写日志；以后接钉钉或邮件，只改这一个函数。"""
    logger.error("【告警】%s 失败（退出码 %d），完整日志：%s\n------ 最后 %d 行 ------\n%s",
                 step.name, code, log_path, TAIL_LINES, tail_lines(log_path))


def run_pipeline(steps: list[Step], run_dir: Path) -> int:
    """按顺序执行各步。全部成功返回 0；任一步非 0 则告警、跳过其后所有步骤并返回 1。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("流水线开始：共 %d 步，日志目录 %s", len(steps), run_dir)
    started = time.monotonic()

    for i, step in enumerate(steps, 1):
        log_path = run_dir / f"{i}_{step.script.stem}.log"
        logger.info("▶ [%d/%d] %s", i, len(steps), step.name)
        t0 = time.monotonic()
        code = run_step(step, log_path)
        if code != 0:
            alert(step, code, log_path)
            skipped = [s.name for s in steps[i:]]
            logger.error("流水线中止，未执行：%s", "、".join(skipped) if skipped else "（无）")
            return 1
        logger.info("✔ [%d/%d] %s 完成，用时 %.1f 秒",
                    i, len(steps), step.name, time.monotonic() - t0)

    logger.info("流水线全部完成 ✅ 总用时 %.1f 秒", time.monotonic() - started)
    return 0
