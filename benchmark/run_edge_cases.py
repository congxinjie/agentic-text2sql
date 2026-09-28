# -*- coding: utf-8 -*-
"""E1-E9 边界用例 runner。

method=real 走真实 LLM; method=unit 走确定性故障注入(不消耗 LLM)。
输出: benchmark/runs/edge_cases_run_<ts>.json
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT2SQL_DIR = ROOT / "text2sql_demo"
DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
EDGE = HERE / "edge_cases.json"
RUNS = HERE / "runs"

sys.path.insert(0, str(TEXT2SQL_DIR))
sys.path.insert(0, str(HERE))
import text2sql  # noqa: E402
from text2sql import QueryAgent, load_api_key, run_query  # noqa: E402
from run_eval import ENTERPRISE_BIZ, SENSITIVE_COLUMNS  # noqa: E402


def run_real(case, agent):
    t0 = time.time()
    clarify = case["check"] == "needs_clarification"
    ans = agent.run(case["input"], clarify=clarify)
    el = int((time.time() - t0) * 1000)
    ok, detail = False, ""
    if case["check"] == "exec_ok_and_nonempty":
        ok = bool(ans.sql and not ans.error and ans.rows)
        detail = f"rows={len(ans.rows or [])} err={ans.error[:80]}"
    elif case["check"] == "exec_ok_and_assumption":
        ok = bool(ans.sql and not ans.error)
        note = (ans.explanation or "")
        ok = ok and ("默认" in note or "假设" in note or "20260331" in (ans.sql or ""))
        detail = f"sql={ans.sql[:80]} note_has_assumption={'默认' in note or '假设' in note}"
    elif case["check"] == "needs_clarification":
        ok = bool(ans.needs_clarification)
        detail = f"clarify={ans.needs_clarification}"
    elif case["check"] == "rejected_with_reason":
        ok = (not ans.answerable) and bool(ans.reject_reason) and not ans.sql
        detail = f"reason={ans.reject_reason[:80]}"
    elif case["check"] == "empty_result_explained":
        ok = bool(ans.sql and not ans.error and not ans.rows)
        note = ans.explanation or ""
        ok = ok and bool(note)
        detail = f"rows={len(ans.rows or [])} explain={note[:80]}"
    else:
        ok, detail = False, "未知 check"
    return {"ok": ok, "detail": detail, "elapsed_ms": el, "error": ans.error or ""}


def run_unit(case, api_key):
    if case["check"] == "repair_once_ok":
        agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ,
                           sensitive_columns=SENSITIVE_COLUMNS)
        calls = []
        agent._understand = lambda q: __import__("text2sql").Understanding(
            answerable=True, summary=case["input"], missing_required=[])
        agent._retrieve = lambda u: __import__("text2sql").Retrieval(
            tables=["dws_cust_aset_d"], columns={"dws_cust_aset_d": ["nm_tot_aset"]})
        agent._plan = lambda u, r: {"summary": "x", "tables": ["dws_cust_aset_d"], "filters": [],
                                    "aggregations": [{"func": "SUM", "field": "dws_cust_aset_d.nm_tot_aset",
                                                     "alias": "资产"}], "group_by": [], "order_by": [],
                                    "limit": None, "steps": ["x"]}
        agent._check_plan = lambda u, r, p: (True, [])
        agent._generate_sql = lambda u, r, p, ci: "SELECT 1"
        agent._validate = lambda s: s
        agent._repair_sql = lambda sql, e, u, plan, stage_title="执行": "SELECT 2"
        agent._check_result = lambda h, rows, tr, plan=None: ([], [])
        agent._explain = lambda u, sql, h, rows, tr, notes: "ok"

        def fake_execute(sql):
            calls.append(sql)
            if len(calls) == 1:
                raise sqlite3.OperationalError("no such column: xyz")
            return (["c"], [(1,)], False)
        agent._execute = fake_execute
        ans = agent.run(case["input"], clarify=False)
        ok = ans.sql == "SELECT 2" and calls == ["SELECT 1", "SELECT 2"]
        return {"ok": ok, "detail": f"calls={calls} sql={ans.sql}", "elapsed_ms": 0, "error": ans.error or ""}

    if case["check"] == "repair_then_fail":
        agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ,
                           sensitive_columns=SENSITIVE_COLUMNS)
        agent._understand = lambda q: __import__("text2sql").Understanding(
            answerable=True, summary=case["input"], missing_required=[])
        agent._retrieve = lambda u: __import__("text2sql").Retrieval(
            tables=["dws_cust_aset_d"], columns={"dws_cust_aset_d": ["nm_tot_aset"]})
        agent._plan = lambda u, r: {"summary": "x", "tables": ["dws_cust_aset_d"], "filters": [],
                                    "aggregations": [{"func": "SUM", "field": "dws_cust_aset_d.nm_tot_aset",
                                                     "alias": "资产"}], "group_by": [], "order_by": [],
                                    "limit": None, "steps": ["x"]}
        agent._check_plan = lambda u, r, p: (True, [])
        agent._generate_sql = lambda u, r, p, ci: "SELECT 1"
        agent._validate = lambda s: s
        agent._repair_sql = lambda sql, e, u, plan, stage_title="执行": "SELECT 2"
        agent._check_result = lambda h, rows, tr, plan=None: ([], [])
        agent._explain = lambda u, sql, h, rows, tr, notes: "ok"

        def always_fail(sql):
            raise sqlite3.OperationalError("syntax error near x")
        agent._execute = always_fail
        ans = agent.run(case["input"], clarify=False)
        ok = bool(ans.error) and any(e.status == "FAIL" for e in ans.trace.entries)
        return {"ok": ok, "detail": f"error={ans.error[:80]}", "elapsed_ms": 0, "error": ans.error or ""}

    if case["check"] == "truncated":
        headers, rows, truncated = run_query(str(DB), "SELECT * FROM dwd_cust_hold_d")
        ok = truncated and len(rows) == 100
        return {"ok": ok, "detail": f"truncated={truncated} rows={len(rows)}", "elapsed_ms": 0, "error": ""}

    if case["check"] == "llm_fault_handled":
        agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ,
                           sensitive_columns=SENSITIVE_COLUMNS)
        orig = text2sql.llm_chat

        def boom(*a, **k):
            raise RuntimeError("LLM API 连接失败")
        text2sql.llm_chat = boom
        try:
            ans = agent.run(case["input"], clarify=False)
        finally:
            text2sql.llm_chat = orig
        ok = bool(ans.error) and "理解问题阶段失败" in ans.error and any(
            e.status == "FAIL" for e in ans.trace.entries)
        return {"ok": ok, "detail": f"error={ans.error[:80]}", "elapsed_ms": 0, "error": ans.error or ""}

    return {"ok": False, "detail": "未知 unit check", "elapsed_ms": 0, "error": ""}


def main():
    data = json.loads(EDGE.read_text(encoding="utf-8"))
    api_key = load_api_key()
    agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ,
                       sensitive_columns=SENSITIVE_COLUMNS)
    # 边界用例真实 LLM 统一用更稳的 deepseek-chat(避免 flash 空返回干扰 E3/E7 判定)
    text2sql.MODEL = os.environ.get("LLM_MODEL_EDGE", "deepseek-chat").strip()
    results = []
    print("E1-E9 边界用例运行开始", flush=True)
    for case in data["items"]:
        try:
            if case["method"] == "real":
                r = run_real(case, agent)
            else:
                r = run_unit(case, api_key)
        except Exception as ex:
            r = {"ok": False, "detail": f"{type(ex).__name__}: {ex}", "elapsed_ms": 0, "error": str(ex)[:120]}
        r.update({"id": case["id"], "scenario": case["scenario"], "method": case["method"]})
        results.append(r)
        print(f"  {case['id']} {case['scenario']} -> {'PASS' if r['ok'] else 'FAIL'} ({r['detail'][:100]})", flush=True)
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"edge_cases_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    n_pass = sum(1 for r in results if r["ok"])
    print(f"\nE1-E9 通过 {n_pass}/9, 输出: {out}")


if __name__ == "__main__":
    main()
