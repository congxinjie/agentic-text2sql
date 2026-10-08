#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M11 工程化 CI 自动校验 —— 纯标准库, 零第三方依赖, 零真实 LLM 调用。

用法(在仓库任意目录下都能跑, 所有路径按本文件位置解析, 不依赖 cwd):
    python3 ci_check.py                      # 跑全部 12 项检查, 逐项打印 OK/FAIL
    python3 ci_check.py --only deps          # 只跑某一项(CI 里每步只跑一项, 便于定位)
    python3 ci_check.py --only deps,judge    # 跑多指定项
    python3 ci_check.py --list               # 列出检查项 id
    python3 ci_check.py --print-judge-hash   # 打印判定器区域 sha256(维护冻结常量时用)

退出码: 全部通过 = 0; 任一 FAIL = 1。

14 项检查与 M11 工作流步骤一一对应:
    1 syntax       语法编译            所有 *.py 逐个 compile()
    2 deps         导入与无第三方包断言  加载引擎与演示层后 sys.modules 只多出标准库
    3 decouple     引擎解耦断言        引擎/演示层不出现券商域词(词表 + enterprise_biz.json 抽取)
    4 judge        判定器冻结断言      run_eval.py 的 judge→pct 区间 sha256 必须等于冻结常量
    5 secret       密钥泄露防线        全仓库文本文件 grep sk-[A-Za-z0-9]{20,}(排除未入库的 .env.local)
    6 synth-smoke  合成库端到端冒烟     sqlite3 现场建 3 张小表 + 桩 LLM 驱动引擎走完整链路
    7 sse          SSE 接口契约冒烟      用合成库起 demo/server.py, 断言 /api/health 200 与阶段/done 事件
    8 privacy      受限字段屏蔽断言      合成库 + 注入敏感列: 样例不外发/SQL 直引与别名拦截/结果掩码, 并含负对照
    9 entities     实体识别与校验断言    注入实体词表: 识别与五项校验通过; 四个负对照(错表/错列/假 explicit/漏识别)均变红
    10 sql-ast      SQL 编译器前端断言    词法+语句结构 AST: 金标全可解析; 错表/错列/WITH 后写语句负对照均被拦
    11 query-plan   查询计划与索引断言    EXPLAIN QUERY PLAN: 全表扫描/索引搜索/覆盖索引分类 + 引擎接入 + 负对照
    12 schema-rag   RAG schema linking 召回断言  BM25+中文 bigram: 承载表召回/表名直投/top-k/无信号负对照 + 中文分词
    13 decompose    任务分解(agentic 多步)断言  桩 LLM: 2 子问题各自执行 + 合成; 子问题失败回退单轮(负对照)
    14 ui           交互层断言           图表 SVG(含单列负对照)/CSV 导出 + 前端钩子 + Streamlit 图表/下载

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

# ---------------- 运行环境守卫 ----------------
# 本文件与引擎都用到 PEP 604 注解(如 str | None), 需要 Python 3.10+。
if sys.version_info < (3, 10):
    print(f"[环境错误] ci_check 需要 Python 3.10 及以上, 当前为 {sys.version.split()[0]}。"
          "请用 python3.11 ci_check.py 重试。")
    raise SystemExit(2)

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

ALLOWED_EXTRA = {"text2sql", "sql_ast", "sql_plan", "schema_rag", "demo", "demo.server", "__main__"}


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
    for col in (biz.get("sensitive_columns") or []):
        tokens.add(str(col))
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

def check_privacy():
    """受限字段四层屏蔽: 样例数据 / SQL 直引 / SQL 别名 / 结果列; 并带负对照。"""
    mod = get_engine()
    with tempfile.TemporaryDirectory(prefix="ci_privacy_") as tmp:
        db = Path(tmp) / "privacy.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE ads_cust_info_d(pty_id TEXT, sor_pty_id TEXT, name TEXT, cust_age INTEGER);"
            "INSERT INTO ads_cust_info_d VALUES('C0001','SOR-SECRET-0001','张***',35);"
            "INSERT INTO ads_cust_info_d VALUES('C0002','SOR-SECRET-0002','李***',41);")
        conn.commit()
        conn.close()
        secret = "SOR-SECRET-0001"
        problems = []
        # (1) 注入受限字段 -> 样例不含原值, 含占位
        schema = mod.build_schema(str(db), sample_rows=2, sensitive_columns=["sor_pty_id"])
        if secret in schema:
            problems.append("build_schema 样例仍含受限字段原值")
        if repr(mod.SENSITIVE_MASK) not in schema:
            problems.append("build_schema 未对受限字段打码")
        # (2) 负对照: 不注入时原值确实出现, 证明上面是屏蔽在起作用
        raw_schema = mod.build_schema(str(db), sample_rows=2)
        if secret not in raw_schema:
            problems.append("负对照失败: 未注入时样例也看不到原值, 检查无效")
        agent = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=2,
                               sensitive_columns=["sor_pty_id"])
        if secret in agent.schema:
            problems.append("QueryAgent.schema 仍含受限字段原值")
        # (3) SQL 直引 / 别名 / 大小写都必须被拒
        for sql in ("SELECT sor_pty_id FROM ads_cust_info_d",
                    "SELECT sor_pty_id AS 客户号 FROM ads_cust_info_d",
                    "select SOR_PTY_ID from ads_cust_info_d"):
            try:
                agent._validate(sql)
                problems.append(f"受限字段未被拦截: {sql}")
            except ValueError:
                pass
        # (3b) 字符串字面量中的同名词不得误伤
        try:
            agent._validate("SELECT pty_id FROM ads_cust_info_d WHERE name = 'sor_pty_id'")
        except ValueError as e:
            problems.append(f"字符串字面量被误伤: {e}")
        # (4) 结果层: SELECT * 时受限列被掩码
        headers, rows, _ = agent._execute("SELECT * FROM ads_cust_info_d")
        idx = headers.index("sor_pty_id")
        if any(r[idx] != mod.SENSITIVE_MASK for r in rows):
            problems.append("结果层未对受限字段掩码")
        # (5) 不含受限字段的正常查询不受影响
        try:
            h2, rows2, _ = agent._execute("SELECT pty_id, cust_age FROM ads_cust_info_d")
            if h2 != ["pty_id", "cust_age"] or [tuple(r) for r in rows2] != [("C0001", 35), ("C0002", 41)]:
                problems.append("正常查询结果被改变")
        except Exception as e:
            problems.append(f"正常查询被误伤: {type(e).__name__}: {e}")
        if problems:
            for pr in problems:
                print(f"      {pr}")
            return False, "受限字段屏蔽失败: " + "; ".join(problems)
    return True, "受限字段四层(样例/SQL直引/SQL别名/结果)屏蔽通过, 负对照有效"


# ---------------- 9. 实体识别与校验断言(M13) ----------------

def check_entities():
    """M13: 实体识别(explicit 问题直指 / implied 计划隐含 + 程序化兜底)与五项程序化校验;
    四个负对照(错表/错列/假 explicit/漏识别)必须变红; 不注入词表时必须零影响。"""
    mod = get_engine()
    with tempfile.TemporaryDirectory(prefix="ci_entities_") as tmp:
        db = Path(tmp) / "ent.db"
        build_synth_db(db)
        vocab = {"客户": {"table": "customers", "key": "cust_id", "aliases": ["客户", "客户信息"]},
                 "订单": {"table": "orders", "key": "order_id", "aliases": ["订单", "交易"]}}
        question = "华东地区有多少客户?"
        plan = {"summary": "华东客户数", "tables": ["customers"], "filters": ["region = '华东'"],
                "aggregations": [{"func": "COUNT", "field": "*", "alias": "客户数"}],
                "group_by": [], "steps": ["筛选华东", "计数"],
                "output_columns": [{"name": "客户数", "desc": "指标列"}]}
        problems = []

        # (1) 词表未注入: 实体功能整体关闭(计划逐字不变)
        off = mod.QueryAgent(str(db), "ci-stub-key", verbose=False)
        if off._enforce_entity_contract(question, dict(plan)) != plan:
            problems.append("词表未注入时计划被改动(应逐字不变)")

        # (2) 注入词表 + 计划自己没声明实体 -> 程序化兜底补上, 校验通过
        agent = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, entities=vocab)

        # (2b) 关键不变量: 注入词表**不得**改变计划阶段发给 LLM 的提示词
        # (设计选择: 曾小样本怀疑词表会带动模型多 JOIN 承载表, 复查判定为 LLM 固有抖动;
        #  仍不把词表拼进提示词, 使"实体识别不影响计划"成为可断言的不变量, 而非靠抽样论证)
        def plan_prompt(ag):
            seen = []
            old = mod.llm_chat

            def cap(system, user, api_key, max_tokens=1500):
                if "计划器" in system:
                    seen.append(user)
                return json.dumps(plan, ensure_ascii=False)

            mod.llm_chat = cap
            try:
                u = mod.Understanding(answerable=True, summary="华东客户数",
                                      metrics=["客户数"], filters=["region=华东"])
                r = mod.Retrieval(tables=["customers"], columns={"customers": ["region"]})
                ag._plan(u, r)
            finally:
                mod.llm_chat = old
            return seen[0] if seen else ""

        pp_off, pp_on = plan_prompt(off), plan_prompt(agent)
        if not pp_off:
            problems.append("未能捕获计划阶段提示词(stub 未生效)")
        elif pp_off != pp_on:
            problems.append("注入词表后计划阶段提示词发生变化(实体识别不得干扰计划)")
        elif "实体词表" in pp_on or "客户信息" in pp_on:
            # 只查"不可能出现在 schema 里"的东西(cust_id 之类是库里的真实列, 出现在 schema 里是合理的)
            problems.append("词表内容泄进了计划提示词")
        p1 = agent._enforce_entity_contract(question, dict(plan))
        ok, issues = agent._check_entities(question, p1)
        if not ok:
            problems.append(f"正例校验未通过: {issues}")
        ents = {e["name"]: e for e in p1.get("entities") or []}
        if ents.get("客户", {}).get("source") != "explicit":
            problems.append("问题原文含「客户」却未识别为 explicit")
        if ents.get("客户", {}).get("key") != "cust_id":
            problems.append("实体未落到注入的关联键 customers.cust_id")
        if p1.get("entities_covered") is not True:
            problems.append("识别出实体时 entities_covered 应为 true")

        # (3) 端到端(桩 LLM): 实体真的从 Answer 出来, 且 Trace 留下「识别实体」阶段
        install_stub(mod)
        ans = agent.run(question, clarify=False)
        if not ans.entities or not ans.entities_covered:
            problems.append("端到端未产出实体")
        if not any(e.stage == "识别实体" for e in ans.trace.entries):
            problems.append("Trace 未留下「识别实体」阶段")

        # (4) 负对照四连 + 覆盖声明矛盾: 必须全部变红
        def probe(ent_list, covered=None):
            p = dict(p1)
            p["entities"] = ent_list
            if covered is not None:
                p["entities_covered"] = covered
            return agent._check_entities(question, p)

        negs = [
            ("错表", [{"name": "客户", "table": "ghost_tbl", "key": "cust_id",
                       "evidence": "x", "source": "explicit"}], "承载表不存在"),
            ("错列", [{"name": "客户", "table": "customers", "key": "ghost_col",
                       "evidence": "x", "source": "explicit"}], "不是真实列"),
            ("假 explicit", [{"name": "订单", "table": "orders", "key": "order_id",
                              "evidence": "x", "source": "explicit"}], "找不到它的别名"),
            ("漏识别", [], "漏识别实体"),
        ]
        for label, ent_list, expect in negs:
            ok_n, iss = probe(ent_list)
            if ok_n:
                problems.append(f"负对照「{label}」竟然通过(校验形同虚设)")
            elif not any(expect in i for i in iss):
                problems.append(f"负对照「{label}」报错文案不含「{expect}」: {iss}")
        ok_e, iss_e = probe(list(p1.get("entities") or []), covered=False)
        if ok_e or not any("不应为 false" in i for i in iss_e):
            problems.append("负对照「覆盖声明矛盾」未按预期报错")

        # (5) 模型自报不实 -> 兜底丢弃且记录原因(不采信词表外的名字/未落到计划里的实体)
        p2 = agent._enforce_entity_contract(
            question,
            {"tables": ["customers"], "filters": [], "steps": ["x"],
             "entities": [{"name": "不存在的实体", "table": "customers", "key": "cust_id"},
                          {"name": "订单", "table": "orders", "key": "order_id"}]})
        if [e["name"] for e in p2["entities"]] != ["客户"]:
            problems.append(f"兜底未丢弃不实声明: {[e['name'] for e in p2['entities']]}")
        if len(p2.get("entities_notes") or []) != 2:
            problems.append(f"兜底未记录丢弃原因: {p2.get('entities_notes')}")

        if problems:
            for pr in problems:
                print(f"      {pr}")
            return False, "实体识别/校验失败: " + "; ".join(problems)
    return True, ("实体识别(问题直指/计划隐含 + 程序化抽取)与五项校验通过; "
                  "四个负对照(错表/错列/假 explicit/漏识别)均变红; "
                  "计划提示词在注入词表前后逐字不变(实体不干扰计划); 不注入词表时零影响")


def check_sql_ast():
    """SQL 编译器前端断言: 金标全可解析 + 表/列/只读负例必被拦。"""
    mod = get_engine()
    items = json.loads((ROOT / "benchmark" / "benchmark.json").read_text(encoding="utf-8"))["items"]
    problems = []
    for it in items:
        qid = it.get("id")
        try:
            st = mod.sql_ast.parse(it["gold_sql"])
            if st.kind != "select" or not st.read_only:
                problems.append(f"{qid}: kind={st.kind}")
        except Exception as e:
            problems.append(f"{qid}: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory(prefix="ci_ast_") as tmp:
        db = Path(tmp) / "synth.db"
        build_synth_db(db)
        agent = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0)
        try:
            agent._ast_check("SELECT COUNT(*) FROM customers")
        except ValueError as e:
            problems.append(f"合法 SQL 被误拦: {e}")
        for sql, tag in (
            ("SELECT COUNT(*) FROM no_such_table", "错表"),
            ("SELECT customers.no_such_col FROM customers", "错列"),
            ("WITH x AS (SELECT 1) DELETE FROM customers", "WITH 后写语句"),
        ):
            try:
                agent._ast_check(sql)
                problems.append(f"{tag} 未被拦截: {sql}")
            except ValueError:
                pass
        try:
            _ph, _pr = agent._project_result(
                ["月份", "买入笔数", "卖出笔数", "交易笔数"], [("202601", 1, 2, 3)],
                {"output_columns": [{"name": "月份"}, {"name": "交易笔数"}]})
            if list(_ph) != ["月份", "交易笔数"] or [tuple(x) for x in _pr] != [("202601", 3)]:
                problems.append(f"输出列投影未生效: {_ph} {_pr}")
        except Exception as e:
            problems.append(f"输出列投影异常: {e}")
    if problems:
        for p in problems:
            print(f"      {p}")
        return False, f"SQL AST 断言失败 {len(problems)} 处"
    return True, f"{len(items)} 道金标全可解析; 错表/错列/WITH 后写语句均被拦; 输出列投影裁剪正确"


def check_schema_rag():
    """RAG schema linking 断言: 合成词表召回金标表 + top-k 约束 + 确定性 + 中文分词 + 负对照。

    不依赖 enterprise.db / 不调 LLM —— 用 4 张合成表 + 中文 biz_context 验证
    BM25 + bigram 的召回行为, 与 rag_ablation.py 的离线口径一致。
    """
    mod = get_engine()
    R = mod.schema_rag
    meta = {
        "customers": ["cust_id", "cust_name", "region", "cust_type"],
        "assets": ["cust_id", "asset_code", "market_value", "stat_date"],
        "transactions": ["cust_id", "trade_date", "amount", "side"],
        "campaigns": ["campaign_id", "campaign_name", "channel"],
    }
    biz = ("客户资产: assets 表按 stat_date 记录每个客户的市值 market_value。\n"
           "交易流水: transactions 表记录客户买卖 side 与成交金额 amount。\n"
           "营销活动: campaigns 表记录活动名称与投放渠道 channel。")
    problems = []
    ix = R.SchemaIndex(meta, biz)

    # 1) 自然语言问题 -> 必召回承载表(中文 bigram + biz_context 映射)
    for q, want in (
        ("查询客户资产市值", "assets"),
        ("最近交易流水金额", "transactions"),
        ("各营销活动渠道分布", "campaigns"),
    ):
        sel = ix.select(q, 3)
        if want not in sel:
            problems.append(f"未召回 {want}: {q!r} -> {sel}")

    # 2) 问题直指表名 -> 直投加分, top-1 必须是该表
    sel = ix.select("customers 表有多少客户", 1)
    if sel != ["customers"]:
        problems.append(f"表名直指未优先召回: {sel}")

    # 3) top-k 上限 + 只返回真实表 + 确定性(同一问题两次一致)
    for k in (1, 2, 4):
        sel = ix.select("客户资产与交易", k)
        if len(sel) > k or not set(sel) <= set(meta):
            problems.append(f"top-{k} 越界/幻觉表: {sel}")
    if ix.select("客户资产与交易", 3) != ix.select("客户资产与交易", 3):
        problems.append("同一问题两次召回结果不一致")

    # 4) 负对照: 空问题/无信号问题不得乱召回
    for q in ("", "???", "!!!"):
        sel = ix.select(q, 3)
        if sel:
            problems.append(f"无信号问题不应召回表: {q!r} -> {sel}")

    # 5) 中文 bigram 分词(免第三方分词器)
    toks = R.tokens("客户资产")
    for t in ("客户", "户资", "资产"):
        if t not in toks:
            problems.append(f"中文 bigram 分词缺 {t}: {toks}")

    if problems:
        for p in problems:
            print(f"      {p}")
        return False, f"RAG schema linking 断言失败 {len(problems)} 处"
    return True, ("BM25 召回承载表/表名直投/top-k 上限/无幻觉表/确定性/中文 bigram/无信号负对照 "
                  "全部通过(不依赖 enterprise.db)")


def check_query_plan():
    """查询计划/索引断言: EXPLAIN QUERY PLAN 分类 + 引擎接入 + 负对照。"""
    mod = get_engine()
    problems = []
    with tempfile.TemporaryDirectory(prefix="ci_plan_") as tmp:
        db = Path(tmp) / "plan.db"
        con = sqlite3.connect(str(db))
        con.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, v REAL); CREATE INDEX idx_t_name ON t(name);")
        con.executemany("INSERT INTO t VALUES (?,?,?)", [(i, "n%03d" % i, float(i)) for i in range(1, 201)])
        con.commit()
        con.close()
        pm = mod.sql_plan
        idx = pm.list_indexes(str(db))
        if "idx_t_name" not in (idx.get("t") or []):
            problems.append(f"索引发现失败: {idx}")
        pk = pm.analyze(str(db), "SELECT * FROM t WHERE id=1")
        if pk.get("n_search", 0) < 1 or not pk.get("used_index"):
            _d = pk.get("details")
            problems.append(f"主键查询应命中索引: {_d}")
        full = pm.analyze(str(db), "SELECT * FROM t")
        if full.get("n_full_scan", 0) < 1 or full.get("used_index"):
            _d = full.get("details")
            problems.append(f"无过滤查询应为全表扫描: {_d}")
        un = pm.analyze(str(db), "SELECT * FROM t WHERE v=1")
        if un.get("n_full_scan", 0) < 1 or un.get("used_index"):
            _d = un.get("details")
            problems.append(f"未索引列过滤应为全表扫描(负对照): {_d}")
        cov = pm.analyze(str(db), "SELECT name FROM t WHERE name LIKE \x27n1%\x27")
        if cov.get("n_index_scan", 0) < 1 or not cov.get("used_index"):
            _d = cov.get("details")
            problems.append(f"覆盖索引扫描应识别为索引: {_d}")
        agent = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0)
        agent._execute("SELECT id FROM t WHERE id=1")
        if (agent._query_plan or {}).get("n_search", 0) < 1:
            problems.append(f"引擎未接入查询计划: {agent._query_plan}")
    if problems:
        for p in problems:
            print(f"      {p}")
        return False, f"查询计划断言失败 {len(problems)} 处"
    return True, "索引发现/主键 SEARCH/全表 SCAN/未索引 SCAN(负对照)/覆盖索引 SCAN 分类正确; 引擎已接入"


def check_decompose():
    """任务分解(agentic 多步)断言: 桩 LLM 下 2 子问题各自执行 + 合成; 子问题失败则回退单轮。"""
    mod = get_engine()
    orig_json, orig_chat = mod.llm_json, mod.llm_chat
    problems = []

    def sj(system, user, api_key, max_tokens=2000):
        if "任务分解器" in system:
            return {"needed": True, "sub_questions": ["华东地区有多少客户?", "华南地区有多少客户?"]}
        if "问题理解器" in system:
            return {"answerable": True, "summary": "客户数", "metrics": ["客户数"], "dimensions": [], "filters": ["region"], "time_range": "", "missing": [], "question_specificity": "clear"}
        if "检索器" in system:
            return {"tables": [{"table": "customers", "columns": ["cust_id", "region"], "why": "计数"}]}
        if "查询计划器" in system:
            return {"summary": "计数", "tables": ["customers"], "filters": [{"field": "region", "op": "=", "value": "华东"}], "aggregations": [{"func": "COUNT", "field": "*", "alias": "客户数"}], "group_by": [], "order_by": [], "limit": None, "steps": ["计数"], "output_columns": [{"name": "客户数"}]}
        raise AssertionError("unexpected json prompt")

    Q = chr(39) + "华东" + chr(39)

    def sc(system, user, api_key, max_tokens=1500):
        if "结论合成器" in system:
            return "合成结论"
        if "解释器" in system:
            return "子结论"
        return "SELECT COUNT(*) AS 客户数 FROM customers WHERE region = " + Q

    mod.llm_json, mod.llm_chat = sj, sc
    try:
        with tempfile.TemporaryDirectory(prefix="ci_decomp_") as tmp:
            db = Path(tmp) / "synth.db"
            build_synth_db(db)
            ag = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0, decompose_mode="auto")
            ans = ag.run("华东和华南地区分别有多少客户?", clarify=False)
            if not ans.decomposed:
                problems.append("未触发任务分解")
            if len(ans.sub_answers) != 2:
                problems.append(f"子问题数不符: {len(ans.sub_answers)}")
            if not all(s.get("sql") for s in ans.sub_answers):
                problems.append(f"子问题缺少 SQL: {ans.sub_answers}")
            if not (ans.explanation or "").strip():
                problems.append("合成结论为空")

            def sj2(system, user, api_key, max_tokens=2000):
                if "任务分解器" in system:
                    return {"needed": True, "sub_questions": ["正常查询", "失败查询"]}
                if "问题理解器" in system and "失败" in user:
                    return {"answerable": False, "reason": "测试失败"}
                return sj(system, user, api_key, max_tokens)

            mod.llm_json = sj2
            ag2 = mod.QueryAgent(str(db), "ci-stub-key", verbose=False, sample_rows=0, decompose_mode="auto")
            ans2 = ag2.run("华东和华南地区分别有多少客户?", clarify=False)
            if ans2.decomposed:
                problems.append("子问题失败时应回退单轮, 但仍标记 decomposed")
            if not (ans2.sql or "").strip():
                problems.append("回退后未产出单轮 SQL")
    finally:
        mod.llm_json, mod.llm_chat = orig_json, orig_chat
    if problems:
        for p in problems:
            print(f"      {p}")
        return False, f"任务分解断言失败 {len(problems)} 处"
    return True, "2 子问题各自执行 + 合成; 子问题失败回退单轮(负对照)"


def check_ui():
    """交互层断言: 图表 SVG/CSV 导出生成 + 前端钩子 + Streamlit 图表/下载。"""
    srv = get_server_module()
    problems = []
    svg = srv.make_chart_svg(["等级", "客户数"], [["钻石", 5], ["白金", 3], ["金卡", 8]])
    if not (svg.startswith("<svg") and "<rect" in svg and "钻石" in svg):
        problems.append("make_chart_svg 未生成有效 SVG")
    if srv.make_chart_svg(["a"], [[1]]) != "":
        problems.append("单列不应绘图(负对照)")
    csv_text = srv.make_csv(["a", "b"], [[1, "x,y"], [2, "q,z"]])
    if (chr(34) + "x,y" + chr(34)) not in csv_text or (chr(34) + "q,z" + chr(34)) not in csv_text:
        problems.append("make_csv 转义失败")
    html = INDEX_HTML.read_text(encoding="utf-8")
    for kw in ("chartWrap", "csvLink", "chart_svg", "csv_url", "lastContext", "context="):
        if kw not in html:
            problems.append(f"index.html 缺少交互钩子: {kw}")
    srv_src = SERVER_PY.read_text(encoding="utf-8")
    for kw in ("make_chart_svg", "make_csv", "csv_url", "context"):
        if kw not in srv_src:
            problems.append(f"server.py 缺少: {kw}")
    app_src = (ROOT / "text2sql_demo" / "app.py").read_text(encoding="utf-8")
    for kw in ("bar_chart", "download_button"):
        if kw not in app_src:
            problems.append(f"app.py 缺少: {kw}")
    if problems:
        for pr in problems:
            print(f"      {pr}")
        return False, f"交互层断言失败 {len(problems)} 处"
    return True, "图表 SVG/CSV 导出生成正确(含单列负对照); 前端/服务端/Streamlit 钩子齐备"


CHECKS = [
    ("syntax", "语法编译", check_syntax),
    ("deps", "导入与无第三方包断言", check_deps),
    ("decouple", "引擎解耦断言(无券商域词)", check_decouple),
    ("judge", "判定器冻结断言", check_judge),
    ("secret", "密钥泄露防线", check_secret),
    ("synth-smoke", "合成库 + 桩 LLM 端到端冒烟", check_synth_smoke),
    ("sse", "SSE 接口契约冒烟", check_sse),
    ("privacy", "受限字段屏蔽断言", check_privacy),
    ("entities", "实体识别与校验断言", check_entities),
    ("sql-ast", "SQL 编译器前端断言", check_sql_ast),
    ("query-plan", "查询计划与索引断言", check_query_plan),
    ("schema-rag", "RAG schema linking 召回断言", check_schema_rag),
    ("decompose", "任务分解(agentic 多步)断言", check_decompose),
    ("ui", "交互层(图表/导出)断言", check_ui),
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
