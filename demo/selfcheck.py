#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8 无头自检脚本(纯标准库, 不调用 LLM)。

用法:
    python demo/selfcheck.py [--db <库路径>] [--port <端口>] [--no-smoke]

检查项:
1. import demo.server 后 sys.modules 无第三方包
2. 引擎 text2sql_demo/text2sql.py 不含企业库域词(域词从 demo/enterprise_biz.json 自动提取)
3. demo/server.py 与 demo/index.html 不含企业库域词(口径只在 enterprise_biz.json 注入)
4. demo/enterprise_biz.json 与 benchmark/run_eval.py 的 ENTERPRISE_BIZ/SQL_HINTS/CALIBER_ASSERTIONS 原样一致
5. judge 函数体与 HEAD 逐字符一致(git 可用时)
6. 起本地服务后: /api/health 正常, 首页 HTML 含 7 个展示要素关键词
"""
import ast
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT2SQL = ROOT / "text2sql_demo" / "text2sql.py"
RUN_EVAL = ROOT / "benchmark" / "run_eval.py"
BIZ_JSON = HERE / "enterprise_biz.json"

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def allowed_module(name: str) -> bool:
    if name in sys.stdlib_module_names:
        return True
    top = name.split(".")[0]
    if top in sys.stdlib_module_names:
        return True
    return name in {"demo", "demo.server", "text2sql", "sql_ast", "sql_plan", "__main__"}


def extract_domain_tokens(biz: dict) -> set:
    """从注入文件提取企业库域词(表名/列名/口径断言 token), 用于 grep 引擎与演示层。"""
    tokens = set()
    for rule in (biz.get("caliber_assertions") or []):
        for key in ("must_contain", "must_not_contain"):
            for v in (rule.get(key) or []):
                if isinstance(v, str):
                    tokens.add(v)
    for key in (biz.get("sql_hints") or {}):
        tokens.add(key)
    for col in (biz.get("sensitive_columns") or []):
        tokens.add(str(col))
    for v in (biz.get("sql_hints") or {}).values():
        for m in re.findall(r"\b[a-z_][a-z0-9_]{3,}\b", str(v)):
            tokens.add(m)
    # 只从 biz_context 提取表名(dim/dwd/dws/ads 前缀), 避免把通用列名当域词
    for m in re.findall(r"\b(?:dim|dwd|dws|ads)_[a-z0-9_]+\b", biz.get("biz_context") or ""):
        tokens.add(m)

    generic_sql = {
        "select", "from", "where", "group", "order", "limit", "having", "join",
        "left", "right", "inner", "outer", "full", "cross", "union", "case",
        "when", "then", "else", "end", "coalesce", "count", "sum", "round",
        "distinct", "between", "like", "null", "desc", "asc", "with", "into",
        "values", "true", "false", "over", "partition", "row", "rows", "only",
        "query", "selects", "table", "column", "columns", "real", "text",
        "code", "describe", "name",
    }
    tokens = {t for t in tokens if t.lower() not in generic_sql}
    return tokens


def grep_tokens_in_file(path: Path, tokens: set) -> list:
    if not path.exists():
        return [f"文件不存在: {path}"]
    text = path.read_text(encoding="utf-8")
    return [t for t in sorted(tokens) if re.search(rf"\b{re.escape(t)}\b", text)]


def extract_run_eval_biz():
    tree = ast.parse(RUN_EVAL.read_text(encoding="utf-8"))
    vals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in {
                        "ENTERPRISE_BIZ", "SQL_HINTS", "CALIBER_ASSERTIONS", "SENSITIVE_COLUMNS", "ENTITIES"}:
                    vals[t.id] = ast.literal_eval(node.value)
    return vals


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def main():
    db = ROOT / "customer_marketing_db" / "marketing.db"
    smoke = True
    args = sys.argv[1:]
    if "--no-smoke" in args:
        smoke = False
        args.remove("--no-smoke")
    if "--db" in args:
        i = args.index("--db")
        db = Path(args[i + 1]).resolve()
        args = args[:i] + args[i + 2:]
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    else:
        port = None

    print("== M8 无头自检 ==")

    # 1. 导入 demo.server, 断言无第三方依赖(只比较 import 前后新增的模块)
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location("demo.server", HERE / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    added = sorted({name for name in sys.modules if name not in before
                    and not allowed_module(name)})
    check("依赖检查: import demo.server 未引入第三方包", not added,
          str(added) if added else "仅标准库")

    # 2. 口径 JSON 与 run_eval.py 同源一致
    biz = json.loads(BIZ_JSON.read_text(encoding="utf-8"))
    src = extract_run_eval_biz()
    same = (biz.get("biz_context") == src["ENTERPRISE_BIZ"]
            and biz.get("sql_hints") == src["SQL_HINTS"]
            and biz.get("caliber_assertions") == src["CALIBER_ASSERTIONS"]
            and biz.get("sensitive_columns") == src["SENSITIVE_COLUMNS"]
            and biz.get("entities") == src["ENTITIES"])
    check("口径同源: enterprise_biz.json == run_eval.py 五常量", same,
          "" if same else "JSON 与 run_eval.py 不一致")

    # 3. 引擎与演示层域词 grep
    tokens = extract_domain_tokens(biz)
    engine_hits = grep_tokens_in_file(TEXT2SQL, tokens)
    check("引擎域词 grep 为空", not engine_hits, str(engine_hits) if engine_hits else "无命中")
    demo_hits = grep_tokens_in_file(HERE / "server.py", tokens) + \
        grep_tokens_in_file(HERE / "index.html", tokens)
    check("演示层(server.py/index.html)不内嵌业务口径", not demo_hits,
          str(demo_hits) if demo_hits else "无命中")

    # 4. judge 函数体未动(git 可用时与 HEAD 比较)
    if shutil.which("git"):
        r1 = subprocess.run(["git", "diff", "--quiet", "--", "benchmark/run_eval.py"],
                            cwd=ROOT).returncode
        r2 = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "benchmark/run_eval.py"],
                            cwd=ROOT).returncode
        same_file = (r1 == 0 and r2 == 0)
        check("judge/run_eval.py 与 HEAD 逐字符一致", same_file,
              "" if same_file else "benchmark/run_eval.py 有未提交改动")
    else:
        print("[SKIP] judge 对比: git 不可用")

    # 5. 本地服务冒烟(不调用 LLM)
    if smoke:
        if not db.exists():
            check("本地服务冒烟", False, f"数据库不存在: {db}")
            return
        p = port or free_port()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "server.py"), "--db", str(db), "--port", str(p), "--offline"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env)
        base = f"http://127.0.0.1:{p}"
        try:
            ok_health = False
            for _ in range(30):
                if proc.poll() is not None:
                    break
                try:
                    status, body = fetch(base + "/api/health", timeout=1.0)
                    if status == 200 and '"ok": true' in body:
                        ok_health = True
                        break
                except Exception:
                    time.sleep(0.25)
            check("本地服务冒烟: /api/health 返回 ok", ok_health)
            if ok_health:
                _, html = fetch(base + "/")
                keywords = ["计划", "SQL", "结果", "口径", "假设", "修复", "实体"]
                missing = [k for k in keywords if k not in html]
                check("首页 HTML 含 7 个展示要素关键词", not missing,
                      "" if not missing else f"缺少: {missing}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("")
    if FAILURES:
        print(f"自检未通过: {FAILURES}")
        sys.exit(1)
    print("自检全部通过。")


if __name__ == "__main__":
    main()
