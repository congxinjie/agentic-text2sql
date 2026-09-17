# -*- coding: utf-8 -*-
"""M10 引擎阶段回调回归自测(纯标准库, 不调 LLM)。

跑法:
    cd benchmark && python engine_callback_parity.py                 # 自比: 回调开/关, 输出逐字节一致
    python engine_callback_parity.py --old <旧版 text2sql.py 路径>   # 与旧版(无回调)逐字节对比

三件事:
1. 同一份引擎、同一桩 LLM 响应下, "不传 on_stage" 与 "传 on_stage"(构造参数 / run 参数)的
   stdout + 执行轨迹 + 答案字段必须完全一致(阶段耗时数字归一化后) —— 证明回调是纯旁路;
2. 传 on_stage 时事件覆盖 >=3 个阶段, 且每个阶段都有 RUNNING 与结束状态;
3. 回调抛异常时, 答案与不传回调时一致 —— 展示层异常不得影响取数。
"""
import argparse
import contextlib
import importlib.util
import io
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_ENGINE = ROOT / "text2sql_demo" / "text2sql.py"
DB = ROOT / "customer_marketing_db" / "marketing.db"
QUESTION = "华东地区有多少客户?"
UNSET = object()  # 哨兵: 完全不传 on_stage(与旧版调用形态一致)

# ---- 桩 LLM 响应(按 system prompt 关键字分发, 保证确定性) ----
UNDERSTAND_JSON = json.dumps({
    "answerable": True,
    "summary": "统计华东地区客户数",
    "metrics": ["客户数"],
    "dimensions": [],
    "filters": ["region=华东"],
    "time_range": "",
    "assumptions": [],
    "missing": [],
    "question_specificity": "clear",
}, ensure_ascii=False)

RETRIEVE_JSON = json.dumps({
    "tables": [{"table": "customers", "columns": ["region", "name"]}],
}, ensure_ascii=False)

PLAN_JSON = json.dumps({
    "summary": "华东地区客户数",
    "tables": ["customers"],
    "filters": ["region = '华东'"],
    "aggregations": [{"func": "COUNT", "field": "*", "alias": "客户数"}],
    "group_by": [],
    "order_by": [],
    "limit": None,
    "steps": ["筛选华东地区", "计数"],
}, ensure_ascii=False)

SQL_TEXT = "SELECT COUNT(*) AS 客户数 FROM customers WHERE region = '华东'"
EXPLAIN_TEXT = "华东地区共有若干客户。默认假设: 统计口径为客户主档行数。"


def load_engine(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_stub(mod, hits: dict):
    def fake_llm(system, user, api_key, max_tokens=1500):
        if "理解器" in system:
            hits["understand"] += 1
            return UNDERSTAND_JSON
        if "检索器" in system:
            hits["retrieve"] += 1
            return RETRIEVE_JSON
        if "计划器" in system:
            hits["plan"] += 1
            return PLAN_JSON
        if "结论解释器" in system:
            hits["explain"] += 1
            return EXPLAIN_TEXT
        hits["sql"] += 1
        return SQL_TEXT
    mod.llm_chat = fake_llm


def norm_stdout(text: str) -> str:
    """阶段耗时数字归一化: 两次运行的秒数必然不同, 不算行为差异。"""
    return re.sub(r"\(\d+\.\d+s\)", "(Xs)", text)


def answer_key(ans) -> dict:
    return {
        "answerable": ans.answerable,
        "reject_reason": ans.reject_reason,
        "needs_clarification": ans.needs_clarification,
        "sql": ans.sql,
        "headers": ans.headers,
        "rows": ans.rows,
        "truncated": ans.truncated,
        "explanation": ans.explanation,
        "error": ans.error,
    }


def trace_key(ans) -> list:
    return [(e.stage, e.status, e.detail) for e in ans.trace.entries]


def snapshot(mod, cb=UNSET, via_run_param=False):
    """跑一次桩问答。cb=UNSET 表示完全不传 on_stage(旧版调用形态)。

    返回 (可对比快照, 事件序列, 事件到达时刻, 总耗时, 桩调用次数)。
    """
    hits = {"understand": 0, "retrieve": 0, "plan": 0, "sql": 0, "explain": 0}
    make_stub(mod, hits)
    events, timeline = [], []

    def stage_cb(stage, status, detail=""):
        events.append((stage, status))
        timeline.append(round(time.time() - t0, 3))
        if not via_run_param and cb is not UNSET:
            cb(stage, status, detail)  # 构造参数回调: 交给调用方(可能故意抛错)

    api_key = "stub-key"
    if via_run_param or cb is UNSET:
        agent = mod.QueryAgent(str(DB), api_key, verbose=True)
    else:
        agent = mod.QueryAgent(str(DB), api_key, verbose=True, on_stage=stage_cb)

    buf = io.StringIO()
    t0 = time.time()
    with contextlib.redirect_stdout(buf):
        if via_run_param:
            ans = agent.run(QUESTION, clarify=False, on_stage=stage_cb)
        else:
            ans = agent.run(QUESTION, clarify=False)
    total = round(time.time() - t0, 3)
    snap = {"stdout": norm_stdout(buf.getvalue()), "trace": trace_key(ans), "answer": answer_key(ans)}
    return snap, events, timeline, total, hits


def main():
    ap = argparse.ArgumentParser(description="M10 阶段回调回归自测(不调 LLM)")
    ap.add_argument("--old", default=None, help="旧版 text2sql.py 路径(对照用)")
    args = ap.parse_args()

    fails = []
    mod_new = load_engine(DEFAULT_ENGINE, "engine_new")

    print("== M10 阶段回调回归自测 ==")
    print(f"引擎: {DEFAULT_ENGINE}")
    print(f"桩问题: {QUESTION} (库: {DB.name}, 真执行 SQL, 不调 LLM)")

    base, _, _, _, base_hits = snapshot(mod_new)                     # 完全不传 on_stage
    with_cb, ev_cb, tl_cb, total_cb, hits_cb = snapshot(mod_new, cb=lambda *a: None)
    with_run, ev_run, _, _, hits_run = snapshot(mod_new, via_run_param=True)

    print("\n[1] 回调开/关输出一致(阶段耗时数字归一化后)")
    print(f"    不传回调:     注册阶段数={len(base['trace'])} 答案行数={len(base['answer']['rows'] or [])}")
    print(f"    构造参数回调: 注册阶段数={len(with_cb['trace'])} 事件数={len(ev_cb)}")
    print(f"    run 参数回调: 注册阶段数={len(with_run['trace'])} 事件数={len(ev_run)}")
    for label, snap in (("构造参数回调", with_cb), ("run 参数回调", with_run)):
        diffs = [k for k in ("stdout", "trace", "answer") if snap[k] != base[k]]
        print(f"    {'PASS' if not diffs else 'FAIL'} {label}: stdout/执行轨迹/答案 与不传回调逐字节一致")
        for k in diffs:
            print(f"       差异字段 {k}:")
            print(f"         有回调: {snap[k]!r}")
            print(f"         无回调: {base[k]!r}")
            fails.append(f"{label} 的 {k} 与不传回调不一致")
    print(f"    桩 LLM 调用次数: 不传={base_hits} 构造回调={hits_cb} run回调={hits_run}")
    if not (base_hits == hits_cb == hits_run):
        fails.append(f"桩 LLM 调用次数不同: {base_hits} / {hits_cb} / {hits_run}")

    print("\n[2] 事件覆盖与边跑边推")
    print(f"    事件序列: {ev_cb}")
    print(f"    各事件到达时刻(相对 t0 秒): {tl_cb}")
    print(f"    总耗时 {total_cb}s; 首条事件 +{tl_cb[0] if tl_cb else None}s")
    n_running = sum(1 for _, st in ev_cb if st == "RUNNING")
    n_done = sum(1 for _, st in ev_cb if st in ("OK", "WARN", "FAIL", "SKIP"))
    ok2 = len(ev_cb) >= 3 and n_running >= 3 and n_done >= 3
    print(f"    {'PASS' if ok2 else 'FAIL'} 事件数 {len(ev_cb)} >= 3; RUNNING 阶段数 {n_running} >= 3; "
          f"结束状态事件数 {n_done} >= 3; 首条事件 +{tl_cb[0] if tl_cb else None}s 早于总耗时 {total_cb}s")
    if not ok2:
        fails.append("阶段事件覆盖不足")

    print("\n[3] 回调抛异常不影响取数")
    def boom(stage, status, detail=""):
        raise RuntimeError("回调故意抛错")

    with_boom, _, _, _, _ = snapshot(mod_new, cb=boom)
    diffs3 = [k for k in ("stdout", "trace", "answer") if with_boom[k] != base[k]]
    print(f"    {'PASS' if not diffs3 else 'FAIL'} 回调抛异常时 stdout/执行轨迹/答案 与不传回调一致")
    if diffs3:
        fails.append("回调异常影响了引擎输出")

    if args.old:
        print(f"\n[4] 与旧版逐字节对比: {args.old}")
        mod_old = load_engine(Path(args.old), "engine_old")
        old_snap, _, _, _, _ = snapshot(mod_old)
        diffs4 = [k for k in ("stdout", "trace", "answer") if old_snap[k] != base[k]]
        print(f"    {'PASS' if not diffs4 else 'FAIL'} 旧版(无回调)与新版(不传回调) stdout/执行轨迹/答案逐字节一致")
        for k in diffs4:
            print(f"       差异字段 {k}:")
            print(f"         旧版: {old_snap[k]!r}")
            print(f"         新版: {base[k]!r}")
            fails.append(f"与旧版 {k} 不一致")

    print("")
    if fails:
        print(f"自测未通过: {fails}")
        sys.exit(1)
    print("自测全部通过。")


if __name__ == "__main__":
    main()
