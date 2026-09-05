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

# 口径启发式: 这些表是单快照/纯维度表, 当其全为必查表时豁免 data_dt 字面量检查
TIME_EXEMPT_TABLES = {"ads_cust_info_d", "dim_public", "dim_branch", "dim_product"}
# 分级模型策略(M3): 复杂题用更稳的 deepseek-chat; 简单/中等保持 flash
MODEL_BY_DIFF = {"simple": "deepseek-v4-flash", "medium": "deepseek-v4-flash", "complex": "deepseek-chat"}
RULES_CHANGES = [
    "R1 口径启发式: expect_tables 全属于单快照/纯维度表(ads_cust_info_d/dim_public/dim_branch/dim_product)时, 豁免 data_dt 字面量检查。",
    "R2 结果比对: 数值以 gold 小数位数为准先四舍五入再比(1e-6 容差兜底); 分组标签规范化(去'岁'/空白)。",
    "R3 分级模型策略: 复杂题用 deepseek-chat, 简单/中等用 deepseek-v4-flash(依据 M3 空响应实验; 可用环境变量 LLM_MODEL 整体覆盖)。",
    "R4 幻觉检查修正: CTE 识别由 'WITH 后第一个名' 改为 '任意 AS(' 模式, 避免多 CTE(WITH s AS..., e AS...)的 e/f 被误判为表幻觉。",
]

# 企业口径语义(隔离在评测脚本内, 不进入 text2sql.py)
ENTERPRISE_BIZ = """券商客户营销库(2026-Q1 事实 + 客户主档单快照)。
【表】dim_branch 营业部(org_id/org_name/up_org_id/up_org_name); dim_public 编码字典(code,code_type_id,describe); dim_product 产品(prdt_id/prdt_name/prdt_type_id 二级/prdt_type_name/up_prdt_type_id 一级/up_prdt_type_name); ads_cust_info_d 客户主档(pty_id/org_id/cust_age/prov_name/city_name/cust_lvl_cd/cust_status/cust_type/gender_cd/edu_cd/prof_cd/name 已脱敏); dwd_cust_hold_d 每日持仓(pty_id/prdt_id/sys_source/ccy/hold_cnt 份额/mkt_val 市值); dwd_cust_tran_d 每日买卖(buy_cnt 次数/buy_mnt 数量/buy_amt 金额/buy_rake 佣金/buy_fare 费用, sell_* 同); dws_cust_aset_d 每日资产(nm_tot_aset 普通总资产/nm_bal 普通现金/fc_pur_aset 信用净资产/fc_bal 信用现金); dws_cust_fin_d 每日资金流(cash_in/cash_out/tran_in/tran_out/assign_in/assign_out)。
【时间口径】客户主档只有 data_dt='20260531' 一个快照; 持仓与资产表 data_dt 覆盖 20260101~20260331(90 天); 交易与资金表覆盖 20260105~20260331(56 个交易日)。2026 年一季度=20260101~20260331。
【编码解码】客户表码字段 join dim_public: cust_lvl_cd→code_type_id='100'(紫金理财钻石卡/白金卡/金卡/银卡客户等); cust_status→'200'; gender_cd→'500'(5000002 男/5000003 女); edu_cd→'600'(6000003 硕士/6000004 学士等); prof_cd→'700'。dim_public 的 code_type_id='300' 证件、'400' 风险等级在库中无任何表引用, 相关业务问题按不可答处理。
【产品】一级分类用 up_prdt_type_id(PT040000 股票/PT030000 债券/PT050000 开放式基金/PT070000 理财/PT090000 恒生多金融/PT020000 权证/PT060000 衍生品/PT080000 回购/PT100000 私募/PT110000 贵金属/PT990000 现金类), 二级分类用 prdt_type_id/prdt_type_name(如 科创板/A股/沪港通)。注意 up_prdt_type_id 与 prdt_type_id 存在同名多义(如 PT090000 同时叫恒生多金融产品/OTC产品), 归类以 ID 为准。产品名(prdt_name)用于按名称过滤, 如 比亚迪/招商银行/中国平安。
【账户与币种】sys_source: nm=普通账户, fc=信用账户; ccy: 0 人民币/1 美元/2 港币。资产表无 sys_source, nm/fc 为并列字段。
【关键口径(队伍约定)】总资产=nm_tot_aset+fc_pur_aset; 现金资产=nm_bal+fc_bal; 交易额=buy_amt+sell_amt; 交易笔数=buy_cnt+sell_cnt; 交易天数=COUNT(DISTINCT data_dt); 日均=区间合计/区间天数(资产 90 天); 交易量未注明单位一律按金额; 盈亏=(期末总资产-期初总资产)+(资金流出-资金流入), 其中资金流入=cash_in+tran_in+assign_in, 资金流出=cash_out+tran_out+assign_out。
【易错提醒】客户表只有 20260531 一个日期; 营业部与客户姓名已脱敏; 过滤日期用 data_dt 的 YYYYMMDD 字符串比较。"""


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
    return str(v).replace("岁", "").replace(" ", "").strip()


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


def rows_equal(gold_rows, got_rows, tol):
    if len(got_rows) != len(gold_rows):
        return False
    key = lambda r: json.dumps(r, ensure_ascii=False, default=str)
    g1 = sorted(gold_rows, key=key)
    g2 = sorted(got_rows, key=key)
    for r1, r2 in zip(g1, g2):
        if len(r1) != len(r2):
            return False
        for c1, c2 in zip(r1, r2):
            if not cells_equal(c1, c2, tol):
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
        rec["result_ok"] = rows_equal(gold["rows"], rec["rows"], gold.get("tolerance", 1e-6))

        # 口径(规则启发式): 必需表 + 必需时间字面量出现(单快照/纯维度表豁免时间检查)
        ok_tables = all(re.search(rf"\b{t}\b", sql, re.I) for t in rec.get("expect_tables", []))
        exempt = bool(rec.get("expect_tables")) and set(rec["expect_tables"]) <= TIME_EXEMPT_TABLES
        if exempt:
            ok_time = True
        else:
            ok_time = all((t in sql) or (dashes(t) in sql) for t in rec.get("expect_time", []))
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


def main():
    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    items = bench["items"]
    meta = get_table_meta(str(DB))
    api_key = load_api_key()
    forced_model = os.environ.get("LLM_MODEL", "").strip()
    agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ)

    print(f"评测开始: {len(items)} 问, 分级模型策略(simple/medium=flash, complex=chat)"
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
            rec["error"] = ans.error or ""
            rec["answerable"] = ans.answerable
            rec["needs_clarification"] = list(ans.needs_clarification)
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
        p50 = times[int(len(times) * 0.5)] if times else 0
        p90 = times[min(int(len(times) * 0.9), len(times) - 1)] if times else 0
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

    # ---- 基线报告 ----
    lines = []
    A = lines.append
    A("# 评测报告-基线(M2)")
    A("")
    A(f"> 生成时间:{datetime.now().isoformat(timespec='seconds')}")
    A(f"> 数据库:`{DB}`(enterprise.db, 91MB, 只读)")
    A("> 引擎:text2sql_demo/text2sql.py 9 阶段 QueryAgent,biz_context=企业口径语义")
    A("> 模型:引擎默认(deepseek-v4-flash 可经 LLM_MODEL 覆盖);全部数字来自真实运行,禁止估算")
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
