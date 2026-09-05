# AGENTS.md — 项目记忆(每次会话自动加载,遵守本文档)

## 项目是什么
客户营销场景 AI Native 智能取数 Agent 参赛项目。权威任务书 = `COMPETITION.md`(通读,按里程碑干活)。命题/评审/注意事项全文在其附录。

## 目录结构
- `text2sql_demo/` — 核心引擎(唯一代码资产)
  - `text2sql.py` — 9 阶段状态机 QueryAgent(理解→检索→计划→检查→SQL→安全校验→执行→结果→解释);biz_context 注入业务语义;Trace 全程记录
  - `README.md` — 用法/架构/安全/biz_context 接入范式
  - `.env.local` — LLM_API_KEY(不入库,勿打印)
- `customer_marketing_db/` — 虚构银行营销库(引擎默认 demo 库,回归用)
- `Agentic智能问数在客户营销场景的应用数据集/` — 券商客户营销库(**主战场**):`enterprise.db` 91MB、源 CSV、`表描述.sql`(元数据设计输入)、`Q&A.xlsx`(**空壳勿依赖**)
- `benchmark/`、`docs/` — M2/M4 起由评测与文档填充

## 铁律
1. `text2sql.py` 引擎禁止重写,只许增量扩展/修 bug;大改先写论证等队伍审批
2. 券商库语义走 `biz_context=` 注入,禁止硬编码进 text2sql.py;默认 BUSINESS_DESC 不动
3. 引擎零第三方依赖(纯标准库);评测/UI 可加依赖但必须 requirements.txt 说明
4. `*.db` 不入库;>10MB CSV 默认不入库;密钥/隐私数据永不提交
5. 子任务完成即 commit(`feat:`/`fix:`/`docs:`/`test:`),中文 message
6. 评测数字必须真实运行得出,禁止估算/编造

## 环境事实
- 仓库根:Windows `C:\Users\30818\programs` = WSL `/mnt/c/Users/30818/programs`
- Python:Windows `python`≈3.12;WSL `python3`=3.14;先探测再统一;输出加 `PYTHONIOENCODING=utf-8`
- LLM:DeepSeek `https://api.deepseek.com`,`deepseek-v4-flash`;密钥 `.env.local`(LLM_API_KEY)/环境变量
- 引擎只读跑库(mode=ro + query_only),评测/演示不得写库
- git 身份:congxinjie &lt;3081864713@qq.com&gt;,main 分支

## 常用命令
- 引擎回归测试方式见 text2sql_demo 内既有脚本(此前有 4 场景断言测试:追问分支/失败修复/修复失败终止/阶段异常兜底),新增功能要补对应断言
- 单问运行:`cd text2sql_demo && python text2sql.py --db <库> "问题"`(Windows 侧 python)
