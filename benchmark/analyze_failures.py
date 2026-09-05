# -*- coding: utf-8 -*-
"""剖析 chat arm 失败题: agent SQL vs gold_sql, 输出差异供人工分类。

用法: python benchmark/analyze_failures.py <run1.json> [run2.json ...]
输出: benchmark/runs/failure_analysis_<ts>.md + 控制台摘要
"""
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BENCH = HERE / "benchmark.json"
RUNS = HERE / "runs"


def main():
    run_files = [Path(a) for a in sys.argv[1:]]
    if not run_files:
        run_files = sorted(RUNS.glob("run_2026*.json"))[-2:]
    bench = {q["id"]: q for q in json.loads(BENCH.read_text(encoding="utf-8"))["items"]}

    lines = []
    seen = set()
    for rf in run_files:
        data = json.loads(rf.read_text(encoding="utf-8"))
        for r in data["records"]:
            if r["id"] in seen:
                continue
            if r.get("model") != "deepseek-chat":
                continue
            if r.get("exec_ok") and r.get("result_ok"):
                continue
            seen.add(r["id"])
            q = bench[r["id"]]
            lines.append(f"\n### {r['id']} {r['difficulty']} — {q['question']}")
            if not r.get("exec_ok"):
                lines.append(f"- 类型: 执行失败")
                lines.append(f"- 错误: {r.get('error','')[:300]}")
                lines.append(f"- agent_sql: `{r.get('sql','')[:400]}`")
            else:
                lines.append(f"- 类型: 结果不一致")
                lines.append(f"- agent_sql: `{r.get('sql','')}`")
                lines.append(f"- gold_sql: `{q['gold_sql']}`")
                g = q["gold_answer"]
                lines.append(f"- gold_rows(前5): {g['rows'][:5]}")
                lines.append(f"- agent_rows(前5): {r.get('rows',[])[:5]}")
            lines.append(f"- 口径: {q['caliber_notes'][:160]}")

    out = RUNS / f"failure_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"失败题数: {len(seen)}")
    print("\n".join(lines))
    print(f"\n输出: {out}")


if __name__ == "__main__":
    main()
