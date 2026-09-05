# -*- coding: utf-8 -*-
"""中等 14 题 deepseek-chat 对比实验: 与 flash 中等结果(run_20260905_132409)并排比较。

输出: benchmark/runs/exp_medium_chat_<ts>.json
"""
import json
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
FLASH_RUN = RUNS / "run_20260905_132409.json"

sys.path.insert(0, str(TEXT2SQL_DIR))
sys.path.insert(0, str(HERE))
import text2sql  # noqa: E402
from text2sql import QueryAgent, load_api_key  # noqa: E402
from run_eval import ENTERPRISE_BIZ, rows_equal  # noqa: E402


def main():
    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    medium = [q for q in bench["items"] if q["difficulty"] == "medium"]
    flash = {r["id"]: r for r in json.loads(FLASH_RUN.read_text(encoding="utf-8"))["records"]
             if r["difficulty"] == "medium"}

    text2sql.MODEL = "deepseek-chat"
    api_key = load_api_key()
    results = []
    print(f"中等题 chat 对比实验: {len(medium)} 题", flush=True)
    for q in medium:
        rec = {"id": q["id"], "question": q["question"][:40]}
        t0 = time.time()
        try:
            agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ)
            ans = agent.run(q["question"], clarify=False)
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = (ans.sql or "")[:400]
            rec["error"] = ans.error or ""
            rec["exec_ok"] = bool(ans.sql and not ans.error and ans.rows is not None)
            rec["result_ok"] = bool(rec["exec_ok"] and rows_equal(
                q["gold_answer"]["rows"], ans.rows or [], q["gold_answer"].get("tolerance", 1e-6)))
        except Exception as ex:
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = ""
            rec["error"] = f"{type(ex).__name__}: {ex}"
            rec["exec_ok"] = False
            rec["result_ok"] = False
        f = flash.get(q["id"], {})
        rec["flash_exec"] = f.get("exec_ok")
        rec["flash_result"] = f.get("result_ok")
        rec["flash_elapsed_ms"] = f.get("elapsed_ms")
        results.append(rec)
        print(f"{q['id']} flash(exec={rec['flash_exec']},res={rec['flash_result']}) "
              f"chat(exec={int(rec['exec_ok'])},res={int(rec['result_ok'])}) t={rec['elapsed_ms']}ms", flush=True)

    n_f = sum(1 for r in results if r["flash_result"])
    n_c = sum(1 for r in results if r["result_ok"])
    print(f"\n中等 14 题: flash 结果正确 {n_f}/14, chat 结果正确 {n_c}/14")
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"exp_medium_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "flash_run": str(FLASH_RUN), "results": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"输出: {out}")


if __name__ == "__main__":
    main()
