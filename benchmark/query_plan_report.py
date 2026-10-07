# -*- coding: utf-8 -*-
"""查询优化报告: 对最新全量评测的 Agent SQL + 46 道金标 SQL 跑 EXPLAIN QUERY PLAN,
统计全表扫描 / 索引命中 / 临时 B 树, 产出 docs/查询优化报告.md。
用法: python benchmark/query_plan_report.py [runA.json runB.json]  零第三方依赖。
"""
import glob, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))
import sql_plan as P  # noqa: E402

DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
OUT = ROOT / "docs" / "查询优化报告.md"


def stats(rows):
    n = len(rows)
    used = sum(1 for r in rows if r["plan"].get("used_index"))
    full = sum(1 for r in rows if r["plan"].get("n_full_scan"))
    temp = sum(1 for r in rows if r["plan"].get("temp_btree"))
    return {"n": n, "used": used, "full": full, "temp": temp}


def collect_run(paths):
    rows = []
    for f in paths:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        for r in data.get("records", []):
            sql = (r.get("sql") or "").strip()
            if not sql:
                continue
            try:
                plan = P.analyze(str(DB), sql)
            except Exception as e:
                plan = {"error": str(e)[:80], "n_full_scan": 0, "used_index": False}
            rows.append({"id": r.get("id"), "difficulty": r.get("difficulty"), "sql": sql, "plan": plan})
    return rows


def collect_gold():
    items = json.loads((HERE / "benchmark.json").read_text(encoding="utf-8"))["items"]
    rows = []
    for it in items:
        plan = P.analyze(str(DB), it["gold_sql"])
        rows.append({"id": it["id"], "difficulty": it["difficulty"], "sql": it["gold_sql"], "plan": plan})
    return rows


def block(name, rows):
    s = stats(rows)
    n, used, full, temp = s["n"], s["used"], s["full"], s["temp"]
    return [
        f"## {name}({n} 条)", "",
        "| 指标 | 值 |", "|---|---|",
        f"| 命中索引(索引搜索/索引扫描) | {used}/{n} |",
        f"| 含全表扫描 | {full}/{n} |",
        f"| 含临时 B 树 | {temp}/{n} |",
        "",
    ]


def main():
    args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if not args:
        cands = sorted(glob.glob(str(HERE / "runs" / "run_2026*.json")))
        args = cands[-2:]
    agent = collect_run(args)
    gold = collect_gold()
    src = ", ".join(Path(a).name for a in args)
    lines = ["# 查询优化报告(EXPLAIN QUERY PLAN)", "",
             f"> 数据库: `{DB.name}`(只读); Agent SQL 来源: {src}",
             "> 口径: EXPLAIN QUERY PLAN 不执行数据; SCAN 表=全表扫描; SEARCH/USING INDEX/COVERING INDEX 视为索引命中。", ""]
    lines += block("Agent 生成的 SQL", agent)
    lines += block("金标 SQL", gold)
    full_rows = [r for r in agent if r["plan"].get("n_full_scan")]
    lines += ["## 含全表扫描的题(Agent)", ""]
    if not full_rows:
        lines.append("无。")
    else:
        lines += ["| ID | 难度 | 计划 |", "|---|---|---|"]
        for r in full_rows:
            rid, rdiff = r["id"], r["difficulty"]
            det = "; ".join(r["plan"].get("full_scans") or [])[:80].replace("|", "/")
            lines.append(f"| {rid} | {rdiff} | {det} |")
    lines += ["", "## 索引清单", "", "| 表 | 索引 |", "|---|---|"]
    for t, names in P.list_indexes(str(DB)).items():
        if names:
            jn = ", ".join(names)
            lines.append(f"| {t} | {jn} |")
    lines.append("")
    lines += ["", "## 优化建议", "",
              "- 当前 `build_db.py` 只建索引: 6 张表的 pty_id + dim_product.prdt_name/prdt_type_name。",
              "- 报告显示大量题目在 data_dt / cust_age / 维度编码列上全表扫描;建议按实际过滤频次补索引(data_dt、(data_dt, pty_id) 等), 再用本报告复测。",
              "- 只读取数场景索引收益明显;写入/存储成本需另行评估。"]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT)
    print("agent:", stats(agent), "gold:", stats(gold))


if __name__ == "__main__":
    main()
