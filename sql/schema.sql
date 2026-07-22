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
