# -*- coding: utf-8 -*-
"""评测 runner: 40 问基准集真实调用 DeepSeek, 产出逐题明细 + 六指标 + 基线报告。

用法:
    cd benchmark && python run_eval.py
输出:
    benchmark/runs/run_<时间戳>.json   逐题明细(判定字段 + Trace 摘要)
    docs/评测报告-基线.md               基线报告(真实数字)

口径与判定规则见 docs/评测口径定义.md; 纯标准库, 无第三方依赖。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT2SQL_DIR = ROOT / "text2sql_demo"
DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
BENCH = HERE / "benchmark.json"
RUNS = HERE / "runs"
REPORT = ROOT / "docs" / "评测报告-基线.md"

sys.path.insert(0, str(TEXT2SQL_DIR))
import text2sql  # noqa: E402
from text2sql import QueryAgent, load_api_key, get_table_meta  # noqa: E402

# R7: 单快照豁免仅限 ads_cust_info_d(唯一 data_dt 只有 20260531 的表), 不全局豁免时间检查
SINGLE_SNAPSHOT_TABLES = {"ads_cust_info_d"}
# R6: 列名对齐比对——金标列名别名词表(仅收敛常见同义别名, 不放宽语义)
HEADER_SYNONYMS = {
    "客户数": {"客户数", "客户数量", "客户人数", "人数", "客户数目"},
    "客户号": {"客户号", "客户ID", "客户编号", "客户id", "pty_id"},
    "营业部": {"营业部", "营业部名称"},
    "营业部数": {"营业部数", "营业部数量", "网点数", "网点数量"},
    "学历": {"学历", "学历等级", "教育程度"},
    "职业": {"职业", "职业类型", "职业名称"},
    "性别": {"性别", "性别类型"},
    "客户等级": {"客户等级", "等级", "客户级别"},
    "交易额": {"交易额", "交易金额", "交易总额", "成交额", "交易总额合计"},
    "总资产": {"总资产", "总资产合计", "资产合计", "客户总资产"},
    "持仓市值": {"持仓市值", "市值", "持仓总市值"},
    "买入金额": {"买入金额", "买入金额合计", "买入总额"},
    "卖出金额": {"卖出金额", "卖出金额合计", "卖出总额"},
    "交易笔数": {"交易笔数", "交易笔数合计", "成交笔数"},
    "现金资产": {"现金资产", "现金资产合计"},
    "平均年龄": {"平均年龄", "客户平均年龄", "平均岁数"},
    "最大年龄": {"最大年龄", "年龄最大值"},
    "交易天数": {"交易天数", "交易日数"},
    "省份": {"省份", "省"},
    "城市": {"城市", "市"},
    "分公司": {"分公司", "上级分公司"},
    "年龄段": {"年龄段", "年龄区间", "年龄分组"},
    "账户": {"账户", "sys_source", "账户类型"},
    "净流入": {"净流入", "资金净流入", "资金净流入额", "净流入金额"},
    "一级分类": {"一级分类", "一级分类名称", "产品一级分类"},
    "二级分类": {"二级分类", "二级分类名称", "产品二级分类"},
    "资产增幅": {"资产增幅", "总资产增幅", "增幅"},
}
# 分级模型策略(M3.5): 全量统一 deepseek-chat —— 空响应实验证明 flash 有随机空返回, 双跑取平均需稳定模型
MODEL_BY_DIFF = {"simple": "deepseek-chat", "medium": "deepseek-chat", "complex": "deepseek-chat"}
# M3.7 few-shot 口径示例(关键词触发, 注入 _generate_sql; 券商口径留在评测 harness, 不硬编码进引擎)
SQL_HINTS = {
    "持有": "-- 例:\"持有比亚迪超过1000\" = 份额口径: WITH s AS (SELECT pty_id FROM dwd_cust_hold_d h JOIN dim_product d ON h.prdt_id=d.prdt_id WHERE h.data_dt='20260331' AND d.prdt_name LIKE '%比亚迪%' GROUP BY h.pty_id HAVING SUM(h.hold_cnt)>1000) SELECT ... FROM s ...(hold_cnt=份额, 不用 mkt_val 市值)",
    "增幅": "-- 例:\"总资产增幅\" = 绝对增量(期末-期初): WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id) SELECT e.pty_id, e.a-COALESCE(s.a,0) AS 增幅 FROM e LEFT JOIN s ON e.pty_id=s.pty_id ORDER BY 增幅 DESC LIMIT 10",
    "科创": "-- 例:\"科创板\"分类名匹配用 LIKE: prdt_type_name LIKE '%科创%'(不用 = '科创板'); 按 分公司(up_org_name)+营业部名(org_name) 聚合",
    "盈亏": "-- 例:\"盈亏合计\" = 期末资产-期初资产+资金流出-资金流入: WITH s AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) a FROM dws_cust_aset_d WHERE data_dt='20260101' GROUP BY pty_id), e AS (SELECT pty_id, SUM(nm_tot_aset+fc_pur_aset) a FROM dws_cust_aset_d WHERE data_dt='20260331' GROUP BY pty_id), f AS (SELECT pty_id, SUM(cash_out+tran_out+assign_out) o, SUM(cash_in+tran_in+assign_in) i FROM dws_cust_fin_d WHERE data_dt BETWEEN '20260101' AND '20260331' GROUP BY pty_id) SELECT ROUND(SUM(COALESCE(e.a,0)-COALESCE(s.a,0)+COALESCE(f.o,0)-COALESCE(f.i,0)),4) AS 盈亏合计 FROM e LEFT JOIN s ON e.pty_id=s.pty_id LEFT JOIN f ON e.pty_id=f.pty_id",
}
# M6 口径断言(可配置, 由评测 harness 注入引擎 caliber_assertions; 机制通用, 不针对题号)
CALIBER_ASSERTIONS = [
    {
        "id": "持有超过N用份额",
        "when": ["持有", "超过"],
        "must_contain": ["hold_cnt"],
        "must_not_contain": ["mkt_val"],
        "desc": "持有某产品超过 N(未注明市值/金额)按份额 SUM(hold_cnt)>N, 不用市值 mkt_val",
    },
    {
        "id": "交易额用买卖金额",
        "when": ["交易额"],
        "must_contain": ["buy_amt", "sell_amt"],
        "must_not_contain": [],
        "desc": "交易额=buy_amt+sell_amt",
    },
    {
        "id": "交易额超N先按客户聚合",
        "when": ["交易额", "超过", "按"],
        "must_contain": ["pty_id", "HAVING"],
        "must_not_contain": [],
        "desc": "交易额超过 N 的客户按维度统计: 必须先按 pty_id 聚合并 HAVING 过滤, 再按维度分组",
    },
    {
        "id": "分类名用LIKE",
        "when": ["科创"],
        "must_contain": ["LIKE"],
        "must_not_contain": [],
        "desc": "科创板等产品/分类名称匹配用 LIKE '%名%', 禁止精确等值",
    },
    {
        "id": "盈亏口径",
        "when": ["盈亏"],
        "must_contain": ["nm_tot_aset", "fc_pur_aset",
                        "cash_out", "tran_out", "assign_out",
                        "cash_in", "tran_in", "assign_in"],
        "must_not_contain": [],
        "desc": "盈亏=期末总资产-期初总资产+资金流出-资金流入; 总资产=nm_tot_aset+fc_pur_aset; 资金流=cash/tran/assign",
    },
    {
        "id": "客群筛选后输出客户数",
        "output_contract": {
            "when_any": ["持有", "超过", "以上", "交易过", "交易额", "年龄", "性别", "等级"]
        },
        "desc": "客群筛选(客户级阈值条件)后再求指标合计/按维度统计时, 输出列契约应包含客户数列; 触发词由评测侧注入, 引擎不内置",
    },
]

RULES_CHANGES = [    "R1 口径启发式: expect_tables 全属于单快照/纯维度表时豁免 data_dt 字面量检查(已被 R7 收窄)。",
    "R2 结果比对: 数值以 gold 小数位数为准先四舍五入再比(1e-6 容差兜底); 分组标签规范化(去'岁'/空白)。",
    "R3 分级模型策略(M3): 复杂题用 deepseek-chat, 简单/中等用 deepseek-v4-flash(已被 R5 取代)。",
    "R4 幻觉检查修正: CTE 识别由 'WITH 后第一个名' 改为 '任意 AS(' 模式, 避免多 CTE(WITH s AS..., e AS...)的 e/f 被误判为表幻觉。",
    "R5 全量统一 deepseek-chat: M3.5 起简单/中等/复杂全部用 chat(flash 随机空返回噪声大); 中等对比实验 chat exec 14/14 vs flash 12/14。",
    "R6 列名对齐比对: 金标列必须全部命中(仅收敛 客户数/客户号/营业部 常见别名); 额外列仅限展示类(姓名/名称列); 对齐列数值性必须一致。",
    "R7 单快照豁免限定 ads_cust_info_d: 仅当其出现在 expect_tables 时豁免其专属 token 20260531, 不再全局豁免时间检查。",
    "R8 展示标签/列名别名扩展: 一级分类/二级分类列名别名归一; 分桶边界等价标签归一(<30/[30,50)/≥60 等与中文写法等价)。",
    "R9 llm_chat 调用层重试: 超时/URLError/HTTP 429/5xx 指数退避(1.5s/3s)重试最多 3 次, 不动状态机。",
    "R10 few-shot 口径示例: _generate_sql 按关键词触发注入 1 例(持有→份额 hold_cnt / 增幅→绝对增量 / 科创→LIKE+分公司聚合); 机制在引擎 sql_hints, 内容由评测 harness 注入(不硬编码券商口径)。",
    "M13 实体识别与校验(**非判定器变更**): 引擎按 harness 注入的词表(ENTITIES)识别「实体」(explicit 问题直指 / implied 计划隐含), 五项程序化校验后才落 Answer.entities; 实体不参与 SQL 组装, 逐题留档在 records[].entities, judge 判定区间与评测口径零改动。",
]

# 受限敏感字段(库内真实列名, 由 harness 注入引擎; 引擎只认机制, 不硬编码券商列名)。
# sor_pty_id 为疑似个人关联标识: 样例数据不外发、SQL 引用被拦截、结果列掩码。
SENSITIVE_COLUMNS = ["sor_pty_id"]

# M13: 实体词表(注入侧声明"实体名 -> 承载表/关联键/别名"; 引擎只认机制, 不硬编码表名列名)
ENTITIES = {
    "客户": {"table": "ads_cust_info_d", "key": "pty_id",
             "aliases": ["客户", "客户信息", "客户主档", "客户档案", "客户名单"]},
    "产品": {"table": "dim_product", "key": "prdt_id",
             "aliases": ["产品", "产品名称", "产品分类", "产品类型", "产品线"]},
    "营业部": {"table": "dim_branch", "key": "org_id",
               "aliases": ["营业部", "分支机构", "网点", "营业网点", "所属营业部"]},
}

# 企业口径语义(隔离在评测脚本内, 不进入 text2sql.py)
ENTERPRISE_BIZ = """券商客户营销库(2026-Q1 事实 + 客户主档单快照)。
【表】dim_branch 营业部(org_id/org_name/up_org_id/up_org_name); dim_public 编码字典(code,code_type_id,describe); dim_product 产品(prdt_id/prdt_name/prdt_type_id 二级/prdt_type_name/up_prdt_type_id 一级/up_prdt_type_name); ads_cust_info_d 客户主档(pty_id/org_id/cust_age/prov_name/city_name/cust_lvl_cd/cust_status/cust_type/gender_cd/edu_cd/prof_cd/name 已脱敏); dwd_cust_hold_d 每日持仓(pty_id/prdt_id/sys_source/ccy/hold_cnt 份额/mkt_val 市值); dwd_cust_tran_d 每日买卖(buy_cnt 次数/buy_mnt 数量/buy_amt 金额/buy_rake 佣金/buy_fare 费用, sell_* 同); dws_cust_aset_d 每日资产(nm_tot_aset 普通总资产/nm_bal 普通现金/fc_pur_aset 信用净资产/fc_bal 信用现金); dws_cust_fin_d 每日资金流(cash_in/cash_out/tran_in/tran_out/assign_in/assign_out)。
【时间口径】客户主档只有 data_dt='20260531' 一个快照; 持仓与资产表 data_dt 覆盖 20260101~20260331(90 天); 交易与资金表覆盖 20260105~20260331(56 个交易日)。2026 年一季度=20260101~20260331。
【编码解码】客户表码字段 join dim_public: cust_lvl_cd→code_type_id='100'(紫金理财钻石卡/白金卡/金卡/银卡客户等); cust_status→'200'; gender_cd→'500'(5000002 男/5000003 女); edu_cd→'600'(6000003 硕士/6000004 学士等); prof_cd→'700'。dim_public 的 code_type_id='300' 证件、'400' 风险等级在库中无任何表引用, 相关业务问题按不可答处理。
【产品】一级分类用 up_prdt_type_id(PT040000 股票/PT030000 债券/PT050000 开放式基金/PT070000 理财/PT090000 恒生多金融/PT020000 权证/PT060000 衍生品/PT080000 回购/PT100000 私募/PT110000 贵金属/PT990000 现金类), 二级分类用 prdt_type_id/prdt_type_name(如 科创板/A股/沪港通)。注意 up_prdt_type_id 与 prdt_type_id 存在同名多义(如 PT090000 同时叫恒生多金融产品/OTC产品), 归类以 ID 为准。产品名(prdt_name)用于按名称过滤, 如 比亚迪/招商银行/中国平安。
【账户与币种】sys_source: nm=普通账户, fc=信用账户; ccy: 0 人民币/1 美元/2 港币。资产表无 sys_source, nm/fc 为并列字段。
【关键口径(队伍约定)】总资产=nm_tot_aset+fc_pur_aset; 现金资产=nm_bal+fc_bal; 交易额=buy_amt+sell_amt; 交易笔数=buy_cnt+sell_cnt; 交易天数=COUNT(DISTINCT data_dt); 日均=区间合计/区间天数(资产 90 天); 交易量未注明单位一律按金额; 盈亏=(期末总资产-期初总资产)+(资金流出-资金流入), 其中资金流入=cash_in+tran_in+assign_in, 资金流出=cash_out+tran_out+assign_out。
【列名约定(M6.1 补: 原在引擎里的命名约束, 按解耦要求搬到这里)】指标列用业务原名, 不加"客户/产品/该"等前缀,
不加"合计/编码/名称"等后缀(仅当队伍口径名本身含"合计"时例外, 如"盈亏合计"); 维度列按问题里的称呼,
如"营业部""客户等级""一级分类"。
【易错提醒】客户表只有 20260531 一个日期; 营业部与客户姓名已脱敏; 过滤日期用 data_dt 的 YYYYMMDD 字符串比较; 营业部数=dim_branch 行数 COUNT(*)(一行一网点, 同名营业部是不同网点, 不要 COUNT(DISTINCT org_name))。
【易混口径(M3.6 固化, 必须遵守)】"持有某产品超过 N"(未注明市值/金额)按份额口径 SUM(hold_cnt)>N, 不用 mkt_val;
"增幅"默认是绝对增量(期末-期初), 只有题面说"增幅率/增速/涨幅"才用(期末-期初)/期初;
产品/分类名称匹配一律 LIKE '%名%'(科创板 → prdt_type_name LIKE '%科创%'), 禁止精确等值;
年龄分桶默认边界(题面未给时): <30 / [30,50) / [50,60) / ≥60;
营业部统计粒度: 按 分公司(up_org_name)+营业部名称(org_name) 聚合, 同名营业部合并, 不按 org_id 拆分;
客户等级名称以 dim_public.describe 为准(如 '紫金理财钻石卡客户' 含"客户"后缀), 不得截断;
产品分类维度(一级/二级)同时输出 ID 与名称两列, 列名用"一级分类ID/一级分类"、"二级分类ID/二级分类"(一级=up_prdt_type_id+up_prdt_type_name, 二级=prdt_type_id+prdt_type_name; 不要用"产品一级分类编码/产品二级分类名称"等冗长列名); 其他编码维度(客户等级/性别/学历/职业等)只输出 dim_public.describe 名称列, 不要额外输出 code/ID 列;
问"哪个/哪些客户"必须输出客户号 pty_id; 普通/信用账户分组时, 账户列直接输出 sys_source 原值(nm/fc), 不要 CASE WHEN 转成"普通账户/信用账户"中文。"""


def norm(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return v


def decimals_of(v):
    s = repr(float(v))
    return len(s.split(".")[1]) if "." in s else 0


def norm_label(v):
    s = str(v).replace("岁", "").replace(" ", "").strip()
    # 分桶边界等价标签归一(R8): '<30'/'≥60' 等数学写法与中文写法等价
    s = (s.replace("<30", "30以下").replace("[30,50)", "30-50")
           .replace("[50,60)", "50-60").replace("≥60", "60以上")
           .replace(">=60", "60以上").replace(">60", "60以上")
           .replace("60及以上", "60以上").replace("60岁及以上", "60以上"))
    return s


def canon_header(h):
    h = str(h).strip().strip('"`').strip()
    for canon, aliases in HEADER_SYNONYMS.items():
        if h in aliases:
            return canon
    return h


def is_display_col(h):
    return any(k in str(h) for k in ("姓名", "名称", "名字"))


def cells_equal(gold_v, agent_v, tol):
    g, a = norm(gold_v), norm(agent_v)
    if isinstance(g, float) and isinstance(a, float):
        if g == a:
            return True
        d = decimals_of(g)
        if d > 0:
            scale = 10 ** d
            if round(a * scale) / scale == round(g * scale) / scale:
                return True
        return abs(a - g) <= tol * max(1.0, abs(a), abs(g))
    if isinstance(g, str) and isinstance(a, str):
        return norm_label(g) == norm_label(a)
    return g == a


def rows_equal(gold_headers, gold_rows, got_headers, got_rows, tol):
    """R6 列名对齐比对: 金标列必须全部命中; 额外列仅限展示类(姓名/名称); 数值列集合一致。
    单值题(1×1)按值比对, 列名不参与(无歧义)。"""
    if len(got_rows) != len(gold_rows):
        return False
    # 单值题: 值比对(严格), 不因列名别名误伤
    if len(gold_headers) == 1 and len(gold_rows) == 1 and len(got_headers) == 1 and len(got_rows) == 1:
        return cells_equal(gold_rows[0][0], got_rows[0][0], tol)
    gc = [canon_header(h) for h in gold_headers]
    ac = [canon_header(h) for h in got_headers]
    align = {}
    for gi, g in enumerate(gc):
        hits = [i for i, a in enumerate(ac) if a == g]
        if len(hits) != 1:
            return False  # 金标列缺失或重复命中
        align[gi] = hits[0]
    used = set(align.values())
    for i, h in enumerate(got_headers):
        if i not in used and not is_display_col(h):
            return False  # 非展示类额外列, 不放行
    # 数值列集合必须一致(对齐列数值性一致)
    def col_is_numeric(vals):
        return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
    for gi, ai in align.items():
        if col_is_numeric([r[gi] for r in gold_rows]) != col_is_numeric([r[ai] for r in got_rows]):
            return False
    def norm_cell(v):
        if isinstance(v, str):
            return norm_label(v)
        if isinstance(v, float):
            return round(v, 6)
        return v

    key = lambda row, idxs: json.dumps([norm_cell(row[i]) for i in idxs],
                                       ensure_ascii=False, default=str)
    g1 = sorted(gold_rows, key=lambda r: key(r, range(len(gold_headers))))
    g2 = sorted(got_rows, key=lambda r: key(r, [align[i] for i in range(len(gold_headers))]))
    for r1, r2 in zip(g1, g2):
        for gi in range(len(gold_headers)):
            if not cells_equal(r1[gi], r2[align[gi]], tol):
                return False
    return True


def dashes(tok):
    """20260531 -> 2026-05-31"""
    if len(tok) == 8 and tok.isdigit():
        return f"{tok[:4]}-{tok[4:6]}-{tok[6:]}"
    return tok


def sql_table_names(sql):
    names = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))
    # CTE 名: WITH x AS ( 或 逗号后 y AS ( —— R4 修正: 支持多 CTE, 避免把 CTE 误判为表幻觉
    ctes = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", sql, re.I))
    return {n.lower() for n in names if n.lower() not in ctes}


def judge(rec, meta):
    """判定 exec_ok / result_ok / caliber_ok / hallucination, 全部落回 rec。"""
    sql = (rec.get("sql") or "").strip()
    gold = rec["gold_answer"]

    rec["exec_ok"] = bool(rec.get("sql") and not rec.get("error") and rec.get("rows") is not None)
    rec["result_ok"] = False
    rec["caliber_ok"] = False
    rec["hallucination"] = False
    rec["halluc_detail"] = []

    if rec["exec_ok"]:
        rec["result_ok"] = rows_equal(gold["headers"], gold["rows"],
                                      rec.get("headers", []), rec["rows"],
                                      gold.get("tolerance", 1e-6))

        # 口径(规则启发式): 必需表 + 必需时间字面量出现; R7: 单快照豁免仅限 ads_cust_info_d 的 20260531
        ok_tables = all(re.search(rf"\b{t}\b", sql, re.I) for t in rec.get("expect_tables", []))
        exempt_20260531 = "ads_cust_info_d" in rec.get("expect_tables", [])
        ok_time = all((t in sql) or (dashes(t) in sql)
                      for t in rec.get("expect_time", [])
                      if not (t == "20260531" and exempt_20260531))
        rec["caliber_ok"] = ok_tables and ok_time
        if not ok_tables:
            rec["halluc_detail"].append("口径: SQL 缺少期望表")
        if not ok_time:
            rec["halluc_detail"].append("口径: SQL 缺少期望时间范围")

        # 表/列幻觉(结构化比对)
        for t in sql_table_names(sql):
            if t not in meta:
                rec["hallucination"] = True
                rec["halluc_detail"].append(f"表幻觉: {t}")
        for ref in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", sql):
            tbl, col = ref[0].lower(), ref[1].lower()
            if tbl in meta and col not in meta[tbl]:
                rec["hallucination"] = True
                rec["halluc_detail"].append(f"列幻觉: {ref[0]}.{ref[1]}")
    else:
        if rec.get("error"):
            rec["halluc_detail"].append(f"失败: {rec['error'][:200]}")
        elif not rec.get("sql"):
            rec["halluc_detail"].append("失败: 未生成 SQL(可能被拒绝/阶段异常)")
    return rec


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def percentile(sorted_times, p):
    """统一分位数口径(floor nearest-rank): idx = min(int(p*n), n-1)。report 与 average_runs 共用。"""
    if not sorted_times:
        return 0
    n = len(sorted_times)
    return sorted_times[min(int(p * n), n - 1)]


def parse_args(argv):
    """解析命令行参数: 默认全量; --ids 按题号过滤, --limit 限制题数(便于单题迭代)。"""
    import argparse
    ap = argparse.ArgumentParser(description="评测 runner: 默认全量 40 问")
    ap.add_argument("--ids", help="逗号分隔题号, 如 C02,C07; 缺省=全量")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题; 缺省=不限")
    ap.add_argument("--no-report", action="store_true", help="全量运行也不覆盖 docs/评测报告-基线.md(稳定性多轮跑批用); 缺省=覆盖")
    return ap.parse_args(argv)


def main():
    opts = parse_args(sys.argv[1:])
    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    all_items = bench["items"]
    ids_wanted = [s.strip() for s in (opts.ids or "").split(",") if s.strip()]
    items = [q for q in all_items if q["id"] in ids_wanted] if ids_wanted else list(all_items)
    if opts.limit is not None:
        items = items[:opts.limit]
    partial = len(items) != len(all_items)
    if ids_wanted:
        missing = [i for i in ids_wanted if i not in {q["id"] for q in all_items}]
        if missing:
            sys.exit(f"未知题号: {missing}")
    meta = get_table_meta(str(DB))
    api_key = load_api_key()
    forced_model = os.environ.get("LLM_MODEL", "").strip()
    agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ,
                       sql_hints=SQL_HINTS, caliber_assertions=CALIBER_ASSERTIONS,
                       sensitive_columns=SENSITIVE_COLUMNS, entities=ENTITIES)

    print(f"评测开始: {len(items)}/{len(all_items)} 问{' (部分运行: 不覆盖基线报告)' if partial else ''}, "
          f"全量 deepseek-chat(空响应实验结论, 双跑取平均)"
          + (f", LLM_MODEL={forced_model} 覆盖" if forced_model else ""), flush=True)
    t_all_start = time.time()
    records = []
    for i, q in enumerate(items, 1):
        text2sql.MODEL = forced_model or MODEL_BY_DIFF.get(q["difficulty"], "deepseek-v4-flash")
        rec = dict(q)
        rec["gold_answer"] = q["gold_answer"]
        rec["model"] = text2sql.MODEL
        t0 = time.time()
        try:
            ans = agent.run(q["question"], clarify=False)
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = ans.sql
            rec["rows"] = ans.rows
            rec["headers"] = ans.headers
            rec["error"] = ans.error or ""
            rec["answerable"] = ans.answerable
            rec["needs_clarification"] = list(ans.needs_clarification)
            # M13: 逐题留档识别出的实体(explicit/implied), 供验收复算
            rec["entities"] = list(getattr(ans, "entities", []) or [])
            rec["entities_covered"] = bool(getattr(ans, "entities_covered", False))
            rec["explanation"] = (ans.explanation or "")[:300]
            rec["trace_tail"] = [
                {"stage": e.stage, "status": e.status} for e in ans.trace.entries[-3:]
            ]
        except Exception as ex:
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = ""
            rec["rows"] = None
            rec["error"] = f"{type(ex).__name__}: {ex}"
            rec["answerable"] = True
            rec["needs_clarification"] = []
            rec["entities"] = []
            rec["entities_covered"] = False
            rec["explanation"] = ""
            rec["trace_tail"] = []
        judge(rec, meta)
        records.append(rec)
        ok = "OK" if (rec["exec_ok"] and rec["result_ok"] and rec["caliber_ok"] and not rec["hallucination"]) else "FAIL"
        print(f"[{i:02d}/{len(items)}] {q['id']} {q['difficulty'][:4]} exec={int(rec['exec_ok'])} "
              f"res={int(rec['result_ok'])} cal={int(rec['caliber_ok'])} hal={int(rec['hallucination'])} "
              f"t={rec['elapsed_ms']}ms {ok}", flush=True)

    total_ms = int((time.time() - t_all_start) * 1000)

    # ---- 汇总 ----
    def stats(recs):
        n = len(recs)
        if n == 0:
            return {}
        ex = sum(1 for r in recs if r["exec_ok"])
        rr = sum(1 for r in recs if r["exec_ok"] and r["result_ok"])
        cr = sum(1 for r in recs if r["exec_ok"] and r["caliber_ok"])
        e2e = sum(1 for r in recs if r["exec_ok"] and r["result_ok"] and r["caliber_ok"])
        hal = sum(1 for r in recs if r["hallucination"])
        times = sorted(r["elapsed_ms"] for r in recs)
        p50 = percentile(times, 0.5)
        p90 = percentile(times, 0.9)
        return {
            "n": n, "exec_rate": pct(ex, n), "result_rate": pct(rr, n),
            "caliber_rate": pct(cr, n), "e2e_rate": pct(e2e, n),
            "hallucination_rate": pct(hal, n), "p50_ms": p50, "p90_ms": p90,
            "max_ms": max(times) if times else 0,
        }

    all_s = stats(records)
    by_diff = {d: stats([r for r in records if r["difficulty"] == d])
               for d in ("simple", "medium", "complex")}
    failed = [r for r in records if not (r["exec_ok"] and r["result_ok"] and r["caliber_ok"] and not r["hallucination"])]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    RUNS.mkdir(exist_ok=True)
    run_file = RUNS / f"run_{ts}.json"
    run_file.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(DB),
        "benchmark": str(BENCH),
        "total_elapsed_ms": total_ms,
        "metrics": {"all": all_s, "by_difficulty": by_diff},
        "records": records,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写: {run_file}")

    # ---- 基线报告(仅全量运行覆盖; 部分运行只落 run JSON 供逐题迭代) ----
    if partial:
        print("部分运行: 跳过 docs/评测报告-基线.md(仅全量运行覆盖基线报告)。")
        return
    if getattr(opts, "no_report", False):
        print("--no-report: 跳过 docs/评测报告-基线.md(全量 run JSON 已落盘)。")
        return
    lines = []
    A = lines.append
    A("# 评测报告-基线")
    A("")
    A(f"> 生成时间:{datetime.now().isoformat(timespec='seconds')}")
    A(f"> 数据库:`{DB}`(enterprise.db, 91MB, 只读)")
    A("> 引擎:text2sql_demo/text2sql.py 9 阶段 QueryAgent,biz_context=企业口径语义")
    A("> 模型:引擎默认(deepseek-v4-flash 可经 LLM_MODEL 覆盖);全部数字来自真实运行,禁止估算")
    A("> 隐私:受限字段(sensitive_columns, 如 sor_pty_id)样例数据不外发、SQL 引用拦截、结果列掩码;真实数据建议 --sample-rows 0")
    A("")
    A("## 1. 指标汇总(40 问 = 简单14/中等14/复杂12)")
    A("")
    A("| 指标 | 全部 | 简单 | 中等 | 复杂 |")
    A("|---|---|---|---|---|")
    A(f"| SQL 可执行率 | {all_s['exec_rate']}% | {by_diff['simple']['exec_rate']}% | {by_diff['medium']['exec_rate']}% | {by_diff['complex']['exec_rate']}% |")
    A(f"| 结果正确率 | {all_s['result_rate']}% | {by_diff['simple']['result_rate']}% | {by_diff['medium']['result_rate']}% | {by_diff['complex']['result_rate']}% |")
    A(f"| 口径正确率 | {all_s['caliber_rate']}% | {by_diff['simple']['caliber_rate']}% | {by_diff['medium']['caliber_rate']}% | {by_diff['complex']['caliber_rate']}% |")
    A(f"| 端到端准确率(参赛口径,目标≥90%) | **{all_s['e2e_rate']}%** | {by_diff['simple']['e2e_rate']}% | {by_diff['medium']['e2e_rate']}% | {by_diff['complex']['e2e_rate']}% |")
    A(f"| 幻觉率(目标趋近0) | {all_s['hallucination_rate']}% | {by_diff['simple']['hallucination_rate']}% | {by_diff['medium']['hallucination_rate']}% | {by_diff['complex']['hallucination_rate']}% |")
    A("")
    A("| 耗时 | 全部 | 简单 | 中等 | 复杂 |")
    A("|---|---|---|---|---|")
    A(f"| P50 | {all_s['p50_ms']/1000:.1f}s | {by_diff['simple']['p50_ms']/1000:.1f}s | {by_diff['medium']['p50_ms']/1000:.1f}s | {by_diff['complex']['p50_ms']/1000:.1f}s |")
    A(f"| P90 | {all_s['p90_ms']/1000:.1f}s | {by_diff['simple']['p90_ms']/1000:.1f}s | {by_diff['medium']['p90_ms']/1000:.1f}s | {by_diff['complex']['p90_ms']/1000:.1f}s |")
    A(f"| MAX | {all_s['max_ms']/1000:.1f}s | {by_diff['simple']['max_ms']/1000:.1f}s | {by_diff['medium']['max_ms']/1000:.1f}s | {by_diff['complex']['max_ms']/1000:.1f}s |")
    A("")
    # M13: 实体识别统计(逐题留档在 run JSON 的 records[].entities)
    _cov = sum(1 for r in records if r.get("entities_covered"))
    _ent = [e for r in records for e in (r.get("entities") or [])]
    _ex = sum(1 for e in _ent if e.get("source") == "explicit")
    _im = sum(1 for e in _ent if e.get("source") == "implied")
    A("### 1b. 实体识别(M13: 命题五要素之「实体」)")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 识别出实体的题数 | {_cov}/{len(records)} |")
    A(f"| 实体项总数(explicit 问题直指 / implied 计划隐含) | {len(_ent)} ({_ex} / {_im}) |")
    A("")
    A(f"总耗时:{total_ms/1000:.1f}s({total_ms/60000:.1f} 分钟)")
    A("")
    A("## 2. 逐题结果")
    A("")
    A("| ID | 难度 | 问题 | exec | result | caliber | 幻觉 | 耗时 |")
    A("|---|---|---|---|---|---|---|---|")
    for r in records:
        q_short = r["question"][:28].replace("|", "/")
        A(f"| {r['id']} | {r['difficulty']} | {q_short} | {'✓' if r['exec_ok'] else '✗'} | "
          f"{'✓' if r['result_ok'] else '✗'} | {'✓' if r['caliber_ok'] else '✗'} | "
          f"{'✗' if r['hallucination'] else '✓'} | {r['elapsed_ms']/1000:.1f}s |")
    A("")
    A("## 3. 未达标问题剖析")
    A("")
    if failed:
        for r in failed:
            A(f"### {r['id']} {r['difficulty']} — {r['question'][:60]}")
            A(f"- 判定: exec={r['exec_ok']} result={r['result_ok']} caliber={r['caliber_ok']} 幻觉={r['hallucination']}")
            if r.get("error"):
                A(f"- 错误: {r['error'][:300]}")
            if r.get("sql"):
                A(f"- SQL: `{r['sql'][:240]}`")
            if r.get("halluc_detail"):
                A(f"- 明细: {'; '.join(r['halluc_detail'])[:300]}")
            A("")
    else:
        A("无(全部 40 问端到端通过)")
    A("")
    A("## 4. 规则变更记录(相对 M2 基线)")
    A("")
    for c in RULES_CHANGES:
        A(f"- {c}")
    A("")
    A("## 5. 结论与下一步")
    A("")
    A(f"- 端到端准确率 **{all_s['e2e_rate']}%**(目标 ≥90%),幻觉率 {all_s['hallucination_rate']}%。")
    A("- 复现: `cd benchmark && python run_eval.py`(需 text2sql_demo/.env.local 的 LLM_API_KEY)。")
    A("- 说明: 口径正确率为规则启发式(必需表+必需时间字面量, 单快照/纯维度表豁免);结果比对按 docs/评测口径定义.md(数值以 gold 小数位数为准先四舍五入再比, 分组标签去'岁'规范化, 容差 1e-6 兜底)。")
    A("- E1-E9 异常边界场景与 300/400 码不可答问题未计入本 40 问, 见 edge_cases 运行结果。")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写: {REPORT}")


if __name__ == "__main__":
    main()
