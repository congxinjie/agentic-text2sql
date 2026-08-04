# text2sql_demo

智能问数 Demo —— 最短闭环：自然语言问题 → LLM 生成只读 SQL → 执行 → 展示 SQL 与结果。

## 用法

```bash
# 单条提问
python3 text2sql.py "华东地区有多少客户？"
python3 text2sql.py --db ../customer_marketing_db/marketing.db "2025年哪个月交易额最高？"

# 交互模式
python3 text2sql.py
```

## 配置

密钥放在同目录 `.env.local`（已被 .gitignore 排除，不入库）：

```
LLM_API_KEY=sk-xxxx
```

可选：`LLM_BASE_URL`（默认 https://api.deepseek.com）、`LLM_MODEL`（默认 deepseek-v4-flash）。

## 闭环流程

1. `build_schema()` 从 sqlite_master 提取全部表结构（DDL）+ 每表 2 行样例数据
2. 结构与问题一起发给 LLM（temperature=0），要求只输出 SQL
3. `validate_sql()` 校验：剥离注释、只取第一个分号前、仅放行 `SELECT/WITH/EXPLAIN`
4. `run_query()` 以 `mode=ro` + `PRAGMA query_only=ON` 双保险只读执行
5. 展示 SQL 与结果表格（最多 100 行）

## 安全设计

| 层级 | 措施 |
|------|------|
| 连接层 | `file:xxx?mode=ro` URI，文件系统级只读 |
| 语句层 | `PRAGMA query_only=ON`，SQLite 拒绝任何写操作 |
| 校验层 | 只允许单条 SELECT/WITH/EXPLAIN；剥离注释防注入；多语句直接拒绝 |

## 依赖

仅 Python 标准库（urllib 调 DeepSeek OpenAI 兼容接口），零第三方包。
