# -*- coding: utf-8 -*-
"""UI 全流程自测: 走 app_core.answer(与 Streamlit UI 同一处理路径), 覆盖
简单 → 中等 → 复杂 → 库外拒绝 → 追问 五类, 记录真实输出。
输出: benchmark/runs/ui_smoke_<ts>.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))

import app_core  # noqa: E402

QUESTIONS = [
    ("simple", "客户主档中共有多少客户?", "ok"),
    ("simple", "2026-03-31 全库客户总资产是多少?", "ok"),
    ("medium", "各客户等级分别有多少客户?", "ok"),
    ("complex", "2026 年一季度日均总资产超过 30 万、且股票交易额超过 10 万的客户有多少人?", "ok"),
    ("reject", "今天天气怎么样?适合买基金吗?", "reject"),
    ("clarify", "帮我分析一下?", "clarify"),
]


def main():
    agent = app_core.build_agent()
    out = []
    for diff, q, expect in QUESTIONS:
        r = app_core.answer(agent, q)
        head = (r["text"] or "")[:120]
        row = {"difficulty": diff, "question": q, "expect": expect, "kind": r["kind"],
               "text": r["text"][:300], "elapsed_s": r["payload"].get("elapsed_s"),
               "rows_n": len(r["payload"].get("rows") or []) if r["kind"] == "ok" else 0,
               "sql": (r["payload"].get("sql") or "")[:300]}
        out.append(row)
        ok = "PASS" if r["kind"] == expect else "MISMATCH"
        print(f"[{ok}] {diff:<8} expect={expect:<7} got={r['kind']:<7} "
              f"t={row['elapsed_s']}s rows={row['rows_n']} | {q[:34]}", flush=True)
        if ok == "MISMATCH":
            print("   text:", head, flush=True)
    dst = ROOT / "benchmark" / "runs" / f"ui_smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    dst.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "results": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("WROTE", dst)


if __name__ == "__main__":
    main()
