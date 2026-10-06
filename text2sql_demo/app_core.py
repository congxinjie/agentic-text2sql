# -*- coding: utf-8 -*-
"""UI 与测试共用的核心(不含 streamlit): 构造 QueryAgent + 单问处理(含追问上下文合并)。

券商库口径从 benchmark/run_eval.py 同源注入(ENTERPRISE_BIZ + SQL_HINTS),
遵守铁律 2: 券商语义不硬编码进 text2sql.py。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))
sys.path.insert(0, str(ROOT / "benchmark"))

import text2sql  # noqa: E402
from run_eval import ENTERPRISE_BIZ, SQL_HINTS, SENSITIVE_COLUMNS, ENTITIES  # noqa: E402

DB = str(ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db")
MAX_CLARIFY_ROUNDS = 3


def build_agent() -> text2sql.QueryAgent:
    """构造企业库 Agent: 库=enterprise.db(只读), 语义=biz_context + sql_hints 注入。"""
    return text2sql.QueryAgent(DB, text2sql.load_api_key(), verbose=False,
                               biz_context=ENTERPRISE_BIZ, sql_hints=SQL_HINTS,
                               sensitive_columns=SENSITIVE_COLUMNS, entities=ENTITIES)


def answer(agent: text2sql.QueryAgent, question: str, pending: dict | None = None) -> dict:
    """处理一轮提问, 返回 UI 可直接渲染的结构。

    pending: 上一轮追问上下文 {"question": 累计问题, "rounds": 已追问轮数}
    返回: {"kind": "ok"|"clarify"|"reject"|"error", "text": str, "payload": dict}
    """
    if pending:
        q = f"{pending['question']}\n[补充信息] {question}"
        rounds = pending["rounds"] + 1
    else:
        q = question
        rounds = 0
    clarify = rounds < MAX_CLARIFY_ROUNDS
    t0 = time.time()
    ans = agent.run(q, clarify=clarify)
    elapsed = round(time.time() - t0, 1)

    if ans.needs_clarification:
        return {"kind": "clarify",
                "text": "需要补充信息才能继续: " + "、".join(ans.needs_clarification),
                "payload": {"question": q, "rounds": rounds, "elapsed_s": elapsed}}
    if not ans.answerable:
        return {"kind": "reject", "text": f"[拒绝回答] {ans.reject_reason}",
                "payload": {"elapsed_s": elapsed}}
    if ans.error:
        return {"kind": "error", "text": f"[失败] {ans.error}",
                "payload": {"elapsed_s": elapsed}}
    return {"kind": "ok",
            "text": ans.explanation or "(无结论)",
            "payload": {"sql": ans.sql, "headers": ans.headers, "rows": ans.rows,
                        "truncated": ans.truncated, "trace": ans.trace.render(),
                        "entities": list(getattr(ans, "entities", []) or []),
                        "entities_covered": bool(getattr(ans, "entities_covered", False)),
                        "elapsed_s": elapsed}}
