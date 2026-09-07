# -*- coding: utf-8 -*-
"""智能问数 Agent — Streamlit Web UI(COMPETITION.md §10.1 方案 A)。

启动: cd text2sql_demo && streamlit run app.py
CLI 双入口: python text2sql.py(单问/交互)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))

import streamlit as st  # noqa: E402
import app_core  # noqa: E402
import text2sql  # noqa: E402

st.set_page_config(page_title="智能问数 Agent", page_icon="📊", layout="wide")

EXAMPLES = [
    "各客户等级分别有多少客户?",
    "2026 年一季度全库客户买入金额合计是多少?",
    "2026 年一季度各产品一级分类的买入金额分别是多少?",
    "2026-03-31 各客户等级中总资产最高的客户及其资产是多少?",
    "2026 年一季度交易过招商银行相关产品、且 2026-03-31 持有中国平安相关产品的客户有多少人?",
    "今天天气怎么样?适合买基金吗?",
]


@st.cache_resource(show_spinner=False)
def get_agent():
    return app_core.build_agent()


agent = get_agent()

st.title("📊 智能问数 Agent")
st.caption("自然语言 → 只读 SQL → 数据表 + 结论 + 可溯源口径。数据源:券商客户营销库(enterprise.db, 只读)。")

with st.sidebar:
    st.header("运行信息")
    st.write(f"- 模型: `{text2sql.MODEL}`")
    st.write(f"- 数据库: enterprise.db(只读, mode=ro + query_only)")
    st.write(f"- 业务口径: 企业语义 + {len(app_core.SQL_HINTS)} 条 few-shot(同源评测口径)")
    st.write("- 引擎: 9 阶段状态机(理解→检索→计划→检查→SQL→安全校验→执行→结果→解释)")
    if st.button("清空会话"):
        st.session_state.messages = []
        st.session_state.pending = None
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending" not in st.session_state:
    st.session_state.pending = None


def set_example():
    st.session_state.q_input = st.session_state.ex_chips


st.pills("示例问题(点击填入)", EXAMPLES, key="ex_chips", on_change=set_example)

q = st.text_input("你的问题", key="q_input",
                  placeholder="例如: 各客户等级分别有多少客户?")
submitted = st.button("提问", type="primary")

if submitted and q and q.strip():
    with st.spinner("Agent 处理中(理解 → 检索 → 计划 → 检查 → SQL → 执行 → 解释)…"):
        pending = st.session_state.pending
        result = app_core.answer(agent, q.strip(), pending)
        # 追问上下文管理
        if result["kind"] == "clarify":
            st.session_state.pending = result["payload"]
        else:
            st.session_state.pending = None
        st.session_state.messages.append({"role": "user", "content": q.strip()})
        st.session_state.messages.append({"role": "assistant", "content": result["text"],
                                          "kind": result["kind"],
                                          "payload": result.get("payload") or {}})
        st.rerun()

# ---- 渲染对话流 ----
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        kind = m.get("kind")
        if kind == "ok":
            st.markdown(f"**结论**\n\n{m['content']}")
            p = m["payload"]
            st.caption(f"耗时 {p.get('elapsed_s')}s · 仅只读查询 · 口径与默认假设见折叠区")
            if p.get("headers") is not None and p.get("rows"):
                st.dataframe([dict(zip(p["headers"], row)) for row in p["rows"]],
                             use_container_width=True, hide_index=True)
                if p.get("truncated"):
                    st.caption("结果超过 100 行, 仅展示前 100 行。")
            elif p.get("headers") is not None:
                st.info("查询无结果: 可能筛选条件过严, 或该时间段无数据。")
            with st.expander("查看 SQL / 执行轨迹 / 口径"):
                st.code(p.get("sql") or "(无 SQL)", language="sql")
                st.text(p.get("trace") or "(无轨迹)")
        elif kind == "clarify":
            st.warning(m["content"] + "\n\n请在下方输入补充信息后再次提问(直接回车不追问, 将使用默认假设)。")
        elif kind == "reject":
            st.error(m["content"])
        elif kind == "error":
            st.error(m["content"])
        else:
            st.markdown(m["content"])
