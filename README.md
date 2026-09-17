# Agentic 智能问数 —— 客户营销场景的应用

把"取数"从**写 SQL** 变成**说人话**：自然语言 → LLM 生成只读 SQL → 执行 → 分层展示结果。

面向客户营销数据的自助问答系统。参赛成果物八项齐备，逐项对账见 [`docs/交付对账.md`](docs/交付对账.md)；
命题、评审标准、参赛注意事项原文见 [`COMPETITION.md`](COMPETITION.md)（**不得增删改写**）。

## 结果（全部来自真实运行）

| 指标 | 值 | 目标 |
|---|---|---|
| 端到端准确率 | **100%**（M10 复算, 双跑均 40/40） | ≥ 90% ✅ |
| SQL 可执行率 / 口径正确率 | 100% / 100% | — |
| 幻觉率 | 0% | — |
| 响应时延 | P50 7.9s · P90 11.8s | 分钟级 ✅ |
| 分难度 | 简单 100% · 中等 100% · 复杂 100% | — |
| 边界场景 E1–E9 | 9/9 兜底 | — |

> 上表是 **2026-09-17 M10 复算**的实测值（引擎加了可选阶段回调后重跑）：`python benchmark/run_eval.py`
> 连跑两轮, 再用 `python benchmark/average_runs.py runs/run_20260917_140703.json runs/run_20260917_141257.json`
> 复核(双轮均达标 40/40); 两份逐题明细与 `docs/评测报告-基线.md` 都是这两轮运行的真实产物。
>
> **波动如实披露**：M10 复算共跑 4 轮全量, 3 轮 40/40; 另有 1 轮(`runs/run_20260917_135617.json`)
> 的 C05(2026 一季度各客户等级资金净流入)判 39/40 —— 该轮 LLM 在 SELECT 里多带了一列
> `COUNT(DISTINCT pty_id) AS 客户数`, 与金标输出列不一致。用**改动前的 HEAD 版引擎**对同一题
> 探测 3 次, 同样出现 1 次多带列 → 属 LLM 生成 SQL 的固有非确定性(温度已是 0 也不能完全消除),
> 不是 M10 回调改动引入的回归; 4 轮的可执行率 / 口径正确率 / 幻觉率均为 100% / 100% / 0%。

评测演进：**M2 60.0% → M3.5 70.0% → R6/R7 77.5% → M3.6 83.8% → M3.7 92.5% → M9 100%(双跑 40/40) → M10 100%(双跑 40/40, 波动披露见上)**
（每一档的口径与判据见 [`docs/评测报告-基线.md`](docs/评测报告-基线.md) 与 [`benchmark/`](benchmark/)；
完整开发过程在 git 历史里）

## 目录

| 路径 | 内容 |
|---|---|
| `text2sql_demo/` | 原型系统：`app.py`（Streamlit UI）+ `text2sql.py`（CLI）双入口、`app_core.py` 引擎 |
| `benchmark/` | 基准集（40 问，人工标注）+ runner + 重判 + 双跑平均；`runs/` 是真实运行产物 |
| `customer_marketing_db/` | 虚构数据的演示小库：`create_db.py` / `analysis.py`（纯标准库） |
| `docs/` | 技术报告 / 数据说明 / 算法说明 / 测试与案例 / 演示脚本 / 安全设计 / 评测口径 / 交付对账 / 环境记录 |
| `Agentic智能问数在客户营销场景的应用数据集/` | 只读数据源（小文件入库，大表见下） |
| `COMPETITION.md` · `AGENTS.md` | 参赛任务书（权威来源）· AI 工程会话规范 |

## 跑起来

环境与部署、双入口启动、配置项（模型 / 温度 / 超时 / 重试 / `sql_hints`）、
`QueryAgent` 官方接入口、评测复现命令与 FAQ —— **见 [`text2sql_demo/README.md`](text2sql_demo/README.md)**
（不在这里重复一份，免得两处不同步）。

要点：演示模式**无需外部账号**；真实调用需本地 `.env.local` 里的 DeepSeek API 密钥（**不入库**）。

## 如何启动演示（M8 本地演示层）

纯标准库本地服务，只绑 `127.0.0.1`，只读访问数据库，API key 只在服务端读取、不进前端：

```bash
# 默认演示小库(引擎默认语义)
python demo/server.py --db customer_marketing_db/marketing.db --port 8000

# 项目主库: 券商企业口径经 JSON 文件注入(演示层不内嵌口径)
python demo/server.py --db "Agentic智能问数在客户营销场景的应用数据集/enterprise.db" \
                      --biz-context demo/enterprise_biz.json --port 8000
```

浏览器打开 `http://127.0.0.1:8000`，一次问答即可看到：结构化计划 / SQL / 结果表格 /
口径说明与默认假设 / 失败自修复过程。接口与演示细节见 [`docs/演示脚本.md`](docs/演示脚本.md)。

**等待期反馈（M10）**：前端用浏览器原生 `EventSource` 消费流式接口
`GET /api/ask/stream?q=…&clarify=false`（纯标准库 SSE），引擎每进入/结束一个阶段就点亮一格进度条，
"已用秒数"计时器一路在走，流式不可用时自动回落到 `POST /api/ask`（不白屏）。引擎侧只是新增一个
**可选**阶段回调 `on_stage`（不传时行为与以前逐字节一致）；流式事件只含阶段名/状态/序号/时间戳/耗时，
不含 API 密钥与任何数据行。

无头自检（不调用 LLM）：

```bash
python demo/selfcheck.py           # 依赖 / 域词 / 口径同源 / judge 未动 / 服务冒烟

# M10 等待期反馈(流式 + 计时)验收
python benchmark/engine_callback_parity.py    # 引擎阶段回调是纯旁路(桩 LLM: 回调开/关输出逐字节一致)
python benchmark/sse_same_source_check.py     # 流式终止事件与 POST 响应同源一致(桩 LLM, 确定性)
python benchmark/sse_stream_check.py          # 真实 LLM: 逐行读 SSE 打时刻 / 与 POST 字段一致 / 密钥 grep
```

## CI / 自动校验（M11）

第三方 clone 本仓库后，**不需要 `enterprise.db`、不需要 API 密钥、也不需要联网**，就能一键自证工程质量：

```bash
python3 ci_check.py        # 逐项打印 OK/FAIL；任一 FAIL 退出码非 0
```

CI 定义在 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)：`ubuntu-latest` + 系统自带 `python3`，
**不做任何第三方包安装**（零 pip、零 requirements）、**不调用真实 LLM**（一律桩）、工作流里无任何密钥，
超时 ≤ 5 分钟，触发 `push` / `pull_request` 到 `main`。它跑的就是下面这 7 项（`ci_check.py` 的检查项 id 一一对应）：

| # | 检查（`python3 ci_check.py --only <id>`） | 内容 |
|---|---|---|
| 1 | `syntax` | 全部 `*.py` 语法编译（CI 步骤用 `python3 -m compileall -q …`） |
| 2 | `deps` | 加载 `text2sql.py` + `demo/server.py` 后 `sys.modules` 只多出标准库 ⇒ **"零第三方依赖"从声明变成机器证明** |
| 3 | `decouple` | 引擎与演示层不出现券商域词（固定词表 + 从 `demo/enterprise_biz.json` 抽取的 token）⇒ 业务语义只走 `biz_context=` 注入 |
| 4 | `judge` | `benchmark/run_eval.py` 的 `judge→pct` 区间 sha256 == 冻结常量 ⇒ 判定器冻结（改一行即红） |
| 5 | `secret` | 全仓库文本文件 grep `sk-[A-Za-z0-9]{20,}`（排除永不入库的 `.env.local` / `.git` / 日志） |
| 6 | `synth-smoke` | `sqlite3` 现场建 3 张小表 + 桩 LLM 驱动引擎走完整链路（理解→检索→计划→检查→SQL→安全校验→执行→检查结果→解释），**零 API 调用** |
| 7 | `sse` | 用合成库起 `demo/server.py`：`/api/health` 返回 200，`/api/ask/stream` 流里既有阶段事件也有 `done` 事件（与 `POST /api/ask` 同源字段） |

**刻意不跑什么（边界写清楚）**：`benchmark/run_eval.py`（40 问全量评测）与 `benchmark/run_edge_cases.py`
（E1–E9 边界用例）**不在 CI 内**，因为它们需要 ① `enterprise.db`（约 88 MB，大表不入库，clone 后无法重建）
② 真实 DeepSeek API 密钥。CI 只覆盖"既不需要数据库、也不需要密钥"的那一半；要复现评测数字，
按 [`docs/评测报告-基线.md`](docs/评测报告-基线.md) 与 [`text2sql_demo/README.md`](text2sql_demo/README.md) 在本地跑。
同理，`demo/selfcheck.py`、`benchmark/` 下的若干无头自检工具与 `benchmark/m7_selfcheck.py` 这类
"与 git HEAD 比对"的检查也留在本机跑（前者需要 `marketing.db`，后者需要完整 git 历史）。

**本地跑同一套检查**（Windows / Linux 同一条命令；脚本用自身文件位置解析仓库根，与 cwd、盘符、系统无关）：

```bash
python3 ci_check.py                  # 7 项全跑，全绿退出码 0
python3 ci_check.py --only judge     # 只跑某一项（与 CI 的单步完全一致）
python3 ci_check.py --only deps,judge
python3 ci_check.py --list           # 列出检查项 id
```

**为什么这些检查不是"永远绿"**：每一项都配过负对照（在临时副本里故意破坏 → 检查变红 → 复原，负对照不入库）：
改坏语法 → `syntax` 红；塞入假 `sk-…` 密钥 → `secret` 红；改判定器一行 → `judge` 红；
引擎里塞券商域词 → `decouple` 红。

## 两个数据库，别搞混

| | `marketing.db` | `enterprise.db` |
|---|---|---|
| 用途 | `customer_marketing_db/` 的演示小库 | 项目主库（评测与演示都用它） |
| 来源 | `create_db.py` 生成，虚构数据、种子固定 | `benchmark/build_db.py` 从 8 张 CSV 重建 |
| 入库 | 否（`*.db` 已忽略） | 否（约 88 MB） |
| 能否从本仓库重建 | ✅ `python3 create_db.py` | ⚠️ 需要那 4 张大表，见下 |

## 数据说明（重要）

- 数据集目录共 8 张 CSV，其中 **4 张大表未入库**（合计约 64 MB：`dim_product` / `dwd_cust_hold_d` /
  `dwd_cust_tran_d` / `dws_cust_aset_d`）。原因：大文件进 git 历史是**永久**的，事后要删得重写历史。
- 因此**从本仓库 clone 出来无法重建 `enterprise.db`**。要重建：把 4 张大表放进
  `Agentic智能问数在客户营销场景的应用数据集/`，再执行

  ```bash
  python3 benchmark/build_db.py        # 纯标准库；按 docs/数据说明.md §3 做行数校验，不一致即失败
  ```

- 已入库的是 4 张小表 + `表描述.sql` + `Q&A.xlsx`（表结构与问答样例，体积小但价值高）。
- 数据来源、每表每字段、预处理与脱敏规则见 [`docs/数据说明.md`](docs/数据说明.md)。

## 安全与合规

只读三保险；姓名与经纪客户号脱敏不展示；不使用未授权数据；开源组件清单一并文档化。
详见 [`docs/安全设计说明.md`](docs/安全设计说明.md)。

## 数据与合规

**哪些内容会离开本机** —— 发起一次提问时, 只有下面这三类会送到外部 LLM:

| 出境内容 | 说明 | 能否关闭 |
|---|---|---|
| 库结构 | 表名、列名、DDL 等元数据(`sqlite_master` 提取) | 不能(关掉就没法生成 SQL) |
| N 行样例数据 | 每表的**真实数据行**前 N 行, 用来让模型理解字段语义; **默认 N=2** | 能: `--sample-rows 0` |
| 用户问题 | 你输入的自然语言问题(交互模式下的补充说明也算) | 不能(输入本身就是它) |

此外, 解释阶段会把**查询结果的前 20 行**(`EXPLAIN_MAX_ROWS`)发给模型用于生成结论文字。

**怎么关掉样例数据出境**(引擎与演示层同一个参数, 默认值都是 2, 保持既有评测口径不变):

```bash
python text2sql_demo/text2sql.py --sample-rows 0 --db <库> "问题"   # CLI: 完全不发样例数据
python demo/server.py --db <库> --sample-rows 0 --port 8000        # 演示层: 同参数, /api/health 回显实际值
```

**日志不落敏感内容**: 引擎与演示层的轮转文件日志(`logs/`, 单文件 5MB × 保留 3 个, 已进 `.gitignore`)只记录
阶段名、耗时、异常类型、重试次数、SQL 长度、结果行数、表名与问题长度 —— **绝不写 API 密钥, 也绝不写数据行内容**。
级别用 `--log-level`(或 `TEXT2SQL_LOG_LEVEL`)调, 默认 INFO。

**本项目的数据是虚构的**: `customer_marketing_db/marketing.db` 由 `create_db.py` 用固定种子生成;
`enterprise.db` 由主办方提供的券商营销样例 CSV 经 `benchmark/build_db.py` 重建 —— 两者都是比赛虚构数据,
姓名与经纪客户号在文档与演示输出中一律脱敏或不展示(见 [`docs/数据说明.md`](docs/数据说明.md)、
[`docs/安全设计说明.md`](docs/安全设计说明.md))。

**接真实生产库时的建议**: ① 一律 `--sample-rows 0`, 不让真实客户数据行进 prompt;
② 或把 `LLM_BASE_URL` 指向**私有化/本地部署模型**, 数据不出企业边界;
③ 用只读账号接入, 遵守最小权限与审计要求, 日志目录按敏感资产同样管控。

## 关于本仓库

从个人程序仓库 `programs` 拆出，**保留了全部开发历史**（从 M1 到 M5 的每一步提交都在），
去掉了与项目无关的小工具。
