# A股量化数据与回测平台

数据采集 → 存储 → 质检 → 因子计算 → 回测的一体化平台，参照中低频量化机构
通行架构设计。存储采用 DuckDB（列式、单文件、完整 SQL），回测为自研向量化
引擎并与 vectorbt 对拍验证。

> 开发中。完整 README（Background / Method / Data / Results / Limitations /
> Reproduce）随 Phase 2 交付。

## 目录结构

```
qdp/            核心包（配置、日志、数据库、后续的采集/质检/因子/回测）
sql/            建表 DDL
scripts/        可执行脚本（建库、采集入口等）
config/         配置文件（TOML）
tests/          单元测试（pytest）
data/           数据库文件（不入库）
```

## 快速开始

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py   # 建库建表（幂等，可重复执行）
pytest                      # 跑单元测试

python scripts/update_data.py --limit 5   # 冒烟：只采成分股前 5 只
python scripts/update_data.py             # 全量增量更新（幂等，可随时重跑）

python scripts/run_qc.py                  # 数据质检：体检单存 reports/，结果存 qc_result 表
python scripts/run_qc.py --skip-cross     # 只做离线检查（零网络请求）
```
