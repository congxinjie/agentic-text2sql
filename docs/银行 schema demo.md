# 银行 schema demo(非券商实战样例)

> 目的:用另一个行业的数据库(银行营销库)证明**同一引擎换库、零券商口径注入,完整跑通**。
> 这是 M6.1 退出标准 2b 的真实运行样例(原要求:用 `marketing.db` 跑通一条问答证明解耦),本轮补全为 3 道题 + 截图式输出。

## 1. 数据库与启动

| 项 | 值 |
|---|---|
| 数据库 | `customer_marketing_db/marketing.db`(虚构银行营销库, 6 表, 4537 行) |
| 生成 | `python3 customer_marketing_db/create_db.py`(纯标准库, 固定种子, 瞬时生成) |
| 表结构 | `customers`(200)/`assets`(1287)/`transactions`(2000)/`campaigns`(10)/`contacts`(1020) |
| 业务口径注入 | **无** —— 走引擎默认 `BUSINESS_DESC`(银行语义),**与券商口径完全无关** |
| 启动命令 | `python3 text2sql_demo/text2sql.py --db ../customer_marketing_db/marketing.db --sample-rows 2 "问题"` |
| 样例行 | 显式 2 行(与基准集一致);真实生产可省略此参数(默认 0) |

## 2. 三道实战样例(全部来自真实运行,2026-10-10)

### 题 1:简单聚合(单表单指标单维度)

**问题**: 各地区分别有多少客户?

**输出**:
```
[意图] 查询
[SQL]
SELECT c.region AS 地区, COUNT(c.customer_id) AS 客户数
FROM customers c GROUP BY c.region ORDER BY 客户数 DESC
[查询结果]
 地区  客户数
 ----  ------
 华东  61
 华北  42
 华南  38
 西南  22
 东北  22
 西北  15
 共 6 行
[执行计划] 全表扫描 1 · 索引扫描 0 · 索引搜索 0
[结论]
按地区统计，客户数量最多的是华东，共 61 家;其次是华北 42 家、华南 38 家。
西南和东北并列各 22 家，西北最少，为 15 家。全部六个地区的客户数合计约 200 家。
```

引擎走完 9 阶段,意图/指标/维度/筛选/时间/输出列全部正确识别。

### 题 2:中等等值(需自动取快照日期)

**问题**: 各产品类型的资产总金额是多少?

**输出**(节选):
```
[意图] 查询; 指标=[资产总金额]; 维度=[产品类型]; 可选默认=[要统计的资产快照日期]
[阶段 03] 生成查询计划 [OK]
        在 assets 表中取最新一期快照(stat_date=2026-06-30),按 product_type 分组汇总 amount
[阶段 04] 检查查询计划 [警告]
        发现问题: 有维度分组但计划缺少 dimension_labels
[阶段 05] 生成查询计划 [警告] (自动修正一次)
        根据检查结果自动修正计划(一次)
[阶段 06] 检查查询计划 [OK]
[SQL]
SELECT product_type AS 产品类型, SUM(amount) AS 资产总金额
FROM assets WHERE stat_date = '2026-06-30' GROUP BY product_type ORDER BY 资产总金额 DESC
[查询结果]
 产品类型  资产总金额
 --------  ------------------
 银行理财  40834389.97
 定期存款  40783736.53
 股票基金  17547828.03
 货币基金  10909347.29
 活期存款  7647843.01
 保险      3787460.73
```

**关键点**:这一题展示了 M6 输出列契约 / 维度展示字段声明的**自修复**能力——
计划第一次缺少 `dimension_labels`,引擎在阶段 04 抛 issue,阶段 05 自动重规划一次通过,SQL 一次成功。LLM 没有再被叫来"打补丁"。

### 题 3:`--sample-rows 0`(新默认)同样跑通

**问题**: 各地区分别有多少客户?(同题 1)

**命令**: `python3 text2sql_demo/text2sql.py --db ../customer_marketing_db/marketing.db --sample-rows 0 "各地区分别有多少客户?"`

**输出**:
```
样例数据行数=0  ← 新默认(2026-10-10 合规整改)
[意图] 查询; 指标=[客户数量]; 维度=[地区]
[阶段 04] 检查查询计划 [OK]  ← 一次过
[SQL]
SELECT customers.region AS 地区, COUNT(customers.customer_id) AS 客户数
FROM customers GROUP BY customers.region ORDER BY 客户数 DESC
[查询结果]  华东 61 / 华北 42 / 华南 38 / 西南 22 / 东北 22 / 西北 15
```

**关键点**:`sample_rows=0` 时,schema 文本里只有表名/列名/DDL,**没有任何真实数据行离开本机**。
SQL 生成一次通过(LLM 不依赖样例即可识别),结果与题 1 完全一致。

## 3. 这证明什么

1. **引擎与 schema 解耦**:`marketing.db` 没有任何券商专有概念(没有 `hold_cnt` / `sor_pty_id` / `prdt_type_name`),但走完 9 阶段正常出 SQL;
2. **零口径注入即可用**:`--biz-context` 参数未传,引擎走默认 `BUSINESS_DESC`(银行营销),不报错;
3. **输出列契约自修复**对任何 schema 都生效(题 2 一次重规划通过);
4. **`sample_rows=0` 的真实数据场景**对任何 schema 都能出结果(题 3),LLM 不依赖样例行理解表结构;
5. **同一道题在不同 `sample_rows` 下结果一致**(题 1 vs 题 3),即"显式 2"和"默认 0"在输出层无差异——为 46 问金标可用 `sample_rows=2` 跑、真实场景可用 0 跑提供机器证据。

## 4. 复现命令(三行)

```bash
# 准备库(若 marketing.db 不存在)
python3 customer_marketing_db/create_db.py

# 跑题 1(样例 2 行,与既有 100% 口径一致)
python3 text2sql_demo/text2sql.py --db ../customer_marketing_db/marketing.db --sample-rows 2 "各地区分别有多少客户?"

# 跑题 3(样例 0 行,新默认,真实数据场景)
python3 text2sql_demo/text2sql.py --db ../customer_marketing_db/marketing.db --sample-rows 0 "各地区分别有多少客户?"

# 一行流式起演示层,浏览器打开 http://127.0.0.1:8000 即可交互
python3 demo/server.py --db customer_marketing_db/marketing.db --port 8000
```

## 5. 这与"接真实数仓"的距离

`marketing.db` 仍是虚构小库。要接到真实生产(银行/保险/零售),需要:

1. 把 `marketing.db` 换成真实库(SQLite 路径或注入 `db_adapter` 给 MySQL/PG);
2. 写一份 `biz_context.json` 描述业务口径(类似 `demo/enterprise_biz.json` 那样);
3. 如有受限字段(如身份证号/手机号),在 `sensitive_columns` 里声明(引擎会做样例打码 + SQL 拦截 + 结果列掩码);
4. **生产环境强烈建议 `--sample-rows 0`**(2026-10-10 后是默认值),不让任何真实数据行进 prompt;
5. 把 LLM 端点换成私有化模型(改 `LLM_BASE_URL` 一个环境变量即可)。

详细复用说明见 [`docs/元数据组织方案.md`](元数据组织方案.md) 与 [`docs/数据库适配方案.md`](数据库适配方案.md)。
