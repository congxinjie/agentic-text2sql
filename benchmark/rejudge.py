# -*- coding: utf-8 -*-
"""判定器改动后的固化重判脚本(方法论: 判定器改动只重判旧输出留档, 再跑新 run)。

用法:
    python benchmark/rejudge.py runs/run_<a>.json [runs/run_<b>.json ...]

输出(每个输入):
    benchmark/runs/rejudged_<原文件名>.json
      - runtime: 跑时中间版判定(原 run JSON 里已存的 result_ok/caliber_ok 等)
      - final:   当前 run_eval.judge() 重判
      - rescued: 跑时 FAIL → 最终 PASS 的题目清单
      - metrics.runtime / metrics.final
"""
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_eval as E  # noqa: E402


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def metrics(recs):
    n = len(recs)
    ex = sum(1 for r in recs if r["exec_ok"])
    rr = sum(1 for r in recs if r["exec_ok"] and r["result_ok"])
    cr = sum(1 for r in recs if r["exec_ok"] and r["caliber_ok"])
    e2 = sum(1 for r in recs if r["exec_ok"] and r["result_ok"] and r["caliber_ok"])
    hal = sum(1 for r in recs if r["hallucination"])
    return {"n": n, "exec": pct(ex, n), "res": pct(rr, n),
            "cal": pct(cr, n), "e2e": pct(e2, n), "hal": pct(hal, n)}


def ok(r):
    return r["exec_ok"] and r["result_ok"] and r["caliber_ok"] and not r["hallucination"]


def main():
    meta = E.get_table_meta(str(E.DB))
    backfill = "--backfill" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--backfill"]
    for arg in args:
        src = Path(arg)
        data = json.loads(src.read_text(encoding="utf-8"))
        records = []
        for rec in data["records"]:
            runtime = {"exec_ok": bool(rec.get("exec_ok")),
                       "result_ok": bool(rec.get("result_ok")),
                       "caliber_ok": bool(rec.get("caliber_ok")),
                       "hallucination": bool(rec.get("hallucination"))}
            rec2 = dict(rec)
            E.judge(rec2, meta)
            final = {"exec_ok": bool(rec2.get("exec_ok")),
                     "result_ok": bool(rec2.get("result_ok")),
                     "caliber_ok": bool(rec2.get("caliber_ok")),
                     "hallucination": bool(rec2.get("hallucination"))}
            records.append({
                "id": rec["id"], "difficulty": rec["difficulty"],
                "question": rec["question"][:50],
                "runtime": runtime, "final": final,
                "rescued": (not ok(runtime)) and ok(final),
            })
            if backfill:
                # 回填 run JSON 的逐题判定为最终判定(保留 SQL/rows/headers 等原始捕获)
                rec["result_ok"] = final["result_ok"]
                rec["caliber_ok"] = final["caliber_ok"]
                rec["hallucination"] = final["hallucination"]
                rec["judged_with"] = "R6/R7 final (rejudged)"
        rt = [r["runtime"] for r in records]
        fn = [r["final"] for r in records]
        rescued = [r["id"] for r in records if r["rescued"]]
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
            "note": "跑时中间版判定(runtime) vs 当前最终判定器(final); 判定器改动只重判不重跑 LLM",
            "metrics": {"runtime": metrics(rt), "final": metrics(fn)},
            "rescued": rescued,
            "records": records,
        }
        dst = src.parent / f"rejudged_{src.name}"
        dst.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{src.name}")
        print(f"  runtime: {metrics(rt)}")
        print(f"  final:   {metrics(fn)}")
        print(f"  rescued: {rescued}")
        print(f"  -> {dst.name}")
        if backfill:
            data["note"] = "跑时中间版判定已废弃, 逐题 result_ok/caliber_ok/hallucination 已回填为 R6/R7 最终判定; 演进对照见 rejudged_*.json"
            data["metrics"] = {
                "all": metrics([r["final"] for r in records]),
                "by_difficulty": {
                    d: metrics([r["final"] for r in records if r["difficulty"] == d])
                    for d in ("simple", "medium", "complex")
                },
            }
            src.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  -> backfilled {src.name}")


if __name__ == "__main__":
    main()
