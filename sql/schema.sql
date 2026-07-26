-- ============================================================
-- A股量化数据平台 · 建表 DDL
--
-- 存储方案（用户拍板）：行情表存交易所真实价（不复权），
-- 复权因子单独一张表，前/后复权价用时现算。
-- 全部使用 CREATE TABLE IF NOT EXISTS，脚本可重复执行（幂等）。
-- ============================================================

-- 股票基本信息：一只股票一行
CREATE TABLE IF NOT EXISTS stock_basic (
    symbol      VARCHAR NOT NULL,   -- 6位代码，字符串存储保住前导零（如 000001）
    name        VARCHAR NOT NULL,   -- 股票简称
    exchange    VARCHAR NOT NULL,   -- SH / SZ
    list_date   DATE,               -- 上市日期
    PRIMARY KEY (symbol)
);

-- 日线行情：存不复权的真实价格（能算市值、判断涨跌停）
-- 主键 (symbol, trade_date)：同一只股票同一天只允许一行，
-- 这是 M2 增量更新"幂等写入"防重复入库的地基。
CREATE TABLE IF NOT EXISTS daily_bar (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE    NOT NULL,
    open        DECIMAL(10, 2),     -- A股价格最小变动 0.01 元，DECIMAL 精确到分
    high        DECIMAL(10, 2),
    low         DECIMAL(10, 2),
    close       DECIMAL(10, 2),
    volume      BIGINT,             -- 成交量（股）
    amount      DECIMAL(18, 2),     -- 成交额（元）
    PRIMARY KEY (symbol, trade_date)
);

-- 复权因子：只在除权除息时变化，与行情分表存，更新逻辑互不干扰
CREATE TABLE IF NOT EXISTS adj_factor (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE    NOT NULL,
    hfq_factor  DOUBLE  NOT NULL,   -- 后复权因子：后复权价 = 真实价 × 因子
    PRIMARY KEY (symbol, trade_date)
);

-- 交易日历：M3 质检"该有的日期缺没缺"的对照基准
CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date    DATE    NOT NULL,
    is_open     BOOLEAN NOT NULL,   -- 是否交易日
    PRIMARY KEY (cal_date)
);

-- 龙虎榜（自写爬虫采集的另类数据）
-- 主键含 reason：同一只股票同一天可因多个原因分别上榜（实测 2026-07-24 有 10 例），
-- 只用 (symbol, trade_date) 会把后来的记录当重复丢掉。
CREATE TABLE IF NOT EXISTS dragon_tiger (
    symbol          VARCHAR NOT NULL,
    trade_date      DATE    NOT NULL,
    reason          VARCHAR NOT NULL,   -- 上榜原因，如"日涨幅偏离值达7%的证券"
    name            VARCHAR,
    close_price     DECIMAL(10, 2),
    change_rate     DOUBLE,             -- 当日涨跌幅（%）
    net_amt         DECIMAL(18, 2),     -- 龙虎榜净买额（元）
    buy_amt         DECIMAL(18, 2),
    sell_amt        DECIMAL(18, 2),
    deal_amt        DECIMAL(18, 2),     -- 龙虎榜成交总额
    accum_amount    DECIMAL(18, 2),     -- 该股当日市场总成交额
    turnover_rate   DOUBLE,             -- 换手率（%）
    free_market_cap DECIMAL(18, 2),     -- 流通市值
    interpret       VARCHAR,            -- 解读，如"2家机构买入，成功率35.38%"
    market          VARCHAR,
    PRIMARY KEY (symbol, trade_date, reason)
);

-- 因子值（长表：一个因子值一行）
-- 用户拍板长表而非宽表：加新因子只是多一种 factor_name，表结构零改动。
-- 时点约定：某行的 trade_date = 算这个值时用到的最后一根 K 线的日期，
-- 即"该日收盘后才能得到的值"。回测层必须 shift(1) 才能用，防未来函数。
CREATE TABLE IF NOT EXISTS factor_value (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE    NOT NULL,
    factor_name VARCHAR NOT NULL,
    value       DOUBLE  NOT NULL,   -- 窗口不足等算不出的日子直接不存行，不存 NULL
    PRIMARY KEY (symbol, trade_date, factor_name)
);

-- 质检结果：每次体检的逐项结论，报告可追溯、调度告警可直接查询
CREATE TABLE IF NOT EXISTS qc_result (
    run_ts      TIMESTAMP NOT NULL, -- 本次质检时间戳
    check_name  VARCHAR NOT NULL,   -- freshness/completeness/factor_sanity/return_sanity/cross_source
    symbol      VARCHAR NOT NULL,   -- 全局项固定为 'ALL'
    level       VARCHAR NOT NULL,   -- INFO / WARN / ERROR
    metric      DOUBLE,             -- 量化指标（缺口天数、异常天数等）
    detail      VARCHAR NOT NULL,   -- 人读描述
    PRIMARY KEY (run_ts, check_name, symbol)
);
