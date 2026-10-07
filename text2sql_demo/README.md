# text2sql_demo — 智能问数 Agent

自然语言 → 只读 SQL → 数据表 + 可解释结论。9 阶段状态机 QueryAgent(理解 → 检索 → 计划 → 检查 → 生成 SQL → 安全校验 → 执行 → 结果检查 → 解释),全程 Trace 留痕,支持主动追问、失败自修复、只读安全围栏。

## 环境与部署

- Python 3.10+(Windows 侧 `python`,≈3.12 验证过)
- 引擎零第三方依赖(纯标准库 urllib 调 DeepSeek)
- Web UI 依赖:`pip install -r requirements.txt`(仅 streamlit)

## 双入口启动

```bash
# 入口 1:Streamlit Web UI(COMPETITION §10.1 方案 A)
cd text2sql_demo
pip install -r requirements.txt     # 首次
streamlit run app.py

# 入口 2:CLI(单问/交互)
python text2sql.py "华东地区有多少客户?"          # 默认演示营销库(虚构)
python text2sql.py --db "..\\Agentic智能问数在客户营销场景的应用数据集\\enterprise.db" "各客户等级分别有多少客户?"
python text2sql.py                                # 交互模式(支持追问)
```

Web UI 默认连券商企业库 enterprise.db,业务口径从 `benchmark/run_eval.py` 同源注入(ENTERPRISE_BIZ + SQL_HINTS)。

## 配置

**无密钥起界面(离线冒烟)**: `python demo/server.py --db customer_marketing_db/marketing.db --offline`
—— 可看界面与 `/api/health`,提问会立即返回"未配置密钥"的明确错误,不发起任何网络调用;真实问答仍需下面配置的密钥。

密钥放在同目录 `.env.local`(已被 .gitignore 排除,不入库):

```
LLM_API_KEY=sk-xxxx
```

可选环境变量:`LLM_BASE_URL`(默认 https://api.deepseek.com)、`LLM_MODEL`(默认 deepseek-v4-flash;评测使用 deepseek-chat,见 benchmark/run_eval.py)。

## QueryAgent 官方接入口

```python
from text2sql import QueryAgent, load_api_key
agent = QueryAgent(
    db_path="<任意 sqlite 库>",
    api_key=load_api_key(),
    verbose=True,
    biz_context="<业务口径说明文本>",   # 不传则用内置演示营销库语义
    sql_hints={"持有": "口径示例 SQL 片段...", ...},  # 关键词触发 few-shot, 可选
    sensitive_columns=["<库内受限列名>", ...],  # 可选: 样例不外发 + SQL/结果双层屏蔽
)
ans = agent.run("问题", clarify=True)   # clarify=False 用于非交互(评测)
# M10 可选: 阶段进度回调(不传时行为与以前逐字节一致), 阶段开始/结束各回调一次
agent.run("问题", clarify=True, on_stage=lambda stage, status, detail: print(stage, status))
```

- `biz_context`:注入各 LLM 阶段的业务口径说明,按库切换语义,券商语义不硬编码进引擎。
- `sql_hints`:生成 SQL 阶段按关键词触发注入口径示例(每类 1 例)。
- `sensitive_columns`:受限字段(库内真实列名)列表,由调用方注入;引擎对其做**样例打码 + SQL 引用拦截 + 结果列掩码**三层屏蔽。企业库示例见 `demo/enterprise_biz.json` 的 `sensitive_columns`。
- `on_stage`(M10):可选阶段回调 `(stage_title, status, detail)`,`status` 取 `RUNNING`(阶段开始)/
  `OK`/`WARN`/`FAIL`/`SKIP`(阶段结束);构造参数与 `run` 参数均可传,默认 `None`(不传则行为不变)。
  演示层用它把阶段进度做成了 SSE 流式接口,见根 [`README.md`](../README.md) 与 [`docs/演示脚本.md`](../docs/演示脚本.md)。
- 返回 `Answer`:answerable / needs_clarification / sql / headers / rows / truncated / explanation / error / trace。

## 评测复现命令(全部真实运行)

```bash
cd benchmark
python build_db.py                    # 重建 enterprise.db(纯标准库, 8 张 CSV → SQLite, 行数校验)
python run_eval.py                    # 单轮全量 46 问评测(全量 deepseek-chat)
python average_runs.py runs/run_A.json runs/run_B.json   # 双跑平均
python rejudge.py --backfill runs/run_*.json             # 判定器改动后: 先重判留档并回填
python run_edge_cases.py               # E1-E9 异常边界用例
python ui_smoke.py                     # UI 全流程自测(5+1 问)
```

- 基准集:benchmark/benchmark.json(46 问,gold_sql 已全量真实跑通闸门)。
- 最新双跑:e2e **100%**(M10 复算, 双跑均 40/40;exec 100% / 口径 100% / 幻觉 0% / P50 8.3s),见 docs/评测报告-基线.md 与根 README 的波动披露。

## 测试

- 引擎回归:此前 4 场景断言(追问分支/失败修复/修复失败终止/阶段异常兜底)+ E1-E9 边界 9/9。
- 评测:46 问双跑平均 + 判定器重判留档(rejudged_*.json)。
- UI:app_core.answer 与 UI 同路径,ui_smoke.py 跑简单→复杂+库外拒绝+追问 6 问。

## FAQ

- **中文乱码**:Windows 终端跑 CLI 加 `PYTHONIOENCODING=utf-8`。
- **库只读**:引擎 mode=ro + PRAGMA query_only,评测/演示不得写库。
- **企业库重建**:由数据集目录 8 张 CSV 载入 SQLite(数值 REAL/日期与 ID TEXT),详见 docs/数据说明.md。
- **复杂题还会波动吗**:M10 复算 4 轮里 3 轮 40/40,1 轮 C05 因 LLM 多带一列判 39/40(温度 0 也不能完全消除);提升路径见 docs/技术报告.md §8。

## M9：日志与数据出境开关

- **日志**: 纯标准库 `logging` + `RotatingFileHandler`, 默认写 `<仓库根>/logs/text2sql.log`(5MB × 3)。
  记录各阶段的开始/结束/耗时、异常、重试, 以及 **LLM 返回空内容时的 WARNING**(野外排查空返回的唯一手段)。
  级别用 `--log-level DEBUG/INFO/WARNING/ERROR`(或 `TEXT2SQL_LOG_LEVEL`)调整, 目录可用 `TEXT2SQL_LOG_DIR` 覆盖。
  日志只记长度/行数/表名/阶段名, **不写 API 密钥与数据行内容**。
- **样例数据出境开关**: `--sample-rows N`(默认 2, 不变更既有行为); `--sample-rows 0` = 完全不把样例数据
  拼进 schema, 即不发给外部 LLM。演示层 `demo/server.py` 有同名参数并在 `/api/health` 回显。
- **静默兜底**: 解释(口径说明)阶段模型返回空串时不再静默空白, 改为兜底文案「（本次未生成口径说明）」并记 WARNING;
  其余阶段维持报错优先, 不猜。
