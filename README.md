# A股量化数据与回测平台

数据采集 → 存储 → 质检 → 因子计算 → 回测的一体化平台，参照中低频量化机构
通行架构设计。存储采用 DuckDB（列式、单文件、完整 SQL），回测为自研向量化
引擎并与 vectorbt 对拍验证。

> 开发中。完整 README（Background / Method / Data / Results / Limitations /
> Reproduce）随 Phase 2 交付。

## 目录结构

```
qdp/            核心包（配置、日志、数据库、采集、质检、爬虫、后续的因子/回测）
sql/            建表 DDL
scripts/        可执行脚本（建库、采集入口等）
config/         配置文件（TOML）
tests/          单元测试（pytest）
data/           数据库文件（不入库）
```

## 因子

| 因子 | 定义 | 维度 |
|---|---|---|
| `mom20` | 后复权价过去 20 个交易日累计涨幅 | 趋势 |
| `vol20` | 日收益率的 20 日滚动标准差 | 风险 |
| `rev5` | 过去 5 日涨幅取负 | 短期均值回归 |
| `illiq20` | Amihud 非流动性：\|日收益率\| ÷ 成交额(亿元) 的 20 日均值 | 流动性 |
| `lhb_cnt60` | 过去 60 个交易日的龙虎榜上榜天数（自写爬虫采集） | 资金关注度 |

两条口径约定：

- **时点语义**：`factor_value` 中 `trade_date` 为 T 的值，是"用到 T 日收盘为止的数据"
  算出的，当日收盘后才可得。回测层必须 `shift(1)` 才能用于决定 T+1 仓位——
  因子定义保持纯粹，滞后期是回测的参数（含单元测试防未来函数）。
- **停牌处理**：滚动窗口按库内已有交易行滚动，不按日历补齐。已知局限：长期停牌
  个股的因子会跨越更长的日历时间。

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

python scripts/update_billboard.py --days 7   # 龙虎榜（自写爬虫）：补最近一周
python scripts/update_billboard.py            # 增量补齐到 T-1

python scripts/compute_factors.py         # 计算全部因子写入 factor_value 表
```

## 回测

自研向量化引擎：多标的、周频调仓、含交易成本（佣金双边 + 印花税卖出单边 + 滑点）。
防未来函数：T 日因子只决定 T+1 起的持仓（引擎内 `shift(1)`，含"篡改未来价格断言
过去净值不变"的单元测试）。

正确性用**三方对拍**验证：自研引擎 vs `vectorbt` vs 一个逐日循环显式记账的参考实现。
零成本口径下自研与参考实现一致到 **2.9e-15**（机器精度）；vectorbt 在同口径偏差 8e-6，
经三方比对定位在其一侧。详见 `reports/backtest_*.md`。

```bash
python scripts/run_backtest.py            # 分层回测 + 主策略 + 三方对拍，输出报告
```
