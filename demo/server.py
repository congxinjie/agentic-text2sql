#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8 本地演示服务(纯标准库 http.server)。

启动:
    python demo/server.py --db <库路径> [--biz-context demo/enterprise_biz.json] [--port 8000]
                          [--sample-rows 2] [--log-level INFO]

--sample-rows 控制每表拼进 schema 的样例数据行数: 默认 2(与引擎一致); 0 = 完全不把样例数据
发给外部 LLM。/api/health 会回显实际生效值。

约定:
- 只绑 127.0.0.1, 不对外网开放。
- 问答链路完全复用 text2sql_demo/text2sql.py 的 QueryAgent(演示层不新造链路)。
- 业务口径只经 --biz-context 指向的 JSON 文件注入, 演示层不内嵌任何业务口径。
- API key 只在服务端经 text2sql.load_api_key() 读取, 绝不进入前端响应。
- 数据库只读由引擎保证(mode=ro + PRAGMA query_only=ON)。
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT2SQL_DIR = ROOT / "text2sql_demo"
sys.path.insert(0, str(TEXT2SQL_DIR))

import text2sql  # noqa: E402
from text2sql import QueryAgent  # noqa: E402

# M9: 演示层自己的轮转文件日志(与引擎同目录、不同文件, 避免多进程争抢同一个轮转文件)。
# 红线: 只记 长度/行数/表名/阶段名/耗时/异常, 不记 API 密钥与数据行内容。
LOG = text2sql.setup_logging(log_file=ROOT / "logs" / "demo_server.log",
                             level=os.environ.get("TEXT2SQL_LOG_LEVEL", "INFO"),
                             name="demo_server")


class DemoQueryAgent(QueryAgent):
    """包装 QueryAgent: 仅捕获引擎内部生成的查询计划供前端展示, 不改动任何阶段逻辑。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_plan = None

    def _plan(self, u, r):
        plan = super()._plan(u, r)
        self.last_plan = plan
        return plan

    def _replan(self, u, r, plan, issues):
        new_plan = super()._replan(u, r, plan, issues)
        self.last_plan = new_plan
        return new_plan


def resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.exists():
        alt = (base / raw).resolve()
        if alt.exists():
            return alt
    return p.resolve()


def load_biz_context_file(path: str | None):
    """从 JSON 文件注入业务口径; 不传则走引擎默认语义。"""
    if not path:
        return None, None, None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (data.get("biz_context"),
            data.get("sql_hints") or {},
            data.get("caliber_assertions") or [])


def matched_assertions(question: str, plan: dict | None, rules: list) -> list:
    """选择与本次问答相关的口径断言(仅用于前端展示; 判定机制仍在引擎内)。"""
    text = " ".join([question or "", json.dumps(plan or {}, ensure_ascii=False)])
    hits = []
    for rule in rules:
        when = rule.get("when") or []
        if when and all(str(k) in text for k in when):
            hits.append(rule)
        oc = rule.get("output_contract")
        if isinstance(oc, dict):
            when_any = [str(k) for k in (oc.get("when_any") or [])]
            if when_any and any(k in (question or "") for k in when_any):
                hits.append(rule)
    seen, out = set(), []
    for r in hits:
        key = json.dumps(r, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def run_ask(agent: QueryAgent, question: str, clarify: bool) -> dict:
    t0 = time.time()
    ans = agent.run(question, clarify=clarify)
    elapsed_s = round(time.time() - t0, 2)
    plan = getattr(agent, "last_plan", None)
    return {
        "question": question,
        "answerable": ans.answerable,
        "reject_reason": ans.reject_reason,
        "needs_clarification": ans.needs_clarification,
        "sql": ans.sql,
        "headers": ans.headers,
        "rows": ans.rows,
        "truncated": ans.truncated,
        "explanation": ans.explanation,
        "error": ans.error,
        "plan": plan,
        "caliber_assertions": matched_assertions(
            question, plan, getattr(agent, "caliber_assertions", []) or []),
        "trace": [{"stage": e.stage, "status": e.status,
                   "detail": e.detail, "elapsed": e.elapsed}
                  for e in ans.trace.entries],
        "elapsed_s": elapsed_s,
        "model": text2sql.MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "M8Demo/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self):
        body = self.server.index_html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ask(self, question: str, clarify: bool) -> dict:
        with self.server.agent_lock:
            try:
                payload = run_ask(self.server.agent, question, clarify)
                payload["ok"] = True
                # M9: 只记长度/行数/耗时/状态, 不记问题正文与结果数据
                LOG.info("[演示问答] 问题长度=%d clarify=%s 耗时=%.2fs 可答=%s 结果行数=%d 有错误=%s",
                         len(question), clarify, payload["elapsed_s"], payload["answerable"],
                         len(payload.get("rows") or []), bool(payload.get("error")))
                return payload
            except Exception as e:  # 服务端兜底: 前端只拿到可读错误, 不泄漏密钥
                LOG.error("[演示问答异常] 问题长度=%d 类型=%s 消息=%s",
                          len(question), type(e).__name__, str(e)[:160])
                traceback.print_exc(file=sys.stderr)
                return {"ok": False, "question": question,
                        "error": f"{type(e).__name__}: {e}"}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send_index()
        if parsed.path == "/api/health":
            return self._send_json({
                "ok": True,
                "model": text2sql.MODEL,
                "db": str(self.server.db_path),
                "biz_context_file": self.server.biz_context_file,
                "readonly": True,
                "sample_rows": self.server.agent.sample_rows,
            })
        if parsed.path == "/api/ask":
            qs = parse_qs(parsed.query)
            question = (qs.get("q") or [""])[0].strip()
            if not question:
                return self._send_json({"ok": False, "error": "缺少问题参数 q"}, 400)
            clarify = (qs.get("clarify") or ["false"])[0].lower() == "true"
            return self._send_json(self._ask(question, clarify))
        return self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/ask":
            return self._send_json({"ok": False, "error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return self._send_json({"ok": False, "error": "请求体为空或过大"}, 400)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(data.get("question") or "").strip()
            if not question:
                return self._send_json({"ok": False, "error": "缺少 question 字段"}, 400)
            clarify = bool(data.get("clarify", False))
        except (ValueError, UnicodeDecodeError) as e:
            return self._send_json({"ok": False, "error": f"请求解析失败: {e}"}, 400)
        return self._send_json(self._ask(question, clarify))

    def log_message(self, fmt, *args):
        sys.stderr.write("[demo] " + fmt % args + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="M8 本地演示服务(纯标准库, 只绑 127.0.0.1, 只读库)")
    ap.add_argument("--db", required=True, help="SQLite 库路径")
    ap.add_argument("--biz-context", default=None,
                    help="业务口径 JSON 文件(如 demo/enterprise_biz.json), 不传走引擎默认语义")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sample-rows", type=int, default=2,
                    help="每表拼进 schema 的样例数据行数(默认 2; 0 = 完全不把样例数据发给外部 LLM)")
    ap.add_argument("--log-level", default=os.environ.get("TEXT2SQL_LOG_LEVEL", "INFO"),
                    help="日志级别 DEBUG/INFO/WARNING/ERROR(默认 INFO)")
    args = ap.parse_args(argv)
    if args.sample_rows < 0:
        ap.error("--sample-rows 不能为负数(0 = 不发送样例数据)")
    # M9: 按命令行级别重启演示层日志 handler
    text2sql.setup_logging(log_file=ROOT / "logs" / "demo_server.log",
                           level=args.log_level, name="demo_server")

    db_path = resolve_path(args.db, ROOT)
    if not db_path.exists():
        ap.error(f"数据库不存在: {args.db}")
    db_path = db_path.resolve()

    biz_file = None
    if args.biz_context:
        biz_file = str(resolve_path(args.biz_context, ROOT).resolve())
    biz_context, sql_hints, caliber_assertions = load_biz_context_file(biz_file)

    api_key = text2sql.load_api_key()
    agent = DemoQueryAgent(str(db_path), api_key, verbose=False,
                           biz_context=biz_context,
                           sql_hints=sql_hints,
                           caliber_assertions=caliber_assertions,
                           sample_rows=args.sample_rows)

    index_html = (HERE / "index.html").read_bytes()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.agent = agent
    server.agent_lock = threading.Lock()
    server.db_path = db_path
    server.biz_context_file = biz_file
    server.index_html = index_html

    print(f"智能问数演示服务已启动: http://127.0.0.1:{args.port}", flush=True)
    print(f"库: {db_path} (只读, mode=ro + query_only)", flush=True)
    print(f"模型: {text2sql.MODEL}", flush=True)
    print(f"业务口径: {biz_file or '未注入(引擎默认语义)'}", flush=True)
    print(f"样例数据行数: {args.sample_rows}(0 = 不把样例数据发给外部 LLM)", flush=True)
    print(f"日志: {ROOT / 'logs' / 'demo_server.log'}(级别 {args.log_level})", flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    LOG.info("演示服务启动: 库=%s 端口=%d 模型=%s 样例数据行数=%d 业务口径=%s",
             db_path.name, args.port, text2sql.MODEL, args.sample_rows,
             biz_file or "未注入(引擎默认语义)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
