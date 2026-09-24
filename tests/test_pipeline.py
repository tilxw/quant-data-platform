"""qdp.pipeline 测试：用临时假脚本验证编排逻辑（不碰真实数据、不联网）。"""

from __future__ import annotations

import logging
from pathlib import Path

from qdp.pipeline import TIMEOUT_EXIT_CODE, Step, run_pipeline, run_step


def make_script(dir_: Path, name: str, body: str) -> Path:
    path = dir_ / name
    path.write_text(body, encoding="utf-8")
    return path


def test_all_steps_succeed(tmp_path):
    a = make_script(tmp_path, "a.py", "print('a done')")
    b = make_script(tmp_path, "b.py", "print('b done')")
    run_dir = tmp_path / "logs"
    assert run_pipeline([Step("A", a), Step("B", b)], run_dir) == 0
    # 一步一个日志文件，子进程的输出都落进去了
    assert "a done" in (run_dir / "1_a.log").read_text(encoding="utf-8")
    assert "b done" in (run_dir / "2_b.log").read_text(encoding="utf-8")


def test_failure_stops_downstream(tmp_path, caplog):
    """闸门：某一步非 0，之后的步骤一个都不跑。"""
    marker = tmp_path / "factors_ran"
    ok = make_script(tmp_path, "ok.py", "")
    qc = make_script(tmp_path, "qc.py", "import sys; print('质检完成：ERROR 3'); sys.exit(1)")
    factors = make_script(tmp_path, "factors.py", f"open(r'{marker}', 'w').close()")
    with caplog.at_level(logging.ERROR):
        code = run_pipeline([Step("采集", ok), Step("质检", qc), Step("因子", factors)],
                            tmp_path / "logs")
    assert code == 1
    assert not marker.exists()              # 因子那一步没有执行
    assert "质检完成：ERROR 3" in caplog.text  # 告警带上了失败步骤的日志尾巴
    assert "未执行：因子" in caplog.text        # 告警列出了被跳过的步骤


def test_crash_is_also_failure(tmp_path):
    """脚本崩溃（没人接住的异常）同样是非 0，同样中止：闸门坏了也要关着。"""
    boom = make_script(tmp_path, "boom.py", "raise RuntimeError('数据库被锁')")
    assert run_pipeline([Step("质检", boom)], tmp_path / "logs") == 1


def test_hanging_step_is_killed(tmp_path):
    hang = make_script(tmp_path, "hang.py", "import time; time.sleep(30)")
    code = run_step(Step("卡死", hang, timeout_s=0.5), tmp_path / "hang.log")
    assert code == TIMEOUT_EXIT_CODE


def test_step_reads_only_its_own_args(tmp_path, monkeypatch):
    """子进程读自己的参数，不会误读编排器的命令行（直接 import 调 main() 就会读错）。"""
    echo = make_script(tmp_path, "echo.py", "import sys; print(sys.argv[1:])")
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--dry-run"])
    log = tmp_path / "echo.log"
    assert run_step(Step("回测", echo, ("--skip-crosscheck",)), log) == 0
    assert "['--skip-crosscheck']" in log.read_text(encoding="utf-8")
