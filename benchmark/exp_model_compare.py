# -*- coding: utf-8 -*-
"""空响应问题对比实验: 复杂题 deepseek-chat vs flash+强提示重试。

策略:
  A(对照, 已跑)  : deepseek-v4-flash + 引擎原样重试 —— 见 M2 基线 run_20260905_121646.json
  B(本实验 arm1) : deepseek-chat + 引擎原样重试
  C(本实验 arm2) : deepseek-v4-flash + 强提示重试(3 次, 显式最小 JSON 兜底)

记录: 每次 LLM 原始返回(长度/头部/尾部/分类: 空/过短/JSON/疑似拒绝/疑似截断)。
输出: benchmark/runs/exp_model_compare_<ts>.json
"""
import json
import re
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

sys.path.insert(0, str(TEXT2SQL_DIR))
sys.path.insert(0, str(HERE))
import text2sql  # noqa: E402
from text2sql import QueryAgent, load_api_key  # noqa: E402
from run_eval import ENTERPRISE_BIZ  # noqa: E402

orig_llm_chat = text2sql.llm_chat
orig_llm_json = text2sql.llm_json
RAW_LOG = []


def classify(raw):
    if raw is None:
        return "None"
    s = raw.strip()
    if not s:
        return "空返回"
    if len(s) < 20:
        return "过短"
    if re.search(r"(抱歉|无法|不能|对不起|作为.*模型|I cannot|Sorry)", s[:300]):
        return "疑似拒绝"
    if s.startswith("{"):
        return "JSON" if s.rstrip().endswith("}") else "疑似截断(JSON未闭合)"
    if s.startswith("SELECT") or re.match(r"^\s*(SELECT|WITH|EXPLAIN)\b", s, re.I):
        return "SQL" if s.rstrip().endswith(";") or True else "SQL"
    return "其他/未知"


def logged_llm_chat(system, user, api_key, max_tokens=1500):
    raw = orig_llm_chat(system, user, api_key, max_tokens=max_tokens)
    RAW_LOG.append({
        "t": time.time(),
        "model": text2sql.MODEL,
        "raw_len": len(raw or ""),
        "raw_head": (raw or "")[:160],
        "raw_tail": (raw or "")[-120:],
        "verdict": classify(raw),
    })
    return raw


def strong_llm_json(system, user, api_key, max_tokens=2000):
    last = None
    for attempt in range(3):
        raw = text2sql.llm_chat(system, user, api_key, max_tokens=max_tokens)
        try:
            data = text2sql.extract_json(raw)
            if not isinstance(data, dict):
                raise ValueError(f"返回了 {type(data).__name__}")
            return data
        except ValueError as e:
            last = e
            if attempt == 0:
                user = (user + "\n\n[系统] 你上次返回为空或不是 JSON 对象。必须输出一个 JSON 对象。"
                        "若信息不足, 也请输出最小合法 JSON: {\"answerable\": true, \"summary\": \"一句话\", "
                        "\"metrics\": [], \"dimensions\": [], \"filters\": [], \"time_range\": \"\", "
                        "\"assumptions\": [], \"missing\": []}")
            elif attempt == 1:
                user = (user + "\n\n[系统] 再次失败, 这是最后一次机会。只输出一行 JSON, 不要任何其他文字、"
                        "不要 markdown 代码块。若确实无法完成, 输出 {\"answerable\": false, \"reason\": \"无法解析问题\"}。")
            continue
    raise ValueError(f"强提示 3 次仍失败: {last}")


def rows_equal(a_rows, b_rows, tol=1e-6):
    if len(a_rows) != len(b_rows):
        return False
    k = lambda r: json.dumps(r, ensure_ascii=False, default=str)
    for r1, r2 in zip(sorted(a_rows, key=k), sorted(b_rows, key=k)):
        if len(r1) != len(r2):
            return False
        for c1, c2 in zip(r1, r2):
            if isinstance(c1, (int, float)) and isinstance(c2, (int, float)):
                if abs(float(c1) - float(c2)) > tol * max(1.0, abs(float(c1)), abs(float(c2))):
                    return False
            elif str(c1) != str(c2):
                return False
    return True


def run_arm(name, questions, model, patch_json=False):
    global RAW_LOG
    RAW_LOG = []
    text2sql.llm_chat = logged_llm_chat
    text2sql.llm_json = strong_llm_json if patch_json else orig_llm_json
    text2sql.MODEL = model
    api_key = load_api_key()
    results = []
    for q in questions:
        rec = {"id": q["id"], "difficulty": q["difficulty"], "question": q["question"][:60]}
        t0 = time.time()
        try:
            agent = QueryAgent(str(DB), api_key, verbose=False, biz_context=ENTERPRISE_BIZ)
            ans = agent.run(q["question"], clarify=False)
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = (ans.sql or "")[:300]
            rec["error"] = ans.error or ""
            rec["exec_ok"] = bool(ans.sql and not ans.error and ans.rows is not None)
            rec["result_ok"] = bool(rec["exec_ok"] and rows_equal(ans.rows or [], q["gold_answer"]["rows"]))
        except Exception as ex:
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rec["sql"] = ""
            rec["error"] = f"{type(ex).__name__}: {ex}"
            rec["exec_ok"] = False
            rec["result_ok"] = False
        rec["raw_calls"] = len(RAW_LOG)
        rec["empty_returns"] = sum(1 for e in RAW_LOG if e["verdict"] == "空返回")
        rec["verdicts"] = [e["verdict"] for e in RAW_LOG]
        rec["raw_log"] = list(RAW_LOG)
        results.append(rec)
        print(f"[{name}] {q['id']} exec={int(rec['exec_ok'])} res={int(rec['result_ok'])} "
              f"calls={rec['raw_calls']} empty={rec['empty_returns']} t={rec['elapsed_ms']}ms", flush=True)
    return results


def main():
    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    complex_ids = {"C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "M13"}
    questions = [q for q in bench["items"] if q["id"] in complex_ids]
    print(f"实验题数: {len(questions)}")
    arms = {
        "arm1_deepseek_chat": dict(model="deepseek-chat", patch_json=False),
        "arm2_flash_strong_retry": dict(model="deepseek-v4-flash", patch_json=True),
    }
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "arms": {}}
    for name, cfg in arms.items():
        print(f"\n===== {name} ({cfg['model']}, patch_json={cfg['patch_json']}) =====", flush=True)
        out["arms"][name] = run_arm(name, questions, cfg["model"], cfg["patch_json"])
    RUNS.mkdir(exist_ok=True)
    p = RUNS / f"exp_model_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n实验输出: {p}")


if __name__ == "__main__":
    main()
