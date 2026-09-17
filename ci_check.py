#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M11 工程化 CI 自动校验 —— 纯标准库, 零第三方依赖, 零真实 LLM 调用。

用法(在仓库任意目录下都能跑, 所有路径按本文件位置解析, 不依赖 cwd):
    python3 ci_check.py                      # 跑全部 7 项检查, 逐项打印 OK/FAIL
    python3 ci_check.py --only deps          # 只跑某一项(CI 里每步只跑一项, 便于定位)
    python3 ci_check.py --only deps,judge    # 跑多指定项
    python3 ci_check.py --list               # 列出检查项 id
    python3 ci_check.py --print-judge-hash   # 打印判定器区域 sha256(维护冻结常量时用)

退出码: 全部通过 = 0; 任一 FAIL = 1。

7 项检查与 M11 工作流步骤一一对应:
    1 syntax       语法编译            所有 *.py 逐个 compile()
    2 deps         导入与无第三方包断言  加载引擎与演示层后 sys.modules 只多出标准库
    3 decouple     引擎解耦断言        引擎/演示层不出现券商域词(词表 + enterprise_biz.json 抽取)
    4 judge        判定器冻结断言      run_eval.py 的 judge→pct 区间 sha256 必须等于冻结常量
    5 secret       密钥泄露防线        全仓库文本文件 grep sk-[A-Za-z0-9]{20,}(排除未入库的 .env.local)
    6 synth-smoke  合成库端到端冒烟     sqlite3 现场建 3 张小表 + 桩 LLM 驱动引擎走完整链路
    7 sse          SSE 接口契约冒烟      用合成库起 demo/server.py, 断言 /api/health 200 与阶段/done 事件

为什么这些能进 CI: 它们都不需要 enterprise.db(大表不入库)、不需要 API 密钥、不需要联网。
评测(run_eval.py)与边界用例(run_edge_cases.py)刻意不在 CI 内 —— 见 README 的"CI / 自动校验"一节。
"""

import argparse
import hashlib
import http.client
import importlib.util
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

# ---------------- 仓库路径(全部按本文件位置解析, 不用盘符/不用 logs 这类与 cwd 绑定的写法) ----------------
HERE = Path(__file__).resolve().parent
ROOT = HERE
TEXT2SQL_PY = ROOT / "text2sql_demo" / "text2sql.py"
SERVER_PY = ROOT / "demo" / "server.py"
INDEX_HTML = ROOT / "demo" / "index.html"
RUN_EVAL_PY = ROOT / "benchmark" / "run_eval.py"
BIZ_JSON = ROOT / "demo" / "enterprise_biz.json"

# 扫描/遍历时跳过的目录与文件(都是运行期产物、二进制或未入库的本地密钥)
SKIP_DIRS = {".git", "__pycache__", "logs", ".reasonix", ".venv", "venv", "env",
             ".idea", ".vscode", "node_modules", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIX = {".db", ".sqlite", ".sqlite3", ".xlsx", ".png", ".jpg", ".jpeg", ".gif",
               ".ico", ".zip", ".gz", ".tar", ".pdf", ".woff", ".woff2", ".exe", ".dll"}
SKIP_NAMES = {".env", ".env.local"}   # 本地密钥文件永不入库, 也不该被"泄露检查"误伤
MAX_SCAN_BYTES = 2 * 1024 * 1024

# ---------------- 检查用常量 ----------------
# 密钥泄露: DeepSeek/OpenAI 风格的 sk- 开头长串
SECRET_RE = re.compile(rb"sk-[A-Za-z0-9]{20,}")
# 券商(企业)域词: 与 benchmark/m7_selfcheck.py 同一词表
DOMAIN_RE = re.compile(r"客户等级|性别|学历|职业|一级分类|二级分类|总资产|交易额|持仓市值|比亚迪|科创板")
# 判定器区域: benchmark/run_eval.py 的 def judge( ... def pct( 之间
JUDGE_BEGIN = "def judge("
JUDGE_END = "def pct("
# 冻结哈希: judge→pct 区间文本的 sha256(行尾统一为 \n)。改动判定器一行 => 哈希变化 => 检查红。
JUDGE_REGION_SHA256 = "eb3cb1108d4d54100542fd16537d7192d8d03813586ac3188c67237fc5a94252"

# ---------------- 小工具 ----------------


def rel(p: Path) -> str:
    """相对仓库根的可读路径(打印用, 跨平台统一 /)。"""
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def iter_files():
    """遍历仓库内需要检查的文本文件(跳过 SKIP_* 与超大文件)。"""
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(ROOT).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if p.name in SKIP_NAMES or p.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            if p.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        yield p


def iter_py_files():
    for p in iter_files():
        if p.suffix == ".py":
            yield p


def load_module(name: str, path: Path):
    """按文件位置加载一个 .py 为模块(不依赖 cwd)。"""
    path = Path(path).resolve()
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_engine():
    """取得引擎模块(同一进程内复用, 保证后面打桩对演示层也生效)。"""
    mod = sys.modules.get("text2sql")
    if mod is None or getattr(mod, "__file__", None) != str(TEXT2SQL_PY):
        mod = load_module("text2sql", TEXT2SQL_PY)
    return mod


def get_server_module():
    """取得 demo/server.py 模块(它内部 `import text2sql`, 因此拿到的是同一个已加载模块)。"""
    mod = sys.modules.get("demo.server")
    if mod is None:
        get_engine()          # 先加载引擎, 保证 server 内的 import 复用同一对象(打桩才生效)
        mod = load_module("demo.server", SERVER_PY)
    return mod


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------- 1. 语法编译 ----------------


def check_syntax():
    bad, total = [], 0
    for p in iter_py_files():
        total += 1
        try:
            src = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            bad.append(f"{rel(p)}: 读取失败 {type(e).__name__}")
            continue
        try:
            compile(src, str(p), "exec")
        except SyntaxError as e:
            bad.append(f"{rel(p)}:{e.lineno}: {e.msg}")
    for b in bad:
        print(f"      语法错误 -> {b}")
    ok = not bad
    return ok, f"编译 {total} 个 *.py, 语法错误 {len(bad)} 处"


# ---------------- 2. 导入与"无第三方包"断言 ----------------

ALLOWED_EXTRA = {"text2sql", "demo", "demo.server", "__main__"}


def _module_allowed(name: str) -> bool:
    top = name.split(".")[0]
    if name in sys.stdlib_module_names or top in sys.stdlib_module_names:
        return True
    return name in ALLOWED_EXTRA


def check_deps():
    before = set(sys.modules)
    get_engine()          # 引擎
    get_server_module()   # 演示层
    added = sorted({n for n in sys.modules if n not in before and not _module_allowed(n)})
    for a in added:
        print(f"      非标准库模块 -> {a}")
    ok = not added
    n_local = len({n for n in sys.modules if n.startswith(("demo", "text2sql"))})
    return ok, (f"加载 text2sql.py + demo/server.py 后新增第三方模块 {len(added)} 个"
                if added else f"加载引擎与演示层后仅标准库(本地模块 {n_local} 个)")


# ---------------- 3. 引擎解耦断言(无券商域词) ----------------


def extract_biz_tokens():
    """从 demo/enterprise_biz.json 抽取企业库域词(表名/列名/口径断言 token)。

    与 demo/selfcheck.py 同一套抽取逻辑: 口径只在注入文件里, 不该出现在引擎/演示层源码里。
    """
    tokens = set()
    if not BIZ_JSON.exists():
        return tokens
    biz = json.loads(BIZ_JSON.read_text(encoding="utf-8"))
    for rule in (biz.get("caliber_assertions") or []):
        for key in ("must_contain", "must_not_contain"):
            for v in (rule.get(key) or []):
                if isinstance(v, str):
                    tokens.add(v)
    for key in (biz.get("sql_hints") or {}):
        tokens.add(key)
    for v in (biz.get("sql_hints") or {}).values():
        for m in re.findall(r"\b[a-z_][a-z0-9_]{3,}\b", str(v)):
            tokens.add(m)
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
    return {t for t in tokens if t.lower() not in generic_sql}


def grep_file(path: Path, word_res, label: str):
    """在文件里逐行找域词, 返回命中说明列表。"""
    hits = []
    if not path.exists():
        return [f"{label}: 文件不存在 {rel(path)}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError) as e:
        return [f"{label}: 读取失败 {type(e).__name__}"]
    for i, line in enumerate(lines, 1):
        if word_res.search(line):
            hits.append(f"{label}:{i}: {line.strip()[:120]}")
    return hits


def check_decouple():
    engine = TEXT2SQL_PY.read_text(encoding="utf-8")
    hits = []
    # (a) 引擎 vs 固定券商域词表
    for i, line in enumerate(engine.splitlines(), 1):
        if DOMAIN_RE.search(line):
            hits.append(f"text2sql_demo/text2sql.py:{i}: {line.strip()[:120]}")
    # (b) 引擎 + 演示层 vs 从 enterprise_biz.json 抽出的域词
    tokens = extract_biz_tokens()
    if tokens:
        pat = re.compile("|".join(rf"\b{re.escape(t)}\b" for t in sorted(tokens)))
        for path, label in ((TEXT2SQL_PY, "text2sql_demo/text2sql.py"),
                            (SERVER_PY, "demo/server.py"),
                            (INDEX_HTML, "demo/index.html")):
            hits += grep_file(path, pat, label)
    for h in hits:
        print(f"      域词命中 -> {h}")
    ok = not hits
    return ok, (f"引擎/演示层券商域词命中 {len(hits)} 处" if hits
                else f"引擎与演示层无券商域词(固定词表 + {len(tokens)} 个注入 token)")


# ---------------- 4. 判定器冻结断言 ----------------


def judge_region_text() -> str:
    text = RUN_EVAL_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
    a = text.index(JUDGE_BEGIN)
    b = text.index(JUDGE_END, a)
    return text[a:b]


def judge_region_sha256() -> str:
    return hashlib.sha256(judge_region_text().encode("utf-8")).hexdigest()


def check_judge():
    actual = judge_region_sha256()
    ok = actual == JUDGE_REGION_SHA256
    n_lines = judge_region_text().count("\n") + 1
    if not ok:
        print(f"      期望 sha256 -> {JUDGE_REGION_SHA256}")
        print(f"      实际 sha256 -> {actual}")
        print("      判定器(benchmark/run_eval.py 的 judge→pct 区间)已改动, 违反冻结铁律")
    return ok, f"judge→pct 区间 {n_lines} 行 sha256={actual[:16]}… 与冻结常量一致"


# ---------------- 5. 密钥泄露防线 ----------------


def check_secret():
    hits, scanned = [], 0
    for p in iter_files():
        scanned += 1
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for i, line in enumerate(data.splitlines(), 1):
            if SECRET_RE.search(line):
                # 只报位置与掩码, 不回显密钥原文(避免检查本身造成二次泄露)
                hits.append(f"{rel(p)}:{i}: sk-****")
    for h in hits:
        print(f"     疑似密钥 -> {h}")
    ok = not hits
    return ok, (f"扫描 {scanned} 个文本文件, 命中疑似密钥 {len(hits)} 处" if hits
                else f"扫描 {scanned} 个入库范围内文本文件, 未命中 sk- 开头的疑似密钥")


# ---------------- 合成库 + 桩 LLM(第 6/7 项共用) ----------------
# 桩响应按 system prompt 关键字分发, 保证确定性: 全程零 API 调用。
STUB_UNDERSTAND = json.dumps({
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
STUB_RETRIEVE = json.dumps({
    "tables": [{"table": "customers", "columns": ["region", "name"]}],
}, ensure_ascii=False)
STUB_PLAN = json.dumps({
    "summary": "华东地区客户数",
    "tables": ["customers"],
    "filters": ["region = '华东'"],
    "aggregations": [{"func": "COUNT", "field": "*", "alias": "客户数"}],
    "group_by": [],
    "order_by": [],
    "limit": None,
    "steps": ["筛选华东地区", "计数"],
}, ensure_ascii=False)
STUB_SQL = "SELECT COUNT(*) AS 客户数 FROM customers WHERE region = '华东'"
STUB_EXPLAIN = "合成库冒烟: 华东地区共 2 位客户。默认假设: 统计口径为客户主档行数。"
SMOKE_QUESTION = "华东地区有多少客户?"


def build_synth_db(path: Path):
    """现场建一个 3 张小表的合成库(零外部文件、零网络)。"""
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE customers (
                cust_id INTEGER PRIMARY KEY,
                name    TEXT,
                region  TEXT,
                risk_level TEXT,
                assets_amount REAL
            );
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                cust_id  INTEGER,
                amount   REAL,
                order_date TEXT
            );
            CREATE TABLE contacts (
                contact_id INTEGER PRIMARY KEY,
                cust_id  INTEGER,
                channel  TEXT,
                status   TEXT
            );
            """
        )
        con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [
            (1, "客户甲", "华东", "高风险", 12000.0),
            (2, "客户乙", "华东", "中风险", 8000.0),
            (3, "客户丙", "华南", "低风险", 3000.0),
        ])
        con.executemany("INSERT INTO orders VALUES (?,?,?,?)", [
            (1, 1, 1500.0, "2026-01-05"),
            (2, 1, 2500.0, "2026-02-11"),
            (3, 3, 900.0, "2026-03-02"),
        ])
        con.executemany("INSERT INTO contacts VALUES (?,?,?,?)", [
            (1, 1, "短信", "已接通"),
            (2, 3, "电话", "未接通"),
        ])
        con.commit()
    finally:
        con.close()


def install_stub(mod) -> dict:
    """把引擎的 llm_chat 换成桩函数, 返回调用计数(证明零 API: 全部由桩应答)。"""
    hits = {"understand": 0, "retrieve": 0, "plan": 0, "sql": 0, "explain": 0, "other": 0}

    def fake_llm(system, user, api_key, max_tokens=1500):
        if "理解器" in system:
            hits["understand"] += 1
            return STUB_UNDERSTAND
        if "检索器" in system:
            hits["retrieve"] += 1
            return STUB_RETRIEVE
        if "计划器" in system:
            hits["plan"] += 1
            return STUB_PLAN
        if "结论解释器" in system:
            hits["explain"] += 1
            return STUB_EXPLAIN
        hits["sql"] += 1
        return STUB_SQL

    mod.llm_chat = fake_llm
    return hits


# ---------------- 6. 合成库 + 桩 LLM 端到端冒烟 ----------------


def check_synth_smoke():
    mod = get_engine()
    with tempfile.TemporaryDirectory(prefix="ci_synth_") as tmp:
        db = Path(tmp) / "synth.db"
        build_synth_db(db)
        hits = install_stub(mod)
        agent = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0)
        ans = agent.run(SMOKE_QUESTION, clarify=False)
        stages = [e.stage for e in ans.trace.entries]
        problems = []
        if not ans.answerable:
            problems.append(f"answerable=False(reject_reason={ans.reject_reason})")
        if not (ans.sql or "").strip():
            problems.append("未生成 SQL")
        if ans.error:
            problems.append(f"执行出错: {ans.error}")
        # 引擎 rows 是 sqlite 行元组(经演示层 JSON 序列化后才是数组), 这里统一成 list of list 比对
        rows_norm = [list(r) for r in (ans.rows or [])]
        if rows_norm != [[2]]:
            problems.append(f"结果行不符: {ans.rows!r}(期望 [[2]])")
        if not (ans.explanation or "").strip():
            problems.append("解释为空")
        if not hits["understand"] or not hits["retrieve"] or not hits["plan"] \
                or not hits["sql"] or not hits["explain"]:
            problems.append(f"桩 LLM 未覆盖完整链路: {hits}")
        for p in problems:
            print(f"      冒烟问题 -> {p}")
        print(f"      SQL      : {ans.sql}")
        print(f"      结果     : headers={ans.headers} rows={ans.rows}")
        print(f"      解释     : {(ans.explanation or '')[:60]}")
        print(f"      阶段数   : {len(stages)} ({' / '.join(stages[:3])} …)")
        print(f"      桩 LLM 调用: {hits} (真实 API 调用 0 次)")
        ok = not problems
    return ok, ("合成库 3 表 + 桩 LLM 端到端(理解→检索→计划→检查→SQL→安全校验→执行→检查结果→解释)通过"
                if ok else f"合成库冒烟失败 {len(problems)} 处")


# ---------------- 7. SSE 接口契约冒烟 ----------------


def check_sse():
    srv = get_server_module()
    mod = get_engine()
    with tempfile.TemporaryDirectory(prefix="ci_sse_") as tmp:
        db = Path(tmp) / "synth.db"
        build_synth_db(db)
        install_stub(mod)          # 演示层复用同一引擎模块 => 桩生效, 零 API 调用

        port = free_port()
        agent = srv.DemoQueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0)
        server = srv.ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
        server.agent = agent
        server.agent_lock = threading.Lock()
        server.db_path = db
        server.biz_context_file = None
        server.index_html = INDEX_HTML.read_bytes()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        problems, stages, done = [], [], None
        try:
            # /api/health 必须 200
            status, body = _get(port, "/api/health", timeout=10.0)
            print(f"      /api/health -> HTTP {status} {body[:80]}")
            if status != 200:
                problems.append(f"/api/health 返回 {status}")
            else:
                try:
                    if not json.loads(body).get("ok"):
                        problems.append("/api/health 未返回 ok=true")
                except ValueError:
                    problems.append("/api/health 不是合法 JSON")

            # SSE: 边跑边推, 直到 done 事件
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request("GET", f"/api/ask/stream?q={quote(SMOKE_QUESTION)}&clarify=false")
            resp = conn.getresponse()
            print(f"      /api/ask/stream -> HTTP {resp.status} "
                  f"Content-Type={resp.getheader('Content-Type')}")
            if resp.status != 200:
                problems.append(f"/api/ask/stream 返回 {resp.status}")
            deadline = time.time() + 60
            while time.time() < deadline:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                try:
                    ev = json.loads(text[5:].strip())
                except ValueError:
                    continue
                if ev.get("type") == "stage":
                    stages.append(ev.get("stage"))
                elif ev.get("type") == "done":
                    done = ev
                    break
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        print(f"      阶段事件: {len(stages)} 条 -> {' / '.join(stages)}")
        if len(stages) < 3:
            problems.append(f"阶段事件不足 3 条(实际 {len(stages)})")
        if done is None:
            problems.append("未收到 done 事件")
        else:
            result = done.get("result") or {}
            print(f"      done 事件: sql={result.get('sql')} rows={result.get('rows')} "
                  f"elapsed_s={result.get('elapsed_s')}")
            if not (result.get("sql") or "").strip():
                problems.append("done.result.sql 为空")
            if list(result.get("rows") or []) != [[2]]:
                problems.append(f"done.result.rows 不符: {result.get('rows')!r}")
        for p in problems:
            print(f"      SSE 问题 -> {p}")
        ok = not problems
    return ok, ("合成库起服务: /api/health 200, SSE 有阶段事件与 done 事件(与 POST /api/ask 同源字段)"
                if ok else f"SSE 契约失败 {len(problems)} 处")


def _get(port: int, path: str, timeout: float = 10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


# ---------------- 调度 ----------------

CHECKS = [
    ("syntax", "语法编译", check_syntax),
    ("deps", "导入与无第三方包断言", check_deps),
    ("decouple", "引擎解耦断言(无券商域词)", check_decouple),
    ("judge", "判定器冻结断言", check_judge),
    ("secret", "密钥泄露防线", check_secret),
    ("synth-smoke", "合成库 + 桩 LLM 端到端冒烟", check_synth_smoke),
    ("sse", "SSE 接口契约冒烟", check_sse),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="M11 CI 自动校验(纯标准库, 零 LLM 调用)")
    ap.add_argument("--only", default=None, help="只跑指定检查项, 逗号分隔(如 deps,judge)")
    ap.add_argument("--list", action="store_true", help="列出检查项 id")
    ap.add_argument("--print-judge-hash", action="store_true",
                    help="打印判定器区域 sha256(维护 JUDGE_REGION_SHA256 冻结常量用)")
    args = ap.parse_args(argv)

    if args.list:
        for cid, desc, _ in CHECKS:
            print(f"{cid:12s} {desc}")
        return 0
    if args.print_judge_hash:
        print(judge_region_sha256())
        return 0

    wanted = None
    if args.only:
        wanted = [c.strip() for c in args.only.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in {cid for cid, _, _ in CHECKS}]
        if unknown:
            ap.error(f"未知检查项: {unknown}(可选: {[c for c, _, _ in CHECKS]})")

    print("== M11 CI 自动校验(纯标准库, 零第三方依赖, 零真实 LLM 调用) ==")
    print(f"仓库根: {ROOT}")
    t0 = time.time()
    failures = []
    for cid, desc, fn in CHECKS:
        if wanted and cid not in wanted:
            continue
        try:
            ok, summary = fn()
        except Exception as e:  # 检查本身炸了也算 FAIL, 不带崩整个脚本
            traceback.print_exc()
            ok, summary = False, f"检查自身异常: {type(e).__name__}: {e}"
        print(f"[{'OK' if ok else 'FAIL'}] {cid:<11} {desc} —— {summary}")
        if not ok:
            failures.append(cid)

    n_run = len(wanted) if wanted else len(CHECKS)
    print(f"-- 共跑 {n_run} 项, 失败 {len(failures)} 项, 用时 {time.time() - t0:.2f}s --")
    if failures:
        print(f"CI 校验未通过: {failures}")
        return 1
    print("CI 校验全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
