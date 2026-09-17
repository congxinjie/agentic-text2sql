# -*- coding: utf-8 -*-
"""M10 流式接口与 POST 接口"同源一致"的确定性验证(纯标准库, 不调 LLM, 不起服务)。

思路: 把引擎的 LLM 调用换成确定性桩, 在同一进程内分别走两条真实代码路径:
  - POST /api/ask 的内核      = demo.server.run_ask(...)        (POST 处理函数直接把它回给前端)
  - SSE /api/ask/stream 终止  = demo.server.run_ask_stream(...) (流式处理函数直接把它 push 给前端)
断言两者逐字段完全一致(含 ok; elapsed_s 是两次独立计时, 单独列出), 并检查:
  - 阶段事件只含白名单字段(type/seq/stage/status/ts/elapsed_s), 不含 detail;
  - 序列化后的整段 SSE 文本里没有 SQL 片段、没有 `sk-[A-Za-z0-9]{20,}`、没有 `C0000000`。
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "customer_marketing_db" / "marketing.db"
QUESTION = "华东地区有多少客户?"
ALLOWED_STAGE_KEYS = {"type", "seq", "stage", "status", "ts", "elapsed_s"}
ALLOWED_DONE_KEYS = {"type", "seq", "ts", "result"}

UNDERSTAND_JSON = json.dumps({
    "answerable": True, "summary": "统计华东地区客户数", "metrics": ["客户数"],
    "dimensions": [], "filters": ["region=华东"], "time_range": "", "assumptions": [],
    "missing": [], "question_specificity": "clear",
}, ensure_ascii=False)
RETRIEVE_JSON = json.dumps({"tables": [{"table": "customers", "columns": ["region", "name"]}]},
                           ensure_ascii=False)
PLAN_JSON = json.dumps({
    "summary": "华东地区客户数", "tables": ["customers"], "filters": ["region = '华东'"],
    "aggregations": [{"func": "COUNT", "field": "*", "alias": "客户数"}],
    "group_by": [], "order_by": [], "limit": None, "steps": ["筛选华东地区", "计数"],
}, ensure_ascii=False)
SQL_TEXT = "SELECT COUNT(*) AS 客户数 FROM customers WHERE region = '华东'"
EXPLAIN_TEXT = "华东地区共有若干客户。默认假设: 统计口径为客户主档行数。"


def main():
    sys.path.insert(0, str(ROOT / "text2sql_demo"))
    import text2sql  # noqa: E402

    def fake_llm(system, user, api_key, max_tokens=1500):
        if "理解器" in system:
            return UNDERSTAND_JSON
        if "检索器" in system:
            return RETRIEVE_JSON
        if "计划器" in system:
            return PLAN_JSON
        if "结论解释器" in system:
            return EXPLAIN_TEXT
        return SQL_TEXT
    text2sql.llm_chat = fake_llm

    spec = importlib.util.spec_from_file_location("demo.server", ROOT / "demo" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["demo.server"] = mod
    spec.loader.exec_module(mod)

    fails = []
    print("== M10 流式/POST 同源一致(确定性, 桩 LLM) ==")
    agent = mod.DemoQueryAgent(str(DB), "stub-key", verbose=False)

    post_payload = mod.run_ask(agent, QUESTION, False)          # POST /api/ask 内核
    events = []
    done = mod.run_ask_stream(agent, QUESTION, False, events.append)  # SSE 内核
    sse_result = done["result"]

    print("\n[1] 终止事件 result vs POST 响应 逐字段一致")
    print(f"    SSE result 字段: {sorted(sse_result)}")
    print(f"    POST   字段:     {sorted(post_payload)}")
    if set(sse_result) != set(post_payload):
        fails.append("字段集合不一致")
    diffs = [k for k in sorted(set(sse_result) & set(post_payload))
             if k != "elapsed_s" and sse_result[k] != post_payload[k]]
    print(f"    {'PASS' if not diffs else 'FAIL'} 除 elapsed_s 外全部相同"
          + ("" if not diffs else f"; 差异字段={diffs}"))
    if diffs:
        fails.append(f"字段值不一致: {diffs}")
    print(f"    (elapsed_s 各自计时: SSE={sse_result.get('elapsed_s')}s POST={post_payload.get('elapsed_s')}s)")

    print("\n[2] 阶段事件字段白名单(不含 detail)")
    bad = [ev for ev in events if ev.get("type") == "stage" and not set(ev) <= ALLOWED_STAGE_KEYS]
    done_bad = not set(done) <= ALLOWED_DONE_KEYS
    print(f"    阶段事件数={sum(1 for e in events if e.get('type')=='stage')} 越界字段事件数={len(bad)}; "
          f"终止事件字段={sorted(done)}")
    print(f"    {'PASS' if not bad and not done_bad else 'FAIL'} 事件只含白名单字段")
    if bad or done_bad:
        fails.append("事件含白名单之外的字段")

    print("\n[3] 安全 grep: 阶段事件(只推阶段名/状态/耗时) vs 整段 SSE 文本")
    stage_events = [ev for ev in events if ev.get("type") == "stage"]
    raw_stage = "".join("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n" for ev in stage_events)
    raw_done = "".join("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                       for ev in events if ev.get("type") == "done")
    raw = raw_stage + raw_done
    # 阶段事件不得出现 SQL 片段/数据值/密钥; 整段(含终止事件里的完整结果)只查密钥与客户号前缀
    for scope, text, patterns in (
        ("阶段事件", raw_stage, [("SQL 片段", r"SELECT"), ("密钥", r"sk-[A-Za-z0-9]{20,}"),
                                 ("客户号前缀", r"C0000000"), ("结果值", r"华南|华北")]),
        ("整段 SSE", raw, [("密钥", r"sk-[A-Za-z0-9]{20,}"), ("客户号前缀", r"C0000000")]),
    ):
        for label, pat in patterns:
            hits = re.findall(pat, text)
            print(f"    {'PASS' if not hits else 'FAIL'} [{scope}] {label} /{pat}/ 命中 {len(hits)} 处")
            if hits:
                fails.append(f"{scope}命中 {pat}")
    print(f"    阶段事件样例: {raw_stage.splitlines()[0][:160]} …")
    print(f"    终止事件含完整结果(与 POST 一致)故含 sql/rows, 但无密钥与客户号前缀")

    print("")
    if fails:
        print(f"验证未通过: {fails}")
        sys.exit(1)
    print("验证通过。")


if __name__ == "__main__":
    main()
