# -*- coding: utf-8 -*-
"""探针: 打印 _understand 对 E3 输入的实际结构化输出(chat 与 flash)。"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "text2sql_demo"))
sys.path.insert(0, str(HERE))
import text2sql  # noqa: E402
from text2sql import QueryAgent, load_api_key  # noqa: E402
from run_eval import ENTERPRISE_BIZ  # noqa: E402

DB = str(ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db")

for model in ("deepseek-chat", "deepseek-v4-flash"):
    text2sql.MODEL = model
    agent = QueryAgent(DB, load_api_key(), verbose=False, biz_context=ENTERPRISE_BIZ)
    u = agent._understand("帮我分析一下?")
    print(f"=== {model} ===")
    print("answerable:", u.answerable)
    print("summary:", u.summary)
    print("metrics:", u.metrics)
    print("dimensions:", u.dimensions)
    print("filters:", u.filters)
    print("time_range:", repr(u.time_range))
    print("assumptions:", u.assumptions)
    print("missing_required:", u.missing_required)
    print("missing_optional:", u.missing_optional)
    print()
